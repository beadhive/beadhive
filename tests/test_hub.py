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

from ws import hub

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


def test_ensure_hub_missing_bd_is_friendly(tmp_path, monkeypatch, capsys):
    """A missing bd binary exits with a friendly message, not a raw FileNotFoundError."""
    monkeypatch.setenv("WS_HUB", str(tmp_path / "hub"))

    def raise_fnf(cmd, **k):
        raise FileNotFoundError("bd")

    monkeypatch.setattr(hub, "run", raise_fnf)
    with pytest.raises(typer.Exit):
        hub.ensure_hub()
    assert "`bd` not found" in capsys.readouterr().err


def test_ensure_hub_init_failure_is_friendly(tmp_path, monkeypatch, capsys):
    """A failing `bd init` exits with the headline error, not a CalledProcessError trace."""
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
