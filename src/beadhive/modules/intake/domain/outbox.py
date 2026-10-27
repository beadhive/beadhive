"""The outbox: communication records and their per-operation state machine, O1 to O19.

Pure rules over intake-owned records (``bh-6wj9y`` R4 to R7, external-intake-module-adr.md
sections 10, 11 and 13), stored as the ``intake.outbox`` document on the report bead. Standard
library only: the application layer translates the tracker port's ``WriteGrant``,
``WriteReceipt``, ``CapabilityReport`` and comment listings to and from these records
(resolution X2). Nothing here sends, reads a tracker, writes a store or reads a clock; the
supervised sender, the store write under ``IntakeLock`` with its ``seq`` re-check, and the CLI
verbs are later beads.

**Records.** A :class:`OutboxRecord` is keyed by an opaque ``outbox_id`` (``ob1_`` plus 26 base32
characters, never derived from a bead id) and identified by its stable :class:`DraftKey`
``(binding_url, event, source_revision)``: drafting an existing key is a no-op (O2), so a re-run
trigger never drafts twice. It carries its target binding, semantic event, supersession
``group``, the explicit publish intent (:class:`PublicFacts` and :class:`Message`: the only values
a render reads; a template has no path to the report, the work, notes or the thread), and at most
one operation of each kind (``comment.create``, ``labels.change``) with its own state.

**The machine** is per operation, because each write is its own gate:

====  ===================================  ==============================  ====================
rule  from                                 event                           to
====  ===================================  ==============================  ====================
O1    (none)                               draft                           drafted
O2    any                                  draft with an existing key      unchanged, no write
O3    drafted                              check finds a leak / missing    held
O4    held                                 edit, or override a finding     drafted (re-checked)
O5    drafted                              approve(digest), operator only  approved + grant
O6    drafted, held, approved              a newer record in the group     superseded
O7    approved                             render or binding moved         drafted, grant void
O8    drafted, held, approved, failed      discard                         abandoned
O9    approved                             send (write-ahead attempt)      sending
O10   sending                              receipt                         sent
O11   sending                              proof nothing was written       failed
O12   sending                              ambiguous result                unknown
O13   sending                              holder gone or epoch moved      unknown
O14   unknown                              own marker observed             sent (reconciled)
O15   unknown                              complete snapshot, no marker    unknown, absences+1
O16   unknown                              k absences                      failed(absent)
O17   failed                               retry, operator only            drafted
O18   unknown                              discard(accept_unknown)         abandoned
O19   abandoned(unknown), failed(absent)   marker observed late            sent (late)
====  ===================================  ==============================  ====================

Absent on purpose: ``unknown -> approved|drafted``, ``sending|unknown -> superseded``, any
automatic ``-> approved`` and any transition out of ``sent`` (a second marked comment on a ``sent``
operation is flagged ``duplicate_observed``, never "fixed"). Hence a post is reachable only from
``approved`` and ``approved`` only through an operator approval minting a fresh grant.

Every rule takes a document and returns a new one (:class:`OutboxApplied`); the input is never
mutated. One applied call is one document write: ``seq`` is bumped once and every
:class:`Step` it appends to an operation's history carries that ``seq``. A refused call raises
:class:`~.errors.IntakeRefused` and changes nothing.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from .delivery import NOTIFIED_BY_EVENT, CompletionPolicy, completion_event, delivery_complete
from .errors import IntakeRefused, RefusalReason
from .leaks import LeakFinding, LeakPolicy, Override, Scanner, check, unresolved
from .models import IssueBinding
from .thread import ThreadActor

OUTBOX_SCHEMA = 1
OUTBOX_ID_PREFIX = "ob1_"
GRANT_ID_PREFIX = "gr1_"
_OPAQUE_BYTES = 16  # 128 bits: 26 base32 characters
_OPAQUE = re.compile(r"^(?:ob1|gr1)_[a-z2-7]{26}$")

#: Seat prefixes that are agents, never a human operator (``bh-6wj9y`` R5 O5; legacy spellings
#: included). A policy may extend them; an empty actor is never an operator either.
AGENT_SEAT_PREFIXES = ("dev/", "disp/", "crew/", "coord/")
#: The read an approval needs usable, so that an unknown outcome can be reconciled (O5).
COMMENTS_READ = "comments.read"
#: The read a labels approval additionally needs usable: labels carry no marker, so an unknown
#: outcome is reconciled by reading the issue's labels back.
ISSUES_READ = "issues.read"
#: The supersession scope of milestone records; a reply is its own group.
MILESTONE_GROUP = "milestone"
#: ``fail_reason`` of an operation that k complete snapshots proved absent (O16).
ABSENT = "absent"


def _opaque(prefix: str, raw: bytes) -> str:
    if len(raw) != _OPAQUE_BYTES:
        raise ValueError(f"an opaque id needs exactly {_OPAQUE_BYTES} random bytes")
    return prefix + base64.b32encode(raw).decode("ascii").rstrip("=").lower()


def mint_outbox_id(raw: bytes) -> str:
    """An ``outbox_id`` from 16 random bytes (an ``IdSource`` supplies them)."""
    return _opaque(OUTBOX_ID_PREFIX, raw)


def mint_grant_id(raw: bytes) -> str:
    """A ``grant_id`` from 16 random bytes."""
    return _opaque(GRANT_ID_PREFIX, raw)


def is_outbox_id(value: str) -> bool:
    return value.startswith(OUTBOX_ID_PREFIX) and bool(_OPAQUE.match(value))


def is_grant_id(value: str) -> bool:
    return value.startswith(GRANT_ID_PREFIX) and bool(_OPAQUE.match(value))


class OutboxEvent(StrEnum):
    """The semantic event a record communicates."""

    MERGED = "merged"
    RELEASED = "released"
    CORRECTED = "corrected"
    REPLY = "reply"


class OpKind(StrEnum):
    """The two tracker writes (the values mirror the port's ``Operation``)."""

    COMMENT = "comment.create"
    LABELS = "labels.change"


class OpState(StrEnum):
    DRAFTED = "drafted"
    HELD = "held"
    APPROVED = "approved"
    SENDING = "sending"
    UNKNOWN = "unknown"
    FAILED = "failed"
    SENT = "sent"
    SUPERSEDED = "superseded"
    ABANDONED = "abandoned"


TERMINAL = frozenset({OpState.SENT, OpState.SUPERSEDED, OpState.ABANDONED})
#: The states O6 may supersede: before any attempt.
_SUPERSEDABLE = frozenset({OpState.DRAFTED, OpState.HELD, OpState.APPROVED})
#: The states the completion close rule counts as settled (``bh-6wj9y`` R3).
SETTLED = frozenset({OpState.SENT, OpState.ABANDONED, OpState.SUPERSEDED})


class GrantStatus(StrEnum):
    MINTED = "minted"  # recorded by O5, usable by exactly one O9
    CONSUMED = "consumed"  # used by O9
    VOIDED = "voided"  # O6, O7 or O8 retired it unused


class RecordStatus(StrEnum):
    """A record's state, derived from its operations (``bh-6wj9y`` R4)."""

    OPEN = "open"  # any operation is non-terminal
    DELIVERED = "delivered"  # every operation is sent
    PARTIAL = "partial"  # some sent, the rest retired
    RETIRED = "retired"  # none sent, all retired (superseded or abandoned)


# -- records ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class DraftKey:
    """A record's stable identity: binding, semantic event and source revision."""

    binding_url: str
    event: OutboxEvent
    source_revision: str


@dataclass(frozen=True)
class PublicFacts:
    """Explicit publish intent: the only values a template may render.

    ``commit_url`` is allowed only when ``outbox.public_evidence.commit_links`` is on;
    ``progress`` is the derived work-progress word, set only when a template asks for it."""

    tag: str = ""
    version: str = ""
    commit_url: str = ""
    progress: str = ""


@dataclass(frozen=True)
class Message:
    """The text to post, without the marker. ``template`` names where it came from; ``text`` may
    use ``{tag}``, ``{version}``, ``{commit_url}`` and ``{progress}`` and nothing else."""

    template: str = ""
    text: str = ""


@dataclass(frozen=True)
class LabelIntent:
    """The owned labels a record asks to add and remove."""

    add: frozenset[str] = frozenset()
    remove: frozenset[str] = frozenset()


@dataclass(frozen=True)
class LabelBase:
    """The issue's labels as one snapshot read them, with that snapshot's revision."""

    revision: str
    labels: frozenset[str]


@dataclass(frozen=True)
class LabelDiff:
    """A label operation's intent, stamped with the base it was drafted against: the snapshot
    revision and the owned labels at that revision (the conflict policy of ``bh-6wj9y`` R2)."""

    add: frozenset[str]
    remove: frozenset[str]
    base_revision: str
    base_owned: frozenset[str]


@dataclass(frozen=True)
class Approval:
    """The recorded gate (D-g): who approved which digest, minting which single-use grant."""

    actor: str
    digest: str
    grant_id: str
    seq: int
    overrides: tuple[Override, ...] = ()
    grant: GrantStatus = GrantStatus.MINTED


@dataclass(frozen=True)
class Attempt:
    """One write-ahead attempt (O9). ``end_seq`` is the last ``seq`` at which its request could
    still have been in flight: a snapshot counts as an absence only when it began after it."""

    n: int
    grant_id: str
    epoch: int
    holder: str
    start_seq: int
    end_seq: int | None = None


@dataclass(frozen=True)
class Receipt:
    """Proof of a write: the remote comment id or the labels after, and how it was learned."""

    comment_id: str | None = None
    global_id: str | None = None
    labels_after: frozenset[str] | None = None
    actor: ThreadActor | None = None
    reconciled: bool = False  # found by its marker or labels (O14, O19), not returned (O10)
    late: bool = False  # found after the operation was abandoned or failed(absent) (O19)


@dataclass(frozen=True)
class Step:
    """One history entry: ``(seq, from, to, event, actor)`` plus the rule that applied."""

    seq: int
    rule: str
    event: str
    from_state: OpState | None
    to_state: OpState
    actor: str
    detail: str = ""


@dataclass(frozen=True)
class OutboxOperation:
    """One tracker write of a record and its own state."""

    kind: OpKind
    state: OpState = OpState.DRAFTED
    labels: LabelDiff | None = None  # set for a labels.change operation only
    findings: tuple[LeakFinding, ...] = ()  # the latest check's findings
    overrides: tuple[Override, ...] = ()  # deliberate publishes awaiting the next approval
    approvals: tuple[Approval, ...] = ()
    attempts: tuple[Attempt, ...] = ()
    receipt: Receipt | None = None
    fail_reason: str = ""  # ``refused:<class>`` (O11) or ``absent`` (O16)
    absent_confirmations: int = 0
    last_absent_seq: int = 0  # the start seq of the last snapshot counted as an absence
    outcome_unknown: bool = False  # abandoned from unknown (O18)
    duplicates: tuple[str, ...] = ()  # further remote comments carrying this operation's marker
    history: tuple[Step, ...] = ()

    @property
    def approval(self) -> Approval | None:
        return self.approvals[-1] if self.approvals else None

    @property
    def attempt(self) -> Attempt | None:
        return self.attempts[-1] if self.attempts else None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def duplicate_observed(self) -> bool:
        return bool(self.duplicates)


@dataclass(frozen=True)
class OutboxRecord:
    """One communication record (``bh-6wj9y`` R4)."""

    outbox_id: str
    draft_key: DraftKey
    binding: IssueBinding
    event: OutboxEvent
    group: str
    facts: PublicFacts
    message: Message
    operations: tuple[OutboxOperation, ...]
    created_seq: int
    supersedes: str | None = None

    def op(self, kind: OpKind) -> OutboxOperation | None:
        return next((o for o in self.operations if o.kind is kind), None)

    @property
    def status(self) -> RecordStatus:
        states = [o.state for o in self.operations]
        if any(s not in TERMINAL for s in states):
            return RecordStatus.OPEN
        if all(s is OpState.SENT for s in states):
            return RecordStatus.DELIVERED
        if any(s is OpState.SENT for s in states):
            return RecordStatus.PARTIAL
        return RecordStatus.RETIRED


@dataclass(frozen=True)
class MergedFact:
    work: str
    commit: str


@dataclass(frozen=True)
class ReleasedFact:
    tag: str
    commit: str
    verified: bool


@dataclass(frozen=True)
class DeliveryFacts:
    """What the milestone triggers established, kept beside the records."""

    merged: MergedFact | None = None
    released: ReleasedFact | None = None


@dataclass(frozen=True)
class OutboxDocument:
    """The ``intake.outbox`` document of one report bead. Never mutated."""

    seq: int = 0
    facts: DeliveryFacts = DeliveryFacts()
    records: Mapping[str, OutboxRecord] = field(default_factory=dict)
    schema: int = OUTBOX_SCHEMA

    def record(self, outbox_id: str) -> OutboxRecord | None:
        return self.records.get(outbox_id)

    def by_key(self, key: DraftKey) -> OutboxRecord | None:
        return next((r for r in self.records.values() if r.draft_key == key), None)


# -- inputs and outcomes ---------------------------------------------------------------------


@dataclass(frozen=True)
class OutboxPolicy:
    """The ``intake.external.outbox.*`` semantics. Off by default: nothing drafts.

    ``labels_enabled`` is the ``labels.change`` gate, separate from ``enabled`` (which gates the
    outbox and so ``comment.create``): with it off no label operation can be approved or sent,
    whatever the adapter enables and whatever grant a comment holds (I9b)."""

    enabled: bool = False
    labels_enabled: bool = False
    absent_confirmations: int = 2  # k in O16
    commit_links: bool = False
    agent_seat_prefixes: tuple[str, ...] = AGENT_SEAT_PREFIXES

    def __post_init__(self) -> None:
        if self.absent_confirmations < 1:
            raise ValueError("absent_confirmations must be at least 1")


@dataclass(frozen=True)
class Capabilities:
    """Which operations the adapter's ``probe_capabilities`` reports usable (values of the
    port's ``Operation``). Nothing is usable by default."""

    usable: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RenderContext:
    """Everything a render reads besides the record itself.

    ``binding_url`` is the report's current binding; ``marker_line`` is the adapter-encoded own
    marker for this record, so the bytes rendered are the bytes posted; ``labels`` is the latest
    issue snapshot's labels (a label operation's render needs it); ``thread_missing`` holds every
    operation (``bh-fptob`` R6). ``sanitizer=None`` holds every operation (fail closed)."""

    binding_url: str
    policy: LeakPolicy
    sanitizer: Scanner | None
    marker_line: str = ""
    labels: LabelBase | None = None
    thread_missing: bool = False


@dataclass(frozen=True)
class Rendered:
    """A dry-run render of one operation: exactly what would be sent, and its digest."""

    outbox_id: str
    kind: OpKind
    target_url: str
    payload: str  # the comment body plus marker line, or the canonical label diff
    body: str  # what the writer is handed (the comment body without the marker)
    base_revision: str | None
    digest: str
    findings: tuple[LeakFinding, ...]
    add: frozenset[str] = frozenset()
    remove: frozenset[str] = frozenset()

    @property
    def short_digest(self) -> str:
        return _hex(self.digest)[:12]


@dataclass(frozen=True)
class DraftRequest:
    """O1. ``binding`` is the report's binding (``None``: born native, nothing to send to)."""

    binding: IssueBinding | None
    event: OutboxEvent
    source_revision: str
    actor: str
    facts: PublicFacts = PublicFacts()
    message: Message = Message()
    comment: bool = True
    labels: LabelIntent | None = None


@dataclass(frozen=True)
class ApprovalRequest:
    """O5. ``digest`` is what the operator typed: the full digest or at least its 12-hex prefix."""

    outbox_id: str
    kind: OpKind
    digest: str
    actor: str


@dataclass(frozen=True)
class SendPlan:
    """What O9 hands the sender: exactly one write, with the grant to pass exactly once."""

    outbox_id: str
    kind: OpKind
    grant_id: str
    target_url: str
    body: str = ""
    add: frozenset[str] = frozenset()
    remove: frozenset[str] = frozenset()


@dataclass(frozen=True)
class MarkedComment:
    """A comment on the issue whose decoded marker names this hive and ``outbox_id``.

    ``known_actor`` is whether its author is a known actor (``whoami`` or an outbox receipt): a
    marker alone is never sufficient."""

    comment_id: str
    outbox_id: str
    actor: ThreadActor | None = None
    known_actor: bool = False
    global_id: str | None = None


@dataclass(frozen=True)
class OutboxObservation:
    """One reconcile read. ``start_seq`` is the document ``seq`` read before the listing began;
    only a ``complete`` listing can count an absence. ``labels`` is the issue's labels when the
    read included them."""

    start_seq: int
    complete: bool
    comments: tuple[MarkedComment, ...] = ()
    labels: frozenset[str] | None = None


@dataclass(frozen=True)
class OutboxApplied:
    """A rule's result: the new document, the steps it applied, and what the caller does next.

    ``notified`` is the ``notified`` value to write in the same write (a comment reached
    ``sent``); ``plan`` is O9's one write to perform."""

    document: OutboxDocument
    steps: tuple[Step, ...] = ()
    outbox_id: str = ""
    notified: str | None = None
    plan: SendPlan | None = None

    @property
    def changed(self) -> bool:
        return bool(self.steps)

    @property
    def record(self) -> OutboxRecord | None:
        return self.document.record(self.outbox_id)


# -- rendering -------------------------------------------------------------------------------

_PLACEHOLDERS = ("tag", "version", "commit_url", "progress")


def render_text(message: Message, facts: PublicFacts) -> str:
    """``message.text`` with only :class:`PublicFacts` placeholders filled. Anything else in
    braces stays literal: a template cannot reach any other value."""
    text = message.text
    for name in _PLACEHOLDERS:
        text = text.replace("{" + name + "}", getattr(facts, name))
    return text


def _hex(digest: str) -> str:
    return digest.split(":", 1)[1] if ":" in digest else digest


def digest_of(target_url: str, kind: OpKind, payload: str, base_revision: str | None) -> str:
    """sha256 over the canonical serialisation of target, operation, bytes and label base."""
    canonical = json.dumps(
        {
            "base_revision": base_revision,
            "operation": kind.value,
            "payload": payload,
            "target": target_url,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def digest_matches(typed: str, digest: str) -> bool:
    """Whether the digest an operator typed names ``digest``: the whole value, or a prefix of at
    least 12 hex characters of it."""
    want, have = _hex(typed.strip()).lower(), _hex(digest)
    return len(want) >= 12 and have.startswith(want)


def _owned(labels: Iterable[str], policy: LeakPolicy) -> frozenset[str]:
    return frozenset(label for label in labels if policy.owns(label))


def _label_view(diff: LabelDiff, ctx: RenderContext) -> tuple[LabelBase, str]:
    """The base a label render is computed against and the revision its digest names: the
    drafted base while no owned label changed since it, else the current snapshot."""
    current = ctx.labels or LabelBase(diff.base_revision, diff.base_owned)
    if _owned(current.labels, ctx.policy) == diff.base_owned:
        return current, diff.base_revision
    return current, current.revision


def render(record: OutboxRecord, kind: OpKind, ctx: RenderContext) -> Rendered:
    """The dry run of one operation (``bh work outbox show``): no read, no write."""
    op = _op(record, kind)
    if kind is OpKind.COMMENT:
        body = render_text(record.message, record.facts)
        payload = f"{body}\n\n{ctx.marker_line}" if ctx.marker_line else body
        findings = check(payload, ctx.policy, sanitizer=ctx.sanitizer)
        return Rendered(
            record.outbox_id,
            kind,
            record.binding.url,
            payload,
            body,
            None,
            digest_of(record.binding.url, kind, payload, None),
            findings,
        )
    diff = op.labels
    if diff is None:  # pragma: no cover - a labels operation always carries its diff
        raise IntakeRefused(RefusalReason.INVALID_DRAFT, "a label operation has no diff")
    current, base_revision = _label_view(diff, ctx)
    add = frozenset(diff.add - current.labels)
    remove = frozenset(diff.remove & current.labels)
    payload = f"add: {','.join(sorted(add))}\nremove: {','.join(sorted(remove))}"
    findings = check(
        payload, ctx.policy, sanitizer=ctx.sanitizer, labels=sorted(diff.add | diff.remove)
    )
    return Rendered(
        record.outbox_id,
        kind,
        record.binding.url,
        payload,
        "",
        base_revision,
        digest_of(record.binding.url, kind, payload, base_revision),
        findings,
        add,
        remove,
    )


def reply_revision(text: str, thread_seq: int) -> str:
    """A reply's ``source_revision``: the composed text's digest plus the thread ``seq`` it
    answers (``bh-6wj9y`` R4)."""
    return f"{hashlib.sha256(text.encode('utf-8')).hexdigest()}@{thread_seq}"


# -- the transaction every rule applies ------------------------------------------------------


class _Tx:
    """One document write: ``seq`` is bumped once and every step carries it."""

    def __init__(self, doc: OutboxDocument) -> None:
        self.doc = doc
        self.seq = doc.seq + 1
        self.records = dict(doc.records)
        self.steps: list[Step] = []
        self.notified: str | None = None

    def put(self, record: OutboxRecord) -> None:
        self.records[record.outbox_id] = record

    def op(self, outbox_id: str, kind: OpKind) -> OutboxOperation:
        return _op(self.records[outbox_id], kind)

    def move(
        self,
        outbox_id: str,
        kind: OpKind,
        to: OpState,
        *,
        rule: str,
        event: str,
        actor: str,
        detail: str = "",
        **changes,
    ) -> OutboxOperation:
        record = self.records[outbox_id]
        op = _op(record, kind)
        step = Step(self.seq, rule, event, op.state, to, actor, detail)
        new = dataclasses.replace(op, state=to, history=(*op.history, step), **changes)
        self.put(
            dataclasses.replace(
                record,
                operations=tuple(new if o.kind is kind else o for o in record.operations),
            )
        )
        self.steps.append(step)
        if to is OpState.SENT and kind is OpKind.COMMENT and op.state is not OpState.SENT:
            self.notified = NOTIFIED_BY_EVENT[record.event.value]
        return new

    def done(self, outbox_id: str = "", plan: SendPlan | None = None) -> OutboxApplied:
        if not self.steps:
            return OutboxApplied(self.doc, outbox_id=outbox_id)
        doc = dataclasses.replace(self.doc, seq=self.seq, records=self.records)
        return OutboxApplied(doc, tuple(self.steps), outbox_id, self.notified, plan)


def _record(doc: OutboxDocument | _Tx, outbox_id: str) -> OutboxRecord:
    records = doc.records
    record = records.get(outbox_id)
    if record is None:
        raise IntakeRefused(RefusalReason.UNKNOWN_OUTBOX, f"no outbox record {outbox_id}")
    return record


def _op(record: OutboxRecord, kind: OpKind) -> OutboxOperation:
    op = record.op(kind)
    if op is None:
        raise IntakeRefused(
            RefusalReason.UNKNOWN_OUTBOX, f"{record.outbox_id} has no {kind.value} operation"
        )
    return op


def _refuse_state(record: OutboxRecord, op: OutboxOperation, event: str) -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.OUTBOX_TRANSITION,
        f"{record.outbox_id} {op.kind.value} is {op.state.value}; {event} is not allowed from it",
    )


def is_operator_seat(actor: str, policy: OutboxPolicy) -> bool:
    """A human operator seat: any named actor that is not an agent seat. Timers, config,
    environment variables and flags are not actors, so they can never approve or send."""
    name = actor.strip()
    return bool(name) and not any(name.startswith(p) for p in policy.agent_seat_prefixes)


def _require_operator(actor: str, policy: OutboxPolicy, event: str) -> None:
    if not is_operator_seat(actor, policy):
        raise IntakeRefused(
            RefusalReason.OPERATOR_SEAT_REQUIRED,
            f"{event} needs a human operator seat, not {actor or 'no actor'!r}",
        )


def require_kind_enabled(kind: OpKind, policy: OutboxPolicy) -> None:
    """The per-operation gate: a label operation needs its own flag (off by default)."""
    if kind is OpKind.LABELS and not policy.labels_enabled:
        raise IntakeRefused(
            RefusalReason.LABELS_DISABLED,
            "labels.change is off for this hive (its own flag, separate from comment.create)",
        )


def _require_owned(op: OutboxOperation, policy: LeakPolicy) -> None:
    """Only labels under an owned prefix are ever changed: refused before any approval or
    attempt, whatever the stored diff says (a diff edited in the store, a narrowed policy)."""
    diff = op.labels
    if op.kind is not OpKind.LABELS or diff is None:
        return
    stray = sorted(label for label in diff.add | diff.remove if not policy.owns(label))
    if stray:
        raise IntakeRefused(
            RefusalReason.LABEL_NOT_OWNED, f"not under an owned prefix: {', '.join(stray)}"
        )


def _require_target(record: OutboxRecord, ctx: RenderContext) -> None:
    if ctx.binding_url != record.binding.url:
        raise IntakeRefused(
            RefusalReason.TARGET_MISMATCH,
            f"{record.outbox_id} targets {record.binding.url}, the report is bound to "
            f"{ctx.binding_url or 'nothing'}",
        )


def _holds(rendered: Rendered, overrides: Iterable[Override], ctx: RenderContext) -> bool:
    return bool(unresolved(rendered.findings, overrides)) or ctx.thread_missing


def _check(tx: _Tx, outbox_id: str, kind: OpKind, ctx: RenderContext, actor: str) -> None:
    """O3 on a ``drafted`` operation: record the findings, hold it on any unresolved one."""
    record = tx.records[outbox_id]
    op = _op(record, kind)
    rendered = render(record, kind, ctx)
    if op.state is OpState.DRAFTED and _holds(rendered, op.overrides, ctx):
        detail = "thread missing" if ctx.thread_missing else ""
        found = ",".join(f.id for f in unresolved(rendered.findings, op.overrides))
        tx.move(
            outbox_id,
            kind,
            OpState.HELD,
            rule="O3",
            event="check",
            actor=actor,
            detail=", ".join(p for p in (found, detail) if p),
            findings=rendered.findings,
        )
    elif rendered.findings != op.findings:
        new = dataclasses.replace(op, findings=rendered.findings)
        tx.put(
            dataclasses.replace(
                record, operations=tuple(new if o.kind is kind else o for o in record.operations)
            )
        )


def _void(op: OutboxOperation) -> tuple[Approval, ...]:
    """The approvals with any unused grant voided (O6, O7, O8)."""
    return tuple(
        dataclasses.replace(a, grant=GrantStatus.VOIDED) if a.grant is GrantStatus.MINTED else a
        for a in op.approvals
    )


def _grant_ids(doc: OutboxDocument) -> frozenset[str]:
    return frozenset(
        a.grant_id for r in doc.records.values() for o in r.operations for a in o.approvals
    )


# -- O1 to O3, O6: draft ---------------------------------------------------------------------


def draft(
    doc: OutboxDocument,
    request: DraftRequest,
    *,
    outbox_id: str,
    policy: OutboxPolicy,
    ctx: RenderContext,
) -> OutboxApplied:
    """O1 (with O3 and O6 in the same write), or O2 when the :class:`DraftKey` exists."""
    if not policy.enabled:
        raise IntakeRefused(
            RefusalReason.OUTBOX_DISABLED, "intake.external.outbox.enabled is off (the default)"
        )
    binding = request.binding
    if binding is None:
        raise IntakeRefused(RefusalReason.NOT_BOUND, "a born-native report has no target")
    key = DraftKey(binding.url, request.event, request.source_revision)
    existing = doc.by_key(key)
    if existing is not None:  # O2: no write
        return OutboxApplied(doc, outbox_id=existing.outbox_id)
    if not is_outbox_id(outbox_id) or outbox_id in doc.records:
        raise IntakeRefused(
            RefusalReason.INVALID_OUTBOX_ID, f"{outbox_id!r} is not a fresh opaque outbox id"
        )
    if request.facts.commit_url and not policy.commit_links:
        raise IntakeRefused(
            RefusalReason.COMMIT_LINKS_OFF,
            "a public commit URL needs intake.external.outbox.public_evidence.commit_links",
        )
    operations: list[OutboxOperation] = []
    if request.comment:
        if not render_text(request.message, request.facts).strip():
            raise IntakeRefused(RefusalReason.INVALID_DRAFT, "a comment needs text")
        operations.append(OutboxOperation(OpKind.COMMENT))
    if request.labels is not None:
        intent = request.labels
        if not intent.add and not intent.remove:
            raise IntakeRefused(RefusalReason.INVALID_DRAFT, "a label operation changes nothing")
        stray = sorted(label for label in intent.add | intent.remove if not ctx.policy.owns(label))
        if stray:
            raise IntakeRefused(
                RefusalReason.LABEL_NOT_OWNED, f"not under an owned prefix: {', '.join(stray)}"
            )
        if ctx.labels is None:
            raise IntakeRefused(RefusalReason.INVALID_DRAFT, "a label operation needs a snapshot")
        base = LabelDiff(
            frozenset(intent.add),
            frozenset(intent.remove),
            ctx.labels.revision,
            _owned(ctx.labels.labels, ctx.policy),
        )
        operations.append(OutboxOperation(OpKind.LABELS, labels=base))
    if not operations:
        raise IntakeRefused(RefusalReason.INVALID_DRAFT, "a record needs an operation")

    tx = _Tx(doc)
    group = MILESTONE_GROUP if request.event is not OutboxEvent.REPLY else f"reply:{tx.seq}"
    older = sorted(
        (r for r in doc.records.values() if r.group == group),
        key=lambda r: r.created_seq,
    )
    record = OutboxRecord(
        outbox_id,
        key,
        binding,
        request.event,
        group,
        request.facts,
        request.message,
        tuple(operations),
        tx.seq,
        supersedes=None,
    )
    tx.put(record)
    for op in record.operations:  # O1, recorded on each operation's history
        step = Step(tx.seq, "O1", "draft", None, OpState.DRAFTED, request.actor)
        tx.steps.append(step)
        current = tx.records[outbox_id]
        tx.put(
            dataclasses.replace(
                current,
                operations=tuple(
                    dataclasses.replace(o, history=(step,)) if o.kind is op.kind else o
                    for o in current.operations
                ),
            )
        )
        _check(tx, outbox_id, op.kind, ctx, request.actor)
    superseded = None
    for old in older:  # O6: retire every unsent operation of an older record in the group
        for op in old.operations:
            if op.state in _SUPERSEDABLE:
                tx.move(
                    old.outbox_id,
                    op.kind,
                    OpState.SUPERSEDED,
                    rule="O6",
                    event="supersede",
                    actor=request.actor,
                    detail=f"by {outbox_id}",
                    approvals=_void(op),
                )
                superseded = old.outbox_id
    if superseded is not None:
        tx.put(dataclasses.replace(tx.records[outbox_id], supersedes=superseded))
    return tx.done(outbox_id)


def recheck(
    doc: OutboxDocument, outbox_id: str, kind: OpKind, *, ctx: RenderContext, actor: str
) -> OutboxApplied:
    """O3 at a render: hold a ``drafted`` operation that now has an unresolved finding."""
    record = _record(doc, outbox_id)
    _op(record, kind)
    tx = _Tx(doc)
    _check(tx, outbox_id, kind, ctx, actor)
    return tx.done(outbox_id)  # no transition: no write (refreshed findings are not kept)


# -- O4: edit and override -------------------------------------------------------------------


def edit(
    doc: OutboxDocument,
    outbox_id: str,
    *,
    actor: str,
    policy: OutboxPolicy,
    ctx: RenderContext,
    text: str | None = None,
    facts: PublicFacts | None = None,
) -> OutboxApplied:
    """New reply text or template facts for the comment: O4 from ``held``, a re-checked edit in
    ``drafted``, O7 from ``approved`` (its grant voided). Overrides are dropped: they named the
    old text."""
    _require_operator(actor, policy, "edit")
    record = _record(doc, outbox_id)
    op = _op(record, OpKind.COMMENT)
    if op.state not in {OpState.DRAFTED, OpState.HELD, OpState.APPROVED}:
        raise _refuse_state(record, op, "edit")
    new_facts = record.facts if facts is None else facts
    if new_facts.commit_url and not policy.commit_links:
        raise IntakeRefused(
            RefusalReason.COMMIT_LINKS_OFF,
            "a public commit URL needs intake.external.outbox.public_evidence.commit_links",
        )
    message = record.message if text is None else dataclasses.replace(record.message, text=text)
    if not render_text(message, new_facts).strip():
        raise IntakeRefused(RefusalReason.INVALID_DRAFT, "a comment needs text")
    tx = _Tx(doc)
    tx.put(dataclasses.replace(record, message=message, facts=new_facts))
    rule = {OpState.DRAFTED: "O4", OpState.HELD: "O4", OpState.APPROVED: "O7"}[op.state]
    tx.move(
        outbox_id,
        OpKind.COMMENT,
        OpState.DRAFTED,
        rule=rule,
        event="edit",
        actor=actor,
        overrides=(),
        approvals=_void(op),
    )
    _check(tx, outbox_id, OpKind.COMMENT, ctx, actor)
    return tx.done(outbox_id)


def override(
    doc: OutboxDocument,
    outbox_id: str,
    kind: OpKind,
    findings: Iterable[str],
    *,
    actor: str,
    policy: OutboxPolicy,
    ctx: RenderContext,
) -> OutboxApplied:
    """O4: deliberately publish named findings of a ``held`` operation (``--publish
    bead_id:1``), then re-check (O3). A ``secret``, ``no_sanitizer`` or ``label_not_owned``
    finding is never overridable."""
    _require_operator(actor, policy, "override")
    record = _record(doc, outbox_id)
    op = _op(record, kind)
    if op.state is not OpState.HELD:
        raise _refuse_state(record, op, "override")
    names = tuple(dict.fromkeys(findings))
    if not names:
        raise IntakeRefused(RefusalReason.UNKNOWN_FINDING, "name at least one finding to publish")
    current = {f.id: f for f in render(record, kind, ctx).findings}
    missing = [name for name in names if name not in current]
    if missing:
        raise IntakeRefused(RefusalReason.UNKNOWN_FINDING, f"no finding {', '.join(missing)}")
    never = [name for name in names if not current[name].overridable]
    if never:
        raise IntakeRefused(
            RefusalReason.NEVER_OVERRIDABLE, f"{', '.join(never)} can never be published"
        )
    added = tuple(Override(name, current[name].kind, current[name].match, actor) for name in names)
    tx = _Tx(doc)
    tx.move(
        outbox_id,
        kind,
        OpState.DRAFTED,
        rule="O4",
        event="override",
        actor=actor,
        detail=",".join(names),
        overrides=(*op.overrides, *added),
    )
    _check(tx, outbox_id, kind, ctx, actor)
    return tx.done(outbox_id)


# -- O5, O7: approve and invalidate ----------------------------------------------------------


def approve(
    doc: OutboxDocument,
    request: ApprovalRequest,
    *,
    grant_id: str,
    policy: OutboxPolicy,
    ctx: RenderContext,
    capabilities: Capabilities,
) -> OutboxApplied:
    """O5, the gate: a human operator approves the digest of a fresh render of one operation,
    which mints exactly one single-use grant. Approving one operation never approves another."""
    record = _record(doc, request.outbox_id)
    op = _op(record, request.kind)
    require_kind_enabled(request.kind, policy)
    if op.state is not OpState.DRAFTED:
        raise _refuse_state(record, op, "approve")
    _require_operator(request.actor, policy, "approve")
    reads = (COMMENTS_READ, ISSUES_READ) if request.kind is OpKind.LABELS else (COMMENTS_READ,)
    missing = [name for name in (request.kind.value, *reads) if name not in capabilities.usable]
    if missing:
        raise IntakeRefused(
            RefusalReason.CAPABILITY_UNUSABLE, f"not usable on this tracker: {', '.join(missing)}"
        )
    _require_target(record, ctx)
    _require_owned(op, ctx.policy)
    if not is_grant_id(grant_id) or grant_id in _grant_ids(doc):
        raise IntakeRefused(RefusalReason.GRANT_REUSED, f"{grant_id!r} is not a fresh grant id")
    fresh = render(record, request.kind, ctx)
    if not digest_matches(request.digest, fresh.digest):
        raise IntakeRefused(
            RefusalReason.STALE_DIGEST,
            f"the render is now {fresh.short_digest}; review it again before approving",
        )
    if _holds(fresh, op.overrides, ctx):
        found = ", ".join(f.id for f in unresolved(fresh.findings, op.overrides))
        raise IntakeRefused(
            RefusalReason.UNRESOLVED_FINDING, found or "the thread's issue is missing"
        )
    if request.kind is OpKind.LABELS and not (fresh.add or fresh.remove):
        raise IntakeRefused(
            RefusalReason.INVALID_DRAFT, "the issue already has this label state; nothing to change"
        )
    tx = _Tx(doc)
    approval = Approval(request.actor, fresh.digest, grant_id, tx.seq, op.overrides)
    tx.move(
        request.outbox_id,
        request.kind,
        OpState.APPROVED,
        rule="O5",
        event="approve",
        actor=request.actor,
        detail=fresh.short_digest,
        approvals=(*op.approvals, approval),
        findings=fresh.findings,
    )
    return tx.done(request.outbox_id)


def _rebased(op: OutboxOperation, ctx: RenderContext) -> LabelDiff | None:
    if op.labels is None or ctx.labels is None:
        return op.labels
    current, base_revision = _label_view(op.labels, ctx)
    return dataclasses.replace(
        op.labels, base_revision=base_revision, base_owned=_owned(current.labels, ctx.policy)
    )


def invalidate(
    doc: OutboxDocument, outbox_id: str, kind: OpKind, *, ctx: RenderContext, actor: str
) -> OutboxApplied:
    """O7, the sender's pre-send check: an ``approved`` operation whose fresh render no longer
    has the approved digest (edited text, an owned label a human changed) or whose binding moved
    goes back to ``drafted`` with its grant voided; a label diff is rebased onto the current
    labels. No change when the approval still holds."""
    record = _record(doc, outbox_id)
    op = _op(record, kind)
    if op.state is not OpState.APPROVED:
        raise _refuse_state(record, op, "invalidate")
    approval = op.approval
    fresh = render(record, kind, ctx)
    moved = ctx.binding_url != record.binding.url
    if not moved and approval is not None and fresh.digest == approval.digest:
        return OutboxApplied(doc, outbox_id=outbox_id)
    tx = _Tx(doc)
    tx.move(
        outbox_id,
        kind,
        OpState.DRAFTED,
        rule="O7",
        event="invalidate",
        actor=actor,
        detail="binding moved" if moved else f"render is now {fresh.short_digest}",
        approvals=_void(op),
        labels=_rebased(op, ctx),
    )
    _check(tx, outbox_id, kind, ctx, actor)
    return tx.done(outbox_id)


# -- O8, O18: discard ------------------------------------------------------------------------


def discard(
    doc: OutboxDocument,
    outbox_id: str,
    *,
    reason: str,
    actor: str,
    policy: OutboxPolicy,
    kind: OpKind | None = None,
    accept_unknown: bool = False,
) -> OutboxApplied:
    """O8 (``drafted``, ``held``, ``approved``, ``failed`` to ``abandoned``, any grant voided)
    and O18 (``unknown`` to ``abandoned`` only with ``accept_unknown``, kept as
    ``outcome_unknown``). ``kind=None`` discards every non-terminal operation of the record, or
    none (an operation in ``sending`` refuses the whole discard)."""
    _require_operator(actor, policy, "discard")
    record = _record(doc, outbox_id)
    ops = (
        [_op(record, kind)]
        if kind is not None
        else [o for o in record.operations if not o.terminal]
    )
    if not ops:
        raise IntakeRefused(RefusalReason.OUTBOX_TRANSITION, f"{outbox_id} has nothing to discard")
    for op in ops:
        if op.state is OpState.UNKNOWN and not accept_unknown:
            raise IntakeRefused(
                RefusalReason.UNKNOWN_OUTCOME,
                f"{outbox_id} {op.kind.value} may have been posted; discarding it needs "
                "accept_unknown",
            )
        if op.state not in {
            OpState.DRAFTED,
            OpState.HELD,
            OpState.APPROVED,
            OpState.FAILED,
            OpState.UNKNOWN,
        }:
            raise _refuse_state(record, op, "discard")
    tx = _Tx(doc)
    for op in ops:
        unknown = op.state is OpState.UNKNOWN
        tx.move(
            outbox_id,
            op.kind,
            OpState.ABANDONED,
            rule="O18" if unknown else "O8",
            event="discard",
            actor=actor,
            detail=reason,
            approvals=_void(op),
            outcome_unknown=unknown,
        )
    return tx.done(outbox_id)


# -- O9 to O13: send -------------------------------------------------------------------------


def begin_send(
    doc: OutboxDocument,
    outbox_id: str,
    kind: OpKind,
    *,
    actor: str,
    policy: OutboxPolicy,
    ctx: RenderContext,
    epoch: int,
    holder: str,
) -> OutboxApplied:
    """O9: the write-ahead attempt, only from ``approved``. Re-renders and re-checks the exact
    bytes, consumes the grant and returns the one :class:`SendPlan` to perform. The caller holds
    ``IntakeLock`` and ``guard_primary`` and persists this document BEFORE the request."""
    record = _record(doc, outbox_id)
    op = _op(record, kind)
    require_kind_enabled(kind, policy)
    if op.state is not OpState.APPROVED:
        raise _refuse_state(record, op, "send")
    _require_operator(actor, policy, "send")
    approval = op.approval
    if approval is None or approval.grant is not GrantStatus.MINTED:
        raise IntakeRefused(
            RefusalReason.GRANT_UNUSABLE, f"{outbox_id} {kind.value} has no unused grant"
        )
    _require_target(record, ctx)
    _require_owned(op, ctx.policy)
    fresh = render(record, kind, ctx)
    if fresh.digest != approval.digest:
        raise IntakeRefused(
            RefusalReason.STALE_DIGEST,
            f"the render moved from the approved {_hex(approval.digest)[:12]} to "
            f"{fresh.short_digest}; invalidate and approve again",
        )
    if _holds(fresh, approval.overrides, ctx):
        found = ", ".join(f.id for f in unresolved(fresh.findings, approval.overrides))
        raise IntakeRefused(
            RefusalReason.UNRESOLVED_FINDING, found or "the thread's issue is missing"
        )
    tx = _Tx(doc)
    attempt = Attempt(len(op.attempts) + 1, approval.grant_id, epoch, holder, tx.seq)
    consumed = dataclasses.replace(approval, grant=GrantStatus.CONSUMED)
    tx.move(
        outbox_id,
        kind,
        OpState.SENDING,
        rule="O9",
        event="send",
        actor=actor,
        detail=f"attempt {attempt.n}",
        approvals=(*op.approvals[:-1], consumed),
        attempts=(*op.attempts, attempt),
    )
    plan = SendPlan(
        outbox_id, kind, approval.grant_id, record.binding.url, fresh.body, fresh.add, fresh.remove
    )
    return tx.done(outbox_id, plan)


def _ended(op: OutboxOperation, end_seq: int) -> tuple[Attempt, ...]:
    attempt = op.attempt
    if attempt is None:  # pragma: no cover - sending always has an attempt
        return op.attempts
    return (*op.attempts[:-1], dataclasses.replace(attempt, end_seq=end_seq))


def _sending(doc: OutboxDocument, outbox_id: str, kind: OpKind, event: str) -> OutboxOperation:
    record = _record(doc, outbox_id)
    op = _op(record, kind)
    if op.state is not OpState.SENDING:
        raise _refuse_state(record, op, event)
    return op


def record_receipt(
    doc: OutboxDocument, outbox_id: str, kind: OpKind, receipt: Receipt, *, actor: str
) -> OutboxApplied:
    """O10: the writer returned a receipt. A sent comment reports its ``notified`` value."""
    op = _sending(doc, outbox_id, kind, "receipt")
    tx = _Tx(doc)
    tx.move(
        outbox_id,
        kind,
        OpState.SENT,
        rule="O10",
        event="receipt",
        actor=actor,
        receipt=receipt,
        attempts=_ended(op, doc.seq),
    )
    return tx.done(outbox_id)


def record_refusal(
    doc: OutboxDocument, outbox_id: str, kind: OpKind, refusal: str, *, actor: str
) -> OutboxApplied:
    """O11: a refusal that proves nothing was written (the application decides which tracker
    errors are proof). The grant stays consumed; a retry needs a new approval."""
    op = _sending(doc, outbox_id, kind, "refusal")
    tx = _Tx(doc)
    tx.move(
        outbox_id,
        kind,
        OpState.FAILED,
        rule="O11",
        event="refused",
        actor=actor,
        detail=refusal,
        fail_reason=f"refused:{refusal}",
        attempts=_ended(op, doc.seq),
    )
    return tx.done(outbox_id)


def record_unknown(
    doc: OutboxDocument, outbox_id: str, kind: OpKind, *, actor: str, detail: str = ""
) -> OutboxApplied:
    """O12: an ambiguous result (``WriteOutcomeUnknown``, a transient failure, anything else).
    Never retried: only reconciliation leaves ``unknown``."""
    op = _sending(doc, outbox_id, kind, "unknown outcome")
    tx = _Tx(doc)
    tx.move(
        outbox_id,
        kind,
        OpState.UNKNOWN,
        rule="O12",
        event="ambiguous",
        actor=actor,
        detail=detail,
        attempts=_ended(op, doc.seq),
    )
    return tx.done(outbox_id)


def recover(
    doc: OutboxDocument, *, live_epoch: int, holder_gone: bool, actor: str
) -> OutboxApplied:
    """O13: every ``sending`` operation whose holder is gone (the caller now holds the lock it
    held) or whose attempt was made under an older epoch becomes ``unknown``. Clock-free."""
    tx = _Tx(doc)
    for record in doc.records.values():
        for op in record.operations:
            attempt = op.attempt
            if op.state is not OpState.SENDING or attempt is None:
                continue
            if holder_gone or attempt.epoch < live_epoch:
                tx.move(
                    record.outbox_id,
                    op.kind,
                    OpState.UNKNOWN,
                    rule="O13",
                    event="recover",
                    actor=actor,
                    detail="holder gone" if holder_gone else f"epoch {attempt.epoch}",
                    attempts=_ended(op, doc.seq),
                )
    return tx.done()


# -- O14 to O16, O19: reconcile --------------------------------------------------------------


def _satisfied(diff: LabelDiff, labels: frozenset[str]) -> bool:
    return diff.add <= labels and not (diff.remove & labels)


def observe(
    doc: OutboxDocument,
    outbox_id: str,
    observation: OutboxObservation,
    *,
    policy: OutboxPolicy,
    actor: str,
) -> OutboxApplied:
    """Apply one reconcile read to a record (tracker-read-only; internal writes only).

    O14: an ``unknown`` comment whose marker is observed from a known actor (or ``unknown``
    labels the issue already satisfies) is ``sent``, reconciled. O15: a complete read begun
    after the attempt ended, without it, counts one absence (once per read); O16: k absences
    make it ``failed(absent)``. O19: an operation abandoned from ``unknown`` or
    ``failed(absent)`` whose marker appears late is ``sent`` (late); a further marked comment on
    a ``sent`` operation is flagged ``duplicate_observed``. An incomplete read without the
    marker changes nothing."""
    record = _record(doc, outbox_id)
    tx = _Tx(doc)
    for op in record.operations:
        if op.kind is OpKind.COMMENT:
            mine = [c for c in observation.comments if c.outbox_id == outbox_id and c.known_actor]
            found = mine[0] if mine else None
            present = found is not None
            receipt = (
                None
                if found is None
                else Receipt(
                    comment_id=found.comment_id,
                    global_id=found.global_id,
                    actor=found.actor,
                    reconciled=True,
                )
            )
        else:
            present = (
                observation.labels is not None
                and op.labels is not None
                and _satisfied(op.labels, observation.labels)
            )
            mine, found = [], None
            receipt = Receipt(labels_after=observation.labels, reconciled=True) if present else None
        late = (op.state is OpState.ABANDONED and op.outcome_unknown) or (
            op.state is OpState.FAILED and op.fail_reason == ABSENT
        )
        if op.state is OpState.UNKNOWN and present:
            tx.move(
                outbox_id,
                op.kind,
                OpState.SENT,
                rule="O14",
                event="observe",
                actor=actor,
                receipt=receipt,
            )
            op = tx.op(outbox_id, op.kind)
        elif late and present:
            tx.move(
                outbox_id,
                op.kind,
                OpState.SENT,
                rule="O19",
                event="observe late",
                actor=actor,
                receipt=dataclasses.replace(receipt, late=True),
            )
            op = tx.op(outbox_id, op.kind)
        elif op.state is OpState.UNKNOWN:
            attempt = op.attempt
            counts = (
                observation.complete
                and (op.kind is OpKind.COMMENT or observation.labels is not None)
                and attempt is not None
                and attempt.end_seq is not None
                and observation.start_seq > attempt.end_seq
                and observation.start_seq > op.last_absent_seq
            )
            if counts:
                absences = op.absent_confirmations + 1
                tx.move(
                    outbox_id,
                    op.kind,
                    OpState.UNKNOWN,
                    rule="O15",
                    event="absent",
                    actor=actor,
                    detail=f"{absences} of {policy.absent_confirmations}",
                    absent_confirmations=absences,
                    last_absent_seq=observation.start_seq,
                )
                if absences >= policy.absent_confirmations:
                    tx.move(
                        outbox_id,
                        op.kind,
                        OpState.FAILED,
                        rule="O16",
                        event="confirm absent",
                        actor=actor,
                        fail_reason=ABSENT,
                    )
        if op.kind is OpKind.COMMENT and op.state is OpState.SENT and op.receipt is not None:
            extra = tuple(
                c.comment_id
                for c in mine
                if c.comment_id != op.receipt.comment_id and c.comment_id not in op.duplicates
            )
            if extra:
                tx.move(
                    outbox_id,
                    op.kind,
                    OpState.SENT,
                    rule="O19",
                    event="duplicate observed",
                    actor=actor,
                    detail=",".join(extra),
                    duplicates=(*op.duplicates, *extra),
                )
    return tx.done(outbox_id)


# -- O17: retry ------------------------------------------------------------------------------


def retry(
    doc: OutboxDocument, outbox_id: str, kind: OpKind, *, actor: str, policy: OutboxPolicy
) -> OutboxApplied:
    """O17: an operator returns a ``failed`` operation to ``drafted``. Sending again needs a new
    render, a new digest and a new grant (a new O5)."""
    _require_operator(actor, policy, "retry")
    record = _record(doc, outbox_id)
    op = _op(record, kind)
    if op.state is not OpState.FAILED:
        raise _refuse_state(record, op, "retry")
    tx = _Tx(doc)
    tx.move(
        outbox_id,
        kind,
        OpState.DRAFTED,
        rule="O17",
        event="retry",
        actor=actor,
        detail=op.fail_reason,
        fail_reason="",
        absent_confirmations=0,
    )
    return tx.done(outbox_id)


# -- reads -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class OutboxLedger:
    """The outbox read own-message recognition needs (the contracts' ``OutboxView``)."""

    document: OutboxDocument

    def _ops(self) -> Iterable[tuple[str, OutboxOperation]]:
        for record in self.document.records.values():
            for op in record.operations:
                yield record.outbox_id, op

    def receipt_ids(self) -> frozenset[str]:
        return frozenset(
            op.receipt.comment_id
            for _, op in self._ops()
            if op.receipt is not None and op.receipt.comment_id
        )

    def pending(self, outbox_id: str) -> bool:
        return any(
            oid == outbox_id and op.state in {OpState.SENDING, OpState.UNKNOWN}
            for oid, op in self._ops()
        )

    def issued(self, outbox_id: str) -> bool:
        return any(oid == outbox_id and op.attempts for oid, op in self._ops())

    def known_actors(self) -> frozenset[ThreadActor]:
        return frozenset(
            op.receipt.actor
            for _, op in self._ops()
            if op.receipt is not None and op.receipt.actor is not None
        )


def outbox_view(doc: OutboxDocument) -> OutboxLedger:
    return OutboxLedger(doc)


def close_rule(
    doc: OutboxDocument,
    *,
    delivery: str | None,
    completion: CompletionPolicy,
    outbox_enabled: bool,
) -> bool:
    """Whether the report may close (``bh-6wj9y`` R3): its delivery reached the completion
    milestone and, with the outbox on, that milestone's latest record's comment is ``sent``,
    ``abandoned`` or ``superseded`` ("told, or the operator decided not to tell"). With the
    outbox on and no such record yet, the report waits."""
    if not delivery_complete(completion, delivery):
        return False
    if not outbox_enabled:
        return True
    event = completion_event(completion)
    records = [r for r in doc.records.values() if r.event.value == event]
    if not records:
        return False
    latest = max(records, key=lambda r: r.created_seq)
    op = latest.op(OpKind.COMMENT)
    return op is None or op.state in SETTLED


__all__ = [
    "ABSENT",
    "AGENT_SEAT_PREFIXES",
    "COMMENTS_READ",
    "ISSUES_READ",
    "GRANT_ID_PREFIX",
    "MILESTONE_GROUP",
    "OUTBOX_ID_PREFIX",
    "OUTBOX_SCHEMA",
    "SETTLED",
    "TERMINAL",
    "Approval",
    "ApprovalRequest",
    "Attempt",
    "Capabilities",
    "DeliveryFacts",
    "DraftKey",
    "DraftRequest",
    "GrantStatus",
    "LabelBase",
    "LabelDiff",
    "LabelIntent",
    "MarkedComment",
    "MergedFact",
    "Message",
    "OpKind",
    "OpState",
    "OutboxApplied",
    "OutboxDocument",
    "OutboxEvent",
    "OutboxLedger",
    "OutboxObservation",
    "OutboxOperation",
    "OutboxPolicy",
    "OutboxRecord",
    "PublicFacts",
    "Receipt",
    "RecordStatus",
    "ReleasedFact",
    "RenderContext",
    "Rendered",
    "SendPlan",
    "Step",
    "approve",
    "begin_send",
    "close_rule",
    "digest_matches",
    "digest_of",
    "discard",
    "draft",
    "edit",
    "invalidate",
    "is_grant_id",
    "is_operator_seat",
    "is_outbox_id",
    "mint_grant_id",
    "mint_outbox_id",
    "observe",
    "outbox_view",
    "override",
    "recheck",
    "record_receipt",
    "record_refusal",
    "record_unknown",
    "recover",
    "render",
    "render_text",
    "require_kind_enabled",
    "reply_revision",
    "retry",
]
