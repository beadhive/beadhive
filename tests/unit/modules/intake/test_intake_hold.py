"""The lifetime intake-hold: its pure rules, the in-memory store's model of it, and the sweep.

External-intake-module-adr.md section 7 and 8 (invariant H), bh-6wj9y R3 and E4. A bound report is
born with an open human gate that keeps it out of ready work for its whole life; it closes only
through ``ReportStore.close`` (force-close first, then resolve the hold), so there is no instant at
which it is open and unblocked. No bd, no clock, no sleep: the order of events is the evidence.
"""

from __future__ import annotations

import pytest

from beadhive.modules.intake import (
    INTAKE_HOLD_GATE_TYPE,
    INTAKE_HOLD_MARKER,
    INTAKE_HOLD_REASON,
    HoldFindingKind,
    HoldGate,
    HoldSweepRequest,
    IntakeRefused,
    IntakeService,
    IntakeStoreError,
    NullIntake,
    RefusalReason,
    ReportDraft,
    blockers_of,
    hold_findings,
    is_hold_gate,
)
from beadhive.testing.intake import MemoryReportStore, draft, memory_store


class _NoLabels:
    def violations(self, labels):
        return ()


def _gate(identifier: str, status: str = "open", *, reason: str = INTAKE_HOLD_REASON) -> dict:
    return {
        "id": identifier,
        "status": status,
        "issue_type": "gate",
        "dependency_type": "blocks",
        "description": f"Ad-hoc gate blocking x\n\nReason: {reason}",
    }


# -- the vocabulary and the pure rules ---------------------------------------------------------


def test_the_hold_reason_carries_the_marker_and_the_gate_is_a_human_one() -> None:
    assert INTAKE_HOLD_REASON.startswith(INTAKE_HOLD_MARKER)
    assert INTAKE_HOLD_GATE_TYPE == "human"


def test_the_module_marker_is_the_roots_marker() -> None:
    """The module may not import the root, so the two spellings are pinned equal here."""
    from beadhive import guard

    assert INTAKE_HOLD_MARKER == guard.INTAKE_HOLD_GATE_MARKER
    assert is_hold_gate(_gate("g1")) and guard.is_intake_hold_gate(_gate("g1"))


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"description": "Ad-hoc gate blocking x\n\nReason: intake-hold: never work"}, True),
        ({"description": "Ad-hoc gate blocking x\n\nReason: INTAKE-HOLD: shouting"}, True),
        ({"reason": "intake-hold: top-level shape"}, True),
        ({"description": "Ad-hoc gate blocking x\n\nReason: bh:review abc1234"}, False),
        # the marker has to be the reason, not a mention in prose
        ({"description": "please see intake-hold: in the docs"}, False),
        ({}, False),
    ],
)
def test_is_hold_gate_matches_the_marker_in_the_reason(row, expected) -> None:
    assert is_hold_gate(row) is expected


def test_blockers_split_the_holds_from_everything_else_and_ignore_the_closed_and_the_soft() -> None:
    row = {
        "id": "acme-1",
        "status": "open",
        "dependencies": [
            _gate("g-open"),
            _gate("g-resolved", "closed"),
            _gate("g-review", reason="bh:review abc1234"),
            {"id": "w-1", "status": "open", "dependency_type": "blocks", "description": "work"},
            {"id": "w-2", "status": "closed", "dependency_type": "blocks"},
            {"id": "w-3", "status": "open", "dependency_type": "waits-for"},
            {"id": "w-4", "status": "open", "dependency_type": "tracks"},  # never blocks
            {"id": "w-5", "status": "open", "dependency_type": "parent-child"},
            "not a mapping",
        ],
    }

    blockers = blockers_of(row)

    assert blockers.holds == ("g-open",)
    assert blockers.others == ("g-review", "w-1", "w-3")
    assert not blockers.closed
    assert blockers_of({"status": "closed"}).closed
    assert blockers_of({}).holds == () and blockers_of({"dependencies": None}).others == ()


