"""Duplicate detection against the store, including under concurrency."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from invoice_web.models import Invoice, InvoiceStatus, ReviewStatus
from invoice_web.processing import load_known_keys, process_invoice
from invoice_web.storage import sha256_of, store_pdf

from .factories import FakeExtractor, invoice, pdf_bytes, result


def upload(client, *files):
    payload = [("files", (name, data, "application/pdf")) for name, data in files]
    return client.post("/upload", files=payload, follow_redirects=False)


def test_a_second_invoice_with_the_same_number_is_flagged(client, session):
    """Different PDFs, same vendor and number: the second points at the first."""
    upload(client, ("first.pdf", pdf_bytes("a")))
    upload(client, ("second.pdf", pdf_bytes("b")))

    first, second = session.scalars(select(Invoice).order_by(Invoice.id)).all()

    assert first.status == InvoiceStatus.VALID
    assert second.status == InvoiceStatus.NEEDS_REVIEW
    assert second.issue_codes == ["DUPLICATE_INVOICE"]
    assert second.issues[0].related_invoice_id == first.id


def test_a_different_number_is_not_a_duplicate(make_client, session):
    client = make_client(
        FakeExtractor(
            [result(), result(extracted=invoice(invoice_number="NL-2026-9999"))]
        )
    )

    upload(client, ("a.pdf", pdf_bytes("a")), ("b.pdf", pdf_bytes("b")))

    assert {row.status for row in session.scalars(select(Invoice)).all()} == {
        InvoiceStatus.VALID
    }


def test_matching_ignores_case_and_spacing(make_client, session):
    client = make_client(
        FakeExtractor(
            [
                result(),
                result(
                    extracted=invoice(
                        vendor_name="  northwind logistics llc ",
                        invoice_number="nl-2026-1011 ",
                    )
                ),
            ]
        )
    )

    upload(client, ("a.pdf", pdf_bytes("a")), ("b.pdf", pdf_bytes("b")))

    second = session.scalars(select(Invoice).order_by(Invoice.id)).all()[1]
    assert second.issue_codes == ["DUPLICATE_INVOICE"]


# --------------------------------------------------------------------------- #
# What counts as "already on record"
# --------------------------------------------------------------------------- #


def test_rejected_invoices_do_not_block_a_resubmission(client, session):
    """Rejecting is a decision not to keep it, so it stops being the original."""
    upload(client, ("first.pdf", pdf_bytes("a")))
    first = session.scalars(select(Invoice)).one()
    first.review_status = ReviewStatus.REJECTED
    session.commit()

    upload(client, ("second.pdf", pdf_bytes("b")))

    second = session.scalars(select(Invoice).order_by(Invoice.id)).all()[1]
    assert second.status == InvoiceStatus.VALID


def test_failed_extractions_are_not_treated_as_originals(session):
    """A failed row has no trustworthy vendor or number to match against."""
    session.add(
        Invoice(
            filename="x.pdf",
            sha256="f" * 64,
            stored_path="/x.pdf",
            status=InvoiceStatus.FAILED,
            dup_key="acme\x1fa-1",
        )
    )
    session.commit()

    assert load_known_keys(session) == {}


def test_the_earliest_invoice_wins_a_key(session):
    """A third copy should point at the original, not at the second copy."""
    for index in range(2):
        session.add(
            Invoice(
                filename=f"{index}.pdf",
                sha256=str(index) * 64,
                stored_path=f"/{index}.pdf",
                status=InvoiceStatus.VALID,
                dup_key="acme\x1fa-1",
            )
        )
    session.commit()
    first_id = session.scalars(select(Invoice.id).order_by(Invoice.id)).first()

    assert load_known_keys(session) == {("acme", "a-1"): str(first_id)}


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_two_workers_racing_on_one_key_flag_exactly_one(settings, session):
    """The advisory lock is what makes this deterministic.

    Both rows are inserted first, then processed in parallel, so without
    serialization each worker would read the keys before the other wrote and
    both would look unique.
    """
    ids = []
    for marker in ("a", "b"):
        data = pdf_bytes(marker)
        digest = sha256_of(data)
        path = store_pdf(data, digest, settings.storage_dir)
        row = Invoice(
            filename=f"{marker}.pdf",
            sha256=digest,
            stored_path=str(path),
            status=InvoiceStatus.PROCESSING,
        )
        session.add(row)
        session.flush()
        ids.append(row.id)
    session.commit()

    # Both workers are held in extraction until the other arrives, so they
    # enter the validate-and-persist section together. Without that, the
    # section is short enough that they simply never overlap and the test
    # would pass with no lock at all.
    barrier = threading.Barrier(2)

    def run(invoice_id: int) -> None:
        process_invoice(invoice_id, settings, FakeExtractor(barrier=barrier))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, ids))

    session.expire_all()
    rows = session.scalars(select(Invoice).order_by(Invoice.id)).all()
    flagged = [row for row in rows if "DUPLICATE_INVOICE" in row.issue_codes]

    assert len(flagged) == 1, [row.issue_codes for row in rows]
    assert flagged[0].issues[0].related_invoice_id in ids
    assert flagged[0].issues[0].related_invoice_id != flagged[0].id
