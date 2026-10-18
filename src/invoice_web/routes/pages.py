"""Full-page HTML routes."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..models import Invoice, InvoiceStatus, ReviewStatus
from . import get_session, get_templates

router = APIRouter()

#: Status values the list page offers as filters, in display order.
STATUS_FILTERS: tuple[str, ...] = (
    InvoiceStatus.PROCESSING,
    InvoiceStatus.VALID,
    InvoiceStatus.NEEDS_REVIEW,
    InvoiceStatus.FAILED,
)

REVIEW_FILTERS: tuple[str, ...] = (
    ReviewStatus.PENDING,
    ReviewStatus.APPROVED,
    ReviewStatus.REJECTED,
)


@dataclass(frozen=True)
class Counts:
    """Headline numbers shown above the list."""

    total: int = 0
    needs_review: int = 0
    processing: int = 0


@router.get("/", response_class=HTMLResponse)
def upload_page(request: Request, templates=Depends(get_templates)):
    """The drag-and-drop upload screen."""
    return templates.TemplateResponse(request, "upload.html", {})


@router.get("/invoices", response_class=HTMLResponse)
def list_page(
    request: Request,
    status: str | None = None,
    review: str | None = None,
    session: Session = Depends(get_session),
    templates=Depends(get_templates),
):
    """The invoice list, optionally filtered by status and review state."""
    invoices = session.scalars(
        _filtered(select(Invoice), status, review)
        .options(selectinload(Invoice.issues))
        .order_by(Invoice.uploaded_at.desc(), Invoice.id.desc())
    ).all()

    return templates.TemplateResponse(
        request,
        "list.html",
        {
            "invoices": invoices,
            "counts": load_counts(session),
            "status": status,
            "review": review,
            "status_filters": STATUS_FILTERS,
            "review_filters": REVIEW_FILTERS,
        },
    )


def load_counts(session: Session) -> Counts:
    """Totals for the header, in one pass over the table."""
    row = session.execute(
        select(
            func.count(Invoice.id),
            func.count(Invoice.id).filter(
                Invoice.status == InvoiceStatus.NEEDS_REVIEW
            ),
            func.count(Invoice.id).filter(Invoice.status == InvoiceStatus.PROCESSING),
        )
    ).one()
    return Counts(total=row[0], needs_review=row[1], processing=row[2])


def _filtered(statement, status: str | None, review: str | None):
    """Apply the list filters, ignoring values that are not real states."""
    if status in STATUS_FILTERS:
        statement = statement.where(Invoice.status == status)
    if review in REVIEW_FILTERS:
        statement = statement.where(Invoice.review_status == review)
    return statement
