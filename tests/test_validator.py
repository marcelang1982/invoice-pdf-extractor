"""Validator rules, covered rule by rule including tolerance boundaries."""

from __future__ import annotations

import pytest

from invoice_extractor.models import Invoice, IssueCode
from invoice_extractor.validator import (
    MAX_TAX_RATE,
    REQUIRED_FIELDS,
    find_duplicates,
    validate_batch,
    validate_invoice,
)


def codes(result) -> set[str]:
    """The issue codes in a result, as plain strings."""
    return {issue.code.value for issue in result.issues}


def build(invoice_data: dict, **overrides) -> Invoice:
    """An invoice built from the consistent payload with fields overridden."""
    return Invoice.model_validate({**invoice_data, **overrides})


# --------------------------------------------------------------------------- #
# Baseline
# --------------------------------------------------------------------------- #


def test_consistent_invoice_is_valid(valid_invoice):
    result = validate_invoice(valid_invoice)
    assert result.status == "valid"
    assert result.issues == []


def test_status_is_needs_review_when_any_issue_fires(invoice_data):
    result = validate_invoice(build(invoice_data, total=999.0))
    assert result.status == "needs_review"
    assert result.issues


# --------------------------------------------------------------------------- #
# Required fields
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("missing", REQUIRED_FIELDS)
def test_each_required_field_is_enforced(invoice_data, missing):
    result = validate_invoice(build(invoice_data, **{missing: None}))

    issues = [i for i in result.issues if i.code is IssueCode.MISSING_REQUIRED_FIELD]
    assert [i.field for i in issues] == [missing]


def test_blank_string_counts_as_missing(invoice_data):
    result = validate_invoice(build(invoice_data, vendor_name="   "))

    assert IssueCode.MISSING_REQUIRED_FIELD.value in codes(result)


def test_optional_fields_may_be_absent(invoice_data):
    """`due_date` and `tax_rate` are not required; omitting them is clean."""
    invoice = build(invoice_data, due_date=None, tax_rate=None)

    assert validate_invoice(invoice).status == "valid"


# --------------------------------------------------------------------------- #
# Line items
# --------------------------------------------------------------------------- #


def test_no_line_items_is_flagged(invoice_data):
    result = validate_invoice(build(invoice_data, line_items=[]))

    assert IssueCode.NO_LINE_ITEMS.value in codes(result)


def test_line_amount_must_match_quantity_times_price(invoice_data):
    items = [
        {"description": "Widget", "quantity": 3.0, "unit_price": 10.0, "amount": 35.0},
    ]
    result = validate_invoice(
        build(invoice_data, line_items=items, subtotal=35.0, tax_amount=0.0,
              tax_rate=0.0, total=35.0)
    )

    issues = [i for i in result.issues if i.code is IssueCode.LINE_ITEM_AMOUNT_MISMATCH]
    assert len(issues) == 1
    assert issues[0].field == "line_items[0].amount"


def test_line_amount_check_skipped_when_inputs_are_missing(invoice_data):
    """A line with no quantity cannot be checked, and must not be guessed at."""
    items = [{"description": "Consulting", "quantity": None,
              "unit_price": None, "amount": 250.0}]
    result = validate_invoice(build(invoice_data, line_items=items))

    assert IssueCode.LINE_ITEM_AMOUNT_MISMATCH.value not in codes(result)
    assert result.status == "valid"


def test_line_items_must_sum_to_subtotal(invoice_data):
    result = validate_invoice(build(invoice_data, subtotal=300.0, total=325.0))

    issues = [i for i in result.issues if i.code is IssueCode.LINE_ITEMS_SUM_MISMATCH]
    assert len(issues) == 1
    assert issues[0].field == "subtotal"


def test_sum_check_skipped_when_a_line_amount_is_missing(invoice_data):
    """One unreadable amount makes the sum meaningless; don't cry wolf."""
    items = [
        {"description": "A", "quantity": 1.0, "unit_price": 100.0, "amount": 100.0},
        {"description": "B", "quantity": 1.0, "unit_price": 150.0, "amount": None},
    ]
    result = validate_invoice(build(invoice_data, line_items=items))

    assert IssueCode.LINE_ITEMS_SUM_MISMATCH.value not in codes(result)


