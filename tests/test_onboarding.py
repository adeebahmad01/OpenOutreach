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
def test_account_gates_on_legal_notice():
    """The account step runs the Legal Notice acceptance gate before finalizing —
    no rendered §4/§6 excerpt any more, just the link the gate's own prompt carries."""
    from openoutreach.core.models import Campaign

    Campaign.objects.create(name="C", product_docs="p", campaign_target="o")

    with patch("openoutreach.core.onboarding.wiz.text", side_effect=["me@posteo.eu", "US"]), \
         patch("openoutreach.core.onboarding.wiz.confirm", side_effect=[True, True]), \
         patch("openoutreach.core.newsletter.subscribe_to_newsletter"), \
         patch("openoutreach.core.onboarding._require_legal") as legal:
        onboarding._run_account()

    legal.assert_called_once()


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


# ── The environment path — the one an agent has ──────────────────

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
def env(monkeypatch):
    """Set onboarding variables, clearing any the developer's own shell exports."""
    def _set(**values):
        for name in list(FULL_ENV):
            monkeypatch.delenv(name, raising=False)
        for name, value in values.items():
            monkeypatch.setenv(name, value)
    return _set


@pytest.fixture
def llm_ok():
    """The LLM step verifies live; accept the key without a network call."""
    with patch("openoutreach.core.llm.verify_llm_credentials", return_value=None) as verify:
        yield verify


@pytest.mark.django_db
def test_full_environment_completes_onboarding_without_a_prompt(env, llm_ok):
    """The claim the card makes: a configured environment needs no TTY and no wizard."""
    env(**FULL_ENV)
    with patch("openoutreach.core.newsletter.subscribe_to_newsletter"), \
         patch("openoutreach.core.onboarding.wiz.text", side_effect=AssertionError("prompted")):
        filled = onboarding.hydrate_from_env()

    assert filled == {"campaign", "llm", "bettercontact", "account"}
    assert onboarding.missing_keys() == set()


@pytest.mark.django_db
def test_partial_environment_leaves_the_rest_missing(env, llm_ok):
    """A step with only some of its fields set is not half-applied — it is skipped."""
    env(
        OPENOUTREACH_BETTERCONTACT_API_KEY="bc-test",
        OPENOUTREACH_PRODUCT_DESCRIPTION="only half of the campaign step",
    )
    assert onboarding.hydrate_from_env() == {"bettercontact"}
    assert onboarding.missing_keys() == {"campaign", "llm", "account"}

    from openoutreach.core.models import Campaign
    assert not Campaign.objects.exists()


@pytest.mark.django_db
def test_missing_variables_are_named(env, llm_ok):
    """What the headless exit prints: the variables that would have satisfied it."""
    env(OPENOUTREACH_BETTERCONTACT_API_KEY="bc-test")
    onboarding.hydrate_from_env()

    help_text = onboarding.env_help()
    assert "OPENOUTREACH_PRODUCT_DESCRIPTION" in help_text
    assert "OPENOUTREACH_ACCEPT_LEGAL_NOTICE" in help_text
    assert "BETTERCONTACT" not in help_text  # satisfied — not asked for again
    assert "mailbox" not in help_text.lower()  # the message that named a dead concept


@pytest.mark.django_db
def test_legal_acceptance_is_never_inferred(env, llm_ok):
    """Email and country present, acceptance absent → the account step stays missing."""
    env(
        OPENOUTREACH_OPERATOR_EMAIL="me@posteo.eu",
        OPENOUTREACH_COUNTRY="US",
    )
    assert onboarding.hydrate_from_env() == set()
    assert "account" in onboarding.missing_keys()


@pytest.mark.django_db
def test_newsletter_defaults_off_when_unset(env, llm_ok):
    """Silence in a config file is not consent — not even outside the EEA."""
    env(**FULL_ENV)
    with patch("openoutreach.core.newsletter.subscribe_to_newsletter") as subscribe:
        onboarding.hydrate_from_env()
    subscribe.assert_not_called()


@pytest.mark.django_db
def test_bad_country_stops_rather_than_asking_for_it_again(env, llm_ok):
    """A set-but-invalid value is a different thing from an absent one."""
    env(**{**FULL_ENV, "OPENOUTREACH_COUNTRY": "XX"})
    with pytest.raises(onboarding.OnboardingEnvError) as exc:
        onboarding.hydrate_from_env()
    assert "OPENOUTREACH_COUNTRY" in str(exc.value)


@pytest.mark.django_db
def test_unverifiable_llm_key_stops_at_boot(env):
    """Headless there is nobody to re-ask, so a bad key fails here, not mid-run."""
    env(**FULL_ENV)
    with patch("openoutreach.core.llm.verify_llm_credentials", return_value="401 Unauthorized"):
        with pytest.raises(onboarding.OnboardingEnvError) as exc:
            onboarding.hydrate_from_env()
    assert "401 Unauthorized" in str(exc.value)


def test_unparseable_boolean_is_rejected(env):
    env(OPENOUTREACH_NEWSLETTER="maybe")
    with pytest.raises(onboarding.OnboardingEnvError):
        onboarding._env_bool("NEWSLETTER")
