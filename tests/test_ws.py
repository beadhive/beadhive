"""ws self-checks — the money paths: prefix derivation, classify, identity,
validation, and the comment-preserving config round-trip."""

from __future__ import annotations

import json
from collections import namedtuple
from pathlib import Path

import pytest
import typer

from beadhive import (
    bd,
    config,
    doctor,
    git,
    gitworkspace,
    hive,
    hub,
    identity,
    registry,
    route,
    state,
    validate,
)

EXAMPLE = Path(__file__).parent / "fixture_config.yaml"
WORKSPACE_TOML = Path(__file__).parent / "fixture_workspace.toml"
Completed = namedtuple("Completed", "returncode stdout stderr")


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text(EXAMPLE.read_text())
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    monkeypatch.setenv("BH_CONFIG", str(p))
    return p


# ---- sanitize / derive_prefix ----------------------------------------------


# ---- git-workspace integration ---------------------------------------------


def test_gitworkspace_providers_orgs():
    cfg = {"git_workspace": {"enabled": True, "path": str(WORKSPACE_TOML)}}
    assert gitworkspace.enabled(cfg)
    assert gitworkspace.providers(cfg) == {"github", "gitlab"}
    assert gitworkspace.orgs(cfg) == {"agentguides", "octo-org", "acme"}


def test_effective_providers_union():
    cfg = {"providers": ["github"], "git_workspace": {"enabled": True, "path": str(WORKSPACE_TOML)}}
    assert registry.effective_providers(cfg) == ["github", "gitlab"]
    cfg["git_workspace"]["enabled"] = False
    assert registry.effective_providers(cfg) == ["github"]


def test_config_paths_excludes_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace.toml").write_text("")
    (tmp_path / "workspace-team.toml").write_text("")  # split config: included
    (tmp_path / "workspace-lock.toml").write_text("")  # lock: excluded
    names = sorted(p.name for p in gitworkspace.config_paths({}))
    assert names == ["workspace-team.toml", "workspace.toml"]


def test_gitworkspace_tracked_repos(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace-lock.toml").write_text(
        '[[repo]]\npath = "github/acme/api"\n[[repo]]\npath = "github/acme/web"\n'
    )
    assert set(gitworkspace.tracked_repos({})) == {
        ("github", "acme", "api"),
        ("github", "acme", "web"),
    }


def test_doctor_scan(tmp_path):
    (tmp_path / "github" / "acme" / "api" / ".git").mkdir(parents=True)
    (tmp_path / "github" / "acme" / "stray").mkdir(parents=True)  # no .git
    (tmp_path / "randomdir").mkdir()  # not a recognized provider
    git_repos, nonrepo, unknown = doctor._scan(tmp_path, {"github"})
    assert git_repos == {"github/acme/api"}
    assert nonrepo == {"github/acme/stray"}
    assert unknown == ["randomdir"]


# ---- hub & sync ------------------------------------------------------------


def test_hub_and_cache_dirs(monkeypatch, tmp_path):
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    monkeypatch.delenv("BH_HUB", raising=False)
    monkeypatch.delenv("BH_CACHE", raising=False)
    assert config.hub_dir() == tmp_path / "hub"
    assert config.cache_dir() == tmp_path / "cache"
    monkeypatch.setenv("BH_HUB", str(tmp_path / "h"))
    assert config.hub_dir() == tmp_path / "h"


def test_repo_urls(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace-lock.toml").write_text(
        '[[repo]]\npath = "github/acme/api"\nurl = "git@github.com:acme/api.git"\n'
    )
    assert gitworkspace.repo_urls({}) == {"github/acme/api": "git@github.com:acme/api.git"}


# ---- bh-rax6: fork detection via resolved host + offline upstream -----------


def test_url_slug_ssh_and_https_forms():
    assert gitworkspace.url_slug("git@github.com:stablyai/orca.git") == "stablyai/orca"
    assert gitworkspace.url_slug("https://github.com/stablyai/orca.git") == "stablyai/orca"
    assert gitworkspace.url_slug("ssh://git@github.com/stablyai/orca") == "stablyai/orca"
    assert gitworkspace.url_slug("") == ""


def test_provider_host_resolves_path_segment_to_host(tmp_path, monkeypatch):
    """`path='contrib' provider='github'` — the fork probe must reach the resolved host, not the
    path label (bh-rax6)."""
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace.toml").write_text(
        '[[provider]]\nprovider = "github"\nname = "briancripe"\npath = "contrib"\n'
    )
    assert gitworkspace.provider_host({}, "contrib") == "github"
    assert gitworkspace.provider_host({}, "unknown") == ""


def test_upstreams_reads_lock_upstream_as_slug(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace-lock.toml").write_text(
        '[[repo]]\npath = "contrib/briancripe/orca"\n'
        'url = "git@github.com:briancripe/orca.git"\n'
        'upstream = "git@github.com:stablyai/orca.git"\n'
    )
    assert gitworkspace.upstreams({}) == {"contrib/briancripe/orca": "stablyai/orca"}