# --------------------------------------------------------------------------- #
# Totals and tolerance
# --------------------------------------------------------------------------- #


def test_total_mismatch_is_flagged(invoice_data):
    result = validate_invoice(build(invoice_data, total=315.0))

    issues = [i for i in result.issues if i.code is IssueCode.TOTAL_MISMATCH]
    assert len(issues) == 1
    assert "315.00" in issues[0].message


@pytest.mark.parametrize("total", [275.0, 275.01, 274.99])
def test_total_within_tolerance_passes(invoice_data, total):
    result = validate_invoice(build(invoice_data, total=total))

    assert IssueCode.TOTAL_MISMATCH.value not in codes(result)


@pytest.mark.parametrize("total", [275.02, 274.98])
def test_total_just_outside_tolerance_is_flagged(invoice_data, total):
    result = validate_invoice(build(invoice_data, total=total))

    assert IssueCode.TOTAL_MISMATCH.value in codes(result)


def test_missing_tax_amount_is_read_as_zero_tax(invoice_data):
    """Untaxed invoices omit the tax line; subtotal must then equal total."""
    clean = build(invoice_data, tax_amount=None, tax_rate=None, total=250.0)
    assert IssueCode.TOTAL_MISMATCH.value not in codes(validate_invoice(clean))

    broken = build(invoice_data, tax_amount=None, tax_rate=None, total=275.0)
    assert IssueCode.TOTAL_MISMATCH.value in codes(validate_invoice(broken))


def test_total_check_skipped_without_a_subtotal(invoice_data):
    result = validate_invoice(build(invoice_data, subtotal=None))

    assert IssueCode.TOTAL_MISMATCH.value not in codes(result)


# --------------------------------------------------------------------------- #
# Tax rate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rate", [0.0, 7.0, 21.0, MAX_TAX_RATE])
def test_plausible_tax_rates_pass(invoice_data, rate):
    invoice = build(
        invoice_data,
        tax_rate=rate,
        tax_amount=round(250.0 * rate / 100, 2),
        total=round(250.0 + 250.0 * rate / 100, 2),
    )

    assert IssueCode.TAX_RATE_OUT_OF_RANGE.value not in codes(validate_invoice(invoice))


@pytest.mark.parametrize("rate", [-1.0, 30.01, 87.5, 210.0])
def test_implausible_tax_rates_are_flagged(invoice_data, rate):
    invoice = build(
        invoice_data,
        tax_rate=rate,
        tax_amount=round(250.0 * rate / 100, 2),
        total=round(250.0 + 250.0 * rate / 100, 2),
    )
    result = validate_invoice(invoice)

    assert IssueCode.TAX_RATE_OUT_OF_RANGE.value in codes(result)
    # The rate alone is at fault; the arithmetic still holds.
    assert IssueCode.TOTAL_MISMATCH.value not in codes(result)


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #


def test_due_date_before_issue_date_is_flagged(invoice_data):
    result = validate_invoice(build(invoice_data, due_date="2026-03-11"))

    issues = [i for i in result.issues if i.code is IssueCode.DUE_DATE_BEFORE_ISSUE_DATE]
    assert len(issues) == 1
    assert issues[0].field == "due_date"


@pytest.mark.parametrize("due", ["2026-03-21", "2026-03-22"])
def test_due_date_on_or_after_issue_date_passes(invoice_data, due):
    result = validate_invoice(build(invoice_data, due_date=due))

    assert IssueCode.DUE_DATE_BEFORE_ISSUE_DATE.value not in codes(result)


@pytest.mark.parametrize("raw", ["21/03/2026", "March 21, 2026", "2026-13-45", "soon"])
def test_unparseable_dates_are_flagged(invoice_data, raw):
    result = validate_invoice(build(invoice_data, issue_date=raw))

    assert IssueCode.INVALID_DATE_FORMAT.value in codes(result)


def test_unparseable_date_does_not_also_trigger_ordering(invoice_data):
    """With one date unreadable, ordering is unknowable, not violated."""
    result = validate_invoice(build(invoice_data, issue_date="21/03/2026"))

    assert IssueCode.DUE_DATE_BEFORE_ISSUE_DATE.value not in codes(result)


