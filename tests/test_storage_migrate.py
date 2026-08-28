"""`bh hive migrate-storage` (bh-areg.4) — move a hive off bd's embedded Dolt engine onto the
fleet's shared-server mode: per hive, back up (VERIFIED) -> migrate -> verify -> report;
fleet-wide, resumable and per-hive isolated, Factory HQ last.

Unit-level: every seam this module touches (engine.backup/backup_restore, `bd` subprocess
calls, `store_locator`'s filesystem facts) is faked here — no real `bd` binary needed. The REAL
round trip (a genuine embedded -> shared-server migration against a real bd binary, proving
constraint 1's metadata-persistence fix against the actual drift bd's own code exhibits) lives
in ``test_storage_migrate_int.py``, self-skipping without ``bd`` on PATH.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import typer

from beadhive import registry, storage_migrate
from beadhive.run import run as real_run
from harness.world import git_env

# ---- fixtures -----------------------------------------------------------------


def _write_metadata(hive_dir, *, dolt_mode, dolt_database="scremb"):
    (hive_dir / ".beads").mkdir(parents=True, exist_ok=True)
    (hive_dir / ".beads" / "metadata.json").write_text(
        json.dumps({"dolt_mode": dolt_mode, "dolt_database": dolt_database})
    )


def _ok(cmd="bd"):
    return subprocess.CompletedProcess([cmd], 0, "", "")


def _fail(cmd="bd", stderr="boom"):
    return subprocess.CompletedProcess([cmd], 1, "", stderr)


# ---- fleet_order: HQ always last -----------------------------------------------


def test_fleet_order_puts_hq_last():
    cfg = {
        "managed_repos": [
            {
                "provider": "local",
                "org": "factory",
                "repo": "hq",
                "prefix": "hq",
                "kind": registry.HQ_KIND,
            },
            {"provider": "github", "org": "z", "repo": "zeta", "prefix": "zeta", "kind": "p"},
            {"provider": "github", "org": "a", "repo": "alpha", "prefix": "alpha", "kind": "p"},
        ]
    }
    order = storage_migrate.fleet_order(cfg)
    assert [e["repo"] for e in order] == ["alpha", "zeta", "hq"]


def test_fleet_order_with_no_hq_registered_is_fine():
    cfg = {"managed_repos": [{"provider": "github", "org": "a", "repo": "b", "prefix": "b"}]}
    order = storage_migrate.fleet_order(cfg)
    assert [e["repo"] for e in order] == ["b"]


# ---- backup: reuses hq.BackupTarget/BackupPlan's verification discipline -----------------


def test_backup_plan_is_not_ok_when_native_backup_writes_nothing(tmp_path, monkeypatch):
    """bh-kobw's own discipline, reused: `bd backup` exiting 0 is not itself proof of a
    restorable artifact — an empty destination must never be reported verified."""

    class _EmptyEngine:
        def export_jsonl(self, cwd, out_path, *, env=None):
            out_path.write_text("")
            return _ok()

        def backup(self, cwd, dest, *, actor=""):
            dest.mkdir(parents=True, exist_ok=True)  # "succeeds" but writes nothing
            return _ok()

    monkeypatch.setattr(storage_migrate.engine, "get_engine", lambda cfg: _EmptyEngine())
    monkeypatch.setattr(storage_migrate, "_issue_count", lambda hive_dir: 0)

    hive_dir = tmp_path / "hive"
    hive_dir.mkdir()
    plan = storage_migrate.take_backup(hive_dir, tmp_path / "backup", {}, "pfx", dry_run=False)

    assert not plan.ok, [(t.name, t.verified, t.detail) for t in plan.targets]


def test_backup_plan_is_ok_when_both_levels_carry_real_content(tmp_path, monkeypatch):
    class _RealEngine:
        def export_jsonl(self, cwd, out_path, *, env=None):
            out_path.write_text('{"id": "pfx-1"}\n{"id": "pfx-2"}\n')
            return _ok()

        def backup(self, cwd, dest, *, actor=""):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "payload").write_text("real dolt-native bytes")
            return _ok()

    monkeypatch.setattr(storage_migrate.engine, "get_engine", lambda cfg: _RealEngine())
    monkeypatch.setattr(storage_migrate, "_issue_count", lambda hive_dir: 2)

    hive_dir = tmp_path / "hive"
    hive_dir.mkdir()
    plan = storage_migrate.take_backup(hive_dir, tmp_path / "backup", {}, "pfx", dry_run=False)

    assert plan.ok, [(t.name, t.verified, t.detail) for t in plan.targets]
    names = {t.name for t in plan.targets}
    assert names == {"jsonl-export", "dolt-native-backup"}


def test_backup_dry_run_writes_nothing(tmp_path, monkeypatch):
    calls = []

    class _CanaryEngine:
        def export_jsonl(self, cwd, out_path, *, env=None):
            calls.append("export")
            return _ok()

        def backup(self, cwd, dest, *, actor=""):
            calls.append("backup")
            return _ok()

    monkeypatch.setattr(storage_migrate.engine, "get_engine", lambda cfg: _CanaryEngine())
    hive_dir = tmp_path / "hive"
    hive_dir.mkdir()
    backup_dir = tmp_path / "backup"

    plan = storage_migrate.take_backup(hive_dir, backup_dir, {}, "pfx", dry_run=True)

    assert calls == []
    assert not backup_dir.exists()
    assert plan.dry_run


# ---- constraint 1: metadata.json persistence + pre-existing drift detection --------------


def test_fix_metadata_dolt_mode_persists_server_and_preserves_other_keys(tmp_path):
    hive_dir = tmp_path / "hive"
    _write_metadata(hive_dir, dolt_mode="embedded", dolt_database="scremb")

    storage_migrate._fix_metadata_dolt_mode(hive_dir, "server")

    data = json.loads((hive_dir / ".beads" / "metadata.json").read_text())
    assert data["dolt_mode"] == "server"
    assert data["dolt_database"] == "scremb"  # untouched


def test_detect_pre_existing_drift_flags_embedded_metadata_under_active_shared_server(
    tmp_path, monkeypatch
):
    """The exact drift bd's own `warnSharedServerEmbeddedMismatch` only warns about: metadata
    still says embedded while `dolt.shared-server` is already persisted true — the fleet driver
    must surface it as a real finding on an UNMIGRATED hive (this bead's notes, constraint 1)."""
    hive_dir = tmp_path / "hive"
    _write_metadata(hive_dir, dolt_mode="embedded")
    monkeypatch.setattr(
        storage_migrate.bd_mod,
        "json",
        lambda args, cwd: {"key": "dolt.shared-server", "value": "true"},
    )

    finding = storage_migrate.detect_pre_existing_drift(hive_dir)

    assert finding is not None
    assert "embedded" in finding and "dolt.shared-server" in finding


def test_detect_pre_existing_drift_none_when_metadata_already_server(tmp_path, monkeypatch):
    hive_dir = tmp_path / "hive"
    _write_metadata(hive_dir, dolt_mode="server")
    monkeypatch.setattr(
        storage_migrate.bd_mod,
        "json",
        lambda args, cwd: {"key": "dolt.shared-server", "value": "true"},
    )
    assert storage_migrate.detect_pre_existing_drift(hive_dir) is None


def test_detect_pre_existing_drift_none_when_shared_server_not_active(tmp_path, monkeypatch):
    hive_dir = tmp_path / "hive"
    _write_metadata(hive_dir, dolt_mode="embedded")
    monkeypatch.setattr(
        storage_migrate.bd_mod, "json", lambda args, cwd: {"key": "dolt.shared-server", "value": ""}
    )
    assert storage_migrate.detect_pre_existing_drift(hive_dir) is None


# ---- bh-l90xk: attribute a failure to the invocation that actually failed, never let an -----
# ---- informational Notice:/Hint: line stand in as the reason --------------------------------


def test_significant_err_line_skips_a_leading_notice_block_and_prefers_the_error_line():
    """The exact real-world shape (module docstring, measured): a `Notice:` headline plus an
    indented continuation line, THEN the real `Error:` line beneath it. The old `err_line`
    (plain first-non-empty-line) would have returned the Notice; this must return the Error."""
    res = subprocess.CompletedProcess(
        ["bd"],
        1,
        "",
        "Notice: shared-server mode is enabled (BEADS_DOLT_SHARED_SERVER or dolt.shared-server "
        "in config.yaml) but\n"
        '    .beads/metadata.json pins dolt_mode="embedded". Using the shared server for this '
        "run.\n"
        '  To persist server mode: set dolt_mode to "server" in .beads/metadata.json and '
        "commit it.\n"
        "Error: cannot start dolt server on port 3308: port 3308 is busy but cannot identify "
        "the process.\n"
        "Check with: lsof -i :3308",
    )

    line = storage_migrate._significant_err_line(res)

    assert line.startswith("Error: cannot start dolt server on port 3308")
    assert "Notice" not in line


def test_significant_err_line_falls_back_to_the_first_line_when_only_notices_exist():
    """Never returns nothing: if every line turns out to be advisory, the plain `err_line`
    fallback still gives the caller something rather than an empty string."""
    res = subprocess.CompletedProcess(["bd"], 1, "", "Notice: informational only\n  continuation")

    line = storage_migrate._significant_err_line(res)

    assert line == "Notice: informational only"


def test_significant_err_line_is_a_noop_on_an_ordinary_single_line_error():
    res = subprocess.CompletedProcess(["bd"], 1, "", "Error: boom")
    assert storage_migrate._significant_err_line(res) == "Error: boom"


def test_bootstrap_shared_server_skips_dolt_start_when_a_server_is_already_reachable(
    tmp_path, monkeypatch
):
    """bh-l90xk: probe first (`dolt_health.probe_shared_server`), and skip `bd dolt start
    --global` entirely when something already answers — running it unconditionally is what
    aborted every migration on a real fleet host whose shared server was started outside bd's
    own bookkeeping, even though the server bootstrap needed was reachable the whole time."""
    monkeypatch.setattr(
        storage_migrate.dolt_health,
        "probe_shared_server",
        lambda **kw: storage_migrate.dolt_health.ProbeResult(True, "127.0.0.1:3308 reachable"),
    )
    calls = []

    def fake_bd(args, cwd, *, actor="", timeout=0, env=None):
        calls.append(list(args))
        return _ok()

    monkeypatch.setattr(storage_migrate, "_bd", fake_bd)

    outcome = storage_migrate._bootstrap_shared_server(tmp_path, "test")

    assert calls == [["bootstrap", "--non-interactive"]]  # `dolt start` never even attempted
    assert outcome.command_label == "bd bootstrap"
    assert outcome.result.returncode == 0


def test_bootstrap_shared_server_starts_when_unreachable_and_attributes_a_start_failure(
    tmp_path, monkeypatch
):
    """When nothing answers, `bd dolt start --global` DOES run — and if IT is the one that
    fails, the outcome must name that command, not "bd bootstrap" (bootstrap must never even be
    attempted once the start step already failed)."""
    monkeypatch.setattr(
        storage_migrate.dolt_health,
        "probe_shared_server",
        lambda **kw: storage_migrate.dolt_health.ProbeResult(False, "nothing listening"),
    )

    def fake_bd(args, cwd, *, actor="", timeout=0, env=None):
        if args[:2] == ["dolt", "start"]:
            return _fail(
                stderr="Error: cannot start dolt server on port 3308: port 3308 is busy but "
                "cannot identify the process.\nCheck with: lsof -i :3308"
            )
        raise AssertionError("bootstrap must never run once `dolt start` already failed")

    monkeypatch.setattr(storage_migrate, "_bd", fake_bd)

    outcome = storage_migrate._bootstrap_shared_server(tmp_path, "test")

    assert outcome.command_label == "bd dolt start --global"
    assert outcome.result.returncode == 1
    assert outcome.port_busy_unattributable is True


def test_bootstrap_shared_server_start_failure_not_matching_the_port_busy_shape_is_unflagged(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        storage_migrate.dolt_health,
        "probe_shared_server",
        lambda **kw: storage_migrate.dolt_health.ProbeResult(False, "nothing listening"),
    )
    monkeypatch.setattr(
        storage_migrate,
        "_bd",
        lambda args, cwd, **kw: _fail(stderr="Error: some other start failure entirely"),
    )

    outcome = storage_migrate._bootstrap_shared_server(tmp_path, "test")

    assert outcome.port_busy_unattributable is False


def test_migrate_hive_bootstrap_failure_attributes_to_dolt_start_and_cross_references_hqmcl(
    tmp_path, monkeypatch
):
    """The full round trip through `migrate_hive`'s own mechanism dispatch (bh-l90xk's
    regression test): a subprocess whose first output line is a `Notice:` and which exits
    non-zero must not have that notice reported as the failure reason, and the command named
    must be the one that actually failed — not a hardcoded "bd bootstrap"."""
    hive_dir = tmp_path / "hive"
    _write_metadata(hive_dir, dolt_mode="embedded", dolt_database="beads")
    store = hive_dir / ".beads" / "embeddeddolt" / "beads"
    store.mkdir(parents=True)
    (store / "manifest").write_bytes(b"x" * 64)
    monkeypatch.setattr(registry, "hive_dir", lambda entry: hive_dir)
    monkeypatch.setattr(storage_migrate.bd_mod, "json", lambda args, cwd: {"value": "h1"})
    monkeypatch.setattr(storage_migrate, "_lock_dir", lambda cfg: tmp_path / "locks")
    monkeypatch.setattr(storage_migrate, "origin_has_dolt_data", lambda hive_dir: True)
    monkeypatch.setattr(storage_migrate, "_issue_count", lambda hive_dir: 1)
    monkeypatch.setattr(
        storage_migrate.dolt_health,
        "probe_shared_server",
        lambda **kw: storage_migrate.dolt_health.ProbeResult(False, "nothing listening"),
    )

    class _OkBackupEngine:
        def export_jsonl(self, cwd, out_path, *, env=None):
            out_path.write_text("{}\n")
            return _ok()

        def backup(self, cwd, dest, *, actor=""):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "x").write_bytes(b"y")
            return _ok()

    monkeypatch.setattr(storage_migrate.engine, "get_engine", lambda cfg: _OkBackupEngine())

    def fake_bd(args, cwd, *, actor="", timeout=0, env=None):
        if args[:2] == ["dolt", "start"]:
            return _fail(
                stderr="Notice: shared-server mode is enabled ... Using the shared server for "
                "this run.\n"
                "Error: cannot start dolt server on port 3308: port 3308 is busy but cannot "
                "identify the process.\nCheck with: lsof -i :3308"
            )
        raise AssertionError("bootstrap must never run once `dolt start` already failed")

    monkeypatch.setattr(storage_migrate, "_bd", fake_bd)

    result = storage_migrate.migrate_hive(_entry(), {})

    assert result.status == "failed"
    assert "bd dolt start --global" in result.detail
    assert "Notice" not in result.detail
    assert "Error: cannot start dolt server on port 3308" in result.detail
    assert "bh-hqmcl" in result.detail


