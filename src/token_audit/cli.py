"""Command line interface for token-audit.

Exit codes
----------
0  success, including a scan that found nothing
1  the scan found waste, or a gate failed
2  usage error: bad flag, missing file, malformed plan, unknown model
"""
from __future__ import annotations

import argparse
import difflib
import json
import pathlib
import sys

from . import __version__
from .accuracy import AccuracyError, accuracy_json, measure_all, render_accuracy
from .budget import BudgetError, build_budget, load_plan, render_budget, render_budget_json
from .optimize import optimize_text
from .pricing import PricingError, load_pricing
from .report import render_html, render_json, render_markdown, render_terminal
from .scanner import ScanError, TrafficProfile, scan
from .tokenizer import ENCODINGS, get_tokenizer

PROGRAM = "token-audit"
EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2


def _fail(message: str) -> int:
    """Print a friendly error and return the usage exit code."""
    sys.stderr.write("%s: error: %s\n" % (PROGRAM, message))
    return EXIT_USAGE


class Parser(argparse.ArgumentParser):
    """argparse subclass that keeps usage errors at exit code 2."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        sys.stderr.write("%s: error: %s\n" % (PROGRAM, message))
        raise SystemExit(EXIT_USAGE)


def build_parser() -> Parser:
    """Build the argument parser with every documented command."""
    parser = Parser(
        prog=PROGRAM,
        description=(
            "Find wasted tokens and model spend in prompts, tool schemas and "
            "chat logs, then optimise them with verifiable before/after deltas."
        ),
        epilog=(
            "exit codes: 0 ok, 1 findings or failed gate, 2 usage error. "
            "Quick start: token-audit scan examples/prompts"
        ),
    )
    parser.add_argument("--version", action="version", version=PROGRAM + " " + __version__)
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    scan_parser = sub.add_parser("scan", help="scan a file or directory for wasted tokens")
    scan_parser.add_argument("target", help="file or directory to scan")
    scan_parser.add_argument(
        "--model", default="gpt-4o", help="model id used for cost math (default: gpt-4o)"
    )
    scan_parser.add_argument(
        "--calls-per-day", type=int, default=1000,
        help="calls per day for the monthly projection (default: 1000)",
    )
    scan_parser.add_argument(
        "--days", type=int, default=30, help="days per month in the projection (default: 30)"
    )
    scan_parser.add_argument(
        "--output-ratio", type=float, default=0.35,
        help="output tokens as a fraction of input tokens (default: 0.35)",
    )
    scan_parser.add_argument(
        "--cache-hit", type=float, default=0.0,
        help="share of input tokens served from cache, 0-1 (default: 0)",
    )
    scan_parser.add_argument(
        "--top", type=int, default=20, help="how many expensive strings to list (default: 20)"
    )
    scan_parser.add_argument(
        "--format", choices=("terminal", "markdown", "json"), default="terminal",
        help="output format (default: terminal)",
    )
    scan_parser.add_argument("--out", default=None, help="write the rendered report to a file")
    scan_parser.add_argument(
        "--fail-on-findings", action="store_true",
        help="exit with code 1 when any waste is detected",
    )
    scan_parser.add_argument(
        "--no-color", action="store_true", help="disable ANSI colour in terminal output"
    )
    scan_parser.add_argument(
        "--pricing", default=None, help="path to an alternative pricing JSON file"
    )
    scan_parser.set_defaults(handler=cmd_scan)

    optimize_parser = sub.add_parser(
        "optimize", help="rewrite a prompt and report the token delta"
    )
    optimize_parser.add_argument("target", help="prompt file to optimise")
    optimize_parser.add_argument(
        "--level", choices=("safe", "aggressive"), default="safe",
        help="transformation level (default: safe)",
    )
    optimize_parser.add_argument(
        "--model", default="gpt-4o", help="model used for the saving estimate (default: gpt-4o)"
    )
    optimize_parser.add_argument(
        "--in-place", action="store_true", help="write the optimised text back to the file"
    )
    optimize_parser.add_argument("--out", default=None, help="write the optimised text here")
    optimize_parser.add_argument("--diff", action="store_true", help="show a unified diff")
    optimize_parser.add_argument(
        "--calls-per-day", type=int, default=1000, help="calls per day for the saving estimate"
    )
    optimize_parser.add_argument("--json", action="store_true", help="emit a JSON ledger")
    optimize_parser.set_defaults(handler=cmd_optimize)

    budget_parser = sub.add_parser(
        "budget", help="project monthly spend for a traffic scenario"
    )
    budget_parser.add_argument("plan", help="plan JSON file, e.g. examples/plan.json")
    budget_parser.add_argument("--json", action="store_true", help="emit JSON")
    budget_parser.add_argument(
        "--pricing", default=None, help="path to an alternative pricing JSON file"
    )
    budget_parser.set_defaults(handler=cmd_budget)

    report_parser = sub.add_parser(
        "report", help="render a scan as a self-contained HTML report"
    )
    report_parser.add_argument("target", help="file or directory to scan")
    report_parser.add_argument("--html", required=True, help="path of the HTML file to write")
    report_parser.add_argument("--model", default="gpt-4o", help="model id for the cost math")
    report_parser.add_argument("--calls-per-day", type=int, default=1000, help="calls per day")
    report_parser.add_argument("--top", type=int, default=20, help="expensive strings to list")
    report_parser.add_argument(
        "--pricing", default=None, help="path to an alternative pricing JSON file"
    )
    report_parser.set_defaults(handler=cmd_report)

    accuracy_parser = sub.add_parser(
        "accuracy", help="measure tokenizer error against the committed golden set"
    )
    accuracy_parser.add_argument(
        "--encoding", choices=sorted(ENCODINGS), default=None,
        help="only measure this encoding",
    )
    accuracy_parser.add_argument("--json", action="store_true", help="emit JSON")
    accuracy_parser.set_defaults(handler=cmd_accuracy)

    models_parser = sub.add_parser(
        "models", help="list the models in the committed pricing snapshot"
    )
    models_parser.add_argument("--json", action="store_true", help="emit JSON")
    models_parser.add_argument(
        "--pricing", default=None, help="path to an alternative pricing JSON file"
    )
    models_parser.set_defaults(handler=cmd_models)

    return parser


def _traffic(args) -> TrafficProfile:
    """Build a traffic profile from parsed flags."""
    return TrafficProfile(
        calls_per_day=args.calls_per_day,
        days=args.days,
        output_ratio=args.output_ratio,
        cache_hit_ratio=args.cache_hit,
    )


def cmd_scan(args) -> int:
    """Run a scan and render the report."""
    if args.calls_per_day < 0:
        return _fail("--calls-per-day cannot be negative")
    if args.days < 1 or args.days > 366:
        return _fail("--days must be between 1 and 366")
    if not 0.0 <= args.cache_hit <= 1.0:
        return _fail("--cache-hit must be between 0 and 1")
    if args.output_ratio < 0.0:
        return _fail("--output-ratio cannot be negative")
    if args.top < 1:
        return _fail("--top must be at least 1")
    try:
        pricing = load_pricing(pathlib.Path(args.pricing)) if args.pricing else None
        result = scan(
            args.target,
            model_id=args.model,
            traffic=_traffic(args),
            pricing=pricing,
            top=args.top,
        )
    except (ScanError, PricingError, FileNotFoundError) as exc:
        return _fail(str(exc))
    if args.format == "json":
        text = render_json(result)
    elif args.format == "markdown":
        text = render_markdown(result)
    else:
        text = render_terminal(
            result, top=args.top, use_colour=not args.no_color and sys.stdout.isatty()
        )
    if args.out:
        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + ("" if text.endswith("\n") else "\n"), encoding="utf-8")
        sys.stdout.write("wrote %s\n" % out)
    else:
        sys.stdout.write(text + "\n")
    if args.fail_on_findings and result.findings:
        return EXIT_FINDINGS
    return EXIT_OK


def cmd_optimize(args) -> int:
    """Optimise one prompt file and report the verifiable token delta."""
    target = pathlib.Path(args.target)
    if not target.exists():
        return _fail("no such prompt file: %s" % target)
    if target.is_dir():
        return _fail("%s is a directory; optimize takes a single file" % target)
    if args.calls_per_day < 0:
        return _fail("--calls-per-day cannot be negative")
    try:
        pricing = load_pricing()
        model = pricing.model(args.model)
        tokenizer = get_tokenizer(model.encoding)
    except (PricingError, FileNotFoundError) as exc:
        return _fail(str(exc))
    try:
        original = target.read_text(encoding="utf-8")
    except OSError as exc:
        return _fail("could not read %s: %s" % (target, exc))
    if not original.strip():
        return _fail("%s is empty; there is nothing to optimise" % target)

    result = optimize_text(original, level=args.level, counter=tokenizer.count)
    monthly = result.delta_tokens * model.input / 1_000_000 * args.calls_per_day * 30

    if args.json:
        payload = {
            "file": str(target),
            "level": args.level,
            "model": model.id,
            "tokens_before": result.before_tokens,
            "tokens_after": result.after_tokens,
            "delta_tokens": result.delta_tokens,
            "percent_saved": round(result.percent_saved, 2),
            "estimated_monthly_saving_usd": round(monthly, 2),
            "steps": [
                {
                    "name": change.name,
                    "before_tokens": change.before_tokens,
                    "after_tokens": change.after_tokens,
                    "delta_tokens": change.delta_tokens,
                    "detail": change.detail,
                }
                for change in result.changes
            ],
        }
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        if args.diff:
            diff = difflib.unified_diff(
                original.splitlines(keepends=True),
                result.optimized.splitlines(keepends=True),
                fromfile="a/%s" % target.name,
                tofile="b/%s" % target.name,
            )
            diff_text = "".join(diff)
            if diff_text and not diff_text.endswith("\n"):
                diff_text += "\n"
            sys.stdout.write(diff_text)
            if diff_text:
                sys.stdout.write("\n")
        sys.stdout.write(
            "%s %s: %d -> %d tokens (%+.1f%%), %d token(s) saved\n"
            % (
                PROGRAM,
                target,
                result.before_tokens,
                result.after_tokens,
                -result.percent_saved,
                result.delta_tokens,
            )
        )
        for change in result.changes:
            detail = " (%s)" % change.detail if change.detail else ""
            sys.stdout.write(
                "  %-32s %6d -> %6d  %+5d%s\n"
                % (
                    change.name,
                    change.before_tokens,
                    change.after_tokens,
                    -change.delta_tokens,
                    detail,
                )
            )
        if not result.changes:
            sys.stdout.write("  no safe transformations applied\n")
        sys.stdout.write(
            "  estimated saving at %s calls/day for 30 days on %s: $%s\n"
            % (format(args.calls_per_day, ","), model.id, format(monthly, ",.2f"))
        )

    if args.in_place:
        target.write_text(result.optimized, encoding="utf-8")
        if not args.json:
            sys.stdout.write("  wrote %s in place\n" % target)
    if args.out:
        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.optimized, encoding="utf-8")
        if not args.json:
            sys.stdout.write("  wrote %s\n" % out)
    return EXIT_OK


def cmd_budget(args) -> int:
    """Project spend for a plan file."""
    try:
        pricing = load_pricing(pathlib.Path(args.pricing)) if args.pricing else None
        plan = load_plan(args.plan)
        report = build_budget(plan, pricing)
    except (BudgetError, PricingError, FileNotFoundError) as exc:
        return _fail(str(exc))
    if args.json:
        sys.stdout.write(render_budget_json(report))
    else:
        sys.stdout.write(render_budget(report) + "\n")
    return EXIT_OK


def cmd_report(args) -> int:
    """Scan a target and write a self-contained HTML report."""
    if args.calls_per_day < 0:
        return _fail("--calls-per-day cannot be negative")
    try:
        pricing = load_pricing(pathlib.Path(args.pricing)) if args.pricing else None
        result = scan(
            args.target,
            model_id=args.model,
            traffic=TrafficProfile(calls_per_day=args.calls_per_day),
            pricing=pricing,
            top=args.top,
        )
    except (ScanError, PricingError, FileNotFoundError) as exc:
        return _fail(str(exc))
    out = pathlib.Path(args.html)
    out.parent.mkdir(parents=True, exist_ok=True)
    page = render_html(result)
    out.write_text(page, encoding="utf-8")
    sys.stdout.write(
        "wrote %s (%d bytes, %d findings, %s estimated monthly saving)\n"
        % (out, len(page.encode("utf-8")), len(result.findings),
           "$%s" % format(result.monthly_saving_usd, ",.2f"))
    )
    return EXIT_OK


def cmd_accuracy(args) -> int:
    """Measure the tokenizer against the golden set."""
    try:
        reports = measure_all()
    except (AccuracyError, FileNotFoundError) as exc:
        return _fail(str(exc))
    if args.encoding:
        reports = [report for report in reports if report.encoding == args.encoding]
        if not reports:
            return _fail("encoding %s is not present in the golden set" % args.encoding)
    if args.json:
        sys.stdout.write(accuracy_json(reports))
    else:
        sys.stdout.write(render_accuracy(reports) + "\n")
    worst = max((abs(report.total_error_pct) for report in reports), default=0.0)
    if worst > 5.0:
        sys.stderr.write(
            "%s: warning: corpus-total error %.2f%% exceeds the documented 5%% budget\n"
            % (PROGRAM, worst)
        )
        return EXIT_FINDINGS
    return EXIT_OK


def cmd_models(args) -> int:
    """List the committed pricing snapshot."""
    try:
        table = load_pricing(pathlib.Path(args.pricing)) if args.pricing else load_pricing()
    except (PricingError, FileNotFoundError) as exc:
        return _fail(str(exc))
    if args.json:
        payload = {
            "snapshot_date": table.snapshot_date,
            "currency": table.currency,
            "models": [
                {
                    "id": model.id,
                    "provider": model.provider,
                    "encoding": model.encoding,
                    "input": model.input,
                    "cached_input": model.cached_input,
                    "output": model.output,
                }
                for model in sorted(table.models.values(), key=lambda item: item.id)
            ],
        }
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return EXIT_OK
    sys.stdout.write(
        "%s %s pricing snapshot %s (USD per 1M tokens)\n"
        % (PROGRAM, table.currency, table.snapshot_date)
    )
    sys.stdout.write("  %-24s %-12s %-8s %9s %9s %9s\n"
                     % ("model", "provider", "encoding", "input", "cached", "output"))
    for model in sorted(table.models.values(), key=lambda item: item.id):
        sys.stdout.write(
            "  %-24s %-12s %-8s %9.3f %9.3f %9.3f\n"
            % (model.id, model.provider, model.encoding, model.input, model.cached_input, model.output)
        )
    sys.stdout.write("\n%s\n" % table.note)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to a command handler."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        sys.stderr.write("\n%s: interrupted\n" % PROGRAM)
        return 130
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
