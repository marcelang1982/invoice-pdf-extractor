"""The detail page, issue placement, review decisions and CSV export."""

from __future__ import annotations

import csv
import io

from sqlalchemy import select

from invoice_web.models import Invoice, InvoiceStatus, ReviewStatus
from invoice_web.routes.detail import place_issues

from .factories import FakeExtractor, failing_extractor, invoice, pdf_bytes, result


def upload(client, *files):
    payload = [("files", (name, data, "application/pdf")) for name, data in files]
    return client.post("/upload", files=payload, follow_redirects=False)


# --------------------------------------------------------------------------- #
# Issue placement
# --------------------------------------------------------------------------- #


def test_header_issues_land_on_their_field(client, session):
    upload(client, ("a.pdf", pdf_bytes("a")))
    stored = session.scalars(select(Invoice)).one()
    stored.issues[:] = []
    session.commit()

    from invoice_web.models import ValidationIssue

    stored.issues = [
        ValidationIssue(code="TOTAL_MISMATCH", field="total", message="off"),
        ValidationIssue(
            code="MISSING_REQUIRED_FIELD", field="vendor_tax_id", message="gone"
        ),
    ]
    session.commit()

    placed = place_issues(stored)

    assert set(placed.header) == {"total", "vendor_tax_id"}
    assert placed.general == []


def test_scoped_issues_land_on_their_row(session):
    from invoice_web.models import ValidationIssue

    row = Invoice(
        filename="a.pdf", sha256="a" * 64, stored_path="/a.pdf",
        status=InvoiceStatus.NEEDS_REVIEW,
        issues=[
            ValidationIssue(
                code="LINE_ITEM_AMOUNT_MISMATCH",
                field="line_items[2].amount",
                message="bad",
            ),
            ValidationIssue(
                code="LINE_ITEM_AMOUNT_MISMATCH",
                field="adjustments[0].amount",
                message="bad",
            ),
        ],
    )
    session.add(row)
    session.commit()

    placed = place_issues(row)

    assert list(placed.line_items) == [2]
    assert list(placed.adjustments) == [0]


def test_an_unanchorable_issue_is_still_shown(session):
    """Better in the general block than silently dropped."""
    from invoice_web.models import ValidationIssue

    row = Invoice(
        filename="a.pdf", sha256="a" * 64, stored_path="/a.pdf",
        status=InvoiceStatus.FAILED,
        issues=[ValidationIssue(code="EXTRACTION_FAILED", field="", message="boom")],
    )
    session.add(row)
    session.commit()

    placed = place_issues(row)

    assert [item.code for item in placed.general] == ["EXTRACTION_FAILED"]
    assert [item.code for item in placed.all()] == ["EXTRACTION_FAILED"]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def test_the_detail_page_shows_the_extracted_data(client):
    upload(client, ("a.pdf", pdf_bytes("a")))

    body = client.get("/invoices/1").text

    assert "Northwind Logistics LLC" in body
    assert "47-8821093" in body
    assert "Freight forwarding" in body
    assert 'src="/invoices/1/file"' in body


def test_a_flagged_field_is_highlighted(make_client):
    client = make_client(FakeExtractor(default=result(extracted=invoice(total=999.0))))
    upload(client, ("a.pdf", pdf_bytes("a")))

    body = client.get("/invoices/1").text

    assert 'class="flagged"' in body
    assert "TOTAL_MISMATCH" in body


def test_a_duplicate_links_to_the_original(client):
    upload(client, ("first.pdf", pdf_bytes("a")))
    upload(client, ("second.pdf", pdf_bytes("b")))

    body = client.get("/invoices/2").text

    assert 'href="/invoices/1"' in body
    assert "View invoice #1" in body


def test_a_failed_invoice_shows_the_error(make_client):
    client = make_client(failing_extractor("APITimeoutError: timed out"))
    upload(client, ("a.pdf", pdf_bytes("a")))

    body = client.get("/invoices/1").text

    assert "EXTRACTION FAILED" in body
    assert "APITimeoutError: timed out" in body


def test_a_missing_invoice_is_a_404(client):
    assert client.get("/invoices/999").status_code == 404


# --------------------------------------------------------------------------- #
# Serving the PDF
# --------------------------------------------------------------------------- #


def test_the_stored_pdf_is_served_inline(client):
    upload(client, ("a.pdf", pdf_bytes("a")))

    response = client.get("/invoices/1/file")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "inline" in response.headers["content-disposition"]
    assert response.content == pdf_bytes("a")


def test_a_vanished_pdf_is_a_404(client, session):
    upload(client, ("a.pdf", pdf_bytes("a")))
    stored = session.scalars(select(Invoice)).one()
    stored.stored_path = "/nowhere/missing.pdf"
    session.commit()

    assert client.get("/invoices/1/file").status_code == 404


# --------------------------------------------------------------------------- #
# Review
# --------------------------------------------------------------------------- #


def test_approving_records_the_decision(client, session):
    upload(client, ("a.pdf", pdf_bytes("a")))

    response = client.post(
        "/invoices/1/review",
        data={"decision": "approved", "note": "Checked against the PO."},
        follow_redirects=False,
    )

    assert response.status_code == 303
    session.expire_all()
    stored = session.scalars(select(Invoice)).one()
    assert stored.review_status == ReviewStatus.APPROVED
    assert stored.review_note == "Checked against the PO."
    assert stored.reviewed_at is not None


