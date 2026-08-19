# tests/test_onboarding.py
"""The onboarding step runner and its crux step (account).

The regressions these lock down:

  * ``missing_keys`` reflects live DB state, so a satisfied step drops out and
    ``onboard_interactive`` never revisits it — no whole-wizard restart.
  * The operator account is created from the operator's own email, needs no
    mailbox, and a declined Legal Notice aborts rather than looping.

The mailbox and signature steps had the bulk of this file — an SMTP auth retry
that re-asked its own fields without rewinding, and a sign-off asked once per
never-asked box. Both steps left with the sending leg.
"""
from unittest.mock import patch

import pytest

from openoutreach.core import onboarding


# ── Runner idempotency ───────────────────────────────────────────

@pytest.mark.django_db
def test_missing_keys_starts_with_every_step():
    assert onboarding.missing_keys() == {"campaign", "llm", "bettercontact", "account"}


@pytest.mark.django_db
def test_satisfied_step_drops_out_of_missing_keys():
    from openoutreach.core.models import Campaign

    Campaign.objects.create(name="C", product_docs="p", campaign_target="o")
    assert "campaign" not in onboarding.missing_keys()


@pytest.mark.django_db
def test_onboard_interactive_skips_done_steps():
    """Every step is already done → no step's run() is invoked."""
    with patch.object(onboarding, "STEPS", [
        onboarding.Step("a", lambda: True, _boom),
        onboarding.Step("b", lambda: True, _boom),
    ]):
        onboarding.onboard_interactive()  # _boom never fires


def _boom():
    raise AssertionError("run() called for an already-satisfied step")


# ── Account step ─────────────────────────────────────────────────

@pytest.mark.django_db
def test_account_is_created_without_a_mailbox():
    """The step that used to weld the finder to the sender.

    ``_finalize_account`` opened with ``Mailbox.objects.first()`` and raised
    ``OnboardingCancelled`` on ``None``, so no install could complete onboarding
    without connecting a sending inbox — however little it wanted to send.
    """
    from django.contrib.auth.models import User

    from openoutreach.core.models import Campaign, SiteConfig

    Campaign.objects.create(name="C", product_docs="p", campaign_target="o")

    # wiz.text is asked twice: operator email, then country.
    with patch("openoutreach.core.onboarding.wiz.text", side_effect=["diego.r@posteo.eu", "US"]), \
         patch("openoutreach.core.onboarding.wiz.confirm", side_effect=[True, True]), \
         patch("openoutreach.core.newsletter.subscribe_to_newsletter") as sub:
        onboarding._run_account()

    user = User.objects.get(is_staff=True, is_active=True)
    # The handle derives from the address's local-part.
    assert user.email == "diego.r@posteo.eu"
    assert user.username == "diego_r"
    assert SiteConfig.load().country_code == "us"
    sub.assert_called_once_with("diego.r@posteo.eu")


@pytest.mark.django_db
def test_account_not_done_for_blank_email_user():
    """A staff user with a blank email (e.g. predating the address prompt) must NOT
    satisfy the account step — else the address prompt is skipped and BCC/newsletter
    have nowhere to go."""
    from django.contrib.auth.models import User

    User.objects.create(username="legacy", email="", is_staff=True, is_active=True)
    assert onboarding._account_done() is False

    User.objects.filter(username="legacy").update(email="me@posteo.eu")
    assert onboarding._account_done() is True


@pytest.mark.django_db
def test_account_shows_funding_notice_before_legal_gate():
    """The plain-language funding-behaviour notice (Legal Notice §4/§6) is shown
    during the account step, before the Legal Notice acceptance prompt."""
    from openoutreach.core.models import Campaign

    Campaign.objects.create(name="C", product_docs="p", campaign_target="o")

    with patch("openoutreach.core.onboarding.wiz.text", side_effect=["me@posteo.eu", "US"]), \
         patch("openoutreach.core.onboarding.wiz.confirm", side_effect=[True, True]), \
         patch("openoutreach.core.newsletter.subscribe_to_newsletter"), \
         patch("openoutreach.core.onboarding._show_information_notice") as notice, \
         patch("openoutreach.core.onboarding._require_legal") as legal:
        onboarding._run_account()

    notice.assert_called_once()  # the funding/contacts notice is rendered…
    legal.assert_called_once()   # …and the acceptance gate still runs after it


def test_legal_notice_sections_are_read_verbatim():
    """§4/§6 are lifted verbatim from the authoritative LEGAL_NOTICE.md, and
    neighbouring sections (§5, §7) don't leak into the excerpt."""
    assert onboarding.LEGAL_NOTICE_PATH.exists()
    text = onboarding._legal_notice_sections(4, 6)

    assert text.startswith("### 4. How the Project Is Funded")
    assert "### 6. Central Contacts Store" in text
    # Verbatim, not paraphrased — exact phrases (with markdown) from the notice survive.
    assert "**Freemium promotional campaign.**" in text
    assert "No name, headline, company, title, phone, or profile text is sent." in text
    # Boundaries: the sections between/around §4 and §6 are excluded.
    assert "### 5." not in text
    assert "### 7." not in text


def test_legal_notice_sections_fall_back_to_url_when_missing(tmp_path, monkeypatch):
    """A missing notice file degrades to the canonical link, never a crash."""
    monkeypatch.setattr(onboarding, "LEGAL_NOTICE_PATH", tmp_path / "nope.md")
    assert onboarding.LEGAL_NOTICE_URL in onboarding._legal_notice_sections(4, 6)


@pytest.mark.django_db
def test_declined_legal_aborts_without_creating_account():
    from django.contrib.auth.models import User

    from openoutreach.core.models import Campaign

    Campaign.objects.create(name="C", product_docs="p", campaign_target="o")

    # newsletter yes, then legal declined, then cancel the legal re-ask.
    with patch("openoutreach.core.onboarding.wiz.text", return_value="US"), \
         patch("openoutreach.core.onboarding.wiz.confirm", side_effect=[True, False, None]):
        with pytest.raises(SystemExit):
            onboarding._run_account()

    assert not User.objects.filter(is_staff=True).exists()
