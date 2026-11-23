"""Real prompt transformations used by both the detectors and the optimize command.

Every function is pure: text in, text out.  Nothing is approximated here, so a
detector can call an optimiser and measure the true token delta instead of
guessing one.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

BASE64_RE = re.compile(r"[A-Za-z0-9+/]{512,}={0,2}")
DATA_URI_RE = re.compile(r"data:([\w.+-]+/[\w.+-]+);base64,([A-Za-z0-9+/=]{200,})")
TRAILING_SPACE_RE = re.compile(r"[ \t]+$", re.MULTILINE)
BLANK_RUN_RE = re.compile(r"\n{3,}")
META_LINE_PREFIXES = (
    "in this example",
    "for example, consider",
    "as you can see",
    "let's walk through",
    "note that this example",
    "here is an example",
)
PROTECTED_WORDS = ("must", "never", "always", "do not", "don't", "required")


@dataclass
class Change:
    """One transformation step with a token delta."""

    name: str
    before_chars: int
    after_chars: int
    before_tokens: int
    after_tokens: int
    detail: str = ""

    @property
    def delta_tokens(self) -> int:
        """Tokens removed (negative means added)."""
        return self.before_tokens - self.after_tokens


@dataclass
class OptimizeResult:
    """The optimised text plus a ledger of what changed."""

    original: str
    optimized: str
    level: str
    before_tokens: int = 0
    after_tokens: int = 0
    changes: list[Change] = field(default_factory=list)

    @property
    def delta_tokens(self) -> int:
        """Total tokens removed across the pipeline."""
        return self.before_tokens - self.after_tokens

    @property
    def percent_saved(self) -> float:
        """Share of tokens removed, as a percentage."""
        if not self.before_tokens:
            return 0.0
        return 100.0 * self.delta_tokens / self.before_tokens


def normalize_whitespace(text: str) -> str:
    """Collapse trailing spaces, blank-line runs and indentation-only lines."""
    lines = [TRAILING_SPACE_RE.sub("", line) for line in text.splitlines()]
    joined = BLANK_RUN_RE.sub("\n\n", "\n".join(lines))
    if text.endswith("\n"):
        return joined.rstrip("\n") + "\n"
    return joined.rstrip("\n")


def dedupe_lines(text: str) -> tuple[str, int]:
    """Remove exact duplicate substantial lines, keeping the first occurrence.

    Returns the new text and how many duplicate lines were dropped.
    """
    seen: set[str] = set()
    output: list[str] = []
    removed = 0
    for line in text.splitlines():
        stripped = line.strip()
        if len(stripped) >= 40:
            if stripped in seen:
                removed += 1
                continue
            seen.add(stripped)
        output.append(line)
    joined = "\n".join(output)
    if text.endswith("\n") and not joined.endswith("\n"):
        joined += "\n"
    return joined, removed


def compact_markdown_tables(text: str) -> str:
    """Strip cosmetic padding from markdown tables without changing cells."""
    output: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 2:
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if all(cell and set(cell) <= set("-: ") for cell in cells):
                cells = ["---"] * len(cells)
            output.append("| " + " | ".join(cells) + " |")
        else:
            output.append(line)
    return "\n".join(output)


def compact_json_schema(text: str) -> tuple[str, int]:
    """Drop redundant schema keys and re-serialise canonically.

    Removes title values that duplicate the property name, empty required
    arrays, additionalProperties true, empty descriptions and empty example
    lists.  Returns the compacted JSON and bytes saved.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text, 0
    if not isinstance(payload, (dict, list)):
        return text, 0

    def prune(node, key: str = ""):
        if isinstance(node, dict):
            cleaned = {}
            for child_key, value in node.items():
                if child_key == "title" and isinstance(value, str) and value == key:
                    continue
                if child_key == "required" and value == []:
                    continue
                if child_key == "additionalProperties" and value is True:
                    continue
                if child_key == "description" and value == "":
                    continue
                if child_key == "examples" and value == []:
                    continue
                cleaned[child_key] = prune(value, child_key)
            return cleaned
        if isinstance(node, list):
            return [prune(item, key) for item in node]
        return node

    compact = json.dumps(prune(payload), ensure_ascii=False, indent=2, sort_keys=True)
    return compact, max(0, len(text) - len(compact))


