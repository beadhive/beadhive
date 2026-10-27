"""The conversation model's pure rules, driven directly (no tracker, no store, no clock).

Revision rules (T4, T5), own-message recognition (T13), deletion candidates (T8, T9, T11, T12),
presence (T16), checkpoint scope (T17), marker privacy (T19), the triage watermark and the
``intake.thread`` codec. Timestamps are explicit values.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from beadhive.modules.intake.domain import (
    CheckpointScope,
    EventKind,
    Evidence,
    Flag,
    IntakeRefused,
    Observation,
    OwnIdentity,
    OwnMark,
    Recognition,
    RecordState,
    RefusalReason,
    Role,
    StorePosture,
    ThreadActor,
    ThreadDocumentError,
    ThreadIssue,
    ThreadPolicy,
    ThreadState,
    advance_checkpoint,
    apply_observations,
    checkpoint_for,
    classify,
    digest,
    flagged,
    mark_considered,
    marker_bead_ids,
    new_evidence,
    new_thread,
    reclassify,
    reconcile_comments,
    reconcile_presence,
    require_private_store,
    thread_from_document,
    thread_to_document,
)

URL = "memory://acme/widgets/issues/1"
ME = OwnIdentity("acme/widgets", "install-1")
BOT = ThreadActor("bot-1", "bot", "beadhive[bot]")
ALICE = ThreadActor("human-alice", "human", "alice")
CTX = Recognition(identity=ME, known_actors=frozenset({BOT}))


def at(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=UTC)


def obs(cid: str = "c1", body: str = "hello", *, day: int = 2, updated: int | None = None, **kw):
    return Observation(
        cid,
        kw.pop("issue_url", URL),
        body,
        at(day),
        at(updated if updated is not None else day),
        author=kw.pop("author", ALICE),
        **kw,
    )


def thread():
    return new_thread("memory", "acme/widgets", ThreadIssue("#1", URL))


def ingest(*observations, base=None, ctx=CTX, policy=None):
    return apply_observations(base or thread(), observations, ctx, policy or ThreadPolicy())


# -- revisions (R5 rule 2) ---------------------------------------------------------------------


def test_a_first_sighting_is_rev_1_with_local_seqs() -> None:
    applied = ingest(obs("c1", day=2), obs("c2", day=3))
    first, second = applied.thread.records.values()
    assert (first.rev, first.first_seen_seq, second.first_seen_seq) == (1, 1, 2)
    assert applied.thread.seq == 2 and [e.kind for e in applied.events] == [EventKind.ADDED] * 2
    assert first.latest.digest == digest("hello") and first.state is RecordState.LIVE


def test_a_boundary_repeat_and_a_stale_read_change_nothing() -> None:
    base = ingest(obs(body="v2", day=2, updated=5)).thread
    assert not ingest(obs(body="v2", day=2, updated=5), base=base).changed  # boundary repeat
    stale = ingest(obs(body="v1", day=2, updated=3), base=base)  # r < r0: T5
    assert not stale.changed and stale.thread is base


def test_a_newer_timestamp_with_the_same_content_updates_in_place() -> None:
    base = ingest(obs(day=2)).thread
    touched = ingest(obs(day=2, updated=4), base=base)
    record = touched.thread.records["c1"]
    assert [e.kind for e in touched.events] == [EventKind.TOUCHED]
    assert record.rev == 1 and record.latest.tracker_revision == at(4)
    assert record.latest.seen_seq == 1 and record.last_seen_seq == 2


def test_new_content_at_an_equal_or_newer_revision_appends_a_rev() -> None:
    base = ingest(obs(body="a", day=2)).thread
    tie = ingest(obs(body="b", day=2), base=base)  # T4: a coarse-clock tie, the later read wins
    newer = ingest(obs(body="c", day=2, updated=3), base=tie.thread)
    record = newer.thread.records["c1"]
    assert [r.rev for r in record.revisions] == [1, 2, 3] and record.body == "c"
    assert [r.seen_seq for r in record.revisions] == [1, 2, 3]


def test_an_observation_of_another_thread_is_ignored() -> None:
    assert not ingest(obs(issue_url="memory://acme/widgets/issues/2")).changed


def test_global_id_and_author_changes_are_kept_and_flagged() -> None:
    base = ingest(obs(global_id="g1")).thread
    moved = ingest(obs(global_id="g2", author=ThreadActor("human-bob", "human")), base=base)
    record = moved.thread.records["c1"]
    assert record.global_id == "g2" and record.author == ThreadActor("human-bob", "human")
    assert {Flag.GLOBAL_ID_CHANGED.value, Flag.AUTHOR_CHANGED.value} <= record.flags
    assert record.rev == 1 and flagged(moved.thread) == ("c1",)


def test_a_display_rename_is_not_an_author_change() -> None:
    base = ingest(obs()).thread
    renamed = ThreadActor(ALICE.id, ALICE.kind, "alice-renamed")
    assert not ingest(obs(author=renamed), base=base).changed


# -- own-message recognition (R7, T13) ---------------------------------------------------------

SELF = OwnMark(ME.hive, ME.install, "ob1_x")
OTHER = OwnMark("acme/other", ME.install, "ob1_y")


@pytest.mark.parametrize(
    ("ctx", "author", "via", "marker", "expected"),
    [
        # row 1: a receipt id wins over everything, even with no marker
        (dataclasses.replace(CTX, receipt_ids=frozenset({"c1"})), ALICE, None, None, "own/receipt"),
        # row 2: self marker + pending (or issued) outbox record + known actor
        (
            dataclasses.replace(CTX, pending=lambda o: o == "ob1_x"),
            BOT,
            None,
            SELF,
            "own/reconciled",
        ),
        (
            dataclasses.replace(CTX, issued=lambda o: o == "ob1_x"),
            BOT,
            None,
            SELF,
            "own/reconciled",
        ),
        # row 3: a configured integration posting as a bot
        (
            dataclasses.replace(CTX, integrations=frozenset({"app-1"})),
            ThreadActor("bot-ci", "bot"),
            "app-1",
            None,
            "own/integration",
        ),
        # row 3 needs the bot kind
        (
            dataclasses.replace(CTX, integrations=frozenset({"app-1"})),
            ALICE,
            "app-1",
            None,
            "external/none",
        ),
        # row 4: self marker + known actor, nothing in the outbox
        (CTX, BOT, None, SELF, "own/actor+marker"),
        # row 5: another hive's (or install's) marker
        (CTX, ALICE, None, OTHER, "peer/marker"),
        (CTX, BOT, None, dataclasses.replace(SELF, install="install-2"), "peer/marker"),
        # row 6: a known actor without a marker (a human on the shared token)
        (CTX, BOT, None, None, "external/none"),
        # row 6: a pasted self marker from an unknown actor
        (CTX, ALICE, None, SELF, "external/none"),
        # row 6: the same display name under another id is not the known actor
        (CTX, ThreadActor("human-imposter", "bot", "beadhive[bot]"), None, SELF, "external/none"),
        # row 6: a pending outbox record never makes an unknown actor's paste own
        (dataclasses.replace(CTX, pending=lambda o: True), ALICE, None, SELF, "external/none"),
        # no identity configured: no self marker can match
        (Recognition(known_actors=frozenset({BOT})), BOT, None, SELF, "peer/marker"),
    ],
)
def test_t13_the_layered_recognition_matrix(ctx, author, via, marker, expected) -> None:
    role, evidence, flags, _ = classify("c1", author, via, marker, ctx)
    assert f"{role.value}/{evidence.value}" == expected
    assert (Flag.UNLEDGERED.value in flags) == (evidence is Evidence.ACTOR_MARKER)


def test_reclassification_promotes_on_a_grown_ledger_and_never_demotes() -> None:
    base = ingest(obs(author=BOT, marker=SELF)).thread
    assert base.records["c1"].evidence is Evidence.ACTOR_MARKER
    grown = reclassify(base, dataclasses.replace(CTX, receipt_ids=frozenset({"c1"})))
    record = grown.thread.records["c1"]
    assert (record.role, record.evidence) == (Role.OWN, Evidence.RECEIPT)
    assert Flag.UNLEDGERED.value not in record.flags and record.outbox_id == "ob1_x"
    rotated = reclassify(grown.thread, Recognition(identity=ME))  # the known actor set changed
    assert not rotated.changed and rotated.thread.records["c1"].role is Role.OWN


def test_a_reconciled_binding_is_reported_once() -> None:
    ctx = dataclasses.replace(CTX, pending=lambda o: o == "ob1_x")
    first = ingest(obs(author=BOT, marker=SELF), ctx=ctx)
    reconciled = [e for e in first.events if e.kind is EventKind.RECONCILED]
    assert [(e.outbox_id, e.comment) for e in reconciled] == [("ob1_x", "c1")]
    again = ingest(obs(author=BOT, marker=SELF), base=first.thread, ctx=ctx)
    assert not again.changed


def test_own_and_peer_records_are_never_new_evidence() -> None:
    applied = ingest(
        obs("c1", author=BOT, marker=SELF),
        obs("c2", marker=OTHER),
        obs("c3"),
        obs("c4", author=BOT),
    )
    assert new_evidence(applied.thread) == ("c3", "c4")


# -- deletions (R6) ----------------------------------------------------------------------------


def _three():
    return ingest(obs("c1"), obs("c2", day=3), obs("c3", day=4)).thread


def test_an_incomplete_snapshot_never_infers_a_deletion() -> None:
    base = _three()
    assert not reconcile_comments(base, set(), complete=False, snapshot_start_seq=base.seq).changed


def test_absence_needs_the_configured_confirmations_and_a_sighting_resets_it() -> None:
    base = _three()
    once = reconcile_comments(base, {"c1", "c3"}, complete=True, snapshot_start_seq=base.seq)
    assert once.thread.records["c2"].absent_confirmations == 1
    assert once.thread.records["c2"].state is RecordState.LIVE
    back = reconcile_comments(
        once.thread, {"c1", "c2", "c3"}, complete=True, snapshot_start_seq=once.thread.seq
    )
    assert back.thread.records["c2"].absent_confirmations == 0
    assert [e.kind for e in back.events] == [EventKind.PRESENT]


def test_two_agreeing_snapshots_tombstone_and_purge_by_default() -> None:
    base = _three()
    first = reconcile_comments(base, {"c1", "c3"}, complete=True, snapshot_start_seq=base.seq)
    second = reconcile_comments(first.thread, {"c1", "c3"}, complete=True, snapshot_start_seq=9)
    record = second.thread.records["c2"]
    assert record.state is RecordState.TOMBSTONED and record.body is None
    assert record.tombstone_seq == second.thread.seq and record.revisions
    assert [e.kind for e in second.events] == [EventKind.TOMBSTONED]
    # a tombstone is not re-counted by a third agreeing snapshot
    assert not reconcile_comments(
        second.thread, {"c1", "c3"}, complete=True, snapshot_start_seq=99
    ).changed


def test_t11_a_record_first_seen_after_the_snapshot_began_is_not_a_candidate() -> None:
    base = ingest(obs("c1")).thread
    started = base.seq
    later = ingest(obs("c2", day=3), base=base).thread
    applied = reconcile_comments(later, {"c1"}, complete=True, snapshot_start_seq=started)
    assert not applied.changed and later.records["c2"].first_seen_seq > started


def test_a_snapshot_resurrects_a_tombstone_without_a_new_rev() -> None:
    policy = ThreadPolicy(deletion_confirmations=1)
    base = _three()
    dead = reconcile_comments(
        base, {"c1", "c3"}, complete=True, snapshot_start_seq=9, policy=policy
    )
    alive = reconcile_comments(dead.thread, {"c1", "c2", "c3"}, complete=True, snapshot_start_seq=9)
    record = alive.thread.records["c2"]
    assert (record.state, record.resurrected, record.rev) == (RecordState.LIVE, 1, 1)
    assert record.tombstone_seq is None


def test_a_read_resurrects_a_tombstone_and_restores_a_purged_body() -> None:
    policy = ThreadPolicy(deletion_confirmations=1)
    dead = reconcile_comments(
        _three(), {"c1", "c3"}, complete=True, snapshot_start_seq=9, policy=policy
    )
    alive = ingest(obs("c2", day=3), base=dead.thread)
    record = alive.thread.records["c2"]
    assert (record.state, record.resurrected, record.rev, record.body) == (
        RecordState.LIVE,
        1,
        1,
        "hello",
    )
    assert [e.kind for e in alive.events] == [EventKind.RESURRECTED]


def test_a_stale_read_never_resurrects() -> None:
    policy = ThreadPolicy(deletion_confirmations=1)
    base = ingest(obs("c1", day=2, updated=5)).thread
    dead = reconcile_comments(base, set(), complete=True, snapshot_start_seq=9, policy=policy)
    assert not ingest(obs("c1", day=2, updated=3), base=dead.thread).changed


# -- presence (R6 rule 6, T16) -----------------------------------------------------------------


def test_presence_needs_two_complete_snapshots_and_recovers() -> None:
    base = ingest(obs()).thread
    assert not reconcile_presence(base, set(), complete=False).changed
    once = reconcile_presence(base, set(), complete=True)
    assert once.thread.state is ThreadState.PRESENT and once.thread.missing_confirmations == 1
    twice = reconcile_presence(once.thread, set(), complete=True)
    assert twice.thread.state is ThreadState.MISSING
    assert not reconcile_presence(twice.thread, set(), complete=True).changed
    found = reconcile_presence(twice.thread, {URL}, complete=True)
    assert found.thread.state is ThreadState.PRESENT and found.thread.missing_confirmations == 0
    assert not reconcile_presence(found.thread, {URL}, complete=True).changed


# -- checkpoints (R5 rules 4, 5; T17) ----------------------------------------------------------


def test_a_withheld_or_unchanged_token_does_not_advance_the_checkpoint() -> None:
    base = thread()
    assert not advance_checkpoint(base, CheckpointScope.ISSUE, URL, None).changed
    stored = advance_checkpoint(base, CheckpointScope.ISSUE, URL, "t1")
    assert stored.changed and stored.thread.checkpoint.seq == stored.thread.seq == 1
    assert not advance_checkpoint(stored.thread, CheckpointScope.ISSUE, URL, "t1").changed
    assert checkpoint_for(stored.thread, CheckpointScope.ISSUE, URL) == "t1"
    assert checkpoint_for(base, CheckpointScope.ISSUE, URL) is None


@pytest.mark.parametrize(
    ("scope", "subject"),
    [(CheckpointScope.PROJECT, ""), (CheckpointScope.ISSUE, "memory://acme/widgets/issues/9")],
)
def test_t17_a_token_is_only_offered_to_a_query_of_its_own_scope(scope, subject) -> None:
    stored = advance_checkpoint(thread(), scope, subject, "t1").thread
    with pytest.raises(IntakeRefused) as refused:
        checkpoint_for(stored, CheckpointScope.ISSUE, URL)
    assert refused.value.reason is RefusalReason.CHECKPOINT_SCOPE


# -- the triage watermark ----------------------------------------------------------------------


def test_considering_marks_what_was_read_and_is_idempotent() -> None:
    base = ingest(obs("c1"), obs("c2", day=3)).thread
    seen = base.seq
    revised = ingest(obs("c1", body="edited", day=2, updated=4), base=base).thread
    marked = mark_considered(revised, seen)
    assert marked.changed and marked.thread.seq == revised.seq + 1
    assert marked.thread.records["c1"].considered_rev == 1  # rev 2 arrived after the read
    assert new_evidence(marked.thread) == ("c1",)
    assert not mark_considered(marked.thread, seen).changed
    everything = mark_considered(marked.thread)
    assert new_evidence(everything.thread) == () and not mark_considered(everything.thread).changed
    assert mark_considered(everything.thread, 10_000).thread is everything.thread  # clamped


def test_a_tombstone_after_the_last_pass_is_new_evidence_until_considered() -> None:
    policy = ThreadPolicy(deletion_confirmations=1)
    considered = mark_considered(_three()).thread
    dead = reconcile_comments(
        considered, {"c1", "c3"}, complete=True, snapshot_start_seq=99, policy=policy
    )
    assert new_evidence(dead.thread) == ("c2",)
    assert new_evidence(mark_considered(dead.thread).thread) == ()


# -- privacy (R4, T19) -------------------------------------------------------------------------


def test_a_published_store_is_refused_unless_acknowledged() -> None:
    require_private_store(StorePosture(published=False))
    require_private_store(StorePosture(published=True, acknowledged=True))
    with pytest.raises(IntakeRefused) as refused:
        require_private_store(StorePosture(published=True))
    assert refused.value.reason is RefusalReason.PUBLISHED_STORE


@pytest.mark.parametrize(
    ("marker", "found"),
    [
        (OwnMark("acme/widgets", "8c1f2d4e-0000-4000-8000-000000000001", "ob1_" + "a" * 26), ()),
        (OwnMark("beadhive/beadhive", "install-1", "ob1_" + "7" * 26), ()),
        (OwnMark("acme/widgets", "install-1", "bh-x6f9l"), ("bh-x6f9l",)),
        (OwnMark("acme/widgets", "install-1", "from-hq-ab12.3"), ("hq-ab12.3",)),
        (OwnMark("acme/bh-rkmq1", "install-1", "ob1_z"), ("bh-rkmq1",)),
    ],
)
def test_t19_a_marker_carrying_a_fleet_bead_id_is_detected(marker, found) -> None:
    assert marker_bead_ids(marker, ("bh", "hq", "acme")) == found


def test_policy_thresholds_are_validated() -> None:
    for bad in (
        {"deletion_confirmations": 0},
        {"missing_confirmations": 0},
        {"max_body_chars": -1},
    ):
        with pytest.raises(ValueError):
            ThreadPolicy(**bad)


# -- the intake.thread document ----------------------------------------------------------------


def _rich():
    ctx = dataclasses.replace(CTX, pending=lambda o: o == "ob1_x")
    base = ingest(
        obs("c1", global_id="g1"),
        obs("c2", author=BOT, marker=SELF, via="app-9"),
        obs("c3", author=None),
        ctx=ctx,
        policy=ThreadPolicy(max_body_chars=3),
    ).thread
    edited = ingest(obs("c1", body="edited", day=2, updated=5), base=base).thread
    dead = reconcile_comments(edited, {"c1", "c2"}, complete=True, snapshot_start_seq=99)
    dead = reconcile_comments(dead.thread, {"c1", "c2"}, complete=True, snapshot_start_seq=99)
    stamped = advance_checkpoint(dead.thread, CheckpointScope.ISSUE, URL, "memory:x").thread
    missing = reconcile_presence(stamped, set(), complete=True).thread
    return mark_considered(missing, 3).thread


def test_the_document_round_trips_every_field() -> None:
    rich = _rich()
    assert rich.records["c3"].state is RecordState.TOMBSTONED
    document = thread_to_document(rich)
    assert document["schema"] == 1 and set(document["comments"]) == {"c1", "c2", "c3"}
    assert thread_from_document(document) == rich


def test_the_document_carries_no_tracker_vocabulary() -> None:
    def keys(value):
        if isinstance(value, dict):
            for k, v in value.items():
                yield k
                yield from keys(v)
        elif isinstance(value, list):
            for v in value:
                yield from keys(v)

    names = set(keys(thread_to_document(_rich()))) - {"c1", "c2", "c3"}
    assert not {n for n in names if any(w in n for w in ("github", "node", "login", "html"))}


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        {"schema": 1},
        {"schema": "x", "binding": {}, "issue": {}, "seq": 0},
        {**thread_to_document(thread()), "seq": "many"},
        {**thread_to_document(thread()), "comments": {"c1": {"revisions": []}}},
        {**thread_to_document(thread()), "state": "vanished"},
    ],
)
def test_a_malformed_document_fails_closed(document) -> None:
    with pytest.raises(ThreadDocumentError):
        thread_from_document(document)


def test_a_newer_schema_is_refused_rather_than_rewritten() -> None:
    with pytest.raises(ThreadDocumentError, match="newer"):
        thread_from_document({**thread_to_document(thread()), "schema": 2})