def test_classify_fork_from_lock_upstream_offline(tmp_path, monkeypatch):
    """`classify contrib briancripe orca` -> fork upstream=stablyai/orca with gh absent and no
    network — the path!=host provider label no longer disarms detection (bh-rax6)."""
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace.toml").write_text(
        '[[provider]]\nprovider = "github"\nname = "briancripe"\npath = "contrib"\n'
    )
    (tmp_path / "workspace-lock.toml").write_text(
        '[[repo]]\npath = "contrib/briancripe/orca"\n'
        'url = "git@github.com:briancripe/orca.git"\n'
        'upstream = "git@github.com:stablyai/orca.git"\n'
    )
    monkeypatch.setattr(registry.shutil, "which", lambda _n: None)  # gh absent → no network
    cfg = {"git_workspace": {"enabled": True}}
    assert registry.classify("contrib", "briancripe", "orca", cfg) == "fork upstream=stablyai/orca"
    # A non-fork under the same workspace stays a prototype.
    assert registry.classify("github", "briancripe", "workspace", cfg) == "personal-or-prototype"


def test_hive_url_lock_then_derive(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace-lock.toml").write_text(
        '[[repo]]\npath = "gitea/self/thing"\nurl = "https://git.example/self/thing.git"\n'
    )
    assert (
        hub._hive_url({}, {"provider": "gitea", "org": "self", "repo": "thing"})
        == "https://git.example/self/thing.git"
    )
    assert (
        hub._hive_url({}, {"provider": "github", "org": "o", "repo": "r"})
        == "git@github.com:o/r.git"
    )
    assert (
        hub._hive_url({}, {"provider": "gitea", "org": "x", "repo": "y"}) is None
    )  # no lock, no default


