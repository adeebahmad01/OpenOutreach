# openoutreach/core/onboarding.py
"""Onboarding as an ordered list of idempotent steps.

First principles
----------------
Onboarding is a **sequence of independent steps**. Each step knows two things:

  * ``is_done()`` — is this already satisfied? (reads the DB, never prompts)
  * ``run()``     — collect what's missing and **persist it immediately**

The runner executes only the steps whose ``is_done()`` is false, in order. Because
every step persists the moment it succeeds, a crash or Ctrl+C mid-onboarding
resumes exactly where it stopped, and a satisfied step is never revisited.

Why this shape kills the "onboarding keeps looping back" bug:

  * The **only** thing that decides ordering is ``is_done()``. Once a step's state
    is persisted it is done — the runner cannot land back on it.
  * A step's ``run()`` owns its **own** retry loop. A credential that fails
    verification re-asks *that step's* fields (with what you typed retained) —
    it never rewinds to an earlier step, and never restarts the whole wizard.
  * There is no end-of-wizard ``apply()`` that could half-fail and strand state:
    each step is its own commit point.

Cancellation is a single exception, not a return value threaded through every
caller: the wizard prompts return ``None`` on Ctrl+C, and ``_required()`` turns that
into ``OnboardingCancelled`` at one boundary.

Order: campaign → LLM (live-verified) → **BetterContact key** → account (your email
+ country + newsletter + legal, then the operator ``User``). The BetterContact key
is mandatory because it powers both discovery and enrichment — note the barrier is
an *account*, not a bill: the Lead Finder search is free and only the address lookup
costs a credit.

**Four steps, down from six.** The mailbox (seven fields, SMTP auth-checked) and the
signature came out with the sending leg. That is most of the install path, and the
reason it mattered is not the typing — it is that a lead finder was asking someone to
connect an inbox, and accept a sending liability, before it had shown them a lead.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

from openoutreach.core import onboarding_wizard as wiz

logger = logging.getLogger(__name__)

DEFAULT_CAMPAIGN_NAME = "Email Outreach"

_INTRO = """
  Welcome to OpenOutreach — a self-hosted lead finder that qualifies for you.
  Describe your product and who you sell to; it discovers matching people, judges
  each one against your ICP, and writes down why it chose them. Export the result
  as CSV and send with whatever you already use.

  Setup takes a few minutes. Have two things ready:
    • an LLM provider key — the agent qualifies your leads and writes the reasons
    • a BetterContact key — powers lead discovery (free) and email finding (paid)

  OpenOutreach is free; you pay only the providers above. Stop anytime — setup
  resumes where you left off.
"""

# The canonical Legal Notice — the single source of truth for how OpenOutreach
# behaves toward the operator's mailbox and the people it contacts. LEGAL_NOTICE.md
# lives at the repo root and ships in the image (COPY . ${APP_HOME}); onboarding
# reads §4/§6 from it at runtime rather than paraphrasing, so the two can't drift.
LEGAL_NOTICE_URL = "https://github.com/eracle/OpenOutreach/blob/main/LEGAL_NOTICE.md"
LEGAL_NOTICE_PATH = Path(__file__).resolve().parents[2] / "LEGAL_NOTICE.md"

# The funding/contacts behaviours the operator most needs to see before accepting.
_INFORMATION_NOTICE_SECTIONS = (4, 6)


def _legal_notice_sections(*numbers: int) -> str:
    """Return the given ``### <n>.`` sections of LEGAL_NOTICE.md as Markdown.

    Splits the notice on its section headings and keeps the requested ones, so the
    excerpt is lifted straight from the authoritative file. Falls back to a link if
    the file can't be read, so a missing notice never blocks onboarding.
    """
    try:
        notice = LEGAL_NOTICE_PATH.read_text(encoding="utf-8")
    except OSError:
        return f"The full Legal Notice is at <{LEGAL_NOTICE_URL}>."

    wanted = {str(n) for n in numbers}
    sections = notice.split("\n### ")  # headings are '### <n>. Title'
    kept = [s for s in sections if s.split(".", 1)[0].strip() in wanted]
    return "\n\n".join("### " + s.strip() for s in kept)


def _information_notice_markdown() -> str:
    """Compose the pre-acceptance notice as Markdown.

    Surfaces §4 (funding) and §6 (contacts store) verbatim from the authoritative
    LEGAL_NOTICE.md, framed by a lead-in and the canonical link — no paraphrase to
    maintain, so it tracks the notice automatically.
    """
    return (
        "## Before you accept: how OpenOutreach funds itself and shares contacts\n\n"
        "Two behaviours help sustain OpenOutreach and touch your mailbox and the "
        "people you contact. Both are governed by the Legal Notice; the two relevant "
        "sections are shown here verbatim.\n\n"
        f"{_legal_notice_sections(*_INFORMATION_NOTICE_SECTIONS)}\n\n"
        f"---\n\nFull text, your responsibilities, and how to opt out: <{LEGAL_NOTICE_URL}>"
    )


def _show_information_notice() -> None:
    """Render the funding/contacts notice to the terminal as Markdown."""
    from rich.console import Console
    from rich.markdown import Markdown

    Console().print(Markdown(_information_notice_markdown()))

_T = TypeVar("_T")


class OnboardingCancelled(SystemExit):
    """Raised when the operator cancels (Ctrl+C) a step that isn't yet satisfied."""

    def __init__(self) -> None:
        super().__init__("Onboarding cancelled.")


