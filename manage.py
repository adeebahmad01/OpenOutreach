#!/usr/bin/env python
"""Django management entrypoint.

Usage:
    python manage.py rundaemon                     # run the daemon (interactive onboarding on first run)
    python manage.py runserver                     # Django Admin at http://localhost:8000/admin/
    python manage.py migrate                       # run Django migrations
    python manage.py createsuperuser

Any command accepts `--db PATH` (or `--db=PATH`) to work against a SQLite file
other than the default `data/db.sqlite3`; the `OPENOUTREACH_DB` env var does the
same.
"""
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openoutreach.settings")


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
                sys.exit("manage.py: --db requires a path")
            db_path = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--db="):
            db_path = arg.split("=", 1)[1]
        else:
            rest.append(arg)
        i += 1
    return rest, db_path


if __name__ == "__main__":
    from django.core.management import execute_from_command_line

    argv, db_path = extract_db_path(sys.argv)
    if db_path:
        os.environ["OPENOUTREACH_DB"] = db_path

    # No subcommand (or first arg is a flag) → default to rundaemon.
    if len(argv) == 1 or argv[1].startswith("-"):
        argv = [argv[0], "rundaemon"] + argv[1:]

    execute_from_command_line(argv)
