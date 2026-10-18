"""Configuration for the web application, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_DATABASE_URL = "postgresql+psycopg://invoices:invoices@localhost:5432/invoices"
DEFAULT_STORAGE_DIR = Path("/data/pdfs")


class WebConfigError(RuntimeError):
    """Raised when required web configuration is missing or unusable."""


@dataclass(frozen=True)
class WebSettings:
    """Everything the web app needs that is not already in `Settings`."""

    database_url: str = DEFAULT_DATABASE_URL
    storage_dir: Path = DEFAULT_STORAGE_DIR
    #: Directories scanned by "Load sample invoices", in order.
    sample_dirs: tuple[Path, ...] = ()

    @classmethod
    def from_env(cls, *, load: bool = True) -> WebSettings:
        """Read settings from the environment, loading `.env` first if present."""
        if load:
            load_dotenv(override=False)

        repo_root = Path(__file__).resolve().parent.parent.parent
        raw_samples = os.getenv("SAMPLE_DIRS")
        if raw_samples:
            sample_dirs = tuple(Path(p) for p in raw_samples.split(os.pathsep) if p)
        else:
            sample_dirs = (
                repo_root / "samples" / "pdfs",
                repo_root / "samples" / "hard" / "pdfs",
            )

        return cls(
            database_url=_normalize_driver(
                os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL
            ),
            storage_dir=Path(os.getenv("STORAGE_DIR") or DEFAULT_STORAGE_DIR),
            sample_dirs=sample_dirs,
        )


def _normalize_driver(url: str) -> str:
    """Pin the URL to psycopg 3.

    Plain `postgresql://` defaults to psycopg2, which is not installed, and
    tooling such as testcontainers hands back a psycopg2 URL. Rewriting here
    means callers never have to think about it.
    """
    for prefix, replacement in (
        ("postgresql+psycopg2://", "postgresql+psycopg://"),
        ("postgres://", "postgresql+psycopg://"),
    ):
        if url.startswith(prefix):
            return replacement + url[len(prefix) :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url
