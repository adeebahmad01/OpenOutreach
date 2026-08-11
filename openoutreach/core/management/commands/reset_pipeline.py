# openoutreach/core/management/commands/reset_pipeline.py
"""Reset a campaign's pipeline state without touching its configuration.

Two scopes, because the two reasons to reset are different:

- **discovery** (the default) — drop the query nodes and the keyword vocabulary so the
  next run re-seeds from the ICP. This is what you want after changing the seed prompt
  or the ICP text: the seed only runs for a campaign with no nodes, so an existing walk
  would otherwise keep the vocabulary it opened with forever. Leads and verdicts survive,
  which is the point — they are the evidence the new walk scores itself against.
- **all** — additionally delete the leads, deals, chat and queued tasks, and clear the
  campaign's anchors and fitted GP. A genuine from-scratch run.

Never touches ``SiteConfig``, the operator account, mailboxes or the campaigns
themselves: those are configuration, not pipeline state.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Reset a campaign's discovery walk (and optionally its leads) to start fresh."

    def add_arguments(self, parser):
        parser.add_argument(
            "--campaign", help="Campaign name to reset (default: every campaign).")
        parser.add_argument(
            "--all", action="store_true",
            help="Also delete leads, deals, chat and tasks, and clear anchors + the "
                 "fitted GP. Without this, only the discovery walk is reset.")
        parser.add_argument(
            "--yes", action="store_true", help="Skip the confirmation prompt.")
        parser.add_argument(
            "--no-backup", action="store_true",
            help="Skip the database file copy taken before a --all reset.")

    # ── entry point ──────────────────────────────────────────────────

    def handle(self, *args, **options):
        from openoutreach.core.models import Campaign

        campaigns = Campaign.objects.all()
        if options["campaign"]:
            campaigns = campaigns.filter(name=options["campaign"])
            if not campaigns.exists():
                raise CommandError(f"No campaign named {options['campaign']!r}")

        counts = self._plan(campaigns, full=options["all"])
        self._report("Will delete", counts)
        if not any(counts.values()):
            self.stdout.write("Nothing to reset.")
            return

        if not options["yes"] and not self._confirm():
            self.stdout.write("Aborted.")
            return

        if options["all"] and not options["no_backup"]:
            self._backup()

        with transaction.atomic():
            self._reset(campaigns, full=options["all"])

        self._report("Deleted", counts)
        self.stdout.write(self.style.SUCCESS(
            "Done — the next run re-seeds from the ICP."))

    # ── planning + reporting ─────────────────────────────────────────

    def _plan(self, campaigns, *, full: bool) -> dict[str, int]:
        """What the reset would remove, counted before anything is touched."""
        from openoutreach.chat.models import ChatMessage
        from openoutreach.core.models import Keyword, QueryNode
        from openoutreach.crm.models import Deal, Lead

        nodes = QueryNode.objects.filter(campaign__in=campaigns)
        counts = {"query nodes": nodes.count()}

        # Keywords are global rows shared across campaigns, so they only go when every
        # campaign is being reset — otherwise one campaign's reset would strip another's
        # vocabulary out from under it.
        if self._resetting_everything(campaigns):
            counts["keywords"] = Keyword.objects.count()

        if not full:
            return counts

        deals = Deal.objects.filter(campaign__in=campaigns)
        counts["deals"] = deals.count()
        counts["chat messages"] = ChatMessage.objects.filter(deal__in=deals).count()
        # Leads are campaign-agnostic (keyed on profile_url), so a partial reset leaves
        # them alone rather than deleting rows another campaign is still working.
        if self._resetting_everything(campaigns):
            counts["leads"] = Lead.objects.count()
        return counts

    @staticmethod
    def _resetting_everything(campaigns) -> bool:
        from openoutreach.core.models import Campaign

        return campaigns.count() == Campaign.objects.count()

    def _report(self, verb: str, counts: dict[str, int]) -> None:
        for name, count in counts.items():
            if count:
                self.stdout.write(f"  {verb.lower():>12} {count:>6} {name}")

    def _confirm(self) -> bool:
        answer = input("Proceed? [y/N] ").strip().lower()
        return answer in ("y", "yes")

    # ── the reset itself ─────────────────────────────────────────────

    def _backup(self) -> None:
        """Copy the SQLite file before a destructive reset. No-op on other backends."""
        db = settings.DATABASES["default"]
        if "sqlite" not in db["ENGINE"]:
            return
        source = Path(db["NAME"])
        if not source.exists():
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = source.with_name(f"{source.name}.bak-reset-{stamp}")
        shutil.copy2(source, target)
        self.stdout.write(f"  backup      {target}")

    def _reset(self, campaigns, *, full: bool) -> None:
        from openoutreach.chat.models import ChatMessage
        from openoutreach.core.models import Keyword, QueryNode
        from openoutreach.crm.models import Deal, Lead

        everything = self._resetting_everything(campaigns)

        if full:
            deals = Deal.objects.filter(campaign__in=campaigns)
            ChatMessage.objects.filter(deal__in=deals).delete()
            deals.delete()
            if everything:
                Lead.objects.all().delete()

        QueryNode.objects.filter(campaign__in=campaigns).delete()
        if everything:
            Keyword.objects.all().delete()

        if not full:
            return

        for campaign in campaigns:
            campaign.anchor_profiles = []
            campaign.anchor_embeddings = None
            campaign.model_blob = None
            campaign.save(update_fields=[
                "anchor_profiles", "anchor_embeddings", "model_blob"])
