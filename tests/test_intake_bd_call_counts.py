"""The ``bd`` call count of the intake upsert, through the real routes and a recording fake ``bd``.

Import and discovery shell out to ``bd`` once per call, and on a large store each call costs
seconds, so the COUNT of calls is the cost model (bh-w4rak). An existing report once paid a
repair check that read every open hold gate (``gate list`` and one ``dep list`` per gate), so one
unchanged upsert cost O(holds) calls and a pass over N reports O(N^2). These cases pin the counts
instead of any duration: an unchanged upsert makes the same few calls however many reports and
holds exist, a created one makes the same fixed sequence, and a pass is linear in its issues.

No clock, no sleep, no process, no network: counts of recorded argv only.
"""

from __future__ import annotations

import pytest
from tests.test_intake_bead_store import _store

from beadhive.modules.intake import (
    ORIGIN_GITHUB,
    DiscoveryRequest,
    ImportKind,
    ImportRequest,
    IssueDiscoverer,
    IssueImporter,
)
from beadhive.testing.intake import InProcessIntakeLock, MemoryCheckpointStore
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at

ACTOR = "dev/counter"

#: The `bd` verbs of one upsert, in order. A name is the argv up to its first flag.
UNCHANGED = ["list", "dep list"]
CREATED = ["list", "create", "gate create", "set-state", "set-state", "list"]


class _Labels:
    def violations(self, labels):
        return ()


class Rig:
    def __init__(self, existing: int) -> None:
        self.world = MemoryWorld()
        self.tracker = MemoryTracker(self.world)
        self.store, self.bd = _store()
        lock = InProcessIntakeLock()
        self.importer = IssueImporter(
            self.store, _Labels(), self.tracker, lock, origin=ORIGIN_GITHUB
        )
        self.discoverer = IssueDiscoverer(
            self.store, self.tracker, self.importer, lock, MemoryCheckpointStore()
        )
        self.keys = [self.world.add_issue(f"Issue {n}", at=at(1 + n % 20)) for n in range(existing)]
        self.discover()  # every issue is imported: held, untriaged, stamped

    def discover(self, *, full: bool = False):
        return self.discoverer.discover(DiscoveryRequest(ACTOR, full_reconcile=full))

    def verbs(self) -> list[str]:
        """The verb of every recorded call, ``dep list`` and ``gate create`` kept as two words."""
        names = []
        for call in self.bd.calls:
            args = [a for a in call["args"] if a != "--json"]
            two = args[0] in {"dep", "gate"}
            names.append(" ".join(args[:2]) if two else args[0])
        return names

    def counted(self, action) -> list[str]:
        self.bd.calls.clear()
        action()
        return self.verbs()


def _import(rig: Rig, key):
    return lambda: rig.importer.import_issue(ImportRequest(key.url, ACTOR))


@pytest.mark.parametrize("existing", [1, 3, 12])
def test_an_unchanged_upsert_makes_the_same_calls_however_many_reports_exist(existing) -> None:
    rig = Rig(existing)
    issue = rig.tracker.get(rig.tracker.resolve(rig.keys[0].url))

    verbs = rig.counted(lambda: rig.importer.upsert(issue, ACTOR))

    assert verbs == UNCHANGED  # the lookup and the one targeted hold read: no gate scan
    assert not any(v in {"create", "gate create", "set-state", "update"} for v in verbs)


@pytest.mark.parametrize("existing", [1, 12])
def test_a_single_issue_import_of_an_existing_report_is_as_cheap(existing) -> None:
    rig = Rig(existing)

    verbs = rig.counted(_import(rig, rig.keys[-1]))

    assert verbs == UNCHANGED


@pytest.mark.parametrize("existing", [1, 12])
def test_a_created_upsert_makes_the_same_fixed_sequence(existing) -> None:
    rig = Rig(existing)
    key = rig.world.add_issue("Brand new", at=at(25))
    issue = rig.tracker.get(rig.tracker.resolve(key.url))

    verbs = rig.counted(lambda: rig.importer.upsert(issue, ACTOR))

    # lookup, create, hold, origin, intake, then the racing-create guard's re-read
    assert verbs == CREATED


def test_a_closed_report_is_not_asked_for_its_hold() -> None:
    rig = Rig(2)
    view = rig.store.find_by_external_ref(rig.keys[0].url)[0]
    rig.store.close(view.id, reason="done", actor=ACTOR)
    issue = rig.tracker.get(rig.tracker.resolve(rig.keys[0].url))

    assert rig.counted(lambda: rig.importer.upsert(issue, ACTOR)) == ["list"]


def test_a_missing_hold_is_still_repaired() -> None:
    rig = Rig(3)
    held = rig.store.find_by_external_ref(rig.keys[1].url)[0].id
    (gate,) = [h.gate for h in rig.store.holds() if h.blocks == (held,)]
    rig.store.resolve_hold(gate, actor=ACTOR)  # an interrupted create left it unheld
    issue = rig.tracker.get(rig.tracker.resolve(rig.keys[1].url))

    outcome = rig.importer.upsert(issue, ACTOR)

    assert outcome.kind is ImportKind.UPDATED and outcome.repaired == ("hold",)
    assert rig.store.is_held(held)
    assert not any(not rig.store.is_held(v.id) for v in rig.store.bound_reports())


def _pass_calls(issues: int) -> int:
    rig = Rig(issues)
    rig.bd.calls.clear()
    rig.discover(full=True)
    return len(rig.bd.calls)


def test_a_discovery_pass_is_linear_in_its_issues() -> None:
    small, large = _pass_calls(4), _pass_calls(12)

    # per unchanged issue the lookup and the hold read; the full pass adds one bound-report read
    assert small == 4 * len(UNCHANGED) + 1
    assert large == 12 * len(UNCHANGED) + 1
    assert (large - small) / (12 - 4) == len(UNCHANGED)
