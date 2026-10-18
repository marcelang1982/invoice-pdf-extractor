"""PDF storage on disk, addressed by content hash."""

from __future__ import annotations

import hashlib
from pathlib import Path

#: PDFs all start with this. Checked on upload so an obvious non-PDF is
#: rejected before it costs an API call.
PDF_MAGIC = b"%PDF-"


def sha256_of(data: bytes) -> str:
    """Content hash, used as both the identity and the stored filename."""
    return hashlib.sha256(data).hexdigest()


def looks_like_pdf(data: bytes) -> bool:
    """Cheap sanity check on uploaded bytes."""
    return data.startswith(PDF_MAGIC)


def store_pdf(data: bytes, digest: str, storage_dir: Path) -> Path:
    """Write bytes under their hash, returning the path.

    Content-addressed, so storing the same PDF twice is idempotent and two
    uploads can never collide on a name. Writes through a temporary file and
    renames, so a crash mid-write cannot leave a truncated PDF that looks
    complete.
    """
    storage_dir.mkdir(parents=True, exist_ok=True)
    destination = storage_dir / f"{digest}.pdf"
    if destination.exists():
        return destination

    temporary = destination.with_suffix(".pdf.part")
    temporary.write_bytes(data)
    temporary.replace(destination)
    return destination
