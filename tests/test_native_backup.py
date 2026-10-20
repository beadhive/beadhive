"""Restore-attested native backups, verify and restore (bh-ksmt0.4).

Unit tests drive :mod:`beadhive.native_backup` with a fake engine and scratch store (mocked clock
and token, no timing, no subprocess). The integration tests at the bottom drive the real pinned
``bd``/``dolt`` against ``file://`` stores only: an embedded hive and a PRIVATE server-mode hive
(its own ``BEADS_SHARED_SERVER_DIR`` and port, never the fleet's server).
"""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from beadhive import backup, engine
from beadhive import corpus_fingerprint as cf
from beadhive import native_backup as nb
from beadhive.backup_scratch import (
    DroppedCopy,
    EmbeddedScratch,
    ScratchError,
    ScratchHandle,
    scratch_facts,
    scratch_name,
)
from beadhive.dolt_remote import RegistryEntry, RegistryKind, RegistrySnapshot
from beadhive.run import run
from harness.beads import embedded_env, skip_if_no_bd
from harness.world import reap_dolt_server

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
SLUG = "local/org/repo"
TOOLS = {"bh": "0.27.0", "bd": "1.3.0", "dolt": "2.3.5"}
TABLES = ("labels",)


def clock():
    return NOW


def token():
    return "abc123"


def snapshot(slot="file:///prior-bk"):
    backups = (RegistryEntry(RegistryKind.BACKUP, "default", slot),) if slot else ()
    return RegistrySnapshot(
        remotes=(RegistryEntry(RegistryKind.REMOTE, "origin", "file:///remote", True),),
        backups=backups,
    )


def facts(**over):
    base = dict(
        project_id="p1",
        prefix="bh",
        schema_version=66,
        main_head="h1",
        branches={"main": "h1"},
        issue_rows={"bh-1": {"id": "bh-1", "t": "a"}, "bh-2": {"id": "bh-2", "t": "b"}},
        tables={"labels": []},
    )
    base.update(over)
    return cf.CorpusFacts(**base)


class FakeEngine:
    """Just enough engine for a snapshot: a mutable registry, a native backup writer and an
    exporter. ``fail`` names the step that should fail."""

    def __init__(self, slot="file:///prior-bk", fail=""):
        self.snap = snapshot(slot)
        self.fail = fail
        self.calls: list[str] = []

    def read_registry(self, cwd):
        return SimpleNamespace(ok=True, error="", snapshot=self.snap)

    def restore_registry(self, cwd, snap, *, extra_env=None):
        self.calls.append("restore_registry")
        late = self.fail == "restore_registry_late" and "backup_restore" in self.calls
        if self.fail == "restore_registry" or late:
            return SimpleNamespace(ok=False, error="engine refused")
        self.snap = snap
        return SimpleNamespace(ok=True, error="")

    def add_backup(self, cwd, url, *, replace=False):
        self.calls.append("add_backup")
        if self.fail == "add_backup":
            return SimpleNamespace(returncode=1, stderr="nope", stdout="")
        rest = tuple(b for b in self.snap.backups if b.name != "default")
        self.snap = RegistrySnapshot(
            remotes=self.snap.remotes,
            backups=(*rest, RegistryEntry(RegistryKind.BACKUP, "default", url)),
        )
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    def backup(self, cwd, dest, *, actor=""):
        self.calls.append("backup")
        if self.fail == "backup":
            return SimpleNamespace(returncode=1, stderr="disk full", stdout="")
        path = Path(str(dest).removeprefix("file://"))
        path.mkdir(parents=True)
        (path / "chunk").write_bytes(b"dolt-bytes")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    def invoke(self, args, *, cwd=None, capture=False, timeout=0, **kw):
        if args[0] == "--version":
            return SimpleNamespace(returncode=0, stdout="bd version 1.3.0 (x)", stderr="")
        assert args[0] == "export"
        out = Path(args[args.index("-o") + 1])
        out.write_text(
            "\n".join(json.dumps({"_type": "issue", "id": i}) for i in ("bh-1", "bh-2")) + "\n"
        )
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    def backup_restore(self, cwd, source, *, actor=""):
        self.calls.append("backup_restore")
        return SimpleNamespace(returncode=0, stderr="", stdout="")


class FakeScratch:
    def __init__(self, scratch_facts=None, fail_drop=False, fail_restore=False):
        self.facts = scratch_facts or facts()
        self.fail_drop = fail_drop
        self.fail_restore = fail_restore
        self.dropped: list[str] = []

    def restore(self, url, name):
        if self.fail_restore:
            raise ScratchError("cannot restore")
        return ScratchHandle(name)

    def drop(self, handle):
        if self.fail_drop:
            raise ScratchError("drop failed")
        self.dropped.append(handle.name)
        return DroppedCopy(handle.name, True, "/srv/.dolt_dropped_databases/x", True, 10, "kept")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_HOME", str(tmp_path / "bhhome"))
    hive = tmp_path / "hive"
    (hive / ".beads").mkdir(parents=True)
    state = SimpleNamespace(source=facts(), scratch=FakeScratch(), records=2)
    monkeypatch.setattr(nb, "source_facts", lambda eng, d, tables: state.source)
    monkeypatch.setattr(nb, "record_counts", lambda q, qu, database=None: state.records)
    monkeypatch.setattr(
        nb, "scratch_facts", lambda store, handle, tables: (state.scratch.facts, state.records)
    )
    return SimpleNamespace(hive=hive, state=state, tmp=tmp_path)


def snap(env, eng=None, **kw):
    return nb.take_snapshot(
        {},
        env.hive,
        slug=SLUG,
        prefix="bh",
        engine=eng or FakeEngine(),
        scratch=env.state.scratch,
        clock=clock,
        token=token,
        tables=TABLES,
        tool_versions=lambda: dict(TOOLS),
        **kw,
    )


# ---- the snapshot ----------------------------------------------------------------------------


def test_success_writes_every_artifact_and_a_fit_attested_manifest(env):
    eng = FakeEngine()
    res = snap(env, eng)
    assert res.ok, res.errors
    d = res.set_dir
    assert d.parent == backup.migrate_hive_root(SLUG, {})
    assert {p.name for p in d.iterdir()} == {
        "issues.jsonl",
        "dolt-native",
        "registry.json",
        "manifest.json",
    }
    m = nb.load_manifest(d)
    assert nb.validate_manifest(m) == []
    assert m["verified"] is True
    assert m["schema_version"] == 66 == m["fingerprint"]["components"]["schema_version"]
    assert m["tool_versions"] == TOOLS and m["bh_version"] == "0.27.0"
    assert m["fingerprint"]["tier"] == "strong"
    assert m["attestation"]["matched"] and m["attestation"]["scratch_dropped"]
    assert set(m["checksums"]) == {"issues.jsonl", "dolt-native", "registry.json"}
    assert m["prior_backup"] == {"registered": True, "name": "default", "url": "file:///prior-bk"}
    assert res.dropped.left_copy and env.state.scratch.dropped == [res.scratch_database]
    assert (d / "registry.json").stat().st_mode & 0o777 == 0o600
    # the slot is exactly as it was, and the snapshot used its own unique destination
    assert eng.snap == snapshot()
    assert res.outcome().phase_reached == "attest-backup" and res.outcome().ok


def test_slot_with_no_prior_registration_is_removed_again(env):
    eng = FakeEngine(slot="")
    res = snap(env, eng)
    assert res.ok and eng.snap.backup("default") is None
    assert nb.load_manifest(res.set_dir)["prior_backup"]["registered"] is False


@pytest.mark.parametrize("step", ["add_backup", "backup"])
def test_failed_native_backup_restores_the_slot_and_leaves_no_set(env, step):
    eng = FakeEngine(fail=step)
    res = snap(env, eng)
    assert not res.ok and res.slot.restored
    assert eng.snap == snapshot()
    assert res.set_dir is None
    assert not list(backup.migrate_hive_root(SLUG, {}).glob("*"))


def test_unrestorable_slot_is_a_failure_with_the_saved_registration_named(env):
    eng = FakeEngine(fail="restore_registry")
    res = snap(env, eng)
    assert not res.ok and not res.slot.restored
    assert any("slot NOT restored" in e for e in res.errors)
    assert any("registry.json" in e for e in res.errors)
    assert res.set_dir is not None and (res.set_dir / "registry.json").is_file()


def test_slot_is_restored_even_when_the_backup_raises(env):
    eng = FakeEngine()

    def boom(*a, **k):
        raise OSError("disk exploded")

    eng.backup = boom
    with pytest.raises(OSError):
        snap(env, eng)
    assert eng.snap == snapshot()  # the finally ran


def test_mismatching_restored_fingerprint_is_not_a_success(env):
    env.state.scratch = FakeScratch(facts(main_head="other", branches={"main": "other"}))
    res = snap(env)
    assert not res.ok and not res.attested
    assert any("main_head" in e for e in res.errors)
    m = nb.load_manifest(res.set_dir)
    assert m["verified"] is False and nb.validate_manifest(m) == []
    assert res.scratch_database in env.state.scratch.dropped  # scratch dropped regardless


def test_restore_failure_still_reports_failure_and_never_verified(env):
    env.state.scratch = FakeScratch(fail_restore=True)
    res = snap(env)
    assert not res.ok and nb.load_manifest(res.set_dir)["verified"] is False


def test_scratch_that_cannot_be_dropped_fails_the_snapshot(env):
    env.state.scratch = FakeScratch(fail_drop=True)
    res = snap(env)
    assert not res.ok and any("NOT dropped" in e for e in res.errors)


def test_source_that_moves_during_the_snapshot_is_refused(env, monkeypatch):
    calls = iter([facts(), facts(main_head="moved", branches={"main": "moved"})])
    monkeypatch.setattr(nb, "source_facts", lambda eng, d, tables: next(calls))
    res = snap(env)
    assert not res.ok and any("changed during the snapshot" in e for e in res.errors)


def test_jsonl_that_disagrees_with_the_store_is_refused(env):
    env.state.records = 3
    res = snap(env)
    assert not res.ok and any("inconsistent floor" in e for e in res.errors)
    assert res.set_dir is None


def test_unreadable_registry_refuses_before_writing(env):
    class Bad(FakeEngine):
        def read_registry(self, cwd):
            return SimpleNamespace(ok=False, error="timeout", snapshot=None)

    res = snap(env, Bad())
    assert not res.ok and res.set_dir is None and "timeout" in res.errors[0]


def test_dry_run_writes_nothing(env):
    res = snap(env, dry_run=True)
    assert res.ok and res.set_dir is None
    assert not backup.migrate_hive_root(SLUG, {}).exists()


def test_a_manifest_that_cannot_be_written_is_not_a_success(env, monkeypatch):
    def fail(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(nb, "write_manifest_strict", fail)
    res = snap(env)
    assert not res.ok and any("manifest" in e for e in res.errors)


def test_unknown_tool_version_is_a_failure_not_a_best_effort(env):
    res = nb.take_snapshot(
        {},
        env.hive,
        slug=SLUG,
        prefix="bh",
        engine=FakeEngine(),
        scratch=env.state.scratch,
        clock=clock,
        token=token,
        tables=TABLES,
        tool_versions=lambda: {"bh": "0.27.0", "bd": "", "dolt": "2.3.5"},
    )
    assert not res.ok and any("tool version for 'bd'" in e for e in res.errors)


# ---- manifest fitness ------------------------------------------------------------------------


@pytest.fixture
def good(env):
    res = snap(env)
    assert res.ok
    return nb.load_manifest(res.set_dir)


@pytest.mark.parametrize(
    "field",
    ["schema_version", "tool_versions", "fingerprint", "checksums", "txn", "jsonl", "artifacts"],
)
def test_every_required_field_is_required(good, field):
    bad = {k: v for k, v in good.items() if k != field}
    assert any(field in p for p in nb.validate_manifest(bad))


def test_placeholder_schema_version_and_hollow_attestation_fail(good):
    assert nb.validate_manifest({**good, "schema_version": 1}) != []  # != the fingerprint's
    assert nb.validate_manifest({**good, "schema_version": True}) != []
    hollow = {**good, "attestation": {"matched": False}}
    assert any("without a matching" in p for p in nb.validate_manifest(hollow))
    forged = {**good, "attestation": {**good["attestation"], "restored_digest": "0" * 64}}
    assert any("attestation digest" in p for p in nb.validate_manifest(forged))


def test_tampered_fingerprint_is_detected(good):
    bad = json.loads(json.dumps(good))
    bad["fingerprint"]["components"]["issue_count"] = 99
    assert any("fingerprint is invalid" in p for p in nb.validate_manifest(bad))


# ---- verify ----------------------------------------------------------------------------------


def test_verify_passes_then_catches_each_kind_of_damage(env):
    d = snap(env).set_dir
    assert nb.verify_set(d).ok
    (d / "issues.jsonl").write_text('{"_type":"issue","id":"x"}\n')
    rep = nb.verify_set(d)
    assert not rep.ok and any("checksum of 'issues.jsonl'" in p for p in rep.problems)


def test_verify_catches_a_modified_native_backup_and_a_missing_artifact(env):
    d = snap(env).set_dir
    (d / "dolt-native" / "chunk").write_bytes(b"corrupt")
    assert any("dolt-native" in p for p in nb.verify_set(d).problems)
    (d / "registry.json").unlink()
    assert any("registry.json" in p and "missing" in p for p in nb.verify_set(d).problems)


def test_verify_without_a_manifest_is_unverified_not_unknown(env):
    d = snap(env).set_dir
    (d / "manifest.json").unlink()
    rep = nb.verify_set(d)
    assert not rep.ok and "no manifest" in rep.problems[0]


def test_verify_restore_test_compares_against_the_manifest(env):
    d = snap(env).set_dir
    ok = nb.verify_set(
        d,
        cfg={},
        hive_dir=env.hive,
        engine=FakeEngine(),
        scratch=env.state.scratch,
        restore_test=True,
        token=token,
    )
    assert ok.ok and ok.attested and ok.dropped is not None
    env.state.scratch = FakeScratch(facts(prefix="drifted"))
    bad = nb.verify_set(
        d,
        cfg={},
        hive_dir=env.hive,
        engine=FakeEngine(),
        scratch=env.state.scratch,
        restore_test=True,
        token=token,
    )
    assert not bad.ok and bad.attested is False and any("prefix" in p for p in bad.problems)


# ---- list ------------------------------------------------------------------------------------


def test_list_reports_verification_state_and_legacy_sets(env):
    good_dir = snap(env).set_dir
    legacy = backup.migrate_hive_root(SLUG, {}) / "2026-01-01T000000Z"
    legacy.mkdir()
    (legacy / "issues.jsonl").write_text("{}\n")
    broken = backup.migrate_hive_root(SLUG, {}) / "2026-02-01T000000Z"
    broken.mkdir()
    nb.write_manifest_strict(broken, {"kind": nb.KIND, "verified": True})
    by_name = {s.name: s for s in nb.list_sets({})}
    assert by_name[good_dir.name].verified is True
    assert by_name["2026-01-01T000000Z"].verified is None
    assert by_name["2026-02-01T000000Z"].verified is False
    assert by_name["2026-02-01T000000Z"].problems
    assert nb.list_sets({}, hive="other/org/repo") == []


# ---- restore ---------------------------------------------------------------------------------


def test_restore_dry_run_never_mutates_and_confirm_must_name_the_hive(env):
    d = snap(env).set_dir
    eng = FakeEngine()
    plan = nb.restore_set({}, env.hive, d, slug=SLUG, engine=eng, dry_run=True)
    assert plan.ok and "OVERWRITES the live database" in " ".join(plan.actions)
    assert "backup_restore" not in eng.calls
    none = nb.restore_set({}, env.hive, d, slug=SLUG, engine=eng, dry_run=False)
    wrong = nb.restore_set({}, env.hive, d, slug=SLUG, engine=eng, dry_run=False, confirm="yes")
    assert not none.ok and not wrong.ok and "backup_restore" not in eng.calls
    assert "value did not match" in wrong.problems[0]


def test_restore_refuses_a_cross_hive_set_and_an_unverified_set(env):
    d = snap(env).set_dir
    other = nb.restore_set(
        {}, env.hive, d, slug="local/org/other", engine=FakeEngine(), dry_run=True
    )
    assert not other.ok and "cross-hive" in other.problems[0]
    (d / "issues.jsonl").write_text("tampered\n")
    bad = nb.restore_set({}, env.hive, d, slug=SLUG, engine=FakeEngine(), dry_run=True)
    assert not bad.ok and "not restorable" in bad.problems[0]


def test_restore_refuses_a_different_project(env):
    d = snap(env).set_dir
    env.state.source = facts(project_id="someone-else")
    out = nb.restore_set({}, env.hive, d, slug=SLUG, engine=FakeEngine(), dry_run=True)
    assert not out.ok and "PROJECT IDENTITY MISMATCH" in out.problems[0]


def test_confirmed_restore_reapplies_the_registry_and_fingerprints_the_result(env):
    d = snap(env).set_dir
    eng = FakeEngine()
    out = nb.restore_set(
        {}, env.hive, d, slug=SLUG, engine=eng, dry_run=False, confirm=SLUG, clock=clock
    )
    assert out.ok, out.problems
    assert eng.calls[-2:] == ["backup_restore", "restore_registry"]
    assert out.registry_restored and out.fingerprint_matched
    assert list(d.parent.glob("registry-before-restore-*.json"))


def test_restore_that_changes_the_data_is_reported_as_a_mismatch(env):
    d = snap(env).set_dir
    env.state.source = facts(main_head="after", branches={"main": "after"})
    # first call (identity check) and second (post-restore) both see the drifted store
    out = nb.restore_set(
        {}, env.hive, d, slug=SLUG, engine=FakeEngine(), dry_run=False, confirm=SLUG, clock=clock
    )
    assert not out.ok and out.fingerprint_matched is False


def test_failed_registry_reapply_after_restore_is_a_failure(env):
    d = snap(env).set_dir
    out = nb.restore_set(
        {},
        env.hive,
        d,
        slug=SLUG,
        engine=FakeEngine(fail="restore_registry_late"),
        dry_run=False,
        confirm=SLUG,
        clock=clock,
    )
    assert not out.ok and out.registry_restored is False
    assert any("registry NOT restored" in p for p in out.problems)


def test_restore_takes_a_pre_restore_snapshot_and_records_it(env):
    d = snap(env).set_dir
    eng = FakeEngine()
    plan = nb.restore_set({}, env.hive, d, slug=SLUG, engine=eng, dry_run=True, clock=clock)
    assert plan.ok and plan.safety_set
    assert any("pre-restore snapshot of the live store will be taken at" in a for a in plan.actions)
    out = nb.restore_set(
        {}, env.hive, d, slug=SLUG, engine=eng, dry_run=False, confirm=SLUG, clock=clock
    )
    assert out.ok, out.problems
    safety = Path(out.safety_set)
    assert safety.is_dir() and safety != d
    m = nb.load_manifest(safety)
    assert m["purpose"] == "pre-restore" and m["verified"] is False
    assert nb.validate_manifest(m) == []
    assert eng.calls.index("backup") < eng.calls.index("backup_restore")
    # a safety set is itself restorable (structurally verified, not attested)
    assert nb.verify_set(safety).ok


def test_failed_pre_restore_snapshot_refuses_with_zero_mutation(env):
    d = snap(env).set_dir
    eng = FakeEngine(fail="backup")
    out = nb.restore_set(
        {}, env.hive, d, slug=SLUG, engine=eng, dry_run=False, confirm=SLUG, clock=clock
    )
    assert not out.ok and "pre-restore snapshot" in out.problems[0]
    assert "backup_restore" not in eng.calls
    assert eng.snap == snapshot() and out.safety_set == ""
    assert not list(d.parent.glob("registry-before-restore-*.json"))


def test_unreadable_live_store_needs_the_explicit_flag(env, monkeypatch):
    d = snap(env).set_dir

    def unreadable(*a, **k):
        raise RuntimeError("server down")

    monkeypatch.setattr(nb, "source_facts", unreadable)
    eng = FakeEngine()
    refused = nb.restore_set(
        {}, env.hive, d, slug=SLUG, engine=eng, dry_run=False, confirm=SLUG, clock=clock
    )
    assert not refused.ok and "--no-pre-restore-snapshot" in refused.problems[0]
    assert "backup_restore" not in eng.calls
    plan = nb.restore_set(
        {}, env.hive, d, slug=SLUG, engine=eng, dry_run=True, skip_pre_restore_snapshot=True
    )
    assert plan.ok and any("NO pre-restore snapshot" in a for a in plan.actions)
    out = nb.restore_set(
        {},
        env.hive,
        d,
        slug=SLUG,
        engine=eng,
        dry_run=False,
        confirm=SLUG,
        clock=clock,
        skip_pre_restore_snapshot=True,
    )
    assert "backup_restore" in eng.calls and out.safety_set == ""


def test_the_flag_is_refused_when_the_live_store_is_readable(env):
    d = snap(env).set_dir
    eng = FakeEngine()
    out = nb.restore_set(
        {},
        env.hive,
        d,
        slug=SLUG,
        engine=eng,
        dry_run=False,
        confirm=SLUG,
        skip_pre_restore_snapshot=True,
    )
    assert not out.ok and "mandatory" in out.problems[0] and "backup_restore" not in eng.calls


# ---- the bd bookkeeping files ----------------------------------------------------------------


def test_slot_bookkeeping_files_come_back_byte_for_byte(tmp_path):
    hive = tmp_path / "h"
    (hive / ".beads").mkdir(parents=True)
    state = hive / ".beads" / "dolt-backup-state.json"
    state.write_bytes(b'{"last_sync": "old"}')
    eng = FakeEngine()
    rep = nb.SlotReport()
    with nb.preserved_backup_slot(eng, hive, eng.snap, rep):
        state.write_bytes(b'{"last_sync": "new"}')
        (hive / ".beads" / "dolt-backup.json").write_text("{}")
    assert rep.restored and state.read_bytes() == b'{"last_sync": "old"}'
    assert not (hive / ".beads" / "dolt-backup.json").exists()


def test_scratch_names_are_unique_and_safe():
    a = scratch_name("bh-infra", "x1")
    assert a == "bh_infra_scr_x1" and scratch_name("bh-infra", "x2") != a
    assert len(scratch_name("p" * 80, "x1")) <= 64


def test_embedded_scratch_refuses_to_overwrite(tmp_path):
    root = tmp_path / "scr"
    (root / "taken").mkdir(parents=True)
    with pytest.raises(ScratchError, match="already exists"):
        EmbeddedScratch(root).restore("file:///nowhere", "taken")


# ---- real bd + dolt, file:// only ------------------------------------------------------------


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=str(path), check=True, capture=True)
    return path


