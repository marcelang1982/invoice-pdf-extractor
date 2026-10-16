"""Model invariants the rest of the engine relies on."""

from __future__ import annotations

from invoice_extractor.models import (
    BatchResult,
    FileResult,
    Invoice,
    IssueCode,
    TokenUsage,
    ValidationIssue,
    ValidationResult,
    invoice_input_schema,
)


def test_every_invoice_field_is_optional():
    """A blank invoice must parse: absent fields are findings, not errors."""
    invoice = Invoice()

    assert invoice.vendor_name is None
    assert invoice.total is None
    assert invoice.line_items == []


def test_tool_schema_exposes_every_invoice_field():
    schema = invoice_input_schema()

    assert set(schema["properties"]) == set(Invoice.model_fields)


def test_tool_schema_marks_nothing_required():
    """Required-ness is the validator's job, not the API's."""
    assert not invoice_input_schema().get("required")


def test_tool_schema_describes_line_items_via_a_ref():
    schema = invoice_input_schema()

    assert "LineItem" in schema["$defs"]
    assert set(schema["$defs"]["LineItem"]["properties"]) == {
        "description",
        "quantity",
        "unit_price",
        "amount",
    }


def test_status_follows_from_the_issue_list():
    assert ValidationResult.from_issues([]).status == "valid"

    issue = ValidationIssue(
        code=IssueCode.TOTAL_MISMATCH, field="total", message="nope"
    )
    assert ValidationResult.from_issues([issue]).status == "needs_review"


def test_token_usage_adds_up():
    total = TokenUsage(input_tokens=10, output_tokens=2) + TokenUsage(
        input_tokens=5, output_tokens=3
    )

    assert (total.input_tokens, total.output_tokens) == (15, 5)
    assert total.total_tokens == 20


def test_batch_result_round_trips_through_json():
    """`results.json` must deserialize cleanly for evaluate.py and later phases."""
    batch = BatchResult(
        model="claude-haiku-4-5",
        results=[
            FileResult(
                file="invoice_01.pdf",
                status="needs_review",
                issues=[
                    ValidationIssue(
                        code=IssueCode.TOTAL_MISMATCH, field="total", message="off by 40"
                    )
                ],
                invoice=Invoice(vendor_name="Acme", total=10.0),
                usage=TokenUsage(input_tokens=100, output_tokens=20),
            )
        ],
    )

    restored = BatchResult.model_validate_json(batch.model_dump_json())

    assert restored.results[0].issues[0].code is IssueCode.TOTAL_MISMATCH
    assert restored.results[0].invoice.vendor_name == "Acme"
    assert restored.results[0].usage.total_tokens == 120
