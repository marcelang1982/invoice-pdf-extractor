"""The list page, its filters, and the HTMX fragments that keep it live."""

from __future__ import annotations

from sqlalchemy import select

from invoice_web.models import Invoice, InvoiceStatus, ReviewStatus

from .factories import FakeExtractor, invoice, pdf_bytes, result


def upload(client, *files):
    payload = [("files", (name, data, "application/pdf")) for name, data in files]
    return client.post("/upload", files=payload, follow_redirects=False)


def add_row(session, **overrides) -> Invoice:
    """Insert a row directly, for states processing cannot easily produce."""
    defaults = {
        "filename": "direct.pdf",
        "sha256": "d" * 64,
        "stored_path": "/direct.pdf",
        "status": InvoiceStatus.PROCESSING,
    }
    row = Invoice(**{**defaults, **overrides})
    session.add(row)
    session.commit()
    return row


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def test_the_upload_page_renders(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "Drop PDFs here" in response.text


def test_an_empty_list_says_so(client):
    response = client.get("/invoices")

    assert response.status_code == 200
    assert "No invoices match this filter" in response.text


def test_uploaded_invoices_appear_with_their_details(client):
    upload(client, ("a.pdf", pdf_bytes("a")))

    body = client.get("/invoices").text

    assert "a.pdf" in body
    assert "Northwind Logistics LLC" in body
    assert "NL-2026-1011" in body
    assert "220.00 USD" in body


def test_statuses_render_as_badges(make_client):
    client = make_client(FakeExtractor(default=result(extracted=invoice(total=999.0))))
    upload(client, ("a.pdf", pdf_bytes("a")))

    body = client.get("/invoices").text

    assert 'class="badge badge-needs-review"' in body
    assert 'class="badge badge-pending"' in body


def test_issue_codes_are_listed_on_the_row(make_client):
    client = make_client(FakeExtractor(default=result(extracted=invoice(total=999.0))))
    upload(client, ("a.pdf", pdf_bytes("a")))

    assert "TOTAL_MISMATCH" in client.get("/invoices").text


def test_a_failed_invoice_shows_its_error(client, session):
    add_row(session, status=InvoiceStatus.FAILED, error="APITimeoutError: timed out")

    assert "APITimeoutError: timed out" in client.get("/invoices").text


# --------------------------------------------------------------------------- #
# Counts and filters
# --------------------------------------------------------------------------- #


def test_counts_summarize_the_table(make_client, session):
    client = make_client(FakeExtractor([result(), result(extracted=invoice(total=999.0))]))
    upload(client, ("a.pdf", pdf_bytes("a")), ("b.pdf", pdf_bytes("b")))

    body = client.get("/invoices").text

    assert "<strong>2</strong> invoices" in body
    assert "<strong>1</strong> need review" in body


def test_filtering_by_status_narrows_the_table(make_client):
    client = make_client(FakeExtractor([result(), result(extracted=invoice(total=999.0))]))
    upload(client, ("ok.pdf", pdf_bytes("a")), ("bad.pdf", pdf_bytes("b")))

    body = client.get("/invoices?status=needs_review").text

    assert "bad.pdf" in body
    assert "ok.pdf" not in body


def test_filtering_by_review_state_narrows_the_table(client, session):
    upload(client, ("a.pdf", pdf_bytes("a")))
    stored = session.scalars(select(Invoice)).one()
    stored.review_status = ReviewStatus.APPROVED
    session.commit()
    add_row(session, filename="pending.pdf", sha256="e" * 64)

    body = client.get("/invoices?review=approved").text

    assert "a.pdf" in body
    assert "pending.pdf" not in body


def test_an_unknown_filter_value_is_ignored(client):
    """A hand-edited query string must not silently hide everything."""
    upload(client, ("a.pdf", pdf_bytes("a")))

    assert "a.pdf" in client.get("/invoices?status=nonsense").text


def test_filters_combine(make_client, session):
    client = make_client(FakeExtractor(default=result(extracted=invoice(total=999.0))))
    upload(client, ("a.pdf", pdf_bytes("a")))

    body = client.get("/invoices?status=needs_review&review=pending").text

    assert "a.pdf" in body


# --------------------------------------------------------------------------- #
# HTMX fragments
# --------------------------------------------------------------------------- #


def test_a_processing_row_asks_to_be_polled(client, session):
    row = add_row(session, status=InvoiceStatus.PROCESSING)

    body = client.get(f"/fragments/invoices/{row.id}/row").text

    assert 'hx-trigger="every 2s"' in body
    assert f"/fragments/invoices/{row.id}/row" in body


def test_a_settled_row_stops_polling(client):
    """The fragment drops its own trigger, so polling ends with no client state."""
    upload(client, ("a.pdf", pdf_bytes("a")))
    row_id = client.get("/invoices").text.count("a.pdf") and 1

    body = client.get(f"/fragments/invoices/{row_id}/row").text

    assert "hx-trigger" not in body
    assert "badge-valid" in body


def test_polling_a_missing_row_is_a_404(client):
    assert client.get("/fragments/invoices/9999/row").status_code == 404


def test_the_counts_fragment_polls_only_while_work_is_pending(client, session):
    add_row(session, status=InvoiceStatus.PROCESSING)
    assert 'hx-trigger="every 2s"' in client.get("/fragments/counts").text

    session.query(Invoice).update({Invoice.status: InvoiceStatus.VALID})
    session.commit()

    assert "hx-trigger" not in client.get("/fragments/counts").text


def test_the_list_page_and_the_fragment_render_the_same_row(client):
    """Both include partials/row.html, so a polled row cannot drift."""
    upload(client, ("a.pdf", pdf_bytes("a")))

    fragment = client.get("/fragments/invoices/1/row").text

    assert fragment.strip() in client.get("/invoices").text


# --------------------------------------------------------------------------- #
# Restart recovery
# --------------------------------------------------------------------------- #


def test_rows_left_processing_are_failed_on_startup(make_client, session, settings):
    """Background tasks die with the process; those rows would poll forever."""
    add_row(session, status=InvoiceStatus.PROCESSING)

    from fastapi.testclient import TestClient

    from invoice_web.main import create_app

    with TestClient(create_app(settings=settings, extractor_factory=FakeExtractor)):
        pass

    session.expire_all()
    stored = session.scalars(select(Invoice)).one()
    assert stored.status == InvoiceStatus.FAILED
    assert stored.error == "interrupted by restart"
