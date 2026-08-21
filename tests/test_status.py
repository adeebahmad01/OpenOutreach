# tests/test_status.py
"""``status`` — the verb an agent asks instead of tailing a log.

Two things carry the weight here. **Nothing may be reported as an empty result**: a
key that was rejected and a run that has simply found nothing yet must never render
the same, which is why the balance reports *why* it is unknown instead of falling
back to zero. And **the next action is arithmetic, not adjectives** — it names counts
and a URL an agent can relay, and it never asks for money before value exists.
"""
import json
from unittest.mock import patch

import pytest

from openoutreach.core import status as status_module
from openoutreach.core.errors import ErrorType
from openoutreach.core.management.commands.status import render
from openoutreach.crm.models import DealState
from tests.factories import DealFactory, LeadFactory


@pytest.fixture
def configured():
    """Onboarding complete, so the tests below are about the pipeline, not setup."""
    with patch("openoutreach.core.onboarding.missing_env_keys", return_value={}):
        yield


@pytest.fixture
def balance():
    """Control the provider balance without a network call."""
    def _set(value=None, error=None, error_type=ErrorType.PROVIDER_UNAVAILABLE):
        if error is not None:
            from openoutreach.enrichment.bettercontact import BetterContactUnavailable
            return patch("openoutreach.enrichment.bettercontact.credit_balance",
                         side_effect=BetterContactUnavailable(error, error_type))
        return patch("openoutreach.enrichment.bettercontact.credit_balance", return_value=value)
    return _set


@pytest.fixture
def has_key():
    with patch("openoutreach.enrichment.bettercontact.is_configured", return_value=True):
        yield


# ── the counts ───────────────────────────────────────────────────

@pytest.mark.django_db
def test_counts_the_deliverable_the_way_the_export_writes_it(campaign, configured, has_key, balance):
    """``exportable`` must agree with the CSV's rows, or the number is a different number."""
    DealFactory(campaign=campaign, lead=LeadFactory(), state=DealState.RESOLVED, reason="fits")
    DealFactory(campaign=campaign, lead=LeadFactory(), state=DealState.QUALIFIED, reason="fits")
    DealFactory(campaign=campaign, lead=LeadFactory(), state=DealState.FAILED, reason="no fit")

    with balance(value=40):
        document = status_module.build_status()

    from openoutreach.core.export import lead_records
    assert document["totals"]["exportable"] == sum(1 for _ in lead_records(campaign)) == 2
    assert document["totals"]["rejected"] == 1


@pytest.mark.django_db
def test_exportable_separates_the_rows_that_carry_an_address(
    campaign, configured, has_key, balance
):
    """An exportable row is not necessarily a mailable one, and the count says so."""
    DealFactory(campaign=campaign, lead=LeadFactory(email="ada@acme.com"),
                state=DealState.RESOLVED, reason="fits")
    DealFactory(campaign=campaign, lead=LeadFactory(email=""),
                state=DealState.QUALIFIED, reason="fits")

    with balance(value=40):
        totals = status_module.build_status()["totals"]

    assert totals["exportable"] == 2
    assert totals["exportable_with_email"] == 1
    assert totals["exportable_without_email"] == 1


# ── the balance, and the difference between unknown and zero ─────

@pytest.mark.django_db
def test_a_rejected_key_is_not_a_balance_of_zero(campaign, configured, has_key, balance):
    with balance(error="BetterContact rejected the API key (401)",
                 error_type=ErrorType.PROVIDER_AUTH):
        document = status_module.build_status()

    assert document["credits"]["balance"] is None
    assert document["credits"]["error"] == ErrorType.PROVIDER_AUTH
    assert any(item["type"] == ErrorType.PROVIDER_AUTH for item in document["blocked"])


@pytest.mark.django_db
def test_an_unreachable_provider_is_its_own_answer(campaign, configured, has_key, balance):
    with balance(error="BetterContact unreachable: timed out"):
        document = status_module.build_status()

    assert document["credits"]["error"] == ErrorType.PROVIDER_UNAVAILABLE


