"""Waste detectors.

Every detector takes the parsed documents plus a token counter and returns
Findings.  Each finding names its evidence, states a recommendation and
estimates a monthly saving from the committed pricing table.  Estimates are
labelled as estimates; the arithmetic lives in one place so it can be audited.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

from .models import Document, Finding, Kind, Segment, Severity
from .optimize import (
    compact_json_schema,
    compact_markdown_tables,
    normalize_whitespace,
)

SHINGLE_SIZE = 7
MINHASH_PERMUTATIONS = 96
NEAR_DUPLICATE_THRESHOLD = 0.55


class Counter:
    """Callable token counter with a memo, used by detectors."""

    def __init__(self, tokenize) -> None:
        self._tokenize = tokenize
        self._cache: dict[str, int] = {}

    def __call__(self, text: str) -> int:
        """Return the token count of text."""
        cached = self._cache.get(text)
        if cached is None:
            cached = self._tokenize(text)
            self._cache[text] = cached
        return cached


def saving_usd(tokens: int, model, calls_per_day: int, days: int) -> float:
    """Dollar saving for removing tokens from every call of a traffic profile."""
    return tokens * model.input / 1_000_000 * calls_per_day * days


def _first_words(text: str, words: int = 8) -> str:
    """A short human-readable excerpt used as evidence."""
    collapsed = " ".join(text.split())
    parts = collapsed.split(" ")
    excerpt = " ".join(parts[:words])
    if len(parts) > words:
        excerpt += " ..."
    return excerpt


def _truncate(text: str, limit: int = 84) -> str:
    """Shorten text for a one-line evidence string."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def detect_duplicate_system_prompts(
    documents: list[Document], counter: Counter, model, calls_per_day: int, days: int
) -> list[Finding]:
    """Find the same system prompt repeated across files and chat turns."""
    groups: dict[str, list[Segment]] = defaultdict(list)
    for document in documents:
        for segment in document.segments:
            if segment.kind is Kind.SYSTEM and len(segment.text.strip()) > 40:
                groups[segment.text.strip()].append(segment)
    findings: list[Finding] = []
    for text, segments in groups.items():
        if len(segments) < 2:
            continue
        tokens = counter(text)
        extra = tokens * (len(segments) - 1)
        findings.append(
            Finding(
                detector="duplicate-system-prompt",
                title="System prompt repeated %d times" % len(segments),
                severity=Severity.HIGH if len(segments) >= 3 else Severity.MEDIUM,
                wasted_tokens=extra,
                monthly_saving_usd=round(saving_usd(extra, model, calls_per_day, days), 2),
                evidence=tuple(
                    "%s line %d: %s" % (seg.source or "chat", segment_line, _truncate(text))
                    for seg, segment_line in (
                        (segment, segment.line) for segment in segments[:4]
                    )
                ),
                recommendation=(
                    "Serve one canonical system prompt from config or prompt cache "
                    "instead of embedding a copy per file and per conversation turn."
                ),
                fixable=False,
            )
        )
    return findings


_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_MASK64 = 0xFFFFFFFFFFFFFFFF
_WORD_HASHES: dict[str, int] = {}
_WORD_CACHE_LIMIT = 500_000


def hash_word(word: str) -> int:
    """FNV-1a over one word, memoised across calls."""
    cached = _WORD_HASHES.get(word)
    if cached is not None:
        return cached
    value = _FNV_OFFSET
    for byte in word.encode("utf-8"):
        value ^= byte
        value = (value * _FNV_PRIME) & _MASK64
    if len(_WORD_HASHES) < _WORD_CACHE_LIMIT:
        _WORD_HASHES[word] = value
    return value


def hash_words(words: list[str]) -> int:
    """FNV-1a over a word sequence; stable across processes and platforms."""
    value = _FNV_OFFSET
    for word in words:
        value ^= hash_word(word)
        value = (value * _FNV_PRIME) & _MASK64
    return value