# ---- verify_migration: readable AND complete -----------------------------------------------


def test_verify_migration_ok_when_consistent(tmp_path, monkeypatch):
    hive_dir = tmp_path / "hive"
    _write_metadata(hive_dir, dolt_mode="server")

    def fake_json(args, cwd):
        if args[:2] == ["status", "--no-activity"]:
            return {"summary": {"total_issues": 3}}
        if args[:2] == ["dolt", "status"]:
            return {"schema_version": 1}
        raise AssertionError(args)

    monkeypatch.setattr(storage_migrate.bd_mod, "json", fake_json)
    out = storage_migrate.verify_migration(hive_dir, pre_count=3, cfg={})

    assert out.ok, out.problems
    assert out.issue_count == 3
    assert out.dolt_mode == "server"


def test_verify_migration_fails_on_issue_count_mismatch(tmp_path, monkeypatch):
    hive_dir = tmp_path / "hive"
    _write_metadata(hive_dir, dolt_mode="server")
    monkeypatch.setattr(
        storage_migrate.bd_mod,
        "json",
        lambda args, cwd: {"summary": {"total_issues": 2}} if "status" in args else {},
    )
    out = storage_migrate.verify_migration(hive_dir, pre_count=3, cfg={})
    assert not out.ok
    assert any("mismatch" in p for p in out.problems)


def test_verify_migration_fails_on_engine_metadata_disagreement(tmp_path, monkeypatch):
    """Constraint 1's own sharpest assertion: even when counts and schema all check out, a
    persisted `dolt_mode` still saying "embedded" post-migration is a FAILED migration, not a
    warning."""
    hive_dir = tmp_path / "hive"
    _write_metadata(hive_dir, dolt_mode="embedded")  # never got fixed — simulate the bug
    monkeypatch.setattr(
        storage_migrate.bd_mod,
        "json",
        lambda args, cwd: {"summary": {"total_issues": 3}} if "status" in args else {},
    )
    out = storage_migrate.verify_migration(hive_dir, pre_count=3, cfg={})
    assert not out.ok
    assert any("disagreement" in p for p in out.problems)


def test_verify_migration_fails_when_store_does_not_open(tmp_path, monkeypatch):
    hive_dir = tmp_path / "hive"
    _write_metadata(hive_dir, dolt_mode="server")
    monkeypatch.setattr(storage_migrate.bd_mod, "json", lambda args, cwd: None)
    out = storage_migrate.verify_migration(hive_dir, pre_count=3, cfg={})
    assert not out.ok
    assert any("did not open" in p for p in out.problems)


# ---- constraint 3: per-hive serialization -----------------------------------------------


