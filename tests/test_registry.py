"""Characterization tests for ``beadhive.registry`` — locked down BEFORE the extract_method
refactor of ``classify`` / ``resolve_hive`` / ``derive_prefix`` / ``docs`` / ``repos_sync``, so
the mechanical extraction can't silently change observable behavior (59 dependents, zero prior
dedicated test file — Repowise's ``untested_hotspot`` finding, critical severity).

``classify`` / ``resolve_hive`` / ``derive_prefix`` already have solid coverage in
``test_bh.py`` and ``test_hq.py``; this file adds the branches those miss (repo-scoped
exclusion, a non-fork/failed gh probe, positive `prefix` hive-match mode, a colliding derived
prefix) plus full ground-up coverage for ``docs`` and ``repos_sync``, which had none at all.
"""

from __future__ import annotations

import json
from collections import namedtuple

from beadhive import config, registry

Completed = namedtuple("Completed", "returncode stdout stderr")


# ---- classify ----------------------------------------------------------------


def test_classify_excluded_by_repo_not_org():
    """`exclude.repos` (not just `exclude.orgs`) short-circuits classify — the repo-list branch
    of `is_excluded` the org-only fixtures elsewhere in the suite never exercise."""
    cfg = {"exclude": {"repos": ["github/acme/blocked"]}}
    assert registry.classify("github", "acme", "blocked", cfg) == "excluded"


def test_classify_gh_probe_non_fork_falls_through(monkeypatch):
    """gh reachable, repo confirmed NOT a fork -> falls through to personal-or-prototype."""
    monkeypatch.setattr(registry.shutil, "which", lambda _n: "/usr/bin/gh")
    monkeypatch.setattr(
        registry, "run", lambda *a, **k: Completed(0, json.dumps({"isFork": False}), "")
    )
    assert registry.classify("github", "acme", "repo", {}) == "personal-or-prototype"


def test_classify_gh_probe_failure_falls_through(monkeypatch):
    """A failing gh probe (nonzero exit) is swallowed, not raised — same fallthrough as gh
    absent."""
    monkeypatch.setattr(registry.shutil, "which", lambda _n: "/usr/bin/gh")
    monkeypatch.setattr(registry, "run", lambda *a, **k: Completed(1, "", "rate limited"))
    assert registry.classify("github", "acme", "repo", {}) == "personal-or-prototype"


# ---- resolve_hive --------------------------------------------------------------

_HIVES = {
    "managed_repos": [
        {
            "provider": "github",
            "org": "acme",
            "repo": "core",
            "prefix": "ac-core",
            "kind": "org-native",
        },
        {"provider": "github", "org": "bob", "repo": "tool", "prefix": "tool", "kind": "personal"},
    ]
}


def test_resolve_hive_prefix_mode_matches_by_prefix():
    """`prefix` mode's positive path — the negative (rejects an org/repo id) is already pinned
    in test_bh.py, but not a successful prefix match under the restricted mode."""
    cfg = {**_HIVES, "git_workspace": {"hive_match": "prefix"}}
    assert registry.resolve_hive(cfg, "ac-core")["repo"] == "core"


# ---- derive_prefix --------------------------------------------------------------


def test_derive_prefix_taken_warns():
    """A derived prefix colliding with another hive's prefix warns — the `prefix_taken` branch,
    distinct from (and previously uncovered alongside) the length-warning branch."""
    cfg = {"managed_repos": [{"provider": "github", "org": "other", "repo": "y", "prefix": "y"}]}
    pref, warns = registry.derive_prefix("github", "b", "y", "prototype", cfg)
    assert pref == "y"
    assert any("already used by another hive" in w for w in warns)


# ---- docs -----------------------------------------------------------------------


def _docs_cfg():
    return {
        "providers": ["github", "gitlab"],
        "orgs": {"acme": {"code": "ac", "policy": "required"}},
        "exclude": {"orgs": ["spammy"]},
        "dimensions": {
            "component": {"description": "Intra-project area. Open set."},
            "size": {"description": "Effort estimate.", "values": ["s", "m", "l"]},
            "reserved": {"description": "Closed, undetermined.", "values": []},
        },
        "managed_repos": [
            {
                "provider": "github",
                "org": "acme",
                "repo": "core",
                "prefix": "ac-core",
                "kind": "org-native",
            },
            {
                "provider": "github",
                "org": "bob",
                "repo": "fork1",
                "prefix": "fork-fork1",
                "kind": "fork",
                "upstream": "acme/fork1",
            },
        ],
    }


