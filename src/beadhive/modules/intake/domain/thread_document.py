"""The ``intake.thread`` document: a :class:`~.thread.Thread` as plain JSON values and back.

One flat top-level metadata key on the report bead (external-intake-module-adr.md section 4),
schema-versioned and rewritten whole. Every field is a tracker value, an opaque token or a local
integer; timestamps are the tracker's, as ISO-8601 strings. Pure; no I/O.

Decoding fails closed (:class:`ThreadDocumentError`) on a malformed document or one written by a
newer schema: a store must never read such a document as an empty thread, because the next write
would replace it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .thread import (
    THREAD_SCHEMA,
    CheckpointScope,
    CommentRecord,
    CommentRevision,
    Evidence,
    OwnMark,
    RecordState,
    Role,
    Thread,
    ThreadActor,
    ThreadCheckpoint,
    ThreadIssue,
    ThreadState,
)

THREAD_KEY = "intake.thread"


class ThreadDocumentError(ValueError):
    """The stored ``intake.thread`` value is not a document this schema can read."""


def _actor(actor: ThreadActor | None) -> dict | None:
    if actor is None:
        return None
    return {"id": actor.id, "kind": actor.kind, "display": actor.display}


def _marker(marker: OwnMark | None) -> dict | None:
    if marker is None:
        return None
    return {"hive": marker.hive, "install": marker.install, "outbox_id": marker.outbox_id}


def _record(record: CommentRecord) -> dict[str, Any]:
    return {
        "global_id": record.global_id,
        "author": _actor(record.author),
        "via": record.via,
        "marker": _marker(record.marker),
        "role": record.role.value,
        "evidence": record.evidence.value,
        "created_at": record.created_at.isoformat(),
        "body": record.body,
        "state": record.state.value,
        "revisions": [
            {
                "rev": r.rev,
                "tracker_revision": r.tracker_revision.isoformat(),
                "digest": r.digest,
                "seen_seq": r.seen_seq,
            }
            for r in record.revisions
        ],
        "first_seen_seq": record.first_seen_seq,
        "last_seen_seq": record.last_seen_seq,
        "tombstone_seq": record.tombstone_seq,
        "absent_confirmations": record.absent_confirmations,
        "resurrected": record.resurrected,
        "considered_rev": record.considered_rev,
        "outbox_id": record.outbox_id,
        "flags": sorted(record.flags),
    }


def thread_to_document(thread: Thread) -> dict[str, Any]:
    """The JSON value stored under :data:`THREAD_KEY`."""
    checkpoint = thread.checkpoint
    return {
        "schema": thread.schema,
        "binding": {"tracker": thread.tracker, "project": thread.project},
        "issue": {
            "identifier": thread.issue.identifier,
            "url": thread.issue.url,
            "internal_id": thread.issue.internal_id,
            "global_id": thread.issue.global_id,
        },
        "seq": thread.seq,
        "state": thread.state.value,
        "missing_confirmations": thread.missing_confirmations,
        "considered_seq": thread.considered_seq,
        "checkpoint": None
        if checkpoint is None
        else {
            "scope": checkpoint.scope.value,
            "subject": checkpoint.subject,
            "token": checkpoint.token,
            "seq": checkpoint.seq,
        },
        "comments": {comment_id: _record(r) for comment_id, r in thread.records.items()},
    }


# -- decoding --------------------------------------------------------------------------------


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ThreadDocumentError(f"{what} is not an object")
    return value


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _when(value: Any) -> datetime:
    return datetime.fromisoformat(str(value))


def _decode_actor(value: Any) -> ThreadActor | None:
    if value is None:
        return None
    raw = _mapping(value, "author")
    return ThreadActor(str(raw["id"]), str(raw["kind"]), str(raw.get("display") or ""))


def _decode_marker(value: Any) -> OwnMark | None:
    if value is None:
        return None
    raw = _mapping(value, "marker")
    return OwnMark(str(raw["hive"]), str(raw["install"]), str(raw["outbox_id"]))


def _decode_record(comment_id: str, value: Any) -> CommentRecord:
    raw = _mapping(value, f"comment {comment_id}")
    revisions = tuple(
        CommentRevision(
            int(r["rev"]), _when(r["tracker_revision"]), str(r["digest"]), int(r["seen_seq"])
        )
        for r in (_mapping(item, "revision") for item in raw["revisions"])
    )
    if not revisions:
        raise ThreadDocumentError(f"comment {comment_id} has no revision")
    return CommentRecord(
        id=comment_id,
        global_id=_optional_str(raw.get("global_id")),
        author=_decode_actor(raw.get("author")),
        via=_optional_str(raw.get("via")),
        marker=_decode_marker(raw.get("marker")),
        role=Role(raw["role"]),
        evidence=Evidence(raw["evidence"]),
        created_at=_when(raw["created_at"]),
        body=_optional_str(raw.get("body")),
        revisions=revisions,
        first_seen_seq=int(raw["first_seen_seq"]),
        last_seen_seq=int(raw["last_seen_seq"]),
        state=RecordState(raw["state"]),
        tombstone_seq=_optional_int(raw.get("tombstone_seq")),
        absent_confirmations=int(raw.get("absent_confirmations", 0)),
        resurrected=int(raw.get("resurrected", 0)),
        considered_rev=_optional_int(raw.get("considered_rev")),
        outbox_id=_optional_str(raw.get("outbox_id")),
        flags=frozenset(str(flag) for flag in raw.get("flags", ())),
    )


def thread_from_document(value: Any) -> Thread:
    """The :class:`Thread` a stored value holds; :class:`ThreadDocumentError` if it is not one."""
    try:
        raw = _mapping(value, "intake.thread")
        schema = int(raw["schema"])
        if schema > THREAD_SCHEMA:
            raise ThreadDocumentError(
                f"intake.thread schema {schema} is newer than this host reads ({THREAD_SCHEMA})"
            )
        binding, issue = _mapping(raw["binding"], "binding"), _mapping(raw["issue"], "issue")
        stored = raw.get("checkpoint")
        checkpoint = None
        if stored is not None:
            ckpt = _mapping(stored, "checkpoint")
            checkpoint = ThreadCheckpoint(
                CheckpointScope(ckpt["scope"]),
                str(ckpt["subject"]),
                str(ckpt["token"]),
                int(ckpt["seq"]),
            )
        comments = _mapping(raw.get("comments", {}), "comments")
        records = [_decode_record(str(cid), item) for cid, item in comments.items()]
        records.sort(key=lambda r: (r.first_seen_seq, r.id))
        return Thread(
            tracker=str(binding["tracker"]),
            project=str(binding["project"]),
            issue=ThreadIssue(
                str(issue["identifier"]),
                str(issue["url"]),
                _optional_str(issue.get("internal_id")),
                _optional_str(issue.get("global_id")),
            ),
            seq=int(raw["seq"]),
            checkpoint=checkpoint,
            state=ThreadState(raw.get("state", ThreadState.PRESENT.value)),
            missing_confirmations=int(raw.get("missing_confirmations", 0)),
            considered_seq=int(raw.get("considered_seq", 0)),
            records={r.id: r for r in records},
            schema=schema,
        )
    except ThreadDocumentError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ThreadDocumentError(f"malformed intake.thread document: {exc}") from exc


__all__ = ["THREAD_KEY", "ThreadDocumentError", "thread_from_document", "thread_to_document"]