def test_hive_migration_lock_blocks_a_concurrent_migrator(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_migrate, "_lock_dir", lambda cfg: tmp_path / "locks")
    with storage_migrate.hive_migration_lock({}, "github/o/h1"):
        with pytest.raises(storage_migrate.HiveLocked):
            with storage_migrate.hive_migration_lock({}, "github/o/h1"):
                pass  # pragma: no cover - never reached


def test_hive_migration_lock_reclaims_a_stale_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_migrate, "_lock_dir", lambda cfg: tmp_path / "locks")
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(parents=True)
    (lock_dir / "github-o-h1.lock").write_text("999999999")  # a PID that cannot exist

    with storage_migrate.hive_migration_lock({}, "github/o/h1"):
        pass  # acquired cleanly despite the pre-existing (stale) lock file


def test_hive_migration_lock_releases_on_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_migrate, "_lock_dir", lambda cfg: tmp_path / "locks")
    with storage_migrate.hive_migration_lock({}, "github/o/h1"):
        pass
    with storage_migrate.hive_migration_lock({}, "github/o/h1"):
        pass  # second acquisition after the first released — no HiveLocked


# ---- mechanism selection (bh-oa225): bootstrap-from-origin vs reinit-in-place -------------
#
# `bd init --reinit-local` REFUSES outright whenever the remote already carries
# `refs/dolt/data` — proven on this fleet: every hive that has ever pushed bead state has it.
# Remote Dolt history is `bd bootstrap`'s PRECONDITION, not its blocker (same branch
# `onboard.py`'s own bd-mint step already takes), so the mechanism must be SELECTED on that
# fact, not assumed to always be reinit.


def test_origin_has_dolt_data_true_when_the_remote_carries_the_ref(tmp_path):
    from harness.world import git

    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", "-b", "main", str(remote))
    # A blob-pointing ref is enough to exist — origin_has_dolt_data only checks presence, never
    # walks the object (gitref.py's own precedent: a blob keeps the object graph empty).
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin", "-t", "blob"],
        cwd=str(remote),
        input="",
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    git("update-ref", "refs/dolt/data", blob, cwd=remote)

    hive_dir = tmp_path / "hive"
    git("init", "-q", "-b", "main", str(hive_dir))
    git("remote", "add", "origin", str(remote), cwd=hive_dir)

    assert storage_migrate.origin_has_dolt_data(hive_dir) is True


def test_origin_has_dolt_data_false_when_the_remote_has_no_dolt_ref(tmp_path):
    from harness.world import git

    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", "-b", "main", str(remote))
    hive_dir = tmp_path / "hive"
    git("init", "-q", "-b", "main", str(hive_dir))
    git("remote", "add", "origin", str(remote), cwd=hive_dir)

    assert storage_migrate.origin_has_dolt_data(hive_dir) is False


def test_origin_has_dolt_data_false_when_no_remote_is_configured(tmp_path):
    from harness.world import git

    hive_dir = tmp_path / "hive"
    git("init", "-q", "-b", "main", str(hive_dir))

    assert storage_migrate.origin_has_dolt_data(hive_dir) is False


def test_select_mechanism_bootstrap_when_origin_has_dolt_data(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_migrate, "origin_has_dolt_data", lambda hive_dir: True)
    assert storage_migrate.select_mechanism(tmp_path) == "bootstrap"


def test_select_mechanism_reinit_when_origin_has_no_dolt_data(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_migrate, "origin_has_dolt_data", lambda hive_dir: False)
    assert storage_migrate.select_mechanism(tmp_path) == "reinit"


def test_mechanism_blocker_none_for_the_expected_pairings(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_migrate, "origin_has_dolt_data", lambda hive_dir: True)
    assert storage_migrate.mechanism_blocker(tmp_path, "bootstrap") is None
    monkeypatch.setattr(storage_migrate, "origin_has_dolt_data", lambda hive_dir: False)
    assert storage_migrate.mechanism_blocker(tmp_path, "reinit") is None


def test_mechanism_blocker_fires_for_reinit_against_a_dolt_data_remote(tmp_path, monkeypatch):
    """The regression guard bh-oa225 exists for: reinit against a remote that already carries
    refs/dolt/data is proven to always be refused by bd itself. `select_mechanism` never
    produces this pairing on its own (the tests above prove that) — this proves the SAFETY NET
    still catches it if it ever does (a future regression back to "always reinit")."""
    monkeypatch.setattr(storage_migrate, "origin_has_dolt_data", lambda hive_dir: True)
    reason = storage_migrate.mechanism_blocker(tmp_path, "reinit")
    assert reason is not None
    assert "refs/dolt/data" in reason


# ---- migrate_hive: idempotency + dry-run + orchestration ---------------------------------


def _entry(org="o", repo="h1", prefix="h1"):
    return {"provider": "github", "org": org, "repo": repo, "prefix": prefix, "kind": "personal"}


def test_migrate_hive_skips_a_checkout_with_no_beads_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "hive_dir", lambda entry: tmp_path / "nowhere")
    result = storage_migrate.migrate_hive(_entry(), {})
    assert result.status == "skipped"


def test_migrate_hive_already_migrated_is_a_noop_but_heals_config(tmp_path, monkeypatch):
    hive_dir = tmp_path / "hive"
    _write_metadata(hive_dir, dolt_mode="server")
    monkeypatch.setattr(registry, "hive_dir", lambda entry: hive_dir)
    monkeypatch.setattr(storage_migrate.bd_mod, "json", lambda args, cwd: {"value": "h1"})

    healed = []
    monkeypatch.setattr(
        storage_migrate,
        "run",
        lambda cmd, **kw: healed.append(cmd) or _ok(),
    )

    result = storage_migrate.migrate_hive(_entry(), {})

    assert result.status == "already-migrated"
    assert result.dolt_mode == "server"
    joined = [" ".join(c) for c in healed]
    assert any("backup.enabled" in c for c in joined)
    assert any("dolt.shared-server" in c for c in joined)


def test_migrate_hive_dry_run_reports_size_and_target_and_changes_nothing(tmp_path, monkeypatch):
    hive_dir = tmp_path / "hive"
    _write_metadata(hive_dir, dolt_mode="embedded", dolt_database="scremb")
    store = hive_dir / ".beads" / "embeddeddolt"
    store.mkdir(parents=True)
    (store / "manifest").write_bytes(b"x" * 1024)

    monkeypatch.setattr(registry, "hive_dir", lambda entry: hive_dir)
    monkeypatch.setattr(storage_migrate.bd_mod, "json", lambda args, cwd: {"value": "h1"})
    # No remote Dolt history for this hive (bh-oa225 mechanism selection) — reinit is safe to
    # preview, which is what the rest of this test's boom-on-mutation assertions below assume.
    monkeypatch.setattr(storage_migrate, "origin_has_dolt_data", lambda hive_dir: False)

    def _boom(*a, **k):
        raise AssertionError("dry-run must not touch bd or mutate the filesystem")

    monkeypatch.setattr(storage_migrate, "_reinit_shared_server", _boom)
    monkeypatch.setattr(storage_migrate, "_bootstrap_shared_server", _boom)

    result = storage_migrate.migrate_hive(_entry(), {}, dry_run=True)

    assert result.status == "would-migrate"
    assert result.mechanism == "reinit"
    assert result.size_bytes == 1024
    # bh-g5ujg: the SERVER database is named from the hive PREFIX ("h1"), not from
    # `dolt_database` ("scremb"). Naming it from dolt_database is what put six hives on one
    # store, since bd defaults that key to "beads" almost everywhere.
    assert result.target_path.endswith("dolt/h1")
    assert result.server_database == "h1"
    assert store.is_dir()  # untouched


def test_migrate_hive_dry_run_selects_bootstrap_when_origin_has_dolt_data(tmp_path, monkeypatch):
    """Regression test (bh-oa225 acceptance): a hive fixture whose remote advertises
    refs/dolt/data does not take the reinit path. `bd init --reinit-local` refuses outright
    against every hive on the real fleet that has ever pushed bead state — this is the bug this
    bead exists to fix, so `_reinit_shared_server` must never even be reachable here."""
    hive_dir = tmp_path / "hive"
    _write_metadata(hive_dir, dolt_mode="embedded", dolt_database="scremb")
    store = hive_dir / ".beads" / "embeddeddolt"
    store.mkdir(parents=True)
    (store / "manifest").write_bytes(b"x" * 1024)

    monkeypatch.setattr(registry, "hive_dir", lambda entry: hive_dir)
    monkeypatch.setattr(storage_migrate.bd_mod, "json", lambda args, cwd: {"value": "h1"})
    monkeypatch.setattr(storage_migrate, "origin_has_dolt_data", lambda hive_dir: True)

    def _boom(*a, **k):
        raise AssertionError("dry-run must not touch bd or mutate the filesystem")

    monkeypatch.setattr(storage_migrate, "_reinit_shared_server", _boom)
    monkeypatch.setattr(storage_migrate, "_bootstrap_shared_server", _boom)

    result = storage_migrate.migrate_hive(_entry(), {}, dry_run=True)

    assert result.status == "would-migrate"
    assert result.mechanism == "bootstrap"


