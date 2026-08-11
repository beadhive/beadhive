"""The aggregate per-hive dispatch log sink (bh-e7r9q.5), and the concurrent-writer contract
that keeps it parseable.

OPERATOR DECISION 2026-08-10 — HIVE-SCOPED, AGGREGATE LOGS. One JSONL sink per hive receives
the structured event stream (`seat_spawned` / `seat_harvested` / `seat_cancelled` /
`dispatch_cause_recorded` / `dispatch_pass`, all from :mod:`beadhive.localloop` via
:mod:`beadhive.log`) from EVERY dispatcher loop that hive runs: the hive-level picker itself
(:mod:`beadhive.dispatch_hive_run`) and every `bh work loop <epic>` child it spawns. `bh host
dispatch logs [--hive H]` reads that one file — never `journalctl` / `log show` / `docker
logs`, which is the entire point of bh-e7r9q.4's backend seam: no platform divergence enters
the codebase.

Per-bead / per-epic / per-session filtering (`logs --bead <id>`) is a later PROJECTION over
this same JSONL and is deliberately NOT built here — see the format guarantee below, which is
what makes that safe to add without a migration.

CONCURRENT WRITERS — THE THING GOTTEN RIGHT NOW BECAUSE IT IS EXPENSIVE LATER. Several
processes (the picker + N `bh work loop` children) append to ONE file at once. The strategy
chosen is **O_APPEND with writes bounded under PIPE_BUF, not a single serializing writer**:

  * every writer opens the sink through `beadhive.log.add_file_sink`, whose `FileHandler` is
    opened with `mode="a"` — i.e. `O_APPEND` — so every write() seeks-to-end-and-writes as one
    kernel operation, never a stale-offset overwrite;
  * `logging.StreamHandler.emit` (stdlib, since bpo-35046) formats the WHOLE record (message +
    newline) and calls `stream.write()` exactly ONCE per record — never two separate `write()`
    calls that another writer's line could land between;
  * a single `write()` under `O_APPEND` on a local filesystem is atomic w.r.t. other writers as
    long as it does not exceed the platform's atomic-write bound (`PIPE_BUF`, 4096 bytes on
    Linux) — the same guarantee `tail -F`-safe multi-process logging has relied on for decades.
    A record is not truncated to fit; `_MAX_LOGGED_FIELD` below bounds the fields most likely to
    grow unboundedly (stdout/stderr tails, long reasons) so a normal record stays well under
    the bound. `tests/test_dispatch_log.py::test_concurrent_writers_produce_parseable_lines`
    proves this empirically: N real OS processes hammering the same sink, every resulting line
    still `json.loads`-parses.

    A single-writer-owned-by-the-supervisor design was considered and rejected: it would mean
    every `bh work loop` child piping its structured events back to the picker over some
    channel instead of writing beads-adjacent state directly to disk, which is exactly the
    kind of runtime-only coordination state the loop-ownership ADR's invariant argues against
    adding. O_APPEND gets the same safety property for free from the filesystem.

ONE JSON OBJECT PER LINE, NO INTERLEAVED FRAMING, NO PER-LOOP HEADERS a reader must skip — so
nothing here forecloses a later `--bead`/`--epic`/`--session` filter; every record already
carries the keys such a filter would read (see `beadhive.localloop`'s module docstring for the
per-event-type key list).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config, registry

#: Fields long enough to risk pushing a single JSONL record over the atomic-write bound.
#: Truncated defensively at the point of writing so ordinary structured events never approach
#: PIPE_BUF; NOT a correctness requirement (O_APPEND is atomic regardless of size on local
#: filesystems for a single write() call) but keeps every record comfortably inside the bound
#: this module documents and tests against.
_MAX_FIELD_CHARS = 2000


def truncate_field(value: str, *, limit: int = _MAX_FIELD_CHARS) -> str:
    """Bound one string field defensively before it is logged. See module docstring."""
    if len(value) <= limit:
        return value
    return value[:limit] + f"...<truncated {len(value) - limit} chars>"


def sink_dir() -> Path:
    """Where every hive's aggregate sink lives: `<beadhive home>/dispatch/`."""
    return config.home() / "dispatch"


def sink_path_for_slug(hive_slug: str) -> Path:
    """The aggregate JSONL sink for one hive, by its already-sanitized slug."""
    return sink_dir() / f"{hive_slug}.jsonl"


def hive_slug(entry: dict) -> str:
    """The sanitized hive slug used for the sink filename, the systemd instance name, and every
    other per-hive-on-this-host identifier — one function, so they can never drift apart."""
    return registry.sanitize(registry.hive_key(entry))


def sink_path(cfg: dict, entry: dict) -> Path:
    """The aggregate sink path for a resolved hive entry."""
    return sink_path_for_slug(hive_slug(entry))


def ensure_sink_dir() -> Path:
    d = sink_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def tail_records(path: Path, *, lines: int = 200) -> list[dict]:
    """The last *lines* JSONL records from *path*, oldest first. A line that fails to parse is
    skipped rather than raising — a reader tailing a file another process is actively writing
    to can observe a torn read on process crash mid-write (the one case O_APPEND's guarantee
    does not cover: the file being truncated or corrupted by something OTHER than a well-formed
    writer), and `logs` should degrade to "skip the bad line", never crash the read."""
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        raw_lines = fh.readlines()
    for line in raw_lines[-lines:] if lines > 0 else raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out
