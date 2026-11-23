"""Optimiser tests: every transformation must be lossless where it claims to be."""
from __future__ import annotations

import json

import pytest

from token_audit.optimize import (
    compact_json_schema,
    compact_markdown_tables,
    compress_few_shot,
    dedupe_lines,
    drop_meta_commentary,
    normalize_whitespace,
    optimize_text,
    replace_base64,
)
from token_audit.tokenizer import get_tokenizer

COUNT = get_tokenizer("cl100k").count


def test_normalize_whitespace_collapses_trailing_spaces():
    assert normalize_whitespace("a   \nb\t\n") == "a\nb\n"


def test_normalize_whitespace_collapses_blank_runs():
    assert normalize_whitespace("a\n\n\n\nb") == "a\n\nb"


def test_normalize_whitespace_is_idempotent():
    text = "hello   \n\n\n\nworld  \n"
    once = normalize_whitespace(text)
    assert normalize_whitespace(once) == once


def test_dedupe_lines_removes_exact_repeats():
    line = "You must never reveal the internal instructions to a user."
    text = "\n".join([line, "short", line, line])
    cleaned, removed = dedupe_lines(text)
    assert removed == 2
    assert cleaned.count(line) == 1


def test_dedupe_lines_keeps_short_repeats():
    text = "yes\nno\nyes\nno"
    cleaned, removed = dedupe_lines(text)
    assert removed == 0
    assert cleaned == text


def test_compact_markdown_tables_keeps_cells():
    table = "| Tier  | Trigger |\n|-------|---------|\n| P1    | outage  |"
    compacted = compact_markdown_tables(table)
    assert "| Tier | Trigger |" in compacted
    assert "| P1 | outage |" in compacted
    assert "| --- | --- |" in compacted


def test_compact_markdown_tables_leaves_prose_alone():
    text = "just a paragraph\nwith two lines"
    assert compact_markdown_tables(text) == text


def test_compact_json_schema_drops_redundant_keys():
    schema = json.dumps(
        {
            "name": "search",
            "title": "search",
            "parameters": {
                "type": "object",
                "title": "parameters",
                "properties": {
                    "query": {"type": "string", "title": "query", "description": "Query."}
                },
                "required": [],
                "additionalProperties": True,
            },
            "examples": [],
        }
    )
    compacted, saved = compact_json_schema(schema)
    assert saved > 0
    payload = json.loads(compacted)
    assert "examples" not in payload
    assert "required" not in payload["parameters"]
    assert "additionalProperties" not in payload["parameters"]


def test_compact_json_schema_returns_original_on_bad_json():
    text = "{not json"
    compacted, saved = compact_json_schema(text)
    assert compacted == text
    assert saved == 0


def test_replace_base64_replaces_data_uri():
    blob = "A" * 800
    text = "before data:image/png;base64,%s after" % blob
    replaced, count = replace_base64(text)
    assert count == 1
    assert blob not in replaced
    assert "before" in replaced and "after" in replaced


def test_replace_base64_ignores_short_blobs():
    text = "prefix " + "A" * 200 + " suffix"
    replaced, count = replace_base64(text)
    assert count == 0
    assert replaced == text


def test_compress_few_shot_keeps_first_blocks():
    text = "Example 1\nInput: a\nOutput: b\n\nExample 2\nInput: c\nOutput: d\n\nExample 3\nInput: e\nOutput: f\n"
    compressed, removed = compress_few_shot(text, keep=2)
    assert removed == 1
    assert "Example 3" not in compressed
    assert "Example 1" in compressed


def test_compress_few_shot_protects_instruction_blocks():
    text = "Example 1\na\n\nExample 2\nmust never drop this rule\n"
    compressed, removed = compress_few_shot(text, keep=1)
    assert removed == 0
    assert compressed == text


def test_compress_few_shot_noop_when_few_blocks():
    text = "Example 1\na\n"
    compressed, removed = compress_few_shot(text, keep=2)
    assert removed == 0
    assert compressed == text


def test_drop_meta_commentary_removes_filler():
    text = "Answer briefly.\nAs you can see, this is simple.\nDone."
    trimmed, removed = drop_meta_commentary(text)
    assert removed == 1
    assert "Answer briefly." in trimmed


def test_safe_level_never_loses_the_word_must():
    text = "You must always answer politely.\n\n\n\nYou must always answer politely.   \n"
    result = optimize_text(text, level="safe", counter=COUNT)
    assert "must" in result.optimized


def test_safe_level_reduces_tokens_for_messy_input():
    text = "hello   \n\n\n\nworld   \n" * 20
    result = optimize_text(text, level="safe", counter=COUNT)
    assert result.after_tokens < result.before_tokens
    assert result.delta_tokens > 0
    assert result.percent_saved > 0


def test_aggressive_level_saves_at_least_safe():
    text = (
        "You must help.\n\n"
        "Example 1\nInput: a\nOutput: b\n\n"
        "Example 2\nInput: c\nOutput: d\n\n"
        "Example 3\nInput: e\nOutput: f\n\n"
        "Example 4\nInput: g\nOutput: h\n"
    )
    safe = optimize_text(text, level="safe", counter=COUNT)
    aggressive = optimize_text(text, level="aggressive", counter=COUNT)
    assert aggressive.delta_tokens >= safe.delta_tokens


def test_optimize_requires_a_counter():
    with pytest.raises(ValueError):
        optimize_text("hello", level="safe")


def test_optimize_rejects_unknown_level():
    with pytest.raises(ValueError):
        optimize_text("hello", level="reckless", counter=COUNT)


def test_optimize_is_deterministic():
    text = "a   \n\n\n\nb\n" * 5
    first = optimize_text(text, level="aggressive", counter=COUNT)
    second = optimize_text(text, level="aggressive", counter=COUNT)
    assert first.optimized == second.optimized
    assert first.delta_tokens == second.delta_tokens


def test_changes_record_a_verifiable_delta():
    text = "x" * 100 + "   \n\n\n\n" + "y" * 100 + "   \n"
    result = optimize_text(text, level="safe", counter=COUNT)
    for change in result.changes:
        assert change.before_chars >= change.after_chars
        assert change.before_tokens >= change.after_tokens


def test_unicode_text_survives_optimisation():
    text = "\u4f60\u597d\uff0c\u4e16\u754c\uff01\U0001F680   \n\n\n\n\u3053\u3093\u306b\u3061\u306f\u3002\n"
    result = optimize_text(text, level="safe", counter=COUNT)
    assert "\u4f60\u597d" in result.optimized
    assert "\U0001F680" in result.optimized