def test_findings_name_a_missing_an_unresolved_and_an_orphaned_hold_in_a_stable_order() -> None:
    reports = [("r-open-held", False), ("r-open-bare", False), ("r-closed", True), ("r-bare", True)]
    holds = [
        HoldGate("g-b", ("r-closed",)),
        HoldGate("g-a", ("r-open-held",)),
        HoldGate("g-lost", ()),
        HoldGate("g-misc", ("native-bead",)),
    ]

    findings = hold_findings(reports, holds)

    assert [(f.kind, f.report, f.gate) for f in findings] == [
        (HoldFindingKind.UNRESOLVED, "r-closed", "g-b"),
        (HoldFindingKind.MISSING, "r-open-bare", ""),
        (HoldFindingKind.ORPHANED, "", "g-lost"),
        (HoldFindingKind.ORPHANED, "", "g-misc"),
    ]
    assert [f.repairable for f in findings] == [True, True, False, False]
    assert "no bead" in findings[2].detail and "not a bound report" in findings[3].detail
    assert hold_findings([("r", False)], [HoldGate("g", ("r",))]) == ()
    assert hold_findings([], []) == ()


def test_a_report_with_two_holds_is_held_and_one_hold_on_two_reports_is_not_orphaned() -> None:
    holds = [HoldGate("g1", ("r1",)), HoldGate("g2", ("r1",)), HoldGate("g3", ("r1", "r2"))]
    assert hold_findings([("r1", False), ("r2", False)], holds) == ()


# -- MemoryReportStore models the hold -----------------------------------------------------------


def test_a_bound_report_is_never_ready_and_a_native_one_is() -> None:
    store = memory_store()
    native = store.create(draft("born native"))
    bound = store.create(draft(bound=True))

    assert store.ready_ids() == (native,)
    assert bound not in store.ready_ids()


def test_a_plain_close_is_refused_and_changes_nothing() -> None:
    store = memory_store()
    bound = store.create(draft(bound=True))
    writes = store.writes

    with pytest.raises(IntakeStoreError, match="cannot close blocked issue"):
        store.plain_close(bound)

    view = store.get(bound)
    assert view is not None and view.status == "open" and store.writes == writes


def test_the_held_close_closes_first_and_resolves_the_hold_after_so_there_is_no_ready_window() -> (
    None
):
    store = memory_store()
    bound = store.create(draft(bound=True))
    (gate,) = [h.gate for h in store.holds()]

    store.close(bound, reason="delivered", actor="dev/test")

    assert [e for e in store.events() if e[0] != "open_hold"] == [
        ("close", bound),
        ("resolve_hold", gate),
    ]
    assert store.ready_ids() == ()  # closed, so never ready, at either step


def test_closing_a_bound_report_with_another_live_blocker_is_refused_and_writes_nothing() -> None:
    store = memory_store()
    bound = store.create(draft(bound=True))
    store.add_blocker(bound, "acme-other")
    writes = store.writes

    with pytest.raises(IntakeRefused) as refused:
        store.close(bound, reason="done", actor="dev/test")

    assert refused.value.reason is RefusalReason.LIVE_BLOCKER
    assert "acme-other" in refused.value.detail
    view = store.get(bound)
    assert view is not None and view.status == "open" and store.writes == writes
    assert len(store.holds()) == 1  # the hold is untouched


def test_a_native_report_with_a_live_blocker_fails_like_bd_does() -> None:
    store = memory_store()
    native = store.create(draft("born native"))
    store.add_blocker(native, "acme-other")

    with pytest.raises(IntakeStoreError, match="cannot close blocked issue"):
        store.close(native, reason="done", actor="dev/test")


def test_closing_again_after_a_crash_is_a_noop_and_the_sweep_resolves_the_left_hold() -> None:
    """A crash between the forced close and the resolve leaves a closed report with an open hold.
    bd closes a closed bead again without a word and leaves its gates alone, so the sweep (not a
    second close) resolves the hold."""
    store = memory_store()
    bound = store.create(draft(bound=True))
    store._finish_close(store._row(bound), bound, "crashed before the resolve")
    assert len(store.holds()) == 1
    writes = store.writes

    store.close(bound, reason="retry", actor="dev/test")

    assert len(store.holds()) == 1 and store.writes == writes
    sweep = IntakeService(store, _NoLabels()).sweep_holds(HoldSweepRequest("doctor", repair=True))
    assert [f.kind for f in sweep.repaired] == [HoldFindingKind.UNRESOLVED]
    assert store.holds() == ()


