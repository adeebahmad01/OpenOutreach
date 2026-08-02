# openoutreach/emails/sender.py
"""Send one outbound email through a Mailbox's SMTP credentials.

No error handling by design: a failed send raises and the EMAIL task is marked
FAILED by the daemon, then retried on the next cycle. The mailbox is left
untouched — re-import with fixed credentials to repair a dead box.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

logger = logging.getLogger(__name__)

SMTP_TIMEOUT_SECONDS = 30


def send_email(
    mailbox,
    to_address: str,
    subject: str,
    body: str,
    *,
    bcc: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    """Send ``body`` from ``mailbox`` to ``to_address``; return the Message-ID.

    The mailbox's signature and the product attribution line are appended to
    ``body`` here rather than at the call sites, so every send — opener and
    follow-up — carries both, in the order body → signature → attribution.

    ``bcc`` (when set) blind-copies the operator's own address so they keep a
    private record of every send; ``send_message`` strips the Bcc header before
    transmission, so the To recipient never sees it. Call sites get it from
    ``operator_bcc`` rather than deciding for themselves.

    ``in_reply_to``/``references`` thread a reply onto an existing email thread
    (both are prior Message-IDs). The returned Message-ID is stored on the
    outgoing ChatMessage so the next touch can thread onto it.
    """
    message = _build_message(mailbox, to_address, subject, body, bcc, in_reply_to, references)
    _deliver(mailbox, message)
    logger.info("email sent from %s to %s: %s [%s]",
                mailbox.from_address, to_address, subject, message["Message-ID"])
    return message["Message-ID"]


def operator_bcc(user, campaign) -> str | None:
    """The address to blind-copy on this campaign's sends, or None for no copy.

    The operator gets a private copy of every send on **their own** campaigns —
    it is their outreach, from their mailbox, and they need the thread in their
    inbox. On a **freemium** campaign the outreach is OpenOutreach's own, so
    there is no copy to give: the operator is not a party to that conversation
    and their inbox is not a log for it.
    """
    if campaign.is_freemium:
        return None
    return user.email or None


# ── Message assembly ──────────────────────────────────────────────


def _build_message(mailbox, to_address, subject, body, bcc, in_reply_to, references) -> EmailMessage:
    """Assemble the email with threading headers and a domain-anchored Message-ID."""
    message = EmailMessage()
    message["Message-ID"] = _mint_message_id(mailbox.from_address)
    message["From"] = mailbox.from_address
    message["To"] = to_address
    if bcc:
        message["Bcc"] = bcc
    message["Subject"] = subject
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = references or in_reply_to
    message.set_content(_attribute(_sign(body, mailbox.signature)))
    return message


def _sign(body: str, signature: str | None) -> str:
    """Append the mailbox's sign-off, separated by a blank line.

    Declined ("") or never asked (None) ⇒ body unchanged.
    """
    signature = (signature or "").strip()
    if not signature:
        return body
    return f"{body.rstrip()}\n\n{signature}\n"


ATTRIBUTION = "Sent with OpenOutreach"


def _attribute(body: str) -> str:
    """Append the product attribution line, separated by two blank lines.

    Always on, last in the message (after the signature): every recipient of an
    outbound email is a plausible future operator. It names the product without
    linking it — a bare name reads as a footer, a URL reads as an ad, and anyone
    curious enough to act on it can search.
    """
    return f"{body.rstrip()}\n\n\n{ATTRIBUTION}\n"


def _mint_message_id(from_address: str) -> str:
    """A unique RFC-5322 Message-ID anchored to the sending domain.

    Anchoring to the From domain (rather than ``make_msgid``'s default local
    hostname) keeps the Message-ID aligned with the sender and avoids leaking
    the container hostname.
    """
    domain = from_address.rsplit("@", 1)[-1]
    return make_msgid(domain=domain)


# ── Transport ─────────────────────────────────────────────────────


def _deliver(mailbox, message: EmailMessage) -> None:
    """Log into the mailbox over SMTP+STARTTLS and send one message.

    A failure is recorded as a ``SendVerdict`` on the way past and then re-raised
    unchanged: the task still fails and is still retried, but the receiver's
    answer — the one direct statement anyone makes about this mailbox's standing
    — is kept instead of dying in the traceback.
    """
    from openoutreach.emails.delivery_policy import record_failure

    try:
        with smtplib.SMTP(mailbox.host, mailbox.port, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
            smtp.starttls()
            smtp.login(mailbox.username, mailbox.password)
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        record_failure(mailbox, exc)
        raise