def _required(answer: _T | None) -> _T:
    """Unwrap a wizard answer, aborting onboarding when the operator cancelled.

    Wizard prompts return ``None`` on Ctrl+C; every mandatory answer is passed
    through here so cancellation raises once, instead of a ``None`` check after
    each prompt.
    """
    if answer is None:
        raise OnboardingCancelled
    return answer


def _say(message: str, style: str) -> None:
    """Print a styled status line (green ✓, red ✗, cyan progress)."""
    import questionary

    questionary.print(message, style=style)


# ---------------------------------------------------------------------------
# Step registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    """One onboarding step: a name, a done-check, and a runner."""

    key: str
    is_done: Callable[[], bool]
    run: Callable[[], None]


# ── Campaign: what you sell, and to whom ─────────────────────────

def _campaign_done() -> bool:
    from openoutreach.core.models import Campaign

    return Campaign.objects.exists()


def _run_campaign() -> None:
    from openoutreach.core.models import Campaign

    print(
        "\n  Campaign — describe what you sell and who you're selling to. This\n"
        "  trains the qualifier (which leads are a fit) and briefs the email agent\n"
        "  (how to pitch). Be specific — a vague target yields vague targeting."
    )
    Campaign.objects.create(
        name=DEFAULT_CAMPAIGN_NAME,
        product_docs=_required(wiz.multiline(
            "Product/service description — what it does, who it's for, the problem it solves "
            "(e.g. 'A self-hosted CI dashboard for small dev teams — replaces spreadsheet "
            "build-tracking; cuts flaky-test triage from hours to minutes')"
        )),
        campaign_target=_required(wiz.multiline(
            "Campaign target — who you're going after and the outcome you want "
            "(e.g. 'book demos with CTOs at Series-A SaaS')"
        )),
        booking_link=_required(wiz.text(
            "Booking link the email agent can share (e.g. https://cal.com/you) — optional",
            required=False,
        )),
    )
    logger.info("Campaign '%s' created.", DEFAULT_CAMPAIGN_NAME)


# ── LLM: the agent's brain (live-verified) ───────────────────────

_AI_MODEL_PROMPT = (
    "AI model — prefix the provider as 'provider:model' "
    "(e.g. anthropic:claude-sonnet-4-5-20250929, openai:gpt-4o, groq:llama-3.3-70b). "
    "Providers: openai, anthropic, google, groq, mistral, cohere, openai_compatible"
)


def _llm_done() -> bool:
    from openoutreach.core.models import SiteConfig

    cfg = SiteConfig.load()
    if not cfg.llm_api_key or not cfg.ai_model:
        return False
    # openai_compatible:* has no default endpoint — it needs an explicit base URL.
    if cfg.ai_model.startswith("openai_compatible:") and not cfg.llm_api_base:
        return False
    return True