def test_migrate_hive_dry_run_reports_blocked_when_mechanism_would_be_refused(
    tmp_path, monkeypatch
):
    """The dry-run must stop lying (bh-oa225 acceptance): if the SELECTED mechanism would be
    refused, `--dry-run` reports it as a blocker instead of a clean `would-migrate` + exit 0
    for an operation proven to always fail. `select_mechanism` itself never produces the
    "reinit against a dolt-data remote" pairing (the test above proves that) — this forces it
    directly to prove `mechanism_blocker`'s safety net is actually wired into the dry-run path,
    not merely defined and unused, mirroring bh-g5ujg's `detect_target_collisions` shape."""
    hive_dir = tmp_path / "hive"
    _write_metadata(hive_dir, dolt_mode="embedded", dolt_database="scremb")
    store = hive_dir / ".beads" / "embeddeddolt"
    store.mkdir(parents=True)
    (store / "manifest").write_bytes(b"x" * 1024)

    monkeypatch.setattr(registry, "hive_dir", lambda entry: hive_dir)
    monkeypatch.setattr(storage_migrate.bd_mod, "json", lambda args, cwd: {"value": "h1"})
    monkeypatch.setattr(storage_migrate, "select_mechanism", lambda hive_dir: "reinit")
    monkeypatch.setattr(storage_migrate, "origin_has_dolt_data", lambda hive_dir: True)

    result = storage_migrate.migrate_hive(_entry(), {}, dry_run=True)

    assert result.status == "blocked"
    assert "refs/dolt/data" in result.detail


def test_migrate_hive_real_run_dispatches_bootstrap_not_reinit(tmp_path, monkeypatch):
    """The mechanism DISPATCH itself, not merely the dry-run preview, must route to bootstrap
    when the remote already carries refs/dolt/data (bh-oa225) — proven by making the mechanism
    call itself fail distinctively and asserting reinit was never even attempted."""
    hive_dir = tmp_path / "hive"
    _write_metadata(hive_dir, dolt_mode="embedded")
    store = hive_dir / ".beads" / "embeddeddolt" / "scremb"
    store.mkdir(parents=True)
    (store / "manifest").write_bytes(b"x" * 64)
    monkeypatch.setattr(registry, "hive_dir", lambda entry: hive_dir)
    monkeypatch.setattr(storage_migrate.bd_mod, "json", lambda args, cwd: {"value": "h1"})
    monkeypatch.setattr(storage_migrate, "_lock_dir", lambda cfg: tmp_path / "locks")
    monkeypatch.setattr(storage_migrate, "origin_has_dolt_data", lambda hive_dir: True)
    monkeypatch.setattr(storage_migrate, "_issue_count", lambda hive_dir: 1)

    class _OkBackupEngine:
        def export_jsonl(self, cwd, out_path, *, env=None):
            out_path.write_text("{}\n")  # one line, matching the mocked _issue_count == 1
            return _ok()

        def backup(self, cwd, dest, *, actor=""):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "x").write_bytes(b"y")
            return _ok()

    monkeypatch.setattr(storage_migrate.engine, "get_engine", lambda cfg: _OkBackupEngine())

    reinit_called = []
    monkeypatch.setattr(
        storage_migrate, "_reinit_shared_server", lambda *a, **k: reinit_called.append(1) or _ok()
    )
    monkeypatch.setattr(
        storage_migrate,
        "_bootstrap_shared_server",
        lambda *a, **k: storage_migrate.MechanismOutcome(
            result=_fail(stderr="bootstrap ran"), command_label="bd bootstrap"
        ),
    )

    result = storage_migrate.migrate_hive(_entry(), {})

    assert result.status == "failed"
    assert "bd bootstrap" in result.detail
    assert "bootstrap ran" in result.detail
    assert reinit_called == []  # the historical bug's own destructive step, never reached


# ---- bh-8g6cj: `bd bootstrap` targets metadata.json's own `dolt_database`, not this module's --
# ---- collision-free name — measured directly against a real bd binary + real shared server ---


def _bootstrap_fixture(tmp_path, monkeypatch, *, dolt_database="beads"):
    """The exact shape a real hive is in (module docstring: the real `beadhive-ui` hive's own
    `.beads/metadata.json` carries `dolt_database: "beads"`, bd's generic default — NOT its
    prefix) — a live embedded store whose remote already carries `refs/dolt/data`, so
    `select_mechanism` picks bootstrap."""
    hive_dir = tmp_path / "hive"
    _write_metadata(hive_dir, dolt_mode="embedded", dolt_database=dolt_database)
    store = hive_dir / ".beads" / "embeddeddolt" / dolt_database
    store.mkdir(parents=True)
    (store / "manifest").write_bytes(b"x" * 64)
    monkeypatch.setattr(registry, "hive_dir", lambda entry: hive_dir)
    monkeypatch.setattr(storage_migrate.bd_mod, "json", lambda args, cwd: {"value": "h1"})
    monkeypatch.setattr(storage_migrate, "_lock_dir", lambda cfg: tmp_path / "locks")
    monkeypatch.setattr(storage_migrate, "origin_has_dolt_data", lambda hive_dir: True)
    monkeypatch.setattr(storage_migrate, "_issue_count", lambda hive_dir: 1)

    class _OkBackupEngine:
        def export_jsonl(self, cwd, out_path, *, env=None):
            out_path.write_text("{}\n")
            return _ok()

        def backup(self, cwd, dest, *, actor=""):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "x").write_bytes(b"y")
            return _ok()

        def backup_restore(self, cwd, native_dir, *, actor=""):
            return _ok()

    monkeypatch.setattr(storage_migrate.engine, "get_engine", lambda cfg: _OkBackupEngine())
    return hive_dir


def test_migrate_hive_repoints_dolt_database_before_calling_bootstrap_and_restores_it_on_failure(
    tmp_path, monkeypatch
):
    """The actual fix (module docstring): `bd bootstrap` has no working `--database` override in
    shared-server mode, so `migrate_hive` must repoint metadata.json's `dolt_database` to its
    own collision-free `db_name` BEFORE calling it — and, since bootstrap didn't actually
    migrate here, put the ORIGINAL value straight back so the still-embedded hive (whose
    on-disk `embeddeddolt/beads/` subdirectory name hasn't moved) stays exactly as readable as
    it was."""
    hive_dir = _bootstrap_fixture(tmp_path, monkeypatch, dolt_database="beads")
    seen = []

    def fake_bootstrap(hd, actor):
        seen.append(storage_migrate._read_dolt_database(hd))
        return storage_migrate.MechanismOutcome(
            result=_fail(stderr="declined"), command_label="bd bootstrap"
        )

    monkeypatch.setattr(storage_migrate, "_bootstrap_shared_server", fake_bootstrap)

    result = storage_migrate.migrate_hive(_entry(), {})

    assert result.status == "failed"
    assert seen == ["h1"]  # repointed to the collision-free name BEFORE the call, not "beads"
    assert storage_migrate._read_dolt_database(hive_dir) == "beads"  # restored after the failure


def test_migrate_hive_leaves_dolt_database_repointed_after_a_successful_bootstrap(
    tmp_path, monkeypatch
):
    """Measured directly (module docstring): restoring `dolt_database` to its pre-bootstrap
    value AFTER a SUCCESSFUL bootstrap breaks the migrated hive outright ("PROJECT IDENTITY
    MISMATCH — refusing to connect") — bd has no separate notion of "the bootstrap target" vs
    "the database this project connects to"; unlike this module's own additive
    `dolt_server_database` key, `dolt_database` is both, for bd, forever after. So a successful
    mechanism call must never trigger the restore."""
    hive_dir = _bootstrap_fixture(tmp_path, monkeypatch, dolt_database="beads")
    monkeypatch.setattr(
        storage_migrate,
        "_bootstrap_shared_server",
        lambda hd, actor: storage_migrate.MechanismOutcome(
            result=_ok(), command_label="bd bootstrap"
        ),
    )
    # Everything past the mechanism call is real-`bd`-shaped bookkeeping this unit fixture can't
    # satisfy (config sets, a live restore, a live verify) — irrelevant to what this test proves
    # (the repoint decision is made and never undone once the mechanism itself reports success),
    # so neutralize it rather than fight it.
    monkeypatch.setattr(storage_migrate, "_persist_shared_server_config", lambda hd, actor: None)
    monkeypatch.setattr(storage_migrate, "_persist_backup_enabled", lambda hd, actor: None)

    storage_migrate.migrate_hive(_entry(), {})

    # Whatever verify_migration made of the rest, dolt_database was never put back to "beads".
    assert storage_migrate._read_dolt_database(hive_dir) == "h1"


