"""gitauth.py — read-only per-repo-group auth introspection (bh-4y0r.3).

Uses `GIT_CONFIG_GLOBAL` (git >= 2.32) to point every `git config --global` read at a
throwaway file instead of the real machine's global config — hermetic, and proves gitauth
never needs to (and never does) write it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beadhive import gitauth


@pytest.fixture
def global_gitconfig(tmp_path, monkeypatch):
    cfg_file = tmp_path / "gitconfig-global"
    cfg_file.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg_file))
    return cfg_file


def _set(cfg_file: Path, *pairs: str) -> None:
    import subprocess

    for kv in pairs:
        key, _, value = kv.partition("=")
        subprocess.run(
            ["git", "config", "--file", str(cfg_file), key, value], check=True, capture_output=True
        )


# ---- global_identity ---------------------------------------------------------


def test_global_identity_empty_when_unset(global_gitconfig):
    assert gitauth.global_identity() == {"name": "", "email": "", "signingkey": ""}


def test_global_identity_reads_set_values(global_gitconfig):
    _set(global_gitconfig, "user.name=Ada Lovelace", "user.email=ada@example.com")
    assert gitauth.global_identity() == {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "signingkey": "",
    }


# ---- insteadof_aliases --------------------------------------------------------


def test_insteadof_aliases_parses_entries(global_gitconfig):
    _set(
        global_gitconfig,
        "url.git@github-work:.insteadof=git@github.com:",
    )
    assert gitauth.insteadof_aliases() == [("git@github-work:", "git@github.com:")]


def test_insteadof_aliases_empty_when_none(global_gitconfig):
    assert gitauth.insteadof_aliases() == []


def test_insteadof_for_urls_matches_prefix(global_gitconfig):
    _set(global_gitconfig, "url.git@github-work:.insteadof=git@github.com:")
    assert gitauth.insteadof_for_urls(["git@github.com:acme/api.git"]) == "git@github-work:"
    assert gitauth.insteadof_for_urls(["git@gitlab.com:acme/api.git"]) is None
    assert gitauth.insteadof_for_urls([]) is None


# ---- includeif_blocks / scoped_identity_for ------------------------------------


def test_scoped_identity_for_matches_gitdir_prefix(tmp_path, global_gitconfig):
    group_dir = tmp_path / "contrib" / "briancripe"
    group_dir.mkdir(parents=True)
    included = tmp_path / "contrib.gitconfig"
    included.write_text("[user]\n\tname = Contrib Bot\n\temail = bot@example.com\n")
    _set(
        global_gitconfig,
        f"includeIf.gitdir:{group_dir}/.path={included}",
    )
    identity = gitauth.scoped_identity_for(group_dir)
    assert identity == {
        "pattern": f"gitdir:{group_dir}/",
        "name": "Contrib Bot",
        "email": "bot@example.com",
        "signingkey": "",
    }


def test_scoped_identity_for_none_when_no_block_matches(tmp_path, global_gitconfig):
    assert gitauth.scoped_identity_for(tmp_path / "nowhere") is None


def test_scoped_identity_for_none_when_included_file_missing(tmp_path, global_gitconfig):
    group_dir = tmp_path / "contrib"
    group_dir.mkdir()
    _set(global_gitconfig, f"includeIf.gitdir:{group_dir}/.path={tmp_path / 'ghost.gitconfig'}")
    assert gitauth.scoped_identity_for(group_dir) is None


# ---- group_auth_table / group_auth_warnings -----------------------------------


def test_group_auth_table_reports_scoped_and_unscoped_groups(
    tmp_path, monkeypatch, global_gitconfig
):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace.toml").write_text(
        '[[provider]]\nprovider = "github"\nname = "briancripe"\npath = "contrib"\n\n'
        '[[provider]]\nprovider = "github"\nname = "agentguides"\npath = "github"\n'
    )
    scoped_dir = tmp_path / "contrib" / "briancripe"
    included = tmp_path / "contrib.gitconfig"
    included.write_text("[user]\n\tname = Contrib Bot\n\temail = bot@example.com\n")
    _set(global_gitconfig, f"includeIf.gitdir:{scoped_dir}/.path={included}",
         "user.name=Default User", "user.email=default@example.com")

    rows = gitauth.group_auth_table({})
    by_path = {r["path"]: r for r in rows}
    assert by_path["contrib"]["scoped"] is True
    assert by_path["contrib"]["name"] == "Contrib Bot"
    assert by_path["github"]["scoped"] is False
    assert by_path["github"]["name"] == "Default User"


def test_group_auth_warnings_flags_unscoped_and_shared_identity():
    rows = [
        {"path": "a", "account": "x", "name": "Default", "email": "d@e.com", "scoped": False,
         "signingkey": "", "insteadof_alias": None},
        {"path": "b", "account": "y", "name": "Default", "email": "d@e.com", "scoped": False,
         "signingkey": "", "insteadof_alias": None},
    ]
    warns = gitauth.group_auth_warnings(rows)
    assert any("no scoped identity" in w and "'a'" in w for w in warns)
    assert any("no scoped identity" in w and "'b'" in w for w in warns)
    assert any("share auth" in w and "a" in w and "b" in w for w in warns)


def test_group_auth_warnings_silent_when_scoped_and_distinct():
    rows = [
        {"path": "a", "account": "x", "name": "Alice", "email": "alice@e.com", "scoped": True,
         "signingkey": "", "insteadof_alias": None},
        {"path": "b", "account": "y", "name": "Bob", "email": "bob@e.com", "scoped": True,
         "signingkey": "", "insteadof_alias": None},
    ]
    assert gitauth.group_auth_warnings(rows) == []


def test_gitauth_never_writes_global_config(tmp_path, monkeypatch, global_gitconfig):
    """Zero writes to git config: run the full table + warnings build, then assert the
    throwaway global gitconfig file is byte-identical afterward."""
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace.toml").write_text(
        '[[provider]]\nprovider = "github"\nname = "acme"\npath = "github"\n'
    )
    before = global_gitconfig.read_text()
    rows = gitauth.group_auth_table({})
    gitauth.group_auth_warnings(rows)
    assert global_gitconfig.read_text() == before
