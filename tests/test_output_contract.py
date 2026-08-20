# tests/test_output_contract.py
"""The CLI's output contract — the part a program depends on.

Three promises: **stdout is result-only** (so a redirect yields data and nothing
else), **an expected failure is one typed line** on stderr with a non-zero exit (so
an agent branches instead of parsing prose), and **a 429 is backed off rather than
retried at speed** — their docs warn that a client which keeps firing can get the
account blocked.
"""
import io
import logging
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

from openoutreach.core.errors import ErrorType, OpenOutreachError, format_error
from openoutreach.core.management.base import OpenOutreachCommand
from openoutreach.enrichment import bettercontact
from openoutreach.enrichment.bettercontact import BetterContactUnavailable


# ── stdout is result-only ────────────────────────────────────────

def test_logs_go_to_stderr_not_stdout(capsys):
    """A log line on stdout would corrupt a redirected CSV or a piped JSON document."""
    from openoutreach.core.logging import configure_logging

    try:
        configure_logging(level=logging.INFO)
        logging.getLogger("openoutreach.test").info("a log line")
    finally:
        logging.getLogger().handlers.clear()

    captured = capsys.readouterr()
    assert "a log line" in captured.err
    assert captured.out == ""


def test_the_banner_is_decoration_and_shares_stderr(capsys):
    from openoutreach.core.logging import print_banner

    print_banner()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "___" in captured.err  # the ASCII banner


# ── one typed error line, non-zero exit ──────────────────────────

def test_error_line_has_the_contract_shape():
    assert format_error("provider_auth", "boom") == "error: provider_auth: boom"
    assert str(OpenOutreachError(ErrorType.PROVIDER_AUTH, "boom")) == \
        "error: provider_auth: boom"


def test_command_renders_the_error_line_and_exits_non_zero(capsys):
    """No traceback: a rejected key is an answer, not a bug."""
    class Failing(OpenOutreachCommand):
        def handle(self, *args, **options):
            raise OpenOutreachError(ErrorType.PROVIDER_AUTH, "the key was rejected")

    with pytest.raises(SystemExit) as exc:
        Failing().run_from_argv(["openoutreach", "failing"])

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "error: provider_auth: the key was rejected"
    assert captured.out == ""


def test_an_unexpected_exception_still_raises():
    """Only *expected* failures are flattened; a bug keeps its traceback."""
    class Buggy(OpenOutreachCommand):
        def handle(self, *args, **options):
            raise ZeroDivisionError("a real bug")

    with pytest.raises(ZeroDivisionError):
        Buggy().run_from_argv(["openoutreach", "buggy"])


# ── the provider's refusals are three different things ───────────

def _session_answering(status_code, body=None, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body or {}
    resp.headers = headers or {}
    resp.raise_for_status.side_effect = None
    session = MagicMock()
    session.__enter__.return_value = session
    session.request.return_value = resp
    return session


@pytest.fixture
def keyed(db):
    from openoutreach.core.models import SiteConfig
    cfg = SiteConfig.load()
    cfg.bettercontact_api_key = "secret"
    cfg.save()
    return cfg


@pytest.mark.django_db
@pytest.mark.parametrize("status_code,expected", [
    (401, ErrorType.PROVIDER_AUTH),
    (402, ErrorType.PROVIDER_OUT_OF_CREDITS),
])
def test_each_refusal_carries_its_own_type(keyed, status_code, expected):
    with patch.object(bettercontact, "_session",
                      return_value=_session_answering(status_code)):
        with pytest.raises(BetterContactUnavailable) as exc:
            bettercontact.credit_balance()

    assert exc.value.error_type == expected


@pytest.mark.django_db
def test_an_exhausted_429_backoff_is_reported_as_rate_limited(keyed):
    """The adapter retries 429 with backoff; when it gives up, the type says why."""
    session = MagicMock()
    session.__enter__.return_value = session
    session.request.side_effect = requests.exceptions.RetryError("too many 429s")

    with patch.object(bettercontact, "_session", return_value=session):
        with pytest.raises(BetterContactUnavailable) as exc:
            bettercontact.credit_balance()

    assert exc.value.error_type == ErrorType.PROVIDER_RATE_LIMITED


def test_the_session_backs_off_on_429_and_only_on_429():
    """Retrying a 401 or a 402 would just be noise — those are final answers."""
    retry = bettercontact._session("k").get_adapter("https://x").max_retries

    assert retry.status_forcelist == (429,)
    assert retry.total == bettercontact._RATE_LIMIT_ATTEMPTS
    assert retry.backoff_factor >= 5          # seconds, doubling
    assert retry.respect_retry_after_header   # the provider's own number wins
