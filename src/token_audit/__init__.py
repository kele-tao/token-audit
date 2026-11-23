"""token-audit: find wasted tokens and model spend in prompts, tool schemas and chat logs.

Pure Python, no API calls, no tokenizer dependency.  See README.md for the
product tour and docs/ARCHITECTURE.md for how the pieces fit together.
"""
from __future__ import annotations

__version__ = "1.0.0"

from .accuracy import AccuracyReport, measure, measure_all
from .budget import BudgetReport, build_budget, load_plan
from .models import Document, Finding, Kind, ScanResult, Segment, Severity
from .optimize import OptimizeResult, optimize_text
from .pricing import Model, PricingError, PricingTable, load_pricing
from .scanner import ScanError, TrafficProfile, scan
from .tokenizer import ENCODINGS, BPETokenizer, HeuristicTokenizer, get_tokenizer

__all__ = [
    "ENCODINGS",
    "AccuracyReport",
    "BPETokenizer",
    "BudgetReport",
    "Document",
    "Finding",
    "HeuristicTokenizer",
    "Kind",
    "Model",
    "OptimizeResult",
    "PricingError",
    "PricingTable",
    "ScanError",
    "ScanResult",
    "Segment",
    "Severity",
    "TrafficProfile",
    "__version__",
    "build_budget",
    "get_tokenizer",
    "load_plan",
    "load_pricing",
    "measure",
    "measure_all",
    "optimize_text",
    "scan",
]
