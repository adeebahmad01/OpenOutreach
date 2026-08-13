# openoutreach/emails/report.py
"""Questions an operator must be able to answer from the system, not from IMAP.

Two of them drove this whole design, and neither was answerable:

- *how many inbound messages have I processed against how many exist?* — with
  reading and deciding fused, a message nothing had a rule for left no row, so
  the denominator did not exist.
- *what is my bounce rate?* — with delivery recorded only inside an exception
  path, the numerator did not exist either, and a hard bounce is not an exception.

Both are now counts over the log. They are read-only and cheap, so anything may
call them: the daemon's own logs, the admin, an operator at a shell.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from openoutreach.emails.models import DeliveryEvent, Direction, Kind, Message

# The window a rate is quoted over. A month: long enough that one bad afternoon
# does not dominate, short enough that a repaired domain is not judged by its worst
# week forever.
RATE_WINDOW_DAYS = 30


def bounce_rate(mailbox=None, *, days: int = RATE_WINDOW_DAYS) -> float:
    """Bounces per accepted send over the trailing window; 0.0 with nothing sent.

    Accepted sends are the denominator rather than *attempted* ones: a send the
    receiver never took responsibility for cannot bounce, and counting it would
    flatter a box whose failures are all at the front door.
    """
    events = DeliveryEvent.objects.filter(
        occurred_at__gte=timezone.now() - timedelta(days=days))
    if mailbox is not None:
        events = events.filter(message__mailbox=mailbox)

    accepted = events.filter(status=DeliveryEvent.Status.ACCEPTED).count()
    if not accepted:
        return 0.0
    bounced = events.filter(status=DeliveryEvent.Status.BOUNCED).count()
    return bounced / accepted


def inbound_backlog(mailbox=None) -> dict[str, int]:
    """``{stored, classified, processed, pending}`` for inbound mail.

    ``pending`` is the number this system exists to keep visible: mail we hold and
    have not yet acted on. It is a number, not an absence — which is the entire
    difference between *no reply* and *reply not read*.
    """
    inbound = Message.objects.filter(direction=Direction.INBOUND)
    if mailbox is not None:
        inbound = inbound.filter(mailbox=mailbox)

    stored = inbound.count()
    processed = inbound.filter(processed_at__isnull=False).count()
    return {
        "stored": stored,
        "classified": inbound.exclude(kind="").count(),
        "processed": processed,
        "pending": stored - processed,
    }


def coverage_lines(mailbox=None) -> list[str]:
    """One line per mirrored folder: how far it is read, and when it last completed."""
    from openoutreach.emails.models import FolderCoverage

    rows = FolderCoverage.objects.select_related("mailbox").order_by("mailbox_id", "folder")
    if mailbox is not None:
        rows = rows.filter(mailbox=mailbox)
    return [
        f"{row.mailbox.from_address}:{row.folder} — read to UID {row.last_uid}"
        f" (uidvalidity {row.uidvalidity}), "
        + (f"complete at {row.synced_at:%Y-%m-%d %H:%M}" if row.synced_at else "never completed")
        for row in rows
    ]


def kind_counts(mailbox=None) -> dict[str, int]:
    """How many inbound messages of each kind — including the unclassified."""
    from django.db.models import Count

    inbound = Message.objects.filter(direction=Direction.INBOUND)
    if mailbox is not None:
        inbound = inbound.filter(mailbox=mailbox)
    counted = dict(
        inbound.values_list("kind").annotate(n=Count("kind")).values_list("kind", "n"))
    return {(kind or "unclassified"): counted.get(kind, 0)
            for kind in [*[k.value for k in Kind], ""]
            if kind != Kind.OUTBOUND}
