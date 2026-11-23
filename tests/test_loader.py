"""Loader tests: chat exports, markdown blocks, schemas and malformed input."""
from __future__ import annotations

import json
import pathlib

from token_audit.loader import (
    iter_source_files,
    load_chat_jsonl,
    load_file,
    load_json,
    message_text,
    split_markdown,
)
from token_audit.models import Kind


def test_openai_chat_jsonl_is_parsed(tmp_path):
    record = {
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
        ]
    }
    path = tmp_path / "chat.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    document = load_file(path, tmp_path)
    assert document.kind == "chat-jsonl"
    kinds = [segment.kind for segment in document.segments]
    assert kinds == [Kind.SYSTEM, Kind.USER, Kind.ASSISTANT]


def test_anthropic_style_messages_are_parsed():
    record = {
        "system": "You are a careful assistant.",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Explain BPE."}]},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Byte pair encoding merges frequent pairs."}],
            },
        ],
    }
    segments, notes = load_chat_jsonl(json.dumps(record), "a.jsonl")
    assert not notes
    assert segments[0].kind is Kind.SYSTEM
    assert "Explain BPE." in segments[1].text


def test_malformed_jsonl_lines_are_counted_not_fatal():
    text = '{"messages": [{"role": "user", "content": "hi"}]}\nnot json\n'
    segments, notes = load_chat_jsonl(text, "b.jsonl")
    assert len(segments) == 1
    assert "1 malformed JSONL line(s) skipped" in notes


def test_jsonl_without_messages_is_noted():
    segments, notes = load_chat_jsonl('{"foo": 1}\n', "c.jsonl")
    assert segments == []
    assert "no chat messages recognised" in notes[0]


def test_message_text_flattens_tool_use_blocks():
    content = [
        {"type": "text", "text": "Searching."},
        {"type": "tool_use", "input": {"query": "bpe", "top_k": 3}},
        {"type": "image", "source": {}},
    ]
    flattened = message_text(content)
    assert "Searching." in flattened
    assert "bpe" in flattened
    assert "[image]" in flattened


def test_message_text_handles_plain_string_and_none():
    assert message_text("plain") == "plain"
    assert message_text(None) == ""
    assert message_text(42) == ""


def test_markdown_splits_into_blocks():
    text = "# Title\n\nFirst paragraph.\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    segments = split_markdown(text, "x.md")
    assert len(segments) >= 3
    assert any(segment.kind is Kind.TABLE for segment in segments)
    assert all(segment.source == "x.md" for segment in segments)


def test_markdown_fenced_code_is_one_block():
    text = "Intro.\n\n" + chr(96) * 3 + "python\nx = 1\n\ny = 2\n" + chr(96) * 3 + "\n"
    segments = split_markdown(text)
    code = [segment for segment in segments if "x = 1" in segment.text]
    assert len(code) == 1
    assert "y = 2" in code[0].text


def test_markdown_example_blocks_detected():
    text = "Example 1\nInput: a\nOutput: b\n"
    segments = split_markdown(text)
    assert segments[0].kind is Kind.EXAMPLE


def test_tool_schema_json_is_flagged(tmp_path):
    schema = {
        "name": "search",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The search query."}},
        },
    }
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema), encoding="utf-8")
    document = load_file(path, tmp_path)
    assert "tool schema detected" in document.notes
    assert any(segment.text == "The search query." for segment in document.segments)


def test_invalid_json_falls_back_to_text():
    segments, notes = load_json("{broken", "bad.json")
    assert segments[0].text == "{broken"
    assert "invalid JSON" in notes[0]


def test_json_without_descriptions_serialises_whole_payload():
    segments, notes = load_json('{"a": 1, "b": 2}', "plain.json")
    assert len(segments) == 1
    assert segments[0].kind is Kind.SCHEMA
    assert notes == []


def test_empty_file_yields_no_segments(tmp_path):
    path = tmp_path / "empty.md"
    path.write_text("   \n\n", encoding="utf-8")
    document = load_file(path, tmp_path)
    assert document.segments == []


def test_binary_suffix_is_ignored(tmp_path):
    path = tmp_path / "image.bin"
    path.write_bytes(b"\x00\x01\x02")
    assert load_file(path, tmp_path) is None


def test_unicode_markdown_round_trips(tmp_path):
    path = tmp_path / "cjk.md"
    path.write_text(
        "# \u4f60\u597d\n\n\u3053\u3093\u306b\u3061\u306f\u3002\n", encoding="utf-8"
    )
    document = load_file(path, tmp_path)
    assert any("\u4f60\u597d" in segment.text for segment in document.segments)


def test_iter_source_files_skips_junk_directories(tmp_path):
    (tmp_path / "keep.md").write_text("x", encoding="utf-8")
    junk = tmp_path / "node_modules"
    junk.mkdir()
    (junk / "skip.js").write_text("x", encoding="utf-8")
    names = [path.name for path in iter_source_files(tmp_path)]
    assert names == ["keep.md"]


def test_iter_source_files_accepts_single_file(tmp_path):
    path = tmp_path / "one.txt"
    path.write_text("x", encoding="utf-8")
    assert list(iter_source_files(path)) == [path]


def test_load_file_uses_given_root_for_relative_paths(tmp_path):
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    path = nested / "note.md"
    path.write_text("hello", encoding="utf-8")
    document = load_file(path, tmp_path)
    assert document.path == str(pathlib.Path("a") / "b" / "note.md")