def test_migrate_hive_flags_an_interrupted_migration_instead_of_healing_it_silently(
    tmp_path, monkeypatch
):
    """bh-8g6cj design point 6: `dolt_mode` flips to "server" a few steps BEFORE the original
    embedded store is moved aside (the LAST step, unchanged). A hive caught with `dolt_mode ==
    "server"` but its original, un-renamed `embeddeddolt/` still on disk was interrupted
    mid-migration (backup_restore/verify/move-aside never ran) — the pre-fix "already-migrated"
    heal branch would mistake that for a clean, finished hive and silently stop there forever."""
    hive_dir = tmp_path / "hive"
    _write_metadata(hive_dir, dolt_mode="server")
    store = hive_dir / ".beads" / "embeddeddolt" / "scremb"
    store.mkdir(parents=True)
    (store / "manifest").write_bytes(b"x" * 64)
    monkeypatch.setattr(registry, "hive_dir", lambda entry: hive_dir)
    monkeypatch.setattr(storage_migrate.bd_mod, "json", lambda args, cwd: {"value": "h1"})

    result = storage_migrate.migrate_hive(_entry(), {})

    assert result.status == "failed"
    assert "interrupted" in result.detail.lower()


def test_migrate_hive_already_migrated_stays_a_noop_when_the_embedded_store_is_really_gone(
    tmp_path, monkeypatch
):
    """The companion case to the interrupted-migration test above: a hive that completed
    `_retire_embedded_store` (so the ORIGINAL `embeddeddolt/` name no longer exists — only
    the renamed `embeddeddolt.pre-migrate-<stamp>/` does) is genuinely done, and must still take
    the ordinary healing no-op path."""
    hive_dir = tmp_path / "hive"
    _write_metadata(hive_dir, dolt_mode="server")
    (hive_dir / ".beads" / "embeddeddolt.pre-migrate-20260101T000000Z").mkdir(parents=True)
    monkeypatch.setattr(registry, "hive_dir", lambda entry: hive_dir)
    monkeypatch.setattr(storage_migrate.bd_mod, "json", lambda args, cwd: {"value": "h1"})
    monkeypatch.setattr(storage_migrate, "run", lambda cmd, **kw: _ok())

    result = storage_migrate.migrate_hive(_entry(), {})

    assert result.status == "already-migrated"


def test_migrate_hive_refuses_to_proceed_past_an_unverified_backup(tmp_path, monkeypatch):
    hive_dir = tmp_path / "hive"
    _write_metadata(hive_dir, dolt_mode="embedded")
    store = hive_dir / ".beads" / "embeddeddolt" / "scremb"
    store.mkdir(parents=True)
    # Must hold real bytes: an EMPTY embedded store is classified `no-store` and returns before
    # the backup gate (bh-g5ujg), which is not the path this test is about.
    (store / "manifest").write_bytes(b"x" * 64)
    monkeypatch.setattr(registry, "hive_dir", lambda entry: hive_dir)
    monkeypatch.setattr(storage_migrate.bd_mod, "json", lambda args, cwd: {"value": "h1"})
    monkeypatch.setattr(storage_migrate, "_lock_dir", lambda cfg: tmp_path / "locks")

    class _FailingBackupEngine:
        def export_jsonl(self, cwd, out_path, *, env=None):
            return _fail()

        def backup(self, cwd, dest, *, actor=""):
            return _fail()

    monkeypatch.setattr(storage_migrate.engine, "get_engine", lambda cfg: _FailingBackupEngine())

    reinit_called = []
    monkeypatch.setattr(
        storage_migrate, "_reinit_shared_server", lambda *a, **k: reinit_called.append(1)
    )

    result = storage_migrate.migrate_hive(_entry(), {})

    assert result.status == "failed"
    assert "backup" in result.detail
    assert reinit_called == []  # never reached the destructive step


# ---- migrate_fleet: resumable + per-hive isolated ------------------------------------------


def test_fleet_resume_after_a_simulated_interrupt(tmp_path, monkeypatch):
    """A test harness simulating interrupt/resume, per this bead's own acceptance wording: a
    fleet run that dies partway (h2 "crashes") must, on a second call, skip the already-migrated
    hive and retry the rest — never restart from hive 1."""
    monkeypatch.setattr(storage_migrate, "_state_path", lambda cfg: tmp_path / "state.json")
    cfg = {
        "managed_repos": [
            _entry(repo="h1", prefix="h1"),
            _entry(repo="h2", prefix="h2"),
            _entry(repo="h3", prefix="h3"),
        ]
    }

    seen = []

    def flaky(entry, cfg, *, dry_run=False, actor="", keep_pre_migrate=False):
        seen.append(entry["repo"])
        if entry["repo"] == "h1":
            return storage_migrate.HiveMigrationResult(
                hive_id=registry.hive_key(entry), hive_dir=tmp_path, status="migrated"
            )
        raise RuntimeError("simulated kill mid-hive")

    monkeypatch.setattr(storage_migrate, "migrate_hive", flaky)
    with pytest.raises(RuntimeError):
        storage_migrate.migrate_fleet(cfg)
    assert seen == ["h1", "h2"]  # died partway through h2; h3 never attempted

    # resume: a second call must skip h1 (fast path, no migrate_hive call at all) and retry h2/h3.
    seen.clear()

    def healthy(entry, cfg, *, dry_run=False, actor="", keep_pre_migrate=False):
        seen.append(entry["repo"])
        return storage_migrate.HiveMigrationResult(
            hive_id=registry.hive_key(entry), hive_dir=tmp_path, status="migrated"
        )

    monkeypatch.setattr(storage_migrate, "migrate_hive", healthy)
    results = storage_migrate.migrate_fleet(cfg)

    assert seen == ["h2", "h3"]
    by_id = {r.hive_id: r.status for r in results}
    assert by_id[registry.hive_key(_entry(repo="h1"))] == "already-migrated"
    assert by_id[registry.hive_key(_entry(repo="h2"))] == "migrated"
    assert by_id[registry.hive_key(_entry(repo="h3"))] == "migrated"


def test_fleet_isolates_one_hive_failure_from_the_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_migrate, "_state_path", lambda cfg: tmp_path / "state.json")
    cfg = {
        "managed_repos": [
            _entry(repo="h1", prefix="h1"),
            _entry(repo="h2", prefix="h2"),
        ]
    }

    def per_hive(entry, cfg, *, dry_run=False, actor="", keep_pre_migrate=False):
        status = "failed" if entry["repo"] == "h1" else "migrated"
        return storage_migrate.HiveMigrationResult(
            hive_id=registry.hive_key(entry), hive_dir=tmp_path, status=status, detail="x"
        )

    monkeypatch.setattr(storage_migrate, "migrate_hive", per_hive)
    results = storage_migrate.migrate_fleet(cfg)

    by_id = {r.hive_id: r.status for r in results}
    assert by_id[registry.hive_key(_entry(repo="h1"))] == "failed"
    assert by_id[registry.hive_key(_entry(repo="h2"))] == "migrated"  # never stranded


