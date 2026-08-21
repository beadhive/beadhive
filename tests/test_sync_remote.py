"""Tests for beadhive.sync_remote — `bh hive sync-remote --all`: the guarded fleet-wide
push+verify orchestrator (bh-59q1.2).

Two layers:
  * ``assess_hive`` — pure classification (clean/dirty/unpushed-git/unpushed-dolt/blocked)
    exercised against real temporary git repos, mirroring test_safety.py's hermetic style.
  * ``sync_remote`` — the guarded orchestrator, exercised against real registered hives under
    ``workspace_root()`` (mirrors test_hive_retire.py's ``world``-fixture style): dry-run vs
    live, refuse-dirty, push-what's-safe, and the non-zero-exit offender list.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import config, sync_remote
from beadhive.engine import FederationPeer, FederationStatus
from beadhive.identity import workspace_root
from beadhive.sync_remote import SyncStatus, assess_hive

_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

# Strips CSI (ANSI) escape sequences — e.g. `\x1b[1m`, `\x1b[38;5;208m` — so a plain-substring
# assert against CLI output can't false-RED just because the operator's shell exports
# FORCE_COLOR/CLICOLOR_FORCE and Rich/Typer render `--help` as color-split spans (bh-76gx).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=_ENV
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "test@ws.dev", cwd=path)
    _git("config", "user.name", "WS Test", cwd=path)
    (path / "file.txt").write_text("hello")
    _git("add", ".", cwd=path)
    _git("commit", "-qm", "init", cwd=path)


def _stub_engine(monkeypatch, fs: FederationStatus, push_calls: list | None = None) -> None:
    """Patch the state engine with a stub whose `push_state` records into *push_calls*.

    The federation method is intentionally present only as a tripwire: ``sync remotes`` must
    assess its configured Dolt remote and never use federation status.
    """

    class _StubEngine:
        def federation_status(self, cwd, *, timeout=None):
            return fs

        def push_state(self, cwd, actor="", message="", *, remote="", force=False):
            if push_calls is None:
                raise AssertionError("push_state must not be called in this test")
            push_calls.append(str(cwd))
            return subprocess.CompletedProcess(args=[], returncode=0)

    monkeypatch.setattr(sync_remote.engine, "get_engine", lambda cfg=None: _StubEngine())
    _mark_managed_dolt_remote(monkeypatch)


_FED_TIMEOUT = FederationStatus(ok=False, error="timeout")


# ---------------------------------------------------------------------------
# assess_hive — pure classification
# ---------------------------------------------------------------------------


def test_assess_missing_clone_is_blocked(tmp_path):
    record = assess_hive("github/o/r", tmp_path / "nope")

    assert record.status == SyncStatus.BLOCKED
    assert "does not exist" in record.reasons[0]


def test_assess_not_a_repo_is_blocked(tmp_path):
    not_repo = tmp_path / "plain-dir"
    not_repo.mkdir()

    record = assess_hive("github/o/r", not_repo)

    assert record.status == SyncStatus.BLOCKED
    assert "not a git repository" in record.reasons[0]


def test_assess_no_origin_is_blocked(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)

    record = assess_hive("github/o/r", repo)

    assert record.status == SyncStatus.BLOCKED
    assert "no origin" in record.reasons[0]


def test_assess_clean_pushed_repo_is_clean(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=remote)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)

    record = assess_hive("github/o/r", repo)

    assert record.status == SyncStatus.CLEAN
    assert record.reasons == []


def test_assess_dirty_worktree_is_dirty(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=remote)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)
    (repo / "file.txt").write_text("uncommitted change")

    record = assess_hive("github/o/r", repo)

    assert record.status == SyncStatus.DIRTY
    assert "dirty branch" in record.reasons[0]


def test_assess_unpushed_git_branch(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=remote)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)
    (repo / "extra.txt").write_text("unpushed work")
    _git("add", ".", cwd=repo)
    _git("commit", "-qm", "unpushed", cwd=repo)

    record = assess_hive("github/o/r", repo)

    assert record.status == SyncStatus.UNPUSHED_GIT
    assert record.unpushed_branches == ["main"]


def test_assess_unpushed_dolt_ref(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=remote)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)
    # refs/dolt/data exists locally and on origin but has since advanced locally (ahead).
    _git("update-ref", "refs/dolt/data", "HEAD", cwd=repo)
    _git("push", "-q", "origin", "refs/dolt/data:refs/dolt/data", cwd=repo)
    (repo / "f2.txt").write_text("dolt advance")
    _git("add", ".", cwd=repo)
    _git("commit", "-qm", "dolt advance", cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)  # keep the branch itself pushed/clean
    _git("update-ref", "refs/dolt/data", "HEAD", cwd=repo)

    record = assess_hive("github/o/r", repo)

    assert record.status == SyncStatus.UNPUSHED_DOLT
    assert record.dolt_status == "ahead"


def test_assess_dolt_no_remote_counts_as_unpushed(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=remote)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)
    # Local refs/dolt/data with no copy on origin at all.
    _git("update-ref", "refs/dolt/data", "HEAD", cwd=repo)

    record = assess_hive("github/o/r", repo)

    assert record.status == SyncStatus.UNPUSHED_DOLT
    assert record.dolt_status == "no-remote"


def test_assess_dolt_ref_behind_is_labelled_not_silently_clean(tmp_path):
    """A hive with nothing local to push, but whose refs/dolt/data is BEHIND origin, must
    not disappear into an unlabelled CLEAN (bh-ummb9.3 — the measured fleet failure: a
    behind hive gave no dry-run warning before a non-fast-forward push rejection)."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=remote)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)
    _git("update-ref", "refs/dolt/data", "HEAD", cwd=repo)
    _git("push", "-q", "origin", "refs/dolt/data:refs/dolt/data", cwd=repo)
    # Advance refs/dolt/data with a plumbing commit-tree (never touches HEAD/main, so the
    # branch itself stays clean/pushed) and push it, then move the local dolt ref back —
    # origin now has a dolt commit this clone hasn't locally "caught up" to, exactly
    # mirroring a peer pushing ahead. The new commit is created IN this repo's own odb, so
    # it stays locally resolvable without a fetch (real "behind" counts, not "diverged").
    behind_sha = _git("rev-parse", "refs/dolt/data", cwd=repo).stdout.strip()
    tree_sha = _git("rev-parse", f"{behind_sha}^{{tree}}", cwd=repo).stdout.strip()
    ahead_sha = _git(
        "commit-tree", tree_sha, "-p", behind_sha, "-m", "dolt advance", cwd=repo
    ).stdout.strip()
    _git("push", "-q", "origin", f"{ahead_sha}:refs/dolt/data", cwd=repo)
    _git("update-ref", "refs/dolt/data", behind_sha, cwd=repo)

    record = assess_hive("github/o/r", repo)

    assert record.status == SyncStatus.CLEAN
    assert record.dolt_status == "behind"
    assert any("behind" in r and "pull first" in r for r in record.reasons)


