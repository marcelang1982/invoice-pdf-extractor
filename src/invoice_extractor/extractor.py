"""PDF -> `Invoice` extraction via the Anthropic API.

The PDF is sent as a base64 `document` content block and the output is
constrained with forced tool use: a single tool whose `input_schema` is
generated from the `Invoice` Pydantic model. Claude cannot reply with prose, so
there is no JSON to scrape out of free text.
"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import anthropic
from pydantic import ValidationError

from .config import Settings
from .models import ExtractionResult, Invoice, TokenUsage, invoice_input_schema

logger = logging.getLogger(__name__)

TOOL_NAME = "record_invoice"

SYSTEM_PROMPT = (
    "You extract structured data from invoice PDFs. Transcribe every value "
    "exactly as printed on the document.\n"
    "\n"
    "Never recompute, correct, or reconcile arithmetic. If the printed total "
    "does not match the subtotal plus tax, report the printed total. If a field "
    "is absent from the document, leave it null rather than inferring it. "
    "Downstream checks depend on seeing the document's own errors."
)

USER_PROMPT = (
    "Extract this invoice using the record_invoice tool. Report every value as "
    "printed, and leave anything the document does not state as null."
)

# Errors worth a second attempt: transient transport and server-side problems.
_RETRYABLE_API_ERRORS: tuple[type[Exception], ...] = (
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)


class ExtractionError(RuntimeError):
    """The API answered, but not with a usable invoice."""


class InvoiceExtractor:
    """Extracts invoices from PDFs. Reusable across batches and phases."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Build an extractor.

        Args:
            settings: Model and retry configuration. Read from the environment
                when omitted.
            client: An Anthropic client. Injected by tests; built on first use
                otherwise, so constructing an extractor never needs an API key.
            sleep: Delay function used for retry backoff. Injected by tests.
        """
        self.settings = settings or Settings.from_env()
        self._client = client
        self._sleep = sleep

    @property
    def client(self) -> Any:
        """The Anthropic client, created on first access."""
        if self._client is None:
            # Our own retry loop owns backoff, so disable the SDK's.
            self._client = anthropic.Anthropic(
                api_key=self.settings.require_api_key(), max_retries=0
            )
        return self._client

    def extract_file(self, path: str | Path) -> ExtractionResult:
        """Extract one PDF, retrying transient failures.

        Never raises for an extraction failure: the error is recorded on the
        returned `ExtractionResult` so a batch run can continue past one bad
        file. Token usage accumulates across attempts, since a failed attempt
        that reached the API was still billed.
        """
        path = Path(path)
        request = self._build_request(path)

        usage = TokenUsage()
        started = time.monotonic()
        last_error: Exception | None = None

        for attempt in range(1, self.settings.max_retries + 1):
            try:
                message = self.client.messages.create(**request)
            except _RETRYABLE_API_ERRORS as exc:
                last_error = exc
                logger.warning(
                    "%s: API error on attempt %d/%d: %s",
                    path.name,
                    attempt,
                    self.settings.max_retries,
                    exc,
                )
            else:
                usage = usage + _usage_from(message)
                try:
                    invoice = _invoice_from(message)
                except (ExtractionError, ValidationError) as exc:
                    last_error = exc
                    logger.warning(
                        "%s: unusable response on attempt %d/%d: %s",
                        path.name,
                        attempt,
                        self.settings.max_retries,
                        exc,
                    )
                else:
                    return ExtractionResult(
                        file=path.name,
                        invoice=invoice,
                        usage=usage,
                        model=self.settings.model,
                        attempts=attempt,
                        duration_ms=_elapsed_ms(started),
                    )

            if attempt < self.settings.max_retries:
                self._sleep(2.0 ** (attempt - 1))

        return ExtractionResult(
            file=path.name,
            invoice=None,
            usage=usage,
            model=self.settings.model,
            attempts=self.settings.max_retries,
            duration_ms=_elapsed_ms(started),
            error=f"{type(last_error).__name__}: {last_error}",
        )

    def extract_paths(self, paths: Iterable[str | Path]) -> list[ExtractionResult]:
        """Extract a sequence of PDFs, in the order given."""
        return [self.extract_file(path) for path in paths]

    def extract_dir(self, directory: str | Path) -> list[ExtractionResult]:
        """Extract every `*.pdf` in a directory, sorted by filename."""
        return self.extract_paths(find_pdfs(directory))

    def _build_request(self, path: Path) -> dict[str, Any]:
        """Assemble the Messages API request for one PDF."""
        encoded = base64.standard_b64encode(path.read_bytes()).decode("ascii")

        return {
            "model": self.settings.model,
            "max_tokens": self.settings.max_tokens,
            "system": SYSTEM_PROMPT,
            "tools": [
                {
                    "name": TOOL_NAME,
                    "description": (
                        "Record the structured contents of an invoice exactly as "
                        "printed on the document."
                    ),
                    "input_schema": invoice_input_schema(),
                }
            ],
            # Forcing the tool is what guarantees schema-shaped output.
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": encoded,
                            },
                        },
                        {"type": "text", "text": USER_PROMPT},
                    ],
                }
            ],
        }


def find_pdfs(directory: str | Path) -> list[Path]:
    """List the PDFs in a directory, sorted for reproducible batch runs."""
    directory = Path(directory)
    if not directory.is_dir():
        raise NotADirectoryError(f"Not a directory: {directory}")
    return sorted(p for p in directory.glob("*.pdf") if p.is_file())


def _invoice_from(message: Any) -> Invoice:
    """Pull the forced tool call out of a response and parse it."""
    for block in message.content:
        if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME:
            return Invoice.model_validate(block.input)
    raise ExtractionError(
        f"No {TOOL_NAME} tool_use block in response (stop_reason="
        f"{getattr(message, 'stop_reason', None)})"
    )


def _usage_from(message: Any) -> TokenUsage:
    """Read token counts off a response, tolerating a missing usage block."""
    usage = getattr(message, "usage", None)
    if usage is None:
        return TokenUsage()
    return TokenUsage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
