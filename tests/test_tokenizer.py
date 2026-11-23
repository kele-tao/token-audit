"""Tokenizer accuracy and behavioural tests."""
from __future__ import annotations

import json
import pathlib

import pytest

from token_audit.accuracy import AccuracyError, load_golden, measure, measure_all
from token_audit.tokenizer import (
    ENCODINGS,
    HeuristicTokenizer,
    get_tokenizer,
    is_letter,
    is_number,
    pretokenize,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_empty_text_counts_zero():
    assert get_tokenizer("cl100k").count("") == 0


def test_ascii_word_is_one_token():
    assert get_tokenizer("cl100k").count("hello") == 1


def test_numbers_split_into_three_digit_groups():
    pieces = pretokenize("1234567")
    assert pieces == ["123", "456", "7"]


def test_contraction_is_its_own_piece_for_cl100k():
    pieces = pretokenize("don't", "cl100k")
    assert pieces == ["don", "'t"]


def test_contraction_is_glued_for_o200k():
    pieces = pretokenize("don't", "o200k")
    assert pieces == ["don't"]


def test_unicode_letters_are_not_ascii_only():
    assert is_letter(ord("\u4f60")) is True
    assert is_letter(ord("\u00e9")) is True
    assert is_letter(ord(" ")) is False
    assert is_number(ord("\u0660")) is True


def test_cjk_text_is_measured_not_rejected():
    tokens = get_tokenizer("cl100k").count("\u4f60\u597d\uff0c\u4e16\u754c")
    assert tokens >= 4


def test_emoji_are_measured():
    tokens = get_tokenizer("cl100k").count("\U0001F680\U0001F389")
    assert tokens >= 2


@pytest.mark.parametrize("encoding", sorted(ENCODINGS))
def test_golden_set_total_error_under_five_percent(encoding):
    report = measure(encoding)
    assert abs(report.total_error_pct) <= 5.0


@pytest.mark.parametrize("encoding", sorted(ENCODINGS))
def test_golden_set_has_two_hundred_samples(encoding):
    report = measure(encoding)
    assert len(report.samples) == 200
    assert report.expected_total > 0


def test_measure_all_covers_both_encodings():
    reports = measure_all()
    assert {report.encoding for report in reports} == {"cl100k", "o200k"}


def test_accuracy_report_exposes_percentiles():
    report = measure("cl100k")
    assert report.median_abs_error_pct >= 0.0
    assert report.p95_abs_error_pct >= report.median_abs_error_pct
    assert report.exact_matches <= len(report.samples)


def test_worst_samples_are_sorted_by_error():
    report = measure("cl100k")
    errors = [sample.absolute_error for sample in report.worst]
    assert errors == sorted(errors, reverse=True)


def test_unknown_encoding_raises_key_error():
    with pytest.raises(KeyError):
        get_tokenizer("nope-2000")


def test_heuristic_tokenizer_handles_ascii_and_unicode():
    estimator = HeuristicTokenizer()
    assert estimator.count("") == 0
    assert estimator.count("hello world") >= 2
    assert estimator.count("\u4f60\u597d\u4e16\u754c") >= 2


def test_golden_file_is_valid_json_with_metadata():
    golden = load_golden()
    assert golden["seed"] == 20260101
    assert set(golden["encodings"]) == {"cl100k", "o200k"}
    assert len(golden["samples"]) == 200


def test_malformed_golden_raises(tmp_path):
    broken = tmp_path / "golden.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(AccuracyError):
        load_golden(broken)


def test_missing_golden_falls_back_to_packaged_copy(tmp_path):
    golden = load_golden(tmp_path / "missing.json")
    assert len(golden["samples"]) == 200


def test_vocab_size_is_reported():
    assert get_tokenizer("cl100k").vocab_size > 90_000
    assert get_tokenizer("o200k").vocab_size > 150_000


def test_long_repetitive_text_is_fast_and_cached():
    tokenizer = get_tokenizer("cl100k")
    text = "The quick brown fox jumps over the lazy dog. " * 2000
    first = tokenizer.count(text)
    second = tokenizer.count(text)
    assert first == second
    assert first > 10_000


def test_newline_runs_are_grouped():
    pieces = pretokenize("a\n\nb")
    assert "\n\n" in pieces


def test_trailing_whitespace_at_end_of_text_is_kept():
    pieces = pretokenize("hello   ")
    assert pieces[-1].endswith(" ")


def test_json_sample_counts_both_encodings():
    payload = json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
    assert get_tokenizer("cl100k").count(payload) == get_tokenizer("o200k").count(payload)