def _bd(hive, *args, env=None):
    return run(["bd", "-C", str(hive), *args], check=True, capture=True, env=env, timeout=120)


@pytest.fixture
def embedded(monkeypatch, tmp_path):
    for key in set(os.environ) - set(embedded_env()):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BD_NON_INTERACTIVE", "1")
    monkeypatch.setenv("BH_HOME", str(tmp_path / "bhhome"))


def _live_digest(eng, hive):
    from beadhive.backup_scratch import source_facts

    return cf.compute(source_facts(eng, hive, cf.DEFAULT_TABLES), cf.TIER_STRONG).digest


@pytest.mark.integration
@skip_if_no_bd
def test_real_embedded_round_trip_backup_scratch_restore_fingerprint_and_slot(tmp_path, embedded):
    hive = _git_repo(tmp_path / "hive")
    run(["bd", "init", "--prefix", "nb", "--quiet"], cwd=str(hive), check=True, capture=True)
    _bd(hive, "create", "first", "-p", "2")
    _bd(hive, "create", "second", "-p", "1")
    eng = engine.BdEngine()
    prior = f"file://{tmp_path / 'prior-backup'}"
    assert eng.add_backup(hive, prior).returncode == 0
    assert eng.backup(hive, prior).returncode == 0  # a populated prior destination
    before = eng.read_registry(hive).snapshot
    files = {n: (hive / ".beads" / n).read_bytes() for n in nb._SLOT_FILES}

    res = nb.take_snapshot({}, hive, slug=SLUG, prefix="nb", engine=eng)
    assert res.ok, res.errors
    m = nb.load_manifest(res.set_dir)
    assert m["verified"] and m["dolt_mode"] == "embedded"
    assert m["schema_version"] > 1  # the REAL migration count, not the envelope constant
    assert m["attestation"]["restored_digest"] == m["fingerprint"]["digest"]
    assert m["jsonl"]["issue_records"] == m["jsonl"]["expected_records"] == 2
    assert not res.dropped.left_copy  # embedded scratch leaves no soft-delete copy
    assert not list((res.set_dir).glob(".scratch-*"))

    # the single slot: same name, same address, same bookkeeping, proven by read-back
    after = eng.read_registry(hive).snapshot
    assert before.diff(after).empty and after.backup("default").url == prior
    assert files == {n: (hive / ".beads" / n).read_bytes() for n in nb._SLOT_FILES}

    # list and verify (structural, then a fresh restore test)
    assert [s.name for s in nb.list_sets({}, hive=SLUG)] == [res.set_dir.name]
    assert nb.verify_set(res.set_dir).ok
    deep = nb.verify_set(res.set_dir, cfg={}, hive_dir=hive, engine=eng, restore_test=True)
    assert deep.ok and deep.attested

    # restore: change the store, then put the snapshot back (confirmed, registry re-applied)
    snap_digest = m["fingerprint"]["digest"]
    _bd(hive, "create", "third, added after the snapshot", "-p", "3")
    pre_restore_digest = _live_digest(eng, hive)
    assert pre_restore_digest != snap_digest
    plan = nb.restore_set({}, hive, res.set_dir, slug=SLUG, engine=eng, dry_run=True)
    assert plan.ok and _live_digest(eng, hive) != snap_digest  # dry run changed nothing
    refused = nb.restore_set({}, hive, res.set_dir, slug=SLUG, engine=eng, dry_run=False)
    assert not refused.ok and _live_digest(eng, hive) != snap_digest
    done = nb.restore_set({}, hive, res.set_dir, slug=SLUG, engine=eng, dry_run=False, confirm=SLUG)
    assert done.ok, done.problems
    assert done.fingerprint_matched and _live_digest(eng, hive) == snap_digest
    # the replaced store was saved first, and restoring that safety set brings it back
    assert done.safety_set and Path(done.safety_set).is_dir()
    assert pre_restore_digest != snap_digest
    back = nb.restore_set(
        {}, hive, Path(done.safety_set), slug=SLUG, engine=eng, dry_run=False, confirm=SLUG
    )
    assert back.ok, back.problems
    assert _live_digest(eng, hive) == pre_restore_digest
    assert before.diff(eng.read_registry(hive).snapshot).empty


