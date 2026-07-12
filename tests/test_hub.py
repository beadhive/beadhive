"""Tests for ws.hub sync/ensure_hub error handling.

The bug: `bd repo add` / `bd repo sync` ran with check=False and uncaptured output, so
re-running `ws sync` dumped bd's full 'already configured' error + usage block per rig,
while genuine failures were swallowed into a green summary. These tests pin the fixed
contract: idempotent re-adds are silent, genuine failures are surfaced (and returned),
and a missing/broken bd yields a friendly error instead of a raw traceback.
"""

from __future__ import annotations

from collections import namedtuple

import pytest
import typer

from beadhive import hub

Completed = namedtuple("Completed", "returncode stdout stderr")

_USAGE_DUMP = (
    "Error: failed to add repository: repository already configured: {src}\n"
    "Usage:\n  bd repo add <path> [flags]\n\nFlags:\n  -h, --help   help for add\n"
)


def _rig_cfg(*repos):
    return {
        "managed_repos": [
            {"provider": "github", "org": "a", "repo": r, "prefix": f"a-{r}"} for r in repos
        ]
    }


def _wire(tmp_path, monkeypatch, fake_run, *repos):
    """Point hub.sync at fake subprocesses + on-disk rig dirs for the given repo names."""
    dirs = {}
    for r in repos:
        d = tmp_path / r
        (d / ".beads").mkdir(parents=True)
        dirs[r] = d
    monkeypatch.setenv("WS_HOME", str(tmp_path))  # keep metadata.invalidate off the real cache
    monkeypatch.setattr(hub, "run", fake_run)
    monkeypatch.setattr(hub, "ensure_hub", lambda: tmp_path / "hub")
    monkeypatch.setattr(hub.config, "load", lambda: _rig_cfg(*repos))
    monkeypatch.setattr(hub.registry, "rig_dir", lambda e: dirs[e["repo"]])
    return dirs


def test_sync_already_configured_readd_is_silent(tmp_path, monkeypatch, capsys):
    """Re-running sync against already-configured rigs prints no error/usage noise and
    still counts every rig as hydrated."""

    def fake_run(cmd, **k):
        if cmd[3:5] == ["repo", "add"]:
            return Completed(1, "", _USAGE_DUMP.format(src=cmd[-1]))
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one", "two")
    failed = hub.sync()
    out = capsys.readouterr()
    assert failed == []
    assert "Usage:" not in out.out + out.err
    assert "already configured" not in out.out + out.err
    assert "2 hydrated, 0 skipped" in out.out
    assert out.out.startswith("✓")


def test_sync_genuine_add_failure_surfaces(tmp_path, monkeypatch, capsys):
    """A repo add failure that is NOT 'already configured' is reported (headline only,
    no usage dump), excluded from the hydrated count, and returned by sync()."""

    def fake_run(cmd, **k):
        if cmd[3:5] == ["repo", "add"] and cmd[-1].endswith("bad"):
            err = "Error: failed to add repository: database locked\nUsage:\n  bd repo add\n"
            return Completed(1, "", err)
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "good", "bad")
    failed = hub.sync()
    out = capsys.readouterr()
    assert failed == ["a-bad"]
    assert "a-bad: bd repo add failed: Error: failed to add repository: database locked" in out.err
    assert "Usage:" not in out.err
    assert "1 hydrated" in out.out
    assert "1 failed to hydrate (a-bad)" in out.out


def test_sync_repo_sync_failure_marks_all_added_failed(tmp_path, monkeypatch, capsys):
    """If the final `bd repo sync` exits non-zero, no added rig is counted hydrated."""

    def fake_run(cmd, **k):
        if cmd[3:5] == ["repo", "sync"]:
            return Completed(1, "", "Error: sync exploded\n")
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one", "two")
    failed = hub.sync()
    out = capsys.readouterr()
    assert sorted(failed) == ["a-one", "a-two"]
    assert "bd repo sync failed: Error: sync exploded" in out.err
    assert "0 hydrated" in out.out
    assert "2 failed to hydrate" in out.out


def test_sync_export_failure_warns_but_continues(tmp_path, monkeypatch, capsys):
    """A failed `bd export` warns (repo sync may still hydrate from existing JSONL) but
    doesn't fail the rig on its own."""

    def fake_run(cmd, **k):
        if len(cmd) > 3 and cmd[3] == "export":
            return Completed(1, "", "Error: export failed\n")
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one")
    failed = hub.sync()
    out = capsys.readouterr()
    assert failed == []
    assert "a-one: bd export failed: Error: export failed" in out.err
    assert "1 hydrated" in out.out


def test_sync_reconciles_stale_hub_registration(tmp_path, monkeypatch, capsys):
    """A repo registered in the hub but no longer managed is dropped via `bd repo remove`,
    while a still-managed registration is left untouched (and the repo/rig itself is never
    touched — only the hub entry)."""
    removed: list[str] = []

    def fake_run(cmd, **k):
        if cmd[3:5] == ["repo", "list"]:
            managed_path = str(tmp_path / "one")
            listing = (
                "Primary repository: .\n\nAdditional repositories:\n"
                f"  - {managed_path}\n"
                "  - /Users/brian/workspace/github/briancripe/story-swarm\n"
            )
            return Completed(0, listing, "")
        if cmd[3:5] == ["repo", "remove"]:
            removed.append(cmd[-1])
            return Completed(0, "", "")
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one")
    hub.sync()
    out = capsys.readouterr()
    stale = "/Users/brian/workspace/github/briancripe/story-swarm"
    assert removed == [stale]
    assert str(tmp_path / "one") not in removed
    assert f"dropped stale hub entry: {stale}" in out.err


