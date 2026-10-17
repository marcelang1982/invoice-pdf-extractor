"""Extract-then-validate pipeline over a batch of PDFs.

This is the entry point later phases import: give it PDFs, get back a
`BatchResult` that is ready to serialize, persist, or render.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Callable, Iterable

from .extractor import InvoiceExtractor, find_pdfs
from .models import (
    BatchResult,
    BatchTotals,
    ExtractionResult,
    FileResult,
    Invoice,
    TokenUsage,
)
from .validator import validate_batch

#: Called with each `ExtractionResult` as it completes, for progress reporting.
ProgressHook = Callable[[ExtractionResult], None]


def process_paths(
    paths: Iterable[str | Path],
    extractor: InvoiceExtractor | None = None,
    on_result: ProgressHook | None = None,
) -> BatchResult:
    """Extract and validate a batch of PDFs.

    Validation runs once over the whole batch so that duplicate invoices are
    detected across files, not just within one.

    Args:
        paths: PDF paths to process, in order.
        extractor: Extractor to use. Built from the environment when omitted.
        on_result: Optional callback invoked after each extraction, before
            validation, so callers can stream progress.

    Returns:
        A `BatchResult` holding one `FileResult` per input plus batch totals.
    """
    extractor = extractor or InvoiceExtractor()

    extractions: list[ExtractionResult] = []
    for path in paths:
        result = extractor.extract_file(path)
        extractions.append(result)
        if on_result is not None:
            on_result(result)

    invoices: OrderedDict[str, Invoice | None] = OrderedDict(
        (item.file, item.invoice) for item in extractions
    )
    validations = validate_batch(invoices)

    results: list[FileResult] = []
    for item in extractions:
        validation = validations[item.file]
        results.append(
            FileResult(
                file=item.file,
                status=validation.status,
                issues=validation.issues,
                totals_path=validation.totals_path,
                invoice=item.invoice,
                usage=item.usage,
                attempts=item.attempts,
                duration_ms=item.duration_ms,
                error=item.error,
            )
        )

    return BatchResult(
        model=extractor.settings.model,
        totals=_totals(results),
        results=results,
    )


def process_directory(
    directory: str | Path,
    extractor: InvoiceExtractor | None = None,
    on_result: ProgressHook | None = None,
) -> BatchResult:
    """Process every `*.pdf` in a directory, sorted by filename."""
    return process_paths(find_pdfs(directory), extractor, on_result)


def _totals(results: list[FileResult]) -> BatchTotals:
    """Aggregate counts and token usage across a batch."""
    usage = TokenUsage()
    for item in results:
        usage = usage + item.usage

    return BatchTotals(
        files=len(results),
        valid=sum(1 for item in results if item.status == "valid"),
        needs_review=sum(1 for item in results if item.status == "needs_review"),
        failed=sum(1 for item in results if item.invoice is None),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )
