# openoutreach/core/management/commands/export_leads.py
"""Export a campaign's qualified leads as CSV or JSONL.

Tier 0 of the integration surface: the operator gets their leads out and sends with
whatever they already run. The record shape is ``core/export.py``; this command is only
the I/O around it — argument parsing, a file or stdout, and a count on stderr.

    manage.py export_leads --campaign "Acme"                    # CSV to stdout
    manage.py export_leads --campaign "Acme" --format jsonl -o leads.jsonl
    manage.py export_leads --campaign "Acme" --state "Ready to Email"

Disqualified leads are left out unless ``--include-disqualified`` is passed: the
default use is handing rows to a sender, and those are exactly the people who must not
be contacted.
"""
from __future__ import annotations

import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from openoutreach.core.export import WRITERS, lead_records


class Command(BaseCommand):
    help = "Export a campaign's qualified leads as CSV or JSONL."

    def add_arguments(self, parser):
        parser.add_argument(
            "--campaign", required=True, help="Campaign name to export.")
        parser.add_argument(
            "--format", choices=sorted(WRITERS), default="csv",
            help="Serialisation (default: csv).")
        parser.add_argument(
            "-o", "--out", help="Write to this file instead of stdout.")
        parser.add_argument(
            "--state", action="append", dest="states", metavar="STATE",
            help="Only leads at this deal state; repeatable. Default: every state.")
        parser.add_argument(
            "--include-disqualified", action="store_true",
            help="Include leads excluded by an opt-out or a rejection. Off by default — "
                 "these are the people who must not be contacted.")

    def handle(self, *args, **options):
        from openoutreach.core.models import Campaign

        campaign = Campaign.objects.filter(name=options["campaign"]).first()
        if campaign is None:
            raise CommandError(f"No campaign named {options['campaign']!r}.")

        records = lead_records(
            campaign,
            states=options["states"],
            include_disqualified=options["include_disqualified"],
        )
        write = WRITERS[options["format"]]

        out = options["out"]
        if out:
            # Atomic: write beside the target and rename, so an interrupted export
            # never leaves a half-file where a whole one used to be.
            target = Path(out)
            partial = target.with_suffix(target.suffix + ".partial")
            try:
                with partial.open("w", newline="", encoding="utf-8") as stream:
                    count = write(records, stream)
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)
        else:
            count = write(records, self.stdout)

        # stderr, so a piped CSV on stdout stays a clean CSV.
        print(f"exported {count} lead(s) from {campaign.name}", file=sys.stderr)
