"""Committed pricing table and cost arithmetic.

Prices live in the package data directory with a snapshot date.  Nothing here
calls a provider API: the tool is a calculator, and the calculator says which
snapshot it used.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass

DATA_DIR = pathlib.Path(__file__).resolve().parent / "data"
PRICING_FILE = DATA_DIR / "pricing.json"
MILLION = 1_000_000


class PricingError(ValueError):
    """Raised when a model id is unknown or the pricing table is malformed."""


@dataclass(frozen=True)
class Model:
    """One row of the pricing table."""

    id: str
    provider: str
    encoding: str
    input: float
    cached_input: float
    output: float

    @property
    def label(self) -> str:
        """Human readable model label."""
        return "%s (%s)" % (self.id, self.provider)


@dataclass(frozen=True)
class PricingTable:
    """The loaded pricing snapshot."""

    snapshot_date: str
    currency: str
    models: dict[str, Model]
    note: str

    def model(self, model_id: str) -> Model:
        """Look up a model, raising a friendly error when it is unknown."""
        key = model_id.strip().lower()
        if key in self.models:
            return self.models[key]
        close = [name for name in self.models if key in name or name in key]
        hint = ""
        if close:
            hint = " Did you mean: %s?" % ", ".join(sorted(close)[:4])
        raise PricingError(
            "unknown model %r.%s Known models: %s"
            % (model_id, hint, ", ".join(sorted(self.models)))
        )

    def encoding_for(self, model_id: str) -> str:
        """Encoding family used to count tokens for a model."""
        return self.model(model_id).encoding


def load_pricing(path: pathlib.Path | None = None) -> PricingTable:
    """Load the pricing snapshot, validating every row."""
    target = path or PRICING_FILE
    if not target.exists():
        raise PricingError(
            "pricing table missing: %s. Reinstall token-audit or pass --pricing." % target
        )
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PricingError(
            "pricing table %s is not valid JSON: %s" % (target, exc)
        ) from exc
    rows = raw.get("models")
    if not isinstance(rows, list) or not rows:
        raise PricingError("pricing table %s has no models" % target)
    models: dict[str, Model] = {}
    for row in rows:
        try:
            model = Model(
                id=str(row["id"]).lower(),
                provider=str(row["provider"]),
                encoding=str(row["encoding"]),
                input=float(row["input"]),
                cached_input=float(row.get("cached_input", row["input"])),
                output=float(row.get("output", 0.0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PricingError("bad pricing row %r: %s" % (row, exc)) from exc
        if model.encoding not in ("cl100k", "o200k"):
            raise PricingError(
                "model %s uses unknown encoding %r (expected cl100k or o200k)"
                % (model.id, model.encoding)
            )
        if min(model.input, model.cached_input, model.output) < 0:
            raise PricingError("model %s has a negative price" % model.id)
        models[model.id] = model
    return PricingTable(
        snapshot_date=str(raw.get("snapshot_date", "unknown")),
        currency=str(raw.get("currency", "USD")),
        models=models,
        note=str(raw.get("note", "")),
    )


def input_cost(tokens: int, model: Model, cache_hit_ratio: float = 0.0) -> float:
    """Cost in USD for tokens of input, honouring a cache-hit ratio."""
    ratio = min(max(cache_hit_ratio, 0.0), 1.0)
    fresh = tokens * (1.0 - ratio)
    cached = tokens * ratio
    return (fresh * model.input + cached * model.cached_input) / MILLION


def output_cost(tokens: int, model: Model) -> float:
    """Cost in USD for tokens of output."""
    return tokens * model.output / MILLION


def cost_per_calls(
    input_tokens: int,
    output_tokens: int,
    model: Model,
    calls: int,
    cache_hit_ratio: float = 0.0,
) -> float:
    """Cost in USD of calls calls with the given token profile."""
    per_call = input_cost(input_tokens, model, cache_hit_ratio) + output_cost(
        output_tokens, model
    )
    return per_call * calls


def monthly_cost(
    input_tokens: int,
    output_tokens: int,
    model: Model,
    calls_per_day: int,
    days: int,
    cache_hit_ratio: float = 0.0,
) -> float:
    """Cost in USD for days days at calls_per_day calls per day."""
    return cost_per_calls(
        input_tokens, output_tokens, model, calls_per_day * days, cache_hit_ratio
    )


def project(
    input_tokens: int,
    output_tokens: int,
    model_ids: list[str],
    calls_per_day: int,
    days: int,
    cache_hit_ratio: float = 0.0,
) -> list[tuple[str, float]]:
    """Monthly cost for several models, sorted by cost then id."""
    table = load_pricing()
    rows = []
    for model_id in model_ids:
        model = table.model(model_id)
        rows.append(
            (
                model.id,
                round(
                    monthly_cost(
                        input_tokens,
                        output_tokens,
                        model,
                        calls_per_day,
                        days,
                        cache_hit_ratio,
                    ),
                    2,
                ),
            )
        )
    rows.sort(key=lambda row: (row[1], row[0]))
    return rows
