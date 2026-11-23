"""Benchmarks for token-audit.

Everything here is measured, not estimated.  The script writes
benchmarks/results.json and rewrites the table inside README.md between the
BENCHMARK markers, so the numbers in the README always come from a real run:

    python benchmarks/run.py
"""
from __future__ import annotations

import json
import pathlib
import platform
import statistics
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from token_audit.accuracy import measure_all  # noqa: E402
from token_audit.scanner import TrafficProfile, scan  # noqa: E402
from token_audit.tokenizer import get_tokenizer  # noqa: E402

RESULTS = ROOT / "benchmarks" / "results.json"
README = ROOT / "README.md"
START = "<!-- BENCHMARK:START -->"
END = "<!-- BENCHMARK:END -->"

PROSE = (
    "You are a senior software engineer. Explain the tradeoff, show the smallest "
    "correct patch, then list the tests you would add. Never guess a build id. "
)


def make_corpus(size_bytes: int) -> str:
    """A deterministic mixed corpus of roughly size_bytes UTF-8 bytes."""
    pieces = [
        PROSE,
        "def parse_config(path):\n    with open(path, encoding='utf-8') as fh:\n"
        "        return json.load(fh)\n",
        "| model | input | output | context |\n| --- | --- | --- | --- |\n"
        "| gpt-4o | 2.50 | 10.00 | 128k |\n",
        '{"name": "search", "parameters": {"type": "object", "properties": '
        '{"query": {"type": "string", "description": "Search query."}}}}\n',
        "SELECT c.name, SUM(o.total) FROM customers c JOIN orders o "
        "ON o.customer_id = c.id GROUP BY c.name;\n",
        "- **Bullet**: a setting and its default value.\n",
        "Localisation note: \u4f60\u597d\uff0c\u8bf7\u5e2e\u6211\u603b\u7ed3\u8fd9\u6bb5\u6587\u5b57\u3002\n",
        "Emoji \U0001F680 \u2713 \u2192 \u2605 in a sentence about tokens.\n",
    ]
    out: list[str] = []
    total = 0
    index = 0
    # Nine ASCII pieces for every non-ASCII one: a realistic prompt estate is
    # overwhelmingly English, while CJK and emoji still exercise the Unicode
    # pre-tokenizer path.
    order = [0, 1, 2, 3, 4, 5, 0, 1, 2, 6, 0, 3, 1, 4, 7, 2, 0, 5, 1, 3]
    while total < size_bytes:
        piece = pieces[order[index % len(order)]]
        out.append(piece)
        total += len(piece.encode("utf-8"))
        index += 1
    return "".join(out)


def time_it(callable_, repeats: int = 3) -> float:
    """Best-of-N wall time in seconds."""
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        callable_()
        samples.append(time.perf_counter() - start)
    return statistics.median(samples)


def bench_tokenizer() -> dict:
    """Throughput of the replay tokenizer, cached and cold."""
    corpus = make_corpus(5 * 1024 * 1024)
    payload_bytes = len(corpus.encode("utf-8"))

    cold = get_tokenizer("o200k")
    start = time.perf_counter()
    cold_tokens = cold.count(corpus)
    cold_seconds = time.perf_counter() - start

    warm = get_tokenizer("cl100k")
    warm.count(corpus[:100_000])
    start = time.perf_counter()
    warm_tokens = warm.count(corpus)
    warm_seconds = time.perf_counter() - start
    return {
        "corpus_mb": round(payload_bytes / (1024 * 1024), 2),
        "cold_seconds": round(cold_seconds, 3),
        "cold_mb_per_second": round(payload_bytes / 1e6 / cold_seconds, 2),
        "warm_seconds": round(warm_seconds, 3),
        "warm_mb_per_second": round(payload_bytes / 1e6 / warm_seconds, 2),
        "o200k_tokens": cold_tokens,
        "cl100k_tokens": warm_tokens,
    }


def bench_scan() -> dict:
    """Wall time of a full scan over a directory holding 5 MB of prompts."""
    corpus = make_corpus(5 * 1024 * 1024)
    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "prompts"
        target.mkdir()
        chunk = len(corpus) // 25 + 1
        for index in range(25):
            body = corpus[index * chunk : (index + 1) * chunk]
            (target / ("prompt-%02d.md" % index)).write_text(body, encoding="utf-8")
        start = time.perf_counter()
        result = scan(target, traffic=TrafficProfile(calls_per_day=1000))
        elapsed = time.perf_counter() - start
    return {
        "files": result.files_scanned,
        "bytes": result.bytes_scanned,
        "seconds": round(elapsed, 3),
        "mb_per_second": round(result.bytes_scanned / 1e6 / elapsed, 2),
        "findings": len(result.findings),
    }


def bench_accuracy() -> list[dict]:
    """Tokenizer error against the committed golden set."""
    rows = []
    for report in measure_all():
        rows.append(
            {
                "encoding": report.encoding,
                "samples": len(report.samples),
                "expected_tokens": report.expected_total,
                "measured_tokens": report.actual_total,
                "total_error_pct": round(report.total_error_pct, 4),
                "median_abs_error_pct": round(report.median_abs_error_pct, 4),
                "p95_abs_error_pct": round(report.p95_abs_error_pct, 4),
                "exact_matches": report.exact_matches,
            }
        )
    return rows


def render_table(data: dict) -> str:
    """Markdown table for README, built only from measured values."""
    tok = data["tokenizer"]
    scan_info = data["scan"]
    lines = [
        "| measurement | value |",
        "| --- | --- |",
        "| corpus size | %s MB of mixed prose, code, JSON, SQL, CJK and emoji |"
        % tok["corpus_mb"],
        "| replay tokenizer, cold cache | %s s (%s MB/s) |"
        % (tok["cold_seconds"], tok["cold_mb_per_second"]),
        "| replay tokenizer, warm cache | %s s (%s MB/s) |"
        % (tok["warm_seconds"], tok["warm_mb_per_second"]),
        "| full scan of %s files / %s MB | %s s (%s MB/s) |"
        % (
            scan_info["files"],
            round(scan_info["bytes"] / 1e6, 2),
            scan_info["seconds"],
            scan_info["mb_per_second"],
        ),
        "| findings on the synthetic corpus | %d |" % scan_info["findings"],
        "",
        "| encoding | samples | reference tokens | measured tokens | total error | median error | exact |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in data["accuracy"]:
        lines.append(
            "| %s | %d | %s | %s | %+.2f%% | %.2f%% | %d/%d |"
            % (
                row["encoding"],
                row["samples"],
                format(row["expected_tokens"], ","),
                format(row["measured_tokens"], ","),
                row["total_error_pct"],
                row["median_abs_error_pct"],
                row["exact_matches"],
                row["samples"],
            )
        )
    return "\n".join(lines)


def update_readme(table: str) -> bool:
    """Replace the benchmark block in README.md when the markers exist."""
    if not README.exists():
        return False
    text = README.read_text(encoding="utf-8")
    if START not in text or END not in text:
        return False
    head, rest = text.split(START, 1)
    _old, tail = rest.split(END, 1)
    README.write_text(head + START + "\n" + table + "\n" + END + tail, encoding="utf-8")
    return True


def main() -> int:
    """Run every benchmark, write results and refresh the README table."""
    data = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "tokenizer": bench_tokenizer(),
        "scan": bench_scan(),
        "accuracy": bench_accuracy(),
    }
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    table = render_table(data)
    updated = update_readme(table)
    print(table)
    print()
    print("wrote %s (README updated: %s)" % (RESULTS.relative_to(ROOT), updated))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
