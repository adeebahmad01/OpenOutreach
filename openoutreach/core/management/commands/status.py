# openoutreach/core/management/commands/status.py
"""Ask the daemon what happened.

    openoutreach status            # human summary
    openoutreach status --json     # the whole document, for a program

The verb an agent uses instead of tailing a log. It reads the same SQLite file the
daemon is writing (WAL, so no lock), touches nothing, and never blocks: a provider
that will not answer is reported as an unknown balance, not an exception.

Output contract: the summary is the **result**, so it goes to stdout; logs go to
stderr. ``--json`` prints one object and nothing else, so it pipes into ``jq``.
"""
from __future__ import annotations

import json

from openoutreach.core.management.base import OpenOutreachCommand
from openoutreach.core.status import build_status


class Command(OpenOutreachCommand):
    help = "Report what is configured, what is blocked, the counts, and the next action."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Emit the full status document as JSON on stdout.",
        )

    def handle(self, *args, **options):
        status = build_status()
        if options["as_json"]:
            self.stdout.write(json.dumps(status, indent=2))
            return
        self.stdout.write(render(status))


# ── the human summary ────────────────────────────────────────────

def render(status: dict) -> str:
    """Render the status document as a short human summary."""
    sections = (
        _render_config(status["onboarding"]),
        _render_pipeline(status["campaigns"], status["totals"]),
        _render_credits(status["credits"]),
        _render_blocked(status["blocked"]),
        _render_next(status["next_action"]),
    )
    return "\n".join(section for section in sections if section)


def _render_config(onboarding: dict) -> str:
    if onboarding["complete"]:
        return "Configuration: complete."
    lines = ["Configuration: incomplete."]
    for step, variables in onboarding["missing"].items():
        lines.append(f"  {step}: set {', '.join(variables)}")
    return "\n".join(lines)


def _render_pipeline(campaigns: list[dict], totals: dict) -> str:
    if not campaigns:
        return "Pipeline: no campaigns yet."

    lines = [
        "Pipeline:",
        f"  {totals['leads_seen']} lead(s) seen, {totals['rejected']} rejected by the qualifier",
        f"  {totals['exportable']} exportable — {totals['exportable_with_email']} with an email, "
        f"{totals['exportable_without_email']} without (a row exports either way)",
        f"  {totals['ranked_for_lookup']} ranked for a paid lookup, "
        f"{totals['lookup_in_flight']} in flight",
    ]
    if len(campaigns) > 1:
        lines.append("  by campaign:")
        lines += [
            f"    {row['name']}: {row['exportable']} exportable of {row['leads_seen']} seen"
            for row in campaigns
        ]
    return "\n".join(lines)


def _render_credits(credits: dict) -> str:
    if credits["balance"] is not None:
        return f"Credits: {credits['balance']} left."
    return f"Credits: unknown ({credits['error']})."


def _render_blocked(blocked: list[dict]) -> str:
    if not blocked:
        return ""
    lines = ["Blocked:"]
    lines += [f"  {item['type']}: {item['message']}" for item in blocked]
    return "\n".join(lines)


def _render_next(action: dict) -> str:
    """The next action, minus its ``variables`` — the configuration section listed those
    already, per step, which is the more useful grouping for a human. They stay in
    ``--json``, where an agent wants them flat."""
    lines = [f"Next: {action['message']}"]
    if action.get("unlocks"):
        lines.append(f"  unlocks: {action['unlocks']}")
    if action.get("path"):
        lines.append(f"  file: {action['path']}")
    if action.get("url"):
        lines.append(f"  go to: {action['url']}")
    return "\n".join(lines)
