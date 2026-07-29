"""Tests for the LLM model factory and onboarding credential verification."""
import pytest

from openoutreach.core import llm


def test_build_llm_model_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        llm.build_llm_model("nope:some-model", "key")


def test_verify_llm_credentials_ok(monkeypatch):
    monkeypatch.setattr(llm, "_ping_model", lambda *a, **k: None)
    assert llm.verify_llm_credentials("anthropic:claude", "sk-key") is None


def test_verify_llm_credentials_reports_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("invalid api key")

    monkeypatch.setattr(llm, "_ping_model", boom)
    assert llm.verify_llm_credentials("anthropic:claude", "bad") == "invalid api key"


def test_every_provider_sdk_is_silenced_at_debug():
    """A new provider must arrive with its SDK on the silenced list.

    Otherwise `--log-level debug` becomes unreadable the first time someone switches
    models: every LLM SDK dumps the whole request body at DEBUG (`Request options:
    {'method': 'post', ...}`, one screen per call), which buries the discovery walk's
    reasoning that the flag exists to show. This failed for `anthropic` in the wild —
    only `openai` was listed.
    """
    from openoutreach.core.llm import _PROVIDER_BUILDERS
    from openoutreach.core.logging import SILENCED_LOGGERS

    # The SDK package each provider actually imports, where it differs from the key.
    sdk_for = {"mistral": "mistralai", "openai_compatible": "openai"}
    missing = [
        provider for provider in _PROVIDER_BUILDERS
        if sdk_for.get(provider, provider) not in SILENCED_LOGGERS
    ]
    assert not missing, f"providers whose SDK logger is not silenced: {missing}"
