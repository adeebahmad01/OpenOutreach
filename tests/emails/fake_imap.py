# tests/emails/fake_imap.py
"""A minimal IMAP server for the mail pass — one INBOX of whole RFC-822 messages.

Only what ``inbox.read_mail`` calls: STATUS for the UID epoch, SELECT, a UID SEARCH
that honours the ``UID lo:*`` range, and a UID FETCH that answers either
``BODY.PEEK[HEADER]`` or ``BODY.PEEK[]``. Fetches are recorded so a test can assert
the pass did *not* pull the body of a message it had no use for.
"""
from __future__ import annotations


def message(uid, *, to, sender, subject="Re: Hi", body="Sure, happy to chat.",
            message_id=None, references=None, in_reply_to=None):
    """One inbox message, as (uid, raw bytes)."""
    headers = [
        f"From: {sender}",
        f"To: {to}",
        f"Subject: {subject}",
        f"Message-ID: {message_id or f'<m{uid}@corp.com>'}",
        "Date: Mon, 16 Mar 2026 10:00:00 +0000",
        "Content-Type: text/plain; charset=utf-8",
    ]
    if references:
        headers.append(f"References: {references}")
    if in_reply_to:
        headers.append(f"In-Reply-To: {in_reply_to}")
    return uid, ("\r\n".join(headers) + "\r\n\r\n" + body).encode()


class FakeIMAP:
    def __init__(self, messages, uidvalidity=1):
        self.messages = dict(messages)  # {uid: raw}
        self.uidvalidity = uidvalidity
        self.searched_ranges = []
        self.body_fetches = []

    def login(self, username, password):
        return "OK", []

    def status(self, mailbox, attributes):
        uidnext = max(self.messages, default=0) + 1
        return "OK", [f"INBOX (UIDVALIDITY {self.uidvalidity} UIDNEXT {uidnext})".encode()]

    def select(self, mailbox, readonly=False):
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "SEARCH":
            return self._search(args)
        return self._fetch(args)

    def _search(self, args):
        uid_range = args[2]
        self.searched_ranges.append(uid_range)
        low = int(uid_range.split(":")[0])
        hits = sorted(uid for uid in self.messages if uid >= low)
        return "OK", [" ".join(str(uid) for uid in hits).encode()]

    def _fetch(self, args):
        uid, spec = int(args[0]), args[1]
        raw = self.messages.get(uid)
        if raw is None:
            return "NO", []
        if "HEADER" in spec:
            raw = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
        else:
            self.body_fetches.append(uid)
        return "OK", [(b"1 (UID)", raw)]

    def close(self):
        pass

    def logout(self):
        pass
