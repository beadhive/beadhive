"""The read-only end-state finders (bh-cd03d, bh-c0x0i), on ``MemoryTracker`` and the memory store.

A closed bound report whose issue the one listing shows open, and a report closed while its
completion comment was abandoned. Both are read-only: nothing is drafted, approved or sent.
"""

from __future__ import annotations

import dataclasses

from beadhive.modules.intake.application import IntakeService, OutboxService
from beadhive.modules.intake.application.end_states import scan_end_states
from beadhive.modules.intake.domain import (
    CloseReason,
    IssueBinding,
    IssueStatus,
    LeakPolicy,
    OpKind,
    OutboxApprove,
    OutboxPolicy,
    OwnIdentity,
    ProposalDisposition,
    ProposalStatus,
    ReplyDraft,
    ReportDraft,
    SourceSnapshot,
    StorePosture,
    UnlinkedFact,
)
from beadhive.modules.intake.domain.proposal import new_proposal
from beadhive.modules.tracker.contracts import READ_OPERATIONS, WRITE_OPERATIONS, IssueState
from beadhive.testing.intake import (
    SCOPE,
    InProcessIntakeLock,
    MemoryReportStore,
    RecordingSanitizer,
    SequentialIdSource,
)
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at

ME = OwnIdentity("acme/widgets", "inst-1")
POLICY = OutboxPolicy(enabled=True, labels_enabled=True, close_enabled=True)


def _marker(marker) -> str:
    return f"<!-- bh:v1 hive={marker.hive} install={marker.install} outbox={marker.outbox_id} -->"


class _NoLabels:
    def violations(self, labels) -> tuple[str, ...]:
        return ()


class Scene:
    def __init__(self) -> None:
        self.world = MemoryWorld()
        self.world.set_write_time(at(2))
        self.tracker = MemoryTracker(self.world, enabled=READ_OPERATIONS | WRITE_OPERATIONS)
        self.store = MemoryReportStore(SCOPE)
        service = OutboxService(
            self.store,
            self.tracker,
            InProcessIntakeLock(),
            ids=SequentialIdSource(),
            identity=ME,
            marker_line=_marker,
            policy=POLICY,
            leak_policy=LeakPolicy(bead_prefixes=frozenset({"acme", "pub"})),
            sanitizer=RecordingSanitizer.of([]),
            posture=StorePosture(published=False),
            holder="host-1:42",
        )
        self.intake = IntakeService(self.store, _NoLabels(), outbox=service)

    def report(self, title: str, *, closed_issue: bool = False):
        issue = self.world.add_issue(title, at=at(1), labels=("bug",))
        if closed_issue:
            self.world.update_issue(issue, at=at(3), state=IssueState.CLOSED)
        binding = IssueBinding("", "memory", self.world.locator, issue.url, issue.identifier)
        report = self.store.create(
            ReportDraft(
                title,
                "bug",
                "origin:github",
                "dev/import",
                binding=binding,
                snapshot=SourceSnapshot(title, "", IssueStatus.OPEN, "r1", labels=("bug",)),
            )
        )
        return report

    def triaged(self, report: str, disposition=ProposalDisposition.REJECT) -> None:
        proposal = dataclasses.replace(new_proposal(disposition), status=ProposalStatus.APPLIED)
        self.store.write_proposal(report, proposal, expect_seq=0, actor="alice")
        self.store.close(report, reason="triaged", actor="alice")

    def send_reply(self, report: str, *, close: CloseReason | None) -> str:
        oid = self.intake.draft_reply(ReplyDraft(report, "thanks", "alice", close=close)).outbox_id
        rendered = self.intake.render(oid).of(OpKind.COMMENT)
        self.intake.approve(OutboxApprove(oid, rendered.short_digest, "alice"))
        self.intake.send(oid, actor="alice")
        return oid

    def scan(self, **kw):
        return self.intake.end_states(**kw)


def test_a_closed_report_with_an_open_issue_is_found_with_the_repair_verb() -> None:
    s = Scene()
    triaged = s.report("rejected")
    plain = s.report("closed by hand")
    shut = s.report("issue closed already", closed_issue=True)
    still_open = s.report("report still open")
    for r in (triaged, shut):
        s.triaged(r)
    s.store.close(plain, reason="by hand", actor="alice")

    found = s.scan()

    assert found.listed
    assert {f.report: f.repair for f in found.closed_open} == {
        triaged: "backfill-close",
        plain: "outbox draft",
    }
    assert still_open not in {f.report for f in found.closed_open}


def test_a_tracked_report_closed_at_delivery_is_a_backfill_close_shape() -> None:
    s = Scene()
    shipped = s.report("shipped")
    s.store.set_state(shipped, "delivery", "released", reason="shipped", actor="alice")
    s.store.close(shipped, reason="delivery complete", actor="alice")
    (finding,) = s.scan().closed_open
    assert (finding.report, finding.repair, finding.delivery) == (
        shipped,
        "backfill-close",
        "released",
    )

    unlinked = s.report("unlinked")
    s.store.set_state(unlinked, "delivery", "merged", reason="shipped", actor="alice")
    doc = s.store.read_outbox(unlinked)
    from beadhive.modules.intake.domain import OutboxDocument

    doc = doc or OutboxDocument()
    s.store.write_outbox(
        unlinked,
        dataclasses.replace(
            doc, seq=doc.seq + 1, facts=dataclasses.replace(doc.facts, unlinked=UnlinkedFact("w"))
        ),
        expect_seq=doc.seq,
        actor="alice",
    )
    s.store.close(unlinked, reason="done", actor="alice")
    repairs = {f.report: f.repair for f in s.scan().closed_open}
    assert repairs[unlinked] == "outbox draft"


def test_a_drafted_close_is_named_as_the_pending_send() -> None:
    s = Scene()
    report = s.report("replied")
    oid = s.intake.draft_reply(
        ReplyDraft(report, "no", "alice", close=CloseReason.NOT_PLANNED)
    ).outbox_id
    s.store.close(report, reason="triaged", actor="alice")
    (finding,) = s.scan().closed_open
    assert (finding.repair, finding.pending) == ("outbox send", oid)


def test_one_listing_serves_every_finding_and_none_is_made_without_a_closed_report() -> None:
    s = Scene()
    s.report("open one")
    before = s.world.read_requests()
    assert s.scan().clean and s.world.read_requests() == before  # no closed report: no listing

    for n in range(3):
        s.triaged(s.report(f"closed {n}"))
    before = s.world.read_requests()
    found = s.scan()
    assert len(found.closed_open) == 3
    one_listing = s.world.read_requests() - before
    s.world.read_requests()
    assert one_listing == 1  # a single page: ONE request for all three findings


def test_the_scan_writes_nothing() -> None:
    s = Scene()
    report = s.report("rejected")
    s.triaged(report)
    writes = s.world.write_requests()
    s.scan()
    assert s.world.write_requests() == writes
    assert s.store.read_outbox(report) is None


def test_the_open_check_can_be_skipped_for_the_local_findings() -> None:
    s = Scene()
    s.triaged(s.report("rejected"))
    before = s.world.read_requests()
    found = s.scan(check_open=False)
    assert not found.listed and found.closed_open == ()
    assert s.world.read_requests() == before


def test_scan_end_states_is_free_standing() -> None:
    s = Scene()
    s.triaged(s.report("rejected"))
    assert len(scan_end_states(s.store, s.tracker).closed_open) == 1