def test_hub_query_uninitialized(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    monkeypatch.delenv("BH_HUB", raising=False)
    with pytest.raises(typer.Exit):  # no hub yet → run bh sync first
        hub.query(["ready"])


def test_sync_routes_cloned_and_uncloned(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(hub, "run", lambda cmd, **k: calls.append(cmd) or Completed(0, "", ""))
    monkeypatch.setattr(hub, "ensure_hub", lambda: tmp_path / "hub")
    monkeypatch.setattr(
        hub.config,
        "load",
        lambda: {
            "managed_repos": [
                {"provider": "github", "org": "a", "repo": "cloned", "prefix": "a-cloned"},
                {"provider": "github", "org": "a", "repo": "remote", "prefix": "a-remote"},
            ]
        },
    )
    cloned_path = tmp_path / "cloned"
    (cloned_path / ".beads").mkdir(parents=True)
    monkeypatch.setattr(
        hub.registry,
        "hive_dir",
        lambda e: cloned_path if e["repo"] == "cloned" else tmp_path / "nope",
    )
    fake_cache = tmp_path / "cache_remote"
    monkeypatch.setattr(hub, "_fetch_cache", lambda cfg, e: fake_cache)
    hub.sync()
    hubdir = str(tmp_path / "hub")
    assert ["bd", "-C", hubdir, "repo", "add", str(cloned_path)] in calls  # cloned by path
    assert ["bd", "-C", hubdir, "repo", "add", str(fake_cache)] in calls  # uncloned by cache
    assert ["bd", "-C", hubdir, "repo", "sync"] in calls
    # dolt-backend rigs keep no issues.jsonl on disk, so each is exported to JSONL first —
    # otherwise repo sync skips them and the hub aggregates nothing.
    assert [
        "bd", "-C", str(cloned_path), "export", "-o", str(cloned_path / ".beads" / "issues.jsonl")
    ] in calls


def test_sync_reports_failed_hydration(tmp_path, monkeypatch, capsys):
    """A hive whose beads bd can't import is reported as failed, not folded into a false green."""
    good = tmp_path / "good"
    bad = tmp_path / "bad"
    (good / ".beads").mkdir(parents=True)
    (bad / ".beads").mkdir(parents=True)

    def fake_run(cmd, **k):
        if cmd[-2:] == ["repo", "sync"]:  # repo sync surfaces per-hive import failures on stderr
            return Completed(0, "", f"Warning: failed to import from {bad}: reconcile error\n")
        return Completed(0, "", "")

    monkeypatch.setattr(hub, "run", fake_run)
    monkeypatch.setattr(hub, "ensure_hub", lambda: tmp_path / "hub")
    monkeypatch.setattr(
        hub.config,
        "load",
        lambda: {
            "managed_repos": [
                {"provider": "github", "org": "a", "repo": "good", "prefix": "a-good"},
                {"provider": "github", "org": "a", "repo": "bad", "prefix": "a-bad"},
            ]
        },
    )
    monkeypatch.setattr(hub.registry, "hive_dir", lambda e: good if e["repo"] == "good" else bad)
    hub.sync()
    out = capsys.readouterr().out
    assert "1 hydrated" in out  # good hive
    assert "1 failed to hydrate (a-bad)" in out  # bad hive named, honestly


# ---- hive routing (-a / -r) -------------------------------------------------

_HIVES = {
    "managed_repos": [
        {
            "provider": "github",
            "org": "agentguides",
            "repo": "infra",
            "prefix": "ag-infra",
            "kind": "org-native",
        },
        {
            "provider": "github",
            "org": "briancripe",
            "repo": "workspace",
            "prefix": "workspace",
            "kind": "personal",
        },
    ]
}


def test_reject_inline_flags():
    with pytest.raises(typer.Exit):  # routing flag after the subcommand → hint
        route.reject_inline_flags(["-a", "status"])
    route.reject_inline_flags(["status"])  # plain args: no raise


def test_global_routing_rejected_on_nonpassthrough():
    from typer.testing import CliRunner

    from beadhive.cli import app

    res = CliRunner().invoke(app, ["-a", "doctor"])  # routing only valid for bd/git
    assert res.exit_code == 1


def test_targets_gating():
    assert route.targets({}, "cwd", None) == [(None, None)]  # cwd never needs git-workspace
    with pytest.raises(typer.Exit):  # routing requires git_workspace enabled
        route.targets({"git_workspace": {"enabled": False}}, "all", None)


def test_resolve_hive_flexible():
    cfg = dict(_HIVES)
    assert registry.resolve_hive(cfg, "ag-infra")["repo"] == "infra"
    assert registry.resolve_hive(cfg, "github/agentguides/infra")["prefix"] == "ag-infra"
    assert registry.resolve_hive(cfg, "agentguides/infra")["prefix"] == "ag-infra"
    assert registry.resolve_hive(cfg, "infra")["prefix"] == "ag-infra"  # bare, unique


def test_resolve_hive_modes_and_ambiguity():
    assert registry.resolve_hive(
        {**_HIVES, "git_workspace": {"hive_match": "triplet"}}, "github/agentguides/infra"
    )
    with pytest.raises(typer.Exit):
        registry.resolve_hive(
            {**_HIVES, "git_workspace": {"hive_match": "prefix"}}, "agentguides/infra"
        )
    ambig = {
        "managed_repos": [
            {"provider": "github", "org": "a", "repo": "x", "prefix": "a-x", "kind": "prototype"},
            {"provider": "github", "org": "b", "repo": "x", "prefix": "b-x", "kind": "prototype"},
        ]
    }
    with pytest.raises(typer.Exit):
        registry.resolve_hive(ambig, "x")  # bare name is ambiguous


def test_resolve_hive_no_match_suggests_next_steps(capsys):
    # bh-xy83: an unregistered hive id gets next-step suggestions, not just a bare error.
    cfg = {**_HIVES, "orgs": {"beadhive": {"code": "bh", "policy": "required"}}}
    with pytest.raises(typer.Exit):
        registry.resolve_hive(cfg, "github/beadhive/beadhive")
    err = capsys.readouterr().err
    assert f"{config.BINARY_ALIAS} hive ls" in err
    assert f"{config.BINARY_ALIAS} hive ls --available" in err
    assert f"{config.BINARY_ALIAS} hive add github/beadhive/beadhive" in err
    assert "org 'beadhive' is already known" in err


def test_fan_out_continue_and_summary(tmp_path):
    calls = []

    def runner(label, _cwd):
        calls.append(label)
        return 0 if label == "ok" else 1

    with pytest.raises(typer.Exit):
        route.fan_out([("ok", tmp_path), ("bad", tmp_path)], runner)
    assert calls == ["ok", "bad"]  # continued past the failure


def test_fan_out_single_cwd_propagates_code():
    with pytest.raises(typer.Exit) as ei:
        route.fan_out([(None, None)], lambda _l, _c: 3)
    assert ei.value.exit_code == 3
    assert route.fan_out([(None, None)], lambda _l, _c: 0) is None


def test_git_workspace_help_translation(monkeypatch):
    cmds = []
    monkeypatch.setattr(git, "run", lambda cmd, **k: cmds.append(cmd) or Completed(0, "", ""))
    git.passthrough("cwd", None, ["workspace", "--help"])
    assert cmds[-1] == ["git-workspace", "--help"]
    git.passthrough("cwd", None, ["workspace", "list"])
    assert cmds[-1] == ["git", "workspace", "list"]


def test_git_workspace_rejects_routing():
    with pytest.raises(typer.Exit):  # can't route a central git-workspace subcommand
        git.passthrough("all", None, ["workspace", "list"])


def test_bd_create_builds_triplet(monkeypatch, tmp_path):
    cmds = []
    monkeypatch.setattr(
        bd, "_run", lambda cmd, **k: cmds.append((cmd, k.get("cwd"))) or Completed(0, "", "")
    )
    monkeypatch.setattr(bd.validate, "has_violations", lambda **k: False)
    monkeypatch.setattr(
        bd, "workspace_identity", lambda cwd=None: ("github", "agentguides", "infra")
    )
    assert bd._create(["My title"], tmp_path) == 0
    cmd, cwd = cmds[-1]
    assert cmd == ["bd", "create", "My title", "-l", "provider:github,org:agentguides,repo:infra"]
    assert cwd == tmp_path


def test_bd_create_help_bypasses_label_gate(monkeypatch, tmp_path):
    # bh-8krs: `bh bd create --help` must print help even with label violations, and the gate
    # must still fire on a real (mutating) invocation.
    cmds = []
    monkeypatch.setattr(
        bd, "_run", lambda cmd, **k: cmds.append((cmd, k.get("cwd"))) or Completed(0, "", "")
    )
    monkeypatch.setattr(bd.validate, "has_violations", lambda **k: True)
    monkeypatch.setattr(
        bd, "workspace_identity", lambda cwd=None: ("github", "agentguides", "infra")
    )
    assert bd.create(["--help"], tmp_path) == (0, "")
    cmd, cwd = cmds[-1]
    assert cmd[:3] == ["bd", "create", "--help"]
    assert cwd == tmp_path
    # a real create is still gated
    code, error = bd.create(["title"], tmp_path)
    assert code == 1
    assert "label violations" in error


def test_augment_labels_merges_triplet_dedup():
    ident = ("github", "agentguides", "runtime")
    records = [
        {"id": "x-1", "labels": ["origin:backfill"]},
        {"id": "x-2"},  # no labels key
        {"id": "x-3", "labels": ["provider:github"]},  # partial triplet already present
    ]
    out = bd.augment_labels(records, ident)
    triplet = ["provider:github", "org:agentguides", "repo:runtime"]
    assert out[0]["labels"] == ["origin:backfill", *triplet]
    assert out[1]["labels"] == triplet
    # already-present tag is not duplicated
    assert out[2]["labels"].count("provider:github") == 1
    # source records are not mutated (immutability)
    assert "labels" not in records[1]


def test_bd_import_injects_triplet(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **k):
        captured["content"] = Path(cmd[-1]).read_text()  # temp still exists during the call
        captured["cmd"] = cmd
        return Completed(0, "", "")

    ident = ("github", "agentguides", "runtime")
    monkeypatch.setattr(bd, "_run", fake_run)
    monkeypatch.setattr(bd.validate, "has_violations", lambda **k: False)
    monkeypatch.setattr(bd, "workspace_identity", lambda cwd=None: ident)
    src = tmp_path / "backfill.jsonl"
    src.write_text('{"id":"x-1","title":"A","labels":["origin:backfill"]}\n{"id":"x-2","title":"B"}\n')
    assert bd._import([str(src)], tmp_path) == 0
    assert captured["cmd"][:2] == ["bd", "import"]
    rows = [json.loads(ln) for ln in captured["content"].splitlines() if ln.strip()]
    assert all("provider:github" in r["labels"] for r in rows)
    assert all("org:agentguides" in r["labels"] for r in rows)
    assert all("repo:runtime" in r["labels"] for r in rows)
    assert "origin:backfill" in rows[0]["labels"]  # existing label preserved


def test_bd_import_help_bypasses_label_gate(monkeypatch, tmp_path):
    # bh-8krs: `--help` must bypass both the label gate and the stdin/identity resolution.
    cmds = []
    monkeypatch.setattr(
        bd, "_run", lambda cmd, **k: cmds.append((cmd, k.get("cwd"))) or Completed(0, "", "")
    )
    monkeypatch.setattr(bd.validate, "has_violations", lambda **k: True)
    assert bd.import_labeled(["--help"], tmp_path) == (0, "")
    cmd, cwd = cmds[-1]
    assert cmd == ["bd", "import", "--help"]
    assert cwd == tmp_path


def test_bd_import_swallows_nothing_to_commit(monkeypatch, tmp_path):
    err = "Error: commit: dolt commit: nothing to commit"
    monkeypatch.setattr(bd, "_run", lambda cmd, **k: Completed(1, "", err))
    monkeypatch.setattr(bd.validate, "has_violations", lambda **k: False)
    monkeypatch.setattr(bd, "workspace_identity", lambda cwd=None: ("github", "agentguides", "run"))
    src = tmp_path / "b.jsonl"
    src.write_text('{"id":"x-1","title":"A"}\n')
    # a zero-change re-import is bd's idempotent no-op, not a failure
    assert bd._import([str(src)], tmp_path) == 0


def test_sanitize():
    assert registry.sanitize("My_Repo!!") == "my-repo"
    assert registry.sanitize("--Foo--Bar--") == "foo-bar"


def test_derive_prefix_kinds(cfg_path):
    cfg = config.load()
    assert (
        registry.derive_prefix("github", "agentguides", "infra", "org-native", cfg)[0] == "ag-infra"
    )
    assert registry.derive_prefix("github", "briancripe", "thing", "personal", cfg)[0] == "bc-thing"
    assert registry.derive_prefix("github", "x", "proto", "prototype", cfg)[0] == "proto"
    assert registry.derive_prefix("github", "x", "up", "fork", cfg)[0] == "fork-up"


def test_derive_prefix_default_bare_vs_code(cfg_path):
    cfg = config.load()
    # "newrepo" is globally unique → bare
    assert registry.derive_prefix("github", "briancripe", "newrepo", "", cfg)[0] == "newrepo"


def test_derive_prefix_long_warns(cfg_path):
    cfg = config.load()
    pref, warns = registry.derive_prefix("github", "x", "a-very-long-repo-name", "prototype", cfg)
    assert any(">8 recommended" in w for w in warns)


# ---- classify ---------------------------------------------------------------


def test_classify_required_and_excluded(cfg_path):
    cfg = config.load()
    assert registry.classify("github", "agentguides", "infra", cfg) == "org-native"
    assert registry.classify("github", "ExcludedOrg", "anything", cfg) == "excluded"


def test_required_violations_flagship_bare_prefix(cfg_path):
    # bh-sva7: a flagship repo (repo == org) may use the bare org code as its prefix.
    cfg = config.load()
    cfg["managed_repos"] = [
        {"provider": "github", "org": "agentguides", "repo": "agentguides", "prefix": "ag"},
        {"provider": "github", "org": "agentguides", "repo": "infra", "prefix": "infra"},
        {"provider": "github", "org": "agentguides", "repo": "docs", "prefix": "ag-docs"},
    ]
    violations = registry.required_violations(cfg)
    assert "agentguides/agentguides: ag != ag-*" not in violations
    assert any(v.startswith("agentguides/infra:") for v in violations)
    assert not any(v.startswith("agentguides/docs:") for v in violations)


def test_classify_fork(cfg_path, monkeypatch):
    cfg = config.load()
    monkeypatch.setattr(registry.shutil, "which", lambda _: "/usr/bin/gh")
    payload = json.dumps({"isFork": True, "parent": {"owner": {"login": "up"}, "name": "stream"}})
    monkeypatch.setattr(registry, "run", lambda *a, **k: Completed(0, payload, ""))
    assert registry.classify("github", "briancripe", "myfork", cfg) == "fork upstream=up/stream"


# ---- identity ---------------------------------------------------------------


def test_workspace_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    top = tmp_path / "github" / "agentguides" / "infra"
    monkeypatch.setattr(identity, "run", lambda *a, **k: Completed(0, str(top) + "\n", ""))
    assert identity.workspace_identity() == ("github", "agentguides", "infra")


def test_workspace_identity_outside(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(identity, "run", lambda *a, **k: Completed(0, "/somewhere/else\n", ""))
    assert identity.workspace_identity() is None


# ---- validate ---------------------------------------------------------------


def _issues(monkeypatch, issues):
    monkeypatch.setattr(validate, "run", lambda *a, **k: Completed(0, json.dumps(issues), ""))


def test_validate_clean(cfg_path, monkeypatch):
    _issues(
        monkeypatch,
        [
            {
                "id": "ag-infra-1",
                "labels": ["provider:github", "org:agentguides", "repo:infra", "phase:1"],
            }
        ],
    )
    assert not validate.has_violations()


def test_validate_triplet_mismatch(cfg_path, monkeypatch):
    _issues(monkeypatch, [{"id": "ag-infra-1", "labels": ["org:wrong"]}])
    assert validate.has_violations()


def test_validate_closed_dimensions_and_unknown_prefix(cfg_path, monkeypatch):
    cfg = config.load()
    # any closed dimension is validated, not just phase
    _issues(monkeypatch, [{"id": "ag-infra-1", "labels": ["phase:9"]}])
    assert any("bad-phase" in p for p in validate._issue_checks(cfg)[0])
    _issues(monkeypatch, [{"id": "ag-infra-1", "labels": ["size:huge"]}])
    assert any("bad-size" in p for p in validate._issue_checks(cfg)[0])
    # open dimensions (no `values:`) accept anything
    _issues(monkeypatch, [{"id": "ag-infra-1", "labels": ["component:whatever", "tag:wip"]}])
    assert validate._issue_checks(cfg)[0] == []
    # `values: []` is closed-but-reserved → every value is rejected until populated
    _issues(monkeypatch, [{"id": "ag-infra-1", "labels": ["reserved:anything"]}])
    assert any("bad-reserved" in p for p in validate._issue_checks(cfg)[0])
    _issues(monkeypatch, [{"id": "zzz-1", "labels": []}])
    assert any("unknown hive prefix" in p for p in validate._issue_checks(cfg)[0])


def test_validate_aggregates_identical_unregistered_prefix(cfg_path, monkeypatch, capsys):
    # bh-9iiz: many beads sharing one unregistered prefix collapse into ONE line with a count
    # + fix command, instead of N identical lines. A genuinely per-issue problem (triplet
    # mismatch) still prints on its own.
    _issues(
        monkeypatch,
        [
            {
                "id": "zzz-1",
                "labels": ["provider:github", "org:zzzorg", "repo:zzzrepo"],
            },
            {
                "id": "zzz-2",
                "labels": ["provider:github", "org:zzzorg", "repo:zzzrepo"],
            },
            {
                "id": "zzz-3",
                "labels": ["provider:github", "org:zzzorg", "repo:zzzrepo"],
            },
            {"id": "ag-infra-1", "labels": ["org:wrong"]},  # per-issue triplet mismatch
        ],
    )
    validate.validate("advisory")
    out = capsys.readouterr().out
    lines = [ln.strip() for ln in out.splitlines() if "not registered" in ln]
    assert len(lines) == 1  # aggregated, not one line per affected bead
    assert "(3 issues affected)" in lines[0]
    assert f"fix: {config.BINARY_ALIAS} hive add github/zzzorg/zzzrepo --prefix=zzz" in lines[0]
    # the per-issue triplet mismatch (a genuinely per-issue problem) still prints on its own
    assert any("ag-infra-1" in ln and "org:wrong" in ln for ln in out.splitlines())


def test_validate_db_unavailable(cfg_path, monkeypatch):
    monkeypatch.setattr(validate, "run", lambda *a, **k: Completed(1, "", "denied"))
    problems, db_ok = validate._issue_checks(config.load())
    assert problems == [] and db_ok is False


def test_bead_violations_scopes_to_a_single_bead(cfg_path):
    """`bead_violations` checks ONE bead's own labels — the intake write
    path — without ever reaching the target hive's DB. Valid triplet + closed channel is clean;
    a bad closed value is flagged; the factory-seed origin now validates clean (Defect 2)."""
    cfg = config.load()
    clean = [
        "provider:github",
        "org:agentguides",
        "repo:infra",
        "origin:escalation",
        "intake:untriaged",
    ]
    assert validate.bead_violations(cfg, "ag-infra-1", clean) == []
    # the HQ factory synthetic-identity origin is a registered closed value
    assert validate.bead_violations(cfg, "ag-infra-1", ["origin:factory-seed"]) == []
    # a bad closed value is still caught, scoped to this bead
    bad = validate.bead_violations(cfg, "ag-infra-1", ["origin:carrier-pigeon"])
    assert any("bad-origin" in p for p in bad)


# ---- intake + outbound state vocabulary ----------------


def test_state_dimensions_are_closed_regardless_of_config(cfg_path):
    """The built-in intake/outbound/publish/origin state dims are code-owned — present in the
    closed set even though the fixture config never declares them."""
    closed = registry.closed_dimensions(config.load())
    # `untriaged` plus the terminal values a triage disposition transitions to
    assert closed["intake"] == {"untriaged", "accepted", "rejected", "rerouted", "promoted"}
    assert closed["outbound"] == {"pending"}
    assert closed["publish"] == {"approved"}
    # `factory-seed` is the HQ factory synthetic-identity channel
    assert closed["origin"] == {"report", "github", "import", "escalation", "factory-seed"}


def test_validate_accepts_valid_state_labels(cfg_path, monkeypatch):
    """intake/outbound/publish/origin beads carrying an allowed value validate clean."""
    cfg = config.load()
    for label in (
        "intake:untriaged",
        "outbound:pending",
        "publish:approved",
        "origin:report",
        "origin:github",
        "origin:import",
        "origin:escalation",
        "origin:factory-seed",
    ):
        _issues(monkeypatch, [{"id": "ag-infra-1", "labels": [label]}])
        assert validate._issue_checks(cfg)[0] == [], label


def test_validate_rejects_bogus_state_value(cfg_path, monkeypatch):
    """An unknown value on a closed state dimension is rejected by the validator."""
    cfg = config.load()
    _issues(monkeypatch, [{"id": "ag-infra-1", "labels": ["outbound:bogus"]}])
    assert any("bad-outbound" in p for p in validate._issue_checks(cfg)[0])
    _issues(monkeypatch, [{"id": "ag-infra-1", "labels": ["intake:whenever"]}])
    assert any("bad-intake" in p for p in validate._issue_checks(cfg)[0])
    _issues(monkeypatch, [{"id": "ag-infra-1", "labels": ["origin:carrier-pigeon"]}])
    assert any("bad-origin" in p for p in validate._issue_checks(cfg)[0])


def test_state_queue_predicates():
    """Helpers resolve the untriaged-intake and outbound-candidate queues."""
    assert state.is_untriaged_intake(["intake:untriaged", "org:x"])
    assert not state.is_untriaged_intake(["org:x"])
    assert not state.is_untriaged_intake(None)
    # a staged candidate is outbound:pending and not yet filed upstream
    assert state.is_outbound_candidate(["outbound:pending"])
    assert not state.is_outbound_candidate(["outbound:pending", "publish:approved"])
    assert not state.is_outbound_candidate(["org:x"])


def test_origin_of_resolves_the_intake_channel():
    """origin_of reads the `origin:<value>` label; is_report_origin resolves the report channel."""
    assert state.origin_of(["origin:report", "org:x"]) == "report"
    assert state.origin_of(["origin:github"]) == "github"
    assert state.origin_of(["org:x"]) is None
    assert state.origin_of(None) is None
    # a bogus origin label is not a valid channel
    assert state.origin_of(["origin:carrier-pigeon"]) is None
    assert state.is_report_origin(["origin:report"])
    assert not state.is_report_origin(["origin:github"])
    assert not state.is_report_origin(None)


def test_origin_derived_from_source_system_without_double_stamping():
    """Imported beads carry a native source_system but NO origin label — the channel is DERIVED
    on read (source_system → origin) so triage is uniform without re-stamping the import."""
    assert state.origin_from_source_system("github") == "github"
    assert state.origin_from_source_system("import") == "import"
    assert state.origin_from_source_system("GitHub") == "github"  # case-normalized
    assert state.origin_from_source_system(None) is None
    assert state.origin_from_source_system("") is None
    assert state.origin_from_source_system("mystery-tracker") is None
    # channel_of unifies both inputs: explicit origin label wins, else derive from source_system
    assert state.channel_of(["origin:report"], "github") == "report"
    assert state.channel_of(["org:x"], "github") == "github"  # imported: derived, not re-stamped
    assert state.channel_of(["org:x"], None) is None


# ---- register round-trip ----------------------------------------------------


def test_bd_passthrough(monkeypatch):
    calls = []
    monkeypatch.setattr(bd, "_run", lambda cmd, **k: calls.append(cmd) or Completed(0, "", ""))
    monkeypatch.setattr(bd.validate, "has_violations", lambda **k: False)
    # non-create (CWD) forwards verbatim
    bd.passthrough("cwd", None, ["ready"])
    assert calls[-1] == ["bd", "ready"]
    # create inside a hive injects the triplet
    monkeypatch.setattr(
        bd, "workspace_identity", lambda cwd=None: ("github", "agentguides", "infra")
    )
    bd.passthrough("cwd", None, ["create", "Fix login", "-p", "1"])
    assert calls[-1] == [
        "bd",
        "create",
        "Fix login",
        "-p",
        "1",
        "-l",
        "provider:github,org:agentguides,repo:infra",
    ]
    # create outside a managed path → plain create
    monkeypatch.setattr(bd, "workspace_identity", lambda cwd=None: None)
    bd.passthrough("cwd", None, ["create", "x"])
    assert calls[-1] == ["bd", "create", "x"]


def test_bd_create_blocks_on_violations(monkeypatch):
    monkeypatch.setattr(bd, "_run", lambda *a, **k: Completed(0, "", ""))
    monkeypatch.setattr(bd.validate, "has_violations", lambda **k: True)
    with pytest.raises(typer.Exit):  # CWD create blocked → exit 1
        bd.passthrough("cwd", None, ["create", "x"])
    # non-create commands are never gated
    bd.passthrough("cwd", None, ["ready"])  # does not raise


def test_bd_create_violation_message_names_real_cli(monkeypatch):
    # bh-nqyv: the label-violation error names the real `bh labels validate` verb, not a bare
    # retired `ws ...` command.
    monkeypatch.setattr(bd.validate, "has_violations", lambda **k: True)
    _code, error = bd.create(["x"], "cwd")
    assert f"'{config.BINARY_ALIAS} labels validate'" in error
    assert "ws " not in error


# ---- passthrough gating (bh bd / bh git) ------------------------------------


def _clear_pass_env(monkeypatch):
    for name in (
        "BH_DEBUG",
        "BH_BD_PASS_ENABLED",
        "BH_GIT_PASS_ENABLED",
        "WS_DEBUG",
        "WS_BD_PASS_ENABLED",
        "WS_GIT_PASS_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


def test_pass_enabled_defaults(monkeypatch):
    _clear_pass_env(monkeypatch)
    # bd off by default, git on by default
    assert config.bd_pass_enabled({}) is False
    assert config.git_pass_enabled({}) is True


def test_pass_enabled_config_layer(monkeypatch):
    _clear_pass_env(monkeypatch)
    assert config.bd_pass_enabled({"passthrough": {"bd_enabled": True}}) is True
    assert config.git_pass_enabled({"passthrough": {"git_enabled": False}}) is False


def test_pass_enabled_env_wins_over_config(monkeypatch):
    _clear_pass_env(monkeypatch)
    monkeypatch.setenv("BH_BD_PASS_ENABLED", "1")
    assert config.bd_pass_enabled({"passthrough": {"bd_enabled": False}}) is True
    monkeypatch.setenv("BH_GIT_PASS_ENABLED", "0")
    assert config.git_pass_enabled({"passthrough": {"git_enabled": True}}) is False


def test_pass_enabled_bh_debug_umbrella(monkeypatch):
    _clear_pass_env(monkeypatch)
    monkeypatch.setenv("BH_DEBUG", "1")
    # umbrella forces both on even against a config/env that would disable them
    assert config.bd_pass_enabled({"passthrough": {"bd_enabled": False}}) is True
    assert config.git_pass_enabled({"passthrough": {"git_enabled": False}}) is True


def test_bd_passthrough_gated_by_default(monkeypatch):
    """CLI `bh bd` exits non-zero and never invokes bd when disabled (the default)."""
    from typer.testing import CliRunner

    from beadhive.cli import app

    _clear_pass_env(monkeypatch)
    monkeypatch.setattr(config, "load", lambda: {})
    calls = []
    monkeypatch.setattr(bd, "passthrough", lambda *a, **k: calls.append(a))
    res = CliRunner().invoke(app, ["bd", "ready"])
    assert res.exit_code != 0
    assert calls == []  # bd was NOT run
    assert "disabled" in res.output


def test_bd_passthrough_reenabled_by_env(monkeypatch):
    from typer.testing import CliRunner

    from beadhive.cli import app

    _clear_pass_env(monkeypatch)
    monkeypatch.setattr(config, "load", lambda: {})
    calls = []
    monkeypatch.setattr(bd, "passthrough", lambda *a, **k: calls.append(a))
    monkeypatch.setenv("BH_BD_PASS_ENABLED", "1")
    res = CliRunner().invoke(app, ["bd", "ready"])
    assert res.exit_code == 0
    assert calls and calls[-1][-1] == ["ready"]


def test_bd_passthrough_reenabled_by_bh_debug(monkeypatch):
    from typer.testing import CliRunner

    from beadhive.cli import app

    _clear_pass_env(monkeypatch)
    monkeypatch.setattr(config, "load", lambda: {})
    calls = []
    monkeypatch.setattr(bd, "passthrough", lambda *a, **k: calls.append(a))
    monkeypatch.setenv("BH_DEBUG", "1")
    res = CliRunner().invoke(app, ["bd", "ready"])
    assert res.exit_code == 0
    assert calls


def test_git_passthrough_enabled_by_default(monkeypatch):
    from typer.testing import CliRunner

    from beadhive import git as git_mod
    from beadhive.cli import app

    _clear_pass_env(monkeypatch)
    monkeypatch.setattr(config, "load", lambda: {})
    calls = []
    monkeypatch.setattr(git_mod, "passthrough", lambda *a, **k: calls.append(a))
    res = CliRunner().invoke(app, ["git", "status"])
    assert res.exit_code == 0
    assert calls  # git still runs by default


def test_deep_merge_unions_lists_preserving_existing():
    base = {
        "permissions": {"deny": ["Bash(rm:*)"]},
        "hooks": {"SessionStart": [{"hooks": [{"command": "existing"}]}]},
    }
    addon = {
        "permissions": {"deny": ["Bash(bd remember:*)"]},
        "hooks": {"SessionStart": [{"hooks": [{"command": "bd prime --hook-json"}]}]},
    }
    merged = hive._deep_merge(base, addon)
    assert merged["permissions"]["deny"] == ["Bash(rm:*)", "Bash(bd remember:*)"]
    assert len(merged["hooks"]["SessionStart"]) == 2
    # idempotent: merging the addon again adds nothing
    assert hive._deep_merge(merged, addon) == merged


# ---- work.conflict.union_globs -------------------------------------------


def test_union_globs_default_empty():
    assert config.union_globs({}, None) == []
    assert config.union_globs({}, {}) == []


def test_union_globs_global():
    cfg = {"work": {"conflict": {"union_globs": ["CHANGELOG*", "*.jsonl"]}}}
    assert config.union_globs(cfg, None) == ["CHANGELOG*", "*.jsonl"]


def test_union_globs_per_hive_override_wins():
    cfg = {"work": {"conflict": {"union_globs": ["CHANGELOG*"]}}}
    entry = {"work": {"conflict": {"union_globs": ["*.jsonl", "registry.txt"]}}}
    assert config.union_globs(cfg, entry) == ["*.jsonl", "registry.txt"]


def test_register_preserves_comments_and_flow(cfg_path):
    registry.register("github", "briancripe", "newthing", "newthing", "prototype")
    text = cfg_path.read_text()
    # comments survive
    assert "# ws config" in text
    assert "policy: required = org-native" in text
    # new entry is a single flow-style line, sorted in
    assert '{"provider": "github", "org": "briancripe", "repo": "newthing"' in text
    # existing entries untouched
    assert '"repo": "infra", "prefix": "ag-infra"' in text
    # reloads and parses
    cfg = config.load()
    assert any(str(e["repo"]) == "newthing" for e in cfg["managed_repos"])
