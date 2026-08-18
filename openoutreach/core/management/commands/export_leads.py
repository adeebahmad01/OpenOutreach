# openoutreach/core/management/commands/export_leads.py
"""Export a campaign's qualified leads as CSV.

Tier 0 of the integration surface: the operator gets their leads out and sends with
whatever they already run. The record shape is ``core/export.py``; this command is only
the I/O around it.

    manage.py export_leads --campaign "Acme"                 # to stdout
    manage.py export_leads --campaign "Acme" > leads.csv     # to a file

One argument on purpose. The shell already redirects to a file, and every other knob
this had (a format switch, an output path, a state filter, a rejected-leads escape
hatch) was a way of asking for something nobody had asked for yet.

Leads the qualifier rejected and leads carrying an opt-out are never exported — see
``export.lead_records``.
"""
from __future__ import annotations

import sys

from django.core.management.base import BaseCommand, CommandError

from openoutreach.core.export import lead_records, write_csv


class Command(BaseCommand):
    help = "Export a campaign's qualified leads as CSV."

    def add_arguments(self, parser):
        parser.add_argument("--campaign", required=True, help="Campaign name to export.")

    def handle(self, *args, **options):
        from openoutreach.core.models import Campaign

        campaign = Campaign.objects.filter(name=options["campaign"]).first()
        if campaign is None:
            raise CommandError(f"No campaign named {options['campaign']!r}.")

        count = write_csv(lead_records(campaign), self.stdout)
        # stderr, so a piped CSV on stdout stays a clean CSV.
        print(f"exported {count} lead(s) from {campaign.name}", file=sys.stderr)
