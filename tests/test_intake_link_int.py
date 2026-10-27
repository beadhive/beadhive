"""Integration: many reports to one work bead against a REAL ``bd`` store (bh-v2rr6 acceptance).

Pins what the design relies on from bd 1.3.0, through the product's own seams
(``IntakeService`` over ``BdReportStore``):

* two bound reports ``tracks`` one work bead; unlinking one leaves the other's edge, hold and
  ``delivery`` intact;
* the reports stay held and never appear in ``bd ready`` at any step, while the work bead is
  dispatchable the whole time (a ``tracks`` edge gates nothing in either direction);
* ``accept --new-work`` creates a plain work bead (no external ref, no origin, no hold), links it
  and clears the report's ``intake`` while the report stays held;
* ``delivery:pending`` is written at link and survives an unlink, the ``unlinked`` fact lands in
  the report's ``intake.outbox`` document next to the other documents, and the edge never leaves
  the one store.

Marked ``integration``; self-skips without ``bd`` on PATH. Nothing here touches a real hive: every
store lives in ``tmp_path``, and a readiness probe is a read of that store, never a clock.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from beadhive.adapters.intake import BdReportStore
from beadhive.modules.intake import (
    Disposition,
    DispositionRequest,
    IntakeRefused,
    IntakeService,
    LinkKind,
    LinkRequest,
    RefusalReason,
    WorkProgress,
)
from beadhive.modules.intake.domain import UnlinkedFact
from beadhive.testing.intake import SCOPE, InProcessIntakeLock, draft
from beadhive_bd_cli import CliRoutes, SubprocessBd, err_line
from harness.beads import embedded_env, init_embedded, skip_if_no_bd
from harness.world import git

pytestmark = [pytest.mark.integration, skip_if_no_bd]

_BD = SubprocessBd(timeout=120, env=embedded_env())
ACTOR = "dev/triage"


class _NoLabels:
    def violations(self, labels):
        return ()


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("intake-link-template")
    git("init", "-q", "-b", "main", cwd=root)
    init_embedded(root, SCOPE.operational_prefix)
    return root


@pytest.fixture
def scratch(template: Path, tmp_path: Path) -> Path:
    target = tmp_path / "store"
    shutil.copytree(template, target)
    return target


class Rig:
    def __init__(self, scratch: Path) -> None:
        self.scratch = scratch
        self.routes = CliRoutes(_BD, scratch)
        self.store = BdReportStore(self.routes, SCOPE, error_line=err_line)
        self.lock = InProcessIntakeLock()
        self.service = IntakeService(self.store, _NoLabels(), lock=self.lock)

    def ready(self) -> set[str]:
        rows = self.routes.ready_all()
        return {str(r["id"]) for r in rows or ()}

    def report(self, title: str) -> str:
        bead = self.store.create(draft(title, bound=True))
        self.store.set_state(bead, "origin", "github", reason="imported", actor=ACTOR)
        self.store.set_state(bead, "intake", "untriaged", reason="imported", actor=ACTOR)
        return bead

    def labels(self, bead: str) -> set[str]:
        view = self.store.get(bead)
        assert view is not None
        return set(view.labels)

    def link(self, report: str, work: str, **kwargs):
        return self.service.link(LinkRequest(report, work, ACTOR, **kwargs))


@pytest.fixture
def rig(scratch) -> Rig:
    return Rig(scratch)


def test_two_reports_one_work_unlink_one_leaves_the_other_and_nothing_becomes_ready(rig) -> None:
    work = rig.store.create(draft("fix the login crash"))
    first, second = rig.report("first"), rig.report("second")
    assert {first, second}.isdisjoint(rig.ready()) and work in rig.ready()

    assert rig.link(first, work).kind is LinkKind.LINKED
    assert rig.link(second, work).kind is LinkKind.LINKED

    assert rig.store.trackers_of(work) == tuple(sorted((first, second)))
    assert "delivery:pending" in rig.labels(first) | rig.labels(second)
    assert {first, second}.isdisjoint(rig.ready())  # held for life: a tracks edge changes nothing
    assert work in rig.ready()  # and the reports never gate the work bead

    outcome = rig.service.unlink(first, actor=ACTOR)

    assert (outcome.kind, outcome.work, outcome.delivery) == (LinkKind.UNLINKED, work, "pending")
    assert rig.store.trackers_of(work) == (second,)
    assert rig.store.tracked_work(second) == work and rig.store.tracked_work(first) is None
    assert "delivery:pending" in rig.labels(second)  # the other report is intact
    assert rig.store.read_outbox(second) is None
    assert {first, second}.isdisjoint(rig.ready()) and work in rig.ready()
    assert {h.blocks for h in rig.store.holds()} >= {(first,), (second,)}  # both still held


def test_unlink_keeps_delivery_on_the_real_store_and_records_the_fact_beside_the_documents(
    rig,
) -> None:
    work = rig.store.create(draft("work"))
    report = rig.report("report")
    rig.link(report, work)

    rig.service.unlink(report, actor=ACTOR)

    assert "delivery:pending" in rig.labels(report)  # set-state cannot clear it; it is kept
    document = rig.store.read_outbox(report)
    assert document is not None and document.facts.unlinked == UnlinkedFact(work)
    assert document.seq == 1 and document.records == {}
    assert rig.service.work_progress(report) is WorkProgress.UNLINKED
    # the source snapshot document is untouched by the rewrite of the outbox document
    assert rig.store.get(report).snapshot is not None

    rig.link(report, work)  # re-link the same work: the fact is cleared, the value kept
    cleared = rig.store.read_outbox(report)
    assert cleared is not None and cleared.facts.unlinked is None and cleared.seq == 2
    assert rig.service.work_progress(report) is WorkProgress.OPEN


def test_accept_new_work_on_a_real_store_creates_plain_dispatchable_work(rig) -> None:
    report = rig.report("the checkout hangs")
    other = rig.report("checkout hangs too")

    outcome = rig.service.dispose(
        DispositionRequest(report, Disposition.ACCEPT, ACTOR, new_work=True, priority="1")
    )
    joined = rig.service.dispose(
        DispositionRequest(other, Disposition.ACCEPT, ACTOR, work=outcome.work)
    )

    work = outcome.work
    assert outcome.created_work and not joined.created_work and joined.work == work
    view = rig.store.get(work)
    assert view is not None and view.title == "the checkout hangs" and view.priority == "1"
    assert view.external_ref == "" and view.binding is None and view.source_system == ""
    assert not [x for x in view.labels if x.startswith(("origin:", "intake:", "delivery:"))]
    assert set(SCOPE.hive.labels()) <= set(view.labels)
    assert rig.store.trackers_of(work) == tuple(sorted((report, other)))
    assert {"delivery:pending", "intake:accepted"} <= rig.labels(report)
    assert "intake:untriaged" not in rig.labels(report)
    assert {report, other}.isdisjoint(rig.ready()) and work in rig.ready()
    # the description is triage's, never the issue body (draft() carries the body "steps")
    row = rig.routes.issue_show_raw(work)
    row = row[0] if isinstance(row, list) else row
    assert report in row["description"] and "steps" not in row["description"]


def test_d_a_and_the_store_boundaries_are_refused_on_a_real_store(rig) -> None:
    work = rig.store.create(draft("work"))
    first, second = rig.report("first"), rig.report("second")
    rig.link(first, work)
    before = rig.store.trackers_of(work)

    for target, reason in (
        (second, RefusalReason.TARGET_IS_REPORT),  # D-a: a bound report is never work
        ("pub-abc1", RefusalReason.PUBLIC_STORE_ID),  # no edge crosses stores
        ("other-abc1", RefusalReason.CROSS_STORE_LINK),
        (f"{SCOPE.operational_prefix}-nope", RefusalReason.CROSS_STORE_LINK),
    ):
        with pytest.raises(IntakeRefused) as refused:
            rig.link(first, target, replace=True)
        assert refused.value.reason is reason

    assert rig.store.trackers_of(work) == before and rig.store.tracked_work(second) is None


def test_a_tracked_report_links_to_an_epic_without_gating_it_or_becoming_ready(rig) -> None:
    """The adopt path (D-c): `bd dep add <report> <epic> -t tracks` is accepted between a held
    report and an epic, blocks nothing in either direction and leaves the report out of ready."""
    created = rig.routes.create_report("an epic", "epic", ",".join(SCOPE.hive.labels()))
    assert created.returncode == 0
    epic = json.loads(created.stdout)["id"]
    report = rig.report("held report")

    rig.store.track(report, epic, actor=ACTOR)

    assert rig.store.trackers_of(epic) == (report,)
    assert report not in rig.ready()  # still held
    shown = rig.routes.issue_show_raw(epic)
    shown = shown[0] if isinstance(shown, list) else shown
    assert shown["status"] == "open"  # the epic is not blocked by the report it tracks
