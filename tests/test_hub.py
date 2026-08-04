"""Tests for ws.hub sync/ensure_hub error handling.

The bug: `bd repo add` / `bd repo sync` ran with check=False and uncaptured output, so
re-running `ws sync` dumped bd's full 'already configured' error + usage block per hive,
while genuine failures were swallowed into a green summary. These tests pin the fixed
contract: idempotent re-adds are silent, genuine failures are surfaced (and returned),
and a missing/broken bd yields a friendly error instead of a raw traceback.
"""

from __future__ import annotations

from collections import namedtuple

import pytest
import typer

from beadhive import bd, hub

Completed = namedtuple("Completed", "returncode stdout stderr")

_USAGE_DUMP = (
    "Error: failed to add repository: repository already configured: {src}\n"
    "Usage:\n  bd repo add <path> [flags]\n\nFlags:\n  -h, --help   help for add\n"
)


def _hive_cfg(*repos):
    return {
        "managed_repos": [
            {"provider": "github", "org": "a", "repo": r, "prefix": f"a-{r}"} for r in repos
        ]
    }


def _wire(tmp_path, monkeypatch, fake_run, *repos):
    """Point hub.sync at fake subprocesses + on-disk hive dirs for the given repo names.

    `hub.run` fakes the hub-store-only ops (repo add/remove/sync/list) hub.py still calls
    directly; `bd._run` fakes the per-hive export/bootstrap ops, which route through the
    `Engine` seam (bh-dw3e.5) and land on the SAME `bd._run` bd.py's own callers hit — same
    fake, same `cmd` shape, just a different interception point."""
    dirs = {}
    for r in repos:
        d = tmp_path / r
        (d / ".beads").mkdir(parents=True)
        dirs[r] = d
    monkeypatch.setenv("WS_HOME", str(tmp_path))  # keep metadata.invalidate off the real cache
    monkeypatch.setattr(hub, "run", fake_run)
    monkeypatch.setattr(bd, "_run", fake_run)
    monkeypatch.setattr(hub, "ensure_hub", lambda: tmp_path / "hub")
    monkeypatch.setattr(hub.config, "load", lambda: _hive_cfg(*repos))
    monkeypatch.setattr(hub.registry, "hive_dir", lambda e: dirs[e["repo"]])
    return dirs