def _run_llm() -> None:
    from openoutreach.core.llm import verify_llm_credentials

    print("\n  LLM — the model that qualifies leads and writes your emails.")
    model = base = ""
    while True:
        model = _required(wiz.text(_AI_MODEL_PROMPT, default=model))
        key = _required(wiz.text("API key for that provider (e.g. sk-...)", secret=True))
        base = _required(wiz.text(
            "API base URL (only for openai_compatible:* — OpenRouter / Together / Ollama / vLLM)",
            default=base, required=False,
        ))

        _say("  Verifying LLM credentials…", "fg:cyan")
        error = verify_llm_credentials(model, key, base)
        if error is None:
            _save_llm(model, key, base)
            _say("  ✓ LLM credentials OK.", "fg:green")
            return
        _say(f"  ✗ {error}", "fg:red")


def _save_llm(model: str, key: str, base: str) -> None:
    from openoutreach.core.models import SiteConfig

    cfg = SiteConfig.load()
    cfg.ai_model, cfg.llm_api_key, cfg.llm_api_base = model, key, base
    cfg.save()
    logger.info("LLM config saved.")


# The mailbox and signature steps used to sit here, between the LLM and the finder
# key: connect a sending inbox (seven fields, SMTP auth-checked), then write the
# sign-off appended to every email it sends. Both are gone with the sending leg.
#
# They were also the most expensive thing on the install path, and the expense was
# not the typing. Behind the mailbox step stood `LEGAL_NOTICE.md` §4 — the disclosure
# that the tool would send the maintainer's promotional campaign from your mailbox,
# under your identity. Asking someone to accept a sending liability before they have
# seen a single lead was the wrong trade for a tool whose output is a CSV.


# ── BetterContact: powers discovery + enrichment (mandatory) ──────

def _bettercontact_done() -> bool:
    from openoutreach.enrichment import bettercontact

    return bettercontact.is_configured()


def _run_bettercontact() -> None:
    from openoutreach.core.models import SiteConfig

    print(
        "\n  BetterContact — the one paid step, and it does double duty: lead\n"
        "  DISCOVERY (ICP search — billed nothing) and email FINDING (one credit\n"
        "  per verified work email, top-ranked leads only). Free tier is ~50\n"
        "  lookups to start.\n\n"
        "  Get a key (affiliate link — supports OpenOutreach, no markup to you):\n"
        "  https://bettercontact.rocks?fpr=openoutreach\n"
        "  Then copy your API key from the dashboard and paste it below."
    )
    cfg = SiteConfig.load()
    cfg.bettercontact_api_key = _required(wiz.text("BetterContact API key", secret=True))
    cfg.save()
    _say("  ✓ BetterContact key saved.", "fg:green")


# ── Account: country + newsletter + information notice + legal, then the operator User ─

def _account_done() -> bool:
    """Done only when an operator exists *with a non-blank email* — the operator's
    own inbox (contacts key + newsletter target, and a BCC copy of every send on
    their own campaigns — see ``emails.sender.operator_bcc``). Requiring a real
    email (not merely 'a staff user exists') stops a legacy blank-email account
    from short-circuiting the address prompt."""
    from django.contrib.auth.models import User

    return User.objects.filter(is_active=True, is_staff=True).exclude(email="").exists()


def _run_account() -> None:
    """Collect jurisdiction, show the funding-behaviour notice, gate on the Legal
    Notice, then create the operator.

    Nothing is persisted until every answer is in and the Legal Notice is
    accepted, so a declined/cancelled step leaves no partial state behind.
    """
    from openoutreach.core.geo import is_gdpr_protected

    # The operator's own inbox — the contacts-give-back key and (if opted in) the
    # newsletter target. It used to need saying that this was *not* the sending
    # mailbox; with no sending mailbox there is one address again.
    operator_email = _required(wiz.text(
        "Your email address. We'll send product updates here if you opt in below.",
        validate=_looks_like_email,
    )).strip()

    country = _required(wiz.text(
        "Your country (ISO 3166 alpha-2, e.g. US, GB, DE) — sets your active-hours "
        "timezone and email-jurisdiction defaults",
        validate=_looks_like_country,
    )).lower()

    # Newsletter opt-in defaults OFF in GDPR/opt-in jurisdictions (no consent by
    # silence), ON elsewhere. An explicit yes is lawful consent anywhere.
    newsletter = _required(wiz.confirm(
        "Subscribe to the OpenOutreach newsletter?",
        default=not is_gdpr_protected(country),
    ))
    _show_information_notice()
    _require_legal()
    _finalize_account(operator_email, country, newsletter)