def test_assess_embedded_dolt_engine_counts_as_unpushed(tmp_path, monkeypatch):
    """bd's embedded engine (bh-fl26) writes no refs/dolt/data at all — assess_hive must not
    silently classify it CLEAN just because the git-ref check found nothing."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=remote)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)
    (repo / ".beads").mkdir()
    monkeypatch.setattr(
        "beadhive.safety._bd_dolt_status_payload",
        lambda path: {"mode": "embedded", "schema_version": 1},
    )
    monkeypatch.setattr("beadhive.safety._bd_has_dolt_remote", lambda path: True)

    record = assess_hive("github/o/r", repo)

    assert record.status == SyncStatus.UNPUSHED_DOLT
    assert record.dolt_status == "unknown"
    assert any("embedded engine" in r for r in record.reasons)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"mode": "external", "database": "bh", "host": "127.0.0.1"},
            "external/shared-server engine",
        ),
        ({"data_dir": "", "running": False}, "bd-managed store"),
    ],
    ids=["external-shared-server", "legacy-mode-omitted"],
)
def test_assess_bd_managed_unknown_does_not_guess_embedded(
    tmp_path, monkeypatch, payload, expected
):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=remote)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)
    (repo / ".beads").mkdir()
    monkeypatch.setattr("beadhive.safety._bd_dolt_status_payload", lambda path: payload)
    monkeypatch.setattr("beadhive.safety._bd_has_dolt_remote", lambda path: True)

    record = assess_hive("github/o/r", repo)

    assert record.status == SyncStatus.UNPUSHED_DOLT
    assert record.dolt_status == "unknown"
    assert any(expected in reason for reason in record.reasons)
    assert all("embedded" not in reason for reason in record.reasons)


def test_dolt_reason_unknown_without_probe_metadata_is_engine_neutral():
    reason = sync_remote._dolt_reason(sync_remote.DoltRefInfo(status="unknown"))

    assert "bd-managed store" in reason
    assert "embedded" not in reason


def test_assess_dirty_wins_over_unpushed(tmp_path):
    """Dirty takes precedence: a hive both dirty AND ahead reports DIRTY, not UNPUSHED_GIT —
    refuse-to-push-over-dirty must never be masked by an also-true unpushed signal."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=remote)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)
    (repo / "extra.txt").write_text("unpushed work")
    _git("add", ".", cwd=repo)
    _git("commit", "-qm", "unpushed", cwd=repo)
    (repo / "extra.txt").write_text("dirty on top")

    record = assess_hive("github/o/r", repo)

    assert record.status == SyncStatus.DIRTY


def _make_bd_repo(tmp_path) -> Path:
    """A clean, pushed repo that is also bd-managed (`.beads/` present, no refs/dolt/data) —
    the shape whose dolt state is resolved via `_scan_bd_dolt_state`."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=remote)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)
    (repo / ".beads").mkdir()
    return repo


def test_assess_fetch_true_surfaces_real_ahead_count(tmp_path, monkeypatch):
    """fetch=True consults `bd federation status` through the engine seam: a reachable peer's
    verified ahead count replaces the no-network path's blanket 'unknown'."""
    repo = _make_bd_repo(tmp_path)
    _stub_engine(
        monkeypatch,
        FederationStatus(ok=True, peers=(FederationPeer(peer="origin", reachable=True, ahead=4),)),
    )

    record = assess_hive("github/o/r", repo, fetch=True)

    assert record.status == SyncStatus.UNPUSHED_DOLT
    assert record.dolt_status == "ahead"
    assert any("4 ahead" in r for r in record.reasons)


def test_assess_fetch_true_timeout_maps_to_unknown(tmp_path, monkeypatch):
    """A federation-status timeout (per-hive timeout enforced inside `federation_status`)
    arrives as `unknown` — the idempotent-push-attempt path — never coerced to in-sync."""
    repo = _make_bd_repo(tmp_path)
    _stub_engine(monkeypatch, _FED_TIMEOUT)

    record = assess_hive("github/o/r", repo, fetch=True)

    assert record.status == SyncStatus.UNPUSHED_DOLT
    assert record.dolt_status == "unknown"
    assert any("could not be verified" in r and "timed out" in r for r in record.reasons)


def test_assess_fetch_defaults_false_and_never_touches_engine(tmp_path, monkeypatch):
    """The default (no `fetch=`) path stays no-network: the engine seam must never be
    constructed, and the embedded-engine heuristics answer as before."""
    repo = _make_bd_repo(tmp_path)
    monkeypatch.setattr(
        "beadhive.safety._bd_dolt_status_payload",
        lambda path: {"mode": "embedded", "schema_version": 1},
    )
    monkeypatch.setattr("beadhive.safety._bd_has_dolt_remote", lambda path: True)

    def _boom(cfg=None):
        raise AssertionError("engine must not be touched when fetch is not requested")

    monkeypatch.setattr(sync_remote.engine, "get_engine", _boom)

    record = assess_hive("github/o/r", repo)

    assert record.status == SyncStatus.UNPUSHED_DOLT
    assert record.dolt_status == "unknown"


# ---------------------------------------------------------------------------
# sync_remote — the guarded fleet-wide orchestrator
# ---------------------------------------------------------------------------


def _register(provider="github", org="myorg", repo="myrepo", prefix="mr") -> None:
    cfg = config.load()
    cfg.setdefault("managed_repos", []).append(
        {"provider": provider, "org": org, "repo": repo, "prefix": prefix, "kind": "personal"}
    )
    config.save(cfg)