def test_fleet_dry_run_never_writes_state(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(storage_migrate, "_state_path", lambda cfg: state_path)
    cfg = {"managed_repos": [_entry(repo="h1", prefix="h1")]}
    monkeypatch.setattr(
        storage_migrate,
        "migrate_hive",
        lambda entry, cfg, *, dry_run=False, actor="", keep_pre_migrate=False: (
            storage_migrate.HiveMigrationResult(
                hive_id=registry.hive_key(entry), hive_dir=tmp_path, status="would-migrate"
            )
        ),
    )
    storage_migrate.migrate_fleet(cfg, dry_run=True)
    assert not state_path.exists()


# ---- CLI entry point: --confirm gate + the rollback statement -----------------------------


def test_migrate_cli_refuses_a_real_run_without_confirm(world, capsys):
    with pytest.raises(typer.Exit):
        storage_migrate.migrate("", dry_run=False, confirm=False)
    out = capsys.readouterr()
    assert "confirm" in (out.out + out.err).lower()


def test_migrate_cli_prints_the_rollback_distinction_before_a_real_run(world, monkeypatch, capsys):
    monkeypatch.setattr(storage_migrate, "migrate_fleet", lambda cfg, **kw: [])
    storage_migrate.migrate("", dry_run=False, confirm=True)
    out = capsys.readouterr().out
    assert "reversible" in out.lower()
    assert "one-way" in out.lower()


def test_migrate_cli_dry_run_does_not_require_confirm(world, monkeypatch, capsys):
    monkeypatch.setattr(storage_migrate, "migrate_fleet", lambda cfg, **kw: [])
    storage_migrate.migrate("", dry_run=True, confirm=False)  # must not raise
    out = capsys.readouterr().out
    assert "reversible" not in out.lower()  # rollback banner only precedes a REAL run


# ---- target collisions: a fleet-wide PRE-FLIGHT invariant (bh-g5ujg) ----------------------
#
# Six hives on this fleet all carried bd's default dolt_database="beads", so every one of them
# resolved to ~/.beads/shared-server/dolt/beads — and the run reported "0 failed". Migrating
# would have merged six independent bead corpora into one store.


def _fleet(tmp_path, monkeypatch, hives):
    """hives: {repo: (dolt_mode, dolt_database, prefix, has_store)} -> a cfg with those hives."""
    cfg = {
        "managed_repos": [
            {"provider": "github", "org": "o", "repo": repo, "prefix": spec[2], "kind": "p"}
            for repo, spec in hives.items()
        ]
    }
    for repo, (mode, database, _prefix, has_store) in hives.items():
        hive = tmp_path / repo
        _write_metadata(hive, dolt_mode=mode, dolt_database=database)
        if has_store:
            store = hive / ".beads" / "embeddeddolt" / database
            store.mkdir(parents=True)
            (store / "chunk").write_text("x" * 32)
    monkeypatch.setattr(storage_migrate.registry, "hive_dir", lambda e: tmp_path / e["repo"])
    monkeypatch.setattr(storage_migrate, "_effective_prefix", lambda hd, e: str(e["prefix"]))
    return cfg


def test_default_dolt_database_no_longer_collides(tmp_path, monkeypatch):
    """The fix: two hives both carrying dolt_database="beads" now resolve by PREFIX, which bh
    already enforces unique fleet-wide — so the collision is structurally impossible."""
    cfg = _fleet(
        tmp_path,
        monkeypatch,
        {
            "beadhive": ("embedded", "beads", "bh", True),
            "beadhive-ui": ("embedded", "beads", "bhui", True),
        },
    )
    assert storage_migrate.detect_target_collisions(cfg) == {}
    targets = {p.hive_id: p.database for p in storage_migrate.plan_targets(cfg)}
    assert set(targets.values()) == {"bh", "bhui"}


def test_detect_target_collisions_flags_two_hives_on_one_database(tmp_path, monkeypatch):
    """Two ALREADY-migrated hives sharing a name are grandfathered as-is (resolution order 2),
    so the pre-flight is what catches them — the belt-and-braces the design keeps even after
    prefix-derived naming makes new collisions impossible."""
    cfg = _fleet(
        tmp_path,
        monkeypatch,
        {
            "one": ("server", "shared", "one", False),
            "two": ("server", "shared", "two", False),
        },
    )
    collisions = storage_migrate.detect_target_collisions(cfg)
    assert len(collisions) == 1
    assert sorted(next(iter(collisions.values()))) == ["github/o/one", "github/o/two"]


def test_collision_detection_counts_an_already_migrated_hive(tmp_path, monkeypatch):
    """An un-migrated hive resolving onto a database an already-migrated one OCCUPIES is exactly
    as destructive as two un-migrated ones colliding, so occupied names must be in the map."""
    cfg = _fleet(
        tmp_path,
        monkeypatch,
        {
            "observaloop": ("server", "obs", "observaloop", False),
            "other": ("embedded", "beads", "obs", True),
        },
    )
    assert storage_migrate.detect_target_collisions(cfg) != {}


def test_migrate_refuses_and_exits_nonzero_on_collision(tmp_path, monkeypatch, capsys):
    """A dry-run that renders a colliding plan without flagging it is the defect; a scripted
    `--dry-run && --confirm` must not be able to walk into it."""
    cfg = _fleet(
        tmp_path,
        monkeypatch,
        {
            "one": ("server", "shared", "one", False),
            "two": ("server", "shared", "two", False),
        },
    )
    monkeypatch.setattr(storage_migrate.config, "load", lambda: cfg)
    monkeypatch.setattr(
        storage_migrate, "migrate_fleet", lambda *a, **k: pytest.fail("must not migrate")
    )

    with pytest.raises(typer.Exit) as exc:
        storage_migrate.migrate("", dry_run=True, confirm=False)

    assert exc.value.exit_code == 1
    assert "collision" in capsys.readouterr().err.lower()


def test_hive_with_beads_but_no_store_is_not_would_migrate(tmp_path, monkeypatch):
    """The `size: 0B  [would-migrate]` rows: `.beads/` came from git (config.yaml +
    metadata.json) with no database under it. Minting an empty database there would shadow a
    later real one."""
    monkeypatch.setattr(storage_migrate.bd_mod, "json", lambda *a, **k: {})
    hive = tmp_path / "empty"
    _write_metadata(hive, dolt_mode="embedded", dolt_database="beads")
    monkeypatch.setattr(storage_migrate.registry, "hive_dir", lambda e: hive)

    result = storage_migrate.migrate_hive(
        {"provider": "github", "org": "o", "repo": "empty", "prefix": "empty"}, {}, dry_run=True
    )

    assert result.status == "no-store"
    assert result.size_bytes == 0


# ---- bh-xsv3: gitignore the moved-aside embedded store -------------------------------------
#
# `.beads/.gitignore`'s exact-name `embeddeddolt/` entry does not match
# `_retire_embedded_store`'s own `embeddeddolt.pre-migrate-<stamp>/` rename target, so a
# furnished hive (tracked `.beads/`) shows the moved-aside store as hundreds of MB of untracked
# files right after migrating. Real `git` repos throughout (a monkeypatched `run` seam can't
# prove an actual commit landed cleanly) — the same discipline `test_hive_repair.py`'s own
# `_git` helper uses for exactly this reason.


def _git(*args, cwd):
    return real_run(["git", *args], cwd=str(cwd), check=True, capture=True, env=git_env())


def _configure_fixture_identity(repo):
    """Give a synthetic repository its own identity, independent of host/global config."""
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)


def _init_tracked_gitignore(base, lines=None):
    """A minimal git repo with a git-TRACKED `.beads/.gitignore` — the furnished-hive shape
    this fix targets."""
    (base / ".beads").mkdir(parents=True, exist_ok=True)
    gi = base / ".beads" / ".gitignore"
    gi.write_text(
        "\n".join(lines if lines is not None else ["dolt/", "embeddeddolt/", "proxieddb/"]) + "\n"
    )
    _git("init", "-q", "-b", "main", cwd=base)
    _configure_fixture_identity(base)
    # This exact fixture file is intentionally tracked even when an operator's global excludes
    # hide `.beads/*`. Do not disable ignores wholesale: production Git configuration still
    # participates in every other fixture operation and verdict.
    _git("add", "-f", "--", ".beads/.gitignore", cwd=base)
    _git("commit", "-q", "-m", "init", cwd=base)
    return gi


def test_tracked_gitignore_fixture_is_hermetic_under_global_excludes(tmp_path, monkeypatch):
    """An operator-wide `.beads/*` exclude cannot change fixture setup or its verdict.

    The poison files live under this test's tmp_path: the fixture must neither consult nor
    mutate the operator's repository or global Git state while proving the exact global-config
    shape that exposed bh-idn2c.
    """
    excludes = tmp_path / "global-excludes"
    excludes.write_text(".beads/*\n")
    global_config = tmp_path / "global-gitconfig"
    global_config.write_text(f"[core]\n\texcludesFile = {excludes}\n")
    before = (global_config.read_bytes(), excludes.read_bytes())
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    operator_repo = tmp_path / "operator-repo"
    operator_repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=operator_repo)
    _configure_fixture_identity(operator_repo)
    (operator_repo / "operator-only").write_text("must remain untouched\n")
    _git("add", "--", "operator-only", cwd=operator_repo)
    _git("commit", "-q", "-m", "operator baseline", cwd=operator_repo)
    operator_git = operator_repo / ".git"
    quarantine = operator_git / "objects" / "incoming-test"
    quarantine.mkdir()
    operator_config = tmp_path / "operator-external-gitconfig"
    operator_config.write_text("[operator]\n\tsentinel = unchanged\n")
    operator_grafts = tmp_path / "operator-grafts"
    operator_grafts.write_text("")
    before_external = (operator_config.read_bytes(), operator_grafts.read_bytes())
    routing = {
        "GIT_CONFIG": operator_config,
        "GIT_DIR": operator_git,
        "GIT_WORK_TREE": operator_repo,
        "GIT_IMPLICIT_WORK_TREE": "1",
        "GIT_INDEX_FILE": operator_git / "index",
        "GIT_COMMON_DIR": operator_git,
        "GIT_OBJECT_DIRECTORY": operator_git / "objects",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": operator_git / "objects",
        "GIT_GRAFT_FILE": operator_grafts,
        "GIT_NAMESPACE": "operator-test",
        "GIT_PREFIX": "operator-prefix/",
        "GIT_INTERNAL_SUPER_PREFIX": "operator-super/",
        "GIT_SHALLOW_FILE": operator_git / "shallow",
        "GIT_QUARANTINE_PATH": quarantine,
        "GIT_CEILING_DIRECTORIES": tmp_path,
        "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1",
        "GIT_REPLACE_REF_BASE": "refs/operator-replace/",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.worktree",
        "GIT_CONFIG_VALUE_0": operator_repo,
        "GIT_CONFIG_PARAMETERS": f"'core.worktree'='{operator_repo}'",
    }
    before_operator = {
        path.relative_to(operator_repo): path.read_bytes()
        for path in operator_repo.rglob("*")
        if path.is_file()
    }
    with monkeypatch.context() as hostile:
        for key, value in routing.items():
            hostile.setenv(key, str(value))

        repo = tmp_path / "fixture-repo"
        gi = _init_tracked_gitignore(repo)
        assert _git("ls-files", "--error-unmatch", ".beads/.gitignore", cwd=repo).stdout.strip()

    # The production helper still sees the poisoned global excludes, but not the intentionally
    # fixture-only routing attack above. Its real Git-config behavior remains part of the verdict.
    assert storage_migrate._ensure_pre_migrate_gitignore(repo) is True
    assert "embeddeddolt.pre-migrate-*/" in gi.read_text()
    assert (global_config.read_bytes(), excludes.read_bytes()) == before
    after_operator = {
        path.relative_to(operator_repo): path.read_bytes()
        for path in operator_repo.rglob("*")
        if path.is_file()
    }
    assert after_operator == before_operator
    assert (operator_config.read_bytes(), operator_grafts.read_bytes()) == before_external


