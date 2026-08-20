from __future__ import annotations

import pytest

from openoutreach.__main__ import extract_db_path


class TestExtractDbPath:
    def test_absent_leaves_argv_untouched(self):
        assert extract_db_path(["openoutreach", "migrate"]) == (["openoutreach", "migrate"], None)

    def test_space_form(self):
        assert extract_db_path(["openoutreach", "--db", "/tmp/x.sqlite3", "migrate"]) == (
            ["openoutreach", "migrate"],
            "/tmp/x.sqlite3",
        )

    def test_equals_form(self):
        assert extract_db_path(["openoutreach", "migrate", "--db=/tmp/x.sqlite3"]) == (
            ["openoutreach", "migrate"],
            "/tmp/x.sqlite3",
        )

    def test_missing_value_exits(self):
        with pytest.raises(SystemExit):
            extract_db_path(["openoutreach", "migrate", "--db"])
