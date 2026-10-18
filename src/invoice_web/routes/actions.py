"""Mutations: uploading PDFs and queueing them for extraction."""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import WebSettings
from ..models import Invoice, InvoiceStatus, ReviewStatus
from ..processing import process_invoice
from ..storage import looks_like_pdf, sha256_of, store_pdf
from . import get_session, get_settings
from .pages import _filtered

logger = logging.getLogger(__name__)

router = APIRouter()


@dataclass
class UploadOutcome:
    """What happened to one uploaded file."""

    queued: int = 0
    duplicates: int = 0
    rejected: int = 0


@router.post("/upload")
async def upload(
    request: Request,
    background: BackgroundTasks,
    files: list[UploadFile],
    session: Session = Depends(get_session),
    settings: WebSettings = Depends(get_settings),
) -> RedirectResponse:
    """Store each PDF and queue it for extraction.

    A file whose bytes are already on record is not re-extracted: the existing
    invoice stands, and the upload is reported as a duplicate. That makes
    re-uploading a folder safe and free.
    """
    outcome = UploadOutcome()

    for upload_file in files:
        data = await upload_file.read()
        if not data or not looks_like_pdf(data):
            outcome.rejected += 1
            logger.info("rejected non-PDF upload %r", upload_file.filename)
            continue

        digest = sha256_of(data)
        existing = session.scalar(select(Invoice).where(Invoice.sha256 == digest))
        if existing is not None:
            outcome.duplicates += 1
            continue

        stored_path = store_pdf(data, digest, settings.storage_dir)
        invoice = Invoice(
            filename=upload_file.filename or f"{digest}.pdf",
            sha256=digest,
            stored_path=str(stored_path),
            status=InvoiceStatus.PROCESSING,
        )
        session.add(invoice)
        # Needed before the task is queued: the id is how it finds the row.
        session.flush()
        _queue(request, background, invoice.id, settings)
        outcome.queued += 1

    session.commit()

    return RedirectResponse(
        f"/invoices?queued={outcome.queued}"
        f"&duplicates={outcome.duplicates}&rejected={outcome.rejected}",
        status_code=303,
    )


@router.post("/samples/load")
def load_samples(
    request: Request,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
    settings: WebSettings = Depends(get_settings),
) -> RedirectResponse:
    """Queue every PDF in the configured sample directories."""
    outcome = UploadOutcome()

    for directory in settings.sample_dirs:
        if not Path(directory).is_dir():
            logger.warning("sample directory %s does not exist", directory)
            continue

        for pdf in sorted(Path(directory).glob("*.pdf")):
            data = pdf.read_bytes()
            digest = sha256_of(data)
            if session.scalar(select(Invoice).where(Invoice.sha256 == digest)):
                outcome.duplicates += 1
                continue

            stored_path = store_pdf(data, digest, settings.storage_dir)
            invoice = Invoice(
                filename=pdf.name,
                sha256=digest,
                stored_path=str(stored_path),
                status=InvoiceStatus.PROCESSING,
            )
            session.add(invoice)
            session.flush()
            _queue(request, background, invoice.id, settings)
            outcome.queued += 1

    session.commit()

    return RedirectResponse(
        f"/invoices?queued={outcome.queued}&duplicates={outcome.duplicates}",
        status_code=303,
    )


def _queue(
    request: Request,
    background: BackgroundTasks,
    invoice_id: int,
    settings: WebSettings,
) -> None:
    """Schedule extraction, building the extractor from the app's factory."""
    background.add_task(
        process_invoice,
        invoice_id,
        settings,
        request.app.state.extractor_factory(),
    )


#: Columns of the CSV export, in order. One row per invoice.
EXPORT_COLUMNS: tuple[str, ...] = (
    "id",
    "filename",
    "status",
    "review_status",
    "review_note",
    "vendor_name",
    "invoice_number",
    "issue_date",
    "currency",
    "total",
    "issue_codes",
    "model",
    "tokens_in",
    "tokens_out",
    "uploaded_at",
    "reviewed_at",
)


@router.post("/invoices/{invoice_id}/review")
def review(
    invoice_id: int,
    decision: str = Form(...),
    note: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Record a human decision.

    Only `review_status` moves. The machine's verdict stays as it was: a person
    approving an invoice does not make its arithmetic add up, and a reviewer
    needs to keep seeing why it was flagged.
    """
    if decision not in (ReviewStatus.APPROVED, ReviewStatus.REJECTED):
        raise HTTPException(status_code=400, detail="Unknown review decision.")

    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="No such invoice.")

    invoice.review_status = decision
    invoice.review_note = note.strip() or None
    invoice.reviewed_at = datetime.now(timezone.utc)
    session.commit()

    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@router.get("/invoices/export.csv")
def export_csv(
    status: str | None = None,
    review: str | None = None,
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """Export the current filter as CSV, one row per invoice.

    The same filters the list page applies, so what is exported is what is on
    screen rather than whatever happens to be in the table.
    """
    invoices = session.scalars(
        _filtered(select(Invoice), status, review)
        .options(selectinload(Invoice.issues))
        .order_by(Invoice.uploaded_at.desc(), Invoice.id.desc())
    ).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPORT_COLUMNS)
    for invoice in invoices:
        writer.writerow(
            [
                invoice.id,
                invoice.filename,
                invoice.status,
                invoice.review_status,
                invoice.review_note or "",
                invoice.vendor_name or "",
                invoice.invoice_number or "",
                invoice.issue_date.isoformat() if invoice.issue_date else "",
                invoice.currency or "",
                f"{invoice.total:.2f}" if invoice.total is not None else "",
                " ".join(invoice.issue_codes),
                invoice.model or "",
                invoice.tokens_in,
                invoice.tokens_out,
                invoice.uploaded_at.isoformat() if invoice.uploaded_at else "",
                invoice.reviewed_at.isoformat() if invoice.reviewed_at else "",
            ]
        )

    # Excel assumes the host's legacy encoding unless a UTF-8 BOM says
    # otherwise, which turns "Gráficos" into "GrÃ¡ficos". Everything else
    # ignores the BOM, so it costs nothing to include.
    body = "\ufeff" + buffer.getvalue()
    return StreamingResponse(
        iter([body]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="invoices.csv"'},
    )
