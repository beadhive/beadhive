"""Outcome sink in bead metadata — the SeatRun channel for seat sessions that are not child
processes (gas-city-runtime-seam-adr.md Decision 2).

The stdout channel (`seatrun.classify_run` over a held pipe) needs a runtime that spawned the
seat itself and kept its stdout. A process host that discards stdout or fires and forgets (Gas
City's subprocess / exec providers) cannot read it. ``bh role --outcome-sink bead`` closes that
gap: once the seat exits, the wrapper writes the SAME JSON line the seat printed to ONE bead
metadata key, ``bh.outcome.<session_id>``, under the seat's stamped identity. A runtime then
implements ``observe()`` as "record present ⇒ classified outcome; absent ⇒ ``running``"
(:func:`observe`).

Record schema — the value of metadata key ``bh.outcome.<session_id>`` is one JSON object, either:

- a full ``SeatRun`` (``{outcome: {status, summary, bead_id?, next_action?}, session_id,
  cost_usd, usage, packs}``) exactly as the seat printed it, or
- a killed-run envelope (``{session_id, cost_usd, usage, terminal_reason}``, no ``outcome``) —
  what the seat printed when it was cancelled, or, when it printed neither, one the wrapper
  synthesizes so a crashed run still answers ``failed`` instead of ``running`` forever.

Rules every reader inherits from :func:`read` / :func:`from_metadata`:

- Classification goes through :func:`beadhive.seatrun.classify_run`, the one shared rule set, so a
  record classifies exactly as the same line would on stdout. A record carries no exit code, and
  none is needed: ``classify_run`` never derives status from one, so a bare exit 0 is never
  success on this path either.
- A record whose own ``session_id`` differs from the key's (or that names no session at all) is
  IGNORED: a stale session can never answer for a new one.
- The record is append-only per session: the writer never overwrites an existing key, and BH
  only ever reads it. Nothing here is authoritative about bead lifecycle — bead state wins.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import bd, bd_cli, seatrun
from .runtime import RoleHandle, RoleOutcome

#: Flat metadata key prefix; the dots are part of the key, not a JSON path.
METADATA_PREFIX = "bh.outcome."

#: The sink choices ``bh role --outcome-sink`` accepts. ``stdout`` is the default and unchanged.
SINK_STDOUT = "stdout"
SINK_BEAD = "bead"
SINKS = (SINK_STDOUT, SINK_BEAD)

#: A metadata record has no exit code. ``classify_run`` only quotes it in prose detail and never
#: reads it for status; this sentinel makes that visible instead of pretending the run exited 0.
_NO_EXIT_CODE = -1


def metadata_key(session_id: str) -> str:
    """``bh.outcome.<session_id>`` — the one key a session's record lives under."""
    if not session_id:
        raise ValueError("an outcome record needs a session_id")
    return f"{METADATA_PREFIX}{session_id}"


def role_outcome(cls: seatrun.Classification, *, routing: dict | None = None) -> RoleOutcome:
    """Map a ``classify_run`` verdict to the runtime protocol's :class:`RoleOutcome`.

    The same mapping ``LocalRuntime.observe`` applies to a held pipe (a parity test pins the two
    together; ``localloop`` does not import this module, which would close an import cycle
    through ``runtime``): a run that produced no parseable ``SeatRun`` is ``failed``; otherwise
    the seat's own ``status`` and ``summary`` stand."""
    if cls.outcome is seatrun.RunOutcome.INCOMPLETE:
        return RoleOutcome(status="failed", summary=cls.detail, routing=routing)
    summary = cls.seat_run.outcome.summary if cls.seat_run else ""
    return RoleOutcome(status=str(cls.outcome), summary=summary, routing=routing)


def _recorded_session(cls: seatrun.Classification) -> str | None:
    if cls.seat_run is not None:
        return cls.seat_run.session_id
    if cls.envelope is not None:
        return cls.envelope.session_id
    return None


def from_metadata(
    metadata: Mapping[str, Any] | None, session_id: str, *, bead_id: str | None = None
) -> RoleOutcome | None:
    """Classify ``session_id``'s record out of an already-loaded metadata mapping.

    ``None`` means "no answer yet": the key is absent, the value is not a JSON object, or the
    record names a different session (stale / mismatched) or no session at all."""
    raw = (metadata or {}).get(metadata_key(session_id))
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, Mapping):
        text = json.dumps(raw)
    else:
        return None
    cls = seatrun.classify_run(_NO_EXIT_CODE, text, bead=bead_id)
    if _recorded_session(cls) != session_id:
        return None
    return role_outcome(cls)


def read(bead_id: str, session_id: str, *, cwd=None) -> RoleOutcome | None:
    """``session_id``'s classified outcome from ``bead_id``'s metadata, or ``None`` when no
    record for that session exists yet. ``cwd`` scopes the ``bd`` read to a hive."""
    data = bd.show(bead_id, cwd)
    return from_metadata((data or {}).get("metadata"), session_id, bead_id=bead_id)


def observe(handle: RoleHandle, *, cwd=None) -> RoleOutcome:
    """``observe()`` for a runtime whose sessions are not child processes: the record's
    classified outcome when present, ``running`` when absent. A status read, never a wait, and
    never authoritative about the bead's lifecycle."""
    found = read(handle.bead_id, handle.session_id, cwd=cwd)
    if found is not None:
        return found
    return RoleOutcome(status="running", summary=f"no {metadata_key(handle.session_id)} record")


def record_document(stdout: str, session_id: str, *, exit_code: int | None) -> dict:
    """The JSON object to store for one finished seat run.

    The seat's own line when it parses as a ``SeatRun`` or an envelope — stored as printed, not
    re-serialized from parsed fields. Otherwise an envelope synthesized for ``session_id`` whose
    ``terminal_reason`` names the missing ``SeatRun``, so the record classifies ``failed``."""
    text = (stdout or "").strip()
    try:
        seatrun.parse_seat_run(text)
    except seatrun.SeatRunParseError:
        if seatrun.parse_envelope(text) is None:
            return {
                "session_id": session_id,
                "cost_usd": 0.0,
                "usage": {},
                "terminal_reason": f"no SeatRun on stdout (exit {exit_code})",
            }
    return json.loads(text)


@dataclass(frozen=True)
class WriteResult:
    """What :func:`write` did. ``written`` is False when a record already existed (append-only)."""

    key: str
    written: bool
    document: dict
    detail: str = ""


class OutcomeWriteError(RuntimeError):
    """``bd`` refused the metadata write."""


def write(
    bead_id: str,
    session_id: str,
    stdout: str,
    *,
    exit_code: int | None,
    cwd,
    actor: str,
) -> WriteResult:
    """Write ``session_id``'s record onto ``bead_id`` as ``actor`` (the seat's stamped identity).

    Exactly one metadata key is touched — ``bh.outcome.<session_id>`` — through ``bd update
    --metadata``, which merges top-level keys, so every other key survives; no label is written.
    Append-only: an existing record for the session is left as it is."""
    key = metadata_key(session_id)
    document = record_document(stdout, session_id, exit_code=exit_code)
    existing = (bd.show(bead_id, cwd) or {}).get("metadata") or {}
    if key in existing:
        return WriteResult(key, False, document, "a record already exists for this session")
    res = bd_cli.routes(cwd).issue_set_metadata_document(bead_id, key, document, actor=actor)
    if res.returncode != 0:
        raise OutcomeWriteError(f"bd update --metadata failed for {bead_id}: {bd.err_line(res)}")
    return WriteResult(key, True, document)
