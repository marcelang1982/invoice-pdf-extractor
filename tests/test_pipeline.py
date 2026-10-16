"""The extract-then-validate pipeline, driven by a stub extractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from invoice_extractor.config import Settings
from invoice_extractor.models import ExtractionResult, Invoice, IssueCode, TokenUsage
from invoice_extractor.pipeline import process_paths


class StubExtractor:
    """Returns canned extraction results, keyed by filename."""

    def __init__(self, results: list[ExtractionResult], model: str = "test-model"):
        self.settings = Settings(api_key="k", model=model)
        self._results = {result.file: result for result in results}
        self.seen: list[str] = []

    def extract_file(self, path) -> ExtractionResult:
        name = Path(path).name
        self.seen.append(name)
        return self._results[name]


def extraction(file: str, invoice: Invoice | None, **kwargs) -> ExtractionResult:
    return ExtractionResult(
        file=file,
        invoice=invoice,
        usage=TokenUsage(
            input_tokens=kwargs.pop("input_tokens", 100),
            output_tokens=kwargs.pop("output_tokens", 20),
        ),
        **kwargs,
    )


@pytest.fixture
def invoice(invoice_data) -> Invoice:
    return Invoice.model_validate(invoice_data)


def test_results_follow_the_input_order(invoice):
    files = ["c.pdf", "a.pdf", "b.pdf"]
    extractor = StubExtractor(
        [
            extraction(name, invoice.model_copy(update={"invoice_number": name}))
            for name in files
        ]
    )

    batch = process_paths(files, extractor=extractor)

    assert [r.file for r in batch.results] == files
    assert extractor.seen == files


def test_validation_findings_land_on_the_right_file(invoice, invoice_data):
    broken = Invoice.model_validate({**invoice_data, "total": 999.0,
                                     "invoice_number": "OTHER-1"})
    extractor = StubExtractor(
        [extraction("good.pdf", invoice), extraction("bad.pdf", broken)]
    )

    batch = process_paths(["good.pdf", "bad.pdf"], extractor=extractor)
    by_file = {r.file: r for r in batch.results}

    assert by_file["good.pdf"].status == "valid"
    assert by_file["bad.pdf"].status == "needs_review"
    assert by_file["bad.pdf"].issues[0].code is IssueCode.TOTAL_MISMATCH


def test_duplicates_are_detected_across_files(invoice):
    """The whole batch is validated at once, which is what makes this possible."""
    extractor = StubExtractor(
        [extraction("a.pdf", invoice), extraction("b.pdf", invoice.model_copy())]
    )

    batch = process_paths(["a.pdf", "b.pdf"], extractor=extractor)

    for result in batch.results:
        assert result.issues[0].code is IssueCode.DUPLICATE_INVOICE


def test_a_failed_extraction_does_not_stop_the_batch(invoice):
    extractor = StubExtractor(
        [
            extraction("ok.pdf", invoice),
            extraction("broken.pdf", None, error="APITimeoutError: timed out"),
        ]
    )

    batch = process_paths(["ok.pdf", "broken.pdf"], extractor=extractor)
    by_file = {r.file: r for r in batch.results}

    assert by_file["ok.pdf"].status == "valid"
    assert by_file["broken.pdf"].issues[0].code is IssueCode.EXTRACTION_FAILED
    assert by_file["broken.pdf"].error == "APITimeoutError: timed out"


def test_totals_aggregate_counts_and_tokens(invoice, invoice_data):
    broken = Invoice.model_validate({**invoice_data, "total": 999.0,
                                     "invoice_number": "OTHER-1"})
    extractor = StubExtractor(
        [
            extraction("a.pdf", invoice, input_tokens=100, output_tokens=10),
            extraction("b.pdf", broken, input_tokens=200, output_tokens=20),
            extraction("c.pdf", None, input_tokens=50, output_tokens=5, error="boom"),
        ]
    )

    totals = process_paths(["a.pdf", "b.pdf", "c.pdf"], extractor=extractor).totals

    assert totals.files == 3
    assert totals.valid == 1
    assert totals.needs_review == 2
    assert totals.failed == 1
    assert totals.input_tokens == 350
    assert totals.output_tokens == 35
    assert totals.total_tokens == 385


def test_progress_hook_fires_once_per_file(invoice):
    extractor = StubExtractor(
        [extraction("a.pdf", invoice), extraction("b.pdf", invoice.model_copy())]
    )
    seen: list[str] = []

    process_paths(["a.pdf", "b.pdf"], extractor=extractor, on_result=seen.append)

    assert [r.file for r in seen] == ["a.pdf", "b.pdf"]


def test_batch_records_the_model_used(invoice):
    extractor = StubExtractor([extraction("a.pdf", invoice)], model="claude-sonnet-5")

    assert process_paths(["a.pdf"], extractor=extractor).model == "claude-sonnet-5"
