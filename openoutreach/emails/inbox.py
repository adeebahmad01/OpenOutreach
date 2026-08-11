# openoutreach/emails/inbox.py
"""The mail pass — one IMAP walk per mailbox, for everything that arrives in it.

Two kinds of inbound mail matter, and they used to be read by two different
readers on two different schedules:

- a **reply**, which threads (its ``References``/``In-Reply-To`` carries the
  opener's Message-ID) and becomes an inbound ``ChatMessage`` on that deal;
- an **opt-out**, which does *not* thread — a mail client's unsubscribe button
  mints a fresh message with no threading headers at all — and is found instead by
  the ``+unsub`` alias it was addressed to.

Both are now one pass over the UIDs above ``Mailbox.unsub_scan_uid``. The reply
reader used to log into IMAP and run a header search **per deal**, which meant the
cost of asking "did anyone answer?" grew with the number of open threads and was
paid before the daemon knew whether there was anything to answer. Reading the box
once instead makes that cost the size of the *new mail*, which is what it actually
is — and it is the only reason an install can leave every unanswered thread open
forever without paying for them.

Nothing here decides anything. The pass writes rows; a deal whose newest inbound
message is newer than its newest outgoing one is what the cycle then serves.

**The cursor now gates replies as well as opt-outs.** That is safe in both
directions: a changed ``UIDVALIDITY`` restarts the walk from zero rather than
trusting a cursor that now points at unrelated mail, and every write is an upsert
keyed on ``(deal, Message-ID)``, so re-reading a message can only rewrite the row
it already wrote. The cursor advances to ``UIDNEXT - 1`` — the box's own high-water
mark — rather than to the last *interesting* message, because the interesting ones
are rare and anchoring on them would re-walk the whole tail every pass.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime

logger = logging.getLogger(__name__)

IMAP_TIMEOUT_SECONDS = 30

# Headers a plus-addressed alias can land in. Clients rewrite recipients freely and
# some providers only record the tagged address in a delivery header, so an opt-out
# is looked for in all of them rather than in ``To`` alone.
_RECIPIENT_HEADERS = ("To", "Cc", "Delivered-To", "X-Original-To", "Envelope-To")

# Message-IDs as they appear inside References / In-Reply-To.
_MESSAGE_ID = re.compile(r"<[^<>@\s]+@[^<>@\s]+>")


def read_mail(mailbox) -> tuple[int, int]:
    """Walk this box's new mail once. Returns ``(replies stored, leads suppressed)``.

    Best-effort: an unreachable box logs and returns ``(0, 0)`` with its cursor
    untouched, so nothing is skipped when the network is at fault rather than the
    mail. The cursor only moves after the walk completes.
    """
    imap = imaplib.IMAP4_SSL(mailbox.imap_host, mailbox.imap_port, timeout=IMAP_TIMEOUT_SECONDS)
    try:
        imap.login(mailbox.username, mailbox.password)
        uidvalidity, uidnext = _uid_state(imap)
        if uidvalidity is None:
            logger.warning("mail pass: %s did not report UIDVALIDITY", mailbox.from_address)
            return 0, 0
        start = _resume_from(mailbox, uidvalidity)
        status, _ = imap.select("INBOX", readonly=True)
        if status != "OK":
            logger.warning("mail pass: cannot select INBOX on %s", mailbox.from_address)
            return 0, 0
        replies, suppressed = _walk(imap, mailbox, start)
    except (imaplib.IMAP4.error, OSError) as exc:
        logger.warning("mail pass: could not read %s (%s)", mailbox.from_address, exc)
        return 0, 0
    finally:
        _logout(imap)

    _advance_cursor(mailbox, uidnext, uidvalidity)
    if replies or suppressed:
        logger.info("mail pass: %s read UIDs >%d — %d reply(ies), %d suppressed",
                    mailbox.from_address, start, replies, suppressed)
    else:
        logger.debug("mail pass: %s read UIDs >%d — nothing new", mailbox.from_address, start)
    return replies, suppressed


def _walk(imap, mailbox, start_uid: int) -> tuple[int, int]:
    """Classify every message above *start_uid*: opt-out, reply to a thread, or neither.

    Headers are read first and the body is fetched only for a message that turns out
    to be a reply we are tracking — most of a real inbox is neither, and paying for
    those bodies is what would make walking the whole box expensive.
    """
    from openoutreach.emails.sender import unsubscribe_address

    alias = unsubscribe_address(mailbox.from_address)
    replies = suppressed = 0

    for uid in _new_uids(imap, start_uid):
        headers = _headers_of(imap, uid)
        if headers is None:
            continue
        if _addressed_to_alias(headers, alias):
            suppressed += _suppress_sender(headers)
            continue
        deal = _deal_for_thread(mailbox, headers)
        if deal is None:
            continue
        if _store_reply(deal, mailbox, _message_of(imap, uid) or headers):
            replies += 1

    return replies, suppressed


# ── Classification ────────────────────────────────────────────────


def _addressed_to_alias(msg: Message, alias: str) -> bool:
    """True when the ``+unsub`` alias appears in any recipient header."""
    alias = alias.lower()
    return any(
        alias in (msg.get(header) or "").lower() for header in _RECIPIENT_HEADERS
    )


def _suppress_sender(msg: Message) -> int:
    """Suppress every lead holding this message's From address. Returns leads suppressed."""
    from openoutreach.core.db.leads import suppress_email

    sender = parseaddr(msg.get("From", ""))[1].lower()
    if not sender:
        return 0
    return suppress_email(sender)


