# tests/test_rundaemon.py
"""Boot: environment first, wizard only if a human is there to answer.

The regression this locks down is the one an agent-driven install hits first —
the daemon used to die on a missing TTY with a message that named a mailbox
(gone with the sending leg) and never said which variables to set.
"""
import io
from unittest.mock import patch

import pytest

from openoutreach.core.management.commands.rundaemon import Command

FULL_ENV = {
    "OPENOUTREACH_PRODUCT_DESCRIPTION": "A self-hosted CI dashboard for small dev teams",
    "OPENOUTREACH_CAMPAIGN_TARGET": "book demos with CTOs at Series-A SaaS",
    "OPENOUTREACH_AI_MODEL": "anthropic:claude-sonnet-4-5-20250929",
    "OPENOUTREACH_LLM_API_KEY": "sk-test",
    "OPENOUTREACH_BETTERCONTACT_API_KEY": "bc-test",
    "OPENOUTREACH_OPERATOR_EMAIL": "me@posteo.eu",
    "OPENOUTREACH_COUNTRY": "US",
    "OPENOUTREACH_ACCEPT_LEGAL_NOTICE": "true",
}


@pytest.fixture
def headless(monkeypatch):
    """No TTY, and none of the developer's own onboarding variables."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    for name in FULL_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def command():
    cmd = Command()
    cmd.stderr = io.StringIO()
    return cmd


@pytest.mark.django_db
def test_headless_and_unconfigured_names_the_variables(headless, command):
    with pytest.raises(SystemExit) as exc:
        command._ensure_onboarded()

    assert exc.value.code == 1
    message = command.stderr.getvalue()
    assert "OPENOUTREACH_PRODUCT_DESCRIPTION" in message
    assert "OPENOUTREACH_BETTERCONTACT_API_KEY" in message
    assert "OPENOUTREACH_ACCEPT_LEGAL_NOTICE" in message
    assert "mailbox" not in message.lower()


@pytest.mark.django_db
def test_headless_and_fully_configured_runs_without_a_prompt(headless, command, monkeypatch):
    for name, value in FULL_ENV.items():
        monkeypatch.setenv(name, value)

    with patch("openoutreach.core.llm.verify_llm_credentials", return_value=None), \
         patch("openoutreach.core.newsletter.subscribe_to_newsletter"), \
         patch("openoutreach.core.onboarding.onboard_interactive",
               side_effect=AssertionError("wizard ran without a TTY")):
        command._ensure_onboarded()  # returns, having onboarded from the environment

    from openoutreach.core.onboarding import missing_keys
    assert missing_keys() == set()


@pytest.mark.django_db
def test_a_tty_still_gets_the_wizard(command, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    for name in FULL_ENV:
        monkeypatch.delenv(name, raising=False)

    with patch("openoutreach.core.onboarding.onboard_interactive") as wizard:
        command._ensure_onboarded()

    wizard.assert_called_once()
