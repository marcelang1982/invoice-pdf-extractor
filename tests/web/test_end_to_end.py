"""The whole journey in one test: upload, process, list, review, export.

Every step goes through HTTP exactly as a browser would, against the real
database. Only the extractor is faked, so no API call is made.
"""

from __future__ import annotations

import csv
import io

from sqlalchemy import select

from invoice_web.models import Invoice, InvoiceStatus, ReviewStatus

from .factories import FakeExtractor, invoice, pdf_bytes, result


def test_upload_process_list_approve_export(make_client, session):
    client = make_client(
        FakeExtractor(
            [
                result(extracted=invoice(invoice_number="NL-2026-0001")),
                result(extracted=invoice(invoice_number="NL-2026-0002", total=999.0)),
            ]
        )
    )

    # 1. Upload two PDFs. Background processing completes before the response.
    response = client.post(
        "/upload",
        files=[
            ("files", ("clean.pdf", pdf_bytes("a"), "application/pdf")),
            ("files", ("broken.pdf", pdf_bytes("b"), "application/pdf")),
        ],
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "queued=2" in response.headers["location"]

    # 2. Both were extracted and judged.
    stored = session.scalars(select(Invoice).order_by(Invoice.id)).all()
    assert [row.status for row in stored] == [
        InvoiceStatus.VALID,
        InvoiceStatus.NEEDS_REVIEW,
    ]

    # 3. The list shows both, with the counts and the finding.
    listing = client.get("/invoices").text
    assert "clean.pdf" in listing
    assert "broken.pdf" in listing
    assert "<strong>2</strong> invoices" in listing
    assert "<strong>1</strong> need review" in listing
    assert "TOTAL_MISMATCH" in listing

    # 4. Filtering narrows it to the one that needs attention.
    flagged = client.get("/invoices?status=needs_review").text
    assert "broken.pdf" in flagged
    assert "clean.pdf" not in flagged

    # 5. The detail page shows the PDF, the data and the finding together.
    detail = client.get(f"/invoices/{stored[1].id}").text
    assert f'src="/invoices/{stored[1].id}/file#pagemode=none' in detail
    assert "NL-2026-0002" in detail
    assert "TOTAL_MISMATCH" in detail
    assert client.get(f"/invoices/{stored[1].id}/file").content == pdf_bytes("b")

    # 6. A human approves it anyway, with a note.
    approved = client.post(
        f"/invoices/{stored[1].id}/review",
        data={"decision": "approved", "note": "Vendor confirmed the total by email."},
        follow_redirects=False,
    )
    assert approved.status_code == 303

    session.expire_all()
    reviewed = session.get(Invoice, stored[1].id)
    assert reviewed.review_status == ReviewStatus.APPROVED
    # The machine's verdict is untouched: the invoice still does not add up.
    assert reviewed.status == InvoiceStatus.NEEDS_REVIEW

    # 7. The export carries both verdicts and the note.
    rows = list(csv.DictReader(io.StringIO(client.get("/invoices/export.csv").text)))
    by_file = {row["filename"]: row for row in rows}

    assert len(rows) == 2
    assert by_file["clean.pdf"]["status"] == "valid"
    assert by_file["clean.pdf"]["review_status"] == "pending"
    assert by_file["broken.pdf"]["status"] == "needs_review"
    assert by_file["broken.pdf"]["review_status"] == "approved"
    assert by_file["broken.pdf"]["review_note"] == "Vendor confirmed the total by email."
    assert by_file["broken.pdf"]["issue_codes"] == "TOTAL_MISMATCH"
    assert by_file["broken.pdf"]["total"] == "999.00"
