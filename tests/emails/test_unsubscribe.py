# tests/emails/test_unsubscribe.py
"""Opt-out: the advertised mechanism, both detection paths, and the enforcement.

The three legs, and what each one locks down:

  * **Advertised** — every send carries the ``List-Unsubscribe`` header *and* a
    visible reply-line. The header is what receiving filters read; the line
    reaches the clients that render no unsubscribe button of their own. Both are
    asserted on the same message, since either alone leaves someone with no exit
    but the spam button.
  * **Detected** — a client-generated unsubscribe (no threading headers, found
    box-wide by the ``+unsub`` alias during the mail pass) and a worded one
    (threads normally, read by the outreach agent) both reach the same suppression.
  * **Enforced** — suppression binds to the person, so every lead holding the
    address is disqualified and every open deal closes at UNSUBSCRIBED, while an
    already-closed deal keeps the outcome that closed it.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from openoutreach.core.agents.outreach import OutreachDecision
from openoutreach.core.db.leads import suppress_email
from openoutreach.crm.models import DealState, Lead, Outcome
from openoutreach.emails.inbox import read_mail
from openoutreach.emails.models import Mailbox
from openoutreach.emails.sender import (
    ATTRIBUTION,
    OPT_OUT_LINE,
    send_email,
    suppressed,
    unsubscribe_address,
)
from openoutreach.emails.steps.reply import answer_reply
from openoutreach.emails.steps.send import send_first_email
from tests.emails.fake_imap import FakeIMAP, message
from tests.factories import DealFactory, LeadFactory

SENDER = "s@infra.com"
ALIAS = "s+unsub@infra.com"


def _box(**kwargs) -> Mailbox:
    return Mailbox.objects.create(
        username=SENDER, password="pw", from_address=SENDER, daily_limit=10, **kwargs,
    )


def _sent_message(**kwargs):
    """The assembled EmailMessage for one send, without touching SMTP."""
    box = Mailbox(username=SENDER, password="pw", from_address=SENDER, signature="Eracle")
    with patch("openoutreach.emails.sender._deliver") as deliver:
        send_email(box, "lead@corp.com", "Hi", "Body", **kwargs)
    return deliver.call_args.args[1]


# ── The advertised mechanism ──────────────────────────────────────


@pytest.mark.django_db
class TestOptOutIsAdvertised:
    def test_opener_carries_the_list_unsubscribe_header(self):
        assert _sent_message()["List-Unsubscribe"] == f"<mailto:{ALIAS}?subject=unsubscribe>"

    def test_follow_up_carries_it_too(self):
        """Threaded replies go through the same assembly, so they carry it too."""
        message = _sent_message(in_reply_to="<prior@corp.com>")
        assert message["List-Unsubscribe"] == f"<mailto:{ALIAS}?subject=unsubscribe>"

    def test_no_list_unsubscribe_post_header(self):
        """One-click is only valid alongside an https: URI — asserting it absent
        stops a future edit adding the header without the endpoint."""
        assert _sent_message()["List-Unsubscribe-Post"] is None

    def test_body_carries_the_visible_opt_out_line(self):
        """The header reaches the filters; this reaches the clients that don't
        render an unsubscribe button of their own."""
        assert OPT_OUT_LINE in _sent_message().get_content()

    def test_opt_out_sits_between_the_signature_and_the_attribution(self):
        body = _sent_message().get_content()
        assert body.index("Eracle") < body.index(OPT_OUT_LINE) < body.index(ATTRIBUTION)


class TestUnsubscribeAddress:
    def test_alias_is_plus_addressed_on_the_sending_box(self):
        assert unsubscribe_address("s@infra.com") == "s+unsub@infra.com"

    def test_existing_plus_tag_is_not_disturbed(self):
        assert unsubscribe_address("s+out@infra.com") == "s+out+unsub@infra.com"


# ── Enforcement ───────────────────────────────────────────────────


@pytest.mark.django_db
class TestSuppressEmail:
    def test_suppresses_every_lead_holding_the_address(self, campaign):
        """``Lead.email`` has no unique constraint — one address, many rows."""
        first = LeadFactory(email="p@corp.com")
        second = LeadFactory(email="p@corp.com")
        assert suppress_email("p@corp.com") == 2
        assert Lead.objects.filter(pk__in=[first.pk, second.pk], disqualified=True).count() == 2

    def test_matches_case_insensitively(self, campaign):
        """A client echoes back whatever casing it was given."""
        lead = LeadFactory(email="p@corp.com")
        suppress_email("P@Corp.Com")
        lead.refresh_from_db()
        assert lead.disqualified

    def test_open_deal_closes_at_unsubscribed(self, campaign):
        deal = DealFactory(
            campaign=campaign,
            lead=LeadFactory(email="p@corp.com"),
            state=DealState.EMAILED,
        )
        suppress_email("p@corp.com")
        deal.refresh_from_db()
        assert deal.state == DealState.UNSUBSCRIBED

    def test_unsubscribed_deal_leaves_outcome_blank(self, campaign):
        """Reachability ended, not the offer — so the ML labeler keeps label=1."""
        deal = DealFactory(
            campaign=campaign,
            lead=LeadFactory(email="p@corp.com"),
            state=DealState.EMAILED,
        )
        suppress_email("p@corp.com")
        deal.refresh_from_db()
        assert deal.outcome == ""

    def test_already_closed_deal_keeps_its_outcome(self, campaign):
        """An opt-out weeks after a thread ended must not erase how it ended."""
        deal = DealFactory(
            campaign=campaign,
            lead=LeadFactory(email="p@corp.com"),
            state=DealState.COMPLETED,
            outcome=Outcome.CONVERTED,
        )
        suppress_email("p@corp.com")
        deal.refresh_from_db()
        assert (deal.state, deal.outcome) == (DealState.COMPLETED, Outcome.CONVERTED)

    def test_unknown_address_is_not_an_error(self, campaign):
        assert suppress_email("stranger@corp.com") == 0

    def test_blank_address_suppresses_nothing(self, campaign):
        """A lead with no resolved email must not match every unemailed lead."""
        LeadFactory(email=None)
        assert suppress_email("") == 0

    def test_is_idempotent(self, campaign):
        deal = DealFactory(
            campaign=campaign,
            lead=LeadFactory(email="p@corp.com"),
            state=DealState.EMAILED,
        )
        assert suppress_email("p@corp.com") == suppress_email("p@corp.com") == 1
        deal.refresh_from_db()
        assert deal.state == DealState.UNSUBSCRIBED


# ── Detection: the client-generated unsubscribe (the mail pass) ───


def _read(box, fake) -> int:
    """Run one mail pass against *fake*; return the number of leads suppressed."""
    with patch("openoutreach.emails.inbox.imaplib.IMAP4_SSL", return_value=fake):
        return read_mail(box)[1]


@pytest.mark.django_db
class TestAliasOptOut:
    """A client's unsubscribe button mints a fresh message with no threading
    headers at all, so the thread reader can never see it. The alias is the
    whole signal."""

    def test_an_alias_message_suppresses_its_sender(self, campaign):
        lead = LeadFactory(email="p@corp.com")
        deal = DealFactory(campaign=campaign, lead=lead, state=DealState.EMAILED)

        assert _read(_box(), FakeIMAP([message(7, to=ALIAS, sender="p@corp.com")])) == 1

        lead.refresh_from_db()
        deal.refresh_from_db()
        assert lead.disqualified
        assert deal.state == DealState.UNSUBSCRIBED

    def test_ordinary_inbox_mail_is_left_alone(self, campaign):
        lead = LeadFactory(email="p@corp.com")
        assert _read(_box(), FakeIMAP([message(7, to=SENDER, sender="p@corp.com")])) == 0
        lead.refresh_from_db()
        assert not lead.disqualified

    def test_a_display_name_around_the_alias_still_matches(self, campaign):
        LeadFactory(email="p@corp.com")
        to = f'"Unsubscribe" <{ALIAS}>'
        assert _read(_box(), FakeIMAP([message(7, to=to, sender="P@corp.com")])) == 1

    def test_an_opt_out_body_is_never_fetched(self, campaign):
        """The alias is decided from headers alone — no body is pulled for it."""
        LeadFactory(email="p@corp.com")
        fake = FakeIMAP([message(7, to=ALIAS, sender="p@corp.com")])
        _read(_box(), fake)
        assert fake.body_fetches == []

    def test_the_cursor_advances_past_everything_examined(self, campaign):
        box = _box()
        _read(box, FakeIMAP([message(7, to=SENDER, sender="x@corp.com")]))
        box.refresh_from_db()
        assert box.unsub_scan_uid == 7
        assert box.unsub_scan_uidvalidity == 1

    def test_a_second_pass_resumes_above_the_cursor(self, campaign):
        box = _box()
        fake = FakeIMAP([message(7, to=ALIAS, sender="p@corp.com")])
        _read(box, fake)
        _read(box, fake)
        assert fake.searched_ranges == ["1:*", "8:*"]

    def test_rereading_the_same_message_changes_nothing(self, campaign):
        """Re-reading a box must be free — suppression is written to the same
        values, not accumulated."""
        lead = LeadFactory(email="p@corp.com")
        deal = DealFactory(campaign=campaign, lead=lead, state=DealState.EMAILED)
        box = _box()
        fake = FakeIMAP([message(7, to=ALIAS, sender="p@corp.com")])

        assert _read(box, fake) == 1
        box.unsub_scan_uid = 0  # as a UIDVALIDITY change would leave it
        assert _read(box, fake) == 1

        deal.refresh_from_db()
        assert deal.state == DealState.UNSUBSCRIBED
        assert Lead.objects.filter(disqualified=True).count() == 1

    def test_a_changed_uidvalidity_restarts_the_walk(self, campaign):
        """Reissued UIDs make the stored cursor point at unrelated mail; trusting
        it would skip every opt-out — and now every reply — below it forever."""
        box = _box()
        _read(box, FakeIMAP([message(7, to=SENDER, sender="x@corp.com")], uidvalidity=1))
        fake = FakeIMAP([message(3, to=ALIAS, sender="p@corp.com")], uidvalidity=2)
        _read(box, fake)

        assert fake.searched_ranges == ["1:*"]
        box.refresh_from_db()
        assert box.unsub_scan_uidvalidity == 2

    def test_an_unreachable_box_keeps_its_cursor(self, campaign):
        """A network fault is not evidence that there was no mail to read."""
        box = _box(unsub_scan_uid=42, unsub_scan_uidvalidity=1)
        fake = FakeIMAP([])
        fake.login = MagicMock(side_effect=OSError("connection reset"))

        assert _read(box, fake) == 0
        box.refresh_from_db()
        assert box.unsub_scan_uid == 42


# ── Detection: the worded unsubscribe (the outreach agent) ────────


def _decision(action, **kwargs):
    return OutreachDecision(action=action, **kwargs)


def _replied_deal(campaign, email="p@corp.com"):
    """An EMAILED deal with an unanswered reply — what the reply step picks up."""
    from openoutreach.chat.models import ChatMessage

    deal = DealFactory(
        campaign=campaign,
        lead=LeadFactory(email=email),
        state=DealState.EMAILED,
        mailbox=_box(),
        email_subject="Hi",
        email_message_id="<root@infra.com>",
    )
    ChatMessage.objects.create(
        deal=deal, external_id="<reply@corp.com>", content="Please stop.",
        is_outgoing=False, creation_date=timezone.now(),
    )
    return deal


@pytest.mark.django_db
class TestWordedUnsubscribe:
    """A worded unsubscribe threads normally, so the alias scan can never see it —
    the agent reading every reply already can."""

    def test_a_suppress_decision_disqualifies_and_closes_the_deal(self, campaign):
        deal = _replied_deal(campaign)

        with patch("openoutreach.core.agents.outreach.run_outreach_agent",
                   return_value=_decision("suppress")), \
             patch("openoutreach.core.db.summaries.update_chat_summary"), \
             patch("openoutreach.emails.sender.send_email") as send:
            next_state = answer_reply(deal)

        deal.state = next_state
        deal.save()
        deal.refresh_from_db()
        assert deal.state == DealState.UNSUBSCRIBED
        assert deal.lead.disqualified
        send.assert_not_called()

    def test_the_deal_closes_even_when_the_address_matches_nothing(self, campaign):
        """``suppress_email`` is keyed on the address, the returned state on the
        deal. A lead with no resolved email would otherwise stay EMAILED with an
        unanswered reply — permanently actionable, re-decided every cycle."""
        deal = _replied_deal(campaign)
        Lead.objects.filter(pk=deal.lead.pk).update(email=None)

        with patch("openoutreach.core.agents.outreach.run_outreach_agent",
                   return_value=_decision("suppress")), \
             patch("openoutreach.core.db.summaries.update_chat_summary"):
            assert answer_reply(deal) == DealState.UNSUBSCRIBED


# ── Enforcement at the send call sites ───────────────────────────


@pytest.mark.django_db
class TestSendGuards:
    def test_suppressed_reads_the_row_not_the_in_memory_copy(self, campaign):
        lead = LeadFactory(email="p@corp.com")
        Lead.objects.filter(pk=lead.pk).update(disqualified=True)
        assert suppressed(lead)  # the stale in-memory copy still says False

    def test_a_first_email_is_not_sent_to_a_lead_suppressed_mid_run(self, campaign):
        """The agent runs for seconds — the query that selected this deal is
        already out of date by the time there is a message to send."""
        box = _box()
        deal = DealFactory(
            campaign=campaign,
            lead=LeadFactory(email="p@corp.com"),
            state=DealState.READY_TO_EMAIL,
        )

        def _suppress_then_decide(target):
            Lead.objects.filter(pk=target.lead.pk).update(disqualified=True)
            return _decision("send_message", subject="Hi", message="Body")

        with patch("openoutreach.core.agents.outreach.run_outreach_agent",
                   side_effect=_suppress_then_decide), \
             patch("openoutreach.core.db.summaries.materialize_profile_summary_if_missing"), \
             patch("openoutreach.emails.sender.send_email") as send:
            assert send_first_email(deal, box) is None

        send.assert_not_called()
        deal.refresh_from_db()
        assert deal.state == DealState.READY_TO_EMAIL

    def test_a_reply_is_not_sent_to_a_lead_suppressed_mid_run(self, campaign):
        deal = _replied_deal(campaign)

        def _suppress_then_decide(target):
            Lead.objects.filter(pk=target.lead.pk).update(disqualified=True)
            return _decision("send_message", message="Body")

        with patch("openoutreach.core.agents.outreach.run_outreach_agent",
                   side_effect=_suppress_then_decide), \
             patch("openoutreach.core.db.summaries.update_chat_summary"), \
             patch("openoutreach.emails.sender.send_email") as send:
            assert answer_reply(deal) is None

        send.assert_not_called()
        deal.refresh_from_db()
        assert deal.state == DealState.EMAILED