def _deal_for_thread(mailbox, msg: Message):
    """The Deal this message replies to, or None.

    Matches the thread root (``Deal.email_message_id``, the opener's Message-ID)
    against every id in ``References``/``In-Reply-To``. Scoped to this box's own
    deals, so a message that happens to quote an id we sent from a *different*
    mailbox is not folded into the wrong thread.
    """
    from openoutreach.crm.models import Deal

    ids = _referenced_ids(msg)
    if not ids:
        return None
    return (
        Deal.objects.filter(mailbox=mailbox, email_message_id__in=ids)
        .select_related("lead", "campaign", "mailbox")
        .first()
    )


def _referenced_ids(msg: Message) -> list[str]:
    """Every Message-ID referenced by this message, with and without angle brackets.

    Both forms are returned because what we stored is whatever the SMTP server
    handed back at send time, and servers differ on the brackets.
    """
    raw = " ".join(filter(None, (msg.get("References"), msg.get("In-Reply-To"))))
    bracketed = _MESSAGE_ID.findall(raw)
    return bracketed + [mid.strip("<>") for mid in bracketed]


# ── Message → ChatMessage ─────────────────────────────────────────


def _store_reply(deal, mailbox, msg: Message) -> bool:
    """Upsert an inbound reply as a ChatMessage. Returns True only if newly created.

    Skips our own outbound copies (From == the sending box) and messages with no
    Message-ID or empty body. Dedup key is ``(deal, external_id=reply Message-ID)``,
    so the walk is idempotent without trusting IMAP ``\\Seen`` flags.
    """
    from openoutreach.chat.models import ChatMessage
    from openoutreach.core.operator import get_active_user

    message_id = (msg.get("Message-ID") or "").strip()
    from_addr = parseaddr(msg.get("From", ""))[1].lower()
    if not message_id or from_addr == (mailbox.from_address or "").lower():
        return False

    body = _plain_text_body(msg)
    if not body:
        return False

    sent_at = _sent_at(msg)
    _, created = ChatMessage.objects.update_or_create(
        deal=deal,
        external_id=message_id,
        defaults={
            "content": body,
            "is_outgoing": False,
            "owner": get_active_user(),
            **({"creation_date": sent_at} if sent_at else {}),
        },
    )
    if created:
        logger.info("reply from %s on deal %s", from_addr, deal.pk)
    return created


# ── UID cursor ────────────────────────────────────────────────────


def _resume_from(mailbox, uidvalidity: int) -> int:
    """The UID to resume above — 0 when the server has reissued its UIDs.

    A changed ``UIDVALIDITY`` means the stored cursor now points at unrelated mail.
    Restarting costs one full pass over a box; trusting the stale cursor would
    silently skip every reply and every opt-out below it, forever.
    """
    if uidvalidity == mailbox.unsub_scan_uidvalidity:
        return mailbox.unsub_scan_uid
    if mailbox.unsub_scan_uidvalidity:
        logger.info("mail pass: %s UIDVALIDITY %d → %d, rereading from the start",
                    mailbox.from_address, mailbox.unsub_scan_uidvalidity, uidvalidity)
    return 0


def _advance_cursor(mailbox, uidnext: int, uidvalidity: int) -> None:
    """Persist the walk's new resume point — everything below ``UIDNEXT`` is read."""
    mailbox.unsub_scan_uid = max(mailbox.unsub_scan_uid, uidnext - 1)
    mailbox.unsub_scan_uidvalidity = uidvalidity
    mailbox.save(update_fields=["unsub_scan_uid", "unsub_scan_uidvalidity"])


