"""The conversation model: one thread per bound report, stored as ``intake.thread``.

Pure rules over intake-owned records (``bh-fptob`` R1 to R7, external-intake-module-adr.md section
9). Standard library only: the domain never sees the tracker port's types; the application layer
translates a ``TrackerComment`` into an :class:`Observation` (resolution X2).

The rules, in short:

* **Identity.** A comment record is keyed on the tracker's comment id; its ``global_id`` is kept and
  never compared. An author is ``(id, kind)``; ``display`` is never compared.
* **Ordering is local.** ``Thread.seq`` is a local integer bumped once per applied change; every
  ``*_seq`` field is a value of it. Content order uses the tracker's own revision timestamp and a
  body digest. Nothing here reads a clock.
* **Revisions** (R5 rule 2). Stale reads and boundary repeats change nothing; a newer timestamp
  with the same digest updates in place; a different digest at a revision not older than the
  latest appends a new local ``rev`` (a coarse-clock tie is won by the later read).
* **Checkpoints** (R5 rules 4, 5). The thread's checkpoint is advanced only by
  :func:`advance_checkpoint`, whose result is written in the same document write as the records
  of every page before it; a stored checkpoint is only handed back to a query of the same scope.
* **Deletions** (R6) are inferred only from complete id snapshots (:func:`reconcile_comments`),
  never from a ``since`` read; a record first seen after the snapshot began is never a
  candidate; ``deletion_confirmations`` agreeing snapshots tombstone it; any later sighting
  resurrects it. The same two-confirmation rule marks a whole thread ``missing``.
* **Own messages** (R7) are classified by layered evidence; a marker alone is never enough and a
  known actor without a marker is never own, so a human posting on Beadhive's shared token stays
  ``external``. Classification uses the evidence of the FIRST sighting, so editing a marker into
  or out of a comment later never changes its role, and an own record is never demoted.
* **Triage watermark.** ``considered_rev`` (per record) and ``considered_seq`` (per thread) say
  what a triage pass has read; :func:`new_evidence` derives what it has not. Neither ever touches
  the ``intake`` dimension.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .errors import IntakeRefused, RefusalReason

THREAD_SCHEMA = 1


class Role(StrEnum):
    """How a comment record relates to Beadhive (``bh-fptob`` R1, R7)."""

    EXTERNAL = "external"
    OWN = "own"
    PEER = "peer"  # another hive's (or install's) marked message


class Evidence(StrEnum):
    """Which R7 row decided a record's role."""

    RECEIPT = "receipt"  # row 1: the comment id is on an outbox receipt
    RECONCILED = "reconciled"  # row 2: self marker + pending/issued outbox record + known actor
    INTEGRATION = "integration"  # row 3: a configured integration posted it as a bot
    ACTOR_MARKER = "actor+marker"  # row 4: self marker + known actor, no outbox record
    MARKER = "marker"  # row 5: a marker naming another hive or install
    NONE = "none"  # row 6: anything else


class RecordState(StrEnum):
    LIVE = "live"
    TOMBSTONED = "tombstoned"


class ThreadState(StrEnum):
    PRESENT = "present"
    MISSING = "missing"  # absent from enough complete issue snapshots; surfaced to triage


class CheckpointScope(StrEnum):
    ISSUE = "issue"  # a per-issue comment read; the subject is the thread's URL
    PROJECT = "project"  # a project-wide sweep; lives in the CheckpointStore, never in a thread


class Flag(StrEnum):
    """Anomalies kept on a record for triage; none of them changes its role."""

    GLOBAL_ID_CHANGED = "global_id_changed"
    AUTHOR_CHANGED = "author_changed"
    EDITED_EXTERNALLY = "edited_externally"  # an own record whose content changed on the tracker
    UNLEDGERED = "unledgered"  # own by actor+marker, but no outbox record matches
    TRUNCATED = "truncated"  # the stored body is capped; the digest is of the whole body


#: Flags that put a record in front of triage (:func:`flagged`).
TRIAGE_FLAGS = frozenset(
    {Flag.GLOBAL_ID_CHANGED, Flag.AUTHOR_CHANGED, Flag.EDITED_EXTERNALLY, Flag.UNLEDGERED}
)