def test_an_orphaned_hold_reads_as_blocking_nothing() -> None:
    store = memory_store()
    bound = store.create(draft(bound=True))
    (gate,) = store.holds()
    store.lose_hold(gate.gate)

    assert store.holds() == (HoldGate(gate.gate, ()),)
    assert bound in store.ready_ids()  # this is the window the doctor sweep exists to close


# -- IntakeService.sweep_holds --------------------------------------------------------------------


def _service(store: MemoryReportStore | None = None) -> tuple[IntakeService, MemoryReportStore]:
    store = store or memory_store()
    return IntakeService(store, _NoLabels()), store


def test_a_healthy_store_sweeps_clean_and_writes_nothing() -> None:
    service, store = _service()
    store.create(draft("born native"))
    store.create(draft(bound=True))
    writes = store.writes

    sweep = service.sweep_holds(HoldSweepRequest(actor="doctor", repair=True))

    assert sweep.clean and sweep.repaired == () and sweep.failed == ()
    assert store.writes == writes


def test_the_sweep_reports_without_repairing_unless_asked() -> None:
    service, store = _service()
    held = store.create(draft(bound=True))
    (gate,) = store.holds()
    store.lose_hold(gate.gate)  # an open bound report that has lost its hold
    closed = store.create(draft("another", bound=True))
    store._finish_close(store._row(closed), closed, "crashed")  # a hold left on a closed one
    writes = store.writes

    sweep = service.sweep_holds(HoldSweepRequest(actor="doctor"))

    assert {(f.kind, f.report or f.gate) for f in sweep.findings} == {
        (HoldFindingKind.MISSING, held),
        (HoldFindingKind.UNRESOLVED, closed),
        (HoldFindingKind.ORPHANED, gate.gate),
    }
    assert sweep.repaired == () and store.writes == writes


def test_the_repair_reopens_a_missing_hold_resolves_a_stale_one_and_leaves_an_orphan() -> None:
    service, store = _service()
    held = store.create(draft(bound=True))
    (lost,) = store.holds()
    store.lose_hold(lost.gate)
    closed = store.create(draft("another", bound=True))
    store._finish_close(store._row(closed), closed, "crashed")

    sweep = service.sweep_holds(HoldSweepRequest(actor="doctor", repair=True))

    kinds = sorted(f.kind for f in sweep.repaired)
    assert kinds == [HoldFindingKind.MISSING, HoldFindingKind.UNRESOLVED]
    assert sweep.failed == ()
    # the report that lost its hold is held again, the stale hold is resolved
    assert held not in store.ready_ids()
    blocked = {h.gate: h.blocks for h in store.holds()}
    assert (held,) in blocked.values() and (closed,) not in blocked.values()
    # the lost gate is an orphan: reported, never repaired, so a second sweep still names it
    again = service.sweep_holds(HoldSweepRequest(actor="doctor", repair=True))
    assert [(f.kind, f.gate) for f in again.findings] == [(HoldFindingKind.ORPHANED, lost.gate)]
    assert again.repaired == ()


def test_a_repair_that_fails_is_reported_and_does_not_stop_the_others() -> None:
    class _FlakyStore(MemoryReportStore):
        def open_hold(self, report, *, actor):
            if report == self.flaky:
                raise IntakeStoreError("database is read-only", bead=report, operation="open_hold")
            super().open_hold(report, actor=actor)

        flaky = ""

    store = _FlakyStore(memory_store().scope())
    service, _ = _service(store)
    first, second = store.create(draft(bound=True)), store.create(draft("two", bound=True))
    for hold in store.holds():
        store.lose_hold(hold.gate)
    store.flaky = first

    sweep = service.sweep_holds(HoldSweepRequest(actor="doctor", repair=True))

    assert [f.report for f in sweep.repaired] == [second]
    ((finding, detail),) = sweep.failed
    assert (finding.kind, finding.report) == (HoldFindingKind.MISSING, first)
    assert detail == "database is read-only"


def test_a_disabled_intake_sweeps_to_nothing() -> None:
    assert NullIntake().sweep_holds(HoldSweepRequest(repair=True)).clean


def test_filing_a_born_native_report_opens_no_hold() -> None:
    """`bh report` and `bh escalate` are unchanged: the hold is only for a bound report."""
    service, store = _service()
    report = service.file_report(ReportDraft("login is broken", "bug", "origin:report", "dev/test"))

    assert store.holds() == () and report in store.ready_ids()
