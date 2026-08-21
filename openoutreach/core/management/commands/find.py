"""Find leads until a goal is met, then print the campaign and exit.

    openoutreach find 1                   # one qualified lead, with its reason — free
    openoutreach find 10                  # ten more leads, guaranteed to spend nothing
    openoutreach find 10 --emails         # ...and buy addresses for what is ready
    openoutreach find 10 emails           # ten more *carrying* an address (≤10 credits)
    openoutreach find 0                   # no work; print what the campaign already has
    openoutreach find 10 --open           # ...and open each new profile in the browser
    openoutreach find 10 --debug          # ...and show the walk's reasoning as it goes

**Finding is free; buying an address is not, and the free thing is the default.**
Discovery and qualification cost only the operator's own LLM key, so a bare ``find 10``
cannot spend a credit however many deals have queued up past the confidence gate. The
address lookup is opt-in — ``--emails`` permits it, and the ``emails`` unit implies it.

This was the other way round until 2026-08-21: buying was on unless ``--no-emails``
turned it off, so ``find 10 leads`` quietly bought an address for whatever an earlier run
had left ready. The docstring even called it free. **A flag you forget should cost you a
feature, never money**, which is the whole argument for the inversion.

**The unit is a noun, not a flag**, and that is a budget decision rather than a style one:
the provider bills one credit per verified hit, so ``find 10 emails`` is capped at ten
credits by construction. The number typed is the budget, in the same unit as the invoice.
The noun says what to *count*; the flag says what may be *paid for*. They are independent
in the one direction that matters — counting leads never authorises a purchase.

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
import webbrowser

from openoutreach.core.errors import ErrorType, OpenOutreachError
from openoutreach.core.export import lead_records, write_csv
from openoutreach.core.job import EMAILS, LEADS, UNITS, Goal, JobResult, run_job
from openoutreach.core.management.base import OpenOutreachCommand
from openoutreach.core.management.bootstrap import (
    ensure_database,
    ensure_onboarded,
    validate_operator,
)

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
        parser.add_argument("--emails", action="store_true", dest="buy_emails",
                            help="Also buy an address for any lead that has cleared the "
                                 "confidence gate — one credit per verified hit. Without "
                                 "this the run cannot spend. Implied by the `emails` unit.")
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
        # Same dest, so the two cannot disagree: whichever comes last on the command
        # line wins. `--debug` is the one an operator reaches for mid-run.
        parser.add_argument("--debug", action="store_const", const="debug",
                            dest="log_level", help="Shorthand for --log-level debug.")

    def handle(self, *args, **options):
        if options["count"] < 0:
            raise OpenOutreachError(ErrorType.BAD_CONFIG, "count cannot be negative")
        # The unit says what to count; the flag says what may be paid for. A goal counted
        # in addresses cannot be met without buying them, so the noun implies the flag —
        # that is the one place the two are not independent.
        buy_addresses = options["buy_emails"] or options["unit"] == EMAILS
        opener = _browser() if options["open_profiles"] else None

        self._configure_logging(options.get("log_level"), options["verbosity"])
        ensure_database(self.stderr)
        ensure_onboarded()
        validate_operator()

        campaign = _select_campaign(options.get("campaign"))
        goal = Goal(count=options["count"], unit=options["unit"])

        result = run_job(campaign, goal, on_new_lead=opener,
                         buy_addresses=buy_addresses)
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

    # ── logging ──────────────────────────────────────────────────

    def _configure_logging(self, log_level: str | None, verbosity: int):
        """``--log-level`` wins; Django's ``-v 2`` stays as the shorthand for debug."""
        from openoutreach.core.logging import configure_logging, print_banner

        if log_level:
            level = getattr(logging, log_level.upper())
        else:
            level = logging.DEBUG if verbosity >= 2 else logging.INFO
        configure_logging(level=level)
        print_banner()


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
