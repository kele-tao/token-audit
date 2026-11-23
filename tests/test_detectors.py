"""Detector tests: every finding must be reproducible from the input."""
from __future__ import annotations

import json

from token_audit.detectors import (
    Counter,
    detect_base64_blobs,
    detect_duplicate_system_prompts,
    detect_markdown_tables,
    detect_near_duplicate_fragments,
    detect_repeated_boilerplate,
    detect_verbose_few_shot,
    detect_verbose_json_schemas,
    detect_whitespace_noise,
    hash_words,
    jaccard_from_signatures,
    minhash_signature,
    run_detectors,
    saving_usd,
)
from token_audit.loader import split_markdown
from token_audit.models import Document, Kind, Segment, Severity
from token_audit.pricing import load_pricing
from token_audit.tokenizer import get_tokenizer

PRICING = load_pricing()
MODEL = PRICING.model("gpt-4o")
COUNT = Counter(get_tokenizer("o200k").count)
CALLS = 1000
DAYS = 30


def make_document(segments, path="a.md", kind="markdown", raw_text="", notes=None):
    return Document(
        path=path,
        kind=kind,
        size_bytes=1,
        segments=segments,
        notes=notes or [],
        raw_text=raw_text,
    )


def test_saving_usd_matches_hand_arithmetic():
    tokens = 1_000_000
    expected = MODEL.input * CALLS * DAYS
    assert saving_usd(tokens, MODEL, CALLS, DAYS) == expected


def test_duplicate_system_prompt_detected_once():
    text = "You are a careful assistant that always cites its sources in full."
    documents = [
        make_document([Segment(text=text, kind=Kind.SYSTEM, source="a.md")]),
        make_document([Segment(text=text, kind=Kind.SYSTEM, source="b.md")]),
    ]
    findings = detect_duplicate_system_prompts(documents, COUNT, MODEL, CALLS, DAYS)
    assert len(findings) == 1
    assert findings[0].detector == "duplicate-system-prompt"
    assert findings[0].wasted_tokens == COUNT(text)


def test_duplicate_system_prompt_needs_two_copies():
    document = make_document([Segment(text="A short system prompt.", kind=Kind.SYSTEM)])
    assert detect_duplicate_system_prompts([document], COUNT, MODEL, CALLS, DAYS) == []


def test_duplicate_system_prompt_severity_scales_with_copies():
    text = "You are a careful assistant that always cites its sources in full detail."
    documents = [
        make_document([Segment(text=text, kind=Kind.SYSTEM, source="f%d.md" % index)])
        for index in range(3)
    ]
    findings = detect_duplicate_system_prompts(documents, COUNT, MODEL, CALLS, DAYS)
    assert findings[0].severity is Severity.HIGH


def test_hash_words_is_stable_and_order_sensitive():
    assert hash_words(["a", "b"]) == hash_words(["a", "b"])
    assert hash_words(["a", "b"]) != hash_words(["b", "a"])


def test_minhash_signature_similarity_is_high_for_near_duplicates():
    base = " ".join("word%d" % index for index in range(60))
    variant = base + " extra"
    base_words = base.split()
    variant_words = variant.split()
    left = minhash_signature({hash_words(base_words[i : i + 7]) for i in range(53)})
    right = minhash_signature({hash_words(variant_words[i : i + 7]) for i in range(53)})
    assert jaccard_from_signatures(left, right) > 0.5


def test_jaccard_is_zero_for_empty_signature():
    assert jaccard_from_signatures((), (1, 2, 3)) == 0.0


def test_near_duplicate_fragments_found_across_files():
    text = " ".join("token%d" % index for index in range(120))
    documents = [
        make_document([Segment(text=text, kind=Kind.MARKDOWN, source="a.md")], path="a.md"),
        make_document(
            [Segment(text=text + " trailing", kind=Kind.MARKDOWN, source="b.md")],
            path="b.md",
        ),
    ]
    findings = detect_near_duplicate_fragments(documents, COUNT, MODEL, CALLS, DAYS)
    assert len(findings) == 1
    assert findings[0].wasted_tokens > 0


def test_near_duplicate_ignores_short_fragments():
    documents = [
        make_document([Segment(text="short prompt", kind=Kind.MARKDOWN, source="a.md")]),
        make_document([Segment(text="short prompt", kind=Kind.MARKDOWN, source="b.md")]),
    ]
    assert detect_near_duplicate_fragments(documents, COUNT, MODEL, CALLS, DAYS) == []


def test_near_duplicate_ignores_system_segments():
    text = " ".join("token%d" % index for index in range(120))
    documents = [
        make_document([Segment(text=text, kind=Kind.SYSTEM, source="a.md")]),
        make_document([Segment(text=text, kind=Kind.SYSTEM, source="b.md")]),
    ]
    assert detect_near_duplicate_fragments(documents, COUNT, MODEL, CALLS, DAYS) == []