@pytest.mark.integration
@skip_if_no_bd
def test_real_snapshot_with_no_prior_slot_leaves_the_registry_exactly_as_found(tmp_path, embedded):
    hive = _git_repo(tmp_path / "hive")
    run(["bd", "init", "--prefix", "nc", "--quiet"], cwd=str(hive), check=True, capture=True)
    _bd(hive, "create", "something to back up", "-p", "2")
    eng = engine.BdEngine()
    # a remote registered elsewhere must also survive untouched
    assert eng.add_remote(hive, "hq-private", f"file://{tmp_path / 'remote'}").returncode == 0
    before = eng.read_registry(hive).snapshot
    assert before.backup("default") is None
    res = nb.take_snapshot({}, hive, slug=SLUG, prefix="nc", engine=eng)
    assert res.ok, res.errors
    after = eng.read_registry(hive).snapshot
    assert before.diff(after).empty and after.backup("default") is None
    assert res.slot.restored


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def private_server(tmp_path, monkeypatch):
    """A PRIVATE bd shared server (own dir, own port): never the fleet's."""
    shared = tmp_path / "shared-server"
    monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(shared))
    monkeypatch.setenv("BEADS_DOLT_SERVER_PORT", str(_free_port()))
    monkeypatch.setenv("BH_HOME", str(tmp_path / "bhhome"))
    monkeypatch.setenv("BD_NON_INTERACTIVE", "1")
    yield shared
    reap_dolt_server(shared)


