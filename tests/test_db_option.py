from __future__ import annotations

import pytest

from openoutreach.__main__ import OVERVIEW, extract_db_path, main, wants_the_overview


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


class TestTheOverview:
    """What someone sees the moment after `pip install openoutreach`.

    Handing a bare invocation to Django answers with every management command it can
    find — fifty lines of `squashmigrations` and `startproject` with the three verbs
    buried halfway down — which reads as a Django project shipped by accident.
    """

    @pytest.mark.parametrize("argv", [
        ["openoutreach"],
        ["openoutreach", "-h"],
        ["openoutreach", "--help"],
        ["openoutreach", "help"],
    ])
    def test_an_invocation_that_names_no_command_gets_the_overview(self, argv):
        assert wants_the_overview(argv) is True

    @pytest.mark.parametrize("argv", [
        ["openoutreach", "find", "10"],
        ["openoutreach", "status"],
        ["openoutreach", "help", "find"],
    ])
    def test_a_named_command_goes_to_django(self, argv):
        """`help find` included — Django writes better per-command help than we would."""
        assert wants_the_overview(argv) is False

    def test_it_names_the_three_verbs_and_not_django_s_plumbing(self, capsys):
        main(["openoutreach"])

        printed = capsys.readouterr().out
        assert all(verb in printed for verb in ("init", "find 10", "status"))
        assert "squashmigrations" not in printed and "startproject" not in printed

    def test_the_db_flag_does_not_turn_a_bare_call_into_a_command(self, capsys):
        """`--db` is stripped before the count, or `openoutreach --db x` would look like
        an invocation naming a command and reach Django with nothing to run."""
        main(["openoutreach", "--db", "/tmp/x.sqlite3"])

        assert capsys.readouterr().out == OVERVIEW
