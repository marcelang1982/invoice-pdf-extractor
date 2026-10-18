"""The cap on simultaneous extractions."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from sqlalchemy import select

from invoice_web.config import WebSettings
from invoice_web.models import Invoice, InvoiceStatus
from invoice_web.processing import extraction_semaphore, process_invoice
from invoice_web.storage import sha256_of, store_pdf

from .factories import invoice, pdf_bytes, result


@dataclass
class CountingExtractor:
    """Records how many extractions overlap, and holds each one open briefly."""

    hold: float = 0.05
    _lock: threading.Lock = field(default_factory=threading.Lock)
    running: int = 0
    peak: int = 0
    completed: int = 0

    def extract_file(self, path):
        with self._lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
            number = f"INV-{self.completed + self.running:04d}"
        try:
            # Long enough that a missing cap would show as overlap.
            time.sleep(self.hold)
            # A distinct number per call, so these tests measure concurrency
            # rather than tripping over duplicate detection.
            return result(file=str(path), extracted=invoice(invoice_number=number))
        finally:
            with self._lock:
                self.running -= 1
                self.completed += 1


def queue_invoices(session, settings: WebSettings, count: int) -> list[int]:
    """Insert `count` invoices in `processing`, each with its own stored PDF."""
    ids = []
    for index in range(count):
        data = pdf_bytes(f"file-{index}")
        digest = sha256_of(data)
        path = store_pdf(data, digest, settings.storage_dir)
        row = Invoice(
            filename=f"{index}.pdf",
            sha256=digest,
            stored_path=str(path),
            status=InvoiceStatus.PROCESSING,
        )
        session.add(row)
        session.flush()
        ids.append(row.id)
    session.commit()
    return ids


def run_all(ids: list[int], settings: WebSettings, extractor) -> None:
    """Process every invoice at once, with more workers than the limit allows."""
    with ThreadPoolExecutor(max_workers=len(ids)) as pool:
        list(pool.map(lambda i: process_invoice(i, settings, extractor), ids))


def test_no_more_than_the_limit_extract_at_once(settings, session):
    settings = WebSettings(
        database_url=settings.database_url,
        storage_dir=settings.storage_dir,
        sample_dirs=(),
        extraction_concurrency=3,
    )
    extractor = CountingExtractor()
    ids = queue_invoices(session, settings, 9)

    run_all(ids, settings, extractor)

    assert extractor.peak <= 3, f"{extractor.peak} extractions overlapped"
    assert extractor.completed == 9


def test_everything_queued_still_gets_processed(settings, session):
    """The cap delays work; it must never drop any."""
    settings = WebSettings(
        database_url=settings.database_url,
        storage_dir=settings.storage_dir,
        sample_dirs=(),
        extraction_concurrency=2,
    )
    ids = queue_invoices(session, settings, 6)

    run_all(ids, settings, CountingExtractor())

    session.expire_all()
    statuses = [row.status for row in session.scalars(select(Invoice)).all()]
    assert statuses == [InvoiceStatus.VALID] * 6


def test_a_limit_of_one_serializes_extraction(settings, session):
    settings = WebSettings(
        database_url=settings.database_url,
        storage_dir=settings.storage_dir,
        sample_dirs=(),
        extraction_concurrency=1,
    )
    extractor = CountingExtractor()
    ids = queue_invoices(session, settings, 4)

    run_all(ids, settings, extractor)

    assert extractor.peak == 1


def test_the_semaphore_is_shared_across_workers():
    """Cached by limit, so every task in the process waits on the same one."""
    assert extraction_semaphore(4) is extraction_semaphore(4)
    assert extraction_semaphore(4) is not extraction_semaphore(5)


def test_a_nonsensical_limit_still_allows_progress():
    """A misconfigured zero must not deadlock every upload."""
    assert extraction_semaphore(0)._value >= 1
