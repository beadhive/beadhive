"""The ``intake.outbox`` document codec: a full round trip, JSON-serialisable, fail-closed decode.

Pure; the store write itself (the whole key in one ``bd update --metadata``, beside
``intake.source`` and ``intake.thread``) is the sender bead's.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from beadhive.modules.intake.contracts import (
    OUTBOX_KEY,
    OutboxDocument,
    OutboxDocumentError,
    outbox_from_document,
    outbox_to_document,
)
from beadhive.modules.intake.domain import (
    ApprovalRequest,
    CanonicalIssue,
    Capabilities,
    CloseReason,
    DeliveryFacts,
    DraftRequest,
    IntakeRefused,
    IssueBinding,
    LabelBase,
    LabelIntent,
    LeakPolicy,
    MarkedComment,
    MergedFact,
    Message,
    OpKind,
    OutboxEvent,
    OutboxObservation,
    OutboxPolicy,
    PublicFacts,
    Receipt,
    RefusalReason,
    ReleasedFact,
    RenderContext,
    ThreadActor,
)
from beadhive.modules.intake.domain import outbox as ob
from beadhive.modules.tracker.contracts import join_marker_line
from beadhive.testing.intake import RecordingSanitizer, SequentialIdSource

URL = "https://tracker.example/acme/widgets/issues/7"
B = IssueBinding("acme-r1", "memory", "acme/widgets", URL, "#7")
POLICY = LeakPolicy(bead_prefixes=frozenset({"acme"}), owned_label_prefixes=("beadhive:",))
ON = OutboxPolicy(enabled=True)
ALL = Capabilities(frozenset({"comment.create", "labels.change", "comments.read"}))
BOT = ThreadActor("bot-1", "bot", "beadhive[bot]")
SECRET = "hunter2-very-secret"
CTX = RenderContext(
    URL,
    POLICY,
    RecordingSanitizer.of([SECRET]),
    "<!-- m -->",
    LabelBase("r1", frozenset({"x"})),
    join_marker_line=join_marker_line,
)
C, L = OpKind.COMMENT, OpKind.LABELS


def rich() -> OutboxDocument:
    """A document exercising every field: findings and overrides, approvals, attempts, a
    reconciled receipt with an actor, a label diff, a duplicate, facts and supersession."""
    ids = SequentialIdSource()
    doc = OutboxDocument(
        facts=DeliveryFacts(MergedFact("acme-w1", "c1"), ReleasedFact("v1.2", "c9", True))
    )

    def draft(event, rev, text, **kw):
        nonlocal doc
        oid = ids.outbox_id()
        request = DraftRequest(
            B,
            event,
            rev,
            "outbox",
            PublicFacts(tag="v1.2", progress="closed"),
            Message(event.value, text),
            **kw,
        )
        doc = ob.draft(doc, request, outbox_id=oid, policy=ON, ctx=CTX).document
        return oid

    def approve(oid, kind=C):
        nonlocal doc
        digest = ob.render(doc.record(oid), kind, CTX).short_digest
        doc = ob.approve(
            doc,
            ApprovalRequest(oid, kind, digest, "alice"),
            grant_id=ids.grant_id(),
            policy=ON,
            ctx=CTX,
            capabilities=ALL,
        ).document

    merged = draft(OutboxEvent.MERGED, "c1", "merged {tag}")
    approve(merged)
    draft(
        OutboxEvent.RELEASED,
        "v1.2@c9",
        "released {tag}",
        labels=LabelIntent(add=frozenset({"beadhive:released"})),
    )
    reply = draft(OutboxEvent.REPLY, "r@3", "see acme-w1")
    doc = ob.override(doc, reply, C, ["bead_id:1"], actor="alice", policy=ON, ctx=CTX).document
    approve(reply)
    doc = ob.begin_send(
        doc, reply, C, actor="alice", policy=ON, ctx=CTX, epoch=3, holder="h:1"
    ).document
    doc = ob.record_unknown(doc, reply, C, actor="s").document
    doc = ob.observe(
        doc,
        reply,
        OutboxObservation(
            doc.seq,
            True,
            (MarkedComment("c5", reply, BOT, True, "G5"), MarkedComment("c6", reply, BOT, True)),
        ),
        policy=ON,
        actor="rec",
    ).document
    draft(OutboxEvent.CORRECTED, "rv1", f"token {SECRET}")  # held on a secret
    return doc


def test_the_document_key_is_the_flat_intake_outbox_key() -> None:
    assert OUTBOX_KEY == "intake.outbox"


def test_round_trip_keeps_every_field() -> None:
    doc = rich()
    encoded = outbox_to_document(doc)
    assert json.loads(json.dumps(encoded)) == encoded
    assert outbox_from_document(json.loads(json.dumps(encoded))) == doc
    reply = next(r for r in doc.records.values() if r.event is OutboxEvent.REPLY)
    op = reply.op(C)
    assert op.receipt == Receipt("c5", "G5", None, BOT, reconciled=True)
    assert op.duplicates == ("c6",) and op.approval.overrides[0].actor == "alice"


def test_a_secret_is_never_written_into_a_finding() -> None:
    encoded = outbox_to_document(rich())
    corrected = next(
        r for r in encoded["records"].values() if r["event"] == OutboxEvent.CORRECTED.value
    )
    assert SECRET not in json.dumps(corrected["operations"][0]["findings"])


def test_an_empty_document_round_trips() -> None:
    assert outbox_from_document(outbox_to_document(OutboxDocument())) == OutboxDocument()


def test_a_newer_schema_is_refused_never_read_as_empty() -> None:
    encoded = outbox_to_document(rich())
    with pytest.raises(OutboxDocumentError, match="newer"):
        outbox_from_document({**encoded, "schema": 2})


@pytest.mark.parametrize(
    "mangle",
    [
        lambda d: None,
        lambda d: [],
        lambda d: {k: v for k, v in d.items() if k != "seq"},
        lambda d: {**d, "records": {"ob1_x": {"operations": []}}},
        lambda d: {**d, "records": "nope"},
        lambda d: {
            **d,
            "records": {
                k: {**v, "operations": [{**v["operations"][0], "state": "posted"}]}
                for k, v in d["records"].items()
            },
        },
    ],
)
def test_a_malformed_document_is_refused(mangle) -> None:
    with pytest.raises(OutboxDocumentError):
        outbox_from_document(mangle(outbox_to_document(rich())))


def test_a_record_without_operations_is_refused() -> None:
    encoded = outbox_to_document(rich())
    oid, record = next(iter(encoded["records"].items()))
    with pytest.raises(OutboxDocumentError, match="no operation"):
        outbox_from_document({**encoded, "records": {oid: {**record, "operations": []}}})


def test_decoding_orders_records_by_creation() -> None:
    doc = rich()
    encoded = outbox_to_document(doc)
    shuffled = {**encoded, "records": dict(reversed(list(encoded["records"].items())))}
    decoded = outbox_from_document(shuffled)
    assert list(decoded.records) == list(doc.records)
    assert dataclasses.replace(decoded) == doc


# -- issue.close (bh-pr5ll): written only when present, decoded strictly ------------------------


def _with_close() -> OutboxDocument:
    ids = SequentialIdSource()
    oid = ids.outbox_id()
    policy = OutboxPolicy(enabled=True, close_enabled=True)
    request = DraftRequest(
        B,
        OutboxEvent.REPLY,
        "rev",
        "alice",
        message=Message("reply", "Works as designed."),
        close=CloseReason.NOT_PLANNED,
    )
    doc = ob.draft(OutboxDocument(), request, outbox_id=oid, policy=policy, ctx=CTX).document
    record = doc.records[oid]
    sent = dataclasses.replace(
        record.op(OpKind.CLOSE),
        state=ob.OpState.SENT,
        receipt=Receipt(state_after="closed", reconciled=True),
    )
    operations = tuple(sent if o.kind is OpKind.CLOSE else o for o in record.operations)
    return dataclasses.replace(
        doc, records={oid: dataclasses.replace(record, operations=operations)}
    )


def test_a_close_round_trips_with_its_reason_and_state_receipt() -> None:
    doc = _with_close()
    encoded = outbox_to_document(doc)
    (record,) = encoded["records"].values()
    comment, close = record["operations"]
    assert "close" not in comment and "state_after" not in (comment["receipt"] or {})
    assert close["kind"] == "issue.close" and close["close"] == "not_planned"
    assert close["receipt"]["state_after"] == "closed"
    assert outbox_from_document(json.loads(json.dumps(encoded))) == doc


def test_a_document_without_a_close_is_written_exactly_as_before() -> None:
    for record in outbox_to_document(rich())["records"].values():
        for op in record["operations"]:
            assert "close" not in op and "state_after" not in (op["receipt"] or {})


@pytest.mark.parametrize(
    "mangle",
    [
        lambda op: {k: v for k, v in op.items() if k != "close"},  # a close without its reason
        lambda op: {**op, "close": "wontfix"},  # a reason this host does not know
    ],
)
def test_a_malformed_close_is_refused(mangle) -> None:
    encoded = outbox_to_document(_with_close())
    ((oid, record),) = encoded["records"].items()
    comment, close = record["operations"]
    broken = {**record, "operations": [comment, mangle(close)]}
    with pytest.raises(OutboxDocumentError):
        outbox_from_document({**encoded, "records": {oid: broken}})


def test_a_close_without_its_comment_and_a_reason_on_a_comment_are_refused() -> None:
    encoded = outbox_to_document(_with_close())
    ((oid, record),) = encoded["records"].items()
    comment, close = record["operations"]
    for operations in ([close], [{**comment, "close": "completed"}, close]):
        with pytest.raises(OutboxDocumentError):
            outbox_from_document(
                {**encoded, "records": {oid: {**record, "operations": operations}}}
            )


def test_an_older_host_fails_closed_on_a_close_it_cannot_decode() -> None:
    """The rotation-order guard: an unknown operation kind is a refusal, never an empty outbox."""
    encoded = outbox_to_document(_with_close())
    ((oid, record),) = encoded["records"].items()
    comment, close = record["operations"]
    unknown = {**record, "operations": [comment, {**close, "kind": "issue.lock"}]}
    with pytest.raises(OutboxDocumentError):
        outbox_from_document({**encoded, "records": {oid: unknown}})


# -- a duplicate's canonical (bh-iamru): written only when present, digested, decoded strictly ---

CANONICAL = CanonicalIssue("#1", "https://github.com/acme/widgets/issues/1")


def _duplicate(duplicate_of: CanonicalIssue | None = CANONICAL) -> tuple[OutboxDocument, str]:
    oid = SequentialIdSource().outbox_id()
    request = DraftRequest(
        B,
        OutboxEvent.REPLY,
        "rev",
        "alice",
        message=Message("reply", "This is a duplicate of #1; the work is tracked there."),
        close=CloseReason.DUPLICATE,
        duplicate_of=duplicate_of,
    )
    policy = OutboxPolicy(enabled=True, close_enabled=True)
    return ob.draft(OutboxDocument(), request, outbox_id=oid, policy=policy, ctx=CTX).document, oid


def test_a_duplicate_close_round_trips_with_its_canonical_and_digests_it() -> None:
    doc, oid = _duplicate()
    encoded = outbox_to_document(doc)
    comment, close = encoded["records"][oid]["operations"]
    assert "duplicate_of" not in comment
    assert close["duplicate_of"] == {"identifier": "#1", "url": CANONICAL.url}
    assert outbox_from_document(json.loads(json.dumps(encoded))) == doc

    named = ob.render(doc.records[oid], OpKind.CLOSE, CTX)
    plain_doc, plain_oid = _duplicate(None)
    plain = ob.render(plain_doc.records[plain_oid], OpKind.CLOSE, CTX)
    assert named.payload == f"state: closed\nstate_reason: duplicate\nduplicate_of: {CANONICAL.url}"
    assert named.duplicate_of == CANONICAL and plain.duplicate_of is None
    assert named.digest != plain.digest  # approving the close approves its canonical
    assert (
        "duplicate_of" not in outbox_to_document(plain_doc)["records"][plain_oid]["operations"][1]
    )


def test_a_canonical_on_a_close_that_is_not_a_duplicate_is_refused() -> None:
    doc, oid = _duplicate()
    encoded = outbox_to_document(doc)
    record = encoded["records"][oid]
    comment, close = record["operations"]
    for operations in (
        [comment, {**close, "close": "completed"}],
        [comment, {**close, "duplicate_of": "#1"}],
    ):
        with pytest.raises(OutboxDocumentError):
            outbox_from_document(
                {**encoded, "records": {oid: {**record, "operations": operations}}}
            )


@pytest.mark.parametrize(
    ("close", "canonical"),
    [
        (CloseReason.COMPLETED, CANONICAL),  # only a duplicate names a canonical
        (CloseReason.DUPLICATE, CanonicalIssue("#9", "https://github.com/acme/widgets/issues/9")),
        (None, CANONICAL),  # a canonical needs a close
    ],
    ids=["another-reason", "itself", "no-close"],
)
def test_a_misnamed_canonical_is_refused_at_draft(close, canonical) -> None:
    binding = dataclasses.replace(B, url=canonical.url, identifier=canonical.identifier)
    target = binding if close is CloseReason.DUPLICATE else B
    request = DraftRequest(
        target,
        OutboxEvent.REPLY,
        "rev",
        "alice",
        message=Message("reply", "A duplicate."),
        close=close,
        duplicate_of=canonical,
    )
    policy = OutboxPolicy(enabled=True, close_enabled=True)
    ctx = dataclasses.replace(CTX, binding_url=target.url)
    with pytest.raises(IntakeRefused) as caught:
        oid = SequentialIdSource().outbox_id()
        ob.draft(OutboxDocument(), request, outbox_id=oid, policy=policy, ctx=ctx)
    assert caught.value.reason is RefusalReason.INVALID_DRAFT