@pytest.mark.django_db
def test_no_key_reports_no_credential(campaign, configured):
    document = status_module.build_status()

    assert document["credits"]["error"] == ErrorType.NO_CREDENTIAL
    assert any(item["type"] == ErrorType.NO_CREDENTIAL for item in document["blocked"])


@pytest.mark.django_db
def test_zero_credits_with_leads_waiting_is_blocked(campaign, configured, has_key, balance):
    DealFactory(campaign=campaign, lead=LeadFactory(), state=DealState.READY_TO_FIND_EMAIL)

    with balance(value=0):
        document = status_module.build_status()

    blocked = [item for item in document["blocked"]
               if item["type"] == ErrorType.PROVIDER_OUT_OF_CREDITS]
    assert blocked and "1 ranked lead(s) waiting, 0 credits left" in blocked[0]["message"]


# ── the next action ──────────────────────────────────────────────

@pytest.mark.django_db
def test_next_action_is_onboarding_when_setup_is_incomplete(campaign):
    document = status_module.build_status()

    action = document["next_action"]
    assert action["type"] == "finish_onboarding"
    assert "OPENOUTREACH_BETTERCONTACT_API_KEY" in action["variables"]


@pytest.mark.django_db
def test_nothing_is_asked_of_a_run_that_has_qualified_nobody(campaign, configured, has_key, balance):
    """Never before value: an empty pipeline at zero credits is asked for nothing."""
    with balance(value=0):
        document = status_module.build_status()

    assert document["next_action"]["type"] == "wait"


@pytest.mark.django_db
def test_the_file_is_the_next_action_once_credits_are_not_the_blocker(
    campaign, configured, has_key, balance
):
    """The rows are already on disk, so the action is a *read* — and it names the file."""
    from openoutreach.core.export import campaign_csv_path

    DealFactory(campaign=campaign, lead=LeadFactory(), state=DealState.RESOLVED, reason="fits")

    with balance(value=40):
        document = status_module.build_status()

    action = document["next_action"]
    assert action["type"] == "read_leads"
    assert action["leads"] == 1
    assert action["path"] == str(campaign_csv_path(campaign)) == document["export"]["path"]
    assert "command" not in action


@pytest.mark.django_db
def test_the_export_path_is_unknown_until_there_is_something_in_it(
    campaign, configured, has_key, balance
):
    """No rows, no file — and `path: null` says that rather than naming a file that
    is not there."""
    with balance(value=40):
        assert status_module.build_status()["export"]["path"] is None


@pytest.mark.django_db
def test_credit_ask_carries_the_count_and_the_attributed_url(campaign, configured, has_key, balance):
    DealFactory(campaign=campaign, lead=LeadFactory(), state=DealState.READY_TO_FIND_EMAIL)

    with balance(value=0):
        action = status_module.build_status()["next_action"]

    assert action["type"] == "add_credits"
    assert action["leads"] == 1
    # Attribution is won at signup, so every path we show carries it.
    assert action["url"].endswith("?fpr=openoutreach")


@pytest.mark.django_db
def test_a_running_campaign_with_nothing_yet_says_so(campaign, configured, has_key, balance):
    with balance(value=40):
        action = status_module.build_status()["next_action"]

    assert action["type"] == "wait"


# ── rendering ────────────────────────────────────────────────────

@pytest.mark.django_db
def test_json_is_one_object_and_nothing_else(campaign, configured, has_key, balance, capsys):
    from django.core.management import call_command

    with balance(value=40):
        call_command("status", "--json")

    document = json.loads(capsys.readouterr().out)  # would raise on any stray line
    assert set(document) == {
        "onboarding", "campaigns", "totals", "credits", "blocked", "export", "next_action",
    }


@pytest.mark.django_db
def test_human_summary_reports_the_balance_and_the_next_action(campaign, configured, has_key, balance):
    DealFactory(campaign=campaign, lead=LeadFactory(), state=DealState.RESOLVED, reason="fits")

    with balance(value=38):
        text = render(status_module.build_status())

    assert "Credits: 38 left." in text
    assert "1 exportable" in text
    assert "Next: 1 qualified lead(s) are already written to " in text
