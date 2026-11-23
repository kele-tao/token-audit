"""Budget planning: project spend for a traffic scenario and its sensitivity."""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

from .pricing import PricingTable, load_pricing, monthly_cost


class BudgetError(ValueError):
    """Raised when a plan file is missing required fields."""


@dataclass
class Traffic:
    """Traffic assumptions for one plan."""

    requests_per_day: int
    days_per_month: int
    input_tokens: int
    output_tokens: int
    cache_hit_ratio: float = 0.0

    def validate(self) -> None:
        """Reject values that cannot describe real traffic."""
        if self.requests_per_day < 1:
            raise BudgetError("traffic.requests_per_day must be at least 1")
        if self.days_per_month < 1 or self.days_per_month > 31:
            raise BudgetError("traffic.days_per_month must be between 1 and 31")
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise BudgetError("token counts cannot be negative")
        if not 0.0 <= self.cache_hit_ratio <= 1.0:
            raise BudgetError("traffic.cache_hit_ratio must be between 0 and 1")


@dataclass
class Scenario:
    """A named multiplier over the base traffic."""

    name: str
    multiplier: float = 1.0


@dataclass
class Plan:
    """A parsed budget plan file."""

    name: str
    models: list[str]
    traffic: Traffic
    scenarios: list[Scenario] = field(default_factory=list)
    cache_sensitivity: list[float] = field(default_factory=list)
    output_sensitivity: list[float] = field(default_factory=list)


@dataclass
class BudgetRow:
    """One (scenario, model) projection."""

    scenario: str
    model: str
    monthly_usd: float
    yearly_usd: float
    per_request_usd: float


@dataclass
class SensitivityRow:
    """One sensitivity probe: a variable, a value and a monthly cost."""

    variable: str
    value: float
    model: str
    monthly_usd: float
    delta_usd: float


@dataclass
class BudgetReport:
    """Everything the budget command renders."""

    plan_name: str
    traffic: Traffic
    rows: list[BudgetRow]
    sensitivity: list[SensitivityRow]
    snapshot_date: str
    currency: str


def load_plan(path: str | pathlib.Path) -> Plan:
    """Load and validate a plan JSON file."""
    target = pathlib.Path(path)
    if not target.exists():
        raise BudgetError(
            "no such plan file: %s. Pass a JSON file like examples/plan.json." % target
        )
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BudgetError("plan file %s is not valid JSON: %s" % (target, exc)) from exc
    if not isinstance(raw, dict):
        raise BudgetError("plan file %s must contain a JSON object" % target)
    traffic_raw = raw.get("traffic")
    if not isinstance(traffic_raw, dict):
        raise BudgetError("plan file %s has no traffic object" % target)
    try:
        traffic = Traffic(
            requests_per_day=int(traffic_raw["requests_per_day"]),
            days_per_month=int(traffic_raw.get("days_per_month", 30)),
            input_tokens=int(traffic_raw["input_tokens"]),
            output_tokens=int(traffic_raw["output_tokens"]),
            cache_hit_ratio=float(traffic_raw.get("cache_hit_ratio", 0.0)),
        )
    except KeyError as exc:
        raise BudgetError("traffic is missing the field %s" % exc) from exc
    except (TypeError, ValueError) as exc:
        raise BudgetError("traffic has a non-numeric value: %s" % exc) from exc
    traffic.validate()

    models = raw.get("models") or ["gpt-4o"]
    if not all(isinstance(item, str) for item in models):
        raise BudgetError("models must be a list of model ids")
    scenarios = [
        Scenario(str(item.get("name", "scenario-%d" % index)), float(item.get("multiplier", 1.0)))
        for index, item in enumerate(raw.get("scenarios") or [], 1)
        if isinstance(item, dict)
    ]
    if not scenarios:
        scenarios = [Scenario("base", 1.0)]
    sensitivity = raw.get("sensitivity") or {}
    cache_values = [float(value) for value in sensitivity.get("cache_hit_ratio", [])]
    output_values = [float(value) for value in sensitivity.get("output_ratio", [])]
    return Plan(
        name=str(raw.get("name", target.stem)),
        models=list(models),
        traffic=traffic,
        scenarios=scenarios,
        cache_sensitivity=cache_values,
        output_sensitivity=output_values,
    )


