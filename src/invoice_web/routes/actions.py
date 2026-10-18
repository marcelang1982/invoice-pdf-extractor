"""Mutations: uploading PDFs and queueing them for extraction."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import WebSettings
from ..models import Invoice, InvoiceStatus
from ..processing import process_invoice
from ..storage import looks_like_pdf, sha256_of, store_pdf
from . import get_session, get_settings

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
