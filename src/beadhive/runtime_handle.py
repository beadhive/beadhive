"""Persisted runtime handles — what lets a restarted loop ADOPT a live session (ADR Decision 4).

A runtime whose sessions outlive the process that scheduled them (``adoptable = True``; Gas City
hosts seats this way) leaves the loop with a problem the in-memory in-flight map cannot answer: on
restart the map is empty, so a still-running session looks exactly like a dead one, and the loop
would reclaim its bead or schedule a second session for it. Decision 4's rule is that a runtime
handle is live until ``observe()`` reports it terminal or the claim's lease expires. To apply that
rule after a restart the loop needs the handle back, so it keeps ONE bead metadata document per
bead, under :data:`METADATA_KEY`:

``bh.runtime.handle`` = ``{"runtime": <name>, "session_id": <sid>, "role": <role>,
"action": <decision verb>, "scheduled_at": <ISO-8601 UTC wall-clock time>}``

Rules:

- **Only for an adoptable runtime.** A ``LocalRuntime`` seat is a child of the loop and dies with
  it, so there is nothing to adopt and the loop writes (and clears) nothing — the default path is
  byte-for-byte unchanged.
- **The LOOP writes it, never the runtime.** It is a lifecycle fact, and a runtime makes no
  lifecycle writes (Decision 4; the conformance kit checks it). Same ``bd update --metadata``
  route the outcome sink uses, so every other key survives.
- **One key, the CURRENT turn.** Each schedule replaces it; harvesting or cancelling a turn
  removes it. A handle present on a bead therefore means "scheduled and not yet harvested by any
  loop" — which is also why a restart harvests a turn that finished while no loop was running.
- **``scheduled_at`` is wall-clock.** The loop's wall-time cap runs on its monotonic clock, which
  means nothing across a restart; an adopting loop rebuilds the run's age from this timestamp.
- **Never authoritative about the bead.** The claim, its lease and the outcome record win: a
  bead whose claim's ``lease_expires_at`` has passed (:func:`lease_expired`) is not adopted,
  even while it is still ``in_progress`` inside ``bd reclaim``'s grace window (operator decision
  2026-11-26); a bead reclaim has reverted is no longer ``in_progress`` and is not adopted
  either; and a fresh session id on re-dispatch means the old session's late
  ``bh.outcome.<sid>`` record can never answer for the new turn.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import bd, bd_cli, outcome
from .runtime.protocol import RoleHandle

#: The one flat metadata key; the dot is part of the key, not a JSON path.
METADATA_KEY = "bh.runtime.handle"

_FIELDS = ("runtime", "session_id", "role", "action", "scheduled_at")


@dataclass(frozen=True)
class PersistedHandle:
    """A :data:`METADATA_KEY` document, parsed."""

    bead_id: str
    runtime: str
    session_id: str
    role: str
    action: str
    scheduled_at: datetime

    @property
    def handle(self) -> RoleHandle:
        return RoleHandle(bead_id=self.bead_id, session_id=self.session_id)

    def document(self) -> dict:
        return {
            "runtime": self.runtime,
            "session_id": self.session_id,
            "role": self.role,
            "action": self.action,
            "scheduled_at": _iso(self.scheduled_at),
        }

    def age_seconds(self, now: datetime) -> float:
        """Seconds since ``scheduled_at`` by the wall clock, never negative (clock skew)."""
        return max((now - self.scheduled_at).total_seconds(), 0.0)


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def parse(metadata: Mapping[str, Any] | None, bead_id: str) -> PersistedHandle | None:
    """The bead's persisted handle out of an already-loaded metadata mapping, or ``None`` when
    there is none or it is malformed (a document missing a field, or not a JSON object)."""
    raw = (metadata or {}).get(METADATA_KEY)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, Mapping):
        return None
    if not all(isinstance(raw.get(f), str) and raw.get(f) for f in _FIELDS):
        return None
    scheduled_at = _parse_time(raw["scheduled_at"])
    if scheduled_at is None:
        return None
    return PersistedHandle(
        bead_id=bead_id,
        runtime=str(raw["runtime"]),
        session_id=str(raw["session_id"]),
        role=str(raw["role"]),
        action=str(raw["action"]),
        scheduled_at=scheduled_at,
    )


def from_row(row: Mapping[str, Any] | None) -> PersistedHandle | None:
    """The persisted handle on a ``bd show`` row."""
    row = row or {}
    return parse(row.get("metadata"), str(row.get("id") or ""))


def terminal_record(row: Mapping[str, Any] | None, handle: PersistedHandle) -> bool:
    """Has the handle's session already written its ``bh.outcome.<session_id>`` record?"""
    metadata = (row or {}).get("metadata")
    return outcome.from_metadata(metadata, handle.session_id, bead_id=handle.bead_id) is not None


class HandleWriteError(RuntimeError):
    """``bd`` refused the handle write or removal."""


def write(cwd, handle: PersistedHandle, *, actor: str) -> None:
    """Store *handle* as the bead's current turn (replacing any earlier one)."""
    res = bd_cli.routes(cwd).issue_set_metadata_document(
        handle.bead_id, METADATA_KEY, handle.document(), actor=actor
    )
    if res.returncode != 0:
        raise HandleWriteError(
            f"bd update --metadata failed for {handle.bead_id}: {bd.err_line(res)}"
        )


def clear(cwd, bead_id: str, *, actor: str) -> None:
    """Remove the bead's handle: the turn it named was harvested or cancelled."""
    res = bd_cli.routes(cwd).issue_unset_metadata(bead_id, [METADATA_KEY], actor=actor)
    if res.returncode != 0:
        raise HandleWriteError(
            f"bd update --unset-metadata failed for {bead_id}: {bd.err_line(res)}"
        )


def lease_expired(row: Mapping[str, Any] | None, now: datetime) -> bool:
    """Has the claim's lease (``lease_expires_at`` on the ``bd show`` row) run out? A row with no
    readable lease time is NOT expired: absence of evidence never frees a live session."""
    expires = _parse_time((row or {}).get("lease_expires_at"))
    return expires is not None and expires <= now


def reclaim_refusal(row: Mapping[str, Any] | None, *, now: datetime) -> str | None:
    """Why reclaiming this bead now would be unsafe, or ``None`` when it is not.

    Decision 4: a handle is live until ``observe()`` is terminal or the lease expires. Without a
    runtime to ask, the durable evidence stands in for ``observe()``: the bead is still
    ``in_progress``, carries a handle, its session has written no outcome record, and the claim's
    lease has not expired. Reclaiming such a bead would let it be dispatched again while the
    first session may still be running — two sessions for one bead. The message names the
    runtime and session so the operator knows what to check first."""
    row = row or {}
    if str(row.get("status") or "") != "in_progress":
        return None
    handle = from_row(row)
    if handle is None or terminal_record(row, handle) or lease_expired(row, now):
        return None
    return (
        f"{handle.bead_id} has a live {handle.runtime!r} runtime handle (session "
        f"{handle.session_id}, {handle.role} seat for {handle.action}, scheduled "
        f"{_iso(handle.scheduled_at)}) and its lease has not expired: the session may still be "
        f"running, and reclaiming the bead now could start a second {handle.runtime!r} session "
        "for it. Check the session in the runtime, wait for it to finish or for the lease to "
        "expire, or force the reclaim."
    )
