"""Uploading PDFs and the background processing that follows.

The extractor is always faked, so nothing here calls the API.
"""

from __future__ import annotations

from sqlalchemy import select

from invoice_web.models import Invoice, InvoiceStatus, ReviewStatus

from .factories import FakeExtractor, failing_extractor, invoice, pdf_bytes, result


def upload(client, *files):
    """Post one or more (name, bytes) pairs to the upload endpoint."""
    payload = [("files", (name, data, "application/pdf")) for name, data in files]
    return client.post("/upload", files=payload, follow_redirects=False)


def test_an_upload_is_stored_and_processed(client, session):
    response = upload(client, ("a.pdf", pdf_bytes("a")))

    assert response.status_code == 303
    stored = session.scalars(select(Invoice)).one()
    assert stored.filename == "a.pdf"
    assert stored.status == InvoiceStatus.VALID
    assert stored.review_status == ReviewStatus.PENDING


def test_key_fields_are_copied_out_of_the_payload(client, session):
    upload(client, ("a.pdf", pdf_bytes("a")))

    stored = session.scalars(select(Invoice)).one()

    assert stored.vendor_name == "Northwind Logistics LLC"
    assert stored.invoice_number == "NL-2026-1011"
    assert str(stored.issue_date) == "2026-03-21"
    assert float(stored.total) == 220.0
    assert stored.currency == "USD"


def test_cost_and_timing_are_recorded(client, session):
    upload(client, ("a.pdf", pdf_bytes("a")))

    stored = session.scalars(select(Invoice)).one()

    assert stored.tokens_in == 4000
    assert stored.tokens_out == 500
    assert stored.model == "claude-haiku-5-5"
    assert stored.processing_ms == 1234


def test_the_whole_extraction_is_kept(client, session):
    """The JSONB payload is the audit trail, so it keeps everything."""
    upload(client, ("a.pdf", pdf_bytes("a")))

    stored = session.scalars(select(Invoice)).one()

    assert stored.extraction["invoice"]["vendor_tax_id"] == "47-8821093"
    assert stored.extraction["usage"]["input_tokens"] == 4000
    assert stored.extraction["attempts"] == 1


def test_several_files_upload_at_once(client, session):
    upload(client, ("a.pdf", pdf_bytes("a")), ("b.pdf", pdf_bytes("b")))

    assert len(session.scalars(select(Invoice)).all()) == 2


def test_the_pdf_lands_in_storage(client, session, settings):
    upload(client, ("a.pdf", pdf_bytes("a")))

    stored = session.scalars(select(Invoice)).one()
    assert (settings.storage_dir / f"{stored.sha256}.pdf").read_bytes() == pdf_bytes("a")


# --------------------------------------------------------------------------- #
# Re-uploading the same bytes
# --------------------------------------------------------------------------- #


def test_the_same_pdf_is_not_extracted_twice(client, session):
    upload(client, ("a.pdf", pdf_bytes("a")))
    upload(client, ("a-again.pdf", pdf_bytes("a")))

    assert len(session.scalars(select(Invoice)).all()) == 1
    assert len(client.extractor.calls) == 1


def test_a_repeat_upload_is_reported_as_a_duplicate(client):
    upload(client, ("a.pdf", pdf_bytes("a")))

    response = upload(client, ("a.pdf", pdf_bytes("a")))

    assert "duplicates=1" in response.headers["location"]
    assert "queued=0" in response.headers["location"]


def test_a_repeat_upload_keeps_the_original_record(client, session):
    """The first record stands; re-uploading must not reset its review."""
    upload(client, ("a.pdf", pdf_bytes("a")))
    stored = session.scalars(select(Invoice)).one()
    stored.review_status = ReviewStatus.APPROVED
    session.commit()

    upload(client, ("a.pdf", pdf_bytes("a")))

    session.expire_all()
    assert session.scalars(select(Invoice)).one().review_status == ReviewStatus.APPROVED


# --------------------------------------------------------------------------- #
# Rejections and failures
# --------------------------------------------------------------------------- #


def test_a_non_pdf_is_rejected_before_it_costs_anything(client, session):
    response = upload(client, ("notes.txt", b"this is not a pdf"))

    assert "rejected=1" in response.headers["location"]
    assert session.scalars(select(Invoice)).all() == []
    assert client.extractor.calls == []


def test_a_failed_extraction_is_recorded_with_its_error(make_client, session):
    client = make_client(failing_extractor("APITimeoutError: timed out"))

    upload(client, ("a.pdf", pdf_bytes("a")))

    stored = session.scalars(select(Invoice)).one()
    assert stored.status == InvoiceStatus.FAILED
    assert stored.error == "APITimeoutError: timed out"
    assert stored.issues == []


def test_a_failed_extraction_still_records_what_it_cost(make_client, session):
    """A failed attempt was billed; the row has to say so."""
    client = make_client(failing_extractor())

    upload(client, ("a.pdf", pdf_bytes("a")))

    assert session.scalars(select(Invoice)).one().tokens_in == 4000


def test_one_bad_file_does_not_stop_the_others(make_client, session):
    client = make_client(
        FakeExtractor(
            [
                result(file="a.pdf"),
                result(file="b.pdf", invoice=None, error="boom"),
            ]
        )
    )

    upload(client, ("a.pdf", pdf_bytes("a")), ("b.pdf", pdf_bytes("b")))

    statuses = {
        row.filename: row.status for row in session.scalars(select(Invoice)).all()
    }
    assert statuses == {"a.pdf": InvoiceStatus.VALID, "b.pdf": InvoiceStatus.FAILED}


# --------------------------------------------------------------------------- #
# Validation findings
# --------------------------------------------------------------------------- #


def test_a_broken_invoice_needs_review_and_keeps_its_issues(make_client, session):
    broken = invoice(total=999.0)
    client = make_client(FakeExtractor(default=result(extracted=broken)))

    upload(client, ("a.pdf", pdf_bytes("a")))

    stored = session.scalars(select(Invoice)).one()
    assert stored.status == InvoiceStatus.NEEDS_REVIEW
    assert stored.issue_codes == ["TOTAL_MISMATCH"]
    assert "999.00" in stored.issues[0].message


def test_an_unparseable_date_leaves_the_column_null(make_client, session):
    client = make_client(
        FakeExtractor(default=result(extracted=invoice(issue_date="21/03/2026")))
    )

    upload(client, ("a.pdf", pdf_bytes("a")))

    stored = session.scalars(select(Invoice)).one()
    assert stored.issue_date is None
    assert "INVALID_DATE_FORMAT" in stored.issue_codes
