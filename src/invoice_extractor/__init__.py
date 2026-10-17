"""Invoice PDF extraction and validation engine.

Public API, stable for the phases built on top of this package:

    from invoice_extractor import InvoiceExtractor, process_directory

    batch = process_directory("samples/pdfs")
    for result in batch.results:
        print(result.file, result.status, len(result.issues))
"""

from .config import ConfigError, Settings
from .extractor import ExtractionError, InvoiceExtractor, find_pdfs
from .models import (
    Adjustment,
    AdjustmentKind,
    BatchResult,
    BatchTotals,
    ExtractionResult,
    FileResult,
    Invoice,
    IssueCode,
    LineItem,
    TokenUsage,
    TotalsPath,
    ValidationIssue,
    ValidationResult,
    invoice_input_schema,
)
from .pipeline import process_directory, process_paths
from .validator import (
    duplicate_key,
    find_duplicates,
    validate_batch,
    validate_invoice,
)

__version__ = "0.1.0"

__all__ = [
    "Adjustment",
    "AdjustmentKind",
    "BatchResult",
    "BatchTotals",
    "ConfigError",
    "ExtractionError",
    "ExtractionResult",
    "FileResult",
    "Invoice",
    "InvoiceExtractor",
    "IssueCode",
    "LineItem",
    "Settings",
    "TokenUsage",
    "TotalsPath",
    "ValidationIssue",
    "ValidationResult",
    "__version__",
    "duplicate_key",
    "find_duplicates",
    "find_pdfs",
    "invoice_input_schema",
    "process_directory",
    "process_paths",
    "validate_batch",
    "validate_invoice",
]
