"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from invoice_extractor import InvoiceExtractor

from .config import WebSettings
from .processing import recover_interrupted
from .routes import actions, fragments, pages

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"


def create_app(
    settings: WebSettings | None = None,
    extractor_factory=InvoiceExtractor,
) -> FastAPI:
    """Build the application.

    Args:
        settings: Configuration. Read from the environment when omitted.
        extractor_factory: Called to build the extractor for each background
            task. Injected so the tests can run the whole stack without
            touching the API.
    """
    settings = settings or WebSettings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        recovered = recover_interrupted(app.state.settings)
        if recovered:
            logger.warning("marked %s interrupted invoice(s) as failed", recovered)
        yield

    app = FastAPI(title="Invoice Extractor", lifespan=lifespan)
    app.state.settings = settings
    app.state.extractor_factory = extractor_factory

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["money"] = _money
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    # actions first: /invoices/export.csv has to be matched before the
    # /invoices/{invoice_id} pattern, which would reject it as a bad int.
    app.include_router(actions.router)
    app.include_router(pages.router)
    app.include_router(fragments.router)
    return app


def _money(value, currency: str | None = None) -> str:
    """Render an amount for display, leaving a missing one visibly missing."""
    if value is None:
        return "—"
    rendered = f"{float(value):,.2f}"
    return f"{rendered} {currency}" if currency else rendered