def test_rejecting_records_the_decision(client, session):
    upload(client, ("a.pdf", pdf_bytes("a")))

    client.post("/invoices/1/review", data={"decision": "rejected"},
                follow_redirects=False)

    session.expire_all()
    assert session.scalars(select(Invoice)).one().review_status == ReviewStatus.REJECTED


def test_a_note_is_optional(client, session):
    upload(client, ("a.pdf", pdf_bytes("a")))

    client.post("/invoices/1/review", data={"decision": "approved", "note": "   "},
                follow_redirects=False)

    session.expire_all()
    assert session.scalars(select(Invoice)).one().review_note is None


def test_reviewing_does_not_touch_the_machine_verdict(make_client, session):
    """Approving a flagged invoice must not hide why it was flagged."""
    client = make_client(FakeExtractor(default=result(extracted=invoice(total=999.0))))
    upload(client, ("a.pdf", pdf_bytes("a")))

    client.post("/invoices/1/review", data={"decision": "approved"},
                follow_redirects=False)

    session.expire_all()
    stored = session.scalars(select(Invoice)).one()
    assert stored.status == InvoiceStatus.NEEDS_REVIEW
    assert stored.issue_codes == ["TOTAL_MISMATCH"]


def test_an_unknown_decision_is_rejected(client):
    upload(client, ("a.pdf", pdf_bytes("a")))

    response = client.post("/invoices/1/review", data={"decision": "maybe"},
                           follow_redirects=False)

    assert response.status_code == 400


def test_reviewing_a_missing_invoice_is_a_404(client):
    assert client.post(
        "/invoices/999/review", data={"decision": "approved"}, follow_redirects=False
    ).status_code == 404


# --------------------------------------------------------------------------- #
# CSV export
# --------------------------------------------------------------------------- #


def read_csv(response) -> list[dict]:
    """Decode as utf-8-sig so the BOM does not end up in the first column name."""
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))


def test_the_export_has_one_row_per_invoice(client):
    upload(client, ("a.pdf", pdf_bytes("a")), ("b.pdf", pdf_bytes("b")))

    rows = read_csv(client.get("/invoices/export.csv"))

    assert len(rows) == 2
    assert {row["filename"] for row in rows} == {"a.pdf", "b.pdf"}


def test_the_export_carries_status_review_and_issue_codes(make_client):
    client = make_client(FakeExtractor(default=result(extracted=invoice(total=999.0))))
    upload(client, ("a.pdf", pdf_bytes("a")))
    client.post("/invoices/1/review",
                data={"decision": "approved", "note": "Vendor confirmed."},
                follow_redirects=False)

    row = read_csv(client.get("/invoices/export.csv"))[0]

    assert row["status"] == "needs_review"
    assert row["review_status"] == "approved"
    assert row["review_note"] == "Vendor confirmed."
    assert row["issue_codes"] == "TOTAL_MISMATCH"


def test_the_export_is_a_download(client):
    upload(client, ("a.pdf", pdf_bytes("a")))

    response = client.get("/invoices/export.csv")

    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]


def test_the_export_respects_the_filter(make_client):
    client = make_client(FakeExtractor([result(), result(extracted=invoice(total=999.0))]))
    upload(client, ("ok.pdf", pdf_bytes("a")), ("bad.pdf", pdf_bytes("b")))

    rows = read_csv(client.get("/invoices/export.csv?status=needs_review"))

    assert [row["filename"] for row in rows] == ["bad.pdf"]


def test_the_export_has_a_header_even_when_empty(client):
    # Decoded as utf-8-sig, the way a correct reader handles the BOM.
    body = client.get("/invoices/export.csv").content.decode("utf-8-sig")
    rows = body.splitlines()

    assert rows[0].startswith("id,filename,status,review_status,review_note")
    assert len(rows) == 1


def test_multiple_issue_codes_are_space_separated(make_client):
    broken = invoice(total=999.0, vendor_tax_id=None)
    client = make_client(FakeExtractor(default=result(extracted=broken)))
    upload(client, ("a.pdf", pdf_bytes("a")))

    codes = read_csv(client.get("/invoices/export.csv"))[0]["issue_codes"].split()

    assert set(codes) == {"MISSING_REQUIRED_FIELD", "TOTAL_MISMATCH"}


def test_the_export_is_utf8_with_a_bom(client):
    """Without the BOM, Excel reads UTF-8 as its legacy encoding and mangles
    every accent: "Gráficos" arrives as "GrÃ¡ficos"."""
    upload(client, ("a.pdf", pdf_bytes("a")))

    raw = client.get("/invoices/export.csv").content

    assert raw.startswith(b"\xef\xbb\xbf")
    assert "charset=utf-8" in client.get("/invoices/export.csv").headers["content-type"]


def test_accented_values_survive_the_round_trip(make_client):
    accented = invoice(vendor_name="Talleres Gráficos Pampa S.R.L.")
    client = make_client(FakeExtractor(default=result(extracted=accented)))
    upload(client, ("a.pdf", pdf_bytes("a")))

    body = client.get("/invoices/export.csv").content.decode("utf-8-sig")
    row = list(csv.DictReader(io.StringIO(body)))[0]

    assert row["vendor_name"] == "Talleres Gráficos Pampa S.R.L."