class EventKind(StrEnum):
    ADDED = "added"
    REVISED = "revised"  # a new content state: a new rev
    TOUCHED = "touched"  # the tracker revision advanced with the same content
    RESURRECTED = "resurrected"
    RECLASSIFIED = "reclassified"
    RECONCILED = "reconciled"  # row 2 bound a remote comment id to an outbox record
    ABSENT = "absent"  # one complete snapshot lacked the id
    PRESENT = "present"  # a sighting reset the absence count
    TOMBSTONED = "tombstoned"
    MISSING = "missing"  # the thread's issue is confirmed absent
    FOUND = "found"  # the thread's issue is present again
    CHECKPOINT = "checkpoint"
    CONSIDERED = "considered"


# -- records ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class ThreadActor:
    """An author: the tracker's immutable id plus its kind. ``display`` is never compared."""

    id: str
    kind: str
    display: str = field(default="", compare=False)


@dataclass(frozen=True)
class OwnMark:
    """A decoded own-message marker (the adapter owns the encoding; it never names a bead)."""

    hive: str
    install: str
    outbox_id: str


@dataclass(frozen=True)
class ThreadIssue:
    """The external issue a thread belongs to. ``url`` is the thread's identity."""

    identifier: str
    url: str
    internal_id: str | None = None
    global_id: str | None = None


@dataclass(frozen=True)
class Observation:
    """One comment as one read reported it (the application maps a ``TrackerComment``)."""

    id: str
    issue_url: str
    body: str
    created_at: datetime
    updated_at: datetime  # the TRACKER's revision timestamp, never the local clock
    global_id: str | None = None
    author: ThreadActor | None = None
    via: str | None = None  # the integration id the tracker says posted it
    marker: OwnMark | None = None


@dataclass(frozen=True)
class CommentRevision:
    """One observed content state: local ``rev``, the tracker revision and the body digest."""

    rev: int
    tracker_revision: datetime
    digest: str
    seen_seq: int


@dataclass(frozen=True)
class CommentRecord:
    """Beadhive's stored view of one tracker comment in one thread."""

    id: str
    global_id: str | None
    author: ThreadActor | None
    via: str | None
    marker: OwnMark | None  # as first seen: the classification evidence
    role: Role
    evidence: Evidence
    created_at: datetime
    body: str | None  # the latest body; None once purged on tombstone
    revisions: tuple[CommentRevision, ...]
    first_seen_seq: int
    last_seen_seq: int
    state: RecordState = RecordState.LIVE
    tombstone_seq: int | None = None
    absent_confirmations: int = 0
    resurrected: int = 0
    considered_rev: int | None = None
    outbox_id: str | None = None  # the outbox record this comment is bound to, when known
    flags: frozenset[str] = frozenset()

    @property
    def latest(self) -> CommentRevision:
        return self.revisions[-1]

    @property
    def rev(self) -> int:
        return self.latest.rev


@dataclass(frozen=True)
class ThreadCheckpoint:
    """An opaque resume token with the scope of the query that produced it."""

    scope: CheckpointScope
    subject: str  # the thread URL for an issue scope
    token: str
    seq: int  # the thread seq at which it was stored


@dataclass(frozen=True)
class Thread:
    """The ``intake.thread`` document of one report bead. ``records`` is keyed by comment id in
    first-seen order and is never mutated: every rule returns a new :class:`Thread`."""

    tracker: str
    project: str
    issue: ThreadIssue
    seq: int = 0
    checkpoint: ThreadCheckpoint | None = None
    state: ThreadState = ThreadState.PRESENT
    missing_confirmations: int = 0
    considered_seq: int = 0
    records: Mapping[str, CommentRecord] = field(default_factory=dict)
    schema: int = THREAD_SCHEMA


@dataclass(frozen=True)
class ThreadPolicy:
    """The ``intake.external.comments.*`` semantics (``bh-fptob`` R9). Defaults are the ADR's."""

    deletion_confirmations: int = 2
    missing_confirmations: int = 2
    purge_deleted_bodies: bool = True
    max_body_chars: int | None = None

    def __post_init__(self) -> None:
        if self.deletion_confirmations < 1 or self.missing_confirmations < 1:
            raise ValueError("confirmation thresholds must be at least 1")
        if self.max_body_chars is not None and self.max_body_chars < 0:
            raise ValueError("max_body_chars must be non-negative")


#: The ADR's defaults, shared by every rule that takes a policy.
DEFAULT_POLICY = ThreadPolicy()


