"""Runtime configuration, loaded from the environment / `.env`."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_TOKENS = 4096


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


@dataclass(frozen=True)
class Settings:
    """Everything the extractor needs to talk to the API."""

    api_key: str | None = None
    model: str = DEFAULT_MODEL
    max_retries: int = DEFAULT_MAX_RETRIES
    max_tokens: int = DEFAULT_MAX_TOKENS

    @classmethod
    def from_env(cls, *, load: bool = True) -> Settings:
        """Read settings from the environment, loading `.env` first if present.

        Existing environment variables win over `.env` values, which is what you
        want for CI and for one-off overrides on the command line.
        """
        if load:
            load_dotenv(override=False)

        return cls(
            api_key=os.getenv("ANTHROPIC_API_KEY") or None,
            model=os.getenv("MODEL") or DEFAULT_MODEL,
            max_retries=_int_env("MAX_RETRIES", DEFAULT_MAX_RETRIES),
            max_tokens=_int_env("MAX_TOKENS", DEFAULT_MAX_TOKENS),
        )

    def require_api_key(self) -> str:
        """Return the API key, or explain how to set it."""
        if not self.api_key:
            raise ConfigError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill "
                "it in, or export the variable in your shell."
            )
        return self.api_key


def _int_env(name: str, default: int) -> int:
    """Read an int from the environment, falling back on anything unparseable."""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default