def _uid_state(imap) -> tuple[int | None, int]:
    """``(UIDVALIDITY, UIDNEXT)`` for INBOX, or ``(None, 0)`` if the server won't say.

    Read with STATUS before SELECT so both numbers are in hand before any message
    is looked at: the walk's resume point and its new cursor are decided from the
    same snapshot of the box. The response is an unordered attribute list, so each
    number is picked out by name rather than by position.
    """
    status, data = imap.status("INBOX", "(UIDVALIDITY UIDNEXT)")
    if status != "OK" or not data or not data[0]:
        return None, 0
    raw = data[0]
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    validity, uidnext = _status_int(text, "UIDVALIDITY"), _status_int(text, "UIDNEXT")
    if validity is None or uidnext is None:
        return None, 0
    return validity, uidnext


def _status_int(text: str, attribute: str) -> int | None:
    """One named integer out of an IMAP STATUS response, or None if absent."""
    match = re.search(rf"{attribute}\s+(\d+)", text)
    return int(match.group(1)) if match else None


# ── IMAP transport ────────────────────────────────────────────────


def _new_uids(imap, start_uid: int) -> list:
    """UIDs strictly above *start_uid*.

    A server answering ``start:*`` when nothing is above ``start`` returns the
    newest message instead of an empty set, so the result is filtered rather than
    trusted — otherwise every quiet pass would re-read the same message.
    """
    status, data = imap.uid("SEARCH", None, "UID", f"{start_uid + 1}:*")
    if status != "OK" or not data or not data[0]:
        return []
    return [uid for uid in data[0].split() if int(uid) > start_uid]


def _headers_of(imap, uid) -> Message | None:
    """One message's headers, or None if unreadable. ``PEEK`` so nothing is marked read."""
    return _fetch_part(imap, uid, "(BODY.PEEK[HEADER])")


def _message_of(imap, uid) -> Message | None:
    """One whole message, or None if unreadable."""
    return _fetch_part(imap, uid, "(BODY.PEEK[])")


def _fetch_part(imap, uid, spec: str) -> Message | None:
    status, data = imap.uid("FETCH", uid, spec)
    if status != "OK" or not data:
        return None
    for item in data:
        if isinstance(item, tuple):
            return email.message_from_bytes(item[1])
    return None


def _logout(imap) -> None:
    """Best-effort IMAP teardown; a failed logout must not mask the real work."""
    try:
        imap.close()
    except Exception:
        pass
    try:
        imap.logout()
    except Exception:
        pass


# ── Body parsing ──────────────────────────────────────────────────


def _sent_at(msg: Message):
    """Timezone-aware send time from the Date header, or None if unparseable."""
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def _plain_text_body(msg: Message) -> str:
    """Extract the text/plain body, stripped of the quoted reply history.

    Prefers the first ``text/plain`` part (skipping attachments); the whole
    payload is the fallback for a non-multipart message. Quoted history is
    trimmed so ``chat_summary`` and the agent see only the lead's new words.
    """
    raw = _first_text_plain(msg)
    return _strip_quoted(raw)


def _first_text_plain(msg: Message) -> str:
    """The decoded text/plain payload, or the bare payload for a simple message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() != "text/plain":
                continue
            if "attachment" in (part.get("Content-Disposition") or "").lower():
                continue
            return _decode(part)
        return ""
    return _decode(msg)


def _decode(part: Message) -> str:
    """Decode a part's payload to text, tolerating a missing/wrong charset."""
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


# Common reply-quote openers: "On <date>, <name> wrote:" and its localized kin,
# plus Outlook's "-----Original Message-----" divider.
_QUOTE_MARKERS = re.compile(
    r"^\s*(on .+wrote:|-{2,}\s*original message\s*-{2,}|_{5,})\s*$",
    re.IGNORECASE,
)


def _strip_quoted(text: str) -> str:
    """Drop everything from the first quote marker or the trailing ``>`` block.

    Conservative: cuts at the first recognized "On … wrote:" / "Original Message"
    divider, else at the first line of a contiguous run of ``>``-quoted lines that
    continues to the end. Leaves inline text untouched when there is no clear
    boundary.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _QUOTE_MARKERS.match(line):
            return "\n".join(lines[:i]).strip()
    for i, line in enumerate(lines):
        if line.lstrip().startswith(">") and all(
            l.lstrip().startswith(">") or not l.strip() for l in lines[i:]
        ):
            return "\n".join(lines[:i]).strip()
    return text.strip()
