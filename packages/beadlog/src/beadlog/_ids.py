"""The v0.1 event id rule (format ADR section 4) — frozen for all of ``0.1.x``."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from ._json import canonical_bytes, thaw
from ._model import BeadEvent, BeadRef, Evidence, Source

# Top-level identity members, each projected only when present and not null. ``source`` is
# added separately, reduced to exactly {system, ref}.
IDENTITY_MEMBERS = (
    "kind",
    "bead",
    "occurred_at",
    "occurred_precision",
    "recorded_at",
    "provenance",
    "actor",
    "lamport",
    "prev",
    "data",
)


def identity_projection(event: BeadEvent | Mapping[str, Any]) -> dict[str, Any]:
    """The JSON object an event id hashes: identity members only, top-level nulls dropped.

    Accepts a :class:`BeadEvent` or a raw event object. Inside ``bead``, ``data`` and
    ``source`` every member is kept as written (a ``null`` inside ``data`` is significant).
    """
    raw = event.to_json() if isinstance(event, BeadEvent) else event
    projection = {name: thaw(raw[name]) for name in IDENTITY_MEMBERS if raw.get(name) is not None}
    source = raw.get("source")
    if not isinstance(source, Mapping) or "system" not in source or "ref" not in source:
        raise ValueError("an event needs source.system and source.ref to have an id")
    projection["source"] = {"system": source["system"], "ref": source["ref"]}
    return projection


def event_id(event: BeadEvent | Mapping[str, Any]) -> str:
    """``sha256:`` + lowercase hex SHA-256 of the RFC 8785 identity projection's UTF-8 bytes.

    Excluded members (``id``, ``extracted_at``, ``confidence``, ``source.evidence``,
    ``redacted``, ``annotations``) never change the id, so re-extraction is idempotent.
    """
    return "sha256:" + hashlib.sha256(canonical_bytes(identity_projection(event))).hexdigest()


def mint_event(
    kind: str,
    bead: BeadRef,
    data: Mapping[str, Any],
    *,
    occurred_at: str | None,
    recorded_at: str | None,
    extracted_at: str,
    source: Source | tuple[str, str],
    provenance: str,
    occurred_precision: str | None = None,
    confidence: float | None = None,
    actor: str | None = None,
    lamport: int | None = None,
    prev: Sequence[str] | None = None,
    evidence: Sequence[Evidence] = (),
    annotations: Mapping[str, Any] | None = None,
) -> BeadEvent:
    """Build an event and mint its id from its identity material.

    ``source`` may be a :class:`Source` or a ``(system, ref)`` pair (then ``evidence`` is
    attached to it). ``prev`` is sorted and de-duplicated as the format requires.
    """
    if not isinstance(source, Source):
        system, ref = source
        source = Source(system, ref, tuple(evidence))
    draft = BeadEvent(
        id="sha256:" + "0" * 64,
        kind=kind,
        bead=bead,
        occurred_at=occurred_at,
        recorded_at=recorded_at,
        extracted_at=extracted_at,
        source=source,
        provenance=provenance,
        data=data,
        occurred_precision=occurred_precision,
        confidence=confidence,
        actor=actor,
        lamport=lamport,
        prev=tuple(sorted(set(prev))) if prev is not None else None,
        annotations=annotations,
    )
    raw = draft.to_json()
    raw["id"] = event_id(raw)
    return BeadEvent.from_json(raw)
