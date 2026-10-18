"""HTMX partials.

Each fragment is the same markup the full page renders, so a polled row and a
freshly loaded one can never drift apart.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import Invoice
from . import get_session, get_templates
from .pages import load_counts

router = APIRouter(prefix="/fragments")


@router.get("/invoices/{invoice_id}/row", response_class=HTMLResponse)
def invoice_row(
    invoice_id: int,
    request: Request,
    session: Session = Depends(get_session),
    templates=Depends(get_templates),
):
    """One table row.

    Rows still processing come back carrying their own polling attribute; once
    the status settles the attribute is gone and the row stops asking. The
    polling is therefore self-terminating, with no client-side bookkeeping.
    """
    invoice = session.scalar(
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .options(selectinload(Invoice.issues))
    )
    if invoice is None:
        return HTMLResponse("", status_code=404)

    return templates.TemplateResponse(
        request, "partials/row.html", {"invoice": invoice}
    )


@router.get("/counts", response_class=HTMLResponse)
def counts(
    request: Request,
    session: Session = Depends(get_session),
    templates=Depends(get_templates),
):
    """The headline counts, polled while anything is still processing."""
    return templates.TemplateResponse(
        request, "partials/counts.html", {"counts": load_counts(session)}
    )
