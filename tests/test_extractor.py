"""Extractor behavior against a fake client. Nothing here touches the network."""

from __future__ import annotations

import base64

import pytest

from conftest import FakeClient, text_only_response, timeout_error, tool_use_response
from invoice_extractor.config import ConfigError, Settings
from invoice_extractor.extractor import TOOL_NAME, InvoiceExtractor, find_pdfs
from invoice_extractor.models import Invoice


@pytest.fixture
def settings() -> Settings:
    """Settings that never read the environment."""
    return Settings(api_key="test-key", model="claude-haiku-5-5", max_retries=3)


def build_extractor(responses, settings, sleeps=None):
    """An extractor wired to a scripted fake client, with sleeps recorded."""
    client = FakeClient(responses)
    extractor = InvoiceExtractor(
        settings=settings,
        client=client,
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
    )
    return extractor, client


# --------------------------------------------------------------------------- #
# Request shape
# --------------------------------------------------------------------------- #


def test_pdf_is_sent_as_a_base64_document_block(settings, pdf_file, invoice_data):
    extractor, client = build_extractor([tool_use_response(invoice_data)], settings)

    extractor.extract_file(pdf_file)

    content = client.messages.calls[0]["messages"][0]["content"]
    document = content[0]
    assert document["type"] == "document"
    assert document["source"]["type"] == "base64"
    assert document["source"]["media_type"] == "application/pdf"
    assert base64.standard_b64decode(document["source"]["data"]) == pdf_file.read_bytes()


def test_document_block_precedes_the_instruction_text(settings, pdf_file, invoice_data):
    extractor, client = build_extractor([tool_use_response(invoice_data)], settings)

    extractor.extract_file(pdf_file)

    content = client.messages.calls[0]["messages"][0]["content"]
    assert [block["type"] for block in content] == ["document", "text"]


def test_output_is_forced_through_the_tool(settings, pdf_file, invoice_data):
    extractor, client = build_extractor([tool_use_response(invoice_data)], settings)

    extractor.extract_file(pdf_file)

    call = client.messages.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    assert [tool["name"] for tool in call["tools"]] == [TOOL_NAME]


def test_tool_schema_comes_from_the_pydantic_model(settings, pdf_file, invoice_data):
    extractor, client = build_extractor([tool_use_response(invoice_data)], settings)

    extractor.extract_file(pdf_file)

    schema = client.messages.calls[0]["tools"][0]["input_schema"]
    assert schema["type"] == "object"
    # Compared against the model, not the fixture: adding a field to Invoice
    # must reach the API schema without anyone remembering to update a test.
    assert set(schema["properties"]) == set(Invoice.model_fields)


def test_configured_model_and_max_tokens_are_used(pdf_file, invoice_data):
    settings = Settings(api_key="k", model="claude-sonnet-5", max_tokens=1234)
    extractor, client = build_extractor([tool_use_response(invoice_data)], settings)

    extractor.extract_file(pdf_file)

    assert client.messages.calls[0]["model"] == "claude-sonnet-5"
    assert client.messages.calls[0]["max_tokens"] == 1234


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_successful_extraction_returns_a_parsed_invoice(settings, pdf_file, invoice_data):
    extractor, _ = build_extractor([tool_use_response(invoice_data)], settings)

    result = extractor.extract_file(pdf_file)

    assert result.succeeded
    assert result.invoice.vendor_name == "Northwind Logistics LLC"
    assert result.invoice.total == 275.0
    assert len(result.invoice.line_items) == 2
    assert result.file == "invoice_01.pdf"
    assert result.attempts == 1
    assert result.error is None


def test_token_usage_is_recorded(settings, pdf_file, invoice_data):
    extractor, _ = build_extractor(
        [tool_use_response(invoice_data, input_tokens=2500, output_tokens=310)],
        settings,
    )

    result = extractor.extract_file(pdf_file)

    assert result.usage.input_tokens == 2500
    assert result.usage.output_tokens == 310
    assert result.usage.total_tokens == 2810


def test_unknown_fields_in_the_payload_are_ignored(settings, pdf_file, invoice_data):
    """Model-invented fields must not break parsing."""
    extractor, _ = build_extractor(
        [tool_use_response({**invoice_data, "purchase_order": "PO-99"})], settings
    )

    result = extractor.extract_file(pdf_file)

    assert result.succeeded
    assert not hasattr(result.invoice, "purchase_order")


def test_partial_payload_parses_with_nulls(settings, pdf_file):
    """Every field is optional, so a sparse invoice is a finding, not a crash."""
    extractor, _ = build_extractor(
        [tool_use_response({"vendor_name": "Acme", "total": 10.0})], settings
    )

    result = extractor.extract_file(pdf_file)

    assert result.succeeded
    assert result.invoice.vendor_tax_id is None
    assert result.invoice.line_items == []