def _looks_like_country(value: str) -> bool | str:
    """Validate an ISO 3166-1 alpha-2 code against the same table active-hours uses.

    ``pytz.country_timezones`` is the country→zone authority ``timezone_for_country``
    reads; validating against it rejects made-up codes (XX, ZZ) and guarantees the
    accepted code resolves a timezone later.
    """
    import pytz

    code = value.strip()
    if len(code) == 2 and code.upper() in pytz.country_timezones:
        return True
    return "Enter a valid ISO 3166 alpha-2 country code (e.g. US, GB, DE)."


def _looks_like_email(value: str) -> bool | str:
    value = value.strip()
    # Minimal shape check — a single @ with non-empty local part and a dotted domain.
    local, _, domain = value.partition("@")
    if local and "." in domain and not domain.startswith(".") and not domain.endswith("."):
        return True
    return "Enter a valid email address (e.g. you@example.com)."


def _require_legal() -> None:
    """Gate onboarding on Legal Notice acceptance; re-ask a decline, abort on cancel."""
    while True:
        accepted = wiz.confirm(
            f"Do you accept the Legal Notice? ({LEGAL_NOTICE_URL})",
            default=False,
        )
        if accepted is None:  # Ctrl+C
            raise OnboardingCancelled
        if accepted:
            return
        _say("  You must accept the Legal Notice to use OpenOutreach.", "fg:red")


def _finalize_account(operator_email: str, country: str, newsletter: bool) -> None:
    """Persist country, create the operator ``User`` from their own email, subscribe once.

    ``operator_email`` is the human's inbox — the contacts-store key and the
    newsletter target. It used to also be distinguished from a mailbox
    ``from_address`` (the sending identity) and used as the BCC target on the
    operator's own sends; with no sending leg there is one email address again.

    **This no longer requires a mailbox.** It used to open with
    ``Mailbox.objects.first()`` and raise ``OnboardingCancelled`` on ``None``, which
    made the whole account step unsatisfiable without a connected inbox — one of the
    two places the finder was welded to the sender.
    """
    from openoutreach.core.models import Campaign, SiteConfig
    from openoutreach.core.newsletter import subscribe_to_newsletter

    cfg = SiteConfig.load()
    cfg.country_code = country
    cfg.save(update_fields=["country_code"])

    user = _create_operator(Campaign.objects.first(), operator_email)
    if newsletter:
        subscribe_to_newsletter(operator_email)
    logger.info("Operator account '%s' created (email=%s).", user.username, operator_email)


def _create_operator(campaign, email: str):
    """Create the operator Django ``User`` from their email (the human's own inbox)."""
    from django.contrib.auth.models import User

    handle = email.split("@")[0].lower().replace(".", "_").replace("+", "_")
    user, created = User.objects.get_or_create(
        username=handle,
        defaults={"is_staff": True, "is_active": True, "email": email},
    )
    if created:
        user.set_unusable_password()
        user.save()
    if campaign is not None:
        campaign.users.add(user)
    return user


# ---------------------------------------------------------------------------
# The ordered pipeline
# ---------------------------------------------------------------------------

STEPS: list[Step] = [
    Step("campaign", _campaign_done, _run_campaign),
    Step("llm", _llm_done, _run_llm),
    Step("bettercontact", _bettercontact_done, _run_bettercontact),
    Step("account", _account_done, _run_account),
]


def missing_keys() -> set[str]:
    """Return the keys of steps that still need attention (empty ⇒ fully onboarded)."""
    return {step.key for step in STEPS if not step.is_done()}


def onboard_interactive() -> None:
    """Run each unsatisfied step in order, persisting as it goes.

    Idempotent: an already-satisfied step is skipped, so a partial onboarding
    resumes where it left off. Raises ``OnboardingCancelled`` (a ``SystemExit``)
    if the operator cancels a step that isn't yet satisfiable.
    """
    if all(step.is_done() for step in STEPS):
        return  # nothing to do — don't print the intro on a fully-onboarded run

    from openoutreach.core.logging import print_banner

    print_banner()
    print(_INTRO)
    for step in STEPS:
        if not step.is_done():
            step.run()