def build_budget(plan: Plan, pricing: PricingTable | None = None) -> BudgetReport:
    """Project monthly cost for every scenario and model, plus sensitivity."""
    table = pricing or load_pricing()
    rows: list[BudgetRow] = []
    for scenario in plan.scenarios:
        requests = int(plan.traffic.requests_per_day * scenario.multiplier)
        input_tokens = int(plan.traffic.input_tokens * scenario.multiplier)
        output_tokens = int(plan.traffic.output_tokens * scenario.multiplier)
        for model_id in plan.models:
            model = table.model(model_id)
            monthly = monthly_cost(
                input_tokens,
                output_tokens,
                model,
                requests,
                plan.traffic.days_per_month,
                plan.traffic.cache_hit_ratio,
            )
            calls = max(1, requests * plan.traffic.days_per_month)
            rows.append(
                BudgetRow(
                    scenario=scenario.name,
                    model=model.id,
                    monthly_usd=round(monthly, 2),
                    yearly_usd=round(monthly * 12, 2),
                    per_request_usd=round(monthly / calls, 6),
                )
            )
    rows.sort(key=lambda row: (row.scenario, row.monthly_usd, row.model))

    base = plan.models[0]
    base_model = table.model(base)
    baseline = monthly_cost(
        plan.traffic.input_tokens,
        plan.traffic.output_tokens,
        base_model,
        plan.traffic.requests_per_day,
        plan.traffic.days_per_month,
        plan.traffic.cache_hit_ratio,
    )
    sensitivity: list[SensitivityRow] = []
    for value in plan.cache_sensitivity:
        cost = monthly_cost(
            plan.traffic.input_tokens,
            plan.traffic.output_tokens,
            base_model,
            plan.traffic.requests_per_day,
            plan.traffic.days_per_month,
            value,
        )
        sensitivity.append(
            SensitivityRow("cache_hit_ratio", value, base, round(cost, 2), round(cost - baseline, 2))
        )
    for ratio in plan.output_sensitivity:
        cost = monthly_cost(
            plan.traffic.input_tokens,
            int(plan.traffic.output_tokens * ratio * 2.0),
            base_model,
            plan.traffic.requests_per_day,
            plan.traffic.days_per_month,
            plan.traffic.cache_hit_ratio,
        )
        sensitivity.append(
            SensitivityRow("output_ratio", ratio, base, round(cost, 2), round(cost - baseline, 2))
        )
    return BudgetReport(
        plan_name=plan.name,
        traffic=plan.traffic,
        rows=rows,
        sensitivity=sensitivity,
        snapshot_date=table.snapshot_date,
        currency=table.currency,
    )


def render_budget(report: BudgetReport) -> str:
    """Render the budget projection as a terminal table."""
    traffic = report.traffic
    lines = [
        "token-audit budget  plan %s" % report.plan_name,
        "pricing snapshot %s (%s per 1M tokens)" % (report.snapshot_date, report.currency),
        "traffic: %s requests/day x %d days, %s input + %s output tokens, %.0f%% cache hit"
        % (
            format(traffic.requests_per_day, ","),
            traffic.days_per_month,
            format(traffic.input_tokens, ","),
            format(traffic.output_tokens, ","),
            100.0 * traffic.cache_hit_ratio,
        ),
        "",
        "MONTHLY PROJECTION",
        "  %-16s %-22s %12s %12s %12s" % ("scenario", "model", "monthly", "yearly", "per request"),
    ]
    for row in report.rows:
        lines.append(
            "  %-16s %-22s %12s %12s %12s"
            % (
                row.scenario,
                row.model,
                "$%s" % format(row.monthly_usd, ",.2f"),
                "$%s" % format(row.yearly_usd, ",.2f"),
                "$%s" % format(row.per_request_usd, ",.4f"),
            )
        )
    if report.sensitivity:
        lines += ["", "SENSITIVITY (first model in the plan)"]
        lines.append("  %-16s %8s %12s %12s" % ("variable", "value", "monthly", "vs base"))
        for row in report.sensitivity:
            lines.append(
                "  %-16s %8.2f %12s %12s"
                % (
                    row.variable,
                    row.value,
                    "$%s" % format(row.monthly_usd, ",.2f"),
                    "%s$%s" % ("+" if row.delta_usd >= 0 else "-", format(abs(row.delta_usd), ",.2f")),
                )
            )
    return "\n".join(lines)


def render_budget_json(report: BudgetReport) -> str:
    """Render the budget projection as deterministic JSON."""
    payload = {
        "plan": report.plan_name,
        "pricing_snapshot": report.snapshot_date,
        "currency": report.currency,
        "traffic": {
            "requests_per_day": report.traffic.requests_per_day,
            "days_per_month": report.traffic.days_per_month,
            "input_tokens": report.traffic.input_tokens,
            "output_tokens": report.traffic.output_tokens,
            "cache_hit_ratio": report.traffic.cache_hit_ratio,
        },
        "rows": [
            {
                "scenario": row.scenario,
                "model": row.model,
                "monthly_usd": row.monthly_usd,
                "yearly_usd": row.yearly_usd,
                "per_request_usd": row.per_request_usd,
            }
            for row in report.rows
        ],
        "sensitivity": [
            {
                "variable": row.variable,
                "value": row.value,
                "model": row.model,
                "monthly_usd": row.monthly_usd,
                "delta_usd": row.delta_usd,
            }
            for row in report.sensitivity
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