def _make_clean_clone(org="myorg", repo="myrepo") -> tuple[Path, Path]:
    root = Path(workspace_root())
    remote = root / "_remotes" / f"{repo}.git"
    remote.mkdir(parents=True)
    _git("init", "-q", "--bare", "-b", "main", cwd=remote)
    clone = root / "github" / org / repo
    _init_repo(clone)
    _git("remote", "add", "origin", str(remote), cwd=clone)
    _git("push", "-q", "-u", "origin", "main", cwd=clone)
    return clone, remote


def _make_ahead_clone(org="myorg", repo="myrepo") -> tuple[Path, Path]:
    clone, remote = _make_clean_clone(org=org, repo=repo)
    (clone / "extra.txt").write_text("unpushed work")
    _git("add", ".", cwd=clone)
    _git("commit", "-qm", "unpushed", cwd=clone)
    return clone, remote


def _register_hq() -> None:
    """Register the HQ store's reserved synthetic identity (kind=hq, no origin by design)."""
    cfg = config.load()
    cfg.setdefault("managed_repos", []).append(
        {"provider": "local", "org": "factory", "repo": "hq", "prefix": "hq", "kind": "hq"}
    )
    config.save(cfg)


def test_hq_entry_is_skipped_and_absent_from_plan(world, capsys):
    """HQ (kind=hq) is local-only by design — no origin, its clone lives outside the
    workspace. It must be skipped with a note, never assessed: before the filter it
    classified BLOCKED and put a clean fleet in plan.offending."""
    _make_clean_clone()
    _register()
    _register_hq()

    plan = sync_remote.sync_remote(dry_run=True)

    assert [r.hive for r in plan.records] == ["github/myorg/myrepo"]
    assert plan.offending == []
    out = capsys.readouterr().out
    assert "skipping HQ — local-only by design" in out
    assert "local/factory/hq" not in out


def test_cli_clean_fleet_with_hq_exits_zero(world):
    """The bug fix, end to end (bh-wty3.3): an all-clean fleet that includes the HQ entry
    exits 0 — before the kind=hq filter, HQ's missing origin made `hive sync-remote --all
    --dry-run` exit 1 on an otherwise-clean fleet."""
    from beadhive.cli import app

    _make_clean_clone()
    _register()
    _register_hq()

    res = CliRunner().invoke(app, ["hive", "sync-remote", "--all", "--dry-run"])

    assert res.exit_code == 0


def test_sync_remote_does_not_assess_dolt_remote_through_federation(world, monkeypatch, capsys):
    """A configured Dolt remote is not a federation peer named ``origin`` (bh-q5i2i)."""
    clone, _remote = _make_clean_clone()
    _register()
    (clone / ".beads").mkdir()
    _stub_engine(
        monkeypatch,
        FederationStatus(ok=True, peers=(FederationPeer(peer="origin", reachable=True, ahead=4),)),
    )

    plan = sync_remote.sync_remote(dry_run=True)

    assert plan.records[0].status == SyncStatus.UNPUSHED_DOLT
    assert plan.records[0].dolt_status == "unknown"
    out = capsys.readouterr().out
    assert "federation peer" not in out
    assert "would attempt: bd dolt push" in out


def test_dry_run_reports_without_mutating(world):
    clone, remote = _make_ahead_clone()
    _register()

    plan = sync_remote.sync_remote(dry_run=True)

    assert plan.dry_run is True
    assert plan.offending == []
    assert plan.pushed_branches == {}
    # Nothing actually reached the remote.
    remote_log = _git("log", "--all", "--format=%s", cwd=remote).stdout
    assert "unpushed" not in remote_log


def test_dry_run_on_absent_dolt_ref_prints_no_dolt_line(world, capsys):
    """A hive with no local `refs/dolt/data` at all (never Dolt-bootstrapped) must not get a
    misleading 'would push dolt' preview line — the dry-run condition must match the live-run
    push gate (`dolt_status in (ahead, diverged, no-remote)`) exactly (bh-jhu0)."""
    _make_ahead_clone()  # UNPUSHED_GIT status, dolt ref never created → dolt_status == "absent"
    _register()

    plan = sync_remote.sync_remote(dry_run=True)

    assert plan.records[0].dolt_status == "absent"
    out = capsys.readouterr().out
    assert "would push dolt" not in out


def test_dry_run_on_clean_hive_prints_no_dolt_line(world, capsys):
    """A fully clean, already-pushed hive also must not show 'would push dolt' (dolt_status
    'clean' is excluded from `_DOLT_PUSHABLE` just like 'absent')."""
    _make_clean_clone()
    _register()

    plan = sync_remote.sync_remote(dry_run=True)

    assert plan.records[0].status == SyncStatus.CLEAN
    out = capsys.readouterr().out
    assert "would push dolt" not in out


def test_dry_run_on_diverged_git_transport_dolt_warns_not_in_sync(world, capsys):
    """A git-transport dolt remote (refs/dolt/data exists locally) whose origin tip isn't
    locally resolvable must be named 'diverged' and warned about, not folded into the plain
    'would push dolt' line an in-sync-looking hive gets (bh-ummb9.3: this is the exact shape
    that gave no dry-run warning before the fleet's non-fast-forward push failures)."""
    clone, remote = _make_clean_clone()
    _git("update-ref", "refs/dolt/data", "HEAD", cwd=clone)
    _git("push", "-q", "origin", "refs/dolt/data:refs/dolt/data", cwd=clone)
    # An unrelated repo pushes over refs/dolt/data with foreign history, so `clone` can't
    # resolve the new tip locally without a fetch. Distinct content (never touched inside
    # `clone`) guarantees a genuinely different, unresolvable commit — not just a
    # same-second timestamp coincidence.
    other = clone.parent / "other"
    other.mkdir()
    _git("init", "-q", "-b", "main", cwd=other)
    _git("config", "user.email", "foreign@ws.dev", cwd=other)
    _git("config", "user.name", "Foreign Peer", cwd=other)
    (other / "foreign.txt").write_text("foreign dolt state, unrelated history")
    _git("add", ".", cwd=other)
    _git("commit", "-qm", "foreign dolt advance", cwd=other)
    _git("remote", "add", "origin", str(remote), cwd=other)
    _git("push", "-qf", "origin", "HEAD:refs/dolt/data", cwd=other)
    _register()

    plan = sync_remote.sync_remote(dry_run=True)

    assert plan.records[0].dolt_status == "diverged"
    out = capsys.readouterr().out
    assert "diverged from origin" in out
    assert "pull first" in out


