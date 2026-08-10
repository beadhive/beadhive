"""bd's coordination surface — thin, tested wrappers for `bd gate` (create/check/resolve),
`bd merge-slot` (create/acquire/release/check), `bd heartbeat`, and `bd reclaim` (bh-c6dk.3).

docs/design/work-runtime-tiers-adr.md's Evidence 1 table names these four as beads' own
durable-execution primitives — blocking wait/signal, exclusive mutex with a waiter queue,
worker liveness heartbeat, and liveness-timeout recovery — and its Consequences section is
explicit that they "move from lightly-used features to the contract every runtime depends on."
Every runtime tier (the `local` poll loop of bh-c6dk.5, the `MergerWorkflow` of bh-c6dk.6) calls
through here instead of re-deriving the same `bd` subprocess calls inline at each call site —
the exact drift `engine.py` was written to correct for passthrough/export/state verbs. This
module is the same shape one level up: it is built entirely on `bd.run`/`bd.json`, which already
route through the configured `Engine.passthrough` (see `engine.py`, `bd.py`'s module docstring)
— so wrapping here adds no second subprocess-calling seam, only typed, documented call shapes.

Command/output shapes below (JSON keys, exit codes, idempotency) were verified against a real
`bd` binary (bd version 1.1.0) during development; the docstring on each wrapper says what was
observed. `bd`'s own behavior is authoritative — these wrappers do not re-implement any of the
staleness/exclusivity/gate-resolution logic bd already owns, they only shape its calls and
outputs for a Python caller.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field

from . import bd as bd_mod


def _err(res) -> str:
    return bd_mod.err_line(res)


def _run_json(args, cwd, actor=""):
    """`bd_mod.run` (the Engine seam) with `--json` appended — every wrapper below wants JSON
    on both the success AND failure path (bd's JSON error shape, `{"error": "..."}`, still
    needs parsing), which `bd.json()` doesn't give: it returns `None` on any failure/parse
    error, discarding the message these wrappers report through `error=`."""
    return bd_mod.run([*args, "--json"], cwd, actor=actor, capture=True)


def _parse_json_tail(stdout: str):
    """Parse the trailing JSON object off `stdout`, tolerating the human-readable progress
    lines bd emits alongside `--json` on some verbs. Verified live: `bd gate check --json`
    still prints one `✓ <id>: resolved - ...` line per gate it closes, THEN a pretty-printed
    JSON object, even with `--json` set — `--json` changes the SUMMARY's shape, not whether the
    per-item lines are suppressed. The JSON block always starts at a line that is exactly `{`;
    take the last one so multiple per-item confirmations before it are never mistaken for the
    payload. Returns None if no such block parses."""
    text = stdout or ""
    try:
        return _json.loads(text)  # the common case: stdout IS the JSON, nothing prefixed
    except ValueError:
        pass
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip() == "{"]
    if not starts:
        return None
    try:
        return _json.loads("\n".join(lines[starts[-1] :]))
    except ValueError:
        return None


# ---- gate ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class GateCreateResult:
    """Outcome of `bd gate create`. `gate_id` is '' when `ok` is False."""

    ok: bool
    gate_id: str = ""
    error: str = ""


@dataclass(frozen=True)
class GateCheckResult:
    """Outcome of `bd gate check`. Verified shape:
    `{"checked": int, "resolved": int, "escalated": int, "errors": int, "dry_run": bool,
    "schema_version": int}`. `bd gate check` (no `--type`) evaluates every OPEN gate but only
    ever resolves timer/gh:run/gh:pr/bead gates — a `human` gate has no auto-resolution
    condition and is left open no matter how many times this runs (verified: a hive with one
    open human gate and nothing else reports `checked=0`, never touching it)."""

    ok: bool
    checked: int = 0
    resolved: int = 0
    escalated: int = 0
    errors: int = 0
    error: str = ""


@dataclass(frozen=True)
class GateResolveResult:
    """Outcome of `bd gate resolve <id>`. Verified: resolving an ALREADY-closed gate still
    exits 0 and prints "Gate resolved" — bd does not error on a redundant resolve, and it does
    NOT overwrite the gate's original `close_reason`/`closed_at` with the new call's `--reason`.
    So `ok=True` here does not mean "this call closed the gate" — callers that need to
    distinguish a fresh close from a no-op redundant one must check gate state before/after."""

    ok: bool
    error: str = ""


def gate_create(
    cwd,
    *,
    blocks: str,
    gate_type: str = "human",
    reason: str = "",
    title: str = "",
    await_id: str = "",
    timeout: str = "",
    actor: str = "",
) -> GateCreateResult:
    """`bd gate create --blocks <blocks> --type <gate_type> [...] --json`. `gate_type` is one
    of `human` (default) / `timer` / `gh:run` / `gh:pr`; `timeout` (e.g. `"2h"`) is required by
    `timer`, `await_id` by `gh:run`/`gh:pr`. Returns the new gate's id on success."""
    args = ["gate", "create", "--blocks", blocks, "--type", gate_type]
    if reason:
        args += ["--reason", reason]
    if title:
        args += ["--title", title]
    if await_id:
        args += ["--await-id", await_id]
    if timeout:
        args += ["--timeout", timeout]
    res = _run_json(args, cwd, actor)
    data = _parse_json_tail(res.stdout)
    if res.returncode != 0 or not isinstance(data, dict) or not data.get("id"):
        return GateCreateResult(ok=False, error=_err(res))
    return GateCreateResult(ok=True, gate_id=str(data["id"]))


