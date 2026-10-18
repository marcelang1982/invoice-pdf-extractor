"""The schema as migrated: constraints, defaults and cascades.

Named test_schema rather than test_models so the basename does not collide
with the engine's own tests/test_models.py.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError

from invoice_extractor import Invoice as ExtractedInvoice
from invoice_extractor import duplicate_key
from invoice_web.models import (
    Invoice,
    InvoiceStatus,
    ReviewStatus,
    ValidationIssue,
    decode_dup_key,
    encode_dup_key,
)


def make_invoice(**overrides) -> Invoice:
    defaults = {
        "filename": "a.pdf",
        "sha256": "a" * 64,
        "stored_path": "/data/pdfs/aaa.pdf",
        "status": InvoiceStatus.VALID,
    }
    return Invoice(**{**defaults, **overrides})


def test_an_invoice_round_trips(session):
    session.add(
        make_invoice(
            vendor_name="Northwind Logistics LLC",
            invoice_number="NL-2026-1011",
            issue_date=date(2026, 3, 21),
            total=Decimal("275.00"),
            currency="USD",
            extraction={"vendor_name": "Northwind Logistics LLC", "total": 275.0},
            model="claude-haiku-5-5",
            tokens_in=4000,
            tokens_out=500,
            processing_ms=2100,
        )
    )
    session.commit()

    stored = session.scalars(select(Invoice)).one()

    assert stored.total == Decimal("275.00")
    assert stored.issue_date == date(2026, 3, 21)
    assert stored.extraction["total"] == 275.0
    assert stored.processing_ms == 2100
    assert stored.uploaded_at is not None


def test_defaults_apply(session):
    """A freshly stored invoice is pending review and has cost nothing yet."""
    session.add(make_invoice(status=InvoiceStatus.PROCESSING))
    session.commit()

    stored = session.scalars(select(Invoice)).one()

    assert stored.review_status == ReviewStatus.PENDING
    assert stored.tokens_in == 0
    assert stored.tokens_out == 0
    assert stored.review_note is None
    assert stored.reviewed_at is None


def test_the_same_pdf_cannot_be_stored_twice(session):
    """The sha256 constraint is what makes re-upload detection reliable."""
    session.add(make_invoice())
    session.commit()

    session.add(make_invoice(filename="again.pdf", stored_path="/other.pdf"))
    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize("column", ["status", "review_status"])
def test_unknown_states_are_rejected_by_the_database(session, column):
    """The CHECK constraints keep a typo from inventing a new state."""
    session.add(make_invoice(**{column: "nonsense"}))

    with pytest.raises((IntegrityError, DBAPIError)):
        session.commit()


def test_issues_cascade_when_an_invoice_is_deleted(session):
    invoice = make_invoice()
    invoice.issues = [
        ValidationIssue(code="TOTAL_MISMATCH", field="total", message="off"),
        ValidationIssue(code="NO_LINE_ITEMS", field="line_items", message="empty"),
    ]
    session.add(invoice)
    session.commit()

    session.delete(invoice)
    session.commit()

    assert session.scalars(select(ValidationIssue)).all() == []


def test_an_issue_can_point_at_the_invoice_it_duplicates(session):
    original = make_invoice(sha256="b" * 64)
    session.add(original)
    session.commit()

    duplicate = make_invoice(
        sha256="c" * 64,
        status=InvoiceStatus.NEEDS_REVIEW,
        issues=[
            ValidationIssue(
                code="DUPLICATE_INVOICE",
                field="invoice_number",
                message="already stored",
                related_invoice_id=original.id,
            )
        ],
    )
    session.add(duplicate)
    session.commit()

    assert duplicate.issues[0].related_invoice_id == original.id


def test_issue_codes_helper_lists_the_codes(session):
    invoice = make_invoice(
        issues=[
            ValidationIssue(code="TOTAL_MISMATCH", field="total", message="x"),
            ValidationIssue(code="DUPLICATE_INVOICE", field="invoice_number", message="y"),
        ]
    )
    session.add(invoice)
    session.commit()

    assert invoice.issue_codes == ["TOTAL_MISMATCH", "DUPLICATE_INVOICE"]


# --------------------------------------------------------------------------- #
# Duplicate key encoding
# --------------------------------------------------------------------------- #


def test_dup_key_round_trips_through_storage():
    key = duplicate_key(
        ExtractedInvoice(vendor_name="  ACME SA ", invoice_number=" A-1 ")
    )

    assert decode_dup_key(encode_dup_key(key)) == key


def test_dup_key_encoding_tolerates_separators_in_the_values():
    """A hyphen or space in either half must not split the key."""
    key = ("north-wind logistics, llc", "nl-2026-1011")

    assert decode_dup_key(encode_dup_key(key)) == key


@pytest.mark.parametrize("value", [None, ""])
def test_missing_dup_keys_stay_missing(value):
    assert encode_dup_key(None) is None
    assert decode_dup_key(value) is None


def test_dup_key_is_queryable(session):
    """Looking an invoice up by its key is what known_keys will do."""
    key = duplicate_key(
        ExtractedInvoice(vendor_name="Acme SA", invoice_number="A-1")
    )
    session.add(make_invoice(dup_key=encode_dup_key(key)))
    session.commit()

    found = session.scalars(
        select(Invoice).where(Invoice.dup_key == encode_dup_key(key))
    ).one()

    assert decode_dup_key(found.dup_key) == key
