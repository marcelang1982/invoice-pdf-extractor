"""SQLAlchemy models for stored invoices and their validation findings.

Postgres-native types throughout: `JSONB` for the extraction payload, native
timestamps with time zone. The test suite runs against a real Postgres 16
container rather than a portable stand-in, so there is nothing to abstract.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

#: Separates the two halves of a stored duplicate key. A unit separator cannot
#: occur in a vendor name or invoice number read off a document.
DUP_KEY_SEPARATOR = "\x1f"


class Base(DeclarativeBase):
    """Declarative base carrying the metadata Alembic autogenerates against."""


class InvoiceStatus(StrEnum):
    """Where an invoice is in the extraction and validation pipeline.

    This is the machine's verdict. What a human decided about it afterwards is
    `ReviewStatus`, deliberately separate: approving an invoice does not make
    its arithmetic correct, and re-running extraction should not silently
    discard someone's decision.
    """

    PROCESSING = "processing"
    VALID = "valid"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class ReviewStatus(StrEnum):
    """What a human decided about an invoice."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Invoice(Base):
    """One uploaded PDF, its extraction, and its review state."""

    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint(
            "status IN ('processing', 'valid', 'needs_review', 'failed')",
            name="ck_invoices_status",
        ),
        CheckConstraint(
            "review_status IN ('pending', 'approved', 'rejected')",
            name="ck_invoices_review_status",
        ),
        Index("ix_invoices_status", "status"),
        Index("ix_invoices_review_status", "review_status"),
        Index("ix_invoices_uploaded_at", "uploaded_at"),
        Index("ix_invoices_dup_key", "dup_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # --- the file ---------------------------------------------------------- #
    filename: Mapped[str] = mapped_column(Text)
    #: Content hash. Unique, so re-uploading the same bytes is recognised
    #: instead of re-extracted.
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    stored_path: Mapped[str] = mapped_column(Text)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # --- machine verdict --------------------------------------------------- #
    status: Mapped[str] = mapped_column(String(16), default=InvoiceStatus.PROCESSING)
    #: Populated only when status is `failed`.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- human verdict ----------------------------------------------------- #
    review_status: Mapped[str] = mapped_column(
        String(16), default=ReviewStatus.PENDING
    )
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- key fields, denormalized out of the payload for listing and sorting - #
    vendor_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Null when the document's date could not be parsed. The validator reports
    #: that separately as INVALID_DATE_FORMAT.
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    #: Normalized vendor and number, encoded for indexing. Built only through
    #: `encode_dup_key` so it can never disagree with the engine.
    dup_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- the full result and what it cost ---------------------------------- #
    extraction: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    processing_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    issues: Mapped[list[ValidationIssue]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="ValidationIssue.id",
    )

    @property
    def issue_codes(self) -> list[str]:
        return [issue.code for issue in self.issues]


class ValidationIssue(Base):
    """One finding against a stored invoice."""

    __tablename__ = "validation_issues"
    __table_args__ = (Index("ix_validation_issues_invoice_id", "invoice_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE")
    )
    code: Mapped[str] = mapped_column(String(64))
    field: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    #: For DUPLICATE_INVOICE, the id of the invoice already on record.
    related_invoice_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    invoice: Mapped[Invoice] = relationship(back_populates="issues")


def encode_dup_key(key: tuple[str, str] | None) -> str | None:
    """Encode the engine's duplicate key for storage in one indexed column."""
    if key is None:
        return None
    return DUP_KEY_SEPARATOR.join(key)


def decode_dup_key(stored: str | None) -> tuple[str, str] | None:
    """Recover the engine's duplicate key from its stored form."""
    if not stored:
        return None
    vendor, separator, number = stored.partition(DUP_KEY_SEPARATOR)
    return (vendor, number) if separator else None
