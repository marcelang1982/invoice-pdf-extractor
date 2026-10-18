"""Content-addressed PDF storage. No database, no Docker."""

from __future__ import annotations

from invoice_web.storage import looks_like_pdf, sha256_of, store_pdf


def test_the_same_bytes_hash_the_same():
    assert sha256_of(b"%PDF-1.4 hello") == sha256_of(b"%PDF-1.4 hello")


def test_different_bytes_hash_differently():
    assert sha256_of(b"%PDF-1.4 a") != sha256_of(b"%PDF-1.4 b")


def test_pdfs_are_recognised_by_their_magic_bytes():
    assert looks_like_pdf(b"%PDF-1.7\n...")
    assert not looks_like_pdf(b"PK\x03\x04")
    assert not looks_like_pdf(b"")


def test_storing_writes_the_file_under_its_hash(tmp_path):
    data = b"%PDF-1.4 content"
    digest = sha256_of(data)

    path = store_pdf(data, digest, tmp_path / "pdfs")

    assert path.name == f"{digest}.pdf"
    assert path.read_bytes() == data


def test_storing_twice_is_idempotent(tmp_path):
    data = b"%PDF-1.4 content"
    digest = sha256_of(data)
    storage = tmp_path / "pdfs"

    first = store_pdf(data, digest, storage)
    second = store_pdf(data, digest, storage)

    assert first == second
    assert list(storage.iterdir()) == [first]


def test_no_partial_file_is_left_behind(tmp_path):
    """The write goes through a .part file, which must not survive."""
    data = b"%PDF-1.4 content"
    storage = tmp_path / "pdfs"

    store_pdf(data, sha256_of(data), storage)

    assert [p.suffix for p in storage.iterdir()] == [".pdf"]


def test_the_storage_directory_is_created_on_demand(tmp_path):
    target = tmp_path / "deep" / "nested" / "pdfs"

    store_pdf(b"%PDF-1.4", sha256_of(b"%PDF-1.4"), target)

    assert target.is_dir()
