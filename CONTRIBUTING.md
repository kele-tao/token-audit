# Contributing

Thanks for taking the time to look at this project. Small, focused pull
requests with a test are the easiest to merge.

## Development setup

```bash
git clone https://github.com/kele-tao/token-audit
cd token-audit
python -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q
ruff check .
```

The package has no runtime dependencies. `tiktoken` is only needed if you want
to regenerate the vendored vocabulary data or the golden set:

```bash
pip install tiktoken
python scripts/build_vocab_data.py
```

That script reads the published vocabulary metadata and writes
`src/token_audit/data/*.bin` plus `tests/data/golden_tokens.json`. Nothing in the
test suite touches the network, so a normal `pytest` run works offline.

## What a good pull request looks like

1. One behaviour change per pull request, with a test that fails before it.
2. `pytest -q` and `ruff check .` both clean.
3. If you change a detector, update its expected saving arithmetic in
   `tests/test_detectors.py` rather than loosening the assertion.
4. If you change the tokenizer or the data files, run
   `python benchmarks/run.py` and commit the regenerated
   `benchmarks/results.json` and README table.
5. No new runtime dependency without a paragraph in the pull request explaining
   why the standard library cannot do the job.

## Reporting a wrong token count

Open an issue with the exact input string (quote it with `repr()`), the model you
used, the count you expected, the count token-audit produced, and the output of
`token-audit accuracy`. Wrong counts are bugs and are treated as such.

## Code style

- Python 3.10+, type hints on public functions, no `eval`, no network calls.
- 100-column lines, standard library first.
- Deterministic output: no timestamps, no random ids, no locale-dependent
  formatting in anything a user sees.
- User-facing errors go through `cli._fail` so the exit code stays 2.
