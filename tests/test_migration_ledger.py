"""Phase ledger primitive (bh-ksmt0.2): hash chain, replay, ADR invariants. No real clock."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from beadhive import migration_ledger as ml


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 19, tzinfo=UTC)

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


@pytest.fixture
def ledger(tmp_path):
    return ml.PhaseLedger(
        tmp_path / "m" / "ledger.jsonl", "txn1", actor="a", host="h1", clock=Clock()
    )


def run_to(ledger, upto):
    for phase in ml.PHASES[: ml.PHASES.index(upto) + 1]:
        ledger.append(phase, ml.RUNNING)
        ledger.append(phase, ml.DONE, evidence={"ok": phase})


def ok(_rec):
    return ml.ResumeVerdict(True)


def test_append_chains_and_replay_derives_state(ledger):
    run_to(ledger, "snapshot")
    rep = ledger.state()
    assert [r.seq for r in rep.records] == [1, 2, 3, 4, 5, 6]
    assert rep.records[0].prev == ml.GENESIS
    assert rep.records[1].prev == rep.records[0].digest()
    assert rep.txn_state == "snapshotted"
    assert rep.next_phase == "attest-backup"
    assert rep.evidence("assess") == {"ok": "assess"}


def test_ts_comes_from_injected_clock(ledger):
    rec = ledger.append("assess", ml.RUNNING)
    assert rec.ts == "2026-10-19T00:00:01+00:00"


def test_done_requires_evidence(ledger):
    ledger.append("assess", ml.RUNNING)
    with pytest.raises(ml.LedgerFault, match="evidence"):
        ledger.append("assess", ml.DONE)


def test_never_skips_forward(ledger):
    with pytest.raises(ml.LedgerFault, match="skips forward"):
        ledger.append("snapshot", ml.RUNNING)


def test_illegal_transition_refused(ledger):
    with pytest.raises(ml.LedgerFault, match="legal"):
        ledger.append("assess", ml.DONE, evidence={"x": 1})


def test_non_holder_writer_is_a_fault(tmp_path):
    led = ml.PhaseLedger(tmp_path / "l.jsonl", "t", actor="b", holder="a")
    with pytest.raises(ml.LedgerFault, match="non-holder"):
        led.append("assess", ml.RUNNING)


def test_secret_in_evidence_refused(ledger):
    ledger.append("assess", ml.RUNNING)
    with pytest.raises(ml.LedgerFault, match="credentials"):
        ledger.append("assess", ml.DONE, evidence={"url": "s3://user:pw@host/b"})


def test_edit_breaks_the_chain(ledger):
    run_to(ledger, "quiesce")
    lines = ledger.path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["evidence"] = {"forged": True}
    lines[1] = json.dumps(rec)
    ledger.path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ml.LedgerCorrupt, match="hash chain"):
        ledger.state()


def test_truncated_middle_record_detected(ledger):
    run_to(ledger, "quiesce")
    lines = ledger.path.read_text().splitlines()
    ledger.path.write_text("\n".join(lines[:1] + lines[2:]) + "\n")
    with pytest.raises(ml.LedgerCorrupt, match="seq"):
        ledger.state()


def test_torn_tail_refused_with_guidance(ledger):
    run_to(ledger, "assess")
    with ledger.path.open("a") as fh:
        fh.write('{"schema": "bh.remote')
    with pytest.raises(ml.LedgerCorrupt, match="torn") as ei:
        ledger.state()
    assert "Do not edit" in str(ei.value)


def test_incompatible_schema_refused(ledger):
    ledger.path.parent.mkdir(parents=True)
    ledger.path.write_text(json.dumps({"schema": "bh.remote-migration-ledger/2"}) + "\n")
    with pytest.raises(ml.LedgerIncompatible):
        ledger.state()


def test_other_transaction_refused(tmp_path):
    p = tmp_path / "l.jsonl"
    ml.PhaseLedger(p, "one", actor="a").append("assess", ml.RUNNING)
    with pytest.raises(ml.LedgerCorrupt, match="belongs to"):
        ml.PhaseLedger(p, "two", actor="a").state()


def test_running_on_resume_becomes_failed_and_reruns(ledger):
    run_to(ledger, "assess")
    ledger.append("quiesce", ml.RUNNING)
    plan = ledger.plan_resume({"assess": ok})
    assert plan.recovered == ["quiesce"]
    assert plan.next_phase == "quiesce"
    assert ledger.state().phases["quiesce"] == ml.FAILED
    ledger.append("quiesce", ml.RUNNING)  # failed -> running is legal


def test_resume_reverifies_done_phases_and_refuses_without_verifier(ledger):
    run_to(ledger, "snapshot")
    with pytest.raises(ml.LedgerFault, match="no verifier"):
        ledger.plan_resume({"assess": ok})
    plan = ledger.plan_resume({p: ok for p in ml.PHASES})
    assert plan.next_phase == "attest-backup"
    assert not plan.invalidated


def test_failed_reverify_invalidates_phase_and_everything_after(ledger):
    run_to(ledger, "snapshot")
    verifiers = {
        "assess": ok,
        "quiesce": lambda r: ml.ResumeVerdict(False, "HEAD moved"),
        "snapshot": ok,
    }
    plan = ledger.plan_resume(verifiers)
    assert plan.next_phase == "quiesce"
    assert plan.invalidated == ["quiesce", "snapshot"]
    rep = ledger.state()
    assert rep.phases["snapshot"] == ml.FAILED
    assert rep.next_phase == "quiesce"
    ledger.append("quiesce", ml.RUNNING)
    ledger.append("quiesce", ml.DONE, evidence={"head": "b"})
    ledger.append("snapshot", ml.RUNNING)  # failed -> running, in order


def test_irreversible_phase_interrupted_goes_to_needs_operator(ledger):
    run_to(ledger, "verify")
    ledger.append("cutover", ml.RUNNING)
    plan = ledger.plan_resume({p: ok for p in ml.PHASES})
    assert plan.needs_operator and plan.next_phase is None
    assert ledger.state().txn_state == "needs-operator"


def test_rollback_clears_needs_operator(ledger):
    run_to(ledger, "verify")
    ledger.append("cutover", ml.RUNNING)
    ledger.append("cutover", ml.FAILED, evidence={"why": "x"})
    assert ledger.state().txn_state == "needs-operator"
    ledger.append("cutover", ml.ROLLED_BACK, evidence={"registry": "restored"})
    assert ledger.state().txn_state == "verified"


def test_abort_terminates(ledger):
    run_to(ledger, "assess")
    ledger.append(ml.ABORT, ml.DONE, evidence={"why": "operator"})
    assert ledger.state().txn_state == "aborted"
    with pytest.raises(ml.LedgerFault, match="aborted"):
        ledger.append("quiesce", ml.RUNNING)


def test_full_run_reaches_retired(ledger):
    run_to(ledger, "retire")
    rep = ledger.state()
    assert rep.txn_state == "retired" and rep.next_phase is None


def test_path_for(tmp_path):
    assert ml.PhaseLedger.path_for(tmp_path, "h", "t") == tmp_path / "migrations/h/t/ledger.jsonl"


def test_cross_host_append_is_a_fault(ledger, tmp_path):
    ledger.append("assess", ml.RUNNING)
    other = ml.PhaseLedger(ledger.path, "txn1", actor="a", host="h2")
    with pytest.raises(ml.LedgerFault, match="cross-host"):
        other.append("assess", ml.DONE, evidence={"x": 1})
    assert ledger.state().records[0].host == "h1"