def test_sync_reconcile_no_op_when_all_registrations_managed(tmp_path, monkeypatch, capsys):
    """When every registered repo maps to a managed rig, no `bd repo remove` is issued."""
    removed: list[str] = []

    def fake_run(cmd, **k):
        if cmd[3:5] == ["repo", "list"]:
            listing = (
                "Primary repository: .\n\nAdditional repositories:\n"
                f"  - {tmp_path / 'one'}\n  - {tmp_path / 'two'}\n"
            )
            return Completed(0, listing, "")
        if cmd[3:5] == ["repo", "remove"]:
            removed.append(cmd[-1])
            return Completed(0, "", "")
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one", "two")
    hub.sync()
    assert removed == []


def test_query_refuses_hub_write_before_running_bd(tmp_path, monkeypatch, capsys):
    """`ws hub bd create` is refused by the guard — bd is never invoked (no stranded bead)."""
    monkeypatch.setattr(hub, "run", lambda *a, **k: pytest.fail("bd must not run on a hub write"))
    monkeypatch.setattr(hub.config, "hub_dir", lambda: tmp_path)
    with pytest.raises(typer.Exit) as exc:
        hub.query(["create", "-t", "stranded"])
    assert exc.value.exit_code == 1
    assert "READ-ONLY" in capsys.readouterr().err


def test_query_read_verb_forwards_to_bd(tmp_path, monkeypatch):
    """A read verb passes the guard and forwards to bd against the hub."""
    (tmp_path / ".beads").mkdir()
    calls = []

    class _Ok:
        returncode = 0

    monkeypatch.setattr(hub.config, "hub_dir", lambda: tmp_path)
    monkeypatch.setattr(hub, "run", lambda cmd, **k: calls.append(cmd) or _Ok())
    hub.query(["ready"])
    assert calls and calls[0][-1] == "ready"


def test_intake_filters_fleet_wide_untriaged(tmp_path, monkeypatch):
    """`ws hub intake` is the superintendent's fleet-wide inbox: a filtered read for untriaged
    intake across every hydrated rig (source-agnostic — keyed on intake:untriaged), with extra
    bd flags forwarded through."""
    from beadhive import state

    (tmp_path / ".beads").mkdir()
    calls = []

    class _Ok:
        returncode = 0

    monkeypatch.setattr(hub.config, "hub_dir", lambda: tmp_path)
    monkeypatch.setattr(hub, "run", lambda cmd, **k: calls.append(cmd) or _Ok())

    hub.intake(["--json"])

    argv = calls[0]
    assert argv[3:] == ["list", "--label", state.INTAKE_UNTRIAGED, "--status", "open", "--json"]


def test_ensure_hub_missing_bd_is_friendly(tmp_path, monkeypatch, capsys):
    """A missing bd binary exits with a friendly message, not a raw FileNotFoundError."""
    # WS_HOME must point at an empty dir so config.load() raises FileNotFoundError and
    # _aggregation_target() falls back to hub_dir() (which honours WS_HUB).
    monkeypatch.setenv("WS_HOME", str(tmp_path))
    monkeypatch.setenv("WS_HUB", str(tmp_path / "hub"))

    def raise_fnf(cmd, **k):
        raise FileNotFoundError("bd")

    monkeypatch.setattr(hub, "run", raise_fnf)
    with pytest.raises(typer.Exit):
        hub.ensure_hub()
    assert "`bd` not found" in capsys.readouterr().err


def test_ensure_hub_init_failure_is_friendly(tmp_path, monkeypatch, capsys):
    """A failing `bd init` exits with the headline error, not a CalledProcessError trace."""
    # Same WS_HOME isolation — see test_ensure_hub_missing_bd_is_friendly.
    monkeypatch.setenv("WS_HOME", str(tmp_path))
    monkeypatch.setenv("WS_HUB", str(tmp_path / "hub"))
    monkeypatch.setattr(
        hub, "run", lambda cmd, **k: Completed(1, "", "Error: init broke\nUsage:\n")
    )
    with pytest.raises(typer.Exit):
        hub.ensure_hub()
    err = capsys.readouterr().err
    assert "bd init failed" in err
    assert "Error: init broke" in err
    assert "Usage:" not in err


def test_sync_emits_banner_and_per_rig_progress(tmp_path, monkeypatch, capsys):
    """sync() emits a 'starting hub sync' banner before the import loop and a per-rig
    progress line for each rig, both on stderr to match the existing err=True convention."""

    def fake_run(cmd, **k):
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one", "two")
    hub.sync()
    err = capsys.readouterr().err
    assert "starting hub sync (2 rig(s))" in err
    assert "• syncing a-one (1/2)" in err
    assert "• syncing a-two (2/2)" in err
