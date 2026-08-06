"""Tests for core/migration_compat.py — the pre-``migrate`` django_migrations fixups.

These run against the live test connection: the rewrites are raw SQL against a
table Django owns, so mocking the cursor would test nothing real.
"""
import pytest
from django.db import connection

from openoutreach.core.migration_compat import reconcile_history


def _record(app, name):
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO django_migrations (app, name, applied) VALUES (%s, %s, %s)",
            [app, name, "2026-01-01 00:00:00"],
        )


def _names(app):
    with connection.cursor() as cursor:
        cursor.execute("SELECT name FROM django_migrations WHERE app = %s", [app])
        return {row[0] for row in cursor.fetchall()}


@pytest.mark.django_db
class TestReconcileHistory:
    def test_renames_marked_migration_filenames(self):
        _record("chat", "0002_add_linkedin_sync_fields")
        _record("legacy", "0002_linkedinprofile_self_lead")

        reconcile_history(connection)

        assert "0002_add_sync_fields" in _names("chat")
        assert "0002_add_linkedin_sync_fields" not in _names("chat")
        assert "0002_profile_self_lead" in _names("legacy")
        assert "0002_linkedinprofile_self_lead" not in _names("legacy")

    def test_relabels_renamed_app_before_renaming_its_migration(self):
        """A pre-pivot DB records the row under the old app label.

        Order matters: the name rewrite keys on `legacy`, so the label rewrite
        has to land first or the row is missed.
        """
        _record("linkedin", "0002_linkedinprofile_self_lead")

        reconcile_history(connection)

        assert _names("linkedin") == set()
        assert "0002_profile_self_lead" in _names("legacy")

    def test_drops_row_for_deleted_migration_file(self):
        _record("legacy", "0011_linkedinprofile_contribute_leads")

        reconcile_history(connection)

        assert "0011_linkedinprofile_contribute_leads" not in _names("legacy")

    def test_is_idempotent(self):
        _record("chat", "0002_add_linkedin_sync_fields")

        first = reconcile_history(connection)
        second = reconcile_history(connection)

        assert first, "first run should report the rewrite it made"
        assert second == [], "second run must be a silent no-op"

    def test_no_op_when_nothing_matches(self):
        """A fresh install has none of these rows; nothing is touched."""
        before = _names("chat")

        assert reconcile_history(connection) == []
        assert _names("chat") == before

    def test_leaves_unrelated_rows_alone(self):
        _record("crm", "0003_public_identifier_unique")

        reconcile_history(connection)

        assert "0003_public_identifier_unique" in _names("crm")