def test_ensure_pre_migrate_gitignore_is_a_noop_with_no_gitignore_file(tmp_path):
    assert storage_migrate._ensure_pre_migrate_gitignore(tmp_path) is False


def test_ensure_pre_migrate_gitignore_is_a_noop_when_untracked(tmp_path):
    """The zero-footprint shape: `.beads/.gitignore` is present on disk but was never `git
    add`ed (the whole `.beads/` is excluded via `.git/info/exclude` instead) — nothing to fix,
    since a hive in this shape is already unaffected by bh-xsv3 (the bead's own scope note)."""
    (tmp_path / ".beads").mkdir()
    gi = tmp_path / ".beads" / ".gitignore"
    gi.write_text("embeddeddolt/\n")
    _git("init", "-q", "-b", "main", cwd=tmp_path)

    assert storage_migrate._ensure_pre_migrate_gitignore(tmp_path) is False
    assert gi.read_text() == "embeddeddolt/\n"  # untouched


def test_ensure_pre_migrate_gitignore_inserts_after_the_embeddeddolt_line(tmp_path):
    gi = _init_tracked_gitignore(tmp_path)

    wrote = storage_migrate._ensure_pre_migrate_gitignore(tmp_path)

    assert wrote is True
    lines = gi.read_text().splitlines()
    assert (
        lines.index(storage_migrate.PRE_MIGRATE_GITIGNORE_PATTERN)
        == lines.index("embeddeddolt/") + 1
    )


def test_ensure_pre_migrate_gitignore_appends_when_no_anchor_line(tmp_path):
    """No `embeddeddolt/` line to anchor on (an unusual hand-edited file) — still lands the
    pattern rather than silently doing nothing."""
    gi = _init_tracked_gitignore(tmp_path, lines=["*.lock"])

    wrote = storage_migrate._ensure_pre_migrate_gitignore(tmp_path)

    assert wrote is True
    assert gi.read_text().splitlines()[-1] == storage_migrate.PRE_MIGRATE_GITIGNORE_PATTERN


def test_ensure_pre_migrate_gitignore_is_idempotent(tmp_path):
    gi = _init_tracked_gitignore(tmp_path)
    storage_migrate._ensure_pre_migrate_gitignore(tmp_path)
    before = gi.read_text()

    wrote_again = storage_migrate._ensure_pre_migrate_gitignore(tmp_path)

    assert wrote_again is False
    assert gi.read_text() == before  # not duplicated


# ---- bh-5009a: retiring the embedded store once verification has passed ---------------------


def _furnished_hive_with_store(tmp_path):
    """A furnished hive (tracked `.beads/`) carrying a real `embeddeddolt/` directory — the shape
    bh-xsv3 was about, and the one the acceptance measures `git status` against."""
    _init_tracked_gitignore(tmp_path)
    store = tmp_path / ".beads" / "embeddeddolt"
    store.mkdir(parents=True)
    (store / "chunk.darc").write_bytes(b"x" * 2048)
    return store


def test_retire_embedded_store_removes_it_and_leaves_a_furnished_hive_clean(tmp_path):
    """The acceptance bh-xsv3 could not meet: after migrating, a furnished hive is CLEAN under
    `git status --untracked-files=all` — because the moved-aside store no longer exists at all,
    not because something committed a gitignore rule into the operator's repo."""
    _furnished_hive_with_store(tmp_path)

    kept, finding = storage_migrate._retire_embedded_store(tmp_path, keep=False)

    assert kept is None and finding is None
    assert not list((tmp_path / ".beads").glob("embeddeddolt*"))
    status = _git("status", "--porcelain", "--untracked-files=all", cwd=tmp_path)
    assert (status.stdout or "").strip() == ""


def test_retire_embedded_store_keeps_it_and_ignores_it_without_committing(tmp_path):
    """`--keep-pre-migrate` buys back the in-place rollback. Only THEN is the gitignore pattern
    needed — and it is written, never committed: a storage migration has no business authoring
    commits in the operator's repo (the bh-xsv3 behaviour bh-5009a retires)."""
    _furnished_hive_with_store(tmp_path)
    head_before = (_git("log", "-1", "--format=%H", cwd=tmp_path).stdout or "").strip()

    kept, finding = storage_migrate._retire_embedded_store(tmp_path, keep=True)

    assert kept is not None and Path(kept).is_dir()
    assert "in-place rollback" in finding and ".beads/.gitignore" in finding
    gi = (tmp_path / ".beads" / ".gitignore").read_text().splitlines()
    assert storage_migrate.PRE_MIGRATE_GITIGNORE_PATTERN in gi
    head_after = (_git("log", "-1", "--format=%H", cwd=tmp_path).stdout or "").strip()
    assert head_after == head_before  # nothing committed on the operator's behalf
    # The kept store IS covered by the pattern it just wrote, so the only thing `git status`
    # reports is that unstaged .gitignore edit — never hundreds of MB of untracked files.
    status = _git("status", "--porcelain", "--untracked-files=all", cwd=tmp_path).stdout or ""
    assert status.strip() == "M .beads/.gitignore"


def test_retire_embedded_store_is_a_noop_without_a_store(tmp_path):
    _init_tracked_gitignore(tmp_path)

    assert storage_migrate._retire_embedded_store(tmp_path, keep=False) == (None, None)


# ---- bh-aef0f: `.beads/dolt-backup*.json` (bd's own backup bookkeeping) is never tracked ----


def test_ensure_backup_json_gitignore_is_a_noop_with_no_gitignore_file(tmp_path):
    assert storage_migrate._ensure_backup_json_gitignore(tmp_path) is False


def test_ensure_backup_json_gitignore_is_a_noop_when_untracked(tmp_path):
    (tmp_path / ".beads").mkdir()
    gi = tmp_path / ".beads" / ".gitignore"
    gi.write_text("backup/\n")
    _git("init", "-q", "-b", "main", cwd=tmp_path)

    assert storage_migrate._ensure_backup_json_gitignore(tmp_path) is False
    assert gi.read_text() == "backup/\n"  # untouched


def test_ensure_backup_json_gitignore_inserts_after_the_backup_line(tmp_path):
    gi = _init_tracked_gitignore(tmp_path, lines=["dolt/", "embeddeddolt/", "backup/", "*.lock"])

    wrote = storage_migrate._ensure_backup_json_gitignore(tmp_path)

    assert wrote is True
    lines = gi.read_text().splitlines()
    assert (
        lines.index(storage_migrate.BD_BACKUP_JSON_GITIGNORE_PATTERN) == lines.index("backup/") + 1
    )


def test_ensure_backup_json_gitignore_appends_when_no_anchor_line(tmp_path):
    gi = _init_tracked_gitignore(tmp_path, lines=["*.lock"])

    wrote = storage_migrate._ensure_backup_json_gitignore(tmp_path)

    assert wrote is True
    assert gi.read_text().splitlines()[-1] == storage_migrate.BD_BACKUP_JSON_GITIGNORE_PATTERN


def test_ensure_backup_json_gitignore_is_idempotent(tmp_path):
    gi = _init_tracked_gitignore(tmp_path)
    storage_migrate._ensure_backup_json_gitignore(tmp_path)
    before = gi.read_text()

    wrote_again = storage_migrate._ensure_backup_json_gitignore(tmp_path)

    assert wrote_again is False
    assert gi.read_text() == before  # not duplicated


def test_backup_json_gitignore_pattern_matches_both_files_bd_writes():
    """`dolt-backup.json` and `dolt-backup-state.json` — both bd's own, per bh-aef0f's own
    measurement — must be matched by the ONE glob this fix adds."""
    import fnmatch

    pattern = storage_migrate.BD_BACKUP_JSON_GITIGNORE_PATTERN
    assert fnmatch.fnmatch("dolt-backup.json", pattern)
    assert fnmatch.fnmatch("dolt-backup-state.json", pattern)
    assert not fnmatch.fnmatch("dolt-backup.json.bak", pattern)


