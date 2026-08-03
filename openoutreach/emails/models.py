# openoutreach/emails/models.py
"""Mailbox: one SMTP sending inbox, imported from the provider's creds export."""
from __future__ import annotations

from django.db import models
from django.utils import timezone

from openoutreach.core.conf import WARM_FLOOR_SENDS
from openoutreach.emails.delivery_policy import Response


def _local_midnight():
    """Start of today in local time — the horizon every per-day ledger counts from."""
    return timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)


class MailboxManager(models.Manager):
    """Pool-level send pacing — the daily-cap accounting the task and planner share."""

    def remaining_today(self) -> int:
        """Total sends left across the pool today (Σ per-box headroom).

        0 when no boxes exist or every box is at its cap.
        """
        return sum(box.headroom_today() for box in self.all())

    def least_loaded_under_cap(self):
        """The box with the most headroom today, or None if none has any.

        Reads ``headroom_today`` rather than comparing counts directly, so a box
        the receiver has paused is excluded here exactly as it is from
        ``remaining_today`` — the picker and the budget must not disagree about
        which boxes can send.
        """
        ranked = [(box, headroom) for box in self.all()
                  if (headroom := box.headroom_today()) > 0]
        if not ranked:
            return None
        return max(ranked, key=lambda pair: pair[1])[0]

    def create_verified(
        self,
        *,
        from_address: str,
        password: str,
        host: str,
        port: int,
        imap_host: str,
        imap_port: int,
    ) -> tuple["Mailbox | None", str]:
        """Auth-check a mailbox over SMTP, then store it — the connect gate.

        The provider has no health API, so the SMTP login *is* the gate: nothing
        is stored unless auth succeeds, so a row always means working credentials.
        The SMTP username is the address itself (a mailbox you own logs in as
        itself; a distinct login is a relay case we don't support). Returns
        ``(mailbox, "")`` on success or ``(None, reason)`` when auth is rejected.
        Re-entering an address repairs that box in place (``update_or_create``).

        The new box keeps the floor capacity it is created with; the first
        ``refresh_capacity`` reads its Sent folder and replaces that with what the
        box has actually sustained. A box with real history is therefore throttled
        for at most one reconcile cycle, not for a warmup calendar.
        """
        from openoutreach.emails.smtp import verify_auth

        ok, reason = verify_auth(host, port, from_address, password)
        if not ok:
            return None, reason
        box, _ = self.update_or_create(
            username=from_address,
            defaults={
                "password": password,
                "from_address": from_address,
                "host": host,
                "port": port,
                "imap_host": imap_host,
                "imap_port": imap_port,
            },
        )
        return box, ""


