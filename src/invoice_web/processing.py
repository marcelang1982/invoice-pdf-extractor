"""Background processing: extract, validate against the store, persist."""

from __future__ import annotations

import logging
import threading
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from invoice_extractor import InvoiceExtractor, duplicate_key, validate_batch
from invoice_extractor.models import Invoice as ExtractedInvoice

from .config import WebSettings
from .db import session_scope
from .models import Invoice, InvoiceStatus, ReviewStatus, ValidationIssue
from .models import decode_dup_key, encode_dup_key

logger = logging.getLogger(__name__)

#: Key for the advisory lock serializing validate-and-persist. One global lock
#: is enough at this scale: the critical section is a couple of small queries,
#: while the expensive part (the API call) stays outside it.
DUPLICATE_LOCK_KEY = 0x1E701CE


@lru_cache(maxsize=8)
def extraction_semaphore(limit: int) -> threading.Semaphore:
    """The process-wide cap on simultaneous extractions.

    Cached by limit so every worker in the process shares one semaphore.
    Uploading a folder of fifty PDFs should not open fifty connections to the
    API at once; the rest queue up and stay visibly `processing` until a slot
    frees.
    """
    return threading.Semaphore(max(1, limit))


def process_invoice(
    invoice_id: int,
    settings: WebSettings,
    extractor: InvoiceExtractor | None = None,
) -> None:
    """Extract one stored PDF and record the result.

    Runs as a background task, so it never raises into a request: every failure
    ends as `status=failed` with the reason on the row.

    The extraction happens outside any transaction because it is slow and
    external. Reading the known duplicate keys, validating against them and
    writing the result happen inside one transaction holding an advisory lock,
    so two invoices with the same vendor and number cannot both look unique.
    """
    extractor = extractor or InvoiceExtractor()

    try:
        with session_scope(settings) as session:
            invoice = session.get(Invoice, invoice_id)
            if invoice is None:
                logger.warning("invoice %s vanished before processing", invoice_id)
                return
            stored_path = Path(invoice.stored_path)
            filename = invoice.filename

        # Only the API call is capped. The database work around it is fast
        # and holding the slot across it would shrink the effective limit.
        with extraction_semaphore(settings.extraction_concurrency):
            result = extractor.extract_file(stored_path)

        with session_scope(settings) as session:
            # Held until this transaction ends, which is what serializes the
            # read-validate-write below against other workers.
            session.execute(select(func.pg_advisory_xact_lock(DUPLICATE_LOCK_KEY)))

            invoice = session.get(Invoice, invoice_id)
            if invoice is None:
                return

            invoice.model = result.model
            invoice.tokens_in = result.usage.input_tokens
            invoice.tokens_out = result.usage.output_tokens
            invoice.processing_ms = result.duration_ms
            invoice.extraction = result.model_dump(mode="json")

            if result.invoice is None:
                invoice.status = InvoiceStatus.FAILED
                invoice.error = result.error or "Extraction failed."
                return

            _apply_extraction(invoice, result.invoice)
            known = load_known_keys(session, exclude_id=invoice_id)
            validation = validate_batch({filename: result.invoice}, known)[filename]

            invoice.status = (
                InvoiceStatus.VALID
                if validation.status == "valid"
                else InvoiceStatus.NEEDS_REVIEW
            )
            invoice.error = None
            invoice.issues = [
                ValidationIssue(
                    code=issue.code.value,
                    field=issue.field,
                    message=issue.message,
                    related_invoice_id=_as_int(issue.related_id),
                )
                for issue in validation.issues
            ]

    except Exception as exc:  # noqa: BLE001 - a background task must not escape
        logger.exception("processing invoice %s failed", invoice_id)
        _mark_failed(settings, invoice_id, f"{type(exc).__name__}: {exc}")


def load_known_keys(
    session: Session, exclude_id: int | None = None
) -> dict[tuple[str, str], str]:
    """Duplicate keys already on record, mapped to the invoice holding them.

    Rejected invoices and failed extractions are excluded: a rejected invoice
    is one a human already decided not to keep, and a failed extraction has no
    trustworthy vendor or number to match on. Ordering by id means the earliest
    invoice wins a key, so a duplicate points at the original rather than at
    whichever row the database happened to return first.
    """
    statement = (
        select(Invoice.id, Invoice.dup_key)
        .where(
            Invoice.dup_key.is_not(None),
            Invoice.review_status != ReviewStatus.REJECTED,
            Invoice.status != InvoiceStatus.FAILED,
        )
        .order_by(Invoice.id)
    )
    if exclude_id is not None:
        statement = statement.where(Invoice.id != exclude_id)

    known: dict[tuple[str, str], str] = {}
    for stored_id, stored_key in session.execute(statement):
        key = decode_dup_key(stored_key)
        if key is not None:
            known.setdefault(key, str(stored_id))
    return known


def recover_interrupted(settings: WebSettings) -> int:
    """Fail anything left mid-processing by a restart.

    Background tasks die with the process, so a row still marked `processing`
    at startup is never going to finish on its own. Leaving it would poll
    forever in the browser.
    """
    with session_scope(settings) as session:
        result = session.execute(
            update(Invoice)
            .where(Invoice.status == InvoiceStatus.PROCESSING)
            .values(status=InvoiceStatus.FAILED, error="interrupted by restart")
        )
        return result.rowcount or 0


def _apply_extraction(invoice: Invoice, extracted: ExtractedInvoice) -> None:
    """Copy the key fields out of the payload into their own columns."""
    invoice.vendor_name = extracted.vendor_name
    invoice.invoice_number = extracted.invoice_number
    invoice.issue_date = _as_date(extracted.issue_date)
    invoice.total = _as_decimal(extracted.total)
    invoice.currency = extracted.currency
    invoice.dup_key = encode_dup_key(duplicate_key(extracted))


def _as_date(value: str | None) -> date | None:
    """Parse an ISO date, or `None`. An unparseable date is a finding, not a crash."""
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        return None


def _as_decimal(value: Any) -> Decimal | None:
    """Convert to Decimal via str, so 275.00 does not become 274.99999..."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _mark_failed(settings: WebSettings, invoice_id: int, error: str) -> None:
    """Last-resort failure write, used when processing itself blew up."""
    try:
        with session_scope(settings) as session:
            invoice = session.get(Invoice, invoice_id)
            if invoice is not None:
                invoice.status = InvoiceStatus.FAILED
                invoice.error = error
    except Exception:  # noqa: BLE001 - nothing left to do but log
        logger.exception("could not mark invoice %s as failed", invoice_id)
