"""config.hq_remote (bh-e0y8.1) — the `hq.remote` config key.

Covers:
- explicit `hq.remote` wins over derivation
- unset derives `<owner>/beadhive-hq` from the logged-in `gh` identity (bh-mw97)
- the derivation is CWD-INVARIANT: HQ is a fleet singleton, so the workspace identity of
  whichever hive you happen to stand in must not move it (bh-mw97's regression)
- unset + no gh login (absent / logged out / offline) -> ""
- the key is present in `bh config schema` output (type + description)
"""

from __future__ import annotations

from beadhive import config
from beadhive.config_schema import iter_schema_fields


def _patch_gh_login(monkeypatch, login):
    monkeypatch.setattr(config, "gh_login", lambda cwd=None: login)


def _patch_cwd_identity(monkeypatch, triplet, leaf=""):
    from beadhive import worktree

    monkeypatch.setattr(worktree, "cwd_identity", lambda *a, **k: (triplet, leaf))


# ---- explicit override -------------------------------------------------------


def test_explicit_remote_wins_over_derivation(monkeypatch):
    # Even with a perfectly resolvable gh login, the explicit value must win.
    _patch_gh_login(monkeypatch, "octocat")
    cfg = {"hq": {"remote": "myorg/custom-hq"}}
    assert config.hq_remote(cfg) == "myorg/custom-hq"


def test_explicit_remote_wins_when_gh_login_unresolvable(monkeypatch):
    _patch_gh_login(monkeypatch, "")
    cfg = {"hq": {"remote": "myorg/custom-hq"}}
    assert config.hq_remote(cfg) == "myorg/custom-hq"


# ---- derivation ---------------------------------------------------------------


def test_unset_derives_owner_beadhive_hq_from_gh_login(monkeypatch):
    _patch_gh_login(monkeypatch, "briancripe")
    assert config.hq_remote({}) == "briancripe/beadhive-hq"


def test_unset_empty_string_remote_treated_as_unset_and_derives(monkeypatch):
    _patch_gh_login(monkeypatch, "briancripe")
    cfg = {"hq": {"remote": ""}}
    assert config.hq_remote(cfg) == "briancripe/beadhive-hq"


# ---- cwd invariance (bh-mw97) -------------------------------------------------


def test_derivation_ignores_the_workspace_identity_org(monkeypatch):
    """The regression: standing in the `beadhive` hive used to derive `beadhive/beadhive-hq`
    even though the operator's identity is `briancripe`. Host identity wins outright."""
    _patch_gh_login(monkeypatch, "briancripe")
    _patch_cwd_identity(monkeypatch, ("github", "beadhive", "beadhive"))
    assert config.hq_remote({}) == "briancripe/beadhive-hq"


def test_derivation_is_identical_from_two_different_hives(monkeypatch):
    """HQ is a fleet SINGLETON — invoking from a different hive must not move it."""
    _patch_gh_login(monkeypatch, "briancripe")

    _patch_cwd_identity(monkeypatch, ("github", "beadhive", "beadhive"))
    from_beadhive = config.hq_remote({})
    _patch_cwd_identity(monkeypatch, ("gitlab", "someorg", "orca"))
    from_orca = config.hq_remote({})

    assert from_beadhive == from_orca == "briancripe/beadhive-hq"


# ---- unset + underivable ------------------------------------------------------


def test_unset_and_underivable_returns_empty_string(monkeypatch):
    """gh absent, logged out, or offline — with no explicit override there is nothing to
    derive `<owner>` from, and the callers prompt instead of guessing."""
    _patch_gh_login(monkeypatch, "")
    assert config.hq_remote({}) == ""


def test_unset_and_no_hq_section_at_all_returns_empty_string(monkeypatch):
    _patch_gh_login(monkeypatch, "")
    assert config.hq_remote({}) == ""


# ---- gh_login itself ----------------------------------------------------------


def test_gh_login_returns_the_login_on_success(monkeypatch):
    import subprocess

    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a[0], 0, stdout="briancripe\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert config.gh_login() == "briancripe"


def test_gh_login_returns_empty_when_gh_fails(monkeypatch):
    import subprocess

    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a[0], 1, stdout="", stderr="not logged in")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert config.gh_login() == ""


def test_gh_login_returns_empty_when_gh_is_absent(monkeypatch):
    """A missing `gh` binary is a derivation miss, never an exception out of a config getter."""
    import subprocess

    def fake_run(*a, **k):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert config.gh_login() == ""


# ---- schema presence (bh config schema) --------------------------------------


def test_hq_remote_present_in_schema_with_type_and_description():
    fields = {f.path: f for f in iter_schema_fields()}
    assert "hq.remote" in fields
    field = fields["hq.remote"]
    assert field.type == "str"
    assert field.description  # non-empty description