# --------------------------------------------------------------------------- #
# Retries
# --------------------------------------------------------------------------- #


def test_transient_api_error_is_retried(settings, pdf_file, invoice_data):
    sleeps: list[float] = []
    extractor, client = build_extractor(
        [timeout_error(), tool_use_response(invoice_data)], settings, sleeps
    )

    result = extractor.extract_file(pdf_file)

    assert result.succeeded
    assert result.attempts == 2
    assert len(client.messages.calls) == 2
    assert sleeps == [1.0]


def test_backoff_doubles_between_attempts(settings, pdf_file, invoice_data):
    sleeps: list[float] = []
    extractor, _ = build_extractor(
        [timeout_error(), timeout_error(), tool_use_response(invoice_data)],
        settings,
        sleeps,
    )

    result = extractor.extract_file(pdf_file)

    assert result.attempts == 3
    assert sleeps == [1.0, 2.0]


def test_response_without_a_tool_call_is_retried(settings, pdf_file, invoice_data):
    extractor, _ = build_extractor(
        [text_only_response(), tool_use_response(invoice_data)], settings
    )

    result = extractor.extract_file(pdf_file)

    assert result.succeeded
    assert result.attempts == 2


def test_payload_failing_the_schema_is_retried(settings, pdf_file, invoice_data):
    extractor, _ = build_extractor(
        [tool_use_response({**invoice_data, "total": "not a number"}),
         tool_use_response(invoice_data)],
        settings,
    )

    result = extractor.extract_file(pdf_file)

    assert result.succeeded
    assert result.attempts == 2


def test_tokens_from_failed_attempts_are_still_counted(settings, pdf_file, invoice_data):
    """A rejected response was still billed; the cost report must say so."""
    extractor, _ = build_extractor(
        [
            text_only_response(input_tokens=1000, output_tokens=50),
            tool_use_response(invoice_data, input_tokens=1000, output_tokens=200),
        ],
        settings,
    )

    result = extractor.extract_file(pdf_file)

    assert result.usage.input_tokens == 2000
    assert result.usage.output_tokens == 250


def test_no_sleep_after_the_final_attempt(settings, pdf_file):
    sleeps: list[float] = []
    extractor, _ = build_extractor(
        [timeout_error(), timeout_error(), timeout_error()], settings, sleeps
    )

    extractor.extract_file(pdf_file)

    assert len(sleeps) == 2


# --------------------------------------------------------------------------- #
# Exhausted retries
# --------------------------------------------------------------------------- #


def test_exhausted_retries_record_the_error_instead_of_raising(settings, pdf_file):
    extractor, client = build_extractor(
        [timeout_error(), timeout_error(), timeout_error()], settings
    )

    result = extractor.extract_file(pdf_file)

    assert not result.succeeded
    assert result.invoice is None
    assert result.attempts == 3
    assert len(client.messages.calls) == 3
    assert "APITimeoutError" in result.error


def test_persistent_missing_tool_call_is_reported(settings, pdf_file):
    extractor, _ = build_extractor([text_only_response()] * 3, settings)

    result = extractor.extract_file(pdf_file)

    assert not result.succeeded
    assert "ExtractionError" in result.error


def test_max_retries_of_one_makes_a_single_attempt(pdf_file):
    settings = Settings(api_key="k", max_retries=1)
    extractor, client = build_extractor([timeout_error()], settings)

    result = extractor.extract_file(pdf_file)

    assert len(client.messages.calls) == 1
    assert result.attempts == 1


# --------------------------------------------------------------------------- #
# Batches and file discovery
# --------------------------------------------------------------------------- #


def test_extract_dir_processes_every_pdf_in_order(settings, tmp_path, invoice_data):
    for name in ("b.pdf", "a.pdf", "c.pdf"):
        (tmp_path / name).write_bytes(b"%PDF-1.4")
    (tmp_path / "notes.txt").write_text("ignored")

    extractor, _ = build_extractor([tool_use_response(invoice_data)] * 3, settings)
    results = extractor.extract_dir(tmp_path)

    assert [r.file for r in results] == ["a.pdf", "b.pdf", "c.pdf"]


def test_find_pdfs_rejects_a_non_directory(tmp_path):
    with pytest.raises(NotADirectoryError):
        find_pdfs(tmp_path / "missing")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_building_an_extractor_does_not_require_an_api_key():
    """Construction must stay cheap so the CLI can validate input first."""
    InvoiceExtractor(settings=Settings(api_key=None))


def test_missing_api_key_is_reported_clearly():
    extractor = InvoiceExtractor(settings=Settings(api_key=None))

    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        _ = extractor.client
