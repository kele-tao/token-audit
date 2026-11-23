"""Renderers: terminal, Markdown, JSON and a self-contained HTML report.

Every renderer is deterministic: no timestamps, no random ids, no locale
dependent formatting.  The HTML report draws its cost bars with inline SVG, so
the file works offline and diffs cleanly.
"""
from __future__ import annotations

import html
import json

from .models import Finding, ScanResult, Severity
from .scanner import json_ready

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
CYAN = "\x1b[36m"
GREY = "\x1b[90m"
SEVERITY_COLOUR = {
    Severity.HIGH: RED,
    Severity.MEDIUM: YELLOW,
    Severity.LOW: BLUE,
    Severity.INFO: GREY,
}


def money(value: float) -> str:
    """Format a dollar amount with thousands separators and cents."""
    return "$%s" % format(value, ",.2f")


def count(value: int) -> str:
    """Format an integer with thousands separators."""
    return format(value, ",")


def _clip(text: str, limit: int) -> str:
    """Clip one line of evidence so it never wraps in a terminal."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def colour(text: str, code: str, enabled: bool) -> str:
    """Wrap text in an ANSI code when colour is enabled."""
    if not enabled:
        return text
    return code + text + RESET


def render_terminal(result: ScanResult, top: int = 5, use_colour: bool = True) -> str:
    """Render the human-readable scan report."""
    lines: list[str] = []
    add = lines.append
    model_id = str(result.assumptions["model"])
    token_total = result.tokens_by_encoding.get(
        next(iter(result.tokens_by_encoding), ""), 0
    )
    encodings = ", ".join(
        "%s %s" % (name, count(total))
        for name, total in sorted(result.tokens_by_encoding.items())
    )
    add(colour("token-audit scan", BOLD, use_colour) + "  " + colour(result.root, DIM, use_colour))
    add(
        "pricing snapshot %s (%s per 1M tokens)  model %s"
        % (result.pricing_snapshot, result.pricing_currency, model_id)
    )
    add("")
    add(colour("FILES", BOLD, use_colour))
    add(
        "  %d scanned, %d skipped, %s bytes"
        % (result.files_scanned, result.files_skipped, count(result.bytes_scanned))
    )
    add("")
    add(colour("TOKENS", BOLD, use_colour))
    add("  %s" % encodings)
    average = result.assumptions["avg_input_tokens_per_file"]
    add("  %s tokens per file on average" % count(int(average)))
    add("")

    add(colour("COST PER 1,000 CALLS", BOLD, use_colour))
    add(
        "  %-24s %11s %11s %11s"
        % ("model", "input", "output", "total")
    )
    ranked = sorted(result.costs, key=lambda cost: cost.monthly_usd)[:6]
    for cost in ranked:
        add(
            "  %-24s %11s %11s %11s"
            % (
                cost.model_id,
                money(cost.input_usd_per_1k_calls),
                money(cost.output_usd_per_1k_calls),
                money(cost.input_usd_per_1k_calls + cost.output_usd_per_1k_calls),
            )
        )
    add("")

    add(colour("MONTHLY AT %s" % str(result.assumptions["profile"]).upper(), BOLD, use_colour))
    for cost in ranked[:3]:
        add(
            "  %-24s %12s   %s"
            % (cost.model_id, money(cost.monthly_usd), cost.provider)
        )
    add("")

    add(colour("TOP %d STRINGS" % top, BOLD, use_colour))
    for tokens, segment, occurrences in result.top_segments[:top]:
        preview = " ".join(segment.text.split())
        if len(preview) > 58:
            preview = preview[:55] + "..."
        suffix = " x%d" % occurrences if occurrences > 1 else ""
        add(
            "  %6s  %-30s %s%s"
            % (
                count(tokens),
                "%s:%d" % (segment.source or "inline", segment.line),
                colour(preview, DIM, use_colour),
                colour(suffix, YELLOW, use_colour),
            )
        )
    add("")

    header = "WASTE FINDINGS (%d)  estimated %s/month" % (
        len(result.findings),
        money(result.monthly_saving_usd),
    )
    add(colour(header, BOLD + YELLOW, use_colour))
    if not result.findings:
        add("  no waste detected")
    for finding in result.findings:
        badge = colour(
            "%-6s" % finding.severity.value.upper(),
            SEVERITY_COLOUR[finding.severity],
            use_colour,
        )
        add(
            "  %s %-24s %8s tok %10s/mo  %s"
            % (
                badge,
                finding.detector,
                count(finding.wasted_tokens),
                money(finding.monthly_saving_usd),
                finding.title,
            )
        )
        detail = finding.evidence[0] if finding.evidence else ""
        add(
            "         %s"
            % colour(_clip(detail, 118), DIM, use_colour)
        )
        add(
            "         %s"
            % colour("fix: " + _clip(finding.recommendation, 108), GREEN, use_colour)
        )
    add("")
    add(
        colour(
            "totals: %s tokens, %s findings, %s estimated monthly saving"
            % (count(token_total), len(result.findings), money(result.monthly_saving_usd)),
            BOLD,
            use_colour,
        )
    )
    add(
        colour(
            "token counts replay a committed BPE merge table; run 'token-audit "
            "accuracy' to see measured error against the golden set.",
            DIM,
            use_colour,
        )
    )
    return "\n".join(lines)


def render_markdown(result: ScanResult) -> str:
    """Render the scan as a Markdown report."""
    lines: list[str] = []
    add = lines.append
    add("# token-audit report")
    add("")
    add("Pricing snapshot %s (%s per 1M tokens). Model: %s." % (
        result.pricing_snapshot, result.pricing_currency, result.assumptions["model"]
    ))
    add("")
    add("| metric | value |")
    add("| --- | --- |")
    add("| files scanned | %d |" % result.files_scanned)
    add("| bytes scanned | %s |" % count(result.bytes_scanned))
    for name, total in sorted(result.tokens_by_encoding.items()):
        add("| tokens (%s) | %s |" % (name, count(total)))
    add("| findings | %d |" % len(result.findings))
    add("| estimated monthly saving | %s |" % money(result.monthly_saving_usd))
    add("")
    add("## Findings")
    add("")
    add("| severity | detector | wasted tokens | monthly saving | recommendation |")
    add("| --- | --- | --- | --- | --- |")
    for finding in result.findings:
        add(
            "| %s | %s | %s | %s | %s |"
            % (
                finding.severity.value,
                finding.detector,
                count(finding.wasted_tokens),
                money(finding.monthly_saving_usd),
                finding.recommendation.replace("|", "/"),
            )
        )
    add("")
    add("## Cost per 1,000 calls")
    add("")
    add("| model | provider | input | output | monthly |")
    add("| --- | --- | --- | --- | --- |")
    for cost in sorted(result.costs, key=lambda item: item.monthly_usd):
        add(
            "| %s | %s | %s | %s | %s |"
            % (
                cost.model_id,
                cost.provider,
                money(cost.input_usd_per_1k_calls),
                money(cost.output_usd_per_1k_calls),
                money(cost.monthly_usd),
            )
        )
    add("")
    add("## Top strings")
    add("")
    add("| tokens | location | preview | repeats |")
    add("| --- | --- | --- | --- |")
    for tokens, segment, occurrences in result.top_segments:
        preview = " ".join(segment.text.split())[:70].replace("|", "/")
        add(
            "| %s | %s:%d | %s | %d |"
            % (count(tokens), segment.source, segment.line, preview, occurrences)
        )
    add("")
    return "\n".join(lines)


def _bar_width(value: float, maximum: float, width: int = 360) -> float:
    """Scale a value to a bar width, keeping a minimum visible bar."""
    if maximum <= 0:
        return 0.0
    return max(2.0, width * value / maximum)


def _svg_bar_chart(title: str, rows: list[tuple[str, float]], unit: str) -> str:
    """Build a horizontal bar chart as inline SVG."""
    if not rows:
        return ""
    maximum = max(value for _label, value in rows)
    row_height = 26
    height = row_height * len(rows) + 34
    parts = [
        '<svg class="bars" role="img" viewBox="0 0 720 %d" '
        'xmlns="http://www.w3.org/2000/svg">' % height,
        '<text x="0" y="14" class="svg-title">%s</text>' % html.escape(title),
    ]
    for index, (label, value) in enumerate(rows):
        y = 26 + index * row_height
        width = _bar_width(value, maximum)
        parts.append(
            '<text x="0" y="%d" class="svg-label">%s</text>'
            % (y + 12, html.escape(label))
        )
        parts.append(
            '<rect x="190" y="%d" width="%.1f" height="16" rx="3" fill="url(#bar)" />'
            % (y, width)
        )
        parts.append(
            '<text x="%.1f" y="%d" class="svg-value">%s %s</text>'
            % (196 + width, y + 12, unit, format(value, ",.2f"))
        )
    parts.append("</svg>")
    return "".join(parts)


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>token-audit report</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 48px 40px 64px; background: #0b0f17; color: #d1d5db;
    font: 15px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  h1 { font-size: 26px; margin: 0 0 4px; color: #f3f4f6; letter-spacing: -0.02em; }
  h2 { font-size: 15px; text-transform: uppercase; letter-spacing: 0.09em;
       color: #8b9bb4; margin: 40px 0 14px; font-weight: 600; }
  .sub { color: #8b9bb4; margin: 0 0 8px; }
  .cards { display: flex; gap: 16px; flex-wrap: wrap; margin-top: 22px; }
  .card { background: #121826; border: 1px solid #1f2937; border-radius: 12px;
          padding: 16px 20px; min-width: 168px; }
  .card .k { font-size: 12px; text-transform: uppercase; letter-spacing: .08em;
             color: #8b9bb4; }
  .card .v { font-size: 24px; font-weight: 650; color: #f3f4f6;
             font-variant-numeric: tabular-nums; }
  .card .v.good { color: #4ade80; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th { text-align: left; color: #8b9bb4; font-weight: 600; padding: 6px 10px;
       border-bottom: 1px solid #1f2937; font-size: 12px; text-transform: uppercase;
       letter-spacing: .07em; }
  td { padding: 7px 10px; border-bottom: 1px solid #151c2a; vertical-align: top; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .sev { display: inline-block; padding: 1px 8px; border-radius: 999px;
         font-size: 11px; font-weight: 700; letter-spacing: .06em; }
  .sev.high { background: #3f1414; color: #ff8787; }
  .sev.medium { background: #3a2a0c; color: #fde68a; }
  .sev.low { background: #10243a; color: #93c5fd; }
  .sev.info { background: #1f2937; color: #9ca3af; }
  .bars { width: 100%; height: auto; }
  .svg-title { fill: #8b9bb4; font-size: 13px; font-weight: 600; }
  .svg-label { fill: #d1d5db; font-size: 13px; font-family: ui-monospace, Consolas, monospace; }
  .svg-value { fill: #9ca3af; font-size: 12px; font-family: ui-monospace, Consolas, monospace; }
  .fix { color: #86efac; }
  .muted { color: #6b7280; font-size: 13px; }
  .mono { font-family: ui-monospace, Consolas, monospace; }
  footer { margin-top: 48px; color: #6b7280; font-size: 12px; }
</style>
</head>
<body>
<svg width="0" height="0" aria-hidden="true"><defs>
  <linearGradient id="bar" x1="0" x2="1">
    <stop offset="0%" stop-color="#22d3ee"/>
    <stop offset="100%" stop-color="#60a5fa"/>
  </linearGradient>
</defs></svg>
<h1>token-audit report</h1>
<p class="sub">root <span class="mono">__ROOT__</span> &middot; model <span class="mono">__MODEL__</span>
 &middot; pricing snapshot __SNAPSHOT__ (__CURRENCY__ per 1M tokens)</p>
<div class="cards">__CARDS__</div>
<h2>Cost bars &mdash; monthly projection</h2>
__BARS__
<h2>Waste findings</h2>
__FINDINGS__
<h2>Cost per 1,000 calls</h2>
__COSTS__
<h2>Most expensive strings</h2>
__TOP__
<footer>Generated by token-audit. Token counts replay a committed BPE merge table;
measured error against the golden set is reported by <span class="mono">token-audit accuracy</span>.
Cost figures are estimates from the committed pricing snapshot, not provider invoices.</footer>
</body>
</html>
"""


