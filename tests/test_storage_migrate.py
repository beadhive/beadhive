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
import subprocess

import pytest
import typer

from beadhive import registry, storage_migrate

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

    def _boom(*a, **k):
        raise AssertionError("dry-run must not touch bd or the filesystem beyond reading")

    monkeypatch.setattr(storage_migrate, "_reinit_shared_server", _boom)
    monkeypatch.setattr(storage_migrate, "run", _boom)

    result = storage_migrate.migrate_hive(_entry(), {}, dry_run=True)

    assert result.status == "would-migrate"
    assert result.size_bytes == 1024
    assert result.target_path.endswith("dolt/scremb")
    assert store.is_dir()  # untouched


def test_migrate_hive_refuses_to_proceed_past_an_unverified_backup(tmp_path, monkeypatch):
    hive_dir = tmp_path / "hive"
    _write_metadata(hive_dir, dolt_mode="embedded")
    (hive_dir / ".beads" / "embeddeddolt").mkdir(parents=True)
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

    def flaky(entry, cfg, *, dry_run=False, actor=""):
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

    def healthy(entry, cfg, *, dry_run=False, actor=""):
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

    def per_hive(entry, cfg, *, dry_run=False, actor=""):
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
        lambda entry, cfg, *, dry_run=False, actor="": storage_migrate.HiveMigrationResult(
            hive_id=registry.hive_key(entry), hive_dir=tmp_path, status="would-migrate"
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
