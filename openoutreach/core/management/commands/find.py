"""Find leads until a goal is met, then print the campaign and exit.

    openoutreach find 1                   # one qualified lead, with its reason
    openoutreach find 10                  # ten more (free — discovery and qualification)
    openoutreach find 10 emails           # ten more carrying an address (≤10 credits)
    openoutreach find 0                   # no work; print what the campaign already has
    openoutreach find 10 --open           # ...and open each new profile in the browser

**The unit is a noun, not a flag**, and that is a spend decision rather than a style one:
the provider bills one credit per verified hit, so ``find 10 emails`` is capped at ten
credits by construction. The number typed is the budget, in the same unit as the invoice.

**stdout carries the whole campaign, not just this run's rows**, which is what makes
``> leads.csv`` correct by construction: the newest file supersedes every earlier one, and
a lead whose address resolved since last time comes back with it filled in. It is one file
to overwrite, not a batch per run. ``--new`` narrows to what this run produced, for a
caller reading stdout into a context window rather than into a file.

Exit 0 means the goal was met, and nothing else. Anything short prints its rows anyway and
exits non-zero with one ``error: <type>: <message>`` line — the code says how much you
got, the type says why it stopped.
"""
from __future__ import annotations

import json
import logging
import sys
import webbrowser

from django.core.management import call_command

from openoutreach.core.errors import ErrorType, OpenOutreachError
from openoutreach.core.export import lead_records, write_csv
from openoutreach.core.job import EMAILS, LEADS, UNITS, Goal, JobResult, run_job
from openoutreach.core.management.base import OpenOutreachCommand

logger = logging.getLogger(__name__)


