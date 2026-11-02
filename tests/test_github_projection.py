"""GitHub Issues -> public store projection: pull-only, identity-preserving (bh-lniwf.6).

Every bd answer here is RECORDED, never live. `tests/fixtures/github_projection/` holds what
bd 1.3.0 printed for `bd github sync --pull-only --prefer-github` and `bd list --all --json`
when run against a scratch detached store with GitHub served by a local stub returning
`github-issues-page1.json` then `github-issues-page2.json` (issue #1 retitled and relabelled,
#2 reopened, #4 new, PR #3 filtered by bd). No test opens a socket or calls `bd github`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from beadhive import bd as bd_mod
from beadhive import cli, github_projection, guard, store_target
from beadhive.github_projection import ProjectionError
from beadhive.store_target import StoreMissing, StoreNotConfigured
from beadhive_plugins.credentials import ResolvedToken

FIXTURES = Path(__file__).parent / "fixtures" / "github_projection"
TOKEN = "ghp_recordedFixtureTokenNeverReal"
PULL = ["github", "sync", "--pull-only", "--prefer-github"]


def _token_source(value):
    """An injected zero-argument TokenSource (the shared port): fixed value, or None."""
    return lambda: ResolvedToken(value, "injected") if value else None


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def _rows(name: str) -> list[dict]:
    return json.loads(_fixture(name))


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    checkout = root / "github" / "acme" / "widget"
    (checkout / ".beads").mkdir(parents=True)
    (checkout / ".beads" / "sentinel").write_text("internal")
    public_root = tmp_path / "stores" / "widget-public"
    (public_root / ".beads").mkdir(parents=True)
    monkeypatch.setenv("GIT_WORKSPACE", str(root))
    monkeypatch.delenv("BEADS_DIR", raising=False)
    return SimpleNamespace(root=root, checkout=checkout, public_root=public_root, tmp=tmp_path)


def _entry(ws, **public):
    return {
        "provider": "github",
        "org": "acme",
        "repo": "widget",
        "prefix": "wid",
        "stores": {
            "internal": {"remote": "widget-private"},
            "public": {
                "prefix": "widpub",
                "path": str(ws.public_root),
                "remote": "git-origin",
                "source": "github-issues",
                **public,
            },
        },
    }


#: What bd 1.3.0 printed for `bd diff HEAD WORKING --json` on a scratch store (exit 0 both ways):
#: a clean working set is plain text, NOT `[]`; a dirty one is a JSON list of these entries.
DIFF_CLEAN = "No changes between HEAD and WORKING\n"
DIFF_ENTRY = {
    "IssueID": "pr-d9q",
    "DiffType": "added",
    "OldValue": None,
    "NewValue": {
        "id": "pr-d9q",
        "title": "x",
        "status": "open",
        "priority": 2,
        "created_at": "0001-01-01T00:00:00Z",
        "updated_at": "0001-01-01T00:00:00Z",
    },
}


def _diff_out(count: int) -> str:
    if not count:
        return DIFF_CLEAN
    return json.dumps([dict(DIFF_ENTRY, IssueID=f"pr-{n}") for n in range(count)], indent=2)


class RecordedBd:
    """Stands in for ``beadhive.bd.run`` beneath the REAL StoreTransport: replays recorded
    stdout per argv shape and records every call (argv, -C dir, env)."""

    def __init__(self, *, pull="pull-1.txt", rows="list-after-pull-1.json", config=None):
        self.calls: list[SimpleNamespace] = []
        self.pull_out = _fixture(pull) if pull else ""
        self.pull_rc = 0
        self.pull_err = ""
        self.rows = _rows(rows) if rows else []
        self.config = config if config is not None else {"issue_prefix": "widpub"}
        # Working-set rows `bd diff HEAD WORKING --json` reports; a pull leaves them there
        # (bd 1.3.0 embedded: HEAD stays at `bd init`), and `bd dolt commit` clears them.
        self.uncommitted = len(self.rows)
        self.diff_rc = 0
        self.diff_out: str | None = None
        self.commit_rc = 0

    def __call__(self, args, cwd, **kw):
        self.calls.append(SimpleNamespace(args=list(args), cwd=cwd, **kw))
        if args[:2] == ["github", "sync"]:
            return subprocess.CompletedProcess(args, self.pull_rc, self.pull_out, self.pull_err)
        if args[:2] == ["config", "list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(self.config), "")
        if args[:1] == ["list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(self.rows), "")
        if args == ["diff", "HEAD", "WORKING", "--json"]:
            out = self.diff_out
            if out is None:
                out = _diff_out(self.uncommitted)
            return subprocess.CompletedProcess(args, self.diff_rc, out, "")
        if args[:2] == ["dolt", "commit"]:
            if self.commit_rc == 0:
                self.uncommitted = 0
            return subprocess.CompletedProcess(
                args, self.commit_rc, "", "boom" * (self.commit_rc > 0)
            )
        raise AssertionError(f"unexpected bd call {args}")

    def commit_calls(self):
        return [c for c in self.calls if c.args[:2] == ["dolt", "commit"]]

    def github_calls(self):
        return [c for c in self.calls if c.args[:1] == ["github"]]


@pytest.fixture
def recorded(monkeypatch):
    fake = RecordedBd()
    monkeypatch.setattr(bd_mod, "run", fake)
    return fake


def _project(ws, **kw):
    kw.setdefault("token_source", _token_source(TOKEN))
    return github_projection.project(_entry(ws), **kw)


# --- resolution: the public store only, and only a pull-only github-issues one -------------


def test_legacy_hive_is_refused_never_answered_from_its_one_store(ws, recorded):
    legacy = {k: v for k, v in _entry(ws).items() if k != "stores"}
    with pytest.raises(StoreNotConfigured):
        github_projection.project(legacy, token_source=_token_source(TOKEN))
    assert recorded.calls == []


def test_internal_only_hive_is_refused(ws, recorded):
    entry = _entry(ws)
    del entry["stores"]["public"]
    with pytest.raises(StoreNotConfigured, match="no public store"):
        github_projection.project(entry, token_source=_token_source(TOKEN))
    assert recorded.calls == []


def test_a_public_store_with_sync_none_is_not_projected(ws, recorded):
    with pytest.raises(ProjectionError, match="pull-only"):
        github_projection.project(_entry(ws, sync="none"), token_source=_token_source(TOKEN))
    assert recorded.calls == []


def test_a_public_store_without_the_github_source_is_not_projected(ws, recorded):
    entry = _entry(ws)
    del entry["stores"]["public"]["source"]
    with pytest.raises(ProjectionError, match="github-issues"):
        github_projection.project(entry, token_source=_token_source(TOKEN))


def test_an_uninitialised_public_store_is_refused_before_any_bd_call(ws, recorded):
    (ws.public_root / ".beads").rmdir()
    with pytest.raises(StoreMissing):
        _project(ws)
    assert recorded.calls == []


def test_owner_and_repo_come_from_the_hive_identity(ws):
    entry = _entry(ws)
    entry.update(org="Acme", repo="widget")
    projection = github_projection.resolve_projection(entry)
    assert projection.slug == "Acme/widget"
    assert github_projection.child_env(projection, None) == {
        "GITHUB_OWNER": "Acme",
        "GITHUB_REPO": "widget",
    }


def test_a_non_github_hive_is_refused(ws):
    entry = _entry(ws)
    entry["provider"] = "gitlab"
    with pytest.raises(ProjectionError, match="not a GitHub repository"):
        github_projection.resolve_projection(entry)


# --- the pull ------------------------------------------------------------------------------


def test_first_pull_runs_pull_only_in_the_public_store_with_ambient_credentials(ws, recorded):
    before = sorted(p.name for p in (ws.checkout / ".beads").iterdir())

    report = _project(ws)

    (pull,) = recorded.github_calls()
    assert pull.args == PULL
    assert pull.cwd == ws.public_root  # `-C <store root>`: bd ignores BEADS_DIR under -C
    assert pull.env["BEADS_DIR"] == str(ws.public_root / ".beads")
    assert pull.env["GITHUB_OWNER"] == "acme" and pull.env["GITHUB_REPO"] == "widget"
    assert pull.env["GITHUB_TOKEN"] == TOKEN
    assert all(call.cwd == ws.public_root for call in recorded.calls)
    assert not any("push" in call.args or "--push-only" in call.args for call in recorded.calls)
    # the internal store is never opened, let alone written
    assert sorted(p.name for p in (ws.checkout / ".beads").iterdir()) == before
    assert (ws.checkout / ".beads" / "sentinel").read_text() == "internal"

    assert (report.pulled, report.created, report.updated) == (2, 2, 0)
    assert report.projected == 2 and report.ok
    assert report.repository == "acme/widget"


def test_second_pull_updates_in_place_and_keeps_identity(ws, recorded):
    first = {r["external_ref"]: r["id"] for r in _rows("list-after-pull-1.json")}
    recorded.pull_out = _fixture("pull-2.txt")
    recorded.rows = _rows("list-after-pull-2.json")

    report = _project(ws)

    second = {r["external_ref"]: r["id"] for r in recorded.rows}
    for ref, bead in first.items():
        assert second[ref] == bead  # same bead, updated in place — never a duplicate
    assert (report.pulled, report.created, report.updated) == (3, 1, 2)
    assert report.projected == 3 and report.ok
    retitled = next(r for r in recorded.rows if r["external_ref"].endswith("/issues/1"))
    assert retitled["title"] == "Crash on start (macOS)"
    assert "os:macos" in retitled["labels"]


def test_a_noop_repull_reports_nothing_pulled_and_stays_consistent(ws, recorded):
    recorded.pull_out = ""  # bd prints nothing when the incremental pull finds no change
    report = _project(ws)
    assert (report.pulled, report.created, report.updated) == (0, 0, 0)
    assert report.projected == 2 and report.ok


def test_dry_run_previews_and_never_lists_or_writes(ws, recorded):
    recorded.pull_out = _fixture("pull-2-dry-run.txt")
    report = _project(ws, dry_run=True)
    (pull,) = recorded.github_calls()
    assert pull.args == [*PULL, "--dry-run"]
    assert report.would_import == [4] and report.would_update == [1, 2]
    assert not any(call.args[:1] == ["list"] for call in recorded.calls)
    assert report.ok


def test_a_duplicated_or_foreign_external_ref_is_reported(ws, recorded):
    rows = _rows("list-after-pull-1.json")
    duplicate = dict(rows[0], id="widpub-dup")
    foreign = dict(rows[1], id="widpub-x", external_ref="https://github.com/other/repo/issues/9")
    recorded.rows = [*rows, duplicate, foreign]
    report = _project(ws)
    assert not report.ok
    assert any("widpub-dup: duplicates" in p for p in report.problems)
    assert any("widpub-x: external_ref" in p and "acme/widget" in p for p in report.problems)
    assert report.projected == 2


def test_external_ref_identity_is_the_issue_url_of_this_repository():
    projection = SimpleNamespace(owner="acme", repo="widget", slug="acme/widget")
    rows = [
        {"id": "a", "external_ref": "https://github.com/Acme/Widget/issues/12"},
        {"id": "b", "external_ref": "https://github.com/acme/widget/pull/3"},
        {"id": "c", "external_ref": None},
        {"id": "d", "external_ref": "https://github.com/acme/widget-two/issues/1"},
    ]
    projected, problems = github_projection.verify_identity(rows, projection)
    assert projected == 1
    assert [p.split(":")[0] for p in problems] == ["b", "c", "d"]


def test_a_bd_failure_is_reported_with_the_credential_scrubbed(ws, recorded):
    recorded.pull_rc = 1
    recorded.pull_err = f"Error: GET https://api.github.com/... token {TOKEN} rejected (401)\n"
    with pytest.raises(ProjectionError) as info:
        _project(ws)
    assert TOKEN not in str(info.value) and "***" in str(info.value)
    assert "internal store and routing were not touched" in str(info.value)
    assert not any(call.args[:1] == ["list"] for call in recorded.calls)


def test_store_config_naming_another_repository_is_refused_before_pulling(ws, recorded):
    recorded.config = {"github.owner": "someone-else", "github.repo": "widget"}
    with pytest.raises(ProjectionError, match="github.owner=someone-else"):
        _project(ws)
    assert recorded.github_calls() == []


def test_store_config_naming_the_same_repository_is_accepted(ws, recorded):
    recorded.config = {"github.repository": "ACME/widget"}
    assert _project(ws).ok


def test_no_ambient_token_leaves_the_credential_to_the_stores_host_local_config(
    ws, recorded, monkeypatch
):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    _project(ws, token_source=_token_source(None))
    (pull,) = recorded.github_calls()
    assert "GITHUB_TOKEN" not in pull.env


def test_ambient_token_uses_the_launchers_precedence_and_reports_the_winner(monkeypatch):
    from beadhive import run as run_mod

    both = github_projection.ambient_token({"GITHUB_TOKEN": "a", "GH_TOKEN": "b"})
    assert (both.value, both.source) == ("a", "env:GITHUB_TOKEN")
    only = github_projection.ambient_token({"GH_TOKEN": "b"})
    assert (only.value, only.source) == ("b", "env:GH_TOKEN")
    monkeypatch.setattr(run_mod, "_derive_gh_auth_token", lambda: None)
    assert github_projection.ambient_token({}) is None


def test_a_bd_failure_echoing_an_unknown_token_shape_is_scrubbed_too(ws, recorded):
    stray = "ghp_" + "Q" * 36
    recorded.pull_rc = 1
    recorded.pull_err = f"Error: upstream echoed {stray} back\n"
    with pytest.raises(ProjectionError) as info:
        _project(ws)
    assert stray not in str(info.value) and "***" in str(info.value)


# --- the guard: only the pull-only shape ever runs against a public store -----------------


@pytest.mark.parametrize(
    "argv",
    [
        PULL,
        ["github", "sync", "--pull-only"],
        [*PULL, "--dry-run"],
        [*PULL, "--json"],
    ],
)
def test_guard_admits_only_pull_only_sync(argv):
    assert guard.public_store_github_refusal(argv) is None


@pytest.mark.parametrize(
    "argv",
    [
        ["github", "sync"],
        ["github", "sync", "--push-only"],
        ["github", "sync", "--pull-only", "--push-only"],
        ["github", "sync", "--pull-only", "--issues", "widpub-1"],
        ["github", "sync", "--pull-only", "--prefer-local"],
        ["github", "push", "--issues", "widpub-1"],
        ["github", "pull", "12"],
        ["github"],
    ],
)
def test_guard_refuses_every_push_or_bidirectional_shape(argv):
    assert guard.public_store_github_refusal(argv) is not None


def test_guard_ignores_non_github_verbs():
    assert guard.public_store_github_refusal(["list", "--all"]) is None


# --- the CLI verb -------------------------------------------------------------------------


@pytest.fixture
def cli_fleet(ws, tmp_path, monkeypatch, recorded):
    cfg_path = tmp_path / "config.yaml"
    entry = json.dumps(_entry(ws))
    cfg_path.write_text(f"schema_version: 1\nmanaged_repos:\n  - {entry}\n")
    monkeypatch.setenv("BH_CONFIG", str(cfg_path))
    monkeypatch.setenv("BH_HOME", str(tmp_path / "bhhome"))
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    return recorded


def test_cli_projects_and_emits_the_versioned_report(cli_fleet):
    result = CliRunner().invoke(cli.app, ["hive", "project-issues", "wid", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["schema_version"] == github_projection.PROJECT_ISSUES_SCHEMA
    assert doc["command"] == "hive project-issues"
    assert doc["ok"] and doc["projected"] == 2 and doc["created"] == 2
    assert TOKEN not in result.output


def test_cli_dry_run_renders_a_preview(cli_fleet):
    cli_fleet.pull_out = _fixture("pull-2-dry-run.txt")
    result = CliRunner().invoke(cli.app, ["hive", "project-issues", "wid", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output and "would import 1 issue(s), update 2" in result.output


def test_cli_refuses_a_hive_without_a_public_store(ws, tmp_path, monkeypatch, recorded):
    cfg_path = tmp_path / "config.yaml"
    legacy = {k: v for k, v in _entry(ws).items() if k != "stores"}
    cfg_path.write_text(f"schema_version: 1\nmanaged_repos:\n  - {json.dumps(legacy)}\n")
    monkeypatch.setenv("BH_CONFIG", str(cfg_path))
    monkeypatch.setenv("BH_HOME", str(tmp_path / "bhhome"))
    result = CliRunner().invoke(cli.app, ["hive", "project-issues", "wid"])
    assert result.exit_code == 1
    assert "no public store" in result.output
    assert recorded.calls == []


def test_cli_exits_nonzero_when_identity_is_broken(cli_fleet):
    rows = _rows("list-after-pull-1.json")
    cli_fleet.rows = [*rows, dict(rows[0], id="widpub-dup")]
    result = CliRunner().invoke(cli.app, ["hive", "project-issues", "wid"])
    assert result.exit_code == 1
    assert "duplicates" in result.output


def test_store_target_is_resolved_public(ws):
    target = store_target.resolve_store(_entry(ws), "public")
    assert github_projection.resolve_projection(_entry(ws)).target == target


# --- `bh hive remote sync --store` (bh-31rf7): pull the tracker, push the replica ------------


@pytest.fixture
def pushes(monkeypatch):
    """Record publish_public calls instead of touching bd/Dolt/Git."""
    from beadhive import public_publish

    calls: list[dict] = []

    def fake(entry, **kw):
        calls.append(kw)
        return public_publish.PublishReport(
            hive="github/acme/widget",
            remote="git-origin",
            destination="https://github.com/acme/widget.git",
            dry_run=bool(kw.get("dry_run")),
            pushed=not kw.get("dry_run"),
        )

    monkeypatch.setattr(public_publish, "publish_public", fake)
    return calls


def test_remote_sync_pulls_then_pushes_fast_forward_only(cli_fleet, pushes):
    result = CliRunner().invoke(
        cli.app, ["hive", "remote", "sync", "wid", "--store", "widpub", "--json"]
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["command"] == "hive remote sync" and doc["store"] == "widpub"
    assert doc["pull"]["created"] == 2 and doc["push"]["pushed"] is True
    assert len(pushes) == 1 and not pushes[0].get("lease")  # never a replacement


def test_remote_sync_pull_only_pushes_nothing(cli_fleet, pushes):
    result = CliRunner().invoke(
        cli.app, ["hive", "remote", "sync", "wid", "--store", "public", "--pull-only"]
    )
    assert result.exit_code == 0, result.output
    assert pushes == []


def test_remote_sync_does_not_push_after_a_failed_pull(cli_fleet, pushes):
    rows = _rows("list-after-pull-1.json")
    cli_fleet.rows = [*rows, dict(rows[0], id="widpub-dup")]
    result = CliRunner().invoke(cli.app, ["hive", "remote", "sync", "wid", "--store", "widpub"])
    assert result.exit_code == 1 and pushes == []


def test_remote_sync_passes_the_lease_and_skips_the_pull(cli_fleet, pushes):
    sha = "a" * 40
    result = CliRunner().invoke(
        cli.app,
        [
            "hive",
            "remote",
            "sync",
            "wid",
            "--store",
            "widpub",
            "--force-with-lease",
            sha,
            "--confirm",
        ],
    )
    assert result.exit_code == 0, result.output
    assert pushes[0]["lease"] == sha and pushes[0]["confirmed"] is True
    assert cli_fleet.calls == []  # no pull on the replacement direction


def test_remote_sync_refuses_an_unknown_or_internal_store(cli_fleet, pushes):
    for store in ("wid", "nope"):
        result = CliRunner().invoke(cli.app, ["hive", "remote", "sync", "wid", "--store", store])
        assert result.exit_code == 1 and "no tracker store" in result.output
    assert pushes == [] and cli_fleet.calls == []


def test_remote_sync_has_no_bare_force_route(cli_fleet, pushes):
    result = CliRunner().invoke(
        cli.app, ["hive", "remote", "sync", "wid", "--store", "widpub", "--force"]
    )
    assert result.exit_code != 0 and pushes == []


def test_project_issues_is_a_deprecated_alias_that_still_works(cli_fleet):
    result = CliRunner().invoke(cli.app, ["hive", "project-issues", "wid"])
    assert result.exit_code == 0 and "deprecated" in result.output


def test_legacy_hive_remote_still_takes_hives_and_flags_without_the_subcommand(
    cli_fleet, monkeypatch
):
    from beadhive import dolt_remote_reconcile

    seen = {}

    def fake(hives, *, dry_run, cfg):
        seen.update(hives=hives, dry_run=dry_run)
        return []

    monkeypatch.setattr(dolt_remote_reconcile, "reconcile", fake)
    result = CliRunner().invoke(cli.app, ["hive", "remote", "--all", "--reconcile", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert seen == {"hives": None, "dry_run": True}
    result = CliRunner().invoke(cli.app, ["hive", "remote", "wid", "--json"])
    assert result.exit_code == 0 and seen["hives"] == ["wid"]


# --- uncommitted rows after the pull (bh-cm4er) ---------------------------------------------


def test_a_pull_reports_the_rows_it_left_uncommitted_and_warns(ws, recorded):
    report = _project(ws)
    assert report.uncommitted == 2 and not report.committed and report.ok
    assert recorded.commit_calls() == []  # off by default: nothing is committed
    assert report.to_json()["uncommitted"] == 2
    text = "\n".join(github_projection.render(report))
    assert "warning: 2 row(s) from the pull are uncommitted" in text
    assert "tracker_sync.commit_after_pull" in text


def test_a_clean_working_set_reports_zero_without_a_warning(ws, recorded):
    recorded.uncommitted = 0
    report = _project(ws)
    assert report.uncommitted == 0 and report.warnings == []
    assert "warning" not in "\n".join(github_projection.render(report))


@pytest.mark.parametrize(
    "stdout", [DIFF_CLEAN, DIFF_CLEAN.strip(), "", "  \n"], ids=["text", "bare", "empty", "blank"]
)
def test_bds_plain_text_clean_diff_is_zero_not_unreadable(ws, recorded, stdout):
    recorded.diff_out = stdout
    report = _project(ws, commit_after_pull=True)
    assert report.uncommitted == 0 and report.warnings == [] and report.ok
    assert recorded.commit_calls() == []  # clean: nothing to commit


def test_a_recorded_dirty_diff_entry_counts_one_row(ws, recorded):
    recorded.diff_out = json.dumps([DIFF_ENTRY], indent=2)
    assert _project(ws).uncommitted == 1


@pytest.mark.parametrize("failure", [{"diff_rc": 1}, {"diff_out": "not json"}, {"diff_out": "{}"}])
def test_an_unreadable_working_set_is_a_warning_never_silent(ws, recorded, failure):
    for name, value in failure.items():
        setattr(recorded, name, value)
    report = _project(ws)
    assert report.uncommitted is None and report.ok
    assert "could not read the store's uncommitted rows" in report.warnings[0]
    assert report.to_json()["uncommitted"] is None


def test_commit_after_pull_commits_the_pulled_rows_with_a_descriptive_message(ws, recorded):
    report = _project(ws, commit_after_pull=True)
    (commit,) = recorded.commit_calls()
    assert commit.args == [
        "dolt",
        "commit",
        "-m",
        "bh: pull 2 GitHub issue(s) from acme/widget (2 created, 0 updated)",
    ]
    assert commit.cwd == ws.public_root and commit.env["BEADS_DIR"] == str(
        ws.public_root / ".beads"
    )
    assert report.committed and report.uncommitted == 0 and report.warnings == []
    assert report.ok
    assert "committed the pulled rows" in "\n".join(github_projection.render(report))


def test_commit_after_pull_skips_a_clean_working_set(ws, recorded):
    recorded.uncommitted = 0
    report = _project(ws, commit_after_pull=True)
    assert recorded.commit_calls() == [] and not report.committed and report.ok


def test_a_failed_commit_is_a_problem_and_the_rows_are_still_counted(ws, recorded):
    recorded.commit_rc = 1
    report = _project(ws, commit_after_pull=True)
    assert not report.ok and not report.committed
    assert "bd dolt commit` failed (boom)" in report.problems[0]
    assert report.uncommitted == 2


def test_commit_after_pull_never_commits_a_pull_whose_identity_failed(ws, recorded):
    rows = _rows("list-after-pull-1.json")
    recorded.rows = [*rows, dict(rows[0], id="widpub-dup")]
    report = _project(ws, commit_after_pull=True)
    assert not report.ok and recorded.commit_calls() == []


def test_a_dry_run_reads_no_working_set(ws, recorded):
    recorded.pull_out = _fixture("pull-2-dry-run.txt")
    report = _project(ws, dry_run=True, commit_after_pull=True)
    assert not any(c.args[:1] in (["diff"], ["dolt"]) for c in recorded.calls)
    assert report.warnings == [] and not report.committed


def test_commit_after_pull_is_read_from_config_and_defaults_off():
    enabled = github_projection.commit_after_pull_enabled
    assert enabled({}) is False and enabled(None) is False
    assert enabled({"tracker_sync": {}}) is False
    assert enabled({"tracker_sync": {"commit_after_pull": True}}) is True
    assert enabled({"tracker_sync": {"commit_after_pull": "true"}}) is False  # strict boolean


def test_remote_sync_pull_only_reports_uncommitted_rows_in_json_and_text(cli_fleet, pushes):
    argv = ["hive", "remote", "sync", "wid", "--store", "widpub", "--pull-only"]
    doc = json.loads(CliRunner().invoke(cli.app, [*argv, "--json"]).stdout)
    assert doc["pull"]["uncommitted"] == 2 and doc["pull"]["committed"] is False
    assert doc["pull"]["warnings"] and doc["ok"]
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 0 and "warning: 2 row(s)" in result.output
    assert cli_fleet.commit_calls() == []


def test_remote_sync_commits_after_pull_when_the_config_key_is_on(cli_fleet, pushes, tmp_path):
    (tmp_path / "config.yaml").write_text(
        (tmp_path / "config.yaml").read_text() + "tracker_sync:\n  commit_after_pull: true\n"
    )
    result = CliRunner().invoke(
        cli.app, ["hive", "remote", "sync", "wid", "--store", "widpub", "--pull-only", "--json"]
    )
    assert result.exit_code == 0, result.output
    pull = json.loads(result.stdout)["pull"]
    assert pull["committed"] is True and pull["uncommitted"] == 0 and pull["warnings"] == []
    assert len(cli_fleet.commit_calls()) == 1


def test_remote_sync_with_a_push_leg_settles_the_pull_warning(cli_fleet, pushes):
    doc = json.loads(
        CliRunner()
        .invoke(cli.app, ["hive", "remote", "sync", "wid", "--store", "widpub", "--json"])
        .stdout
    )
    assert doc["pull"]["uncommitted"] == 2 and doc["pull"]["warnings"] == []
    assert doc["push"]["pushed"] is True