def test_dry_run_on_unverifiable_dolt_state_prints_attempt_not_ahead(world, capsys, monkeypatch):
    """When even the fetch=True federation check can't verify the dolt state (timeout/offline
    peer — the successor of the embedded engine's blanket 'unknown', bh-fl26), dry-run must
    report an honest 'would attempt' plan, not a fabricated push line, and must never call
    push_state (the fleet assessment's federation_status read is the only engine touch)."""
    clone, _remote = _make_clean_clone()
    _register()
    (clone / ".beads").mkdir()
    _stub_engine(monkeypatch, _FED_TIMEOUT)  # push_calls=None → push_state hard-fails

    plan = sync_remote.sync_remote(dry_run=True)

    assert plan.records[0].dolt_status == "unknown"
    out = capsys.readouterr().out
    assert "would attempt: bd dolt push" in out
    assert "would push dolt: refs/dolt/data" not in out


def test_live_pushes_unverifiable_dolt_state_via_engine(world, monkeypatch):
    """Live mode just calls Engine.push_state (already-existing wiring) for the unverifiable
    'unknown' status too, trusting bd dolt push's own idempotent success/failure."""
    clone, _remote = _make_clean_clone()
    _register()
    (clone / ".beads").mkdir()

    calls: list[str] = []
    _stub_engine(monkeypatch, _FED_TIMEOUT, push_calls=calls)

    plan = sync_remote.sync_remote(dry_run=False)

    assert plan.offending == []
    assert plan.dolt_pushed == ["github/myorg/myrepo"]
    assert calls == [str(clone)]


def test_live_pushes_unpushed_git_branch(world):
    clone, remote = _make_ahead_clone()
    _register()

    plan = sync_remote.sync_remote(dry_run=False)

    assert plan.offending == []
    assert plan.pushed_branches == {"github/myorg/myrepo": ["main"]}
    remote_log = _git("log", "--all", "--format=%s", cwd=remote).stdout
    assert "unpushed" in remote_log


def test_clean_hive_is_left_alone(world):
    _make_clean_clone()
    _register()

    plan = sync_remote.sync_remote(dry_run=False)

    assert plan.offending == []
    assert plan.pushed_branches == {}
    assert plan.records[0].status == SyncStatus.CLEAN


def test_dirty_hive_is_refused_and_reported_offending(world):
    clone, _remote = _make_clean_clone()
    _register()
    (clone / "file.txt").write_text("uncommitted change")

    plan = sync_remote.sync_remote(dry_run=False)

    assert plan.offending == ["github/myorg/myrepo"]
    assert plan.pushed_branches == {}
    # Refused, not force-reset: the uncommitted change is untouched.
    assert (clone / "file.txt").read_text() == "uncommitted change"


def test_remote_only_hive_is_reported_and_not_offending_in_all_sync(world, capsys):
    _register(repo="ghost", prefix="ghost")

    plan = sync_remote.sync_remote(dry_run=False)

    assert plan.offending == []
    assert plan.records[0].status == SyncStatus.REMOTE_ONLY
    assert "remote-only hive" in capsys.readouterr().out


def test_named_missing_clone_is_blocked_and_offending(world):
    _register(repo="ghost", prefix="ghost")

    plan = sync_remote.sync_remote(dry_run=False, hive_ids=["ghost"])

    assert plan.offending == ["github/myorg/ghost"]
    assert plan.records[0].status == SyncStatus.BLOCKED


def test_git_push_failure_marks_hive_offending(world):
    clone, _remote = _make_ahead_clone()
    _register()
    # Point origin at a nonexistent remote so the push fails.
    _git("remote", "set-url", "origin", str(Path(workspace_root()) / "nope.git"), cwd=clone)

    plan = sync_remote.sync_remote(dry_run=False)

    assert plan.offending == ["github/myorg/myrepo"]
    assert plan.pushed_branches == {}


def test_git_push_failure_surfaces_underlying_error(world, capsys):
    """A failed git push must print the captured git stderr, not just the branch name, so an
    operator can tell a stale/non-fast-forward ref apart from an auth failure or anything else
    (bh-jhu0)."""
    clone, _remote = _make_ahead_clone()
    _register()
    _git("remote", "set-url", "origin", str(Path(workspace_root()) / "nope.git"), cwd=clone)

    sync_remote.sync_remote(dry_run=False)

    err = capsys.readouterr().err
    assert "failed to push git: main:" in err
    # git's real complaint (its stderr's last line, e.g. "...the repository exists.") must
    # appear after the branch name, not just the bare branch name on its own.
    line = next(ln for ln in err.splitlines() if "failed to push git: main:" in ln)
    assert line.strip() != "✗ failed to push git: main:"


def test_dolt_state_pushed_via_engine(world, monkeypatch):
    """The dolt push goes through Engine.push_state (bh-dw3e.6 wiring), not raw git."""
    clone, remote = _make_clean_clone()
    _register()
    _git("update-ref", "refs/dolt/data", "HEAD", cwd=clone)  # local-only, no-remote → unpushed

    calls = []

    class _FakeEngine:
        def push_state(self, cwd, actor="", message="", *, remote="", force=False):
            calls.append((str(cwd), message))
            return subprocess.CompletedProcess(args=[], returncode=0)

    monkeypatch.setattr(sync_remote.engine, "get_engine", lambda cfg: _FakeEngine())

    plan = sync_remote.sync_remote(dry_run=False)

    assert plan.offending == []
    assert plan.dolt_pushed == ["github/myorg/myrepo"]
    assert calls and calls[0][0] == str(clone)


def test_dolt_push_failure_marks_hive_offending(world, monkeypatch):
    clone, _remote = _make_clean_clone()
    _register()
    _git("update-ref", "refs/dolt/data", "HEAD", cwd=clone)

    class _FailingEngine:
        def push_state(self, cwd, actor="", message="", *, remote="", force=False):
            return subprocess.CompletedProcess(args=[], returncode=1)

    monkeypatch.setattr(sync_remote.engine, "get_engine", lambda cfg: _FailingEngine())

    plan = sync_remote.sync_remote(dry_run=False)

    assert plan.offending == ["github/myorg/myrepo"]
    assert plan.dolt_pushed == []


