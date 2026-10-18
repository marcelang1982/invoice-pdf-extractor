"""Database engine and session plumbing."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import WebSettings


@lru_cache(maxsize=4)
def get_engine(database_url: str) -> Engine:
    """Build (and reuse) the engine for a URL.

    `pool_pre_ping` matters here: the app and Postgres start together under
    Compose, and a connection checked out from the pool after the database
    restarts would otherwise fail on first use.
    """
    return create_engine(database_url, pool_pre_ping=True, future=True)


@lru_cache(maxsize=4)
def get_sessionmaker(database_url: str) -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_engine(database_url), expire_on_commit=False, future=True
    )


@contextmanager
def session_scope(settings: WebSettings) -> Iterator[Session]:
    """A transactional session: commit on success, roll back on error.

    Used by background work, which has no request to hang a dependency off.
    """
    factory = get_sessionmaker(settings.database_url)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
