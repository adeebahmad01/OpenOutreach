# openoutreach/emails/management/commands/mailreport.py
"""``manage.py mailreport`` — what the mailboxes have seen, and what came back.

The operator-facing half of the mail log: the two questions that were previously
answerable only by a human with a live IMAP session, plus the coverage rows that
say how much of each box we have actually read.
"""
from django.core.management.base import BaseCommand

from openoutreach.emails import report
from openoutreach.emails.models import Mailbox


class Command(BaseCommand):
    help = "Inbound mail backlog, per-kind counts, bounce rate and folder coverage."

    def add_arguments(self, parser):
        parser.add_argument(
            "--mailbox", default=None,
            help="Limit the report to one mailbox, by from-address.",
        )
        parser.add_argument(
            "--days", type=int, default=report.RATE_WINDOW_DAYS,
            help="Window for the bounce rate, in days.",
        )

    def handle(self, *args, **options):
        mailbox = self._mailbox(options["mailbox"])

        backlog = report.inbound_backlog(mailbox)
        self.stdout.write(self.style.MIGRATE_HEADING("Inbound"))
        self.stdout.write(
            f"  {backlog['processed']} processed of {backlog['stored']} stored"
            f" — {backlog['pending']} pending",
        )
        for kind, count in report.kind_counts(mailbox).items():
            self.stdout.write(f"    {count:>6}  {kind}")

        rate = report.bounce_rate(mailbox, days=options["days"])
        self.stdout.write(self.style.MIGRATE_HEADING("Delivery"))
        self.stdout.write(f"  bounce rate over {options['days']}d: {rate:.2%}")

        self.stdout.write(self.style.MIGRATE_HEADING("Coverage"))
        for line in report.coverage_lines(mailbox) or ["  no folder has been walked yet"]:
            self.stdout.write(f"  {line}")

    def _mailbox(self, from_address):
        """The named mailbox, or None for the whole pool."""
        if not from_address:
            return None
        box = Mailbox.objects.filter(from_address=from_address).first()
        if box is None:
            raise SystemExit(f"no mailbox with from_address={from_address}")
        return box