def _shingles(text: str, size: int = SHINGLE_SIZE) -> set[int]:
    """Word-level shingle hashes (FNV-1a), computed from cached word hashes."""
    words = re.findall(r"[a-z0-9']+", text.lower())
    if not words:
        return set()
    hashes = [hash_word(word) for word in words]
    if len(hashes) < size:
        return {hash_words(words)}
    out: set[int] = set()
    add = out.add
    for index in range(len(hashes) - size + 1):
        value = _FNV_OFFSET
        for word_hash in hashes[index : index + size]:
            value ^= word_hash
            value = (value * _FNV_PRIME) & _MASK64
        add(value)
    return out


def minhash_signature(shingles: set[int], permutations: int = MINHASH_PERMUTATIONS) -> tuple[int, ...]:
    """MinHash signature: for each of N salts, the smallest (hash xor salt)."""
    signature = []
    for salt in range(permutations):
        salt_value = (salt + 1) * 0x9E3779B97F4A7C15 & 0xFFFFFFFFFFFFFFFF
        signature.append(min(value ^ salt_value for value in shingles))
    return tuple(signature)


def jaccard_from_signatures(left: tuple[int, ...], right: tuple[int, ...]) -> float:
    """Estimate Jaccard similarity from two MinHash signatures."""
    if not left or not right:
        return 0.0
    matches = sum(1 for a, b in zip(left, right, strict=True) if a == b)
    return matches / len(left)


def detect_near_duplicate_fragments(
    documents: list[Document], counter: Counter, model, calls_per_day: int, days: int
) -> list[Finding]:
    """Find near-duplicate prompt fragments across files with MinHash shingling."""
    candidates: list[tuple[str, Segment]] = []
    for document in documents:
        for segment in document.segments:
            if segment.kind in (Kind.TEXT, Kind.MARKDOWN, Kind.EXAMPLE) and counter(segment.text) >= 25:
                candidates.append((document.path, segment))
    if len(candidates) < 2:
        return []
    signatures = [
        minhash_signature(_shingles(segment.text)) for _path, segment in candidates
    ]
    findings: list[Finding] = []
    reported: set[int] = set()
    for left in range(len(candidates)):
        if left in reported:
            continue
        duplicates: list[int] = []
        for right in range(left + 1, len(candidates)):
            similarity = jaccard_from_signatures(signatures[left], signatures[right])
            if similarity >= NEAR_DUPLICATE_THRESHOLD:
                duplicates.append(right)
        if not duplicates:
            continue
        for index in duplicates:
            reported.add(index)
        longest = max(
            [counter(candidates[left][1].text)]
            + [counter(candidates[index][1].text) for index in duplicates]
        )
        wasted = longest * len(duplicates)
        evidence = [
            "%s: %s" % (candidates[left][0], _truncate(candidates[left][1].text, 70))
        ] + [
            "%s: %s" % (candidates[index][0], _truncate(candidates[index][1].text, 70))
            for index in duplicates[:3]
        ]
        findings.append(
            Finding(
                detector="near-duplicate-fragment",
                title="%d near-duplicate prompt fragment(s)" % (len(duplicates) + 1),
                severity=Severity.MEDIUM,
                wasted_tokens=wasted,
                monthly_saving_usd=round(saving_usd(wasted, model, calls_per_day, days), 2),
                evidence=tuple(evidence),
                recommendation=(
                    "Factor the shared fragment into one include and reference it; "
                    "MinHash similarity is measured on word 7-shingles, not on lines."
                ),
                fixable=False,
            )
        )
    return findings


def detect_verbose_few_shot(
    documents: list[Document], counter: Counter, model, calls_per_day: int, days: int
) -> list[Finding]:
    """Measure how much of each prompt is few-shot examples that could shrink."""
    findings: list[Finding] = []
    for document in documents:
        example_tokens = 0
        example_segments = 0
        total_tokens = 0
        for segment in document.segments:
            tokens = counter(segment.text)
            total_tokens += tokens
            lowered = segment.text.strip().lower()
            if segment.kind is Kind.EXAMPLE or lowered.startswith(
                ("example", "input:", "output:")
            ):
                example_tokens += tokens
                example_segments += 1
        if example_segments < 3 or not total_tokens:
            continue
        share = 100.0 * example_tokens / total_tokens
        if share < 30.0:
            continue
        wasted = int(example_tokens * 0.25)
        findings.append(
            Finding(
                detector="verbose-few-shot",
                title="Examples are %.0f%% of %s" % (share, document.path),
                severity=Severity.MEDIUM,
                wasted_tokens=wasted,
                monthly_saving_usd=round(saving_usd(wasted, model, calls_per_day, days), 2),
                evidence=(
                    "%d example block(s), %d of %d tokens"
                    % (example_segments, example_tokens, total_tokens),
                ),
                recommendation=(
                    "Keep two representative examples and drop the rest; "
                    "token-audit optimize --level aggressive shows the exact delta."
                ),
                fixable=True,
            )
        )
    return findings