def gate_check(
    cwd,
    *,
    gate_type: str | None = None,
    dry_run: bool = False,
    escalate: bool = False,
    limit: int | None = None,
    actor: str = "",
) -> GateCheckResult:
    """`bd gate check [--type T] [--dry-run] [--escalate] [--limit N] --json` — evaluate open
    gates and close the ones bd can resolve on its own (timer/gh:run/gh:pr/bead). Human gates
    are never touched by this call; resolving one is always `bd gate resolve` (see
    `gate_resolve`), driven by a human or `bh work approve`."""
    args = ["gate", "check"]
    if gate_type:
        args += ["--type", gate_type]
    if dry_run:
        args += ["--dry-run"]
    if escalate:
        args += ["--escalate"]
    if limit is not None:
        args += ["--limit", str(limit)]
    res = _run_json(args, cwd, actor)
    data = _parse_json_tail(res.stdout)
    if res.returncode != 0 or not isinstance(data, dict):
        return GateCheckResult(ok=False, error=_err(res))
    return GateCheckResult(
        ok=True,
        checked=int(data.get("checked") or 0),
        resolved=int(data.get("resolved") or 0),
        escalated=int(data.get("escalated") or 0),
        errors=int(data.get("errors") or 0),
    )


def gate_resolve(cwd, gate_id: str, *, reason: str = "", actor: str = "") -> GateResolveResult:
    """`bd gate resolve <gate_id> [--reason R]` — manually close a (typically `human`) gate.
    Idempotent per bd's own behavior (see `GateResolveResult`): calling this again on an
    already-closed gate is not an error."""
    args = ["gate", "resolve", gate_id]
    if reason:
        args += ["--reason", reason]
    res = _run_json(args, cwd, actor)
    if res.returncode != 0:
        return GateResolveResult(ok=False, error=_err(res))
    return GateResolveResult(ok=True)


# ---- merge-slot -----------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotStatus:
    """`bd merge-slot check --json` parsed: `{"available": bool, "holder": str|None,
    "waiters": list[str]|None, "id": str}`. A slot with no `create` yet exits non-zero
    ("not found") — reported here as `ok=False`, `exists=False`, never as a false `held`."""

    ok: bool
    exists: bool = True
    held: bool = False
    holder: str = ""
    waiters: tuple[str, ...] = field(default_factory=tuple)
    error: str = ""


@dataclass(frozen=True)
class SlotAcquireResult:
    """`bd merge-slot acquire --holder H [--wait] --json`. Verified shapes:
    held (won) -> `{"acquired": true, "holder": H, "id": ...}`, exit 0;
    held (lost, no --wait) -> `{"acquired": false, "holder": <other>, "id": ...}`, exit 1;
    held (lost, --wait) -> adds `"waiting": true, "position": N` (1-based queue position),
    exit 1. `acquired=False` + `waiting=False` means the caller was refused outright (no
    `--wait`); `acquired=False` + `waiting=True` means queued as waiter `position`."""

    acquired: bool
    holder: str = ""
    waiting: bool = False
    position: int | None = None
    error: str = ""


@dataclass(frozen=True)
class SlotReleaseResult:
    """`bd merge-slot release [--holder H] --json`. Verified: releasing with a `--holder` that
    does not match the current holder is REFUSED (`{"error": "slot held by X, not H"}`, exit
    1) — bd itself is the guard against a non-holder (e.g. an orphan cleanup that raced a live
    holder) releasing someone else's slot."""

    ok: bool
    error: str = ""


def merge_slot_create(cwd, *, actor: str = "") -> bool:
    """`bd merge-slot create --json` — idempotent: a second call against an existing slot bead
    is a no-op success, so callers never need to probe first."""
    res = _run_json(["merge-slot", "create"], cwd, actor)
    return res.returncode == 0


def merge_slot_check(cwd, *, actor: str = "") -> SlotStatus:
    """`bd merge-slot check --json`."""
    res = _run_json(["merge-slot", "check"], cwd, actor)
    data = _parse_json_tail(res.stdout)
    if res.returncode != 0 or not isinstance(data, dict):
        # bd reports "not found" (no slot bead yet) as a non-zero exit with a plain message,
        # not JSON — never surface that as a false "held".
        return SlotStatus(ok=False, exists=False, error=_err(res))
    waiters = data.get("waiters") or []
    return SlotStatus(
        ok=True,
        exists=True,
        held=not bool(data.get("available")),
        holder=str(data.get("holder") or ""),
        waiters=tuple(str(w) for w in waiters),
    )


