"""The ``intake.outbox`` document: an :class:`~.outbox.OutboxDocument` as JSON values and back.

One flat top-level metadata key on the report bead (external-intake-module-adr.md section 4),
schema-versioned and rewritten whole, beside ``intake.source`` and ``intake.thread``. Every field
is an opaque id, a local integer, a tracker value or recorded intent; no token, and never a
secret's text (a secret finding is stored redacted). Pure; no I/O.

Decoding fails closed (:class:`OutboxDocumentError`) on a malformed document or one written by a
newer schema: a store must never read such a document as an empty outbox, because the next write
would replace it and lose the records of messages that may already exist.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .leaks import LeakFinding, LeakKind, Override
from .models import IssueBinding
from .outbox import (
    OUTBOX_SCHEMA,
    Approval,
    Attempt,
    DeliveryFacts,
    DraftKey,
    GrantStatus,
    LabelDiff,
    MergedFact,
    Message,
    OpKind,
    OpState,
    OutboxDocument,
    OutboxEvent,
    OutboxOperation,
    OutboxRecord,
    PublicFacts,
    Receipt,
    ReleasedFact,
    Step,
    UnlinkedFact,
)
from .thread import ThreadActor

OUTBOX_KEY = "intake.outbox"


class OutboxDocumentError(ValueError):
    """The stored ``intake.outbox`` value is not a document this schema can read."""


def _labels(values) -> list[str]:
    return sorted(values)


def _override(o: Override) -> dict[str, Any]:
    return {"finding": o.finding, "kind": o.kind.value, "match": o.match, "actor": o.actor}


def _operation(op: OutboxOperation) -> dict[str, Any]:
    receipt = op.receipt
    return {
        "kind": op.kind.value,
        "state": op.state.value,
        "labels": None
        if op.labels is None
        else {
            "add": _labels(op.labels.add),
            "remove": _labels(op.labels.remove),
            "base_revision": op.labels.base_revision,
            "base_owned": _labels(op.labels.base_owned),
        },
        "findings": [
            {"id": f.id, "kind": f.kind.value, "match": f.match, "start": f.start}
            for f in op.findings
        ],
        "overrides": [_override(o) for o in op.overrides],
        "approvals": [
            {
                "actor": a.actor,
                "digest": a.digest,
                "grant_id": a.grant_id,
                "seq": a.seq,
                "overrides": [_override(o) for o in a.overrides],
                "grant": a.grant.value,
            }
            for a in op.approvals
        ],
        "attempts": [
            {
                "n": a.n,
                "grant_id": a.grant_id,
                "epoch": a.epoch,
                "holder": a.holder,
                "start_seq": a.start_seq,
                "end_seq": a.end_seq,
            }
            for a in op.attempts
        ],
        "receipt": None
        if receipt is None
        else {
            "comment_id": receipt.comment_id,
            "global_id": receipt.global_id,
            "labels_after": None if receipt.labels_after is None else _labels(receipt.labels_after),
            "actor": None
            if receipt.actor is None
            else {
                "id": receipt.actor.id,
                "kind": receipt.actor.kind,
                "display": receipt.actor.display,
            },
            "reconciled": receipt.reconciled,
            "late": receipt.late,
        },
        "fail_reason": op.fail_reason,
        "absent_confirmations": op.absent_confirmations,
        "last_absent_seq": op.last_absent_seq,
        "outcome_unknown": op.outcome_unknown,
        "duplicates": list(op.duplicates),
        "history": [
            {
                "seq": s.seq,
                "rule": s.rule,
                "event": s.event,
                "from": None if s.from_state is None else s.from_state.value,
                "to": s.to_state.value,
                "actor": s.actor,
                "detail": s.detail,
            }
            for s in op.history
        ],
    }


def _record(record: OutboxRecord) -> dict[str, Any]:
    b = record.binding
    return {
        "draft_key": {
            "binding_url": record.draft_key.binding_url,
            "event": record.draft_key.event.value,
            "source_revision": record.draft_key.source_revision,
        },
        "binding": {
            "report": b.report,
            "tracker": b.tracker,
            "project": b.project,
            "url": b.url,
            "identifier": b.identifier,
        },
        "event": record.event.value,
        "group": record.group,
        "facts": {
            "tag": record.facts.tag,
            "version": record.facts.version,
            "commit_url": record.facts.commit_url,
            "progress": record.facts.progress,
        },
        "message": {"template": record.message.template, "text": record.message.text},
        "operations": [_operation(op) for op in record.operations],
        "created_seq": record.created_seq,
        "supersedes": record.supersedes,
    }


def outbox_to_document(doc: OutboxDocument) -> dict[str, Any]:
    """The JSON value stored under :data:`OUTBOX_KEY`."""
    merged, released = doc.facts.merged, doc.facts.released
    facts: dict[str, Any] = {
        "merged": None if merged is None else {"work": merged.work, "commit": merged.commit},
        "released": None
        if released is None
        else {"tag": released.tag, "commit": released.commit, "verified": released.verified},
    }
    if doc.facts.unlinked is not None:  # omitted when unset: a document without one is unchanged
        facts["unlinked"] = {"work": doc.facts.unlinked.work}
    return {
        "schema": doc.schema,
        "seq": doc.seq,
        "facts": facts,
        "records": {oid: _record(r) for oid, r in doc.records.items()},
    }


# -- decoding --------------------------------------------------------------------------------


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OutboxDocumentError(f"{what} is not an object")
    return value


def _opt(value: Any) -> str | None:
    return None if value is None else str(value)


def _decode_override(value: Any) -> Override:
    raw = _mapping(value, "override")
    return Override(
        str(raw["finding"]), LeakKind(raw["kind"]), str(raw["match"]), str(raw["actor"])
    )


def _decode_operation(value: Any) -> OutboxOperation:
    raw = _mapping(value, "operation")
    labels = raw.get("labels")
    diff = None
    if labels is not None:
        lab = _mapping(labels, "labels")
        diff = LabelDiff(
            frozenset(map(str, lab["add"])),
            frozenset(map(str, lab["remove"])),
            str(lab["base_revision"]),
            frozenset(map(str, lab["base_owned"])),
        )
    receipt = None
    if raw.get("receipt") is not None:
        rec = _mapping(raw["receipt"], "receipt")
        actor = rec.get("actor")
        after = rec.get("labels_after")
        receipt = Receipt(
            comment_id=_opt(rec.get("comment_id")),
            global_id=_opt(rec.get("global_id")),
            labels_after=None if after is None else frozenset(map(str, after)),
            actor=None
            if actor is None
            else ThreadActor(
                str(_mapping(actor, "actor")["id"]),
                str(actor["kind"]),
                str(actor.get("display") or ""),
            ),
            reconciled=bool(rec.get("reconciled", False)),
            late=bool(rec.get("late", False)),
        )
    return OutboxOperation(
        kind=OpKind(raw["kind"]),
        state=OpState(raw["state"]),
        labels=diff,
        findings=tuple(
            LeakFinding(str(f["id"]), LeakKind(f["kind"]), str(f["match"]), int(f["start"]))
            for f in (_mapping(item, "finding") for item in raw.get("findings", ()))
        ),
        overrides=tuple(_decode_override(o) for o in raw.get("overrides", ())),
        approvals=tuple(
            Approval(
                str(a["actor"]),
                str(a["digest"]),
                str(a["grant_id"]),
                int(a["seq"]),
                tuple(_decode_override(o) for o in a.get("overrides", ())),
                GrantStatus(a["grant"]),
            )
            for a in (_mapping(item, "approval") for item in raw.get("approvals", ()))
        ),
        attempts=tuple(
            Attempt(
                int(a["n"]),
                str(a["grant_id"]),
                int(a["epoch"]),
                str(a["holder"]),
                int(a["start_seq"]),
                None if a.get("end_seq") is None else int(a["end_seq"]),
            )
            for a in (_mapping(item, "attempt") for item in raw.get("attempts", ()))
        ),
        receipt=receipt,
        fail_reason=str(raw.get("fail_reason", "")),
        absent_confirmations=int(raw.get("absent_confirmations", 0)),
        last_absent_seq=int(raw.get("last_absent_seq", 0)),
        outcome_unknown=bool(raw.get("outcome_unknown", False)),
        duplicates=tuple(str(d) for d in raw.get("duplicates", ())),
        history=tuple(
            Step(
                int(s["seq"]),
                str(s["rule"]),
                str(s["event"]),
                None if s.get("from") is None else OpState(s["from"]),
                OpState(s["to"]),
                str(s["actor"]),
                str(s.get("detail", "")),
            )
            for s in (_mapping(item, "step") for item in raw.get("history", ()))
        ),
    )


def _decode_record(outbox_id: str, value: Any) -> OutboxRecord:
    raw = _mapping(value, f"record {outbox_id}")
    key, b = _mapping(raw["draft_key"], "draft_key"), _mapping(raw["binding"], "binding")
    facts, message = _mapping(raw["facts"], "facts"), _mapping(raw["message"], "message")
    operations = tuple(_decode_operation(op) for op in raw["operations"])
    if not operations:
        raise OutboxDocumentError(f"record {outbox_id} has no operation")
    return OutboxRecord(
        outbox_id=outbox_id,
        draft_key=DraftKey(
            str(key["binding_url"]), OutboxEvent(key["event"]), str(key["source_revision"])
        ),
        binding=IssueBinding(
            str(b["report"]),
            str(b["tracker"]),
            str(b["project"]),
            str(b["url"]),
            str(b["identifier"]),
        ),
        event=OutboxEvent(raw["event"]),
        group=str(raw["group"]),
        facts=PublicFacts(
            str(facts.get("tag", "")),
            str(facts.get("version", "")),
            str(facts.get("commit_url", "")),
            str(facts.get("progress", "")),
        ),
        message=Message(str(message.get("template", "")), str(message.get("text", ""))),
        operations=operations,
        created_seq=int(raw["created_seq"]),
        supersedes=_opt(raw.get("supersedes")),
    )


def outbox_from_document(value: Any) -> OutboxDocument:
    """The :class:`OutboxDocument` a stored value holds; :class:`OutboxDocumentError` if it is
    not one."""
    try:
        raw = _mapping(value, OUTBOX_KEY)
        schema = int(raw["schema"])
        if schema > OUTBOX_SCHEMA:
            raise OutboxDocumentError(
                f"intake.outbox schema {schema} is newer than this host reads ({OUTBOX_SCHEMA})"
            )
        facts = _mapping(raw.get("facts", {}), "facts")
        merged, released = facts.get("merged"), facts.get("released")
        unlinked = facts.get("unlinked")
        records = [
            _decode_record(str(oid), item)
            for oid, item in _mapping(raw.get("records", {}), "records").items()
        ]
        records.sort(key=lambda r: (r.created_seq, r.outbox_id))
        return OutboxDocument(
            seq=int(raw["seq"]),
            facts=DeliveryFacts(
                merged=None
                if merged is None
                else MergedFact(str(merged["work"]), str(merged["commit"])),
                released=None
                if released is None
                else ReleasedFact(
                    str(released["tag"]), str(released["commit"]), bool(released["verified"])
                ),
                unlinked=None
                if unlinked is None
                else UnlinkedFact(str(_mapping(unlinked, "unlinked")["work"])),
            ),
            records={r.outbox_id: r for r in records},
            schema=schema,
        )
    except OutboxDocumentError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise OutboxDocumentError(f"malformed intake.outbox document: {exc}") from exc


__all__ = ["OUTBOX_KEY", "OutboxDocumentError", "outbox_from_document", "outbox_to_document"]