def replace_base64(text: str, placeholder: str = "<base64 blob>") -> tuple[str, int]:
    """Replace long base64 runs and data URIs with a short placeholder."""
    replaced = 0

    def uri_sub(match: re.Match) -> str:
        nonlocal replaced
        replaced += 1
        return "data:%s;base64,%s" % (match.group(1), placeholder)

    def blob_sub(match: re.Match) -> str:
        nonlocal replaced
        replaced += 1
        return placeholder

    text = DATA_URI_RE.sub(uri_sub, text)
    text = BASE64_RE.sub(blob_sub, text)
    return text, replaced


def is_protected(line: str) -> bool:
    """True when a line carries an instruction that must survive compression."""
    lowered = line.lower()
    return any(word in lowered for word in PROTECTED_WORDS)


def compress_few_shot(text: str, keep: int = 2) -> tuple[str, int]:
    """Drop redundant few-shot example blocks, keeping instructions intact.

    A block starts at a line beginning with Example / Input: and ends at the
    next blank line.  Blocks beyond keep are removed unless they contain a
    protected instruction.  Returns the new text and blocks removed.
    """
    lines = text.splitlines()
    blocks: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        head = lines[index].strip().lower()
        if head.startswith(("example", "input:", "### example", "#### example")):
            end = index
            while end + 1 < len(lines) and lines[end + 1].strip() != "":
                end += 1
            blocks.append((index, end))
            index = end + 1
        else:
            index += 1
    if len(blocks) <= keep:
        return text, 0
    drop: set[int] = set()
    removed = 0
    for start, end in blocks[keep:]:
        if any(is_protected(line) for line in lines[start : end + 1]):
            continue
        drop.update(range(start, end + 1))
        removed += 1
    if not removed:
        return text, 0
    kept = [line for number, line in enumerate(lines) if number not in drop]
    return BLANK_RUN_RE.sub("\n\n", "\n".join(kept)).strip("\n"), removed


def drop_meta_commentary(text: str) -> tuple[str, int]:
    """Remove filler commentary lines that add no instruction and no example."""
    kept: list[str] = []
    removed = 0
    for line in text.splitlines():
        stripped = line.strip().lower()
        if stripped and stripped.startswith(META_LINE_PREFIXES) and not is_protected(line):
            removed += 1
            continue
        kept.append(line)
    return "\n".join(kept), removed


def optimize_text(text: str, level: str = "safe", counter=None) -> OptimizeResult:
    """Apply the safe or aggressive pipeline and record every step.

    counter is a callable returning the token count of a string; it is injected
    so this module stays independent of the tokenizer.
    """
    if level not in ("safe", "aggressive"):
        raise ValueError("level must be 'safe' or 'aggressive', not %r" % level)
    if counter is None:
        raise ValueError("optimize_text needs a token counter callable")

    result = OptimizeResult(
        original=text,
        optimized=text,
        level=level,
        before_tokens=counter(text),
        after_tokens=counter(text),
    )

    def add_step(name: str, before: str, after: str, detail: str = "") -> str:
        if before == after:
            return after
        result.changes.append(
            Change(
                name=name,
                before_chars=len(before),
                after_chars=len(after),
                before_tokens=counter(before),
                after_tokens=counter(after),
                detail=detail,
            )
        )
        return after

    current = text
    current = add_step("normalize whitespace", current, normalize_whitespace(current))
    deduped, dropped = dedupe_lines(current)
    current = add_step(
        "drop duplicate lines", current, deduped, "%d duplicate line(s)" % dropped
    )
    current = add_step(
        "compact markdown tables", current, compact_markdown_tables(current)
    )
    base64_text, blobs = replace_base64(current)
    current = add_step(
        "replace base64 blobs", current, base64_text, "%d blob(s) replaced" % blobs
    )
    schema_text, _saved = compact_json_schema(current)
    current = add_step("compact JSON schema", current, schema_text)

    if level == "aggressive":
        compressed, examples = compress_few_shot(current)
        current = add_step(
            "trim redundant few-shot examples",
            current,
            compressed,
            "%d example block(s) removed" % examples,
        )
        trimmed, meta = drop_meta_commentary(current)
        current = add_step(
            "drop meta commentary",
            current,
            trimmed,
            "%d filler line(s) removed" % meta,
        )

    # Preserve the file's trailing-newline convention so a diff stays clean.
    if text.endswith("\n") and not current.endswith("\n"):
        current += "\n"
    elif not text.endswith("\n") and current.endswith("\n"):
        current = current.rstrip("\n")
    result.optimized = current
    result.after_tokens = counter(current)
    return result
