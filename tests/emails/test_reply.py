# tests/emails/test_reply.py
"""Answering a reply — and the invariant that there is no other reason to email.

The first test here is the whole follow-up policy: a lead who does not write back
is never emailed again. Everything the old scheduler did to pace, prioritise and
reserve capacity for chasing is gone because of it.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from openoutreach.chat.models import ChatMessage
from openoutreach.core.agents.outreach import OutreachDecision
from openoutreach.core.cycle import unanswered_replies
from openoutreach.crm.models import DealState
from openoutreach.emails.models import Mailbox
from openoutreach.emails.steps.reply import answer_reply
from tests.factories import DealFactory, LeadFactory

SENDER = "s@infra.com"


def _box() -> Mailbox:
    return Mailbox.objects.create(
        username=SENDER, password="pw", from_address=SENDER, daily_limit=10,
    )


def _emailed(campaign, box, when=None):
    """A deal we have emailed, with the first email recorded as the thread root."""
    when = when or timezone.now() - timedelta(days=3)
    deal = DealFactory(
        campaign=campaign,
        lead=LeadFactory(email="p@corp.com"),
        state=DealState.EMAILED,
        mailbox=box,
        email_subject="Hi",
        email_message_id="<root@infra.com>",
        email_sent_at=when,
    )
    ChatMessage.objects.create(
        deal=deal, external_id="<root@infra.com>", content="The opener.",
        is_outgoing=True, creation_date=when,
    )
    return deal


def _reply(deal, when=None, content="Sure, tell me more."):
    return ChatMessage.objects.create(
        deal=deal, external_id=f"<r{ChatMessage.objects.count()}@corp.com>",
        content=content, is_outgoing=False, creation_date=when or timezone.now(),
    )


# ── What makes a deal actionable ──────────────────────────────────


@pytest.mark.django_db
class TestUnansweredReplies:
    def test_silence_is_never_actionable(self, campaign):
        """**The core invariant.** No reply, no further email — ever. Not after a
        day, not after a month: an unanswered thread simply is not work."""
        box = _box()
        _emailed(campaign, box, when=timezone.now() - timedelta(days=90))

        assert list(unanswered_replies(campaign)) == []

    def test_a_reply_makes_the_deal_actionable(self, campaign):
        box = _box()
        deal = _emailed(campaign, box)
        _reply(deal)

        assert list(unanswered_replies(campaign)) == [deal]

    def test_a_thread_we_have_already_answered_is_not_actionable(self, campaign):
        box = _box()
        deal = _emailed(campaign, box)
        _reply(deal, when=timezone.now() - timedelta(hours=2))
        ChatMessage.objects.create(
            deal=deal, external_id="<ours@infra.com>", content="Answered.",
            is_outgoing=True, creation_date=timezone.now(),
        )

        assert list(unanswered_replies(campaign)) == []

    def test_a_second_reply_after_our_answer_reopens_it(self, campaign):
        box = _box()
        deal = _emailed(campaign, box)
        _reply(deal, when=timezone.now() - timedelta(hours=2))
        ChatMessage.objects.create(
            deal=deal, external_id="<ours@infra.com>", content="Answered.",
            is_outgoing=True, creation_date=timezone.now() - timedelta(hours=1),
        )
        _reply(deal, when=timezone.now())

        assert list(unanswered_replies(campaign)) == [deal]

    def test_a_closed_deal_is_left_alone(self, campaign):
        box = _box()
        deal = _emailed(campaign, box)
        _reply(deal)
        deal.outcome = "not_interested"
        deal.save(update_fields=["outcome"])

        assert list(unanswered_replies(campaign)) == []

    def test_oldest_reply_first(self, campaign):
        box = _box()
        waiting = _emailed(campaign, box)
        _reply(waiting, when=timezone.now() - timedelta(hours=5))
        fresh = DealFactory(
            campaign=campaign, lead=LeadFactory(email="q@corp.com"),
            state=DealState.EMAILED, mailbox=box, email_message_id="<root2@infra.com>",
        )
        _reply(fresh, when=timezone.now())

        assert list(unanswered_replies(campaign)) == [waiting, fresh]


# ── answer_reply (the step) ───────────────────────────────────────


@pytest.mark.django_db
class TestAnswerReply:
    def _run(self, deal, decision):
        with patch("openoutreach.core.agents.outreach.run_outreach_agent",
                   return_value=decision), \
                patch("openoutreach.core.db.summaries.update_chat_summary"), \
                patch("openoutreach.emails.sender.send_email",
                      return_value="<sent@infra.com>") as send:
            return send, answer_reply(deal)

    def test_a_reply_is_threaded_and_recorded(self, campaign):
        box = _box()
        deal = _emailed(campaign, box)
        _reply(deal)

        send, next_state = self._run(
            deal, OutreachDecision(action="send_message", message="Glad to."))

        assert next_state is None  # stays EMAILED
        kwargs = send.call_args.kwargs
        assert kwargs["references"] == "<root@infra.com>"
        assert send.call_args.args[2] == "Re: Hi"
        assert ChatMessage.objects.filter(
            deal=deal, is_outgoing=True, external_id="<sent@infra.com>").exists()

    def test_answering_makes_the_deal_quiet_again(self, campaign):
        box = _box()
        deal = _emailed(campaign, box)
        _reply(deal)

        self._run(deal, OutreachDecision(action="send_message", message="Glad to."))

        assert list(unanswered_replies(campaign)) == []

    def test_a_reply_ignores_the_daily_cap(self, campaign):
        """A reply is not cold volume, so a box at its ceiling still answers."""
        box = _box()
        deal = _emailed(campaign, box)
        _reply(deal)
        box.daily_limit = 0
        box.save(update_fields=["daily_limit"])

        send, _ = self._run(
            deal, OutreachDecision(action="send_message", message="Glad to."))

        send.assert_called_once()

    def test_a_reply_ignores_send_spacing(self, campaign):
        box = _box()
        box.next_send_at = timezone.now() + timedelta(hours=1)
        box.save(update_fields=["next_send_at"])
        deal = _emailed(campaign, box)
        _reply(deal)

        send, _ = self._run(
            deal, OutreachDecision(action="send_message", message="Glad to."))

        send.assert_called_once()
        box.refresh_from_db()
        assert box.next_send_at > timezone.now()  # untouched by the reply

    def test_completing_carries_the_outcome(self, campaign):
        box = _box()
        deal = _emailed(campaign, box)
        _reply(deal)

        send, next_state = self._run(deal, OutreachDecision(
            action="mark_completed", outcome="not_interested"))

        assert next_state == DealState.COMPLETED
        assert deal.outcome == "not_interested"
        send.assert_not_called()
