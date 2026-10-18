"""Initial schema: invoices and their validation issues.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invoices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("stored_path", sa.Text(), nullable=False),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "review_status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("vendor_name", sa.Text(), nullable=True),
        sa.Column("invoice_number", sa.Text(), nullable=True),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("total", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("dup_key", sa.Text(), nullable=True),
        sa.Column("extraction", postgresql.JSONB(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processing_ms", sa.Integer(), nullable=True),
        sa.UniqueConstraint("sha256", name="uq_invoices_sha256"),
        sa.CheckConstraint(
            "status IN ('processing', 'valid', 'needs_review', 'failed')",
            name="ck_invoices_status",
        ),
        sa.CheckConstraint(
            "review_status IN ('pending', 'approved', 'rejected')",
            name="ck_invoices_review_status",
        ),
    )
    op.create_index("ix_invoices_status", "invoices", ["status"])
    op.create_index("ix_invoices_review_status", "invoices", ["review_status"])
    op.create_index("ix_invoices_uploaded_at", "invoices", ["uploaded_at"])
    op.create_index("ix_invoices_dup_key", "invoices", ["dup_key"])

    op.create_table(
        "validation_issues",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("related_invoice_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.id"],
            name="fk_validation_issues_invoice_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_validation_issues_invoice_id", "validation_issues", ["invoice_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_validation_issues_invoice_id", table_name="validation_issues")
    op.drop_table("validation_issues")
    op.drop_index("ix_invoices_dup_key", table_name="invoices")
    op.drop_index("ix_invoices_uploaded_at", table_name="invoices")
    op.drop_index("ix_invoices_review_status", table_name="invoices")
    op.drop_index("ix_invoices_status", table_name="invoices")
    op.drop_table("invoices")