# ---- bh-ypfnu / bh-aef0f: end-to-end — a real migration leaves a furnished hive fully clean,
# ---- and bd's live backup destination at root #2, not the migrate snapshot -------------------


class _FullMigrationEngine:
    """Fakes `engine.Engine` closely enough to drive `migrate_hive` all the way to `migrated`
    without a real `bd` binary — but its `backup()` reproduces the ONE side effect bh-ypfnu is
    about, measured against a real `bd` binary: `bd backup add <dest>` + `bd backup sync`
    REPLACES bd's single destination slot and records it, ABSOLUTE, into
    `<hive>/.beads/dolt-backup.json`. Without that, this test could not measure the bug at all
    — a fake that only wrote to `dest` and never touched `.beads/dolt-backup.json` would leave
    nothing for the fix's re-point step to correct."""

    def export_jsonl(self, cwd, out_path, *, env=None):
        out_path.write_text("{}\n")
        return _ok()

    def backup(self, cwd, dest, *, actor=""):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "payload").write_bytes(b"dolt-native bytes")
        beads = Path(cwd) / ".beads"
        (beads / "dolt-backup.json").write_text(
            json.dumps({"backup_url": f"file://{dest}", "backup_name": "default"})
        )
        (beads / "dolt-backup-state.json").write_text(json.dumps({"last_sync": "now"}))
        return _ok()

    def backup_restore(self, cwd, source, *, actor=""):
        return _ok()


def _fake_bd_json_for_full_migration(issue_count: int):
    def fake_json(args, cwd):
        if args[:2] == ["config", "get"]:
            return {"value": "frn"}
        if args[:2] == ["status", "--no-activity"]:
            return {"summary": {"total_issues": issue_count}}
        if args[:2] == ["dolt", "status"]:
            return {"schema_version": "59"}
        raise AssertionError(args)

    return fake_json


def test_migrate_hive_real_run_on_a_furnished_hive_ends_fully_clean_and_bd_backup_at_root_2(
    tmp_path, monkeypatch
):
    """MEASURED, not asserted (bh-aef0f's own acceptance bar, applied to the full path a prior
    bead only unit-tested a helper in isolation for): drive a REAL `migrate_hive` run over a
    real git repo with a real, TRACKED `.beads/.gitignore`, and check the actual `git status`
    afterward, not a special-cased substring. Also proves bh-ypfnu end to end: bd's live
    backup registration ends up back at root #2 (`.beads/backup`), never left inside the
    migrate snapshot `take_backup` pointed it at mid-migration."""
    hive_dir = tmp_path / "hive"
    # `backup/` (root #2's own directory) matches a REAL bd-shipped `.beads/.gitignore`
    # (measured against bd 1.1.0's own `bd init` output) — included here so this test measures
    # ONLY what bh-ypfnu/bh-aef0f are about, not the separately-already-covered root #2 dir.
    _init_tracked_gitignore(hive_dir, lines=["dolt/", "embeddeddolt/", "proxieddb/", "backup/"])
    _write_metadata(hive_dir, dolt_mode="embedded", dolt_database="frn")
    store = hive_dir / ".beads" / "embeddeddolt" / "frn"
    store.mkdir(parents=True)
    (store / "manifest").write_bytes(b"x" * 256)
    _git("add", "-A", cwd=hive_dir)
    _git("commit", "-q", "-m", "add store", cwd=hive_dir)

    monkeypatch.setattr(registry, "hive_dir", lambda entry: hive_dir)
    monkeypatch.setattr(storage_migrate.bd_mod, "json", _fake_bd_json_for_full_migration(1))
    monkeypatch.setattr(storage_migrate, "_issue_count", lambda hd: 1)
    monkeypatch.setattr(storage_migrate, "_lock_dir", lambda cfg: tmp_path / "locks")
    monkeypatch.setattr(storage_migrate, "origin_has_dolt_data", lambda hd: False)
    monkeypatch.setattr(storage_migrate.engine, "get_engine", lambda cfg: _FullMigrationEngine())
    # `_bd()`'s own `run` — `bd init --reinit-local`/`config set` calls this fixture has no real
    # bd store to satisfy; every OTHER git call this run makes (git ls-files, inside the
    # gitignore helpers) goes through this SAME `run`, so it must still report success rather
    # than raise, which `_ok()` does either way.
    monkeypatch.setattr(storage_migrate, "run", lambda cmd, **kw: _ok())

    result = storage_migrate.migrate_hive(_entry(prefix="frn"), {}, dry_run=False, actor="test")

    assert result.status == "migrated", (result.detail, result.backup_plan)
    assert result.pre_issue_count == 1
    assert result.post_issue_count == 1

    # bh-ypfnu: the registration bd's own `backup add`/`sync` left pointed at the migrate
    # snapshot got re-pointed back to root #2 once verification passed.
    from beadhive import backup as backup_mod

    assert backup_mod.bd_backup_target(hive_dir) == backup_mod.hive_backup_dir(hive_dir)
    assert backup_mod.bd_backup_points_into_migrate_root(hive_dir, {}) is None

    # bh-aef0f: MEASURED — every `??` (untracked) line is gone, not just one special-cased
    # pattern. `dolt-backup.json`/`dolt-backup-state.json` are real, on-disk, and would
    # otherwise show up right here.
    status = _git("status", "--porcelain", "--untracked-files=all", cwd=hive_dir).stdout or ""
    untracked = [line for line in status.splitlines() if line.startswith("??")]
    assert untracked == [], status
    gitignore_lines = (hive_dir / ".beads" / ".gitignore").read_text().splitlines()
    assert storage_migrate.BD_BACKUP_JSON_GITIGNORE_PATTERN in gitignore_lines

    # Reported, never committed (bh-5009a's own retired-auto-commit posture, unchanged here).
    assert any("dolt-backup" in f for f in result.findings)
    head_message = (_git("log", "-1", "--format=%s", cwd=hive_dir).stdout or "").strip()
    assert head_message == "add store"  # no new commit landed on the operator's behalf


def test_migrate_hive_already_migrated_heals_a_dangling_backup_registration(tmp_path, monkeypatch):
    """bh-ypfnu's acceptance: re-running `bh hive migrate-storage` against an already-migrated
    hive repairs a dangling/mis-pointed registration — the nvhack/bh-infra shape on a real
    fleet, reproduced here with a registration pointed at a migrate-root path that no longer
    exists (nvhack's exact state: relocated out from under it by an earlier, unhealed
    `migrate_layout` run)."""
    hive_dir = tmp_path / "hive"
    _init_tracked_gitignore(hive_dir)
    _write_metadata(hive_dir, dolt_mode="server")

    from beadhive import backup as backup_mod

    monkeypatch.setenv("BH_HOME", str(tmp_path / "bh-home"))
    legacy_target = backup_mod.legacy_migrate_root({}) / "gone" / "dolt-native"
    (hive_dir / ".beads" / "dolt-backup.json").write_text(
        json.dumps({"backup_url": f"file://{legacy_target}", "backup_name": "default"})
    )
    assert not legacy_target.exists()  # nvhack's exact shape: the path is already gone

    monkeypatch.setattr(registry, "hive_dir", lambda entry: hive_dir)
    monkeypatch.setattr(storage_migrate.bd_mod, "json", lambda args, cwd: {"value": "h1"})

    repointed_to = []

    class _RepointEngine:
        def backup(self, cwd, dest, *, actor=""):
            repointed_to.append(Path(dest))
            (hive_dir / ".beads" / "dolt-backup.json").write_text(
                json.dumps({"backup_url": f"file://{dest}", "backup_name": "default"})
            )
            return _ok()

    monkeypatch.setattr(storage_migrate.engine, "get_engine", lambda cfg: _RepointEngine())
    monkeypatch.setattr(storage_migrate, "run", lambda cmd, **kw: _ok())

    result = storage_migrate.migrate_hive(_entry(prefix="frn"), {})

    assert result.status == "already-migrated"
    assert repointed_to == [backup_mod.hive_backup_dir(hive_dir)]
    assert backup_mod.bd_backup_target(hive_dir) == backup_mod.hive_backup_dir(hive_dir)
    assert any("healed" in f for f in result.findings), result.findings

    # bh-aef0f's own self-heal, on the very same re-run: a furnished hive migrated before this
    # fix never got bd's backup bookkeeping files covered either.
    gitignore_lines = (hive_dir / ".beads" / ".gitignore").read_text().splitlines()
    assert storage_migrate.BD_BACKUP_JSON_GITIGNORE_PATTERN in gitignore_lines
    assert any("dolt-backup" in f for f in result.findings if "healed" not in f)