@pytest.mark.integration
@pytest.mark.dolt_server
@skip_if_no_bd
def test_real_server_mode_round_trip_reports_the_dropped_database_copy(tmp_path, private_server):
    hive = _git_repo(tmp_path / "hive")
    run(
        ["bd", "init", "--shared-server", "--prefix", "ns", "--non-interactive", "--quiet"],
        cwd=str(hive),
        check=True,
        capture=True,
        timeout=120,
    )
    _bd(hive, "create", "server side", "-p", "2")
    eng = engine.BdEngine()
    prior = f"file://{tmp_path / 'prior-backup'}"
    assert eng.add_backup(hive, prior).returncode == 0
    before = eng.read_registry(hive).snapshot

    res = nb.take_snapshot({}, hive, slug=SLUG, prefix="ns", engine=eng)
    assert res.ok, res.errors
    m = nb.load_manifest(res.set_dir)
    assert m["dolt_mode"] == "server" and m["verified"]
    assert m["attestation"]["restored_digest"] == m["fingerprint"]["digest"]
    # bh-2hvmd: a dropped database leaves a full copy, which we REPORT (and did not purge)
    assert res.dropped.left_copy and res.dropped.located and res.dropped.size_bytes > 0
    assert Path(res.dropped.location).is_dir()
    assert res.scratch_database in res.dropped.location
    live_dbs = {
        r["n"]
        for r in eng.query_rows(hive, "SELECT schema_name AS n FROM information_schema.schemata")
    }
    assert res.scratch_database not in live_dbs
    assert before.diff(eng.read_registry(hive).snapshot).empty
    assert nb.verify_set(res.set_dir, cfg={}, hive_dir=hive, engine=eng, restore_test=True).ok