@dataclass(frozen=True)
class StorePosture:
    """Whether the operational store a thread would be written to is published (a single-store
    hive, or an internal remote on the code origin), and whether the operator acknowledged it."""

    published: bool
    acknowledged: bool = False


@dataclass(frozen=True)
class OwnIdentity:
    """This hive's marker identity: the repository identity and the install UUID."""

    hive: str
    install: str


def _never(_outbox_id: str) -> bool:
    return False


@dataclass(frozen=True)
class Recognition:
    """The evidence own-message recognition reads besides the record itself (R7)."""

    identity: OwnIdentity | None = None
    known_actors: frozenset[ThreadActor] = frozenset()
    receipt_ids: frozenset[str] = frozenset()
    pending: Callable[[str], bool] = _never
    issued: Callable[[str], bool] = _never
    integrations: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ThreadEvent:
    kind: EventKind
    comment: str = ""
    seq: int = 0
    outbox_id: str = ""


@dataclass(frozen=True)
class Applied:
    """A rule's result: the new thread and what changed (empty when nothing did)."""

    thread: Thread
    events: tuple[ThreadEvent, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.events)


# -- the port's requests and outcomes --------------------------------------------------------


@dataclass(frozen=True)
class ThreadRequest:
    """Refresh (``ingest_thread``) or reconcile (``reconcile_thread``) one report's thread."""

    report: str
    actor: str


@dataclass(frozen=True)
class PresenceRequest:
    """Check, with one issue snapshot, that the issues of ``reports``' threads still exist."""

    reports: tuple[str, ...]
    actor: str


@dataclass(frozen=True)
class ConsiderRequest:
    """A triage pass read ``report``'s thread as it stood at ``through_seq`` (None: as it is
    now). Pass the ``seq`` of the :class:`ThreadView` that was read, so a comment that arrived
    after the read stays new evidence."""

    report: str
    actor: str
    through_seq: int | None = None


@dataclass(frozen=True)
class ThreadOutcome:
    """What one thread operation did. ``written`` is True only after a durable document write;
    ``complete`` is False when a tracker read failed part-way (whatever was ingested before it is
    written, but the checkpoint covers only fully ingested pages)."""

    report: str
    seq: int
    written: bool = False
    complete: bool = True
    checkpoint_advanced: bool = False
    events: tuple[ThreadEvent, ...] = ()
    detail: str = ""

    @property
    def reconciled(self) -> tuple[tuple[str, str], ...]:
        """``(outbox_id, comment_id)`` pairs row 2 bound: the outbox's unknown-outcome recovery
        (the sender must never post those records again)."""
        return tuple(
            (e.outbox_id, e.comment) for e in self.events if e.kind is EventKind.RECONCILED
        )


@dataclass(frozen=True)
class PresenceOutcome:
    complete: bool
    outcomes: tuple[ThreadOutcome, ...] = ()


@dataclass(frozen=True)
class ThreadView:
    """A report's thread for a reader (triage, a skill): the document plus the derived reads."""

    report: str
    thread: Thread
    new_evidence: tuple[str, ...] = ()
    flagged: tuple[str, ...] = ()

    @property
    def seq(self) -> int:
        return self.thread.seq


# -- small helpers ---------------------------------------------------------------------------


def digest(body: str) -> str:
    """The content digest of a comment body."""
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _capped(body: str, policy: ThreadPolicy) -> tuple[str, bool]:
    cap = policy.max_body_chars
    if cap is not None and len(body) > cap:
        return body[:cap], True
    return body, False


def _with_flag(flags: frozenset[str], flag: Flag, on: bool = True) -> frozenset[str]:
    return flags | {flag.value} if on else flags - {flag.value}


def new_thread(tracker: str, project: str, issue: ThreadIssue) -> Thread:
    """An empty thread (``seq`` 0): what a report without an ``intake.thread`` document holds."""
    return Thread(tracker, project, issue)


# -- own-message recognition (R7) ------------------------------------------------------------


def _self_marker(marker: OwnMark | None, identity: OwnIdentity | None) -> bool:
    return (
        marker is not None
        and identity is not None
        and (marker.hive, marker.install) == (identity.hive, identity.install)
    )


