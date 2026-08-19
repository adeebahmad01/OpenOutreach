"""Enrichment — the finder's one paid step: a profile URL in, a work email out.

Two modules, and the split is between *the provider* and *the pipeline step*:

- ``bettercontact`` is the client. It also serves ``discovery.py``, which pages the
  same vendor's free Lead Finder index through ``submit_and_poll`` — one account, one
  key, two endpoints, and only this one bills.
- ``lookup`` is the two-step handshake the cycle drives: ``buy_address`` resolves the
  free sources first and fires a job only if they miss, ``check_lookup`` polls it.

**This is deliberately not part of discovery, and no longer part of a mail package.**
It used to live under ``emails/`` because a resolved address existed to be written to;
that is no longer true, and the coupling it implied — resolve only what there is send
headroom for — was the single line that made a mailbox-less install produce nothing.
An address is now just a column in the export: nice to have, never a precondition, and
a lead with none still exports with its ``reason``.
"""