@pytest.mark.integration
@pytest.mark.dolt_server
@skip_if_no_bd
def test_server_source_and_embedded_restore_of_its_own_backup_fingerprint_identically(
    tmp_path, private_server
):
    """bh-m4ups: the SAME database read through the server (``bd sql --json``) and through an
    embedded restore of its own native backup (``dolt sql -r json``) has equal fingerprints,
    although the two render DATETIME and JSON columns differently."""
    from beadhive.backup_scratch import source_facts

    hive = _git_repo(tmp_path / "hive")
    run(
        ["bd", "init", "--shared-server", "--prefix", "nm", "--non-interactive", "--quiet"],
        cwd=str(hive),
        check=True,
        capture=True,
        timeout=120,
    )
    out = _bd(
        hive,
        "create",
        'ünï 日本 "q" \\ <b>&',
        "-p",
        "2",
        "--json",
        "--metadata",
        '{"b":[1,2.5,"ü"],"a":{"z":1,"y":null}}',
    )
    first = json.loads(out.stdout)["id"]
    second = json.loads(_bd(hive, "create", "second", "-p", "1", "-l", "foo", "--json").stdout)[
        "id"
    ]
    _bd(hive, "comments", "add", first, "hi ü 日本")
    _bd(hive, "dep", "add", second, first)
    _bd(hive, "update", first, "--estimate", "90", "--due", "2026-12-01")
    eng = engine.BdEngine()
    dest = f"file://{tmp_path / 'native'}"
    assert eng.add_backup(hive, dest).returncode == 0
    assert eng.backup(hive, dest).returncode == 0

    tables = cf.DEFAULT_TABLES
    server = source_facts(eng, hive, tables)
    store = EmbeddedScratch(tmp_path / "scratch")
    handle = store.restore(dest, "restored_nm")
    embedded, _records = scratch_facts(store, handle, tables)

    # the two engines really do render differently -- the premise of the bug
    row_s = server.issue_rows[first]
    row_e = embedded.issue_rows[first]
    assert row_s["created_at"].endswith("Z") and "T" in row_s["created_at"]
    assert "T" not in row_e["created_at"]
    assert "started_at" in row_s and "started_at" not in row_e  # NULLs: key present vs omitted
    assert isinstance(row_e["metadata"], dict) and isinstance(row_s["metadata"], str)
    assert row_s["title"] == row_e["title"]  # unicode and escapes survive both paths

    for tier in (cf.TIER_STRONG, cf.TIER_CORPUS):
        a = cf.compute(server, tier, tables=tables)
        b = cf.compute(embedded, tier, tables=tables)
        assert a.digest == b.digest, cf.compare(a, b).detail
        assert cf.compare(a, b).equal
    # the Dolt-native cross-check is present on both sides and equal
    a = cf.compute(server, cf.TIER_STRONG, tables=tables)
    b = cf.compute(embedded, cf.TIER_STRONG, tables=tables)
    assert a.native and a.native == b.native and a.native["db"]
    assert set(a.native["tables"]) >= {"issues", *tables}

    # the server reader (bd sql) agrees with the embedded scratch reader too
    via_bd_sql = cf.fingerprint(cf.BdSqlCorpusReader(), hive, cf.TIER_STRONG, tables=tables)
    assert via_bd_sql.digest == b.digest and via_bd_sql.native == b.native

    # and the real snapshot attests a server-mode source by an EMBEDDED restore
    res = nb.take_snapshot(
        {}, hive, slug=SLUG, prefix="nm", engine=eng, scratch=EmbeddedScratch(tmp_path / "s2")
    )
    assert res.ok, res.errors
    m = nb.load_manifest(res.set_dir)
    assert m["dolt_mode"] == "server" and m["verified"]
    assert m["attestation"]["restored_digest"] == m["fingerprint"]["digest"]
    assert m["fingerprint"]["schema"] == "bh.corpus-fingerprint/2"


