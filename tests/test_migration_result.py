"""Shared per-hive result (bh-ksmt0.2)."""

from __future__ import annotations

from beadhive import migration_ledger as ml
from beadhive import migration_result as mr
from beadhive.storage_migrate import HiveMigrationResult


def test_from_replay_reports_phase_reached_state_and_evidence_paths(tmp_path):
    led = ml.PhaseLedger(tmp_path / "l.jsonl", "t1", actor="a", host="h")
    for phase in ("assess", "quiesce", "snapshot"):
        led.append(phase, ml.RUNNING)
        led.append(phase, ml.DONE, evidence={"backup_path": f"/b/{phase}", "n": 1})
    led.append("attest-backup", ml.RUNNING)
    out = mr.from_replay("github/o/h", led.state())
    assert (out.hive, out.txn, out.phase_reached) == ("github/o/h", "t1", "snapshot")
    assert out.state == "snapshotted" and out.ok
    assert out.evidence_paths == ("/b/assess", "/b/quiesce", "/b/snapshot")
    assert out.to_dict()["phase_reached"] == "snapshot"


def test_needs_operator_is_not_ok(tmp_path):
    led = ml.PhaseLedger(tmp_path / "l.jsonl", "t1", actor="a", host="h")
    for phase in ml.PHASES[:7]:
        led.append(phase, ml.RUNNING)
        led.append(phase, ml.DONE, evidence={"k": 1})
    led.append("cutover", ml.RUNNING)
    out = mr.from_replay("h", led.state())
    assert out.state == "needs-operator" and not out.ok and out.phase_reached == "verify"


def test_wraps_storage_result_unchanged(tmp_path):
    res = HiveMigrationResult(
        hive_id="github/o/h",
        hive_dir=tmp_path,
        status="migrated",
        backup_dir="/bk",
        target_path="/t",
        detail="ok",
    )
    out = mr.from_storage_result(res, txn="x")
    assert out.ok and out.state == "migrated" and out.evidence_paths == ("/bk", "/t")
    res.status = "failed"
    assert not mr.from_storage_result(res).ok
