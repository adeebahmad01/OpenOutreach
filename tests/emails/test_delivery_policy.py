from __future__ import annotations

import smtplib

import pytest

from openoutreach.emails.delivery_policy import (
    Response,
    classify,
    policy_for,
    record_failure,
)
from openoutreach.emails.models import Mailbox, SendVerdict


def _box() -> Mailbox:
    return Mailbox.objects.create(
        username="a@b.com", password="pw", from_address="a@b.com",
    )


class TestClassify:
    def test_deferral_is_a_pacing_signal(self):
        exc = smtplib.SMTPDataError(421, b"4.7.0 Temporary System Problem. Try again later")
        response, code, detail = classify(exc)
        assert response == Response.DEFERRED
        assert code == 421
        assert "Try again later" in detail

    def test_quota_exceeded_is_distinguished_from_other_550s(self):
        # 5.4.5 and 5.7.1 are both 550; only the enhanced status tells them apart.
        exc = smtplib.SMTPDataError(550, b"5.4.5 Daily user sending quota exceeded")
        assert classify(exc)[0] == Response.QUOTA_EXCEEDED

    def test_policy_block_is_read_from_the_enhanced_status(self):
        exc = smtplib.SMTPDataError(550, b"5.7.1 Our system has detected ... blocked")
        assert classify(exc)[0] == Response.BLOCKED

    def test_other_permanent_refusal(self):
        exc = smtplib.SMTPDataError(550, b"5.1.1 The email account does not exist")
        assert classify(exc)[0] == Response.REFUSED

    def test_auth_failure_is_not_a_reputation_signal(self):
        exc = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
        response, _, _ = classify(exc)
        # 5.7.8 would otherwise read as a policy block — the exception type wins.
        assert response == Response.AUTH_FAILED
        assert not policy_for(response).from_receiver

    def test_recipients_refused_reads_the_per_recipient_code(self):
        exc = smtplib.SMTPRecipientsRefused({"x@y.com": (550, b"5.4.5 quota exceeded")})
        response, code, _ = classify(exc)
        assert response == Response.QUOTA_EXCEEDED
        assert code == 550

    def test_dropped_connection_carries_no_smtp_code(self):
        response, code, _ = classify(smtplib.SMTPServerDisconnected("connection closed"))
        assert response == Response.TRANSPORT
        assert code is None

    def test_socket_error_is_transport(self):
        assert classify(OSError("network unreachable"))[0] == Response.TRANSPORT

    def test_reply_without_an_enhanced_status_still_classifies(self):
        assert classify(smtplib.SMTPDataError(451, b"try later"))[0] == Response.DEFERRED
        assert classify(smtplib.SMTPDataError(554, b"rejected"))[0] == Response.REFUSED


class TestPolicies:
    def test_a_deferral_does_not_pause_the_box(self):
        # The whole point: a sporadic 421 is routine and costs no capacity.
        policy = policy_for(Response.DEFERRED)
        assert policy.from_receiver
        assert not policy.pause_today

    def test_quota_exceeded_pauses_the_box(self):
        assert policy_for(Response.QUOTA_EXCEEDED).pause_today

    def test_a_block_needs_the_operator(self):
        assert policy_for(Response.BLOCKED).needs_operator

    def test_transport_carries_no_information(self):
        policy = policy_for(Response.TRANSPORT)
        assert not policy.from_receiver
        assert not policy.pause_today


@pytest.mark.django_db
class TestRecordFailure:
    def test_persists_the_verdict_and_returns_its_policy(self):
        box = _box()
        policy = record_failure(
            box, smtplib.SMTPDataError(550, b"5.4.5 Daily user sending quota exceeded"),
        )
        assert policy.pause_today
        verdict = SendVerdict.objects.get(mailbox=box)
        assert verdict.response == Response.QUOTA_EXCEEDED
        assert verdict.smtp_code == 550

    def test_transport_failure_is_recorded_without_a_code(self):
        box = _box()
        record_failure(box, OSError("connection reset"))
        assert SendVerdict.objects.get(mailbox=box).smtp_code is None


@pytest.mark.django_db
class TestHeadroomRespectsVerdicts:
    def test_a_paused_box_has_no_headroom(self):
        box = _box()
        box.daily_limit = 40
        box.save()
        assert box.headroom_today() == 40
        SendVerdict.objects.create(
            mailbox=box, response=Response.QUOTA_EXCEEDED, smtp_code=550,
        )
        assert box.headroom_today() == 0

    def test_a_deferral_leaves_headroom_intact(self):
        box = _box()
        box.daily_limit = 40
        box.save()
        SendVerdict.objects.create(mailbox=box, response=Response.DEFERRED, smtp_code=421)
        assert box.headroom_today() == 40

    def test_a_paused_box_is_not_offered_for_sending(self):
        box = _box()
        box.daily_limit = 40
        box.save()
        SendVerdict.objects.create(
            mailbox=box, response=Response.BLOCKED, smtp_code=550,
        )
        assert Mailbox.objects.remaining_today() == 0
        assert Mailbox.objects.free_for_first_email() is None

    def test_sending_falls_through_to_an_unpaused_box(self):
        paused = _box()
        paused.daily_limit = 40
        paused.save()
        SendVerdict.objects.create(
            mailbox=paused, response=Response.QUOTA_EXCEEDED, smtp_code=550,
        )
        healthy = Mailbox.objects.create(
            username="c@d.com", password="pw", from_address="c@d.com",
            daily_limit=10,
        )
        assert Mailbox.objects.free_for_first_email() == healthy
        assert Mailbox.objects.remaining_today() == 10