def classify(
    comment_id: str,
    author: ThreadActor | None,
    via: str | None,
    marker: OwnMark | None,
    ctx: Recognition,
) -> tuple[Role, Evidence, frozenset[str], str | None]:
    """``(role, evidence, flags, outbox_id)`` for one record's evidence, in R7's order."""
    known = author is not None and author in ctx.known_actors
    mine = _self_marker(marker, ctx.identity)
    outbox_id = marker.outbox_id if mine and marker is not None else None
    if comment_id in ctx.receipt_ids:
        return Role.OWN, Evidence.RECEIPT, frozenset(), outbox_id
    if mine and known and outbox_id and (ctx.pending(outbox_id) or ctx.issued(outbox_id)):
        return Role.OWN, Evidence.RECONCILED, frozenset(), outbox_id
    if via is not None and via in ctx.integrations and author is not None and author.kind == "bot":
        return Role.OWN, Evidence.INTEGRATION, frozenset(), None
    if mine and known:
        return Role.OWN, Evidence.ACTOR_MARKER, frozenset({Flag.UNLEDGERED.value}), outbox_id
    if marker is not None and not mine:
        return Role.PEER, Evidence.MARKER, frozenset(), None
    return Role.EXTERNAL, Evidence.NONE, frozenset(), None


def _reclassified(record: CommentRecord, ctx: Recognition) -> CommentRecord | None:
    """The record under the current evidence, or None when nothing changes. An own record is
    never demoted: the evidence that made it own was valid when it was seen, and demoting it
    would surface Beadhive's own message to triage as new evidence."""
    role, evidence, flags, outbox_id = classify(
        record.id, record.author, record.via, record.marker, ctx
    )
    if record.role is Role.OWN and role is not Role.OWN:
        return None
    kept = frozenset(record.flags) - {Flag.UNLEDGERED.value}
    new_flags = kept | flags
    new_outbox = outbox_id or record.outbox_id
    if (role, evidence, new_flags, new_outbox) == (
        record.role,
        record.evidence,
        frozenset(record.flags),
        record.outbox_id,
    ):
        return None
    return dataclasses.replace(
        record, role=role, evidence=evidence, flags=new_flags, outbox_id=new_outbox
    )


def reclassify(thread: Thread, ctx: Recognition) -> Applied:
    """Re-derive every record's role from its stored evidence: a pure recomputation (for when
    the outbox ledger grew), never a tracker read."""
    seq, records, events = thread.seq, dict(thread.records), []
    for comment_id, record in thread.records.items():
        updated = _reclassified(record, ctx)
        if updated is None:
            continue
        seq += 1
        records[comment_id] = updated
        events.append(ThreadEvent(EventKind.RECLASSIFIED, comment_id, seq))
        if updated.evidence is Evidence.RECONCILED and record.evidence is not Evidence.RECONCILED:
            events.append(
                ThreadEvent(EventKind.RECONCILED, comment_id, seq, updated.outbox_id or "")
            )
    if not events:
        return Applied(thread)
    return Applied(dataclasses.replace(thread, seq=seq, records=records), tuple(events))


# -- revisions (R5) --------------------------------------------------------------------------


def _first_sight(obs: Observation, ctx: Recognition, policy: ThreadPolicy, seq: int):
    role, evidence, flags, outbox_id = classify(obs.id, obs.author, obs.via, obs.marker, ctx)
    body, truncated = _capped(obs.body, policy)
    record = CommentRecord(
        id=obs.id,
        global_id=obs.global_id,
        author=obs.author,
        via=obs.via,
        marker=obs.marker,
        role=role,
        evidence=evidence,
        created_at=obs.created_at,
        body=body,
        revisions=(CommentRevision(1, obs.updated_at, digest(obs.body), seq),),
        first_seen_seq=seq,
        last_seen_seq=seq,
        outbox_id=outbox_id,
        flags=_with_flag(flags, Flag.TRUNCATED, truncated),
    )
    events = [ThreadEvent(EventKind.ADDED, obs.id, seq)]
    if evidence is Evidence.RECONCILED:
        events.append(ThreadEvent(EventKind.RECONCILED, obs.id, seq, outbox_id or ""))
    return record, events


