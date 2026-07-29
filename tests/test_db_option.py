from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MANAGE_PY = Path(__file__).resolve().parent.parent / "manage.py"


def _load_manage():
    spec = importlib.util.spec_from_file_location("manage_entrypoint", MANAGE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


extract_db_path = _load_manage().extract_db_path


class TestExtractDbPath:
    def test_absent_leaves_argv_untouched(self):
        assert extract_db_path(["manage.py", "migrate"]) == (["manage.py", "migrate"], None)

    def test_space_form(self):
        assert extract_db_path(["manage.py", "--db", "/tmp/x.sqlite3", "migrate"]) == (
            ["manage.py", "migrate"],
            "/tmp/x.sqlite3",
        )

    def test_equals_form(self):
        assert extract_db_path(["manage.py", "migrate", "--db=/tmp/x.sqlite3"]) == (
            ["manage.py", "migrate"],
            "/tmp/x.sqlite3",
        )

    def test_missing_value_exits(self):
        with pytest.raises(SystemExit):
            extract_db_path(["manage.py", "migrate", "--db"])
