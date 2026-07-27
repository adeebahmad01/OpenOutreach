# openoutreach/emails/delivery_policy.py
"""Read the receiver's answer to a send, and decide what it means.

Every send ends in a verdict from the receiving server, and until now that
verdict was thrown away: ``sender.py`` let the exception propagate, the task was
marked FAILED, and the SMTP code — the only direct statement anyone makes about
this mailbox's standing — was lost in the traceback.

The distinction this module exists to draw is that **a failed send is not one
kind of event**. Three things arrive down the same exception channel and mean
completely different things:

- the receiver deferring us (4xx) — *slow down*, and it expects us to retry;
  routine, and a sporadic one costs nothing
- the receiver refusing us (5xx) — either we hit the real daily ceiling or the
  box has been actioned; the two need opposite responses
- nothing to do with the receiver at all — a dropped socket, a rejected
  password. These say nothing whatsoever about reputation, and treating them as
  if they did means a flaky network quietly throttles a healthy mailbox.

There is deliberately no retry ladder here and no rate threshold. A deferred
cold opener is not a message we have accepted responsibility for: the deal stays
READY_TO_EMAIL and ``reconcile`` re-mints it, already spaced by the send pacing.
And capacity does not need an explicit cut — a box that sends less leaves less
in its Sent folder, which ``warmth.py`` reads back the next day.
"""
from __future__ import annotations

import logging
import re
import smtplib
from dataclasses import dataclass

from django.db import models

logger = logging.getLogger(__name__)


class Response(models.TextChoices):
    """What the far end said, reduced to the cases we act on differently."""

    DEFERRED = "deferred", "Deferred (4xx)"
    QUOTA_EXCEEDED = "quota_exceeded", "Daily quota exceeded"
    BLOCKED = "blocked", "Blocked for reputation"
    REFUSED = "refused", "Refused (5xx)"
    AUTH_FAILED = "auth_failed", "Authentication failed"
    TRANSPORT = "transport", "Transport failure (no SMTP response)"


@dataclass(frozen=True)
class Policy:
    """What a ``Response`` means for the mailbox.

    ``from_receiver`` is the load-bearing flag: only a verdict the receiving
    server actually issued is evidence about this box. It gates the growth check
    in ``warmth.py``, so a socket timeout can never be mistaken for pushback.

    ``pause_today`` stops the box for the rest of the day. ``needs_operator``
    marks the case no amount of backing off will fix.
    """

    from_receiver: bool
    pause_today: bool = False
    needs_operator: bool = False


POLICIES: dict[str, Policy] = {
    # Routine. The receiver is pacing us and expects a later retry; reconcile
    # already provides one. Counts as pushback so capacity stops growing.
    Response.DEFERRED: Policy(from_receiver=True),
    # The receiver stating its own daily ceiling — the one number our measured
    # capacity cannot discover on its own. Believe it over our measurement.
    Response.QUOTA_EXCEEDED: Policy(from_receiver=True, pause_today=True),
    # A reputation action. Sending slower does not undo it, so stop and escalate.
    Response.BLOCKED: Policy(from_receiver=True, pause_today=True, needs_operator=True),
    # Some other permanent refusal — usually about the recipient, not us.
    Response.REFUSED: Policy(from_receiver=True),
    # Credentials, not standing. The box is unusable until repaired, but its
    # reputation is untouched — do not let this depress measured capacity.
    Response.AUTH_FAILED: Policy(from_receiver=False, pause_today=True, needs_operator=True),
    # We never reached the receiver. No information about anything.
    Response.TRANSPORT: Policy(from_receiver=False),
}


def policy_for(response: str) -> Policy:
    """The policy for a ``Response``; transport (no information) if unrecognised."""
    return POLICIES.get(response, POLICIES[Response.TRANSPORT])


# Derived from the table above rather than listed again, so a policy can only be
# changed in one place.
PAUSING_RESPONSES = frozenset(r for r, p in POLICIES.items() if p.pause_today)
RECEIVER_RESPONSES = frozenset(r for r, p in POLICIES.items() if p.from_receiver)


# ── Classification ────────────────────────────────────────────────────

# Gmail reports the enhanced status inline ("550-5.4.5 Daily user sending quota
# exceeded"), and it is more specific than the bare code — 5.4.5 and 5.7.1 are
# both 550 but mean entirely different things.
_ENHANCED_STATUS = re.compile(rb"\b([2-5])\.(\d{1,3})\.(\d{1,3})\b")


def classify(exc: Exception) -> tuple[str, int | None, str]:
    """Classify a failed send as ``(Response, smtp_code, detail)``.

    ``smtp_code`` is None when the failure never produced an SMTP response,
    which is itself the signal that the failure carries no reputation meaning.
    """
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return Response.AUTH_FAILED, exc.smtp_code, _detail(exc.smtp_error)

    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        # Per-recipient dict rather than a single response; we send to one
        # recipient at a time, so the first entry is the whole story.
        code, error = next(iter(exc.recipients.values()), (None, b""))
        return _from_code(code, error), code, _detail(error)

    if isinstance(exc, smtplib.SMTPResponseException):
        return _from_code(exc.smtp_code, exc.smtp_error), exc.smtp_code, _detail(exc.smtp_error)

    return Response.TRANSPORT, None, _detail(str(exc))


def _from_code(code: int | None, error) -> str:
    """Map an SMTP code plus its enhanced status onto a ``Response``."""
    if code is None:
        return Response.TRANSPORT
    if 400 <= code < 500:
        return Response.DEFERRED
    if code < 500:
        return Response.TRANSPORT

    status = _enhanced_status(error)
    if status == (5, 4, 5):
        return Response.QUOTA_EXCEEDED
    if status and status[1] == 7:
        # 5.7.x is the policy/reputation class — blocked, not merely refused.
        return Response.BLOCKED
    return Response.REFUSED


def _enhanced_status(error) -> tuple[int, int, int] | None:
    """The ``(class, subject, detail)`` enhanced status in a server reply, if any."""
    raw = error if isinstance(error, bytes) else str(error).encode("utf-8", "replace")
    match = _ENHANCED_STATUS.search(raw)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _detail(error) -> str:
    """The server's own words, decoded and trimmed for storage."""
    if isinstance(error, bytes):
        error = error.decode("utf-8", errors="replace")
    return " ".join(str(error).split())[:255]


# ── Recording ─────────────────────────────────────────────────────────


def record_failure(mailbox, exc: Exception):
    """Classify a failed send, persist the verdict, and return its ``Policy``.

    The single entry point: callers hand over the exception and act on the
    policy, rather than each deciding for itself what a code means.
    """
    from openoutreach.emails.models import SendVerdict

    response, code, detail = classify(exc)
    SendVerdict.objects.create(
        mailbox=mailbox, response=response, smtp_code=code, detail=detail,
    )
    policy = policy_for(response)
    logger.warning("send verdict on %s: %s (code=%s) %s",
                   mailbox.from_address, response, code, detail)
    if policy.needs_operator:
        logger.error("%s needs attention — %s: %s", mailbox.from_address, response, detail)
    return policy