def test_docs_generates_expected_markdown(monkeypatch, capsys):
    monkeypatch.setattr(registry.config, "load", lambda: _docs_cfg())

    registry.docs()

    written = config.docs_path().read_text()
    assert written == (
        "# Registry & label taxonomy\n"
        "\n"
        f"> Generated from `config.yaml` by `{config.BINARY_ALIAS} label docs` — "
        "do not edit by hand.\n"
        "\n"
        "Identity = labels `provider:`/`org:`/`repo:` (full names). "
        "Prefix = short stable handle (provider not included).\n"
        "\n"
        "## Providers\n"
        "\n"
        "- `provider:github`\n"
        "- `provider:gitlab`\n"
        "\n"
        "## Orgs\n"
        "\n"
        "- `org:acme` — code `ac`, policy **required**\n"
        "\n"
        "## Excluded (beads ignores)\n"
        "\n"
        "- org `spammy`\n"
        "\n"
        "## Non-identity dimensions\n"
        "\n"
        "| Dimension | Values | Description |\n"
        "|---|---|---|\n"
        "| `component:` | _(open)_ | Intra-project area. Open set. |\n"
        "| `size:` | s, m, l | Effort estimate. |\n"
        "| `reserved:` | _(closed; no values yet)_ | Closed, undetermined. |\n"
        "\n"
        "## Managed hives (2)\n"
        "\n"
        "- `ac-core` — github/acme/core (org-native)\n"
        "- `fork-fork1` — github/bob/fork1 (fork, fork of acme/fork1)\n"
    )
    out = capsys.readouterr().out
    assert f"wrote {config.docs_path()}" in out


# ---- repos_sync -----------------------------------------------------------------


def _sync_cfg():
    return {
        "managed_repos": [
            {"provider": "github", "org": "acme", "repo": "core", "prefix": "dup"},
            {"provider": "github", "org": "acme", "repo": "extra", "prefix": "dup"},
        ],
        "exclude": {"orgs": ["spammy"], "repos": ["github/acme/blockedrepo"]},
        "orgs": {"acme": {"code": "ac", "policy": "required"}},
    }


def test_repos_sync_reports_candidates_collisions_and_violations(monkeypatch, capsys):
    monkeypatch.setattr(registry.config, "load", lambda: _sync_cfg())
    monkeypatch.setattr(
        registry,
        "run",
        lambda *a, **k: Completed(
            0,
            "github/acme/core\n"  # already registered -> not a candidate
            "github/acme/newthing\n"  # unregistered, not excluded -> a candidate
            "github/spammy/anything\n"  # org-excluded -> not a candidate
            "github/acme/blockedrepo\n"  # repo-excluded -> not a candidate
            "not-a-triplet\n",  # too few segments -> skipped silently, no crash
            "",
        ),
    )

    registry.repos_sync()

    out = capsys.readouterr().out.splitlines()
    assert (
        "# Candidates (in git-workspace, not registered, not excluded) — run 'bh hive init'" in out
    )
    assert "  github/acme/newthing" in out
    assert "  github/acme/core" not in out
    assert "  github/spammy/anything" not in out
    assert "  github/acme/blockedrepo" not in out
    assert "# Prefix collisions" in out
    assert "  dup: acme/core, acme/extra" in out
    assert "# Required-org prefix violations" in out
    assert "    acme/core: dup != ac-*" in out
    assert "    acme/extra: dup != ac-*" in out


def test_repos_sync_git_workspace_unavailable_reports_on_stderr(monkeypatch, capsys):
    monkeypatch.setattr(registry.config, "load", lambda: {"managed_repos": []})
    monkeypatch.setattr(registry, "run", lambda *a, **k: Completed(1, "", "not found"))

    registry.repos_sync()

    err = capsys.readouterr().err
    assert "git-workspace not available — skipping candidate scan." in err
