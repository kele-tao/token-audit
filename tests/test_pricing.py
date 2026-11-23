"""Pricing table and budget maths.

Every number in the README that involves money is produced by these functions,
so they are tested against hand arithmetic rather than against a previous run.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from token_audit.budget import (
    BudgetError,
    Plan,
    Scenario,
    Traffic,
    build_budget,
    load_plan,
    render_budget,
    render_budget_json,
)
from token_audit.pricing import (
    PricingError,
    cost_per_calls,
    input_cost,
    load_pricing,
    monthly_cost,
    output_cost,
    project,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLAN = ROOT / "examples" / "plan.json"


def test_snapshot_has_a_date_and_models():
    table = load_pricing()
    assert table.snapshot_date.count("-") == 2
    assert table.currency == "USD"
    assert len(table.models) >= 10
    assert all(model.encoding in ("cl100k", "o200k") for model in table.models.values())


def test_input_cost_matches_hand_arithmetic():
    model = load_pricing().model("gpt-4o")
    # 1M tokens at $2.50 per 1M is exactly $2.50.
    assert input_cost(1_000_000, model) == pytest.approx(2.50)
    assert input_cost(1_000, model) == pytest.approx(0.0025)


def test_output_cost_matches_hand_arithmetic():
    model = load_pricing().model("gpt-4o")
    assert output_cost(1_000_000, model) == pytest.approx(10.0)


def test_cache_hit_discount_is_a_weighted_average():
    model = load_pricing().model("gpt-4o")
    full = input_cost(1_000_000, model, 0.0)
    cached = input_cost(1_000_000, model, 1.0)
    half = input_cost(1_000_000, model, 0.5)
    assert cached == pytest.approx(model.cached_input)
    assert half == pytest.approx((full + cached) / 2)


def test_cache_ratio_is_clamped_to_the_unit_interval():
    model = load_pricing().model("gpt-4o")
    assert input_cost(1_000_000, model, 5.0) == pytest.approx(model.cached_input)
    assert input_cost(1_000_000, model, -3.0) == pytest.approx(model.input)


def test_cost_per_calls_scales_linearly():
    model = load_pricing().model("gpt-4o")
    one = cost_per_calls(1000, 500, model, 1)
    thousand = cost_per_calls(1000, 500, model, 1000)
    assert thousand == pytest.approx(one * 1000)


def test_monthly_cost_uses_days_and_requests():
    model = load_pricing().model("gpt-4o")
    monthly = monthly_cost(1000, 0, model, 10, 30)
    assert monthly == pytest.approx(cost_per_calls(1000, 0, model, 300))


def test_unknown_model_raises_with_a_hint():
    table = load_pricing()
    with pytest.raises(PricingError) as info:
        table.model("gpt-4o-mni")
    assert "Did you mean" in str(info.value)


def test_unknown_model_without_a_close_match_still_lists_models():
    with pytest.raises(PricingError) as info:
        load_pricing().model("zzz")
    assert "Known models" in str(info.value)


def test_malformed_pricing_file_is_rejected(tmp_path):
    bad = tmp_path / "pricing.json"
    bad.write_text("{broken", encoding="utf-8")
    with pytest.raises(PricingError):
        load_pricing(bad)


def test_pricing_without_models_is_rejected(tmp_path):
    empty = tmp_path / "pricing.json"
    empty.write_text('{"snapshot_date": "2026-01-01"}', encoding="utf-8")
    with pytest.raises(PricingError):
        load_pricing(empty)


def test_pricing_row_with_unknown_encoding_is_rejected(tmp_path):
    bad = tmp_path / "pricing.json"
    bad.write_text(
        json.dumps(
            {
                "snapshot_date": "2026-01-01",
                "models": [
                    {
                        "id": "x",
                        "provider": "p",
                        "encoding": "bpe2000",
                        "input": 1,
                        "output": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PricingError) as info:
        load_pricing(bad)
    assert "unknown encoding" in str(info.value)


def test_pricing_row_with_negative_price_is_rejected(tmp_path):
    bad = tmp_path / "pricing.json"
    bad.write_text(
        json.dumps(
            {
                "models": [
                    {"id": "x", "provider": "p", "encoding": "cl100k", "input": -1, "output": 1}
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PricingError) as info:
        load_pricing(bad)
    assert "negative" in str(info.value)


def test_project_returns_sorted_rows():
    rows = project(1000, 100, ["gpt-4o", "gpt-4o-mini"], 100, 30)
    assert [row[0] for row in rows] == ["gpt-4o-mini", "gpt-4o"]


def test_plan_file_loads_with_expected_shape():
    plan = load_plan(PLAN)
    assert plan.name == "support-copilot"
    assert plan.traffic.requests_per_day == 12000
    assert plan.scenarios
    assert plan.cache_sensitivity


def test_missing_plan_raises_budget_error(tmp_path):
    with pytest.raises(BudgetError):
        load_plan(tmp_path / "nope.json")


def test_plan_without_traffic_is_rejected(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text('{"name": "x"}', encoding="utf-8")
    with pytest.raises(BudgetError) as info:
        load_plan(plan)
    assert "traffic" in str(info.value)


def test_plan_with_non_numeric_traffic_is_rejected(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps({"traffic": {"requests_per_day": "many", "input_tokens": 1, "output_tokens": 1}}),
        encoding="utf-8",
    )
    with pytest.raises(BudgetError):
        load_plan(plan)


def test_traffic_validation_rejects_impossible_values():
    with pytest.raises(BudgetError):
        Traffic(0, 30, 10, 10).validate()
    with pytest.raises(BudgetError):
        Traffic(1, 40, 10, 10).validate()
    with pytest.raises(BudgetError):
        Traffic(1, 30, -1, 10).validate()
    with pytest.raises(BudgetError):
        Traffic(1, 30, 10, 10, cache_hit_ratio=2.0).validate()


def test_budget_rows_scale_with_scenario_multiplier():
    plan = Plan(
        name="t",
        models=["gpt-4o"],
        traffic=Traffic(requests_per_day=100, days_per_month=30, input_tokens=1000, output_tokens=100),
        scenarios=[Scenario("base", 1.0), Scenario("double", 2.0)],
    )
    report = build_budget(plan)
    by_scenario = {row.scenario: row.monthly_usd for row in report.rows}
    assert by_scenario["double"] == pytest.approx(by_scenario["base"] * 4, rel=1e-6)


def test_budget_renderers_are_deterministic():
    report = build_budget(load_plan(PLAN))
    assert render_budget(report) == render_budget(report)
    assert render_budget_json(report) == render_budget_json(report)


def test_budget_json_is_parseable_and_complete():
    report = build_budget(load_plan(PLAN))
    payload = json.loads(render_budget_json(report))
    assert payload["traffic"]["requests_per_day"] == 12000
    assert len(payload["rows"]) == len(report.rows)
    assert all("monthly_usd" in row for row in payload["rows"])


def test_sensitivity_slope_is_monotonic_in_cache_ratio():
    report = build_budget(load_plan(PLAN))
    cache_rows = [row for row in report.sensitivity if row.variable == "cache_hit_ratio"]
    costs = [row.monthly_usd for row in cache_rows]
    assert costs == sorted(costs, reverse=True)