# Real `bd dolt push` stderr captured on this fleet on 2026-08-20 (bh-ummb9.4 review feedback).
# Both WRAP the actual cause and APPEND unrelated hint text after it — so a "last stderr line"
# extraction (correct for raw `git push`, see `_last_stderr_line`) yields boilerplate for
# either, and actively misleading boilerplate for the second (points at SSH auth instead of
# the real cause, a missing bare clone — bh-j42f0).
_REAL_NON_FAST_FORWARD_STDERR = (
    "! [rejected]  main -> main (non-fast-forward)\n"
    "hint: Updates were rejected because the tip of your current branch is behind its remote\n"
    "hint: counterpart. Integrate the remote changes (e.g. 'dolt pull ...') before pushing "
    "again.: exit status 1"
)
_REAL_MISSING_GIT_REMOTE_CACHE_STDERR = (
    "Error 1105 (HY000): failed to read latest version of remote database "
    "origin@git+ssh://...: fatal: not a git repository: "
    "'.../.dolt/git-remote-cache/<hash>/repo.git'\n"
    "hint: dolt does not support interactive credential prompts\n"
    "hint: ensure non-interactive auth is configured for GCM: exit status 1"
)


@pytest.mark.parametrize(
    "stderr,must_contain",
    [
        (_REAL_NON_FAST_FORWARD_STDERR, "non-fast-forward"),
        (_REAL_MISSING_GIT_REMOTE_CACHE_STDERR, "not a git repository"),
    ],
)
def test_dolt_push_failure_surfaces_underlying_error(
    world, monkeypatch, capsys, stderr, must_contain
):
    """A failed dolt push must surface `bd dolt push`'s real cause verbatim beneath the summary
    line — not a bare 'failed to push dolt' (the original defect) and not a wrong line picked
    from bd's hint-wrapped stderr (the changes-requested defect: a last-line extraction yields
    generic auth advice for both real fleet failures, actively misleading for the second)."""
    clone, _remote = _make_clean_clone()
    _register()
    _git("update-ref", "refs/dolt/data", "HEAD", cwd=clone)

    class _FailingEngine:
        def push_state(self, cwd, actor="", message="", *, remote="", force=False):
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)

    monkeypatch.setattr(sync_remote.engine, "get_engine", lambda cfg: _FailingEngine())

    sync_remote.sync_remote(dry_run=False)

    err = capsys.readouterr().err
    assert "✗ failed to push dolt: refs/dolt/data" in err
    assert must_contain in err


def test_dry_run_does_not_call_engine_push(world, monkeypatch):
    clone, _remote = _make_clean_clone()
    _register()
    _git("update-ref", "refs/dolt/data", "HEAD", cwd=clone)

    def _boom(cfg):
        raise AssertionError("engine.get_engine must not be called during --dry-run")

    monkeypatch.setattr(sync_remote.engine, "get_engine", _boom)

    plan = sync_remote.sync_remote(dry_run=True)

    assert plan.offending == []


# ---------------------------------------------------------------------------
# --verbose: recently-touched beads as content context (bh-5rn7)
# ---------------------------------------------------------------------------


def _mark_unverifiable_dolt(clone: Path, monkeypatch) -> None:
    """Make a clean clone look like a bd-managed Dolt remote whose state is unverifiable."""
    (clone / ".beads").mkdir(exist_ok=True)
    _mark_managed_dolt_remote(monkeypatch)


def _mark_managed_dolt_remote(monkeypatch) -> None:
    """Stub bd's local status probe as an existing database with a Dolt remote."""
    monkeypatch.setattr(
        "beadhive.safety._bd_dolt_status_payload",
        lambda path: {"mode": "embedded", "schema_version": 1},
    )
    monkeypatch.setattr("beadhive.safety._bd_has_dolt_remote", lambda path: True)


def test_verbose_false_makes_no_extra_query_on_unpushed_dolt_hive(world, monkeypatch, capsys):
    """Default (non-verbose) output is unchanged: no recently-touched block, and the extra
    `bd list` query never runs at all for an unpushed-dolt hive."""
    clone, _remote = _make_clean_clone()
    _register()
    _mark_unverifiable_dolt(clone, monkeypatch)

    def _boom(args, cwd):
        raise AssertionError("bd.json must not be called when --verbose is not passed")

    monkeypatch.setattr(sync_remote.bd, "json", _boom)

    plan = sync_remote.sync_remote(dry_run=True, verbose=False)

    assert plan.records[0].status == SyncStatus.UNPUSHED_DOLT
    out = capsys.readouterr().out
    assert "recently touched" not in out


def test_verbose_true_shows_bounded_list_on_unpushed_dolt_hive(world, monkeypatch, capsys):
    """--verbose on an unpushed-dolt hive prints the bounded recently-touched list, clearly
    labeled as an approximation, scoped (`-C <clone_path>`) to that hive."""
    clone, _remote = _make_clean_clone()
    _register()
    _mark_unverifiable_dolt(clone, monkeypatch)

    calls = []

    def _fake_json(args, cwd):
        calls.append((args, str(cwd)))
        return [
            {"id": "mr-1", "title": "one"},
            {"id": "mr-2", "title": "two"},
        ]

    monkeypatch.setattr(sync_remote.bd, "json", _fake_json)

    plan = sync_remote.sync_remote(dry_run=True, verbose=True)

    assert plan.records[0].status == SyncStatus.UNPUSHED_DOLT
    out = capsys.readouterr().out
    assert "recently touched (not a precise diff" in out
    assert "mr-1: one" in out
    assert "mr-2: two" in out
    # Scoped to the hive's own clone, filtered/sorted/bounded via bd's own flags.
    assert len(calls) == 1
    args, cwd = calls[0]
    assert cwd == str(clone)
    assert "list" in args
    assert "--updated-after" in args
    assert "--sort" in args and "updated" in args


def test_verbose_true_makes_no_extra_query_on_clean_hive(world, monkeypatch, capsys):
    """--verbose is gated on unpushed-dolt status, not just the flag: a clean hive never
    triggers the extra `bd list` query even with --verbose."""
    _make_clean_clone()
    _register()

    def _boom(args, cwd):
        raise AssertionError("bd.json must not be called for a clean hive even with --verbose")

    monkeypatch.setattr(sync_remote.bd, "json", _boom)

    plan = sync_remote.sync_remote(dry_run=True, verbose=True)

    assert plan.records[0].status == SyncStatus.CLEAN
    out = capsys.readouterr().out
    assert "recently touched" not in out


