"""Core data structures shared by the scanner, detectors and reporters."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Kind(str, Enum):
    """What kind of content a document segment holds."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SCHEMA = "schema"
    MARKDOWN = "markdown"
    TEXT = "text"
    TABLE = "table"
    EXAMPLE = "example"


class Severity(str, Enum):
    """How much a finding is worth acting on."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


@dataclass(frozen=True)
class Segment:
    """One billable string extracted from a source file."""

    text: str
    kind: Kind = Kind.TEXT
    label: str = ""
    line: int = 0
    source: str = ""

    @property
    def chars(self) -> int:
        """Character length of the segment."""
        return len(self.text)


@dataclass
class Document:
    """A parsed source file."""

    path: str
    kind: str
    size_bytes: int
    segments: list[Segment] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    raw_text: str = ""

    @property
    def chars(self) -> int:
        """Total characters across all segments."""
        return sum(segment.chars for segment in self.segments)


@dataclass(frozen=True)
class Finding:
    """A single waste detector result."""

    detector: str
    title: str
    severity: Severity
    wasted_tokens: int
    monthly_saving_usd: float
    evidence: tuple[str, ...]
    recommendation: str
    fixable: bool = False

    @property
    def sort_key(self) -> tuple[float, str]:
        """Deterministic ordering: biggest saving first, then detector name."""
        return (-self.monthly_saving_usd, self.detector + ":" + self.title)


@dataclass
class ModelCost:
    """Cost of one model for a fixed traffic profile."""

    model_id: str
    provider: str
    input_usd_per_1k_calls: float
    output_usd_per_1k_calls: float
    monthly_usd: float


@dataclass
class ScanResult:
    """Everything a scan produced, ready to render."""

    root: str
    documents: list[Document]
    findings: list[Finding]
    tokens_by_encoding: dict[str, int]
    costs: list[ModelCost]
    top_segments: list[tuple[int, Segment, int]]
    assumptions: dict[str, str | float | int]
    pricing_snapshot: str
    pricing_currency: str
    files_scanned: int
    files_skipped: int
    bytes_scanned: int

    @property
    def total_tokens(self) -> int:
        """Token count for the encoding used by the default model."""
        if not self.tokens_by_encoding:
            return 0
        return sum(self.tokens_by_encoding.values()) // len(self.tokens_by_encoding)

    @property
    def monthly_saving_usd(self) -> float:
        """Sum of every finding's estimated monthly saving."""
        return round(sum(finding.monthly_saving_usd for finding in self.findings), 2)
