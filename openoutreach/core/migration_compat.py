"""Pre-``migrate`` reconciliation of renames recorded in django_migrations.

When an app or a migration file is renamed, existing installs still have the old
name recorded. Django runs ``check_consistent_history()`` *before* executing any
migration, so it aborts on the mismatch before a data migration could fix it —
the reconciliation therefore cannot live in a migration file. It runs from the
overridden ``migrate`` command instead (``core/management/commands/migrate.py``),
just before the real migrate.

Every rewrite is idempotent: a no-op on fresh installs (table absent / no rows)
and after the first run. Nothing here touches schema or data — these are
bookkeeping updates to django_migrations only.

Caveat: this fires from ``migrate`` alone, so on a DB that has not yet been
reconciled, *other* commands that run the consistency check (``makemigrations``,
``showmigrations``) fail with ``InconsistentMigrationHistory`` naming one of the
renames below. Running ``migrate`` once clears it permanently. The daemon is
unaffected — ``rundaemon`` calls ``migrate`` before touching the DB.
"""
from __future__ import annotations

# old app label -> new app label
_RENAMED_APPS = {
    "linkedin": "legacy",
}

# (app label, old migration name) -> new migration name
_RENAMED_MIGRATIONS = {
    ("chat", "0002_add_linkedin_sync_fields"): "0002_add_sync_fields",
    ("legacy", "0002_linkedinprofile_self_lead"): "0002_profile_self_lead",
}

# Rows for migration files that no longer exist. Django ignores unknown recorded
# migrations, so these are inert — but they are dead bookkeeping, and a DB that
# applied one during its short life carries the row forever. `legacy/0011_...`
# shipped in e42f9bc and was deleted in b18f920, superseded by
# `0011_pivot_drop_channel_models`; installs from that window have both.
_DELETED_MIGRATIONS = {
    ("legacy", "0011_linkedinprofile_contribute_leads"),
}


def reconcile_app_labels(connection) -> list[str]:
    """Rewrite renamed app labels in django_migrations. Returns notes for logging."""
    notes = []
    with connection.cursor() as cursor:
        for old_label, new_label in _RENAMED_APPS.items():
            cursor.execute(
                "UPDATE django_migrations SET app = %s WHERE app = %s",
                [new_label, old_label],
            )
            if cursor.rowcount:
                notes.append(f"app {old_label} -> {new_label} ({cursor.rowcount} rows)")
    return notes


def reconcile_migration_names(connection) -> list[str]:
    """Rewrite renamed migration filenames in django_migrations.

    Must run *after* ``reconcile_app_labels``: a pre-pivot DB records the legacy
    entry under the old ``linkedin`` label, and these keys match the new one.
    """
    notes = []
    with connection.cursor() as cursor:
        for (app, old_name), new_name in _RENAMED_MIGRATIONS.items():
            cursor.execute(
                "UPDATE django_migrations SET name = %s WHERE app = %s AND name = %s",
                [new_name, app, old_name],
            )
            if cursor.rowcount:
                notes.append(f"{app}.{old_name} -> {new_name} ({cursor.rowcount} rows)")
    return notes


def drop_deleted_migrations(connection) -> list[str]:
    """Remove django_migrations rows whose migration file no longer exists."""
    notes = []
    with connection.cursor() as cursor:
        for app, name in _DELETED_MIGRATIONS:
            cursor.execute(
                "DELETE FROM django_migrations WHERE app = %s AND name = %s",
                [app, name],
            )
            if cursor.rowcount:
                notes.append(f"dropped {app}.{name} ({cursor.rowcount} rows)")
    return notes


def reconcile_history(connection) -> list[str]:
    """Run every django_migrations fixup, in dependency order."""
    if "django_migrations" not in connection.introspection.table_names():
        return []  # fresh DB, nothing recorded yet

    return (
        reconcile_app_labels(connection)
        + reconcile_migration_names(connection)
        + drop_deleted_migrations(connection)
    )