def test_cli_verbs_are_catalogued_and_confirm_is_typed():
    from typer.main import get_command

    from beadhive.cli import app

    group = get_command(app).commands["backup"]
    assert {"snapshot", "list", "verify", "restore"} <= set(group.commands)
    confirm = next(p for p in group.commands["restore"].params if p.name == "confirm")
    assert confirm.type.name == "text"  # a typed hive name, not a bare flag


# ---- the operator verbs ----------------------------------------------------------------------


@pytest.fixture
def cli(env, monkeypatch, capsys):
    import contextlib

    import typer

    from beadhive import backup_cli, migration_lock
    from beadhive import engine as engine_mod

    cfg = {"managed_repos": []}
    eng = FakeEngine()
    monkeypatch.setattr(engine_mod, "get_engine", lambda c=None: eng)
    monkeypatch.setattr(backup_cli, "_target", lambda c, hive, hq: (env.hive, SLUG, "bh"))
    monkeypatch.setattr(migration_lock, "require_lease", lambda *a, **k: "host")
    monkeypatch.setattr(
        migration_lock, "hive_migration_lock", lambda *a, **k: contextlib.nullcontext()
    )
    monkeypatch.setattr(nb, "detect_tool_versions", lambda engine=None: dict(TOOLS))
    monkeypatch.setattr(nb, "make_scratch", lambda *a, **k: env.state.scratch)

    def run_verb(verb, *args):
        """Call the verb's implementation; returns ``(exit_code, stdout + stderr)``."""
        capsys.readouterr()
        code = 0
        try:
            getattr(backup_cli, f"impl_{verb}")(cfg, *args)
        except typer.Exit as exc:
            code = exc.exit_code
        out = capsys.readouterr()
        return code, out.out + out.err

    return SimpleNamespace(run=run_verb, eng=eng)


def test_cli_snapshot_list_verify_and_a_typed_restore(cli, env):
    code, out = cli.run("snapshot", "", False, True, False)
    assert code == 0 and "would snapshot" in out
    code, out = cli.run("snapshot", "", False, False, True)
    assert code == 0, out
    payload = json.loads(out)
    assert payload["ok"] and payload["slot_restored"] and payload["dropped_copy"]["left_copy"]

    listing = json.loads(cli.run("list", "", True)[1])
    assert [s["verified"] for s in listing["sets"]] == [True]

    assert cli.run("verify", None, "", False, False, False)[0] == 0
    code, out = cli.run("restore", None, "", False, True, "", False)
    assert code == 0 and "preview only" in out and "backup_restore" not in cli.eng.calls

    code, out = cli.run("restore", None, "", False, False, "", False)
    assert code == 1 and "without --confirm" in out
    code, out = cli.run("restore", None, "", False, False, "nope", False)
    assert code == 1 and "backup_restore" not in cli.eng.calls

    code, out = cli.run("restore", None, "", False, False, SLUG, False)
    assert code == 0, out
    assert "backup_restore" in cli.eng.calls


def test_cli_verify_fails_closed_on_a_damaged_set(cli, env):
    d = Path(json.loads(cli.run("snapshot", "", False, False, True)[1])["set"])
    (d / "issues.jsonl").write_text("{}\n")
    code, out = cli.run("verify", str(d), "", False, False, False)
    assert code == 1 and "NOT verified" in out
    assert cli.run("restore", str(d), "", False, True, "", False)[0] == 1


def test_cli_failed_snapshot_exits_nonzero_and_says_why(cli, env):
    env.state.scratch = FakeScratch(facts(prefix="drifted"))
    code, out = cli.run("snapshot", "", False, False, False)
    assert code == 1 and "FAILED" in out and "prefix" in out


def test_restore_confirm_option_is_typed_not_a_bare_flag():
    from typer.main import get_command

    from beadhive.cli import app

    restore = get_command(app).commands["backup"].commands["restore"]
    confirm = next(p for p in restore.params if p.name == "confirm")
    assert confirm.type.name == "text" and not confirm.is_flag