def test_sync_already_configured_readd_is_silent(tmp_path, monkeypatch, capsys):
    """Re-running sync against already-configured hives prints no error/usage noise and
    still counts every hive as hydrated."""

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
    """If the final `bd repo sync` exits non-zero, no added hive is counted hydrated."""

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
    doesn't fail the hive on its own."""

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
    while a still-managed registration is left untouched (and the repo/hive itself is never
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
    """When every registered repo maps to a managed hive, no `bd repo remove` is issued."""
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


def test_query_label_defaults_to_hq_and_forwards_to_guard(tmp_path, monkeypatch, capsys):
    """`hub.query`'s default `label` ("hq") reaches the guard's refusal message unchanged, and
    an explicit `label="hub"` (the deprecated alias's call site) overrides it (bh-ohx2)."""
    monkeypatch.setattr(hub, "run", lambda *a, **k: pytest.fail("bd must not run on a write"))
    monkeypatch.setattr(hub.config, "hub_dir", lambda: tmp_path)

    with pytest.raises(typer.Exit):
        hub.query(["create", "-t", "boom"])
    assert "`bh hq bd create`" in capsys.readouterr().err

    with pytest.raises(typer.Exit):
        hub.query(["create", "-t", "boom"], label="hub")
    assert "`bh hub bd create`" in capsys.readouterr().err


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
    intake across every hydrated hive (source-agnostic — keyed on intake:untriaged), with extra
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
    """A failing `bd init` exits with a legible bh-level message, not a CalledProcessError
    trace. bd's own error streams straight through and is never captured/re-quoted here
    (bh-areg.7's review, round 3 — capturing it read the WRONG line for `--shared-server`'s
    two-phase git-then-dolt-server shape), so a hermetic fake `hub.run` — which only returns
    a value, never actually writing to the terminal the way a real streaming subprocess would
    — has nothing of bd's own to assert on beyond bh's own summary line and the clean exit."""
    # Same WS_HOME isolation — see test_ensure_hub_missing_bd_is_friendly.
    monkeypatch.setenv("WS_HOME", str(tmp_path))
    monkeypatch.setenv("WS_HUB", str(tmp_path / "hub"))
    monkeypatch.setattr(hub, "run", lambda cmd, **k: Completed(1, "", ""))
    with pytest.raises(typer.Exit):
        hub.ensure_hub()
    err = capsys.readouterr().err
    assert "bd init failed" in err
    assert "bd's error is above" in err
    assert "Usage:" not in err


# ---------------------------------------------------------------------------
# ensure_store defaults a FRESH store onto bd's shared server (bh-areg.7)
# ---------------------------------------------------------------------------


def test_ensure_store_passes_shared_server_flag_on_a_fresh_store(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_HOME", str(tmp_path))
    monkeypatch.setenv("WS_HUB", str(tmp_path / "hub"))
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        return Completed(0, "", "")

    monkeypatch.setattr(hub, "run", fake_run)
    from beadhive import store_locator

    monkeypatch.setattr(store_locator, "ensure_server_mode_persisted", lambda store: False)

    hub.ensure_hub()

    init_call = calls[0]
    assert init_call[:2] == ["bd", "init"]
    assert "--shared-server" in init_call
    config_call = ["bd", "-C", str(tmp_path / "hub"), "config", "set", "dolt.shared-server", "true"]
    assert config_call in calls


def test_ensure_store_warns_visibly_when_dolt_mode_needed_fixing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WS_HOME", str(tmp_path))
    monkeypatch.setenv("WS_HUB", str(tmp_path / "hub"))
    monkeypatch.setattr(hub, "run", lambda cmd, **k: Completed(0, "", ""))
    from beadhive import store_locator

    monkeypatch.setattr(store_locator, "ensure_server_mode_persisted", lambda store: True)

    hub.ensure_hub()

    err = capsys.readouterr().err
    assert "dolt_mode" in err
    assert "⚠" in err


def test_ensure_store_leaves_an_existing_store_untouched(tmp_path, monkeypatch):
    """The `.beads`-exists guard: a pre-existing store is never re-inited, so a store that
    predates this bead (or was migrated by hand) is never touched by the shared-server
    default — same "existing hives untouched" discipline as onboarding's own skip path."""
    monkeypatch.setenv("WS_HOME", str(tmp_path))
    store = tmp_path / "hub"
    (store / ".beads").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(hub, "run", lambda cmd, **k: calls.append(cmd) or Completed(0, "", ""))
    monkeypatch.setenv("WS_HUB", str(store))

    hub.ensure_store(store, "hub")

    assert calls == []


def test_bd_ni_env_reads_os_environ_fresh_on_every_call(monkeypatch):
    """`_bd_ni_env()` must never behave like the module-level snapshot it replaced
    (bh-areg.7's review, round 3): a constant frozen at import time could not see a
    later-set env override (e.g. a test's own `BEADS_SHARED_SERVER_DIR` isolation fixture),
    silently falling through to whatever was ambient at first import — a real path back into
    the operator's production shared server."""
    monkeypatch.delenv("BH_AREG7_PROBE", raising=False)
    assert "BH_AREG7_PROBE" not in hub._bd_ni_env()

    monkeypatch.setenv("BH_AREG7_PROBE", "1")
    env = hub._bd_ni_env()
    assert env["BH_AREG7_PROBE"] == "1"
    assert env["BD_NON_INTERACTIVE"] == "1"


def test_sync_emits_banner_and_per_hive_progress(tmp_path, monkeypatch, capsys):
    """sync() emits a 'starting hub sync' banner before the import loop and a per-hive
    progress line for each hive, both on stderr to match the existing err=True convention."""

    def fake_run(cmd, **k):
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one", "two")
    hub.sync()
    err = capsys.readouterr().err
    assert "starting hub sync (2 hive(s))" in err
    assert "• syncing a-one (1/2)" in err
    assert "• syncing a-two (2/2)" in err
