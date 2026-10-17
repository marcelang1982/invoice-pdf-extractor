"""Scoring and stability measurement in scripts/evaluate.py.

These are pure functions over already-extracted results, so nothing here needs
the API.
"""

from __future__ import annotations

import evaluate as ev
import pytest

from invoice_extractor.models import (
    BatchResult,
    FileResult,
    Invoice,
    TokenUsage,
)


def make_truth(file: str, invoice: dict) -> dict:
    """A ground truth record in the shape the generators write."""
    return {
        "file": file,
        "layout": "test",
        "defect": None,
        "expected_status": "valid",
        "expected_issues": [],
        "invoice": invoice,
    }


def make_batch(file: str, invoice: dict) -> BatchResult:
    """A single-file batch carrying one extracted invoice."""
    return BatchResult(
        model="test-model",
        results=[
            FileResult(
                file=file,
                status="valid",
                invoice=Invoice.model_validate(invoice),
                usage=TokenUsage(input_tokens=100, output_tokens=20),
                attempts=1,
            )
        ],
    )


@pytest.fixture
def line_items_only() -> dict:
    """An invoice with line items and no document-level adjustments."""
    return {
        "vendor_name": "Acme",
        "vendor_tax_id": "47-8821093",
        "invoice_number": "A-1",
        "issue_date": "2026-03-01",
        "due_date": "2026-03-31",
        "currency": "USD",
        "line_items": [
            {"description": "Widget", "quantity": 2.0, "unit_price": 10.0, "amount": 20.0},
            {"description": "Gadget", "quantity": 1.0, "unit_price": 30.0, "amount": 30.0},
        ],
        "adjustments": [],
        "subtotal": 50.0,
        "taxable_base": None,
        "tax_rate": 10.0,
        "tax_amount": 5.0,
        "total": 55.0,
    }


# --------------------------------------------------------------------------- #
# Stability keys must be scoped to where the field lives
# --------------------------------------------------------------------------- #


def test_stability_keys_are_scoped_by_location(line_items_only):
    """`description` and `amount` exist on both line items and adjustments.

    Keyed by the bare field name they pool together, and an adjustments row
    silently reports line-item stability.
    """
    truths = {"a.pdf": make_truth("a.pdf", line_items_only)}
    runs = [
        ev.collect_values(make_batch("a.pdf", line_items_only), truths)
        for _ in range(2)
    ]

    stability, _, _ = ev.measure_stability(runs)

    assert stability["line_items[].description"].total == 2
    assert stability["line_items[].amount"].total == 2
    # No adjustments in the corpus, so these must not exist at all.
    assert "adjustments[].description" not in stability
    assert "adjustments[].amount" not in stability
    # And the ambiguous bare keys must be gone entirely.
    assert "description" not in stability
    assert "amount" not in stability


def test_adjustment_columns_report_na_without_adjustments(line_items_only, capsys):
    """The end-to-end symptom: n/a in all three adjustments[] columns."""
    truths = {"a.pdf": make_truth("a.pdf", line_items_only)}
    batches = [make_batch("a.pdf", line_items_only) for _ in range(2)]

    multi = ev.MultiRunReport(
        runs=2, per_run=[ev.evaluate(batch, truths) for batch in batches]
    )
    values = [ev.collect_values(batch, truths) for batch in batches]
    multi.stability, multi.overall_stability, multi.unstable = ev.measure_stability(
        values
    )

    ev.print_multi_report(multi, set_name="test")
    lines = capsys.readouterr().out.splitlines()

    for name in ev.ADJUSTMENT_FIELDS:
        row = next(line for line in lines if line.startswith(f"adjustments[].{name}"))
        assert row.split() == [f"adjustments[].{name}", "n/a", "n/a", "n/a"], row


def test_line_item_columns_still_report_numbers(line_items_only, capsys):
    """The scoping fix must not blank out the fields that are exercised."""
    truths = {"a.pdf": make_truth("a.pdf", line_items_only)}
    batches = [make_batch("a.pdf", line_items_only) for _ in range(2)]

    multi = ev.MultiRunReport(
        runs=2, per_run=[ev.evaluate(batch, truths) for batch in batches]
    )
    values = [ev.collect_values(batch, truths) for batch in batches]
    multi.stability, multi.overall_stability, multi.unstable = ev.measure_stability(
        values
    )

    ev.print_multi_report(multi, set_name="test")
    row = next(
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("line_items[].description")
    )

    assert row.split() == ["line_items[].description", "100.0%", "100.0%", "100.0%"]


# --------------------------------------------------------------------------- #
# Stability detection itself
# --------------------------------------------------------------------------- #


def test_identical_runs_are_fully_stable(line_items_only):
    truths = {"a.pdf": make_truth("a.pdf", line_items_only)}
    runs = [
        ev.collect_values(make_batch("a.pdf", line_items_only), truths)
        for _ in range(3)
    ]

    _, overall, unstable = ev.measure_stability(runs)

    assert overall.accuracy == 1.0
    assert unstable == []


def test_a_differing_run_is_reported_as_unstable(line_items_only):
    """One run reading a description differently must surface, with both values."""
    drifted = {
        **line_items_only,
        "line_items": [
            {**line_items_only["line_items"][0], "description": "Widgets"},
            line_items_only["line_items"][1],
        ],
    }
    truths = {"a.pdf": make_truth("a.pdf", line_items_only)}
    runs = [
        ev.collect_values(make_batch("a.pdf", line_items_only), truths),
        ev.collect_values(make_batch("a.pdf", drifted), truths),
    ]

    stability, overall, unstable = ev.measure_stability(runs)

    assert [item.path for item in unstable] == ["line_items[0].description"]
    assert unstable[0].values == ["widget", "widgets"]
    assert stability["line_items[].description"].accuracy == 0.5
    assert overall.accuracy < 1.0


def test_adjustments_are_scored_when_the_corpus_has_them(line_items_only):
    """With adjustments present, their columns must carry real numbers."""
    with_adjustments = {
        **line_items_only,
        "adjustments": [
            {"description": "Volume discount", "amount": -5.0, "kind": "discount"}
        ],
    }
    truths = {"a.pdf": make_truth("a.pdf", with_adjustments)}
    runs = [
        ev.collect_values(make_batch("a.pdf", with_adjustments), truths)
        for _ in range(2)
    ]

    stability, _, _ = ev.measure_stability(runs)

    assert stability["adjustments[].description"].total == 1
    assert stability["adjustments[].kind"].total == 1
    assert stability["line_items[].description"].total == 2
