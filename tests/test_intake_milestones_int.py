"""Integration: the merge and release milestones against a REAL ``bd`` store (I10 acceptance).

Pins what the design relies on from bd 1.3.0, through the product's own seams
(``MilestoneService`` over ``BdReportStore``):

* a merge sets ``delivery:merged`` on every report that tracks the work, OPEN or CLOSED (a
  ``set-state`` and a metadata document write on a closed bead are accepted), and records the
  merge fact in ``intake.outbox`` beside the other documents;
* merged but unreleased keeps a held report open and out of ``bd ready`` (``close-on-release``);
* a verified release moves it to ``released`` and the held close completes it with no instant at
  which the report is ready;
* a drafted ``merged`` record lands in the real ``intake.outbox`` document, and nothing is sent.

Marked ``integration``; self-skips without ``bd`` on PATH. Every store lives in ``tmp_path``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from beadhive.adapters.intake import BdReportStore
from beadhive.modules.intake.application import MilestoneService
from beadhive.modules.intake.domain import (
    CompletionPolicy,
    LeakPolicy,
    MergeFact,
    MilestonePolicy,
    OpState,
    OutboxPolicy,
    ReleaseFact,
    StorePosture,
    Verdict,
    WorkDraft,
)
from beadhive.testing.intake import (
    SCOPE,
    FakeReleaseVerifier,
    InProcessIntakeLock,
    RecordingSanitizer,
    SequentialIdSource,
    draft,
)
from beadhive_bd_cli import CliRoutes, SubprocessBd, err_line
from harness.beads import embedded_env, init_embedded, skip_if_no_bd
from harness.world import git

pytestmark = [pytest.mark.integration, skip_if_no_bd]

_BD = SubprocessBd(timeout=120, env=embedded_env())
ACTOR = "merger/test"
MERGE_SHA = "a" * 40
RELEASE_SHA = "b" * 40


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("intake-milestones-template")
    git("init", "-q", "-b", "main", cwd=root)
    init_embedded(root, SCOPE.operational_prefix)
    return root


@pytest.fixture
def scratch(template: Path, tmp_path: Path) -> Path:
    target = tmp_path / "store"
    shutil.copytree(template, target)
    return target


class Rig:
    def __init__(self, scratch: Path, *, outbox: bool, milestones: MilestonePolicy) -> None:
        self.routes = CliRoutes(_BD, scratch)
        self.store = BdReportStore(self.routes, SCOPE, error_line=err_line)
        self.verifier = FakeReleaseVerifier(
            tags={"v1.0.0": RELEASE_SHA}, contained={RELEASE_SHA: frozenset({MERGE_SHA})}
        )
        self.service = MilestoneService(
            self.store,
            InProcessIntakeLock(),
            ids=SequentialIdSource(),
            leak_policy=LeakPolicy(bead_prefixes=frozenset({SCOPE.operational_prefix, "pub"})),
            sanitizer=RecordingSanitizer(),
            posture=StorePosture(published=False),
            outbox=OutboxPolicy(enabled=outbox),
            milestones=milestones,
            verifier=self.verifier,
        )

    def ready(self) -> set[str]:
        return {str(r["id"]) for r in self.routes.ready_all() or ()}

    def labels(self, bead: str) -> set[str]:
        view = self.store.get(bead)
        assert view is not None
        return set(view.labels)

    def status(self, bead: str) -> str:
        view = self.store.get(bead)
        assert view is not None
        return view.status

    def report(self, work: str, title: str) -> str:
        bead = self.store.create(draft(title, bound=True))
        self.store.set_state(bead, "origin", "github", reason="imported", actor=ACTOR)
        self.store.track(bead, work, actor=ACTOR)
        self.store.set_state(bead, "delivery", "pending", reason="link", actor=ACTOR)
        return bead


def test_a_merge_marks_open_and_closed_reports_and_close_on_release_keeps_them_held(scratch):
    rig = Rig(
        scratch,
        outbox=False,
        milestones=MilestonePolicy(completion=CompletionPolicy.CLOSE_ON_RELEASE),
    )
    work = rig.store.create_work(WorkDraft("fix the login crash", "bug", ACTOR))
    first, second = rig.report(work, "first"), rig.report(work, "second")
    duplicate = rig.report(work, "closed early")
    rig.store.close(duplicate, reason="duplicate", actor=ACTOR)

    outcome = rig.service.on_work_merged(MergeFact(work, MERGE_SHA, ACTOR))

    assert sorted(outcome.reports) == sorted((first, second, duplicate)) and not outcome.errors
    for report in (first, second, duplicate):
        assert "delivery:merged" in rig.labels(report) and "delivery:pending" not in rig.labels(
            report
        )
        document = rig.store.read_outbox(report)
        assert document is not None and document.facts.merged.commit == MERGE_SHA
        assert rig.store.get(report).snapshot is not None  # the other documents survived the write
    assert rig.status(first) == rig.status(second) == "open" and rig.status(duplicate) == "closed"
    assert {first, second, duplicate}.isdisjoint(rig.ready())  # held for life


def test_a_verified_release_completes_the_held_reports_without_a_ready_window(scratch):
    rig = Rig(
        scratch,
        outbox=False,
        milestones=MilestonePolicy(completion=CompletionPolicy.CLOSE_ON_RELEASE),
    )
    work = rig.store.create_work(WorkDraft("fix", "bug", ACTOR))
    report = rig.report(work, "the report")
    rig.service.on_work_merged(MergeFact(work, MERGE_SHA, ACTOR))

    outcome = rig.service.on_release(ReleaseFact("v1.0.0", RELEASE_SHA, ACTOR))

    (item,) = outcome.items
    assert item.verdict is Verdict.YES and item.delivery == "released" and item.closed
    assert "delivery:released" in rig.labels(report) and rig.status(report) == "closed"
    assert report not in rig.ready()
    assert not rig.store.holds()  # the hold was resolved as bookkeeping after the close


def test_close_on_merge_with_the_outbox_off_closes_at_the_merge(scratch):
    rig = Rig(scratch, outbox=False, milestones=MilestonePolicy())
    work = rig.store.create_work(WorkDraft("fix", "bug", ACTOR))
    report = rig.report(work, "the report")

    rig.service.on_work_merged(MergeFact(work, MERGE_SHA, ACTOR))

    assert rig.status(report) == "closed" and "delivery:merged" in rig.labels(report)
    assert report not in rig.ready()


def test_a_drafted_record_lands_in_the_real_document_and_the_report_waits(scratch):
    rig = Rig(scratch, outbox=True, milestones=MilestonePolicy(draft_on_merge=True))
    work = rig.store.create_work(WorkDraft("fix", "bug", ACTOR))
    report = rig.report(work, "the report")

    outcome = rig.service.on_work_merged(MergeFact(work, MERGE_SHA, ACTOR))

    (item,) = outcome.items
    assert item.drafted.startswith("ob1_") and not item.closed
    document = rig.store.read_outbox(report)
    assert document is not None
    (record,) = document.records.values()
    assert record.event.value == "merged" and record.operations[0].state is OpState.DRAFTED
    assert document.facts.merged.work == work
    assert rig.status(report) == "open" and report not in rig.ready()
    assert [v.id for v in rig.store.reports_with_outbox()] == [report]

    again = rig.service.on_work_merged(MergeFact(work, MERGE_SHA, ACTOR))  # a re-run
    assert again.items[0].drafted == "" and len(rig.store.read_outbox(report).records) == 1
