"""Deterministic invoice validation. No LLM involved.

Arithmetic, required fields, date coherence and duplicate detection are all
decidable in plain Python, so they are checked in plain Python: same input,
same findings, every time, at no token cost.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import date

from .models import (
    Invoice,
    IssueCode,
    TotalsPath,
    ValidationIssue,
    ValidationResult,
)

#: Money comparisons tolerate half a cent of rounding on each side.
TOLERANCE = 0.01

#: Fields an invoice must state to be processable without human review.
REQUIRED_FIELDS: tuple[str, ...] = (
    "vendor_name",
    "vendor_tax_id",
    "invoice_number",
    "issue_date",
    "currency",
    "subtotal",
    "total",
)

#: Plausible range for a sales-tax/VAT rate, as a percentage.
MIN_TAX_RATE = 0.0
MAX_TAX_RATE = 30.0


def validate_invoice(invoice: Invoice) -> ValidationResult:
    """Run every single-invoice rule and collect the findings.

    Duplicate detection needs the whole batch and is not applied here; use
    `validate_batch` for that.

    The returned result carries `totals_path`, recording which reconciliation
    closed the arithmetic: `direct` for subtotal + tax == total, `adjusted` when
    it only closed after applying document-level adjustments.
    """
    issues: list[ValidationIssue] = []
    issues.extend(_check_required_fields(invoice))
    issues.extend(_check_line_items(invoice))

    totals_issues, totals_path = _check_totals(invoice)
    issues.extend(totals_issues)

    issues.extend(_check_tax_rate(invoice))
    issues.extend(_check_dates(invoice))
    return ValidationResult.from_issues(issues, totals_path)


def validate_batch(
    invoices: Mapping[str, Invoice | None],
) -> dict[str, ValidationResult]:
    """Validate a whole batch, including cross-invoice duplicate detection.

    Args:
        invoices: Maps a file name to its extracted invoice, or to `None` when
            extraction failed for that file.

    Returns:
        A result per input key, in the same order.
    """
    duplicates = find_duplicates(invoices)

    results: dict[str, ValidationResult] = {}
    for name, invoice in invoices.items():
        if invoice is None:
            results[name] = ValidationResult.from_issues(
                [
                    ValidationIssue(
                        code=IssueCode.EXTRACTION_FAILED,
                        field="",
                        message="Extraction did not produce an invoice.",
                    )
                ]
            )
            continue

        single = validate_invoice(invoice)
        issues = list(single.issues)
        if name in duplicates:
            issues.append(duplicates[name])
        results[name] = ValidationResult.from_issues(issues, single.totals_path)

    return results


def find_duplicates(
    invoices: Mapping[str, Invoice | None],
) -> dict[str, ValidationIssue]:
    """Flag invoices sharing a vendor and invoice number.

    Every member of a duplicate group is flagged, not just the later ones: from
    the batch's point of view there is no principled "original", and a human
    needs to look at all of them.

    Returns:
        An issue per offending file name. Files with a unique key are absent.
    """
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)

    for name, invoice in invoices.items():
        if invoice is None:
            continue
        key = _duplicate_key(invoice)
        if key is not None:
            groups[key].append(name)

    issues: dict[str, ValidationIssue] = {}
    for (vendor, number), names in groups.items():
        if len(names) < 2:
            continue
        for name in names:
            others = ", ".join(sorted(n for n in names if n != name))
            issues[name] = ValidationIssue(
                code=IssueCode.DUPLICATE_INVOICE,
                field="invoice_number",
                message=(
                    f"Invoice number {number!r} from vendor {vendor!r} also "
                    f"appears in: {others}."
                ),
            )

    return issues


def _duplicate_key(invoice: Invoice) -> tuple[str, str] | None:
    """Normalized (vendor, number) identity, or `None` if either is missing."""
    if not invoice.vendor_name or not invoice.invoice_number:
        return None
    return (
        invoice.vendor_name.strip().casefold(),
        invoice.invoice_number.strip().casefold(),
    )


def _check_required_fields(invoice: Invoice) -> list[ValidationIssue]:
    """Report any required field that is absent or blank."""
    issues: list[ValidationIssue] = []
    for field in REQUIRED_FIELDS:
        value = getattr(invoice, field)
        if value is None or (isinstance(value, str) and not value.strip()):
            issues.append(
                ValidationIssue(
                    code=IssueCode.MISSING_REQUIRED_FIELD,
                    field=field,
                    message=f"Required field {field!r} is missing.",
                )
            )
    return issues


def _check_line_items(invoice: Invoice) -> list[ValidationIssue]:
    """Check that lines exist, each line's math holds, and they sum to subtotal."""
    if not invoice.line_items:
        return [
            ValidationIssue(
                code=IssueCode.NO_LINE_ITEMS,
                field="line_items",
                message="Invoice has no line items.",
            )
        ]

    issues: list[ValidationIssue] = []

    for index, item in enumerate(invoice.line_items):
        if item.amount is None or item.quantity is None or item.unit_price is None:
            continue
        expected = item.quantity * item.unit_price
        if not _close(item.amount, expected):
            issues.append(
                ValidationIssue(
                    code=IssueCode.LINE_ITEM_AMOUNT_MISMATCH,
                    field=f"line_items[{index}].amount",
                    message=(
                        f"Line amount {item.amount:.2f} does not match quantity x "
                        f"unit_price ({expected:.2f})."
                    ),
                )
            )

    amounts = [item.amount for item in invoice.line_items if item.amount is not None]
    if invoice.subtotal is not None and len(amounts) == len(invoice.line_items):
        line_sum = sum(amounts)
        if not _close(line_sum, invoice.subtotal):
            issues.append(
                ValidationIssue(
                    code=IssueCode.LINE_ITEMS_SUM_MISMATCH,
                    field="subtotal",
                    message=(
                        f"Line items sum to {line_sum:.2f} but subtotal is "
                        f"{invoice.subtotal:.2f}."
                    ),
                )
            )

    return issues


