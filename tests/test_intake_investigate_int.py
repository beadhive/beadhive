"""Integration: the ``investigates`` relation against a REAL ``bd`` store (bh-u3a53).

Pins what the design relies on from bd 1.3.0, through the product's own seams (``IntakeService``
over ``BdReportStore``):

* bd refuses a custom edge type, so an investigation is bd's built-in ``validates`` edge from the
  work to the report; it gates nothing: the held report stays out of ``bd ready`` and the
  reproduction bead is dispatchable throughout, although it "depends on" an open report;
* ``--new-work`` creates a plain bead (no external ref, no origin, no hold, no delivery);
* the same pair may carry both a ``validates`` and a ``tracks`` edge (opposite directions), and
  removing either leaves the other;
* closing the reproduction bead leaves the report open, held and with no ``delivery`` value, and
  the investigation readable on both closed and open beads.

Marked ``integration``; self-skips without ``bd`` on PATH. Every store lives in ``tmp_path``, and a
readiness probe is a read of that store, never a clock.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from beadhive.adapters.intake import BdReportStore
from beadhive.modules.intake import (
    INVESTIGATES_EDGE,
    IntakeService,
    InvestigateRequest,
    LinkKind,
    LinkRequest,
    ReportRelations,
    WorkDraft,
    WorkProgress,
)
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
    root = tmp_path_factory.mktemp("intake-investigate-template")
    git("init", "-q", "-b", "main", cwd=root)
    init_embedded(root, SCOPE.operational_prefix)
    return root


class Rig:
    def __init__(self, scratch: Path) -> None:
        self.routes = CliRoutes(_BD, scratch)
        self.store = BdReportStore(self.routes, SCOPE, error_line=err_line)
        self.service = IntakeService(self.store, _NoLabels(), lock=InProcessIntakeLock())

    def ready(self) -> set[str]:
        return {str(r["id"]) for r in self.routes.ready_all() or ()}

    def report(self, title: str) -> str:
        bead = self.store.create(draft(title, bound=True))
        self.store.set_state(bead, "intake", "untriaged", reason="imported", actor=ACTOR)
        return bead

    def work(self, title: str) -> str:
        return self.store.create_work(WorkDraft(title, "task", ACTOR))

    def labels(self, bead: str) -> set[str]:
        view = self.store.get(bead)
        assert view is not None
        return set(view.labels)

    def investigate(self, report: str, **kwargs):
        return self.service.investigate(InvestigateRequest(report, ACTOR, **kwargs))


@pytest.fixture
def rig(template: Path, tmp_path: Path) -> Rig:
    target = tmp_path / "store"
    shutil.copytree(template, target)
    return Rig(target)


def test_an_investigation_is_a_validates_edge_that_gates_nothing(rig) -> None:
    report = rig.report("the checkout hangs")
    work = rig.work("Reproduce: hang")
    assert report not in rig.ready() and work in rig.ready()

    outcome = rig.investigate(report, work=work)

    assert outcome.kind is LinkKind.LINKED
    rows = rig.routes.dependency_list(work, direction="down", type_=INVESTIGATES_EDGE)
    assert [str(row["id"]) for row in rows] == [report]  # work -> report, validates
    assert rig.store.investigations_of(report) == (work,)
    assert rig.store.tracked_work(report) is None and rig.store.trackers_of(work) == ()
    assert report not in rig.ready()  # still held
    assert work in rig.ready()  # depends on an open report through validates: still ready
    assert not [x for x in rig.labels(report) if x.startswith("delivery:")]
    assert "intake:untriaged" in rig.labels(report)
    assert rig.store.read_outbox(report) is None


def test_new_work_creates_plain_dispatchable_reproduction_work(rig) -> None:
    report = rig.report("login page crashes")

    outcome = rig.investigate(report, new_work=True, priority="1")

    work = outcome.work
    assert outcome.created_work and rig.store.investigations_of(report) == (work,)
    view = rig.store.get(work)
    assert view is not None and view.title == "Reproduce: login page crashes"
    assert (view.issue_type, view.priority) == ("task", "1")
    assert view.external_ref == "" and view.binding is None
    assert not [x for x in view.labels if x.startswith(("origin:", "intake:", "delivery:"))]
    assert work in rig.ready() and report not in rig.ready()
    row = rig.routes.issue_show_raw(work)
    row = row[0] if isinstance(row, list) else row
    assert row["description"].startswith(
        f"Reproduction task created by triage for intake report {report}."
    )


def test_tracks_and_investigates_coexist_and_unlink_independently(rig) -> None:
    report = rig.report("a crash")
    fix, work = rig.work("fix"), rig.work("Reproduce: crash")
    rig.investigate(report, work=work)
    rig.investigate(report, work=fix)  # investigated first, tracked after: allowed
    assert rig.service.link(LinkRequest(report, fix, ACTOR)).kind is LinkKind.LINKED

    assert rig.service.relations(report) == ReportRelations(
        report, tracks=fix, investigated_by=tuple(sorted((fix, work))), progress=WorkProgress.OPEN
    )

    rig.service.uninvestigate(report, fix, actor=ACTOR)
    assert rig.store.tracked_work(report) == fix  # the tracks edge on the same pair survives
    rig.service.unlink(report, actor=ACTOR)
    assert rig.store.investigations_of(report) == (work,)  # and the reverse
    assert {fix, work} <= rig.ready() and report not in rig.ready()


def test_closing_the_reproduction_bead_leaves_the_report_open_and_held(rig) -> None:
    report = rig.report("a hang")
    work = rig.investigate(report, new_work=True).work

    rig.routes.issue_close(work, reason="reproduced on v2.3.1", actor=ACTOR, capture=True)

    view = rig.store.get(report)
    assert view is not None and view.status == "open" and report not in rig.ready()
    assert not [x for x in view.labels if x.startswith(("delivery:", "notified:"))]
    assert rig.store.investigations_of(report) == (work,)
    assert any(report in hold.blocks for hold in rig.store.holds())