def test_cli_verbose_flag_documented_in_help():
    """ANSI-robust by construction (bh-76gx): strips CSI escapes before the substring assert, so
    an ambient FORCE_COLOR/CLICOLOR_FORCE in the operator's shell — which makes Rich/Typer render
    `--help` as color-split spans — can't false-RED this plain-substring check."""
    from beadhive.cli import app

    res = CliRunner().invoke(app, ["hive", "sync-remote", "--help"])

    assert res.exit_code == 0
    assert "--verbose" in _strip_ansi(res.output)


# ---------------------------------------------------------------------------
# CLI wiring: `bh hive sync-remote --all [--dry-run]`
# ---------------------------------------------------------------------------


def test_cli_requires_all_flag(world):
    from beadhive.cli import app

    _make_clean_clone()
    _register()

    res = CliRunner().invoke(app, ["hive", "sync-remote"])

    assert res.exit_code != 0
    assert "--all" in res.output


def test_cli_exits_zero_when_everything_clean(world):
    from beadhive.cli import app

    _make_clean_clone()
    _register()

    res = CliRunner().invoke(app, ["hive", "sync-remote", "--all"])

    assert res.exit_code == 0


def test_cli_all_sync_skips_remote_only_hive(world):
    from beadhive.cli import app

    _register(repo="ghost", prefix="ghost")

    res = CliRunner().invoke(app, ["hive", "sync", "--all", "--dry-run"])

    assert res.exit_code == 0
    assert "remote-only" in res.output


def test_cli_named_sync_keeps_missing_checkout_as_failure(world):
    from beadhive.cli import app

    _register(repo="ghost", prefix="ghost")

    res = CliRunner().invoke(app, ["hive", "sync", "remotes", "ghost", "--dry-run"])

    assert res.exit_code != 0
    assert "clone path does not exist" in res.output


def test_cli_exits_nonzero_and_lists_offenders_when_dirty(world):
    from beadhive.cli import app

    clone, _remote = _make_clean_clone()
    _register()
    (clone / "file.txt").write_text("uncommitted change")

    res = CliRunner().invoke(app, ["hive", "sync-remote", "--all"])

    assert res.exit_code != 0
    assert "github/myorg/myrepo" in res.output


def test_cli_dry_run_exits_zero_and_mutates_nothing(world):
    from beadhive.cli import app

    clone, remote = _make_ahead_clone()
    _register()

    res = CliRunner().invoke(app, ["hive", "sync-remote", "--all", "--dry-run"])

    assert res.exit_code == 0
    remote_log = _git("log", "--all", "--format=%s", cwd=remote).stdout
    assert "unpushed" not in remote_log


# ---------------------------------------------------------------------------
# CLI wiring: `bh hive sync` — the unified group (bh-ummb9.1: remotes/peers subcommands,
# bare = remotes). `sync-remote` deprecates to an alias for `sync remotes --push`.
# ---------------------------------------------------------------------------


def test_bare_sync_is_equivalent_to_sync_remotes(world):
    """`bh hive sync --all` (no subcommand) must be `sync remotes --all` — same targets,
    same outcome — per bh-ummb9.1's acceptance."""
    from beadhive.cli import app

    _make_clean_clone()
    _register()

    bare = CliRunner().invoke(app, ["hive", "sync", "--all"])
    explicit = CliRunner().invoke(app, ["hive", "sync", "remotes", "--all"])

    assert bare.exit_code == 0 == explicit.exit_code
    assert "github/myorg/myrepo" in _strip_ansi(bare.output)
    assert "github/myorg/myrepo" in _strip_ansi(explicit.output)


def test_sync_remotes_still_refuses_a_dirty_tree(world):
    """The guard `sync-remote` earned survives under the new `remotes` name: dirty is still
    refused and offending, exit non-zero."""
    from beadhive.cli import app

    clone, _remote = _make_clean_clone()
    _register()
    (clone / "file.txt").write_text("uncommitted change")

    res = CliRunner().invoke(app, ["hive", "sync", "remotes", "--all"])

    assert res.exit_code != 0
    assert "github/myorg/myrepo" in res.output


def test_sync_remotes_dry_run_still_mutates_nothing(world):
    from beadhive.cli import app

    clone, remote = _make_ahead_clone()
    _register()

    res = CliRunner().invoke(app, ["hive", "sync", "remotes", "--all", "--dry-run"])

    assert res.exit_code == 0
    remote_log = _git("log", "--all", "--format=%s", cwd=remote).stdout
    assert "unpushed" not in remote_log


def test_dry_run_names_the_pull_leg_when_eligible(world, monkeypatch, capsys):
    """Review fix (bh-ummb9.1): --dry-run must NAME the pull leg it will actually run under a
    live invocation — a pull is not inert (it can auto-merge, LWW-resolve). Default (both
    pull+push) says "would pull, then push"."""
    clone, _remote = _make_clean_clone()
    _register()
    (clone / ".beads").mkdir()
    _stub_engine(monkeypatch, _FED_TIMEOUT)  # dolt_status "unknown" — pull-eligible

    sync_remote.sync_remote(dry_run=True)  # default: pull=False, push=True (unchanged default)

    out = capsys.readouterr().out
    assert "would pull" not in out  # pull=False by default — nothing changes here

    sync_remote.sync_remote(dry_run=True, pull=True, push=True)
    out = capsys.readouterr().out
    assert "would pull, then push: refs/dolt/data" in out

    sync_remote.sync_remote(dry_run=True, pull=True, push=False)
    out = capsys.readouterr().out
    assert "would pull: refs/dolt/data" in out
    assert "would pull, then push" not in out


def test_dry_run_never_names_a_pull_for_a_plain_git_clone(world, monkeypatch, capsys):
    """A hive with no dolt state at all (`dolt_status` "absent") is never pulled by the live
    leg — the dry-run preview must not claim it would be, even with `pull=True`."""
    _make_clean_clone()
    _register()

    sync_remote.sync_remote(dry_run=True, pull=True, push=True)

    out = capsys.readouterr().out
    assert "would pull" not in out


