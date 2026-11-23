"""Directory scanner: load files, count tokens, run detectors, project cost."""
from __future__ import annotations

import pathlib
from dataclasses import dataclass

from .detectors import Counter, run_detectors
from .loader import iter_source_files, load_file
from .models import Document, ModelCost, ScanResult, Segment
from .pricing import PricingTable, cost_per_calls, load_pricing
from .tokenizer import get_tokenizer


class ScanError(ValueError):
    """Raised when a scan target does not exist or cannot be read."""


@dataclass
class TrafficProfile:
    """Assumptions used to turn a token count into a monthly dollar figure."""

    calls_per_day: int = 1000
    days: int = 30
    output_ratio: float = 0.35
    cache_hit_ratio: float = 0.0

    def label(self) -> str:
        """Short human-readable assumption string."""
        return "%d calls/day x %d days, %.0f%% output, %.0f%% cache hit" % (
            self.calls_per_day,
            self.days,
            100.0 * self.output_ratio,
            100.0 * self.cache_hit_ratio,
        )


DEFAULT_TOP = 20


def collect_documents(
    target: pathlib.Path, max_files: int = 20000
) -> tuple[list[Document], int, int, int]:
    """Load every scannable file under target.

    Returns (documents, files_scanned, files_skipped, bytes_scanned).
    """
    if not target.exists():
        raise ScanError(
            "no such file or directory: %s. Pass a prompt directory, a .jsonl "
            "chat export or a .json schema." % target
        )
    documents: list[Document] = []
    scanned = 0
    skipped = 0
    total_bytes = 0
    root = target if target.is_dir() else target.parent
    for path in iter_source_files(target):
        if scanned >= max_files:
            skipped += 1
            continue
        document = load_file(path, root)
        if document is None:
            skipped += 1
            continue
        documents.append(document)
        scanned += 1
        total_bytes += document.size_bytes
    documents.sort(key=lambda document: document.path)
    return documents, scanned, skipped, total_bytes


def scan(
    target: str | pathlib.Path,
    model_id: str = "gpt-4o",
    traffic: TrafficProfile | None = None,
    pricing: PricingTable | None = None,
    top: int = DEFAULT_TOP,
) -> ScanResult:
    """Run a full audit over a file or directory."""
    profile = traffic or TrafficProfile()
    table = pricing or load_pricing()
    chosen = table.model(model_id)
    target_path = pathlib.Path(target)
    documents, scanned, skipped, total_bytes = collect_documents(target_path)

    encodings = sorted({chosen.encoding})
    counters: dict[str, Counter] = {}
    tokens_by_encoding: dict[str, int] = {}
    for encoding in encodings:
        tokenizer = get_tokenizer(encoding)
        counters[encoding] = Counter(tokenizer.count)
        tokens_by_encoding[encoding] = sum(
            counters[encoding](segment.text)
            for document in documents
            for segment in document.segments
        )
    counter = counters[chosen.encoding]

    aggregated: dict[str, tuple[int, Segment, int]] = {}
    for document in documents:
        for segment in document.segments:
            key = segment.text
            tokens = counter(segment.text)
            existing = aggregated.get(key)
            if existing is None or segment.source < existing[1].source:
                aggregated[key] = (tokens, segment, (existing[2] if existing else 0) + 1)
            else:
                aggregated[key] = (existing[0], existing[1], existing[2] + 1)
    top_segments = sorted(
        aggregated.values(), key=lambda item: (-item[0], item[1].source, item[1].line)
    )[: max(1, top)]

    findings = run_detectors(
        documents,
        counter,
        chosen,
        profile.calls_per_day,
        profile.days,
    )

    average_input = (
        round(tokens_by_encoding[chosen.encoding] / scanned) if scanned else 0
    )
    calls = profile.calls_per_day * profile.days
    costs: list[ModelCost] = []
    for model in sorted(table.models.values(), key=lambda item: item.id):
        per_thousand = cost_per_calls(
            average_input,
            max(1, int(average_input * profile.output_ratio)),
            model,
            1000,
            profile.cache_hit_ratio,
        )
        monthly = per_thousand * calls / 1000
        costs.append(
            ModelCost(
                model_id=model.id,
                provider=model.provider,
                input_usd_per_1k_calls=round(per_thousand, 4),
                output_usd_per_1k_calls=round(
                    max(1, int(average_input * profile.output_ratio))
                    * model.output
                    / 1_000_000
                    * 1000,
                    4,
                ),
                monthly_usd=round(monthly, 2),
            )
        )

    assumptions: dict[str, str | float | int] = {
        "model": chosen.id,
        "calls_per_day": profile.calls_per_day,
        "days": profile.days,
        "output_ratio": profile.output_ratio,
        "cache_hit_ratio": profile.cache_hit_ratio,
        "avg_input_tokens_per_file": average_input,
        "profile": profile.label(),
    }
    return ScanResult(
        root=str(target_path),
        documents=documents,
        findings=findings,
        tokens_by_encoding=tokens_by_encoding,
        costs=costs,
        top_segments=top_segments,
        assumptions=assumptions,
        pricing_snapshot=table.snapshot_date,
        pricing_currency=table.currency,
        files_scanned=scanned,
        files_skipped=skipped,
        bytes_scanned=total_bytes,
    )


def json_ready(value):
    """Convert dataclasses and enums into JSON-serialisable values."""
    if hasattr(value, "__dataclass_fields__"):
        return {key: json_ready(getattr(value, key)) for key in value.__dataclass_fields__}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if hasattr(value, "value") and hasattr(value, "name"):
        return value.value
    return value