def _resighted(
    record: CommentRecord, obs: Observation, policy: ThreadPolicy, seq: int
) -> tuple[CommentRecord, list[EventKind]] | None:
    """``record`` after one more sighting at ``seq``, or None when the sighting changes nothing
    (a stale read or a boundary repeat)."""
    latest = record.latest
    if obs.updated_at < latest.tracker_revision:
        return None  # stale: never moves a record back, nor resurrects it
    kinds: list[EventKind] = []
    changes: dict = {}
    flags = frozenset(record.flags)
    body_digest = digest(obs.body)
    body, truncated = _capped(obs.body, policy)
    if record.state is RecordState.TOMBSTONED:
        kinds.append(EventKind.RESURRECTED)
        changes.update(
            state=RecordState.LIVE,
            tombstone_seq=None,
            absent_confirmations=0,
            resurrected=record.resurrected + 1,
        )
    elif record.absent_confirmations:
        kinds.append(EventKind.PRESENT)
        changes["absent_confirmations"] = 0
    if body_digest != latest.digest:  # a new content state (r >= r0; a tie is a coarse clock)
        kinds.append(EventKind.REVISED)
        revision = CommentRevision(latest.rev + 1, obs.updated_at, body_digest, seq)
        changes["revisions"] = (*record.revisions, revision)
        changes["body"] = body
        flags = _with_flag(flags, Flag.TRUNCATED, truncated)
        if record.role is Role.OWN:
            flags = _with_flag(flags, Flag.EDITED_EXTERNALLY)
    else:
        if obs.updated_at > latest.tracker_revision:
            kinds.append(EventKind.TOUCHED)
            touched = dataclasses.replace(latest, tracker_revision=obs.updated_at)
            changes["revisions"] = (*record.revisions[:-1], touched)
        if record.body is None:  # purged on tombstone: the tracker still has it
            changes["body"] = body
            flags = _with_flag(flags, Flag.TRUNCATED, truncated)
    if obs.global_id is not None and obs.global_id != record.global_id:
        changes["global_id"] = obs.global_id
        if record.global_id is not None:
            flags = _with_flag(flags, Flag.GLOBAL_ID_CHANGED)
    if obs.author is not None and obs.author != record.author:
        changes["author"] = obs.author
        flags = _with_flag(flags, Flag.AUTHOR_CHANGED)
    if flags != frozenset(record.flags):
        changes["flags"] = flags
    if not changes:
        return None
    if not kinds:
        kinds.append(EventKind.TOUCHED)
    return dataclasses.replace(record, last_seen_seq=seq, **changes), kinds


def apply_observations(
    thread: Thread,
    observations: Iterable[Observation],
    ctx: Recognition,
    policy: ThreadPolicy = DEFAULT_POLICY,
) -> Applied:
    """Upsert one page of observations on ``(id, revision, digest)``, then re-derive roles.

    An observation of another thread is ignored. The checkpoint is never touched here."""
    seq, records, events = thread.seq, dict(thread.records), []
    for obs in observations:
        if obs.issue_url != thread.issue.url:
            continue
        record = records.get(obs.id)
        if record is None:
            seq += 1
            records[obs.id], added = _first_sight(obs, ctx, policy, seq)
            events.extend(added)
            continue
        resighted = _resighted(record, obs, policy, seq + 1)
        if resighted is None:
            continue
        seq += 1
        records[obs.id], kinds = resighted
        events.extend(ThreadEvent(kind, obs.id, seq) for kind in kinds)
    upserted = dataclasses.replace(thread, seq=seq, records=records) if events else thread
    again = reclassify(upserted, ctx)
    return Applied(again.thread, (*events, *again.events))


# -- checkpoints (R5 rules 4, 5) -------------------------------------------------------------


def checkpoint_for(thread: Thread, scope: CheckpointScope, subject: str) -> str | None:
    """The stored token to resume a ``scope`` query of ``subject`` from (None: from the start).

    A token stored for another scope or subject is refused before any read: tokens are opaque,
    so one is never offered to a query it did not come from."""
    stored = thread.checkpoint
    if stored is None:
        return None
    if (stored.scope, stored.subject) != (scope, subject):
        raise IntakeRefused(
            RefusalReason.CHECKPOINT_SCOPE,
            f"the stored checkpoint is for a {stored.scope.value} read, not this {scope.value} "
            "read; it is never offered to another query",
        )
    return stored.token