class Command(OpenOutreachCommand):
    help = "Find leads until the goal is met, then print the campaign as CSV."

    # The verb that migrates. Every other one is entitled to find a schema already there.
    requires_database = False

    def add_arguments(self, parser):
        parser.add_argument("count", type=int, help="How many more to find. 0 prints what is there.")
        parser.add_argument("unit", nargs="?", default=LEADS, choices=UNITS,
                            help=f"{LEADS} (default) or {EMAILS} — one credit per address.")
        parser.add_argument("--campaign", help="Campaign name. Required only if there are several.")
        parser.add_argument("--new", action="store_true", dest="only_new",
                            help="Print only the rows this run produced.")
        parser.add_argument("--json", action="store_true", dest="as_json",
                            help="Emit one JSON object: the goal, the outcome, and the rows.")
        parser.add_argument("--open", action="store_true", dest="open_profiles",
                            help="Open each new lead's profile in your browser as it lands.")
        parser.add_argument(
            "--log-level",
            choices=("debug", "info", "warning", "error"),
            help="Log verbosity (default: info). `debug` shows the discovery walk's "
                 "reasoning — the frontier, each node's counts and draw, why a node "
                 "was expanded or not, and the provider's raw answer.",
        )

    def handle(self, *args, **options):
        if options["count"] < 0:
            raise OpenOutreachError(ErrorType.BAD_CONFIG, "count cannot be negative")
        opener = _browser() if options["open_profiles"] else None

        self._configure_logging(options.get("log_level"), options["verbosity"])
        self._ensure_db()
        self._ensure_onboarded()
        self._validate_operator()

        campaign = _select_campaign(options.get("campaign"))
        goal = Goal(count=options["count"], unit=options["unit"])

        result = run_job(campaign, goal, on_new_lead=opener)
        self._report(campaign, result, options)

        if not result.reached:
            raise OpenOutreachError(result.stopped_because, result.detail)

    # ── output ───────────────────────────────────────────────────

    def _report(self, campaign, result: JobResult, options) -> None:
        """Write the rows to stdout. Called whether or not the goal was met — seven
        leads are seven leads, and a caller that only wanted rows should not have to
        care that it asked for ten."""
        records = list(lead_records(campaign))
        if options["only_new"]:
            produced = set(result.produced_ids)
            records = [row for row in records if row["lead_id"] in produced]

        if options["as_json"]:
            self.stdout.write(json.dumps({
                "campaign": campaign.name,
                "goal": {"count": result.goal.count, "unit": result.goal.unit},
                "produced": result.produced,
                "reached": result.reached,
                "stopped_because": result.stopped_because,
                "detail": result.detail or None,
                "leads": records,
            }, indent=2))
            return

        write_csv(records, self.stdout)
        # The count belongs on stderr: a stray line in a CSV is not a CSV.
        logger.info("%d of %d %s · %d row(s) printed",
                    result.produced, result.goal.count, result.goal.unit, len(records))

    # ── the preamble, inherited from the deleted `run` verb ───────

    def _configure_logging(self, log_level: str | None, verbosity: int):
        """``--log-level`` wins; Django's ``-v 2`` stays as the shorthand for debug."""
        from openoutreach.core.logging import configure_logging, print_banner

        if log_level:
            level = getattr(logging, log_level.upper())
        else:
            level = logging.DEBUG if verbosity >= 2 else logging.INFO
        configure_logging(level=level)
        print_banner()

    def _ensure_db(self):
        # Django's migrate narrates to stdout, and stdout is the CSV. A stray
        # "Applying core.0001_initial… OK" in a redirected file is exactly what the
        # output contract exists to prevent.
        call_command("migrate", "--no-input", stdout=self.stderr)

        from openoutreach.core.management.setup_crm import setup_crm
        setup_crm()

    def _ensure_onboarded(self):
        """Environment first, wizard only if a human is there to answer.

        The order is the point: an agent-driven install has no TTY, so the
        non-interactive path is the main path. What the environment cannot satisfy
        goes to the wizard on a TTY, or exits **naming the variables** that would
        have satisfied it — never a bare "onboarding incomplete".
        """
        from openoutreach.core import onboarding

        if not onboarding.missing_keys():
            return

        filled = onboarding.hydrate_from_env()
        if filled:
            logger.info("Configured from the environment: %s.", ", ".join(sorted(filled)))
        if not onboarding.missing_keys():
            return

        if sys.stdin.isatty():
            onboarding.onboard_interactive()
            return

        raise OpenOutreachError(
            ErrorType.ONBOARDING_INCOMPLETE,
            "no TTY, and the environment does not carry everything.\n"
            "Set these and run again:\n"
            f"{onboarding.env_help()}\n"
            "Optional: "
            f"{onboarding.ENV_PREFIX}CAMPAIGN_NAME, {onboarding.ENV_PREFIX}LLM_API_BASE "
            f"(required for openai_compatible:*), {onboarding.ENV_PREFIX}NEWSLETTER.\n"
            f"{onboarding.ENV_PREFIX}ACCEPT_LEGAL_NOTICE must be set to 'true' — it "
            f"records that you accept {onboarding.LEGAL_NOTICE_URL}.",
        )

    def _validate_operator(self):
        """Fail loudly on the three things a job cannot run without.

        Each exits with a typed line rather than a log record: these are answers to
        the reader, and a program needs to branch on them.
        """
        from openoutreach.core.models import SiteConfig
        from openoutreach.core.operator import campaigns, get_active_user

        if not SiteConfig.load().llm_api_key:
            raise OpenOutreachError(
                ErrorType.ONBOARDING_INCOMPLETE,
                "no LLM API key — set OPENOUTREACH_LLM_API_KEY, or edit Site "
                "Configuration in the Django Admin.",
            )

        if get_active_user() is None:
            raise OpenOutreachError(
                ErrorType.ONBOARDING_INCOMPLETE, "no active operator account.")

        if not campaigns():
            raise OpenOutreachError(
                ErrorType.ONBOARDING_INCOMPLETE, "no campaigns for this operator.")


# ── choosing what to work on ─────────────────────────────────────

def _select_campaign(name: str | None):
    """The named campaign, or the only one. Ambiguity is an error, never a guess."""
    from openoutreach.core.operator import campaigns

    known = campaigns()
    if name:
        match = next((c for c in known if c.name == name), None)
        if match is None:
            raise OpenOutreachError(
                ErrorType.BAD_CONFIG,
                f"no campaign named {name!r} — this operator has: "
                + ", ".join(repr(c.name) for c in known),
            )
        return match

    if len(known) > 1:
        raise OpenOutreachError(
            ErrorType.BAD_CONFIG,
            "several campaigns — name one with --campaign: "
            + ", ".join(repr(c.name) for c in known),
        )
    return known[0]


def _browser():
    """A callback that opens each new lead's profile, or a refusal if it cannot.

    **This does not spend the browserless claim.** Nothing is fetched, automated or
    authenticated here: a URL is handed to the operator's own browser and a human looks
    at it. ``profile_url`` stays *stored, never fetched* by us.

    A flag that silently does nothing is the bug you find at 2am, so an environment with
    no browser is an error at argument time rather than a no-op at the end of a long job.
    """
    try:
        webbrowser.get()
    except webbrowser.Error:
        raise OpenOutreachError(
            ErrorType.BAD_CONFIG,
            "--open needs a browser, and none is available here (headless?)",
        ) from None

    def open_profile(lead):
        if lead.profile_url:
            webbrowser.open(lead.profile_url)

    return open_profile
