# tests/emails/test_mail_pass.py
"""The mail pass — one IMAP walk per box, matching replies back to their threads.

The opt-out half of the same walk is covered in ``test_unsubscribe.py``; this file
is about the reply half: a message reaches its deal by ``References``/``In-Reply-To``
carrying the first email's Message-ID, and nothing else does.
"""
from unittest.mock import patch

import pytest

from openoutreach.chat.models import ChatMessage
from openoutreach.crm.models import DealState
from openoutreach.emails.inbox import read_mail
from openoutreach.emails.models import Mailbox
from tests.emails.fake_imap import FakeIMAP, message
from tests.factories import DealFactory, LeadFactory

SENDER = "s@infra.com"
ROOT = "<root@infra.com>"


def _box(**kwargs) -> Mailbox:
    return Mailbox.objects.create(
        username=SENDER, password="pw", from_address=SENDER, daily_limit=10, **kwargs,
    )


def _emailed(campaign, box, email="p@corp.com", root=ROOT):
    return DealFactory(
        campaign=campaign,
        lead=LeadFactory(email=email),
        state=DealState.EMAILED,
        mailbox=box,
        email_subject="Hi",
        email_message_id=root,
    )


def _read(box, fake) -> int:
    """Run one mail pass; return the number of replies stored."""
    with patch("openoutreach.emails.inbox.imaplib.IMAP4_SSL", return_value=fake):
        return read_mail(box)[0]


@pytest.mark.django_db
class TestReplyThreading:
    def test_a_reply_lands_on_its_deal(self, campaign):
        box = _box()
        deal = _emailed(campaign, box)

        stored = _read(box, FakeIMAP([
            message(7, to=SENDER, sender="p@corp.com", references=ROOT,
                    body="Sure, happy to chat."),
        ]))

        assert stored == 1
        row = ChatMessage.objects.get(deal=deal, is_outgoing=False)
        assert "happy to chat" in row.content

    def test_in_reply_to_alone_is_enough(self, campaign):
        """Clients differ on which threading header they populate."""
        box = _box()
        deal = _emailed(campaign, box)

        assert _read(box, FakeIMAP([
            message(7, to=SENDER, sender="p@corp.com", in_reply_to=ROOT),
        ])) == 1
        assert ChatMessage.objects.filter(deal=deal, is_outgoing=False).count() == 1

    def test_a_bare_message_id_matches_a_stored_root_with_brackets(self, campaign):
        """What we stored is whatever SMTP handed back; servers differ on brackets."""
        box = _box()
        _emailed(campaign, box, root="root@infra.com")

        assert _read(box, FakeIMAP([
            message(7, to=SENDER, sender="p@corp.com", references=ROOT),
        ])) == 1

    def test_unrelated_mail_is_ignored(self, campaign):
        box = _box()
        _emailed(campaign, box)

        assert _read(box, FakeIMAP([
            message(7, to=SENDER, sender="newsletter@x.com"),
        ])) == 0

    def test_a_thread_from_another_box_is_not_folded_in(self, campaign):
        """Scoped to the box's own deals, so a quoted id we sent elsewhere can't
        attach a reply to the wrong thread."""
        box = _box()
        other = Mailbox.objects.create(
            username="o@infra.com", password="pw", from_address="o@infra.com")
        _emailed(campaign, other)

        assert _read(box, FakeIMAP([
            message(7, to=SENDER, sender="p@corp.com", references=ROOT),
        ])) == 0

    def test_our_own_copy_of_the_thread_is_not_stored_as_a_reply(self, campaign):
        box = _box()
        _emailed(campaign, box)

        assert _read(box, FakeIMAP([
            message(7, to="p@corp.com", sender=SENDER, references=ROOT),
        ])) == 0

    def test_rereading_the_same_reply_creates_no_duplicate(self, campaign):
        """Dedup is on ``(deal, Message-ID)``, so a restarted walk is free."""
        box = _box()
        deal = _emailed(campaign, box)
        fake = FakeIMAP([message(7, to=SENDER, sender="p@corp.com", references=ROOT)])

        assert _read(box, fake) == 1
        box.unsub_scan_uid = 0  # as a UIDVALIDITY change would leave it
        assert _read(box, fake) == 0  # upserted, not created again

        assert ChatMessage.objects.filter(deal=deal, is_outgoing=False).count() == 1

    def test_quoted_history_is_stripped(self, campaign):
        box = _box()
        deal = _emailed(campaign, box)

        _read(box, FakeIMAP([
            message(7, to=SENDER, sender="p@corp.com", references=ROOT,
                    body="My answer.\r\n\r\nOn Mon, Eracle wrote:\r\n> the opener"),
        ]))

        row = ChatMessage.objects.get(deal=deal, is_outgoing=False)
        assert row.content == "My answer."
        assert "the opener" not in row.content

    def test_a_body_is_fetched_only_for_a_message_we_track(self, campaign):
        """Most of a real inbox is neither a reply nor an opt-out; paying for those
        bodies is what would make walking the whole box expensive."""
        box = _box()
        _emailed(campaign, box)
        fake = FakeIMAP([
            message(7, to=SENDER, sender="stranger@x.com"),
            message(8, to=SENDER, sender="p@corp.com", references=ROOT),
        ])

        _read(box, fake)

        assert fake.body_fetches == [8]