def advance_checkpoint(
    thread: Thread, scope: CheckpointScope, subject: str, token: str | None
) -> Applied:
    """Store ``token`` as the thread's checkpoint. ``None`` (a page that withholds it) and an
    unchanged token change nothing. The caller writes the result in the same document write as
    the records of every page the token covers."""
    stored = thread.checkpoint
    unchanged = stored is not None and (stored.scope, stored.subject, stored.token) == (
        scope,
        subject,
        token,
    )
    if token is None or unchanged:
        return Applied(thread)
    seq = thread.seq + 1
    checkpoint = ThreadCheckpoint(scope, subject, token, seq)
    return Applied(
        dataclasses.replace(thread, seq=seq, checkpoint=checkpoint),
        (ThreadEvent(EventKind.CHECKPOINT, "", seq),),
    )


# -- deletions and presence (R6) -------------------------------------------------------------


def reconcile_comments(
    thread: Thread,
    ids: Collection[str],
    *,
    complete: bool,
    snapshot_start_seq: int,
    policy: ThreadPolicy = DEFAULT_POLICY,
) -> Applied:
    """Apply one comment-id snapshot. Only a ``complete`` snapshot counts; a live record is a
    deletion candidate only when absent AND first seen at or before ``snapshot_start_seq``."""
    if not complete:
        return Applied(thread)
    present = frozenset(ids)
    seq, records, events = thread.seq, dict(thread.records), []
    for comment_id, record in thread.records.items():
        if comment_id in present:
            if record.state is RecordState.TOMBSTONED:
                seq += 1
                records[comment_id] = dataclasses.replace(
                    record,
                    state=RecordState.LIVE,
                    tombstone_seq=None,
                    absent_confirmations=0,
                    resurrected=record.resurrected + 1,
                    last_seen_seq=seq,
                )
                events.append(ThreadEvent(EventKind.RESURRECTED, comment_id, seq))
            elif record.absent_confirmations:
                seq += 1
                records[comment_id] = dataclasses.replace(
                    record, absent_confirmations=0, last_seen_seq=seq
                )
                events.append(ThreadEvent(EventKind.PRESENT, comment_id, seq))
            continue
        if record.state is not RecordState.LIVE or record.first_seen_seq > snapshot_start_seq:
            continue
        seq += 1
        absent = record.absent_confirmations + 1
        if absent >= policy.deletion_confirmations:
            records[comment_id] = dataclasses.replace(
                record,
                state=RecordState.TOMBSTONED,
                tombstone_seq=seq,
                absent_confirmations=absent,
                body=None if policy.purge_deleted_bodies else record.body,
            )
            events.append(ThreadEvent(EventKind.TOMBSTONED, comment_id, seq))
        else:
            records[comment_id] = dataclasses.replace(record, absent_confirmations=absent)
            events.append(ThreadEvent(EventKind.ABSENT, comment_id, seq))
    if not events:
        return Applied(thread)
    return Applied(dataclasses.replace(thread, seq=seq, records=records), tuple(events))


def reconcile_presence(
    thread: Thread,
    issue_urls: Collection[str],
    *,
    complete: bool,
    policy: ThreadPolicy = DEFAULT_POLICY,
) -> Applied:
    """Apply one issue-id snapshot to the thread's own presence. A thread absent from
    ``missing_confirmations`` complete snapshots is ``missing``; it is surfaced to triage and
    nothing else changes (the report is never closed or re-triaged here)."""
    if not complete:
        return Applied(thread)
    if thread.issue.url in issue_urls:
        if thread.state is ThreadState.PRESENT and not thread.missing_confirmations:
            return Applied(thread)
        seq = thread.seq + 1
        found = dataclasses.replace(
            thread, seq=seq, state=ThreadState.PRESENT, missing_confirmations=0
        )
        return Applied(found, (ThreadEvent(EventKind.FOUND, "", seq),))
    if thread.state is ThreadState.MISSING:
        return Applied(thread)
    seq, absent = thread.seq + 1, thread.missing_confirmations + 1
    state = ThreadState.MISSING if absent >= policy.missing_confirmations else ThreadState.PRESENT
    kind = EventKind.MISSING if state is ThreadState.MISSING else EventKind.ABSENT
    updated = dataclasses.replace(thread, seq=seq, state=state, missing_confirmations=absent)
    return Applied(updated, (ThreadEvent(kind, "", seq),))


# -- the triage watermark --------------------------------------------------------------------


