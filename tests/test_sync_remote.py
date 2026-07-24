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
    """Patch `engine.get_engine` (the module object shared by sync_remote AND safety's lazy
    fetch-path import) with a stub whose `federation_status` returns *fs*. `push_state`
    records into *push_calls* when given, and hard-fails when not (read-only expectation)."""

    class _StubEngine:
        def federation_status(self, cwd, *, timeout=None):
            return fs

        def push_state(self, cwd, actor="", message=""):
            if push_calls is None:
                raise AssertionError("push_state must not be called in this test")
            push_calls.append(str(cwd))
            return subprocess.CompletedProcess(args=[], returncode=0)

    monkeypatch.setattr(sync_remote.engine, "get_engine", lambda cfg=None: _StubEngine())


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
    monkeypatch.setattr("beadhive.safety._bd_dolt_mode", lambda path: "embedded")
    monkeypatch.setattr("beadhive.safety._bd_has_dolt_remote", lambda path: True)

    record = assess_hive("github/o/r", repo)

    assert record.status == SyncStatus.UNPUSHED_DOLT
    assert record.dolt_status == "unknown"
    assert any("embedded engine" in r for r in record.reasons)


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
        FederationStatus(
            ok=True, peers=(FederationPeer(peer="origin", reachable=True, ahead=4),)
        ),
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
    monkeypatch.setattr("beadhive.safety._bd_dolt_mode", lambda path: "embedded")
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


def test_sync_remote_assesses_with_fetch_and_surfaces_ahead_count(world, monkeypatch, capsys):
    """The fleet pass pre-assesses with fetch=True: a reachable federation peer's verified
    ahead count surfaces in the per-hive reason line instead of 'could not be verified'."""
    clone, _remote = _make_clean_clone()
    _register()
    (clone / ".beads").mkdir()
    _stub_engine(
        monkeypatch,
        FederationStatus(
            ok=True, peers=(FederationPeer(peer="origin", reachable=True, ahead=4),)
        ),
    )

    plan = sync_remote.sync_remote(dry_run=True)

    assert plan.records[0].status == SyncStatus.UNPUSHED_DOLT
    assert plan.records[0].dolt_status == "ahead"
    out = capsys.readouterr().out
    assert "4 ahead" in out
    assert "would push dolt: refs/dolt/data" in out


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


def test_missing_clone_is_blocked_and_offending(world):
    _register(repo="ghost", prefix="ghost")

    plan = sync_remote.sync_remote(dry_run=False)

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
        def push_state(self, cwd, actor="", message=""):
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
        def push_state(self, cwd, actor="", message=""):
            return subprocess.CompletedProcess(args=[], returncode=1)

    monkeypatch.setattr(sync_remote.engine, "get_engine", lambda cfg: _FailingEngine())

    plan = sync_remote.sync_remote(dry_run=False)

    assert plan.offending == ["github/myorg/myrepo"]
    assert plan.dolt_pushed == []


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
    """Make a clean clone look like the genuinely-unverifiable unpushed-dolt case (the
    successor of bh-fl26's embedded-engine blanket 'unknown'): bd-managed (`.beads/`) but
    federation status times out — dolt_status == 'unknown', status == UNPUSHED_DOLT."""
    (clone / ".beads").mkdir(exist_ok=True)
    _stub_engine(monkeypatch, _FED_TIMEOUT)


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
    from beadhive import worktree

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
