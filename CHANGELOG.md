# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-23

### Added

- `token-audit scan` for files and directories: loads prompts, tool schemas,
  OpenAI/Anthropic JSONL chat exports and markdown, counts tokens, projects cost
  per 1,000 calls and per month from a committed pricing snapshot, and lists the
  most expensive strings.
- A from-scratch BPE replay tokenizer driven by two committed merge tables
  (cl100k-style and o200k-style) plus a character/byte heuristic fallback.
- `token-audit accuracy`: measures the tokenizer against a committed 200-sample
  golden set and reports total, median and p95 error plus exact-match count.
- Eight waste detectors: duplicated system prompts, MinHash near-duplicate
  fragments, example-heavy prompts, verbose JSON schemas, repeated chat
  boilerplate, base64 blobs, markdown table padding and whitespace noise.
- `token-audit optimize` with `safe` and `aggressive` levels, a per-step token
  ledger, unified diff output, in-place rewriting and JSON output.
- `token-audit budget`: monthly/yearly projection per scenario and model, plus
  sensitivity tables for cache-hit ratio and output ratio.
- `token-audit report --html`: deterministic, offline HTML with inline SVG cost
  bars.
- `token-audit models`: lists the pricing snapshot and its date.
- `benchmarks/run.py`, which measures throughput and accuracy and rewrites the
  benchmark block in the README.

### Notes

- The tokenizer reproduces the reference pre-tokenizer split and replays the
  committed merge tables, so counts on the golden corpus and on the held-out
  corpus used during development are exact. Error is still possible on text
  unlike anything tested; the accuracy command is the honest way to check.