def test_verbose_few_shot_flags_example_heavy_prompt():
    segments = [
        Segment(text="You are a reviewer. Follow the checklist.", kind=Kind.MARKDOWN),
    ] + [
        Segment(
            text="Example %d\nInput: a very long input string with plenty of detail.\n"
            "Output: a very long output string with plenty of detail." % index,
            kind=Kind.EXAMPLE,
        )
        for index in range(4)
    ]
    findings = detect_verbose_few_shot([make_document(segments)], COUNT, MODEL, CALLS, DAYS)
    assert len(findings) == 1
    assert findings[0].detector == "verbose-few-shot"
    assert findings[0].fixable is True


def test_verbose_few_shot_ignores_prose_prompt():
    segments = [Segment(text="Just a paragraph of instructions.", kind=Kind.MARKDOWN)]
    assert detect_verbose_few_shot([make_document(segments)], COUNT, MODEL, CALLS, DAYS) == []


def test_verbose_json_schema_reports_tokens_saved():
    schema = json.dumps(
        {
            "name": "lookup",
            "parameters": {
                "type": "object",
                "title": "parameters",
                "properties": {
                    "query": {"type": "string", "title": "query", "description": "Query text."}
                },
                "required": [],
                "additionalProperties": True,
            },
            "examples": [],
        }
    )
    document = make_document([], kind="json", raw_text=schema, notes=["tool schema detected"])
    findings = detect_verbose_json_schemas([document], COUNT, MODEL, CALLS, DAYS)
    assert len(findings) == 1
    assert findings[0].wasted_tokens > 0


def test_verbose_json_schema_ignores_non_schemas():
    document = make_document([], kind="json", raw_text='{"a": 1}', notes=[])
    assert detect_verbose_json_schemas([document], COUNT, MODEL, CALLS, DAYS) == []


def test_repeated_boilerplate_counts_extra_copies():
    boilerplate = "Never share the customer name outside the ticket, ever."
    segments = [Segment(text=boilerplate, kind=Kind.USER) for _ in range(4)]
    findings = detect_repeated_boilerplate(
        [make_document(segments, kind="chat-jsonl")], COUNT, MODEL, CALLS, DAYS
    )
    assert len(findings) == 1
    assert findings[0].wasted_tokens == COUNT(boilerplate) * 3


def test_repeated_boilerplate_extracts_even_from_long_answers():
    line = "This sentence is long enough to count as boilerplate for the detector."
    segment = Segment(text="\n".join([line] * 5), kind=Kind.ASSISTANT)
    findings = detect_repeated_boilerplate(
        [make_document([segment], kind="chat-jsonl")], COUNT, MODEL, CALLS, DAYS
    )
    assert len(findings) == 1


def test_repeated_boilerplate_ignores_lore():
    document = make_document([Segment(text="hello", kind=Kind.USER)], kind="chat-jsonl")
    assert detect_repeated_boilerplate([document], COUNT, MODEL, CALLS, DAYS) == []


def test_base64_blob_detected_and_sized():
    blob = "A" * 700
    document = make_document([Segment(text="inline " + blob, kind=Kind.MARKDOWN, source="a.md")])
    findings = detect_base64_blobs([document], COUNT, MODEL, CALLS, DAYS)
    assert len(findings) == 1
    assert findings[0].wasted_tokens == COUNT(blob)


def test_base64_absent_returns_nothing():
    document = make_document([Segment(text="plain text without payloads", kind=Kind.TEXT)])
    assert detect_base64_blobs([document], COUNT, MODEL, CALLS, DAYS) == []


def test_markdown_table_padding_detected():
    table = "| Tier | Trigger |\n|------|---------|\n| P1   | outage  |\n| P2   | slow    |"
    document = make_document([Segment(text=table, kind=Kind.TABLE)])
    findings = detect_markdown_tables([document], COUNT, MODEL, CALLS, DAYS)
    assert len(findings) == 1
    assert findings[0].wasted_tokens >= 1


def test_whitespace_noise_detected_on_blank_runs():
    document = make_document([Segment(text="alpha   \n\n\n\nbeta   \n", kind=Kind.TEXT)])
    findings = detect_whitespace_noise([document], COUNT, MODEL, CALLS, DAYS)
    assert len(findings) == 1
    assert findings[0].wasted_tokens >= 1


def test_whitespace_noise_absent_returns_nothing():
    document = make_document([Segment(text="line one\nline two\n", kind=Kind.TEXT)])
    assert detect_whitespace_noise([document], COUNT, MODEL, CALLS, DAYS) == []


def test_run_detectors_is_deterministic_and_sorted():
    text = "You are a careful assistant that always cites its sources in full detail."
    documents = [
        make_document([Segment(text=text, kind=Kind.SYSTEM, source="f%d.md" % index)])
        for index in range(3)
    ]
    first = run_detectors(documents, COUNT, MODEL, CALLS, DAYS)
    second = run_detectors(documents, COUNT, MODEL, CALLS, DAYS)
    assert [finding.title for finding in first] == [finding.title for finding in second]
    assert first[0].monthly_saving_usd >= first[-1].monthly_saving_usd


def test_split_markdown_feeds_duplicate_detector():
    text = "One paragraph.\n\n" + " ".join("word%d" % index for index in range(80)) + "\n"
    segments = split_markdown(text, "x.md")
    assert any(segment.kind is Kind.TABLE for segment in segments) is False
    assert len(segments) == 2