def test_whitespace_around_a_date_is_tolerated(invoice_data):
    result = validate_invoice(build(invoice_data, issue_date="  2026-03-21 "))

    assert IssueCode.INVALID_DATE_FORMAT.value not in codes(result)


# --------------------------------------------------------------------------- #
# Duplicates
# --------------------------------------------------------------------------- #


def test_duplicate_flags_every_member_of_the_group(invoice_data):
    batch = {
        "a.pdf": build(invoice_data),
        "b.pdf": build(invoice_data),
    }
    duplicates = find_duplicates(batch)

    assert set(duplicates) == {"a.pdf", "b.pdf"}
    assert "b.pdf" in duplicates["a.pdf"].message
    assert "a.pdf" in duplicates["b.pdf"].message


def test_three_way_duplicate_group(invoice_data):
    batch = {name: build(invoice_data) for name in ("a.pdf", "b.pdf", "c.pdf")}
    duplicates = find_duplicates(batch)

    assert set(duplicates) == {"a.pdf", "b.pdf", "c.pdf"}
    assert "b.pdf, c.pdf" in duplicates["a.pdf"].message


def test_duplicate_matching_ignores_case_and_whitespace(invoice_data):
    batch = {
        "a.pdf": build(invoice_data),
        "b.pdf": build(
            invoice_data,
            vendor_name="  northwind logistics llc ",
            invoice_number="nl-2026-1011 ",
        ),
    }

    assert set(find_duplicates(batch)) == {"a.pdf", "b.pdf"}


def test_different_invoice_numbers_are_not_duplicates(invoice_data):
    batch = {
        "a.pdf": build(invoice_data),
        "b.pdf": build(invoice_data, invoice_number="NL-2026-1012"),
    }

    assert find_duplicates(batch) == {}


def test_same_number_from_different_vendors_is_not_a_duplicate(invoice_data):
    batch = {
        "a.pdf": build(invoice_data),
        "b.pdf": build(invoice_data, vendor_name="Meridian Office Supply Co."),
    }

    assert find_duplicates(batch) == {}


def test_invoices_missing_an_identity_field_are_never_duplicates(invoice_data):
    """Two invoices with no number aren't duplicates of each other."""
    batch = {
        "a.pdf": build(invoice_data, invoice_number=None),
        "b.pdf": build(invoice_data, invoice_number=None),
    }

    assert find_duplicates(batch) == {}


def test_failed_extractions_are_skipped_by_duplicate_detection(invoice_data):
    batch = {"a.pdf": build(invoice_data), "b.pdf": None}

    assert find_duplicates(batch) == {}


# --------------------------------------------------------------------------- #
# Batch validation
# --------------------------------------------------------------------------- #


def test_validate_batch_merges_duplicate_findings(invoice_data):
    results = validate_batch({"a.pdf": build(invoice_data), "b.pdf": build(invoice_data)})

    for name in ("a.pdf", "b.pdf"):
        assert results[name].status == "needs_review"
        assert codes(results[name]) == {IssueCode.DUPLICATE_INVOICE.value}


def test_validate_batch_reports_failed_extractions(invoice_data):
    results = validate_batch({"a.pdf": build(invoice_data), "broken.pdf": None})

    assert results["a.pdf"].status == "valid"
    assert codes(results["broken.pdf"]) == {IssueCode.EXTRACTION_FAILED.value}


def test_validate_batch_preserves_input_order(invoice_data):
    names = ["c.pdf", "a.pdf", "b.pdf"]
    results = validate_batch({name: build(invoice_data, invoice_number=name)
                              for name in names})

    assert list(results) == names


def test_an_invoice_can_accumulate_several_issues(invoice_data):
    invoice = build(
        invoice_data,
        vendor_tax_id=None,
        due_date="2026-01-01",
        tax_rate=90.0,
        total=1000.0,
    )
    result = validate_invoice(invoice)

    assert codes(result) >= {
        IssueCode.MISSING_REQUIRED_FIELD.value,
        IssueCode.DUE_DATE_BEFORE_ISSUE_DATE.value,
        IssueCode.TAX_RATE_OUT_OF_RANGE.value,
        IssueCode.TOTAL_MISMATCH.value,
    }
