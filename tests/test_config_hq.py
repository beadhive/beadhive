"""config.hq_remote (bh-e0y8.1) — the `hq.remote` config key.

Covers:
- explicit `hq.remote` wins over derivation
- unset derives `<owner>/beadhive-hq` from the resolved workspace identity (org)
- unset + unresolvable identity (outside any managed workspace/worktree) -> ""
- the key is present in `bh config schema` output (type + description)
"""

from __future__ import annotations

from beadhive import config
from beadhive.config_schema import iter_schema_fields


def _patch_cwd_identity(monkeypatch, triplet, leaf=""):
    from beadhive import worktree

    monkeypatch.setattr(worktree, "cwd_identity", lambda *a, **k: (triplet, leaf))


# ---- explicit override -------------------------------------------------------


def test_explicit_remote_wins_over_derivation(monkeypatch):
    # Even with a perfectly resolvable identity, the explicit value must win.
    _patch_cwd_identity(monkeypatch, ("github", "acme", "widgets"))
    cfg = {"hq": {"remote": "myorg/custom-hq"}}
    assert config.hq_remote(cfg) == "myorg/custom-hq"


def test_explicit_remote_wins_when_identity_unresolvable(monkeypatch):
    _patch_cwd_identity(monkeypatch, None)
    cfg = {"hq": {"remote": "myorg/custom-hq"}}
    assert config.hq_remote(cfg) == "myorg/custom-hq"


# ---- derivation ---------------------------------------------------------------


def test_unset_derives_owner_beadhive_hq_from_resolved_identity(monkeypatch):
    _patch_cwd_identity(monkeypatch, ("github", "acme", "widgets"))
    assert config.hq_remote({}) == "acme/beadhive-hq"


def test_unset_derivation_uses_org_not_provider_or_repo(monkeypatch):
    _patch_cwd_identity(monkeypatch, ("gitlab", "octocat", "some-repo"))
    assert config.hq_remote({}) == "octocat/beadhive-hq"


def test_unset_empty_string_remote_treated_as_unset_and_derives(monkeypatch):
    _patch_cwd_identity(monkeypatch, ("github", "acme", "widgets"))
    cfg = {"hq": {"remote": ""}}
    assert config.hq_remote(cfg) == "acme/beadhive-hq"


# ---- unset + underivable ------------------------------------------------------


def test_unset_and_underivable_returns_empty_string(monkeypatch):
    """Outside any managed workspace/worktree, `cwd_identity` resolves to (None, '') — with no
    explicit override, there is nothing to derive `<owner>` from."""
    _patch_cwd_identity(monkeypatch, None)
    assert config.hq_remote({}) == ""


def test_unset_and_no_hq_section_at_all_returns_empty_string(monkeypatch):
    _patch_cwd_identity(monkeypatch, None)
    assert config.hq_remote({}) == ""


# ---- schema presence (bh config schema) --------------------------------------


def test_hq_remote_present_in_schema_with_type_and_description():
    fields = {f.path: f for f in iter_schema_fields()}
    assert "hq.remote" in fields
    field = fields["hq.remote"]
    assert field.type == "str"
    assert field.description  # non-empty description