class Mailbox(models.Model):
    """One SMTP inbox, connected field-by-field at onboarding.

    A row exists only once its credentials pass the SMTP auth-check
    (``objects.create_verified``) — the provider has no health API, so the login
    is the gate. Send-time failures are not swallowed: a bad send fails its task
    and is retried, the box is left untouched (re-enter fixed credentials to
    repair it). host/port/imap default to IceMail's Google Workspace boxes; any
    other provider overrides them at entry.
    """

    host = models.CharField(max_length=255, default="smtp.gmail.com")
    port = models.PositiveIntegerField(default=587)
    # IMAP read side, for the agentic follow-up loop's reply-reader
    # (emails/inbox.py); authenticates with the same address + app password as SMTP.
    imap_host = models.CharField(max_length=255, default="imap.gmail.com")
    imap_port = models.PositiveIntegerField(default=993)
    # The SMTP login — always the address itself (a mailbox you own logs in as
    # itself), kept as its own column for the unique constraint.
    username = models.CharField(max_length=320, unique=True)
    password = models.CharField(max_length=255)
    from_address = models.EmailField(max_length=320)
    # Sign-off appended verbatim to every email sent from this box (opener and
    # follow-ups alike). Per box, not global: the signature is part of the sending
    # identity, and a second box is usually a second identity. The email agent is
    # told never to sign (prompts/outreach_agent.j2), so this is the only sign-off.
    # NULL and "" are distinct: NULL means never asked (the onboarding signature
    # step backfills those), "" means the operator declined one and must stick —
    # collapsing them would re-ask a declining operator on every startup.
    signature = models.TextField(blank=True, null=True, default=None)
    # Warm-safe sends/day for this box — *measured*, not configured. Recomputed
    # daily by ``emails/warmth.py`` from the box's own Sent folder, so a mailbox
    # that has been carrying volume for months is trusted with it immediately and
    # one connected an hour ago is not. Enforced at send time, per box (counts
    # this box's outgoing messages since midnight). Persisted rather than derived
    # on demand only because reading it costs an IMAP round trip; it is a cache of
    # the box's own history and safe to discard. The default is the floor, not a
    # working volume: it applies only to a box that has never been measured, and
    # an unmeasured box is one we know nothing about.
    daily_limit = models.PositiveIntegerField(default=WARM_FLOOR_SENDS)
    # Resume point for the box-wide unsubscribe scan (``emails/inbox.py``): the
    # highest INBOX UID already examined. A UID, not a date — dates are coarse and
    # a resume from "yesterday" either re-reads a day of mail or skips the seam.
    # ``unsub_scan_uidvalidity`` is the server's declared UID epoch: when it
    # changes the server has reissued UIDs, so the stored cursor now points at
    # unrelated mail and the scan restarts from 0 rather than silently skipping
    # everything before it. Both are a cache like ``daily_limit``, not state to
    # protect — resetting them to 0 costs one full rescan, and the scan is
    # idempotent, so nothing is lost.
    unsub_scan_uid = models.PositiveBigIntegerField(default=0)
    unsub_scan_uidvalidity = models.PositiveBigIntegerField(default=0)

    objects = MailboxManager()

    class Meta:
        verbose_name_plural = "Mailboxes"

    def __str__(self):
        return self.from_address or self.username

    def paused_today(self) -> bool:
        """True when a receiver verdict has stopped this box for the rest of today.

        The receiver's own word beats our measurement: a ``550 5.4.5`` is Gmail
        stating the real ceiling, which no amount of Sent-folder history could
        have discovered. Resets at midnight because that is the horizon the
        verdicts describe.
        """
        from openoutreach.emails.delivery_policy import PAUSING_RESPONSES

        return self.verdicts.filter(
            created_at__gte=_local_midnight(), response__in=PAUSING_RESPONSES,
        ).exists()

    def sent_today(self) -> int:
        """Emails this box has sent since local midnight (the per-box cap ledger).

        Counts **outgoing ChatMessages** on this box's deals, not deals: the
        agentic loop sends many emails per deal (opener + every follow-up reply),
        and each outbound email is one row, so the message count is the true send
        volume. LinkedIn ChatMessages never carry a mailbox (``deal.mailbox`` is
        null), so they are naturally excluded.
        """
        from openoutreach.chat.models import ChatMessage

        return ChatMessage.objects.filter(
            deal__mailbox=self, is_outgoing=True, creation_date__gte=_local_midnight(),
        ).count()

    def headroom_today(self) -> int:
        """Sends this box has left today before hitting its measured capacity.

        Zero once the receiver has paused the box, whatever the measurement says.
        """
        if self.paused_today():
            return 0
        return max(0, self.daily_limit - self.sent_today())


class SendVerdict(models.Model):
    """One receiving server's answer to one send attempt.

    The only record of what the far end actually said about this mailbox. It has
    to be stored because it is evidence that exists nowhere else and does not
    survive: the SMTP response lives for the duration of one exception, and
    nothing in the CRM can reconstruct it afterwards. Everything derived *from*
    these rows — whether a box is paused, whether capacity may grow — is computed
    at read time, never stored.

    Written only on failure. A successful send is the absence of a verdict, so
    the table stays small and every row means something.
    """

    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name="verdicts")
    response = models.CharField(max_length=20, choices=Response.choices)
    # None when the failure never produced an SMTP response at all — which is
    # itself the signal that it carries no meaning about reputation.
    smtp_code = models.PositiveSmallIntegerField(null=True, blank=True)
    detail = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["mailbox", "created_at"])]

    def __str__(self):
        return f"{self.mailbox.from_address}: {self.response} ({self.smtp_code})"


def has_mailbox() -> bool:
    """True when ≥1 mailbox is configured — i.e. email is a viable channel to
    send from. Gates the find-email leg: with no mailbox there's nothing to send,
    so resolving an address (and spending a credit) is pointless — the leg stays
    idle until a mailbox is connected."""
    return Mailbox.objects.exists()
