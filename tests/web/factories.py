"""Builders shared by the web tests: fake extractors and sample PDFs."""

from __future__ import annotations

from pathlib import Path

from invoice_extractor.models import ExtractionResult, Invoice, TokenUsage

#: Bytes that pass the PDF magic-number check. Never parsed locally, because
#: the extractor is always faked in tests.
PDF_BYTES = b"%PDF-1.4\nfake invoice\n%%EOF"


def pdf_bytes(marker: str = "a") -> bytes:
    """Distinct PDF-ish bytes, so each upload gets its own sha256."""
    return b"%PDF-1.4\n" + marker.encode() * 32 + b"\n%%EOF"


def invoice(**overrides) -> Invoice:
    """A consistent invoice that passes every validation rule."""
    defaults = {
        "vendor_name": "Northwind Logistics LLC",
        "vendor_tax_id": "47-8821093",
        "invoice_number": "NL-2026-1011",
        "issue_date": "2026-03-21",
        "due_date": "2026-05-05",
        "currency": "USD",
        "line_items": [
            {
                "description": "Freight forwarding",
                "quantity": 2.0,
                "unit_price": 100.0,
                "amount": 200.0,
            }
        ],
        "subtotal": 200.0,
        "tax_rate": 10.0,
        "tax_amount": 20.0,
        "total": 220.0,
    }
    return Invoice.model_validate({**defaults, **overrides})


def result(extracted: Invoice | None = None, **overrides) -> ExtractionResult:
    """An `ExtractionResult` as the engine would return it."""
    defaults = {
        "file": "upload.pdf",
        "invoice": invoice() if extracted is None else extracted,
        "usage": TokenUsage(input_tokens=4000, output_tokens=500),
        "model": "claude-haiku-5-5",
        "attempts": 1,
        "duration_ms": 1234,
    }
    return ExtractionResult.model_validate({**defaults, **overrides})


class FakeExtractor:
    """Stands in for `InvoiceExtractor`, returning scripted results.

    Results are consumed in call order, falling back to `default` once the
    script runs out. Deliberately not keyed by filename: PDFs are stored
    content-addressed, so what reaches `extract_file` is `<sha256>.pdf`, and a
    name-keyed script would silently miss and return the wrong result.
    """

    settings = None

    def __init__(
        self,
        results: list[ExtractionResult] | None = None,
        default: ExtractionResult | None = None,
        barrier=None,
    ):
        self._results = list(results or [])
        self._default = default
        #: Optional threading.Barrier. Workers sharing one are released from
        #: extraction together, so they enter the critical section at the same
        #: moment instead of merely near each other.
        self._barrier = barrier
        self.calls: list[Path] = []

    def extract_file(self, path) -> ExtractionResult:
        path = Path(path)
        self.calls.append(path)
        if self._barrier is not None:
            self._barrier.wait(timeout=10)
        if self._results:
            return self._results.pop(0)
        return self._default or result(file=path.name)


def failing_extractor(message: str = "APITimeoutError: timed out") -> FakeExtractor:
    """An extractor whose every call comes back with no invoice."""
    return FakeExtractor(default=result(extracted=None, invoice=None, error=message))
