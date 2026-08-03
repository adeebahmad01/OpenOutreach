# tests/emails/test_unsubscribe.py
"""Opt-out: the advertised mechanism, both detection paths, and the enforcement.

The three legs, and what each one locks down:

  * **Advertised** — every send carries the ``List-Unsubscribe`` header *and* a
    visible reply-line. The header is what receiving filters read; the line
    reaches the clients that render no unsubscribe button of their own. Both are
    asserted on the same message, since either alone leaves someone with no exit
    but the spam button.
  * **Detected** — a client-generated unsubscribe (no threading headers, found
    box-wide by the ``+unsub`` alias) and a worded one (threads normally, read by
    the follow-up agent) both reach the same suppression.
  * **Enforced** — suppression binds to the person, so every lead holding the
    address is disqualified and every open deal closes at UNSUBSCRIBED, while an
    already-closed deal keeps the outcome that closed it.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from openoutreach.core import scheduler
from openoutreach.core.agents.outreach import OutreachDecision
from openoutreach.core.db.leads import suppress_email
from openoutreach.crm.models import DealState, Lead, Outcome
from openoutreach.emails.inbox import scan_unsubscribes
from openoutreach.emails.models import Mailbox
from openoutreach.emails.sender import (
    ATTRIBUTION,
    OPT_OUT_LINE,
    send_email,
    suppressed,
    unsubscribe_address,
)
from openoutreach.emails.tasks.follow_up import handle_follow_up
from openoutreach.emails.tasks.send import handle_email
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
    def test_suppresses_every_lead_holding_the_address(self, fake_session):
        """``Lead.email`` has no unique constraint — one address, many rows."""
        first = LeadFactory(email="p@corp.com")
        second = LeadFactory(email="p@corp.com")
        assert suppress_email("p@corp.com") == 2
        assert Lead.objects.filter(pk__in=[first.pk, second.pk], disqualified=True).count() == 2

    def test_matches_case_insensitively(self, fake_session):
        """A client echoes back whatever casing it was given."""
        lead = LeadFactory(email="p@corp.com")
        suppress_email("P@Corp.Com")
        lead.refresh_from_db()
        assert lead.disqualified

    def test_open_deal_closes_at_unsubscribed(self, fake_session):
        deal = DealFactory(
            campaign=fake_session.campaign,
            lead=LeadFactory(email="p@corp.com"),
            state=DealState.EMAILED,
        )
        suppress_email("p@corp.com")
        deal.refresh_from_db()
        assert deal.state == DealState.UNSUBSCRIBED

    def test_unsubscribed_deal_leaves_outcome_blank(self, fake_session):
        """Reachability ended, not the offer — so the ML labeler keeps label=1."""
        deal = DealFactory(
            campaign=fake_session.campaign,
            lead=LeadFactory(email="p@corp.com"),
            state=DealState.EMAILED,
        )
        suppress_email("p@corp.com")
        deal.refresh_from_db()
        assert deal.outcome == ""

    def test_already_closed_deal_keeps_its_outcome(self, fake_session):
        """An opt-out weeks after a thread ended must not erase how it ended."""
        deal = DealFactory(
            campaign=fake_session.campaign,
            lead=LeadFactory(email="p@corp.com"),
            state=DealState.COMPLETED,
            outcome=Outcome.CONVERTED,
        )
        suppress_email("p@corp.com")
        deal.refresh_from_db()
        assert (deal.state, deal.outcome) == (DealState.COMPLETED, Outcome.CONVERTED)

    def test_unknown_address_is_not_an_error(self, fake_session):
        assert suppress_email("stranger@corp.com") == 0

    def test_blank_address_suppresses_nothing(self, fake_session):
        """A lead with no resolved email must not match every unemailed lead."""
        LeadFactory(email=None)
        assert suppress_email("") == 0

    def test_is_idempotent(self, fake_session):
        deal = DealFactory(
            campaign=fake_session.campaign,
            lead=LeadFactory(email="p@corp.com"),
            state=DealState.EMAILED,
        )
        assert suppress_email("p@corp.com") == suppress_email("p@corp.com") == 1
        deal.refresh_from_db()
        assert deal.state == DealState.UNSUBSCRIBED


# ── Detection: the client-generated unsubscribe (box-wide scan) ───


class FakeIMAP:
    """A minimal IMAP server holding one INBOX of ``(uid, to, from)`` messages.

    Only what ``scan_unsubscribes`` calls: STATUS for the UID epoch, SELECT, a
    UID SEARCH that honours both the ``UID lo:*`` range and the ``HEADER To``
    substring match, and a headers-only UID FETCH.
    """

    def __init__(self, messages, uidvalidity=1):
        self.messages = messages  # [(uid, to_header, from_header)]
        self.uidvalidity = uidvalidity
        self.searched_ranges = []

    def login(self, username, password):
        return "OK", []

    def status(self, mailbox, attributes):
        uidnext = max((uid for uid, _, _ in self.messages), default=0) + 1
        return "OK", [
            f'INBOX (UIDVALIDITY {self.uidvalidity} UIDNEXT {uidnext})'.encode()
        ]

    def select(self, mailbox, readonly=False):
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "SEARCH":
            return self._search(args)
        return self._fetch(args)

    def _search(self, args):
        _, _, uid_range, _, _, needle = args
        low = int(uid_range.split(":")[0])
        self.searched_ranges.append(uid_range)
        hits = [uid for uid, to, _ in self.messages if uid >= low and needle in to]
        return "OK", [" ".join(str(uid) for uid in hits).encode()]

    def _fetch(self, args):
        uid = int(args[0])
        sender = next(f for u, _, f in self.messages if u == uid)
        return "OK", [(b"1 (UID)", f"From: {sender}\r\n\r\n".encode())]

    def close(self):
        pass

    def logout(self):
        pass


def _run_scan(box, fake) -> int:
    with patch("openoutreach.emails.inbox.imaplib.IMAP4_SSL", return_value=fake):
        return scan_unsubscribes(box)


@pytest.mark.django_db
class TestScanUnsubscribes:
    def test_alias_message_suppresses_its_sender(self, fake_session):
        lead = LeadFactory(email="p@corp.com")
        deal = DealFactory(
            campaign=fake_session.campaign, lead=lead, state=DealState.EMAILED,
        )
        assert _run_scan(_box(), FakeIMAP([(7, ALIAS, "p@corp.com")])) == 1
        lead.refresh_from_db()
        deal.refresh_from_db()
        assert lead.disqualified
        assert deal.state == DealState.UNSUBSCRIBED

    def test_ordinary_inbox_mail_is_left_alone(self, fake_session):
        """The alias is the whole signal — a reply to the sending address is a
        reply, and belongs to the thread reader, not here."""
        lead = LeadFactory(email="p@corp.com")
        assert _run_scan(_box(), FakeIMAP([(7, SENDER, "p@corp.com")])) == 0
        lead.refresh_from_db()
        assert not lead.disqualified

    def test_display_name_around_the_alias_still_matches(self, fake_session):
        LeadFactory(email="p@corp.com")
        to = f'"Unsubscribe" <{ALIAS}>'
        assert _run_scan(_box(), FakeIMAP([(7, to, "P@corp.com")])) == 1

    def test_cursor_advances_past_everything_examined(self, fake_session):
        box = _box()
        _run_scan(box, FakeIMAP([(7, SENDER, "x@corp.com")]))
        box.refresh_from_db()
        assert box.unsub_scan_uid == 7
        assert box.unsub_scan_uidvalidity == 1

    def test_second_scan_resumes_above_the_cursor(self, fake_session):
        box = _box()
        fake = FakeIMAP([(7, ALIAS, "p@corp.com")])
        _run_scan(box, fake)
        _run_scan(box, fake)
        assert fake.searched_ranges == ["1:*", "8:*"]

    def test_rescanning_the_same_message_changes_nothing(self, fake_session):
        """Re-reading a mailbox (a reissued UID epoch, a restored cursor) must be
        free — suppression is written to the same values, not accumulated."""
        lead = LeadFactory(email="p@corp.com")
        deal = DealFactory(
            campaign=fake_session.campaign, lead=lead, state=DealState.EMAILED,
        )
        box = _box()
        fake = FakeIMAP([(7, ALIAS, "p@corp.com")])
        assert _run_scan(box, fake) == 1
        box.unsub_scan_uid = 0  # as a UIDVALIDITY change would leave it
        assert _run_scan(box, fake) == 1
        deal.refresh_from_db()
        assert deal.state == DealState.UNSUBSCRIBED
        assert Lead.objects.filter(disqualified=True).count() == 1

    def test_changed_uidvalidity_restarts_the_scan(self, fake_session):
        """Reissued UIDs make the stored cursor point at unrelated mail; trusting
        it would skip every opt-out below it forever."""
        box = _box()
        _run_scan(box, FakeIMAP([(7, SENDER, "x@corp.com")], uidvalidity=1))
        fake = FakeIMAP([(3, ALIAS, "p@corp.com")], uidvalidity=2)
        _run_scan(box, fake)
        assert fake.searched_ranges == ["1:*"]
        box.refresh_from_db()
        assert box.unsub_scan_uidvalidity == 2

    def test_unreachable_box_keeps_its_cursor(self, fake_session):
        """A network fault is not evidence that there was no mail to read."""
        box = _box(unsub_scan_uid=42, unsub_scan_uidvalidity=1)
        fake = FakeIMAP([])
        fake.login = MagicMock(side_effect=OSError("connection reset"))
        assert _run_scan(box, fake) == 0
        box.refresh_from_db()
        assert box.unsub_scan_uid == 42


@pytest.mark.django_db
class TestScanCadence:
    """The scan is hourly, not per-reconcile: reconcile fires every few minutes."""

    @pytest.fixture(autouse=True)
    def _clear_cadence_cache(self):
        scheduler._unsub_scanned_at = None
        yield
        scheduler._unsub_scanned_at = None

    def test_second_reconcile_within_the_hour_does_not_rescan(self, fake_session):
        _box()
        with patch("openoutreach.emails.inbox.scan_unsubscribes") as scan:
            scheduler._scan_unsubscribes()
            scheduler._scan_unsubscribes()
        assert scan.call_count == 1


# ── Detection: the worded unsubscribe (follow-up agent) ───────────


def _decision(action, **kwargs):
    return OutreachDecision(action=action, follow_up_hours=24, **kwargs)


def _due_emailed_deal(session, email="p@corp.com"):
    """An EMAILED deal past its countdown — what the follow-up drain picks up."""
    return DealFactory(
        campaign=session.campaign,
        lead=LeadFactory(email=email),
        state=DealState.EMAILED,
        mailbox=_box(),
        email_subject="Hi",
        email_message_id="<root@infra.com>",
        next_follow_up_at=timezone.now() - timedelta(hours=1),
    )


@pytest.mark.django_db
class TestWordedUnsubscribe:
    def test_suppress_decision_disqualifies_and_closes_the_deal(self, fake_session):
        deal = _due_emailed_deal(fake_session)
        with patch("openoutreach.core.agents.outreach.run_outreach_agent",
                   return_value=_decision("suppress")), \
             patch("openoutreach.emails.sender.send_email") as send:
            handle_follow_up(None, fake_session, None)

        deal.refresh_from_db()
        assert deal.state == DealState.UNSUBSCRIBED
        assert deal.lead.disqualified
        send.assert_not_called()

    def test_deal_closes_even_when_the_address_matches_nothing(self, fake_session):
        """``suppress_email`` is keyed on the address, this branch on the deal. A
        lead with no resolved email would otherwise neither close nor re-arm its
        countdown — leaving it permanently due, re-deciding on every reconcile."""
        deal = _due_emailed_deal(fake_session)
        Lead.objects.filter(pk=deal.lead.pk).update(email=None)

        with patch("openoutreach.core.agents.outreach.run_outreach_agent",
                   return_value=_decision("suppress")):
            handle_follow_up(None, fake_session, None)

        deal.refresh_from_db()
        assert deal.state == DealState.UNSUBSCRIBED


# ── Enforcement at the send call sites ───────────────────────────


@pytest.mark.django_db
class TestSendGuards:
    def test_suppressed_reads_the_row_not_the_in_memory_copy(self, fake_session):
        lead = LeadFactory(email="p@corp.com")
        Lead.objects.filter(pk=lead.pk).update(disqualified=True)
        assert suppressed(lead)  # the stale in-memory copy still says False

    def test_opener_is_not_sent_to_a_lead_suppressed_mid_run(self, fake_session):
        """The agent runs for seconds and reads the inbox on its way past — the
        pool query that selected this deal is already out of date."""
        _box()
        deal = DealFactory(
            campaign=fake_session.campaign,
            lead=LeadFactory(email="p@corp.com"),
            state=DealState.READY_TO_EMAIL,
        )

        def _suppress_then_decide(session, target):
            Lead.objects.filter(pk=target.lead.pk).update(disqualified=True)
            return _decision("send_message", subject="Hi", message="Body")

        with patch("openoutreach.core.agents.outreach.run_outreach_agent",
                   side_effect=_suppress_then_decide), \
             patch("openoutreach.core.db.summaries.materialize_profile_summary_if_missing"), \
             patch("openoutreach.emails.sender.send_email") as send:
            handle_email(None, fake_session, None)

        send.assert_not_called()
        deal.refresh_from_db()
        assert deal.state == DealState.READY_TO_EMAIL

    def test_follow_up_reply_is_not_sent_to_a_lead_suppressed_mid_run(self, fake_session):
        deal = _due_emailed_deal(fake_session)

        def _suppress_then_decide(session, target):
            Lead.objects.filter(pk=target.lead.pk).update(disqualified=True)
            return _decision("send_message", message="Body")

        with patch("openoutreach.core.agents.outreach.run_outreach_agent",
                   side_effect=_suppress_then_decide), \
             patch("openoutreach.emails.sender.send_email") as send:
            handle_follow_up(None, fake_session, None)

        send.assert_not_called()
        deal.refresh_from_db()
        assert deal.state == DealState.EMAILED