def _check_totals(
    invoice: Invoice,
) -> tuple[list[ValidationIssue], TotalsPath | None]:
    """Reconcile the totals, trying the simple arithmetic first.

    Two paths, in order:

    1. **direct** - ``subtotal + tax_amount == total``. Covers the ordinary
       invoice, where tax is charged on the subtotal.
    2. **adjusted** - only attempted when the invoice carries document-level
       adjustments or states a taxable base. Tax is charged on
       ``subtotal + sum(adjustments)`` rather than on the subtotal, so all
       three of these must hold: the adjustments reach the stated taxable base,
       the tax matches the rate applied to that base, and base + tax reaches
       the total.

    A missing `tax_amount` is read as zero tax rather than skipped: plenty of
    real invoices omit the line, and `subtotal == total` should still hold.

    Returns:
        The issues found (empty when the totals reconcile) and the path that
        closed, or `None` if neither did.
    """
    if invoice.subtotal is None or invoice.total is None:
        return [], None

    tax = invoice.tax_amount if invoice.tax_amount is not None else 0.0
    direct = invoice.subtotal + tax
    if _close(direct, invoice.total):
        return [], "direct"

    has_adjustments = bool(invoice.adjustments) or invoice.taxable_base is not None
    if not has_adjustments:
        return [
            ValidationIssue(
                code=IssueCode.TOTAL_MISMATCH,
                field="total",
                message=(
                    f"Subtotal {invoice.subtotal:.2f} plus tax {tax:.2f} is "
                    f"{direct:.2f}, but total is {invoice.total:.2f}."
                ),
            )
        ], None

    return _check_adjusted_totals(invoice, tax, direct)


