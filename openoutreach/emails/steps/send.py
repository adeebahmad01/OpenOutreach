# openoutreach/emails/steps/send.py
"""The first email — the only cold message this person will ever get.

The outreach agent opens the conversation, SMTP sends it, and the send is recorded
on the Deal, which moves it to EMAILED. From there the deal is only ever touched
again if the recipient answers.

That is the whole reason this step is the one under a cap. A first email is *cold
volume*, which is what a receiver punishes; a reply inside a thread someone started
is not, and goes out with no cap and no spacing (``steps/reply.py``). So the two
guards — the box's measured daily ceiling and the ≥3-minute spacing — live here and
nowhere else.
"""
from __future__ import annotations

import logging
import random
from datetime import timedelta

from django.utils import timezone
from termcolor import colored

from openoutreach.core.conf import MIN_SEND_INTERVAL_SECONDS, SEND_INTERVAL_JITTER_SECONDS
from openoutreach.crm.models import DealState

logger = logging.getLogger(__name__)


def send_first_email(deal, mailbox) -> DealState | None:
    """Open the conversation with *deal* from *mailbox*. Returns the next state.

    The caller has already established that this box may send right now
    (``Mailbox.objects.free_for_first_email``). Returns ``None`` without sending if
    the lead was suppressed while the agent was writing — an unsubscribe can land in
    the seconds an LLM call takes, and this is the last gate before the message goes.
    """
    from openoutreach.chat.models import ChatMessage
    from openoutreach.core.agents.outreach import run_outreach_agent
    from openoutreach.core.db.summaries import materialize_profile_summary_if_missing
    from openoutreach.core.operator import get_active_user
    from openoutreach.emails.sender import operator_bcc, send_email, suppressed

    logger.info("[%s] %s %s via %s", deal.campaign,
                colored("▶ first email", "blue", attrs=["bold"]),
                deal.lead.profile_url, mailbox.from_address)

    materialize_profile_summary_if_missing(deal)
    opener = run_outreach_agent(deal)

    if suppressed(deal.lead):
        logger.warning("[%s] %s was suppressed mid-run — not sending",
                       deal.campaign, deal.lead.profile_url)
        return None

    message_id = send_email(
        mailbox, deal.lead.email, opener.subject, opener.message,
        bcc=operator_bcc(get_active_user(), deal.campaign),
    )

    now = timezone.now()
    deal.mailbox = mailbox
    deal.email_subject = opener.subject
    deal.email_message_id = message_id
    deal.email_sent_at = now
    ChatMessage.objects.create(
        deal=deal,
        external_id=message_id,
        content=opener.message,
        is_outgoing=True,
        owner=get_active_user(),
        creation_date=now,
    )
    _space_out(mailbox, now)
    return DealState.EMAILED


def _space_out(mailbox, now) -> None:
    """Set when this box may send its next first email.

    Fresh jitter every time: a fixed 3-minute cadence is its own machine signature,
    so the gap has to vary. Per box, because the daily ceiling is per box — two
    mailboxes are two sending identities and one receiver's rhythm says nothing
    about the other's.
    """
    mailbox.next_send_at = now + timedelta(
        seconds=MIN_SEND_INTERVAL_SECONDS + random.uniform(0, SEND_INTERVAL_JITTER_SECONDS),
    )
    mailbox.save(update_fields=["next_send_at"])
