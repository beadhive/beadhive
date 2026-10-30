"""BeadLog v0.1 event envelope (format ADR sections 2 and 5) as frozen value objects."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ._json import freeze, thaw
from ._time import check_timestamp

_EVENT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


class EventKind(StrEnum):
    """The twelve v0.1 kinds (format ADR section 5)."""

    CREATE = "create"
    SET = "set"
    LABEL_ADD = "label_add"
    LABEL_REMOVE = "label_remove"
    EDGE_ADD = "edge_add"
    EDGE_REMOVE = "edge_remove"
    COMMENT = "comment"
    PHASE = "phase"
    RETIRE = "retire"
    DELETE = "delete"
    TOMBSTONE = "tombstone"
    CHECKPOINT = "checkpoint"


class Provenance(StrEnum):
    """observed: read from a system of record; derived: computed; inferred: a judgment."""

    OBSERVED = "observed"
    DERIVED = "derived"
    INFERRED = "inferred"


class Precision(StrEnum):
    """Precision of ``occurred_at`` when coarser than written (the start of the interval)."""

    SECOND = "second"
    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"
    MONTH = "month"
    YEAR = "year"


class EventFormatError(ValueError):
    """A JSON object that is not a well-formed BeadLog event envelope."""


@dataclass(frozen=True, order=True)
class BeadRef:
    """Durable bead identity ``(group, id)``; readable prefixes are aliases, not identity.

    Ordering compares ``group`` then ``id`` by code point, which equals the UTF-8 byte order
    the archive part order is defined in.
    """

    group: str
    id: str

    def to_json(self) -> dict[str, str]:
        return {"group": self.group, "id": self.id}

    @classmethod
    def from_json(cls, value: Any) -> BeadRef:
        if not isinstance(value, Mapping):
            raise EventFormatError(f"a bead reference must be an object, got {value!r}")
        group, ident = value.get("group"), value.get("id")
        if not isinstance(group, str) or not group or not isinstance(ident, str) or not ident:
            raise EventFormatError(f"a bead reference needs non-empty group and id: {value!r}")
        return cls(group, ident)


@dataclass(frozen=True, order=True)
class Edge:
    """A directed edge from a bead to ``target`` (``"A depends on B"`` is an edge on A to B)."""

    type: str
    target: BeadRef

    def to_json(self) -> dict[str, Any]:
        return {"target": self.target.to_json(), "type": self.type}

    @classmethod
    def from_json(cls, value: Any) -> Edge:
        if not isinstance(value, Mapping) or not isinstance(value.get("type"), str):
            raise EventFormatError(f"an edge needs a type and a target: {value!r}")
        return cls(value["type"], BeadRef.from_json(value.get("target")))


@dataclass(frozen=True)
class Evidence:
    """Supporting material for an event. Not identity; merges take the set union."""

    type: str
    ref: str
    note: str | None = None

    def to_json(self) -> dict[str, str]:
        out = {"type": self.type, "ref": self.ref}
        if self.note is not None:
            out["note"] = self.note
        return out

    @classmethod
    def from_json(cls, value: Any) -> Evidence:
        if not isinstance(value, Mapping):
            raise EventFormatError(f"evidence must be an object, got {value!r}")
        kind, ref, note = value.get("type"), value.get("ref"), value.get("note")
        if not isinstance(kind, str) or not isinstance(ref, str):
            raise EventFormatError(f"evidence needs a string type and ref: {value!r}")
        if note is not None and not isinstance(note, str):
            raise EventFormatError(f"evidence note must be a string: {value!r}")
        return cls(kind, ref, note)


@dataclass(frozen=True)
class Source:
    """Where a fact came from. ``system`` and ``ref`` are identity; ``evidence`` is not."""

    system: str
    ref: str
    evidence: tuple[Evidence, ...] = ()

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"system": self.system, "ref": self.ref}
        if self.evidence:
            out["evidence"] = [item.to_json() for item in self.evidence]
        return out

    @classmethod
    def from_json(cls, value: Any) -> Source:
        if not isinstance(value, Mapping):
            raise EventFormatError(f"source must be an object, got {value!r}")
        system, ref = value.get("system"), value.get("ref")
        if not isinstance(system, str) or not isinstance(ref, str) or not ref:
            raise EventFormatError(f"source needs a string system and a non-empty ref: {value!r}")
        evidence = value.get("evidence") or ()
        return cls(system, ref, tuple(Evidence.from_json(item) for item in evidence))


def _optional_str(value: Any, name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise EventFormatError(f"{name} must be a string or null, got {value!r}")
    return value


@dataclass(frozen=True)
class BeadEvent:
    """One atomic, content-addressed bead history event (format ADR section 2).

    JSON-valued members (``data``, ``annotations``) are deep-frozen on construction. The
    dataclass hash covers only hashable members; equality compares every member.
    """

    id: str
    kind: EventKind
    bead: BeadRef
    occurred_at: str | None
    recorded_at: str | None
    extracted_at: str
    source: Source = field(hash=False)
    provenance: Provenance
    data: Mapping[str, Any] = field(hash=False)
    occurred_precision: Precision | None = None
    confidence: float | None = None
    actor: str | None = None
    lamport: int | None = None
    prev: tuple[str, ...] | None = None
    redacted: tuple[str, ...] | None = None
    annotations: Mapping[str, Any] | None = field(default=None, hash=False)

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        if not isinstance(self.id, str) or _EVENT_ID.fullmatch(self.id) is None:
            raise EventFormatError(f"event id must be sha256:<64 lowercase hex>, got {self.id!r}")
        try:
            set_(self, "kind", EventKind(self.kind))
            set_(self, "provenance", Provenance(self.provenance))
            if self.occurred_precision is not None:
                set_(self, "occurred_precision", Precision(self.occurred_precision))
        except ValueError as exc:
            raise EventFormatError(str(exc)) from exc
        for name in ("occurred_at", "recorded_at"):
            value = getattr(self, name)
            if value is not None:
                check_timestamp(value)
        check_timestamp(self.extracted_at)
        if not isinstance(self.data, Mapping):
            raise EventFormatError(f"data must be an object, got {self.data!r}")
        set_(self, "data", freeze(self.data))
        if self.annotations is not None:
            set_(self, "annotations", freeze(self.annotations))
        if self.prev is not None:
            set_(self, "prev", tuple(self.prev))
        if self.redacted is not None:
            set_(self, "redacted", tuple(self.redacted))

    def to_json(self) -> dict[str, Any]:
        """The event as a plain JSON object; absent optional members are omitted."""
        out: dict[str, Any] = {
            "id": self.id,
            "kind": str(self.kind),
            "bead": self.bead.to_json(),
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
            "extracted_at": self.extracted_at,
            "source": self.source.to_json(),
            "provenance": str(self.provenance),
            "data": thaw(self.data),
        }
        if self.occurred_precision is not None:
            out["occurred_precision"] = str(self.occurred_precision)
        if self.confidence is not None:
            out["confidence"] = self.confidence
        if self.actor is not None:
            out["actor"] = self.actor
        if self.lamport is not None:
            out["lamport"] = self.lamport
        if self.prev is not None:
            out["prev"] = list(self.prev)
        if self.redacted is not None:
            out["redacted"] = list(self.redacted)
        if self.annotations is not None:
            out["annotations"] = thaw(self.annotations)
        return out

    @classmethod
    def from_json(cls, value: Any) -> BeadEvent:
        """Parse one event object (a decoded JSONL line). The id is taken as written."""
        if not isinstance(value, Mapping):
            raise EventFormatError(f"an event must be a JSON object, got {type(value).__name__}")
        missing = [
            name
            for name in (
                "id",
                "kind",
                "bead",
                "occurred_at",
                "recorded_at",
                "extracted_at",
                "source",
                "provenance",
                "data",
            )
            if name not in value
        ]
        if missing:
            raise EventFormatError(f"event is missing required members: {', '.join(missing)}")
        confidence = value.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool) or not isinstance(confidence, (int, float))
        ):
            raise EventFormatError(f"confidence must be a number, got {confidence!r}")
        lamport = value.get("lamport")
        if lamport is not None and (isinstance(lamport, bool) or not isinstance(lamport, int)):
            raise EventFormatError(f"lamport must be an integer, got {lamport!r}")
        return cls(
            id=value["id"],
            kind=value["kind"],
            bead=BeadRef.from_json(value["bead"]),
            occurred_at=_optional_str(value["occurred_at"], "occurred_at"),
            recorded_at=_optional_str(value["recorded_at"], "recorded_at"),
            extracted_at=value["extracted_at"],
            source=Source.from_json(value["source"]),
            provenance=value["provenance"],
            data=value["data"],
            occurred_precision=value.get("occurred_precision"),
            confidence=confidence,
            actor=_optional_str(value.get("actor"), "actor"),
            lamport=lamport,
            prev=tuple(value["prev"]) if value.get("prev") is not None else None,
            redacted=tuple(value["redacted"]) if value.get("redacted") is not None else None,
            annotations=value.get("annotations"),
        )