def detect_verbose_json_schemas(
    documents: list[Document], counter: Counter, model, calls_per_day: int, days: int
) -> list[Finding]:
    """Measure how many tokens a tool schema can lose without losing semantics."""
    findings: list[Finding] = []
    for document in documents:
        if document.kind != "json" or "tool schema detected" not in document.notes:
            continue
        original = document.raw_text
        if not original.strip():
            continue
        compacted, _saved = compact_json_schema(original)
        if compacted == original:
            continue
        before = counter(original)
        after = counter(compacted)
        wasted = max(0, before - after)
        if wasted < 5:
            continue
        findings.append(
            Finding(
                detector="verbose-json-schema",
                title="Schema %s can lose %d tokens" % (document.path, wasted),
                severity=Severity.MEDIUM,
                wasted_tokens=wasted,
                monthly_saving_usd=round(saving_usd(wasted, model, calls_per_day, days), 2),
                evidence=(
                    "%d tokens -> %d tokens by pruning redundant keys and indentation"
                    % (before, after),
                ),
                recommendation=(
                    "Drop title fields that repeat the property name and empty "
                    "required arrays; send the schema with indent-free JSON."
                ),
                fixable=True,
            )
        )
    return findings


def detect_repeated_boilerplate(
    documents: list[Document], counter: Counter, model, calls_per_day: int, days: int
) -> list[Finding]:
    """Find boilerplate repeated across many turns of a chat history."""
    findings: list[Finding] = []
    for document in documents:
        if document.kind != "chat-jsonl":
            continue
        lines: dict[str, int] = defaultdict(int)
        for segment in document.segments:
            if segment.kind in (Kind.SYSTEM, Kind.TOOL):
                continue
            for line in segment.text.splitlines():
                stripped = line.strip()
                if len(stripped) >= 48:
                    lines[stripped] += 1
        repeated = {line: count for line, count in lines.items() if count >= 3}
        if not repeated:
            continue
        wasted = sum(counter(line) * (count - 1) for line, count in repeated.items())
        top = sorted(repeated.items(), key=lambda item: (-item[1], item[0]))[:3]
        findings.append(
            Finding(
                detector="repeated-boilerplate",
                title="%d boilerplate line(s) repeat in %s" % (len(repeated), document.path),
                severity=Severity.HIGH if wasted > 500 else Severity.MEDIUM,
                wasted_tokens=wasted,
                monthly_saving_usd=round(saving_usd(wasted, model, calls_per_day, days), 2),
                evidence=tuple(
                    "%dx %s" % (count, _truncate(line, 70)) for line, count in top
                ),
                recommendation=(
                    "Send the shared instruction once in the system message and "
                    "keep prior turns as a rolling summary instead of replaying them."
                ),
                fixable=False,
            )
        )
    return findings


def detect_base64_blobs(
    documents: list[Document], counter: Counter, model, calls_per_day: int, days: int
) -> list[Finding]:
    """Find inline base64 payloads that are billed like ordinary input tokens."""
    total_tokens = 0
    evidence: list[str] = []
    for document in documents:
        for segment in document.segments:
            text = segment.text
            blobs = re.findall(r"[A-Za-z0-9+/]{512,}={0,2}", text)
            for blob in blobs[:3]:
                tokens = counter(blob)
                total_tokens += tokens
                evidence.append(
                    "%s: %d base64 chars ~= %d tokens"
                    % (segment.source or document.path, len(blob), tokens)
                )
    if not evidence:
        return []
    return [
        Finding(
            detector="base64-blob",
            title="Inline base64 payloads in prompt context",
            severity=Severity.HIGH,
            wasted_tokens=total_tokens,
            monthly_saving_usd=round(saving_usd(total_tokens, model, calls_per_day, days), 2),
            evidence=tuple(evidence[:5]),
            recommendation=(
                "Replace the payload with a stable URL or a short handle; base64 "
                "costs roughly one token per three characters."
            ),
            fixable=True,
        )
    ]