def render_html(result: ScanResult) -> str:
    """Render a deterministic, offline HTML report with inline SVG bars."""
    cards = [
        ("files scanned", str(result.files_scanned)),
        ("bytes", count(result.bytes_scanned)),
    ]
    for name, total in sorted(result.tokens_by_encoding.items()):
        cards.append(("tokens (%s)" % name, count(total)))
    cards.append(("findings", str(len(result.findings))))
    cards.append(("est. monthly saving", money(result.monthly_saving_usd)))
    card_html = "".join(
        '<div class="card"><div class="k">%s</div><div class="v%s">%s</div></div>'
        % (
            html.escape(key),
            " good" if key.startswith("est.") else "",
            html.escape(value),
        )
        for key, value in cards
    )
    bars = _svg_bar_chart(
        "monthly USD by model",
        [(cost.model_id, cost.monthly_usd) for cost in sorted(result.costs, key=lambda c: -c.monthly_usd)[:8]],
        "$",
    )
    if result.findings:
        rows = "".join(
            "<tr>"
            '<td><span class="sev %s">%s</span></td>'
            "<td class=\"mono\">%s</td><td>%s</td>"
            '<td class="num">%s</td><td class="num">%s</td>'
            '<td class="fix">%s</td></tr>'
            % (
                finding.severity.value,
                finding.severity.value,
                html.escape(finding.detector),
                html.escape(finding.title),
                count(finding.wasted_tokens),
                money(finding.monthly_saving_usd),
                html.escape(finding.recommendation),
            )
            for finding in result.findings
        )
        findings_html = (
            "<table><thead><tr><th>severity</th><th>detector</th><th>finding</th>"
            "<th>wasted tok</th><th>usd/month</th><th>recommendation</th></tr></thead>"
            "<tbody>%s</tbody></table>" % rows
        )
        evidence = "".join(
            '<p class="muted"><span class="mono">%s</span> &mdash; %s</p>'
            % (
                html.escape(finding.detector),
                html.escape("; ".join(finding.evidence[:2])),
            )
            for finding in result.findings
            if finding.evidence
        )
        findings_html += evidence
    else:
        findings_html = '<p class="muted">No waste detected in this input.</p>'

    cost_rows = "".join(
        "<tr><td class=\"mono\">%s</td><td>%s</td>"
        '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td></tr>'
        % (
            html.escape(cost.model_id),
            html.escape(cost.provider),
            money(cost.input_usd_per_1k_calls),
            money(cost.output_usd_per_1k_calls),
            money(cost.monthly_usd),
        )
        for cost in sorted(result.costs, key=lambda item: item.monthly_usd)
    )
    costs_html = (
        "<table><thead><tr><th>model</th><th>provider</th><th>input / 1k calls</th>"
        "<th>output / 1k calls</th><th>monthly</th></tr></thead><tbody>%s</tbody></table>"
        % cost_rows
    )
    top_rows = "".join(
        '<tr><td class="num">%s</td><td class="mono">%s:%d</td><td>%s</td>'
        '<td class="num">%d</td></tr>'
        % (
            count(tokens),
            html.escape(segment.source or "inline"),
            segment.line,
            html.escape(" ".join(segment.text.split())[:110]),
            occurrences,
        )
        for tokens, segment, occurrences in result.top_segments
    )
    top_html = (
        "<table><thead><tr><th>tokens</th><th>location</th><th>preview</th>"
        "<th>repeats</th></tr></thead><tbody>%s</tbody></table>" % top_rows
    )

    page = HTML_TEMPLATE
    replacements = {
        "__ROOT__": html.escape(result.root),
        "__MODEL__": html.escape(str(result.assumptions["model"])),
        "__SNAPSHOT__": html.escape(result.pricing_snapshot),
        "__CURRENCY__": html.escape(result.pricing_currency),
        "__CARDS__": card_html,
        "__BARS__": bars,
        "__FINDINGS__": findings_html,
        "__COSTS__": costs_html,
        "__TOP__": top_html,
    }
    for key, value in replacements.items():
        page = page.replace(key, value)
    return page


def render_json(result: ScanResult) -> str:
    """Render the scan as stable, indented JSON."""
    payload = json_ready(result)
    payload["findings"] = [json_ready(finding) for finding in result.findings]
    payload["top_segments"] = [
        {
            "tokens": tokens,
            "occurrences": occurrences,
            "segment": json_ready(segment),
        }
        for tokens, segment, occurrences in result.top_segments
    ]
    payload["costs"] = [json_ready(cost) for cost in result.costs]
    payload["documents"] = [json_ready(document) for document in result.documents]
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def finding_lines(findings: list[Finding]) -> list[str]:
    """One-line summaries used by the optimize command."""
    return [
        "%s %s (%d tokens)" % (item.severity.value, item.title, item.wasted_tokens)
        for item in findings
    ]
