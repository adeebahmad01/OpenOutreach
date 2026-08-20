# openoutreach/core/management/base.py
"""The base command that implements the CLI's output contract.

Three rules, and they exist because the reader is as often a program as a person:

  * **stdout is result-only** — the thing you would pipe into ``jq`` or a file. Logs
    and progress go to stderr (``core/logging.py``), so redirecting stdout yields
    data and nothing else.
  * **errors are one line with a stable type** — ``error: <type>: <message>`` on
    stderr, from the vocabulary in ``core/errors.py``, and a non-zero exit.
  * **no traceback for an expected failure.** A rejected API key is not a bug; it is
    an answer, and it should read like one.
"""
from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from openoutreach.core.errors import OpenOutreachError


class OpenOutreachCommand(BaseCommand):
    """A management command whose expected failures obey the error contract."""

    def run_from_argv(self, argv):
        """Render ``OpenOutreachError`` as the contract's line, then exit non-zero.

        Anything else keeps Django's behaviour — an unexpected exception is a bug and
        deserves its traceback.
        """
        try:
            super().run_from_argv(argv)
        except OpenOutreachError as exc:
            sys.stderr.write(f"{exc}\n")
            sys.exit(1)
