"""Shared fixtures and API fakes.

No test in this suite touches the network. The extractor talks to a fake client
that replays a scripted list of responses, which also makes retry behavior and
token accounting directly assertable.
"""

from __future__ import annotations

from typing import Any

import anthropic
import pytest

from invoice_extractor.extractor import TOOL_NAME
from invoice_extractor.models import Invoice


class FakeToolUseBlock:
    """Stands in for a `tool_use` content block."""

    def __init__(self, payload: dict, name: str = TOOL_NAME) -> None:
        self.type = "tool_use"
        self.id = "toolu_fake"
        self.name = name
        self.input = payload


class FakeTextBlock:
    """Stands in for a `text` content block."""

    def __init__(self, text: str = "Here is the invoice.") -> None:
        self.type = "text"
        self.text = text


class FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeMessage:
    """Stands in for an API response."""

    def __init__(
        self,
        content: list[Any],
        input_tokens: int = 1000,
        output_tokens: int = 200,
        stop_reason: str = "tool_use",
    ) -> None:
        self.content = content
        self.usage = FakeUsage(input_tokens, output_tokens)
        self.stop_reason = stop_reason


class FakeMessages:
    """Replays scripted responses; raises any scripted exception in order."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeMessages ran out of scripted responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    """Minimal stand-in for `anthropic.Anthropic`."""

    def __init__(self, responses: list[Any]) -> None:
        self.messages = FakeMessages(responses)


def tool_use_response(payload: dict, **kwargs: Any) -> FakeMessage:
    """A response carrying the forced tool call."""
    return FakeMessage([FakeToolUseBlock(payload)], **kwargs)


def text_only_response(**kwargs: Any) -> FakeMessage:
    """A response with no tool call, which the extractor must reject."""
    return FakeMessage([FakeTextBlock()], stop_reason="end_turn", **kwargs)


def timeout_error() -> anthropic.APITimeoutError:
    """A retryable transport error.

    `request` is only stored, never inspected, so a plain object keeps the test
    suite decoupled from whichever HTTP library the SDK vendors.
    """
    return anthropic.APITimeoutError(request=object())


@pytest.fixture
def invoice_data() -> dict:
    """A fully consistent invoice payload: no rule should fire on it."""
    return {
        "vendor_name": "Northwind Logistics LLC",
        "vendor_tax_id": "47-8821093",
        "invoice_number": "NL-2026-1011",
        "issue_date": "2026-03-21",
        "due_date": "2026-05-05",
        "currency": "USD",
        "line_items": [
            {
                "description": "Freight forwarding",
                "quantity": 2.0,
                "unit_price": 100.0,
                "amount": 200.0,
            },
            {
                "description": "Container handling fee",
                "quantity": 1.0,
                "unit_price": 50.0,
                "amount": 50.0,
            },
        ],
        "subtotal": 250.0,
        "tax_rate": 10.0,
        "tax_amount": 25.0,
        "total": 275.0,
    }


@pytest.fixture
def valid_invoice(invoice_data: dict) -> Invoice:
    """The consistent payload, as an `Invoice`."""
    return Invoice.model_validate(invoice_data)


@pytest.fixture
def pdf_file(tmp_path):
    """A file on disk with PDF-ish bytes. Content is never parsed locally."""
    path = tmp_path / "invoice_01.pdf"
    path.write_bytes(b"%PDF-1.4\nfake invoice bytes\n%%EOF")
    return path
