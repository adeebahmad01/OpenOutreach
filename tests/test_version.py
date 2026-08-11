# tests/test_version.py
"""What build am I — read against real git checkouts, not mocked ones.

The module's whole job is parsing on-disk git state, so the tests build actual
repos in ``tmp_path``: mocking ``.git`` would only assert that the mock matches
the code's assumptions, which is the thing most likely to be wrong.
"""

import subprocess

import pytest

from openoutreach.core import version


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A one-commit git repo, with the version module pointed at it."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t.io")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app.py").write_text("x = 1\n")
    _git(tmp_path, "add", "app.py")
    # Only the *author* date is pinned, and the committer date is deliberately left
    # as "now": the CalVer must come from when the change was written, not from when
    # a rebase last touched it, and reading ``%ct`` would make this assertion drift
    # with the calendar.
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "first",
         "--date", "2026-08-07T10:00:00+00:00")
    monkeypatch.setattr(version, "REPO_ROOT", tmp_path)
    version._build.cache_clear()
    yield tmp_path
    version._build.cache_clear()


def _head(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


# ── identity ─────────────────────────────────────────────────────────

def test_reads_head_sha_without_shelling_out(repo, monkeypatch):
    """The sha must survive an image with no git binary — hence the .git read."""
    monkeypatch.setattr(version, "_git", lambda *a: None)
    version._build.cache_clear()
    assert version.commit_sha() == _head(repo)


def test_resolves_head_from_packed_refs(repo):
    """A fresh clone keeps refs packed, so the loose ref file simply isn't there."""
    _git(repo, "pack-refs", "--all")
    version._build.cache_clear()
    assert version.commit_sha() == _head(repo)


def test_resolves_detached_head(repo):
    _git(repo, "checkout", "-q", "--detach")
    version._build.cache_clear()
    assert version.commit_sha() == _head(repo)


def test_resolves_gitdir_pointer_file(repo, tmp_path, monkeypatch):
    """Development happens in a submodule, where .git is a file, not a directory."""
    checkout = tmp_path / "elsewhere"
    checkout.mkdir()
    (checkout / ".git").write_text(f"gitdir: {repo / '.git'}\n")
    monkeypatch.setattr(version, "REPO_ROOT", checkout)
    version._build.cache_clear()
    assert version.commit_sha() == _head(repo)


def test_missing_git_metadata_is_unknown_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "REPO_ROOT", tmp_path)
    version._build.cache_clear()
    assert version.commit_sha() == version.UNKNOWN
    assert version.calver() == version.UNKNOWN


# ── ordering ─────────────────────────────────────────────────────────

def test_calver_comes_from_the_commit_date(repo):
    assert version.calver() == "2026.08.07"


def test_version_string_pairs_the_date_with_the_sha(repo):
    assert version.version_string() == f"2026.08.07+g{_head(repo)[:7]}"


def test_user_agent_is_a_product_token(repo):
    assert version.user_agent() == f"OpenOutreach/{version.version_string()}"


# ── local modification ───────────────────────────────────────────────

def test_edited_working_copy_reports_dirty(repo):
    (repo / "app.py").write_text("x = 2  # patched locally\n")
    version._build.cache_clear()
    assert version.is_dirty() is True
    assert version.version_string().endswith(".dirty")


def test_clean_working_copy_is_not_dirty(repo):
    assert version.is_dirty() is False
    assert ".dirty" not in version.version_string()


def test_undeterminable_dirtiness_is_none_not_false(repo, monkeypatch):
    """No git binary must not produce a confident 'clean' we never verified."""
    monkeypatch.setattr(version, "_git", lambda *a: None)
    version._build.cache_clear()
    assert version.is_dirty() is None
    assert ".dirty" not in version.version_string()


# ── build override ───────────────────────────────────────────────────

def test_build_env_var_wins_over_the_checkout(repo, monkeypatch):
    monkeypatch.setenv("OPENOUTREACH_BUILD", f"{'a' * 40}@2026.01.01")
    version._build.cache_clear()
    assert version.commit_sha() == "a" * 40
    assert version.calver() == "2026.01.01"
