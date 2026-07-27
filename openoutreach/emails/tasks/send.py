# openoutreach/emails/tasks/send.py
"""EMAIL task — sends the opening email for a deal at READY_TO_EMAIL.

The first touch: pick the oldest queued deal + an under-cap box, let the outreach
agent open the conversation, send over SMTP, and record the send on the Deal —
which moves it to EMAILED, where the FOLLOW_UP task takes the same agent through
the rest of the thread.

Each concern lives where it's cohesive; this module is just the orchestration:
  - the queue (one FSM state)  → ``core.db.deals.get_emailable_deals``
  - the per-box daily cap       → ``emails.models.Mailbox`` pacing manager
  - SMTP transport              → ``emails.sender.send_email``
"""
from __future__ import annotations

import logging

from django.utils import timezone
from termcolor import colored

from openoutreach.chat.models import ChatMessage
from openoutreach.core.business_time import add_business_hours
from openoutreach.crm.models import DealState

logger = logging.getLogger(__name__)


def handle_email(task, session, qualifiers):
    from openoutreach.core.agents.outreach import run_outreach_agent
    from openoutreach.core.db.deals import get_emailable_deals
    from openoutreach.core.db.summaries import materialize_profile_summary_if_missing
    from openoutreach.emails.models import Mailbox
    from openoutreach.emails.sender import operator_bcc, send_email

    campaign = session.campaign

    mailbox = Mailbox.objects.least_loaded_under_cap()
    deal = get_emailable_deals(session).first() if mailbox else None
    if mailbox is None or deal is None:
        logger.info("[%s] email: nothing to send (empty queue or every box at cap)", campaign)
        return

    public_id = deal.lead.profile_url
    logger.info("[%s] %s %s via %s", campaign,
                colored("▶ email", "blue", attrs=["bold"]), public_id, mailbox.from_address)

    materialize_profile_summary_if_missing(deal, session)
    opener = run_outreach_agent(session, deal)

    message_id = send_email(
        mailbox, deal.lead.email, opener.subject, opener.message,
        bcc=operator_bcc(session.django_user, campaign),
    )
    _record_sent_email(session, deal, mailbox, opener, message_id)


def _record_sent_email(session, deal, mailbox, opener, message_id) -> None:
    """Bind the box, stamp the email fields, record the opener message, move to EMAILED.

    The send record and the state transition live on the same row, so a single
    write commits both: the email can never be sent without leaving READY_TO_EMAIL
    (no double-send window), and EMAILED is never set without its audit fields.
    ``next_follow_up_at`` is seeded from the agent's own ``follow_up_hours`` — it
    owns the first gap just as it owns every later one — counted in business hours
    so the countdown never expires on a weekend, so the loop reads replies + acts
    when that countdown fires. The opener is also written as the thread's
    first outgoing ChatMessage — the next turn reads it, and the per-box cap counts
    it (``email_message_id`` is the thread root the reply-reader matches on, and
    the flag that tells the agent this is no longer a first touch).
    """
    now = timezone.now()
    deal.mailbox = mailbox
    deal.email_subject = opener.subject
    deal.email_message_id = message_id
    deal.email_sent_at = now
    deal.next_follow_up_at = add_business_hours(now, opener.follow_up_hours)
    deal.state = DealState.EMAILED
    deal.save(update_fields=[
        "mailbox", "email_subject", "email_message_id", "email_sent_at",
        "next_follow_up_at", "state",
    ])

    ChatMessage.objects.create(
        deal=deal,
        external_id=message_id,
        content=opener.message,
        is_outgoing=True,
        owner=session.django_user,
        creation_date=now,
    )
