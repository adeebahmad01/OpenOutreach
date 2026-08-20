"""The `openoutreach` console script — the single entry point for both readers.

Usage:
    openoutreach                                   # run the daemon (onboarding on first run)
    openoutreach rundaemon                         # the same thing, named
    openoutreach migrate                           # run Django migrations
    openoutreach runserver                         # Django Admin at http://localhost:8000/admin/
    openoutreach createsuperuser

Any command accepts `--db PATH` (or `--db=PATH`) to work against a SQLite file
other than the default `~/.openoutreach/data/db.sqlite3`; the `OPENOUTREACH_DB`
env var does the same.

`manage.py` is a thin shim over this module, kept for work inside a checkout.
"""

import os
import sys


def extract_db_path(argv):
    """Strip `--db PATH` / `--db=PATH` out of argv, returning (rest, path_or_None).

    Django parses arguments per-command, so the flag has to come off before
    execute_from_command_line ever sees argv.
    """
    rest, db_path, i = [], None, 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--db":
            if i + 1 >= len(argv):
                sys.exit("openoutreach: --db requires a path")
            db_path = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--db="):
            db_path = arg.split("=", 1)[1]
        else:
            rest.append(arg)
        i += 1
    return rest, db_path


def main(argv=None):
    """Run a management command, defaulting a bare invocation to `rundaemon`."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openoutreach.settings")

    from django.core.management import execute_from_command_line

    argv, db_path = extract_db_path(list(sys.argv if argv is None else argv))
    if db_path:
        os.environ["OPENOUTREACH_DB"] = db_path

    # No subcommand (or first arg is a flag) → default to rundaemon.
    if len(argv) == 1 or argv[1].startswith("-"):
        argv = [argv[0], "rundaemon"] + argv[1:]

    execute_from_command_line(argv)


if __name__ == "__main__":
    main()