def test_sync_remotes_requires_hive_or_all(world):
    from beadhive.cli import app

    res = CliRunner().invoke(app, ["hive", "sync", "remotes"])

    assert res.exit_code != 0
    assert "HIVE" in res.output or "--all" in res.output


def test_sync_remotes_pull_and_push_are_mutually_exclusive(world):
    from beadhive.cli import app

    _make_clean_clone()
    _register()

    res = CliRunner().invoke(app, ["hive", "sync", "remotes", "--all", "--pull", "--push"])

    assert res.exit_code != 0
    assert "mutually exclusive" in res.output


def test_sync_remotes_push_only_skips_pull(world, monkeypatch):
    """`--push` alone must not call `Engine.pull_state` — pull is a distinct, opt-in leg."""
    from beadhive import sync_remote
    from beadhive.cli import app

    _make_clean_clone()
    _register()

    class _NoPull:
        def push_state(self, cwd, actor="", message="", *, remote="", force=False):
            return subprocess.CompletedProcess(args=[], returncode=0)

        def pull_state(self, cwd, *, remote=""):
            raise AssertionError("pull_state must not be called with --push only")

    monkeypatch.setattr(sync_remote.engine, "get_engine", lambda cfg=None: _NoPull())

    res = CliRunner().invoke(app, ["hive", "sync", "remotes", "--all", "--push"])

    assert res.exit_code == 0


def test_sync_peers_has_no_pull_or_push_option(world):
    """bd exposes no single-direction peer verb — `--pull`/`--push` must not exist on
    `sync peers`, not merely error at runtime."""
    from beadhive.cli import app

    res = CliRunner().invoke(app, ["hive", "sync", "peers", "--help"])
    out = _strip_ansi(res.output)

    assert res.exit_code == 0
    # An actual option renders as its own bulleted line in the Options panel; check that
    # shape rather than a bare substring, since the help prose itself names `--pull`/`--push`
    # to explain their absence.
    assert not re.search(r"│\s*--pull\b", out)
    assert not re.search(r"│\s*--push\b", out)


def test_sync_peers_rejects_pull_as_an_unknown_option(world):
    from beadhive.cli import app

    res = CliRunner().invoke(app, ["hive", "sync", "peers", "--all", "--pull"])

    assert res.exit_code != 0
    assert "No such option" in res.output


def test_sync_remotes_accepts_remote_and_force_flags(world, monkeypatch):
    """`--remote`/`--force` must be accepted (parsed) and threaded through to the push call —
    covered here via the engine seam rather than a real multi-remote dolt setup."""
    from beadhive import sync_remote
    from beadhive.cli import app

    clone, _remote = _make_clean_clone()
    _register()
    _git("update-ref", "refs/dolt/data", "HEAD", cwd=clone)  # local-only, no-remote → unpushed

    calls = []

    class _Recording:
        def push_state(self, cwd, actor="", message="", *, remote="", force=False):
            calls.append((remote, force))
            return subprocess.CompletedProcess(args=[], returncode=0)

        def pull_state(self, cwd, *, remote=""):
            return subprocess.CompletedProcess(args=[], returncode=0)

    monkeypatch.setattr(sync_remote.engine, "get_engine", lambda cfg=None: _Recording())

    res = CliRunner().invoke(
        app, ["hive", "sync", "remotes", "--all", "--push", "--remote", "upstream", "--force"]
    )

    assert res.exit_code == 0
    assert calls == [("upstream", True)]


def test_sync_remotes_pull_targets_a_named_remote(world, monkeypatch):
    """`--remote NAME` must reach the PULL leg too, not just push — this fleet has one remote
    per hive today (measured, per bh-ummb9's design section), so a hive with several
    configured dolt remotes can't be verified live. This exercises the routing through the
    `Engine.pull_state` seam instead: real coverage of "the flag reaches the right call",
    honest about not being a live multi-remote fixture."""
    from beadhive import sync_remote
    from beadhive.cli import app

    clone, _remote = _make_clean_clone()
    _register()
    (clone / ".beads").mkdir()
    _mark_managed_dolt_remote(monkeypatch)

    calls = []

    class _Recording:
        def federation_status(self, cwd, *, timeout=None):
            return _FED_TIMEOUT  # dolt_status "unknown" — pull-eligible

        def push_state(self, cwd, actor="", message="", *, remote="", force=False):
            return subprocess.CompletedProcess(args=[], returncode=0)

        def pull_state(self, cwd, *, remote=""):
            calls.append(remote)
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sync_remote.engine, "get_engine", lambda cfg=None: _Recording())

    res = CliRunner().invoke(
        app, ["hive", "sync", "remotes", "--all", "--pull", "--remote", "upstream"]
    )

    assert res.exit_code == 0
    assert calls == ["upstream"]


def test_pull_surfaces_an_auto_merge_notice(world, monkeypatch, capsys):
    """A pull is not inert — measured on the fleet (bh-ummb9.2's own brief):
    `beadhive/baml-harness` auto-merged on pull and said so via a `Notice:` line. That must be
    REPORTED per hive, not silently absorbed into a bare 'pulled'."""
    from beadhive import sync_remote

    clone, _remote = _make_clean_clone()
    _register()
    (clone / ".beads").mkdir()
    _mark_managed_dolt_remote(monkeypatch)

    notice = (
        "Notice: auto-merged issue bh-xyz; updated_at settled last-write-wins (the older "
        "side's edit was superseded)"
    )

    class _AutoMerging:
        def federation_status(self, cwd, *, timeout=None):
            return _FED_TIMEOUT  # dolt_status "unknown" — pull-eligible

        def push_state(self, cwd, actor="", message="", *, remote="", force=False):
            return subprocess.CompletedProcess(args=[], returncode=0)

        def pull_state(self, cwd, *, remote=""):
            return subprocess.CompletedProcess(args=[], returncode=0, stdout=notice, stderr="")

    monkeypatch.setattr(sync_remote.engine, "get_engine", lambda cfg=None: _AutoMerging())

    plan = sync_remote.sync_remote(pull=True, push=True)
    out = capsys.readouterr().out

    assert "auto-merged issue bh-xyz" in out
    assert plan.auto_merges  # keyed by hive_id, non-empty
    assert notice in next(iter(plan.auto_merges.values()))


