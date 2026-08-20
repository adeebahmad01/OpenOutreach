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

A freshly installed tool is the fourth case, and it used to be a raw Django traceback:
the database file exists (``settings.py`` creates its directory) but has no schema until
``run`` migrates, so asking anything else first hit ``no such table: core_campaign``.
"""
from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from openoutreach.core.errors import ErrorType, OpenOutreachError


class OpenOutreachCommand(BaseCommand):
    """A management command whose expected failures obey the error contract."""

    requires_database = True
    """Set ``False`` on a verb that creates the schema rather than reading it."""

    def execute(self, *args, **options):
        """Guard the schema after argument parsing, so ``--help`` still answers."""
        if self.requires_database:
            require_initialized_database()
        return super().execute(*args, **options)

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


def require_initialized_database() -> None:
    """Refuse to read a database that has never been migrated.

    Answering with zero campaigns instead would be the empty-result failure the error
    vocabulary exists to prevent: nothing was found because nothing has ever run.
    """
    from django.conf import settings
    from django.db import connection

    if "core_campaign" in connection.introspection.table_names():
        return

    raise OpenOutreachError(
        ErrorType.NOT_INITIALIZED,
        f"no pipeline yet at {settings.DATABASE_PATH} — run `openoutreach` once to create it",
    )
