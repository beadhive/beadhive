"""Validation verdict ledger (bh-dfx0): skip redundant clean-checkout validations.

Records the outcome of every clean-checkout validation keyed by **(commit sha,
validate-cmd hash)** in a small untracked JSON file inside the hive's git dir
(``<hive>/.git/bh-validation-ledger.json`` — repo-local state, never a tracked file,
dies with the clone). Opt-in callers (``work submit``) reuse a recorded GREEN verdict
for the exact key and skip the throwaway checkout entirely; a red verdict is recorded
but never reused, so a failure is always re-validated.

Trust: the ledger is a **local optimization for trusted-local seats** — anything that
can write the file can fake a green — so landing-boundary validations (merge /
postland / finish / batch land) NEVER consult it: the gate at landing stays fresh.
Reviewer-facing runs (``work review --run``) default to fresh too and only reuse via
an explicit ``--no-fresh``.

Staleness: entries carry a timestamp and expire after :data:`LEDGER_TTL_SECONDS`;
the cmd hash in the key covers command drift. Toolchain/env drift beyond the command
string is an accepted residual, bounded by the TTL.

In-flight marker: deliberately NOT implemented. Per-invocation verify dirs (bh-nikb)
already make concurrent duplicate validations *safe* — the ledger only removes
duplicate *cost* — and a wait/skip protocol would add cross-process locking for a
marginal saving. Revisit if `bh.work.validation` telemetry shows overlap matters.

All writes are best-effort (atomic tmp+rename, exceptions swallowed): a broken ledger
must never fail or skew the validation it records — callers just fall back to fresh runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from pathlib import Path

from . import registry

LEDGER_FILENAME = "bh-validation-ledger.json"
LEDGER_TTL_SECONDS = 24 * 60 * 60  # a verdict older than this is stale — revalidate
_MAX_ENTRIES = 200  # hard cap so the ledger never grows unbounded


def cmd_hash(cmd: str) -> str:
    """Short stable hash of the validation command string — the env-drift half of the key."""
    return hashlib.sha256(cmd.encode()).hexdigest()[:16]


def _ledger_path(entry) -> Path | None:
    """The hive-local ledger file, or None when there is no plain `.git` dir to keep it in
    (linked worktree / missing clone) — callers then simply fall back to fresh runs."""
    git_dir = registry.hive_dir(entry) / ".git"
    return git_dir / LEDGER_FILENAME if git_dir.is_dir() else None


def _load(path: Path) -> list[dict]:
    """The ledger's entry list; [] on any read/shape problem (corrupt file == empty ledger)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _is_fresh(e: dict, now: float, ttl: int) -> bool:
    try:
        return now - float(e["at"]) <= ttl
    except (KeyError, TypeError, ValueError):
        return False


def record(entry, sha: str, cmd: str, rc: int) -> None:
    """Record a validation verdict for (sha, cmd). Best-effort: never raises, never fails
    the validation it records. Prunes expired entries and replaces a same-key entry."""
    path = _ledger_path(entry)
    if path is None or not sha:
        return
    now = time.time()
    key = cmd_hash(cmd)
    kept = [
        e
        for e in _load(path)
        if _is_fresh(e, now, LEDGER_TTL_SECONDS)
        and not (e.get("sha") == sha and e.get("cmd_hash") == key)
    ]
    new = {"sha": sha, "cmd_hash": key, "rc": int(rc), "at": now, "host": socket.gethostname()}
    entries = (kept + [new])[-_MAX_ENTRIES:]
    try:
        tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
        tmp.write_text(json.dumps(entries) + "\n")
        os.replace(tmp, path)  # atomic: a concurrent reader never sees a torn file
    except OSError:
        pass


def green_verdict(entry, sha: str, cmd: str, ttl: int = LEDGER_TTL_SECONDS) -> dict | None:
    """The recorded entry for exactly (sha, cmd) iff it is GREEN (rc == 0) and fresh (within
    `ttl`), else None. A red / stale / missing verdict always means: run the validation."""
    path = _ledger_path(entry)
    if path is None or not sha:
        return None
    now = time.time()
    key = cmd_hash(cmd)
    hit = next(
        (e for e in reversed(_load(path)) if e.get("sha") == sha and e.get("cmd_hash") == key),
        None,
    )
    if hit is None or not _is_fresh(hit, now, ttl) or hit.get("rc") != 0:
        return None
    return hit