def test_pull_success_with_no_auto_merge_reports_nothing_extra(world, monkeypatch, capsys):
    """The common case (a plain pull, no LWW resolution) must stay quiet — no
    `plan.auto_merges` entry, no extra output line."""
    from beadhive import sync_remote

    clone, _remote = _make_clean_clone()
    _register()
    (clone / ".beads").mkdir()
    _mark_managed_dolt_remote(monkeypatch)

    class _PlainPull:
        def federation_status(self, cwd, *, timeout=None):
            return _FED_TIMEOUT  # dolt_status "unknown" — pull-eligible

        def push_state(self, cwd, actor="", message="", *, remote="", force=False):
            return subprocess.CompletedProcess(args=[], returncode=0)

        def pull_state(self, cwd, *, remote=""):
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sync_remote.engine, "get_engine", lambda cfg=None: _PlainPull())

    plan = sync_remote.sync_remote(pull=True, push=True)
    out = capsys.readouterr().out

    assert "auto-merged" not in out.lower()
    assert plan.auto_merges == {}


def test_sync_remote_deprecation_warns_once_and_still_works(world):
    from beadhive.cli import app

    _make_clean_clone()
    _register()

    res = CliRunner().invoke(app, ["hive", "sync-remote", "--all"])

    assert res.exit_code == 0
    assert res.output.count("deprecated") == 1
    assert "sync remotes --push" in _strip_ansi(res.output)


def test_sync_remote_deprecation_still_refuses_missing_all(world):
    from beadhive.cli import app

    res = CliRunner().invoke(app, ["hive", "sync-remote"])

    assert res.exit_code != 0
    assert "--all" in res.output


def test_sync_remote_deprecated_alias_is_push_only(world, monkeypatch):
    """`sync-remote` deprecates to `sync remotes --push` — never pulls."""
    from beadhive import sync_remote
    from beadhive.cli import app

    _make_clean_clone()
    _register()

    class _NoPull:
        def push_state(self, cwd, actor="", message="", *, remote="", force=False):
            return subprocess.CompletedProcess(args=[], returncode=0)

        def pull_state(self, cwd, *, remote=""):
            raise AssertionError("sync-remote must never pull — push-only alias")

    monkeypatch.setattr(sync_remote.engine, "get_engine", lambda cfg=None: _NoPull())

    res = CliRunner().invoke(app, ["hive", "sync-remote", "--all"])

    assert res.exit_code == 0


# ---------------------------------------------------------------------------
# clean_checkout: color-neutral validation env (bh-76gx regression)
# ---------------------------------------------------------------------------


def _ensure_checkout_hive(tmp_path, monkeypatch):
    """Minimal hive scaffold for a `worktree.clean_checkout()` call: a real git clone under
    `GIT_WORKSPACE` plus an isolated `BH_WORKTREES` root. Deliberately self-contained (not
    imported from test_worktree.py's own `_ensure_hive`) — this file owns its fixtures."""
    ws_root = tmp_path / "ws"
    repo = ws_root / "github" / "myorg" / "myrepo"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("hi")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)

    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("BH_WORKTREES", str(tmp_path / "wts"))
    # Isolate HOME so ws's git ops (which scrub GIT_CONFIG_GLOBAL) use default git config.
    (tmp_path / "home").mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    return {"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}


def test_clean_checkout_validation_env_is_color_neutral(tmp_path, monkeypatch):
    """Regression for bh-76gx: a clean_checkout validation child runs with color-forcing env
    scrubbed even when the OPERATOR's ambient shell has FORCE_COLOR/CLICOLOR_FORCE set — the
    false-RED root cause (Rich/Typer honoring an inherited FORCE_COLOR, splitting `--help` output
    into ANSI spans and breaking a plain-substring assert in a supposedly-hermetic validation
    run). Before the fix, `env` handed to the validation spawn still carried FORCE_COLOR/
    CLICOLOR_FORCE through unscrubbed."""
    from beadhive import host, worktree

    host.mint_if_needed()  # the verify-dir liveness marker keys on host.host_id() (bh-ytbb.4)
    entry = _ensure_checkout_hive(tmp_path, monkeypatch)
    monkeypatch.setenv("FORCE_COLOR", "3")
    monkeypatch.setenv("CLICOLOR_FORCE", "1")

    calls = []

    class _Done:
        returncode = 0

    def _fake_run(cmd, **kw):
        calls.append((list(cmd), kw))
        return _Done()

    # Fake the subprocess seam so the git worktree add/remove no-op (rc 0) and we can inspect the
    # env handed to the validation spawn without running a real command.
    monkeypatch.setattr(worktree, "run", _fake_run)

    rc = worktree.clean_checkout(entry, "main", "just check")
    assert rc == 0

    # The validation spawn is the only non-git run() call (others are `git worktree add/remove`).
    val = [(cmd, kw) for cmd, kw in calls if cmd[:1] != ["git"]]
    assert len(val) == 1
    _cmd, kw = val[0]
    env = kw["env"]
    assert "FORCE_COLOR" not in env
    assert "CLICOLOR_FORCE" not in env
    assert env["NO_COLOR"] == "1"


def test_clean_checkout_names_a_missing_validation_binary(tmp_path, monkeypatch, capsys):
    """bh-7m2h9, N4. This seam runs WITHOUT capture, so a missing `just`/`uv` exited 127 having
    printed NOTHING — no stdout, no stderr — and then _BARE_CHECKOUT_HINT pointed the operator at
    the checkout, which is not the problem. Silence plus a misdirection is a worse operator
    experience than the FileNotFoundError it replaced, and the tag needed to tell "the binary is
    absent" from "the tests failed" was already on the result, just unread."""
    from beadhive import host, worktree

    host.mint_if_needed()
    entry = _ensure_checkout_hive(tmp_path, monkeypatch)

    def _fake_run(cmd, **kw):
        if list(cmd)[:1] == ["git"]:
            return subprocess.CompletedProcess(cmd, 0, None, None)
        res = subprocess.CompletedProcess(cmd, 127, None, None)  # capture-less: no output at all
        res.bh_missing_binary = "just"
        return res

    monkeypatch.setattr(worktree, "run", _fake_run)

    rc = worktree.clean_checkout(entry, "main", "just check")

    assert rc == 127
    err = capsys.readouterr().err
    assert "`just` is not on PATH" in err
    assert "not a test failure" in err
    assert "bare" not in err.lower(), "the misleading bare-checkout hint was printed anyway"