def detect_markdown_tables(
    documents: list[Document], counter: Counter, model, calls_per_day: int, days: int
) -> list[Finding]:
    """Measure padding characters wasted by wide markdown tables in context."""
    total = 0
    evidence: list[str] = []
    for document in documents:
        for segment in document.segments:
            if segment.kind is not Kind.TABLE:
                continue
            compacted = compact_markdown_tables(segment.text)
            if compacted == segment.text:
                continue
            wasted = max(0, counter(segment.text) - counter(compacted))
            if wasted < 3:
                continue
            total += wasted
            evidence.append(
                "%s: %d tokens -> %d tokens after stripping cell padding"
                % (segment.source or document.path, counter(segment.text), counter(compacted))
            )
    if not evidence:
        return []
    return [
        Finding(
            detector="markdown-table-padding",
            title="Markdown table padding in context",
            severity=Severity.LOW,
            wasted_tokens=total,
            monthly_saving_usd=round(saving_usd(total, model, calls_per_day, days), 2),
            evidence=tuple(evidence[:5]),
            recommendation=(
                "Collapse padded cells before sending the table, or convert the "
                "table to CSV when the model only needs the values."
            ),
            fixable=True,
        )
    ]


def detect_whitespace_noise(
    documents: list[Document], counter: Counter, model, calls_per_day: int, days: int
) -> list[Finding]:
    """Count tokens spent on blank-line runs and padding the model does not need.

    Single trailing spaces are almost never worth a token because the
    pre-tokenizer folds them into the following whitespace piece; runs of three
    or more newlines and indentation-only lines are what actually cost tokens.
    """
    total = 0
    evidence: list[str] = []
    for document in documents:
        for segment in document.segments:
            normalized = normalize_whitespace(segment.text)
            if normalized == segment.text:
                continue
            if len(segment.text) - len(normalized) < 2:
                continue
            wasted = max(0, counter(segment.text) - counter(normalized))
            if wasted < 1:
                continue
            total += wasted
            evidence.append(
                "%s: %d token(s) recoverable from whitespace"
                % (segment.source or document.path, wasted)
            )
    if not evidence:
        return []
    return [
        Finding(
            detector="whitespace-noise",
            title="Redundant whitespace in prompt text",
            severity=Severity.INFO,
            wasted_tokens=total,
            monthly_saving_usd=round(saving_usd(total, model, calls_per_day, days), 2),
            evidence=tuple(evidence[:5]),
            recommendation=(
                "Run token-audit optimize --level safe before committing a prompt; "
                "the transform is lossless for the model's view of the text."
            ),
            fixable=True,
        )
    ]


ALL_DETECTORS = (
    detect_duplicate_system_prompts,
    detect_near_duplicate_fragments,
    detect_verbose_few_shot,
    detect_verbose_json_schemas,
    detect_repeated_boilerplate,
    detect_base64_blobs,
    detect_markdown_tables,
    detect_whitespace_noise,
)


def run_detectors(
    documents: list[Document],
    counter: Counter,
    model,
    calls_per_day: int,
    days: int,
) -> list[Finding]:
    """Run every detector and return findings in deterministic order."""
    findings: list[Finding] = []
    for detector in ALL_DETECTORS:
        findings.extend(detector(documents, counter, model, calls_per_day, days))
    findings.sort(key=lambda finding: finding.sort_key)
    return findings


def dedupe_key(finding: Finding) -> str:
    """Stable identity for a finding, used by the HTML report."""
    return json.dumps(
        [finding.detector, finding.title, finding.evidence[:2]], ensure_ascii=False
    )
