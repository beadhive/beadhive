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


def test_dry_run_on_embedded_dolt_engine_prints_attempt_not_ahead_count(world, capsys, monkeypatch):
    """The embedded engine (bh-fl26) has no read-only ahead/behind primitive — dry-run must
    report an honest 'would attempt' plan, not a fabricated push line, and must call nothing."""
    clone, _remote = _make_clean_clone()
    _register()
    (clone / ".beads").mkdir()
    monkeypatch.setattr("beadhive.safety._bd_dolt_mode", lambda path: "embedded")
    monkeypatch.setattr("beadhive.safety._bd_has_dolt_remote", lambda path: True)

    def _boom(cfg):
        raise AssertionError("engine must not be constructed/called under --dry-run")

    monkeypatch.setattr(sync_remote.engine, "get_engine", _boom)

    plan = sync_remote.sync_remote(dry_run=True)

    assert plan.records[0].dolt_status == "unknown"
    out = capsys.readouterr().out
    assert "would attempt: bd dolt push" in out
    assert "would push dolt: refs/dolt/data" not in out


def test_live_pushes_embedded_dolt_engine_via_engine(world, monkeypatch):
    """Live mode just calls Engine.push_state (already-existing wiring) for the embedded
    engine's 'unknown' status too, trusting bd dolt push's own idempotent success/failure."""
    clone, _remote = _make_clean_clone()
    _register()
    (clone / ".beads").mkdir()
    monkeypatch.setattr("beadhive.safety._bd_dolt_mode", lambda path: "embedded")
    monkeypatch.setattr("beadhive.safety._bd_has_dolt_remote", lambda path: True)

    calls = []

    class _FakeEngine:
        def push_state(self, cwd, actor="", message=""):
            calls.append(str(cwd))
            return subprocess.CompletedProcess(args=[], returncode=0)

    monkeypatch.setattr(sync_remote.engine, "get_engine", lambda cfg: _FakeEngine())

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


def _mark_embedded_dolt(clone: Path, monkeypatch) -> None:
    """Make a clean clone look like the embedded-Dolt-engine unpushed-dolt case (bh-fl26):
    dolt_status == 'unknown', status == UNPUSHED_DOLT."""
    (clone / ".beads").mkdir(exist_ok=True)
    monkeypatch.setattr("beadhive.safety._bd_dolt_mode", lambda path: "embedded")
    monkeypatch.setattr("beadhive.safety._bd_has_dolt_remote", lambda path: True)


def test_verbose_false_makes_no_extra_query_on_unpushed_dolt_hive(world, monkeypatch, capsys):
    """Default (non-verbose) output is unchanged: no recently-touched block, and the extra
    `bd list` query never runs at all for an unpushed-dolt hive."""
    clone, _remote = _make_clean_clone()
    _register()
    _mark_embedded_dolt(clone, monkeypatch)

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
    _mark_embedded_dolt(clone, monkeypatch)

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
