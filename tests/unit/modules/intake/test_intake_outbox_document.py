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
    Capabilities,
    DeliveryFacts,
    DraftRequest,
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
    ReleasedFact,
    RenderContext,
    ThreadActor,
)
from beadhive.modules.intake.domain import outbox as ob
from beadhive.testing.intake import RecordingSanitizer, SequentialIdSource

URL = "https://tracker.example/acme/widgets/issues/7"
B = IssueBinding("acme-r1", "memory", "acme/widgets", URL, "#7")
POLICY = LeakPolicy(bead_prefixes=frozenset({"acme"}), owned_label_prefixes=("beadhive:",))
ON = OutboxPolicy(enabled=True)
ALL = Capabilities(frozenset({"comment.create", "labels.change", "comments.read"}))
BOT = ThreadActor("bot-1", "bot", "beadhive[bot]")
SECRET = "hunter2-very-secret"
CTX = RenderContext(
    URL, POLICY, RecordingSanitizer.of([SECRET]), "<!-- m -->", LabelBase("r1", frozenset({"x"}))
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
