"""The sweep for a rejected report's stray ``delivery`` value (bh-5kpwc), over the memory store.

A ``duplicate-of`` linked before the fix wrote ``delivery:pending`` on the duplicate. The sweep
lists those reports (the dry run, which writes nothing) and, with ``repair``, clears the value from
the bead's label cache. It never touches an accepted report, an unbound one, or any other
dimension, and a rerun finds nothing.
"""

from __future__ import annotations

import pytest

from beadhive.modules.intake import IntakeService, IntakeStoreError, NullIntake
from beadhive.modules.intake.domain import (
    DeliverySweep,
    DeliverySweepRequest,
    StrayDelivery,
    delivery_unowned,
    stray_deliveries,
)
from beadhive.testing.intake import InProcessIntakeLock, MemoryReportStore, draft, memory_store

ACTOR = "dev/repair"


class _NoLabels:
    def violations(self, labels):
        return ()


class Rig:
    def __init__(self) -> None:
        self.store: MemoryReportStore = memory_store()
        self.service = IntakeService(self.store, _NoLabels(), lock=InProcessIntakeLock())

    def report(self, intake: str, delivery: str = "", *, bound: bool = True, closed=False) -> str:
        bead = self.store.create(draft(f"{intake} {delivery}", bound=bound))
        self.store.set_state(bead, "intake", intake, reason="triaged", actor=ACTOR)
        if delivery:
            self.store.set_state(bead, "delivery", delivery, reason="linked", actor=ACTOR)
        if closed:
            self.store.close(bead, reason="duplicate", actor=ACTOR)
        return bead

    def delivery(self, bead: str) -> list[str]:
        view = self.store.get(bead)
        assert view is not None
        return [label for label in view.labels if label.startswith("delivery:")]

    def sweep(self, *, repair: bool = False) -> DeliverySweep:
        return self.service.sweep_delivery(DeliverySweepRequest(ACTOR, repair))


@pytest.fixture
def rig() -> Rig:
    return Rig()


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (("intake:rejected", "delivery:pending"), True),
        (("intake:rejected",), True),
        (("intake:accepted", "delivery:pending"), False),
        ((), False),
    ],
)
def test_only_a_rejected_reports_delivery_is_unowned(labels, expected) -> None:
    assert delivery_unowned(labels) is expected


def test_the_dry_run_lists_every_rejected_report_with_a_delivery_and_writes_nothing(rig) -> None:
    duplicate = rig.report("rejected", "pending", closed=True)  # #75's shape: closed, pending
    stale = rig.report("rejected", "merged")
    rig.report("rejected")  # rejected, no delivery: nothing to find
    rig.report("accepted", "pending")  # a tracks link: its delivery is its own
    rig.report("rejected", "pending", bound=False)  # born native: no tracker issue to project to
    writes = rig.store.writes

    sweep = rig.sweep()

    assert sweep.findings == (StrayDelivery(duplicate, "pending"), StrayDelivery(stale, "merged"))
    assert sweep.cleared == () and sweep.failed == () and not sweep.clean
    assert rig.store.writes == writes and rig.delivery(duplicate) == ["delivery:pending"]


def test_repair_clears_only_the_delivery_value_of_each_finding(rig) -> None:
    duplicate = rig.report("rejected", "pending", closed=True)
    accepted = rig.report("accepted", "pending")

    sweep = rig.sweep(repair=True)

    assert sweep.cleared == sweep.findings == (StrayDelivery(duplicate, "pending"),)
    assert rig.delivery(duplicate) == [] and rig.delivery(accepted) == ["delivery:pending"]
    labels = rig.store.get(duplicate).labels
    assert "intake:rejected" in labels  # no other dimension moves
    assert rig.store.get(duplicate).status == "closed"
    assert rig.store.history(duplicate)[-1][:2] == ("delivery", "")


def test_a_repair_is_idempotent_and_a_later_dry_run_is_clean(rig) -> None:
    rig.report("rejected", "pending", closed=True)
    rig.sweep(repair=True)
    writes = rig.store.writes

    again = rig.sweep(repair=True)

    assert again.clean and again.cleared == () and rig.store.writes == writes
    assert rig.sweep().clean


def test_one_failed_clear_is_reported_and_the_rest_are_still_cleared(rig, monkeypatch) -> None:
    first = rig.report("rejected", "pending", closed=True)
    second = rig.report("rejected", "pending", closed=True)
    real = rig.store.clear_state

    def flaky(bead, dimension, *, reason, actor):
        if bead == first:
            raise IntakeStoreError("bd label remove failed", bead=bead, operation="clear_state")
        real(bead, dimension, reason=reason, actor=actor)

    monkeypatch.setattr(rig.store, "clear_state", flaky)

    sweep = rig.sweep(repair=True)

    assert [f.report for f in sweep.cleared] == [second]
    assert [(f.report, why) for f, why in sweep.failed] == [(first, "bd label remove failed")]
    assert rig.delivery(first) == ["delivery:pending"] and rig.delivery(second) == []


def test_a_store_without_any_reports_sweeps_clean_and_intake_off_sweeps_nothing(rig) -> None:
    assert rig.sweep().clean
    assert NullIntake().sweep_delivery(DeliverySweepRequest(ACTOR, True)) == DeliverySweep()


def test_stray_deliveries_are_listed_in_report_order() -> None:
    store = memory_store()
    ids = [store.create(draft(f"r{n}", bound=True)) for n in range(3)]
    for bead in reversed(ids):
        store.set_state(bead, "intake", "rejected", reason="x", actor=ACTOR)
        store.set_state(bead, "delivery", "pending", reason="x", actor=ACTOR)

    views = [store.get(bead) for bead in reversed(ids)]

    assert [f.report for f in stray_deliveries(views)] == sorted(ids)
