"""The email steps — one function per deal state the cycle serves.

Each step takes exactly one entity and returns the ``DealState`` to move it to, or
``None`` to leave it where it is. Nothing here decides *when* it runs (that is
``core/cycle.py``'s ordered query) and nothing here reaches for another step's
work: a step does one thing to one deal and returns.

Steps are **total** — every failure a step can actually meet is caught and turned
into an explicit next state, so the cycle's own ``try/except`` is a bug backstop
rather than a retry policy. A step that wants to wait writes ``deal.not_before``
and returns ``None``; that is the only retry mechanism there is.
"""
