"""Settings resolution. `load=False` keeps these tests off any real `.env`."""

from __future__ import annotations

import pytest

from invoice_extractor.config import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MODEL,
    ConfigError,
    Settings,
)


def test_defaults_apply_when_nothing_is_set(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "MODEL", "MAX_RETRIES", "MAX_TOKENS"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env(load=False)

    assert settings.api_key is None
    assert settings.model == DEFAULT_MODEL
    assert settings.max_retries == DEFAULT_MAX_RETRIES


def test_environment_values_are_read(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("MODEL", "claude-sonnet-5")
    monkeypatch.setenv("MAX_RETRIES", "5")
    monkeypatch.setenv("MAX_TOKENS", "8192")

    settings = Settings.from_env(load=False)

    assert settings.api_key == "sk-test"
    assert settings.model == "claude-sonnet-5"
    assert settings.max_retries == 5
    assert settings.max_tokens == 8192


@pytest.mark.parametrize("raw", ["", "   ", "not-a-number"])
def test_unusable_numeric_values_fall_back_to_defaults(monkeypatch, raw):
    """A typo in `.env` should not crash a batch run hours into the night."""
    monkeypatch.setenv("MAX_RETRIES", raw)

    assert Settings.from_env(load=False).max_retries == DEFAULT_MAX_RETRIES


def test_empty_api_key_is_treated_as_absent(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    assert Settings.from_env(load=False).api_key is None


def test_require_api_key_explains_how_to_set_it():
    with pytest.raises(ConfigError, match=r"\.env"):
        Settings(api_key=None).require_api_key()


def test_require_api_key_returns_the_key():
    assert Settings(api_key="sk-test").require_api_key() == "sk-test"