def mark_considered(thread: Thread, through_seq: int | None = None) -> Applied:
    """Record that a triage pass read the thread as it stood at ``through_seq`` (default: now).

    Each record's ``considered_rev`` becomes its highest ``rev`` seen at or before that seq, and
    the thread's ``considered_seq`` covers tombstones up to it. A revision that arrived after the
    pass read the thread stays new evidence. ``through_seq`` beyond the thread's seq is clamped.
    The ``intake`` dimension is never touched."""
    through = thread.seq if through_seq is None else min(through_seq, thread.seq)
    records, moved = dict(thread.records), False
    for comment_id, record in thread.records.items():
        seen = [r.rev for r in record.revisions if r.seen_seq <= through]
        if seen and (record.considered_rev is None or max(seen) > record.considered_rev):
            records[comment_id] = dataclasses.replace(record, considered_rev=max(seen))
            moved = True
        tombstoned = record.tombstone_seq
        if tombstoned is not None and thread.considered_seq < tombstoned <= through:
            moved = True
    if not moved:  # nothing unread up to ``through``: idempotent, no write
        return Applied(thread)
    seq = thread.seq + 1
    considered = dataclasses.replace(
        thread, seq=seq, records=records, considered_seq=max(through, thread.considered_seq)
    )
    return Applied(considered, (ThreadEvent(EventKind.CONSIDERED, "", seq),))


def new_evidence(thread: Thread) -> tuple[str, ...]:
    """Comment ids triage has not considered: a live external record with a ``rev`` above its
    ``considered_rev`` (or never considered), or an external record tombstoned after the last
    triage pass. Own and peer records are context, never evidence."""
    found: list[str] = []
    for comment_id, record in thread.records.items():
        if record.role is not Role.EXTERNAL:
            continue
        if record.state is RecordState.LIVE:
            if record.considered_rev is None or record.rev > record.considered_rev:
                found.append(comment_id)
        elif (record.tombstone_seq or 0) > thread.considered_seq:
            found.append(comment_id)
    return tuple(found)


def flagged(thread: Thread) -> tuple[str, ...]:
    """Comment ids carrying an anomaly triage should see (:data:`TRIAGE_FLAGS`)."""
    return tuple(
        comment_id
        for comment_id, record in thread.records.items()
        if TRIAGE_FLAGS & frozenset(record.flags)
    )


# -- privacy ---------------------------------------------------------------------------------


def require_private_store(posture: StorePosture) -> None:
    """Refuse to ingest into an operational store that would be published, unless the operator
    acknowledged it (``privacy.allow_published_operational_store``; ``bh-fptob`` R4)."""
    if posture.published and not posture.acknowledged:
        raise IntakeRefused(
            RefusalReason.PUBLISHED_STORE,
            "this hive's operational store would be published (a single-store hive, or an "
            "internal remote on the code origin); partition the hive's stores or set "
            "intake.external.privacy.allow_published_operational_store",
        )


def marker_bead_ids(marker: OwnMark, prefixes: Iterable[str]) -> tuple[str, ...]:
    """Every bead-id-shaped value of one of ``prefixes`` inside ``marker``'s fields (T19): a
    marker is published with every own comment, so it must never carry one."""
    found: list[str] = []
    for prefix in sorted(set(prefixes)):
        pattern = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(prefix)}-[a-z0-9]{{2,}}(?:\.[0-9]+)*(?![A-Za-z0-9])"
        )
        for value in (marker.hive, marker.install, marker.outbox_id):
            found.extend(pattern.findall(value))
    return tuple(found)


__all__ = [
    "DEFAULT_POLICY",
    "THREAD_SCHEMA",
    "TRIAGE_FLAGS",
    "Applied",
    "CheckpointScope",
    "CommentRecord",
    "CommentRevision",
    "ConsiderRequest",
    "EventKind",
    "Evidence",
    "Flag",
    "Observation",
    "OwnIdentity",
    "OwnMark",
    "PresenceOutcome",
    "PresenceRequest",
    "Recognition",
    "RecordState",
    "Role",
    "StorePosture",
    "Thread",
    "ThreadActor",
    "ThreadCheckpoint",
    "ThreadEvent",
    "ThreadIssue",
    "ThreadOutcome",
    "ThreadPolicy",
    "ThreadRequest",
    "ThreadState",
    "ThreadView",
    "advance_checkpoint",
    "apply_observations",
    "checkpoint_for",
    "classify",
    "digest",
    "flagged",
    "mark_considered",
    "marker_bead_ids",
    "new_evidence",
    "new_thread",
    "reclassify",
    "reconcile_comments",
    "reconcile_presence",
    "require_private_store",
]
