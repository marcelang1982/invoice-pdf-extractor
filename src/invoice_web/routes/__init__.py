"""HTTP routes, split by what they return.

`pages` renders full HTML documents, `fragments` renders the partials HTMX
swaps in, and `actions` handles mutations and downloads.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from ..config import WebSettings
from ..db import get_sessionmaker


def get_settings(request: Request) -> WebSettings:
    """The application's settings, as a dependency."""
    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    """A request-scoped session that commits on success."""
    factory = get_sessionmaker(request.app.state.settings.database_url)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_templates(request: Request):
    """The configured Jinja environment."""
    return request.app.state.templates
