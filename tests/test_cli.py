"""CLI tests: exit codes, output formats, friendly errors and the examples."""
from __future__ import annotations

import json
import pathlib

import pytest

from token_audit.cli import main
from token_audit.report import render_html, render_json, render_markdown, render_terminal
from token_audit.scanner import ScanError, TrafficProfile, scan

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "examples" / "prompts"
CHAT = ROOT / "examples" / "chat.jsonl"
PLAN = ROOT / "examples" / "plan.json"


@pytest.fixture
def messy_dir(tmp_path):
    """A small directory with a duplicate system prompt and a base64 blob."""
    prompt = "You are a careful assistant that always cites its sources in full detail."
    (tmp_path / "a.md").write_text(prompt, encoding="utf-8")
    (tmp_path / "b.md").write_text(prompt, encoding="utf-8")
    (tmp_path / "c.md").write_text("inline data " + "A" * 700, encoding="utf-8")
    return tmp_path


def test_scan_examples_returns_zero(capsys):
    assert main(["scan", str(PROMPTS), "--no-color"]) == 0
    out = capsys.readouterr().out
    assert "WASTE FINDINGS" in out
    assert "token-audit scan" in out


def test_scan_fail_on_findings_exits_one(capsys):
    assert main(["scan", str(PROMPTS), "--no-color", "--fail-on-findings"]) == 1


def test_scan_clean_directory_exits_zero(tmp_path, capsys):
    (tmp_path / "clean.md").write_text("A single short prompt with nothing wrong.", encoding="utf-8")
    assert main(["scan", str(tmp_path), "--no-color", "--fail-on-findings"]) == 0


