"""Fixtures for the web tests.

These run against a real Postgres 16 in a throwaway container, migrated with
`alembic upgrade head` so the migration is exercised rather than assumed. That
costs a few seconds of startup and needs Docker; when Docker is not available
the whole directory skips with a clear reason, and the engine's own tests keep
running with no services at all.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text

from invoice_web.config import WebSettings, _normalize_driver
from invoice_web.db import get_engine, get_sessionmaker

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

try:  # pragma: no cover - import guard, exercised only by its absence
    # Moved packages in testcontainers 4.x; the old path still works but warns.
    from testcontainers.community.postgres import PostgresContainer
except ImportError:  # pragma: no cover
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        PostgresContainer = None


def _docker_available() -> bool:
    """True when a Docker daemon is reachable."""
    try:
        return (
            subprocess.run(
                ["docker", "info"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """A migrated, empty Postgres 16 for the whole session."""
    if PostgresContainer is None:
        pytest.skip(
            "testcontainers is not installed. Install the dev extra with "
            "`pip install -e \".[dev,web]\"` to run the web tests."
        )
    if not _docker_available():
        pytest.skip(
            "Docker is not running, so the web tests cannot start Postgres. "
            "Start Docker Desktop and re-run; the engine tests do not need it."
        )

    with PostgresContainer("postgres:16-alpine") as container:
        url = _normalize_driver(container.get_connection_url())
        _migrate(url)
        yield url


def _migrate(url: str) -> None:
    """Run `alembic upgrade head`, failing loudly with alembic's own output."""
    # Invoked through this interpreter rather than a bare `alembic`: the venv's
    # bin directory is not necessarily on PATH when pytest is run as
    # `.venv/bin/python -m pytest`.
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}"
        )


@pytest.fixture
def settings(database_url: str, tmp_path: Path) -> WebSettings:
    """Settings pointing at the test database and a per-test storage dir."""
    return WebSettings(
        database_url=database_url,
        storage_dir=tmp_path / "pdfs",
        sample_dirs=(),
    )


@pytest.fixture(autouse=True)
def clean_tables(request: pytest.FixtureRequest) -> Iterator[None]:
    """Empty both tables between tests.

    Truncating is far cheaper than re-running migrations per test, and
    RESTART IDENTITY keeps ids predictable so assertions can name them.
    """
    if "database_url" not in request.fixturenames:
        yield
        return

    url = request.getfixturevalue("database_url")
    engine = get_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text("TRUNCATE validation_issues, invoices RESTART IDENTITY CASCADE")
        )
    yield


@pytest.fixture
def session(settings: WebSettings) -> Iterator:
    """A plain session for tests that poke at the database directly."""
    factory = get_sessionmaker(settings.database_url)
    with factory() as database_session:
        yield database_session
