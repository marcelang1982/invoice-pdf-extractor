"""Pydantic models for extracted invoices and validation results.

Design note: every field on `Invoice` is optional. The model describes *what the
PDF actually contains*, not what a well-formed invoice should contain. A missing
vendor tax ID is a finding for the validator to report, not a parse failure that
aborts extraction. Required-field enforcement lives in `validator.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ValidationStatus = Literal["valid", "needs_review"]


class IssueCode(StrEnum):
    """Stable codes for validation findings. Downstream phases key off these."""

    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    LINE_ITEMS_SUM_MISMATCH = "LINE_ITEMS_SUM_MISMATCH"
    LINE_ITEM_AMOUNT_MISMATCH = "LINE_ITEM_AMOUNT_MISMATCH"
    TOTAL_MISMATCH = "TOTAL_MISMATCH"
    TAX_RATE_OUT_OF_RANGE = "TAX_RATE_OUT_OF_RANGE"
    DUE_DATE_BEFORE_ISSUE_DATE = "DUE_DATE_BEFORE_ISSUE_DATE"
    INVALID_DATE_FORMAT = "INVALID_DATE_FORMAT"
    DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
    NO_LINE_ITEMS = "NO_LINE_ITEMS"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"


class LineItem(BaseModel):
    """A single billed line on an invoice."""

    model_config = ConfigDict(extra="ignore")

    description: str | None = Field(
        default=None, description="Description of the product or service billed."
    )
    quantity: float | None = Field(
        default=None, description="Number of units billed."
    )
    unit_price: float | None = Field(
        default=None, description="Price per unit, before tax."
    )
    amount: float | None = Field(
        default=None,
        description="Line total, before tax. Normally quantity x unit_price.",
    )


class Invoice(BaseModel):
    """Structured invoice data as printed on the document.

    Transcribe values exactly as they appear. Do not recompute or correct
    arithmetic: a wrong total on the page must come back as the wrong total so
    the validator can flag it.
    """

    model_config = ConfigDict(extra="ignore")

    vendor_name: str | None = Field(
        default=None, description="Legal or trading name of the issuing company."
    )
    vendor_tax_id: str | None = Field(
        default=None,
        description="Vendor tax identifier, e.g. VAT ID, EIN or CUIT, as printed.",
    )
    invoice_number: str | None = Field(
        default=None, description="Invoice number or identifier as printed."
    )
    issue_date: str | None = Field(
        default=None, description="Date the invoice was issued, as ISO 8601 (YYYY-MM-DD)."
    )
    due_date: str | None = Field(
        default=None, description="Payment due date, as ISO 8601 (YYYY-MM-DD)."
    )
    currency: str | None = Field(
        default=None, description="ISO 4217 currency code, e.g. USD or EUR."
    )
    line_items: list[LineItem] = Field(
        default_factory=list, description="Every billed line on the invoice."
    )
    subtotal: float | None = Field(
        default=None, description="Sum of all line amounts, before tax."
    )
    tax_rate: float | None = Field(
        default=None,
        description="Tax rate as a percentage, e.g. 21 for 21%. Not a fraction.",
    )
    tax_amount: float | None = Field(
        default=None, description="Total tax charged, in the invoice currency."
    )
    total: float | None = Field(
        default=None, description="Grand total due, including tax."
    )


class ValidationIssue(BaseModel):
    """One problem found in an invoice."""

    code: IssueCode
    field: str
    message: str


class ValidationResult(BaseModel):
    """Outcome of validating a single invoice."""

    status: ValidationStatus
    issues: list[ValidationIssue] = Field(default_factory=list)

    @classmethod
    def from_issues(cls, issues: list[ValidationIssue]) -> ValidationResult:
        """Build a result, deriving `status` from whether any issue was found."""
        return cls(status="needs_review" if issues else "valid", issues=issues)


class TokenUsage(BaseModel):
    """Tokens billed for one extraction."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class ExtractionResult(BaseModel):
    """What the extractor returns for one PDF: the invoice plus call metadata."""

    file: str
    invoice: Invoice | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model: str = ""
    attempts: int = 0
    duration_ms: int = 0
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.invoice is not None


class FileResult(BaseModel):
    """Extraction plus validation for one PDF. One row of `results.json`."""

    file: str
    status: ValidationStatus
    issues: list[ValidationIssue] = Field(default_factory=list)
    invoice: Invoice | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    attempts: int = 0
    duration_ms: int = 0
    error: str | None = None


class BatchTotals(BaseModel):
    """Aggregates across a batch run."""

    files: int = 0
    valid: int = 0
    needs_review: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class BatchResult(BaseModel):
    """Top-level shape of `results.json`."""

    model: str
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    totals: BatchTotals = Field(default_factory=BatchTotals)
    results: list[FileResult] = Field(default_factory=list)


def invoice_input_schema() -> dict[str, Any]:
    """JSON Schema for the extraction tool, generated from the `Invoice` model.

    Single source of truth: the schema Claude is constrained to and the model we
    parse into cannot drift apart.
    """
    return Invoice.model_json_schema()