def merge_slot_acquire(
    cwd, holder: str, *, wait: bool = False, actor: str = ""
) -> SlotAcquireResult:
    """`bd merge-slot acquire --holder <holder> [--wait] --json`."""
    args = ["merge-slot", "acquire", "--holder", holder]
    if wait:
        args += ["--wait"]
    res = _run_json(args, cwd, actor)
    data = _parse_json_tail(res.stdout)
    if not isinstance(data, dict):
        return SlotAcquireResult(acquired=False, error=_err(res))
    return SlotAcquireResult(
        acquired=bool(data.get("acquired")),
        holder=str(data.get("holder") or ""),
        waiting=bool(data.get("waiting")),
        position=data.get("position"),
        error="" if res.returncode == 0 else str(data.get("error") or _err(res)),
    )


def merge_slot_release(cwd, *, holder: str | None = None, actor: str = "") -> SlotReleaseResult:
    """`bd merge-slot release [--holder H] --json`. Passing `holder` asks bd to verify it
    matches the current holder before releasing (see `SlotReleaseResult`); omitting it releases
    unconditionally."""
    args = ["merge-slot", "release"]
    if holder:
        args += ["--holder", holder]
    res = _run_json(args, cwd, actor)
    if res.returncode != 0:
        data = _parse_json_tail(res.stdout)
        err = str(data.get("error")) if isinstance(data, dict) and data.get("error") else _err(res)
        return SlotReleaseResult(ok=False, error=err)
    return SlotReleaseResult(ok=True)


# ---- heartbeat --------------------------------------------------------------------------------


@dataclass(frozen=True)
class HeartbeatResult:
    """`bd heartbeat <id> --json`. Verified success: `{"id":..., "owner":..., "status":
    "heartbeat"}`, exit 0. Verified failure (not the current holder, already reclaimed, or
    closed): `{"error": "..."}`, exit 1 — bd itself refuses a heartbeat from anyone but the
    live owner, which is exactly the signal a worker needs to learn its claim is gone and stop."""

    ok: bool
    error: str = ""


def heartbeat(cwd, bead_id: str, *, actor: str = "") -> HeartbeatResult:
    """Refresh the lease on `bead_id`, held `in_progress` by `actor` (or the resolved default
    actor). Nothing releases a claim on its own — a worker that stops calling this is exactly
    what makes its lease go stale and eligible for `reclaim`."""
    res = _run_json(["heartbeat", bead_id], cwd, actor)
    if res.returncode != 0:
        return HeartbeatResult(ok=False, error=_err(res))
    return HeartbeatResult(ok=True)


# ---- reclaim ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReclaimedIssue:
    id: str
    previous_owner: str = ""


@dataclass(frozen=True)
class ReclaimResult:
    """`bd reclaim [...] --json`. Verified shape: `{"count": int, "reclaimed":
    [{"id":..., "previous_owner":...}, ...] | null, "scoped": bool, "schema_version": int}`.
    Only `in_progress` issues whose lease expired more than `older_than` ago (default 10m grace
    on top of the 5-minute lease TTL) are reverted; a heartbeated (live) lease is left
    untouched — this is the ONLY recovery path for a dead holder's claim, nothing releases it
    on its own."""

    ok: bool
    count: int = 0
    reclaimed: tuple[ReclaimedIssue, ...] = field(default_factory=tuple)
    error: str = ""

    @property
    def reclaimed_ids(self) -> tuple[str, ...]:
        return tuple(r.id for r in self.reclaimed)


def reclaim(
    cwd,
    *,
    older_than: str | None = None,
    label: list[str] | None = None,
    label_any: list[str] | None = None,
    exclude_label: list[str] | None = None,
    assignee: list[str] | None = None,
    ids: list[str] | None = None,
    any_replica: bool = False,
    actor: str = "",
) -> ReclaimResult:
    """`bd reclaim [--older-than D] [--label L]... [--label-any L]... [--exclude-label L]...
    [--assignee A]... [--id ID]... [--any-replica] --json`. Filters AND-combine and never widen
    the set bd would otherwise consider — a reclaimed lease must still be stale. Scoped to
    leases THIS replica granted unless `any_replica` is set (see `bd reclaim --help`'s
    "Replicas and leases" section)."""
    args = ["reclaim"]
    if older_than is not None:
        args += ["--older-than", older_than]
    for lbl in label or ():
        args += ["--label", lbl]
    for lbl in label_any or ():
        args += ["--label-any", lbl]
    for lbl in exclude_label or ():
        args += ["--exclude-label", lbl]
    for who in assignee or ():
        args += ["--assignee", who]
    for issue_id in ids or ():
        args += ["--id", issue_id]
    if any_replica:
        args += ["--any-replica"]
    res = _run_json(args, cwd, actor)
    data = _parse_json_tail(res.stdout)
    if res.returncode != 0 or not isinstance(data, dict):
        return ReclaimResult(ok=False, error=_err(res))
    reclaimed = tuple(
        ReclaimedIssue(id=str(r.get("id") or ""), previous_owner=str(r.get("previous_owner") or ""))
        for r in (data.get("reclaimed") or [])
        if isinstance(r, dict)
    )
    return ReclaimResult(ok=True, count=int(data.get("count") or 0), reclaimed=reclaimed)
