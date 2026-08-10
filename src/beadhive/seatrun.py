"""The role-binary contract, as `bh` consumes it (bh-c6dk.2).

`docs/design/work-runtime-tiers-adr.md` Amendment 2 §1 is the specification; this module is
the beadhive-side parsing/classification layer that sits between a spawned seat process and any
tier that schedules one (`local` today, `temporal` later). It does NOT bake authority, run a
CLI wrapper, or implement `--provider`/permission baking — those live in `baml-harness`'s own
build, filed separately, out of this hive's scope. What lives here is everything a *caller* of
an already-built seat binary needs to do correctly:

- parse `SeatRun` / `RoleOutcome` off stdout (`SeatRun`/`RoleOutcome`, `parse_seat_run`)
- classify a completed run from (exit code, stdout) into ONE outcome, so no tier reinvents this
  and nobody reads a 0 exit as "succeeded" (`classify_run`) — the taxonomy in the ADR's `EXIT`
  row (`0 done · 10 blocked · 11 handoff`) is the TARGET and is unbuilt upstream today: exit is
  a 2-value signal (0 whenever a `SeatRun` came back at all, 1 when BAML threw first). Until the
  taxonomy lands, `classify_run` never treats exit 0 alone as success — see its docstring.
- validate `--workspace` before spawn, so a bad path yields a typed result instead of a raw
  `ENOENT` traceback indistinguishable from an unimplemented-provider panic (`validate_workspace`)
- recognize a killed run's priced envelope, which still carries `session_id`, `cost_usd`,
  `usage` and a `terminal_reason` even though the run never reached a clean `RoleOutcome`
  (`Envelope`, `parse_envelope`)
- decide whether a bead is already advanced enough that re-dispatching a role binary against it
  would be a no-op (`already_advanced`) — the ADR's INVARIANT row, made mechanical rather than
  left to agent judgment

`RoleOutcome.status` is the escalation channel; `classify_run`'s result is what every tier is
supposed to consult, and it is deliberately the ONLY place that logic lives (acceptance
criterion on bh-c6dk.2).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import StrEnum

#: Never sent — SIGINT-cancelled runs exit 0, colliding with `0 = done`. See CANCEL rung 3 in
#: Amendment 2 §1. Nothing in this module sends signals (that's the scheduler's job, bh-c6dk.5 /
#: bh-c6dk.4), but the constant lives here so the rule has exactly one citable home.
NEVER_SIGINT = "SIGTERM only — a SIGINT-cancelled run exits 0 and collides with 0 = done"

#: The TARGET exit taxonomy from Amendment 2 §1. UNBUILT upstream as of this bead: today's
#: shipped binary exits 0 whenever a `SeatRun` came back (whatever `.outcome.status` says) and 1
#: whenever BAML threw before producing one. `classify_run` treats these as *hints* to cross-check
#: against a parsed `SeatRun`, never as the source of truth on their own.
EXIT_DONE = 0
EXIT_BLOCKED = 10
EXIT_HANDOFF = 11


class Status(StrEnum):
    """`RoleOutcome.status` — the escalation channel. Values match the wire format exactly."""

    DONE = "done"
    BLOCKED = "blocked"
    HANDOFF = "handoff"


@dataclass(frozen=True)
class RoleOutcome:
    """`{ status, summary, bead_id?, next_action? }` — the typed return every seat produces.

    `status` is the source of truth whenever stdout parses at all; `summary`/`next_action` are
    the payload a human or scheduler reads. `bead_id` is model-echoed prose upstream today (no
    `--bead` input exists to cross-check it against in the shipped binary) but the ADR's target
    contract adds `--bead`, so `parse_seat_run`/`classify_run` cross-check it against the
    caller's own `--bead` argument whenever both are present.
    """

    status: str
    summary: str
    bead_id: str | None = None
    next_action: str | None = None


@dataclass(frozen=True)
class SeatRun:
    """`{ outcome, session_id, cost_usd, usage, packs }` — one line of JSON on stdout.

    `usage` and `cost_usd` are exact (bh-a7so.7 §10-§11 retired the earlier "under-reports by
    35-40%" claim as a transcript double-count) and are safe bases for a budget.
    """

    outcome: RoleOutcome
    session_id: str
    cost_usd: float
    usage: dict = field(default_factory=dict)
    packs: list = field(default_factory=list)


class SeatRunParseError(ValueError):
    """`stdout` did not parse as a well-formed `SeatRun` line."""


def parse_seat_run(stdout: str) -> SeatRun:
    """Parse one line of `SeatRun` JSON off a seat's stdout.

    Raises `SeatRunParseError` (never a raw `json.JSONDecodeError`/`KeyError`) on anything that
    isn't well-formed — blank stdout, multiple lines, a shape missing `outcome`/`status`. Callers
    that only want a best-effort parse (never raising) should go through `classify_run` instead;
    this function is the strict primitive it's built on.
    """
    text = (stdout or "").strip()
    if not text:
        raise SeatRunParseError("empty stdout — no SeatRun line was produced")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) != 1:
        raise SeatRunParseError(f"expected exactly one JSON line on stdout, got {len(lines)}")
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise SeatRunParseError(f"stdout is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SeatRunParseError("stdout JSON is not an object")

    outcome_raw = payload.get("outcome")
    if not isinstance(outcome_raw, dict) or "status" not in outcome_raw:
        raise SeatRunParseError("missing outcome.status")
    if outcome_raw["status"] not in {s.value for s in Status}:
        raise SeatRunParseError(f"unknown outcome.status {outcome_raw['status']!r}")

    if "session_id" not in payload:
        raise SeatRunParseError("missing session_id")

    outcome = RoleOutcome(
        status=outcome_raw["status"],
        summary=outcome_raw.get("summary", ""),
        bead_id=outcome_raw.get("bead_id"),
        next_action=outcome_raw.get("next_action"),
    )
    return SeatRun(
        outcome=outcome,
        session_id=payload["session_id"],
        cost_usd=float(payload.get("cost_usd", 0.0)),
        usage=payload.get("usage") or {},
        packs=payload.get("packs") or [],
    )


@dataclass(frozen=True)
class Envelope:
    """The priced envelope a killed run still emits (~0.63s later, per bh-a7so.7 §4/§9), carrying
    enough to price the run even though it never reached a clean `RoleOutcome`. Distinguished from
    a full `SeatRun` by the absence of `outcome` — a killed run has no status to report."""

    session_id: str
    cost_usd: float
    usage: dict
    terminal_reason: str | None = None


def parse_envelope(stdout: str) -> Envelope | None:
    """Best-effort: does *stdout* look like a killed-run envelope (session_id/cost_usd/usage,
    no `outcome`)? Returns `None` rather than raising — this is a secondary shape a caller checks
    only after `parse_seat_run` fails, never the primary path."""
    text = (stdout or "").strip()
    if not text:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) != 1:
        return None
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or "outcome" in payload:
        return None
    if "session_id" not in payload:
        return None
    return Envelope(
        session_id=payload["session_id"],
        cost_usd=float(payload.get("cost_usd", 0.0)),
        usage=payload.get("usage") or {},
        terminal_reason=payload.get("terminal_reason"),
    )


class RunOutcome(StrEnum):
    """What `classify_run` decided actually happened — a superset of `Status` that also names
    the "did not complete" case the ADR's EXIT row reserves for "anything else"."""

    DONE = "done"
    BLOCKED = "blocked"
    HANDOFF = "handoff"
    INCOMPLETE = "incomplete"  # no parseable SeatRun — killed, crashed, or never produced one


@dataclass(frozen=True)
class Classification:
    """The single verdict every tier is supposed to consult (never re-derive from raw exit code
    alone)."""

    outcome: RunOutcome
    seat_run: SeatRun | None = None
    envelope: Envelope | None = None
    bead_id_mismatch: bool = False
    detail: str = ""


def classify_run(exit_code: int, stdout: str, *, bead: str | None = None) -> Classification:
    """Classify one completed seat process from its (exit code, stdout) into a single
    `Classification`. **The only place any tier is meant to consult this logic** — bh-c6dk.2's
    acceptance criterion.

    Rules, in order:

    1. Prefer stdout: if it parses as a `SeatRun`, `.outcome.status` is the source of truth,
       full stop — regardless of what the exit code says. Both observed `blocked` results in
       `bh-a7so.1` exited 0, so the exit code is not asked to agree.
    2. `exit_code` is NEVER read as a success signal on its own. Today's binary is a 2-value
       signal: 0 whenever a `SeatRun` came back (whatever it says), 1 whenever BAML threw first.
       Even the target taxonomy (0 done · 10 blocked · 11 handoff) only ever *corroborates* a
       parsed status — it is not a substitute for stdout, because the target is unbuilt upstream
       today. A caller that skips this function and branches on `exit_code == 0` is exactly the
       bug this contract exists to prevent.
    3. If stdout doesn't parse as a `SeatRun` at all, look for a killed-run envelope (session_id
       / cost_usd / usage / terminal_reason survives a kill even with no outcome) — the run is
       still `INCOMPLETE`, but priced.
    4. If neither parses, the run is `INCOMPLETE` with no pricing info (BAML threw before
       producing anything, or the process never wrote to stdout at all).
    5. When *bead* is given, cross-check it against `SeatRun.outcome.bead_id` when both are
       present; a mismatch is flagged but does not change `outcome` — that's a caller decision.
    """
    try:
        seat_run = parse_seat_run(stdout)
    except SeatRunParseError as exc:
        envelope = parse_envelope(stdout)
        if envelope is not None:
            return Classification(
                outcome=RunOutcome.INCOMPLETE,
                envelope=envelope,
                detail=f"killed before a full SeatRun; envelope survived ({exc})",
            )
        return Classification(
            outcome=RunOutcome.INCOMPLETE,
            detail=f"no SeatRun and no envelope on stdout (exit {exit_code}): {exc}",
        )

    mismatch = bool(bead and seat_run.outcome.bead_id and seat_run.outcome.bead_id != bead)
    return Classification(
        outcome=RunOutcome(seat_run.outcome.status),
        seat_run=seat_run,
        bead_id_mismatch=mismatch,
        detail="stdout parsed; exit code not consulted for status",
    )


@dataclass(frozen=True)
class WorkspaceValidation:
    """A typed result for `--workspace` validation — never a raw `ENOENT`/`OSError`."""

    ok: bool
    path: str
    reason: str = ""
    is_git_worktree: bool = False


def validate_workspace(path: str) -> WorkspaceValidation:
    """Validate a `--workspace` path BEFORE spawning a seat process.

    Today's shipped binary passes `workspace` straight into the child's `cwd` with zero
    validation — a bad path surfaces as a bare OS `ENOENT` on the `claude` spawn, indistinguishable
    from an unimplemented-provider panic (`bh-a7so.1` Evidence 2/3). This function is the
    beadhive-side guard a scheduler runs before spawn so that failure mode never reaches a caller
    as an unlabeled traceback.

    At minimum: the path must exist and be a directory. `is_git_worktree` additionally reports
    whether it looks like a git worktree (`.git` present, either a real directory for the main
    clone or the `gitdir:` pointer file a linked worktree uses) — the ADR's "preferably: is a git
    worktree" bar. A workspace that exists but isn't a worktree is still `ok=True`; that stronger
    bar is informational, not a hard refusal, matching the ADR's wording.
    """
    if not path:
        return WorkspaceValidation(ok=False, path=path, reason="--workspace is required")
    if not os.path.exists(path):
        return WorkspaceValidation(ok=False, path=path, reason=f"no such path: {path!r}")
    if not os.path.isdir(path):
        return WorkspaceValidation(ok=False, path=path, reason=f"not a directory: {path!r}")

    git_marker = os.path.join(path, ".git")
    is_worktree = os.path.exists(git_marker)
    return WorkspaceValidation(ok=True, path=path, is_git_worktree=is_worktree)


#: Bead states past which re-dispatching a role binary against the same bead is a no-op — the
#: ADR's INVARIANT row, made mechanical. A fresh `claim` only makes sense against `open` (not yet
#: started) or `in_progress` (still being worked); anything past that has already advanced and a
#: re-run would duplicate work a later stage already accounted for.
_ADVANCED_STATUSES = frozenset(
    {"review:pending", "review:approved", "review:changes-requested", "merged", "closed"}
)


def already_advanced(bead_status: str) -> bool:
    """Is *bead_status* past the point where re-dispatching a role binary is meaningful?

    Mechanizes the ADR's INVARIANT row ("re-run against an already-advanced bead is a no-op —
    observed, held by agent judgment, unenforced today"). A scheduler calls this before spawning
    a seat and skips the dispatch when it returns `True`, rather than relying on the seat itself
    to notice.
    """
    return bead_status in _ADVANCED_STATUSES
