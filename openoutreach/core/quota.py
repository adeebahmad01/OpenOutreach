# openoutreach/core/quota.py
"""Proportional send allocation — the one place ``Campaign.action_fraction`` binds.

``action_fraction`` declares how much of the operator's outreach the maintainer's
freemium promo campaign may take (``LEGAL_NOTICE.md``, ``README.md``). Nothing
enforced it: ``reconcile`` looped the campaigns and let each fill as much of
``Mailbox.objects.remaining_today()`` as it could, so the realized split was
whichever pipeline produced candidates faster — and freemium wins that race by
construction (``pipeline/freemium_pool.py`` draws from *any* embedded lead with no
deal in its campaign, with no GP confidence gate to clear).

Two decisions shape what "the fraction" means here:

**The quota counts openers, not sends.** A follow-up on a thread the promo already
opened is not new reach — and throttling it mid-conversation would leave a human
who replied hanging, which is worse behavior than the send. So the ledger is
``Deal.email_sent_at`` (stamped once, when the opener goes out) and the drained
resource is the ``email`` task. ``follow_up`` rides outside the quota; ``reconcile``
reserves today's due follow-ups off the top before splitting what's left.

**The allocation is error-diffusing, not a cap.** Slots go one at a time to
whichever campaign is furthest below its weighted share — Bresenham's line
algorithm, the same trick as Floyd-Steinberg dithering and stride scheduling. The
invariant holds at *every prefix* of the opener stream, not just at day's end:
after each send, ``|sent_c - w_c * total| < 1``. At f=0.2 that interleaves one promo
into every five openers instead of letting 28 land back-to-back from one box in an
afternoon — which matters beyond fairness, since a burst of near-identical mail
from a single mailbox is exactly the pattern reputation systems cluster on.

The ledger is *derived*, never stored: ``Deal.email_sent_at`` is already the
authoritative opener record, so there is no counter to migrate, and nothing to
drift after a crash. It is read over a trailing window (``QUOTA_WINDOW_DAYS``)
rather than all-time — an all-time ledger is unbounded debt, and a campaign that
overshot once would be sentenced to silence for the rest of the install's life.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone

from openoutreach.core.conf import QUOTA_WINDOW_DAYS

logger = logging.getLogger(__name__)


# ── The ledger ────────────────────────────────────────────────────────


def _window_start():
    return timezone.now() - timedelta(days=QUOTA_WINDOW_DAYS)


def opener_counts(campaigns) -> dict[int, int]:
    """Openers each campaign has sent inside the quota window, keyed by pk.

    Derived from ``Deal.email_sent_at`` — stamped exactly once per deal, when the
    opener sends, so one row is one person first contacted.
    """
    from openoutreach.crm.models import Deal

    counted = dict(
        Deal.objects.filter(campaign__in=campaigns, email_sent_at__gte=_window_start())
        .values_list("campaign_id")
        .annotate(n=Count("pk"))
    )
    return {c.pk: counted.get(c.pk, 0) for c in campaigns}


def realized_share(campaign) -> float:
    """Fraction of all openers sent in the window that were *campaign*'s.

    The number to compare against ``action_fraction``. 0.0 when nothing has been
    sent in the window.
    """
    from openoutreach.crm.models import Deal

    sent = Deal.objects.filter(email_sent_at__gte=_window_start())
    total = sent.count()
    return sent.filter(campaign=campaign).count() / total if total else 0.0


# ── The split ─────────────────────────────────────────────────────────


def weights(campaigns) -> dict[int, float]:
    """Each campaign's target share of openers, summing to 1.

    Freemium campaigns take ``action_fraction`` (normalized against each other if
    somehow more than one exists); the operator's own campaigns split the rest
    evenly. With no operator campaign the promo takes everything — a work-conserving
    fallback, since holding back the only sendable campaign would just stall the
    daemon, and the fraction has no one to protect.
    """
    promo = [c for c in campaigns if c.is_freemium]
    own = [c for c in campaigns if not c.is_freemium]

    promo_total = sum(c.action_fraction for c in promo)
    if not promo or promo_total <= 0:
        promo_share = 0.0
    elif not own:
        promo_share = 1.0
    else:
        promo_share = min(promo_total, 1.0)

    split = {c.pk: promo_share * (c.action_fraction / promo_total) for c in promo} \
        if promo_total > 0 else {c.pk: 0.0 for c in promo}
    if own:
        split.update({c.pk: (1.0 - promo_share) / len(own) for c in own})
    return split


def allocate(campaigns, slots: int, sent: dict[int, int]) -> dict[int, int]:
    """Split *slots* opener slots across *campaigns* by weight — Bresenham.

    Each slot goes to the campaign furthest below its weighted share of the stream
    so far, so the realized ratio tracks the target after every single send rather
    than converging only in aggregate. *sent* is the window ledger from
    ``opener_counts`` (not mutated).

    Campaigns with zero weight get no slots but still count toward the stream total
    — they are part of the ratio being corrected, just not candidates to receive.
    """
    grant = {c.pk: 0 for c in campaigns}
    target = weights(campaigns)
    eligible = [c for c in campaigns if target[c.pk] > 0]
    if slots <= 0 or not eligible:
        return grant

    running = dict(sent)
    for _ in range(slots):
        total = sum(running.values()) + 1
        hungriest = max(eligible, key=lambda c: target[c.pk] * total - running[c.pk])
        grant[hungriest.pk] += 1
        running[hungriest.pk] += 1
    return grant


def by_hunger(campaigns, sent: dict[int, int]) -> list:
    """*campaigns* ordered by how far below their weighted share they are.

    The single-slot form of ``allocate``: openers are minted one at a time under
    send pacing, so rather than splitting a day's budget up front the scheduler
    asks who is owed the *next* one. Same Bresenham comparison, so the
    ``|sent - w*total| < 1`` invariant is unchanged.

    Ordering rather than picking a single winner keeps the drain work-conserving:
    the hungriest campaign may have nothing ready to send, and the slot should
    fall through to the next one rather than be wasted. Zero-weight campaigns are
    excluded — they still count toward the stream total, but never receive.
    """
    target = weights(campaigns)
    total = sum(sent.values()) + 1
    eligible = [c for c in campaigns if target[c.pk] > 0]
    return sorted(eligible, key=lambda c: target[c.pk] * total - sent[c.pk], reverse=True)


def log_shares(campaigns) -> None:
    """Log realized vs target opener share — the audit that was missing.

    A declared 0.2 drifted to 0.7 in production because nothing ever compared the
    two out loud.
    """
    target = weights(campaigns)
    for campaign in campaigns:
        logger.info(
            "[%s] opener share over %dd: %.0f%% realized vs %.0f%% target",
            campaign, QUOTA_WINDOW_DAYS,
            100 * realized_share(campaign), 100 * target[campaign.pk],
        )
