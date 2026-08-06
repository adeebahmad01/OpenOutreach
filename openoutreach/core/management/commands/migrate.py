"""`migrate`, with a pre-flight reconciliation of renames recorded in history.

Two things have been renamed since the first release: the `linkedin` app (now
`legacy`), and two migration files whose names carried the platform mark.
Existing installs recorded both under their old names. Django validates
migration-history consistency *before* running any migration, so the fix can't
be a migration file — it has to run first. Overriding the command makes it fire
on every `migrate` (direct or via `rundaemon`'s `call_command`), so no manual
SQL is ever needed.
"""
from django.core.management.commands.migrate import Command as MigrateCommand
from django.db import DEFAULT_DB_ALIAS, connections

from openoutreach.core.migration_compat import reconcile_history


class Command(MigrateCommand):
    def handle(self, *args, **options):
        connection = connections[options.get("database", DEFAULT_DB_ALIAS)]
        for note in reconcile_history(connection):
            self.stdout.write(f"Reconciled rename in django_migrations: {note}")
        super().handle(*args, **options)
