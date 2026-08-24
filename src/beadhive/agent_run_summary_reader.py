"""The `AgentRunSummary` read path (bh-6eu2c.2) over a per-hive dispatch sink.

`docs/design/agent-run-summary-projection-contract.md` fixes the state-derivation rules; the
typed shape and the two pure single-record mapping helpers (`state_for_seat_harvested`,
`SEAT_CANCELLED_STATE`) live in `agent_run_summary.py` (bh-6eu2c.1) and are reused, not
re-derived. This module is the stateful part the contract module explicitly does not do:
replaying `dispatch_log.tail_records` in file order to correlate `seat_spawned` /
`seat_harvested` / `seat_cancelled` / `dispatch_pass` records into one `AgentRunSummary` per
`session_id` (plus bead-keyed `waiting` entries, which have no session yet).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config, dispatch_log, registry
from .agent_run_summary import (
    SEAT_CANCELLED_STATE,
    AgentRunState,
    AgentRunSummary,
    Freshness,
    state_for_seat_harvested,
)


def _parse_timestamp(value: object) -> float | None:
    """Best-effort ISO8601 -> unix seconds. Never raises: a record with a torn/foreign
    `timestamp` field should not crash the read any more than a torn JSON line does."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def compute_freshness(sink: Path, *, now: float | None = None) -> Freshness:
    """Describe the sink observation without claiming unproved writer colocation.

    A path under this process's ``config.home()`` and a recent mtime prove only that this reader
    can see a recently modified file. They do not prove that one of the sink's possible writers
    is alive on this host: the file can be copied in, bind-mounted, or left behind by a writer
    that exited immediately after its last append. The current sink format has no writer token,
    PID marker, or other positive colocation signal, so the projection contract requires this
    reader to fail closed to ``unknown``.

    Preserve the mtime as ``as_of`` because it is still a truthful observation useful to a
    consumer. ``now`` exists only to make the human-readable age deterministic in tests; it is
    deliberately not a freshness-state threshold.
    """
    try:
        mtime = sink.stat().st_mtime
    except OSError:
        return Freshness()
    clock = now if now is not None else time.time()
    age = max(clock - mtime, 0.0)
    return Freshness(
        state="unknown",
        as_of=mtime,
        detail=f"writer colocation unverified; sink last written {age:.0f}s ago",
    )


@dataclass
class _Run:
    """Mutable accumulator for one session's (or bead's, for `waiting`) replay, before freezing
    into an `AgentRunSummary`."""

    bead: str
    session_id: str
    state: AgentRunState
    owner_seat: str | None = None
    started_at: float | None = None
    updated_at: float | None = None
    ended_at: float | None = None


def read_agent_run_summaries(hive: str = "", *, cfg: dict | None = None) -> list[AgentRunSummary]:
    """One `AgentRunSummary` per `session_id` seen in *hive*'s dispatch sink, plus one
    bead-keyed entry per bead currently `waiting` (no session yet) — see the projection contract
    doc for the full state-derivation rules this replays.

    Never raises on a torn/unparseable sink line (`tail_records` already skips those) and
    returns `[]`, not an error, when the sink does not exist yet — same hive-resolution
    convention as `dispatch_status.compute_status` (`registry.hive_dir_for` /
    `registry.entry_for_dir` -> `dispatch_log.hive_slug` -> `sink_path_for_slug`).
    """
    cfg = cfg if cfg is not None else config.load()
    main = registry.hive_dir_for(cfg, hive)
    entry = registry.entry_for_dir(cfg, main) or {}
    slug = dispatch_log.hive_slug(entry)
    sink = dispatch_log.sink_path_for_slug(slug)
    return read_from_sink(sink)


def read_from_sink(sink: Path) -> list[AgentRunSummary]:
    """The correlation itself, over an already-resolved sink path — split out from
    `read_agent_run_summaries` so a test can point it at a fixture file without faking hive
    resolution."""
    records = dispatch_log.tail_records(sink, lines=0)  # 0 == the whole file, oldest first
    freshness = compute_freshness(sink)

    sessions: dict[str, _Run] = {}
    #: The bead's currently-open (not yet terminal) session, if any. Used to (a) promote
    #: `starting -> active` when a later `dispatch_pass.in_flight` still names the bead, and
    #: (b) keep a bead out of `waiting` while it already has a live session.
    open_session_for_bead: dict[str, str] = {}
    waiting: dict[str, _Run] = {}

    for record in records:
        if not isinstance(record, dict):
            continue  # tail_records only ever yields dicts, but never trust it further than that
        event = record.get("event")
        ts = _parse_timestamp(record.get("timestamp"))

        if event == "seat_spawned":
            bead, session_id = record.get("bead"), record.get("session_id")
            if not isinstance(bead, str) or not isinstance(session_id, str) or not session_id:
                continue
            waiting.pop(bead, None)  # a spawn ends `waiting` for this bead, per the contract
            role = record.get("role")
            sessions[session_id] = _Run(
                bead=bead,
                session_id=session_id,
                state=AgentRunState.STARTING,
                owner_seat=role if isinstance(role, str) else None,
                started_at=ts,
                updated_at=ts,
            )
            open_session_for_bead[bead] = session_id

        elif event == "dispatch_pass":
            in_flight = record.get("in_flight")
            in_flight_beads = set(in_flight) if isinstance(in_flight, list) else set()

            for bead, session_id in open_session_for_bead.items():
                run = sessions.get(session_id)
                if run and run.state is AgentRunState.STARTING and bead in in_flight_beads:
                    run.state = AgentRunState.ACTIVE
                    if ts is not None:
                        run.updated_at = ts

            # `waiting`: PassReport.denied carries no bead itself (just {reason, detail} — an
            # admission-cap verdict, not a per-bead record), so the bead identity comes from
            # `decision.beads` (the ready candidates this pass considered): when `denied` is
            # non-empty, this pass could not admit (some of) those candidates. A candidate that
            # DID get admitted this pass shows up in `in_flight` by pass end, so filtering
            # `decision.beads` down to "not in this pass's in_flight" recovers exactly the
            # denied-and-never-spawned subset the contract describes.
            denied = record.get("denied")
            if isinstance(denied, list) and denied:
                decision = record.get("decision")
                candidates = decision.get("beads") if isinstance(decision, dict) else None
                for bead in candidates if isinstance(candidates, list) else []:
                    if not isinstance(bead, str):
                        continue
                    if bead in in_flight_beads or bead in open_session_for_bead:
                        continue
                    waiting[bead] = _Run(
                        bead=bead, session_id="", state=AgentRunState.WAITING, updated_at=ts
                    )

        elif event in ("seat_harvested", "seat_cancelled"):
            bead, session_id = record.get("bead"), record.get("session_id")
            if not isinstance(session_id, str) or session_id not in sessions:
                continue
            run = sessions[session_id]
            if event == "seat_harvested":
                outcome = record.get("outcome")
                run.state = state_for_seat_harvested(outcome if isinstance(outcome, str) else "")
            else:
                run.state = SEAT_CANCELLED_STATE
            run.ended_at = ts
            run.updated_at = ts
            if isinstance(bead, str) and open_session_for_bead.get(bead) == session_id:
                del open_session_for_bead[bead]

    def _freeze(run: _Run) -> AgentRunSummary:
        return AgentRunSummary(
            bead=run.bead,
            session_id=run.session_id,
            state=run.state,
            owner_seat=run.owner_seat,
            started_at=run.started_at,
            updated_at=run.updated_at,
            ended_at=run.ended_at,
            freshness=freshness,
        )

    return [_freeze(r) for r in sessions.values()] + [_freeze(r) for r in waiting.values()]