def test_scan_json_format_is_valid_json(capsys):
    assert main(["scan", str(PROMPTS), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["files_scanned"] == 6
    assert payload["findings"]
    assert "pricing_snapshot" in payload


def test_scan_markdown_format_has_tables(capsys):
    assert main(["scan", str(PROMPTS), "--format", "markdown"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# token-audit report")
    assert "| severity | detector |" in out


def test_scan_writes_out_file(tmp_path, capsys):
    target = tmp_path / "report.md"
    assert main(["scan", str(PROMPTS), "--format", "markdown", "--out", str(target)]) == 0
    assert target.exists()
    assert "wrote" in capsys.readouterr().out


def test_scan_unknown_model_is_usage_error(capsys):
    assert main(["scan", str(PROMPTS), "--model", "gpt-4o-mni"]) == 2
    err = capsys.readouterr().err
    assert "unknown model" in err
    assert "Did you mean" in err


def test_scan_missing_target_is_usage_error(capsys):
    assert main(["scan", "does/not/exist"]) == 2
    assert "no such file or directory" in capsys.readouterr().err


def test_scan_rejects_cache_hit_above_one(capsys):
    assert main(["scan", str(PROMPTS), "--cache-hit", "1.5"]) == 2
    assert "--cache-hit" in capsys.readouterr().err


def test_scan_rejects_zero_top(capsys):
    assert main(["scan", str(PROMPTS), "--top", "0"]) == 2
    assert "--top" in capsys.readouterr().err


def test_scan_rejects_negative_calls(capsys):
    assert main(["scan", str(PROMPTS), "--calls-per-day", "-5"]) == 2


def test_scan_chat_jsonl_finds_duplicate_system_prompt(capsys):
    assert main(["scan", str(CHAT), "--model", "gpt-4o", "--no-color"]) == 0
    out = capsys.readouterr().out
    assert "duplicate-system-prompt" in out
    assert "System prompt repeated 40 times" in out


def test_optimize_reports_token_delta(capsys):
    assert main(["optimize", str(PROMPTS / "onboarding.md"), "--level", "aggressive"]) == 0
    out = capsys.readouterr().out
    assert "tokens" in out
    assert "estimated saving" in out


def test_optimize_json_ledger_is_parseable(capsys):
    assert main(["optimize", str(PROMPTS / "rag_answer.md"), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tokens_after"] < payload["tokens_before"]
    assert payload["delta_tokens"] > 0
    assert payload["steps"]


def test_optimize_in_place_rewrites_file(tmp_path, capsys):
    target = tmp_path / "prompt.md"
    target.write_text("hello   \n\n\n\nworld   \n", encoding="utf-8")
    assert main(["optimize", str(target), "--level", "safe", "--in-place"]) == 0
    assert target.read_text(encoding="utf-8") == "hello\n\nworld\n"


def test_optimize_out_writes_copy(tmp_path, capsys):
    target = tmp_path / "prompt.md"
    out = tmp_path / "optimized.md"
    target.write_text("hello   \n\n\n\nworld   \n", encoding="utf-8")
    assert main(["optimize", str(target), "--out", str(out)]) == 0
    assert out.exists()
    assert target.read_text(encoding="utf-8") != out.read_text(encoding="utf-8")


def test_optimize_diff_shows_hunk(tmp_path, capsys):
    target = tmp_path / "prompt.md"
    target.write_text("Example 1\na\n\nExample 2\nb\n\nExample 3\nc\n", encoding="utf-8")
    assert main(["optimize", str(target), "--level", "aggressive", "--diff"]) == 0
    out = capsys.readouterr().out
    assert "@@" in out
    assert "-Example 3" in out


def test_optimize_missing_file_is_usage_error(capsys):
    assert main(["optimize", "nope.md"]) == 2
    assert "no such prompt file" in capsys.readouterr().err


def test_optimize_directory_is_usage_error(capsys):
    assert main(["optimize", str(PROMPTS)]) == 2
    assert "is a directory" in capsys.readouterr().err


def test_optimize_empty_file_is_usage_error(tmp_path, capsys):
    target = tmp_path / "empty.md"
    target.write_text("   \n", encoding="utf-8")
    assert main(["optimize", str(target)]) == 2
    assert "empty" in capsys.readouterr().err


def test_budget_table_renders(capsys):
    assert main(["budget", str(PLAN)]) == 0
    out = capsys.readouterr().out
    assert "MONTHLY PROJECTION" in out
    assert "SENSITIVITY" in out


def test_budget_json_renders(capsys):
    assert main(["budget", str(PLAN), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["currency"] == "USD"
    assert len(payload["rows"]) == 9
    assert payload["sensitivity"]


def test_budget_missing_plan_is_usage_error(capsys):
    assert main(["budget", "nope.json"]) == 2
    assert "no such plan file" in capsys.readouterr().err


def test_budget_malformed_plan_is_usage_error(tmp_path, capsys):
    broken = tmp_path / "plan.json"
    broken.write_text("{oops", encoding="utf-8")
    assert main(["budget", str(broken)]) == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_budget_missing_traffic_is_usage_error(tmp_path, capsys):
    plan = tmp_path / "plan.json"
    plan.write_text('{"models": ["gpt-4o"]}', encoding="utf-8")
    assert main(["budget", str(plan)]) == 2
    assert "traffic" in capsys.readouterr().err


def test_budget_rejects_zero_requests(tmp_path, capsys):
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "traffic": {
                    "requests_per_day": 0,
                    "input_tokens": 10,
                    "output_tokens": 5,
                }
            }
        ),
        encoding="utf-8",
    )
    assert main(["budget", str(plan)]) == 2
    assert "requests_per_day" in capsys.readouterr().err


def test_report_writes_html(tmp_path, capsys):
    out = tmp_path / "report.html"
    assert main(["report", str(PROMPTS), "--html", str(out)]) == 0
    page = out.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert "<svg" in page
    assert "Waste findings" in page or "Waste findings" in page.replace("&mdash;", "-")
    assert "wrote" in capsys.readouterr().out


def test_report_html_is_deterministic(tmp_path, capsys):
    first = tmp_path / "one.html"
    second = tmp_path / "two.html"
    main(["report", str(PROMPTS), "--html", str(first)])
    main(["report", str(PROMPTS), "--html", str(second)])
    assert first.read_bytes() == second.read_bytes()


def test_accuracy_command_passes(capsys):
    assert main(["accuracy"]) == 0
    out = capsys.readouterr().out
    assert "cl100k" in out and "o200k" in out


def test_accuracy_json_shape(capsys):
    assert main(["accuracy", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["samples"] == 200
    assert {item["encoding"] for item in payload["encodings"]} == {"cl100k", "o200k"}


def test_accuracy_single_encoding(capsys):
    assert main(["accuracy", "--encoding", "cl100k"]) == 0
    out = capsys.readouterr().out
    assert "o200k" not in out.split("golden set")[0]


def test_models_lists_snapshot(capsys):
    assert main(["models"]) == 0
    out = capsys.readouterr().out
    assert "gpt-4o" in out
    assert "claude-sonnet-4" in out


def test_models_json(capsys):
    assert main(["models", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["snapshot_date"]
    assert any(model["id"] == "gpt-4o" for model in payload["models"])


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert "token-audit 1.0.0" in capsys.readouterr().out


def test_unknown_command_exits_two(capsys):
    with pytest.raises(SystemExit) as info:
        main(["frobnicate"])
    assert info.value.code == 2


def test_scan_rejects_unknown_format(capsys):
    with pytest.raises(SystemExit) as info:
        main(["scan", str(PROMPTS), "--format", "yaml"])
    assert info.value.code == 2


def test_scan_result_is_deterministic():
    first = scan(str(PROMPTS))
    second = scan(str(PROMPTS))
    assert render_json(first) == render_json(second)
    assert render_terminal(first, use_colour=False) == render_terminal(second, use_colour=False)


def test_scan_missing_target_raises_scan_error(tmp_path):
    with pytest.raises(ScanError):
        scan(str(tmp_path / "missing"))


def test_traffic_profile_label_mentions_assumptions():
    label = TrafficProfile(calls_per_day=10, days=7, output_ratio=0.5).label()
    assert "10 calls/day" in label
    assert "50% output" in label


def test_render_html_contains_escaped_root(tmp_path):
    result = scan(str(PROMPTS))
    page = render_html(result)
    assert "<script" not in page
    assert "token-audit report" in page


def test_render_markdown_top_table_has_repeats_column():
    result = scan(str(PROMPTS))
    assert "| tokens | location | preview | repeats |" in render_markdown(result)


def test_scan_aggregates_repeated_strings():
    result = scan(str(CHAT), top=5)
    tokens, _segment, occurrences = result.top_segments[0]
    assert occurrences == 40
    assert tokens > 100