def _check_adjusted_totals(
    invoice: Invoice, tax: float, direct: float
) -> tuple[list[ValidationIssue], TotalsPath | None]:
    """Reconcile totals through document-level adjustments.

    Only called once the direct path has already failed and the invoice has
    something to adjust with.
    """
    assert invoice.subtotal is not None and invoice.total is not None

    adjustments_total = _round(
        sum(item.amount for item in invoice.adjustments if item.amount is not None)
    )
    derived_base = _round(invoice.subtotal + adjustments_total)
    base = invoice.taxable_base if invoice.taxable_base is not None else derived_base

    failures: list[str] = []

    # The stated taxable base, when printed, must be what the adjustments reach.
    if invoice.taxable_base is not None and not _close(derived_base, base):
        failures.append(
            f"subtotal {invoice.subtotal:.2f} plus adjustments "
            f"{adjustments_total:.2f} is {derived_base:.2f}, but the stated "
            f"taxable base is {base:.2f}"
        )

    # The tax must be the rate applied to that base, when a rate is stated.
    if invoice.tax_rate is not None:
        expected_tax = _round(base * invoice.tax_rate / 100.0)
        if not _close(expected_tax, tax):
            failures.append(
                f"{invoice.tax_rate:.2f}% of the taxable base {base:.2f} is "
                f"{expected_tax:.2f}, but tax is {tax:.2f}"
            )

    # And base plus tax must reach the printed total.
    adjusted = _round(base + tax)
    if not _close(adjusted, invoice.total):
        failures.append(
            f"taxable base {base:.2f} plus tax {tax:.2f} is {adjusted:.2f}, "
            f"but total is {invoice.total:.2f}"
        )

    if not failures:
        return [], "adjusted"

    return [
        ValidationIssue(
            code=IssueCode.TOTAL_MISMATCH,
            field="total",
            message=(
                f"Totals do not reconcile. Direct: subtotal "
                f"{invoice.subtotal:.2f} plus tax {tax:.2f} is {direct:.2f}, "
                f"but total is {invoice.total:.2f}. With adjustments: "
                + "; ".join(failures)
                + "."
            ),
        )
    ], None


def _check_tax_rate(invoice: Invoice) -> list[ValidationIssue]:
    """Check that the tax rate is a plausible percentage."""
    if invoice.tax_rate is None:
        return []
    if MIN_TAX_RATE <= invoice.tax_rate <= MAX_TAX_RATE:
        return []

    return [
        ValidationIssue(
            code=IssueCode.TAX_RATE_OUT_OF_RANGE,
            field="tax_rate",
            message=(
                f"Tax rate {invoice.tax_rate:.2f}% is outside the plausible range "
                f"{MIN_TAX_RATE:.0f}-{MAX_TAX_RATE:.0f}%."
            ),
        )
    ]


def _check_dates(invoice: Invoice) -> list[ValidationIssue]:
    """Check date parseability and that the due date is not before issue."""
    issues: list[ValidationIssue] = []

    parsed: dict[str, date] = {}
    for field in ("issue_date", "due_date"):
        raw = getattr(invoice, field)
        if raw is None:
            continue
        value = _parse_date(raw)
        if value is None:
            issues.append(
                ValidationIssue(
                    code=IssueCode.INVALID_DATE_FORMAT,
                    field=field,
                    message=f"Could not parse {field} {raw!r} as an ISO 8601 date.",
                )
            )
        else:
            parsed[field] = value

    issue_date = parsed.get("issue_date")
    due_date = parsed.get("due_date")
    if issue_date and due_date and due_date < issue_date:
        issues.append(
            ValidationIssue(
                code=IssueCode.DUE_DATE_BEFORE_ISSUE_DATE,
                field="due_date",
                message=(
                    f"Due date {due_date.isoformat()} is before issue date "
                    f"{issue_date.isoformat()}."
                ),
            )
        )

    return issues


def _parse_date(raw: str) -> date | None:
    """Parse an ISO 8601 date, returning `None` for anything unparseable."""
    try:
        return date.fromisoformat(raw.strip())
    except (ValueError, AttributeError):
        return None


def _close(left: float, right: float, tolerance: float = TOLERANCE) -> bool:
    """Compare two amounts within `tolerance`, inclusive."""
    return abs(left - right) <= tolerance + 1e-9


def _round(value: float) -> float:
    """Round to cents, so intermediate sums don't drift into the tolerance."""
    return round(value + 0.0, 2)
