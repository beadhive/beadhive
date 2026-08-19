"""The TWO shapes a cross-hive dataset is read with — and the rule for choosing between them.

Before this module every cross-hive read invented its own shape: five hand-rolled
``ThreadPoolExecutor`` blocks with three different worker bounds (bh-1qxjn). The problem was
never any one of them; it was that a SIXTH dataset had nothing to be added to, so it invented a
seventh shape. There are two shapes here and adding a dataset means picking one.

## Shape A — bulk cross-hive read (:func:`sql` / :func:`sql_rows`)

ONE query against the shared Dolt server, reading every hive's database by qualified name. Its
cost does not scale with fleet size. Sound ONLY when the value is something the server itself
holds:

* a stored column (``issues.status``, ``schema_migrations`` version, a config row), or
* a value bd exposes as a server-side VIEW (``ready_issues`` / ``blocked_issues``).

NOT sound when the value is derived in bd's own Go code with no server-side equivalent. bd is
not a thin wrapper over its tables — reimplementing a derivation as hand-written SQL produces a
number that looks right, and then DRIFTS every time bd's resolver changes upstream with nothing
to catch it. When in doubt the answer is shape B, which asks bd.

## Shape H — a HOST-GLOBAL fact (:func:`once`, or better, hoisting)

Some facts do not vary by hive at all: the local ``bd`` binary's version, the user's global git
config, this host's node id. Reading one of those inside a per-hive loop asks the same question
N times and is guaranteed to get the same answer.

**The cheapest correct code for a host-global fact has no cache in it.** Resolve the value ONCE
before the fan-out and pass it in — nothing to cache, nothing to lock, no invalidation contract.
``dolt_health.bulk_schema_versions`` -> ``hive_schema.refresh_with_detail(probed=…)`` is the
worked example of threading a pre-resolved value into a pooled callable.

:func:`once` is the fallback for when the value is consumed several call levels deep inside the
pooled callable and threading it would mean changing signatures that exist for other reasons.

WHY THIS SHAPE HAS TO BE NAMED, and not left as "obviously don't do that": before it existed
this module documented A and B, both per-hive. An author with a host-global fact was told to
pick one of two wrong answers, and ``bd --version`` was consequently forked 15 times per
``bh doctor`` run (bh-gy7bc). A taxonomy that omits a category does not stay neutral about it.

## Shape B — bounded per-hive fan-out (:func:`fanout`)

N independent calls, one per hive, run concurrently under a worker cap. Its cost still scales
with fleet size, divided by the cap. The right shape whenever A is unsound, and the ONLY shape
for anything that is not a bead-store read at all: hitch preflights, MCP round-trips, git and
filesystem probes, and every embedded-mode hive (which has no shared-server database to qualify).

## Choosing, for a new dataset

1. **Does this fact vary by hive at all?** If not, it is shape H — hoist it out of the loop.
   Ask this FIRST: it is the question whose omission caused bh-gy7bc, and both of the other
   answers are wrong for a host-global value.
2. Read bd's own implementation of the value and write down what it MEANS. This is the step that
   decides between A and B, and it is the step that gets skipped.
3. Stored column or server-side view -> shape A.
4. Derived in bd's code, or not a bead-store read at all -> shape B. Say so in the bead.
5. Never hand-roll a fourth shape. If none fits, that is a finding worth a bead, not a local
   ``ThreadPoolExecutor`` and not a local cache.

## A trap this module's own existence created: ``functools.cache`` is not pool-safe

``functools.cache`` / ``lru_cache`` have NO stampede protection. When N pool workers call a
memoized function before any of them returns, all N miss and all N execute. Measured:

    @cache under a 15-way pool: underlying function ran 15 times (want 1)
    @cache called sequentially : underlying function ran  1 times

This is how a memo added against a SEQUENTIAL loop (bh-i6e5g took ``bd --version`` from 12
spawns to 1) was silently defeated when that loop became concurrent (bh-ti7ws) — with the memo's
docstring still claiming it worked. Making fan-out the house pattern is what armed the trap, so
the guard belongs here: use :func:`once`, not ``functools.cache``, for anything a pool can reach.

## What this module deliberately does NOT do: bound the child process

:func:`fanout` owns concurrency and ORDER. It does not bound the subprocesses its workers spawn,
because it cannot: it is handed an arbitrary callable, and stopping WAITING on a future
(``future.result(timeout=…)``) does not reap the process behind it — it would report a timeout
while the real child ran on, which is worse than not claiming to bound at all.

Per-call bounding belongs at the subprocess seam, :func:`beadhive.run.bounded`, which has the
timeout, the process group and the reaper. The seam a fanned-out call reaches it through is the
``fn`` passed here — bound the CALL, not the pool.

PDEATHSIG is explicitly out of scope for this module (bh-1qxjn's scope line). ``run.bounded``
arms it via ``preexec_fn``, which CPython documents as unsafe in a multi-threaded process — and
a thread pool is exactly what makes bh multi-threaded. Routing pooled calls through
``run.bounded`` would make that hazard reachable. Resolving it is bh-0tjqd's call, not this
module's; until it lands, a pooled ``fn`` should use a plain timeout, not the PDEATHSIG path.
"""

from __future__ import annotations

import json as _json
import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

from .run import run

T = TypeVar("T")
R = TypeVar("R")

# The cap every fanned-out read shares. Per-hive reads spawn store-bound subprocesses (`bd`,
# `dolt`, `git`), so one-thread-per-hive is not "more parallel", it is an unbounded process
# count at fleet scale. 16 is what the doctor sections measured well at; the I/O-heavy network
# passes pass a smaller cap of their own.
MAX_WORKERS = 16


def once(fn: Callable[[], R]) -> Callable[[], R]:
    """Shape H fallback: run ``fn`` at most once per process, even under a pool.

    ``functools.cache`` cannot be used for this — see the trap section above; N concurrent
    callers all miss and all execute. A double-checked lock makes the first caller run ``fn``
    while the rest block on the lock and then read the stored result.

    ZERO-ARGUMENT ON PURPOSE. A keyed cache invites "cache this per hive", which is shape B
    wearing a cache, and a keyed cache under a pool needs a lock PER KEY to avoid re-introducing
    the stampede one level down. If a value varies by anything, it is not shape H.

    A raised exception is NOT stored: the next caller retries. Caching a failure would turn one
    transient probe failure into a permanently wrong answer for the life of the process, which
    is the failure mode this module's read path can least afford.

    Prefer hoisting where the value can be threaded in as an argument — see shape H above. This
    exists for the case where it cannot.
    """
    lock = threading.Lock()
    box: list[R] = []

    def wrapper() -> R:
        if box:
            return box[0]
        with lock:
            if not box:  # re-check: another thread may have filled it while we waited
                box.append(fn())
        return box[0]

    wrapper.cache_clear = box.clear  # type: ignore[attr-defined]
    return wrapper


def fanout(
    fn: Callable[[T], R], items: Sequence[T] | Iterable[T], *, workers: int = MAX_WORKERS
) -> list[R]:
    """Run ``fn`` over ``items`` concurrently; return results in INPUT ORDER.

    Order is part of the contract, not an accident of ``Executor.map``: every caller renders a
    per-hive report, and a report whose rows reshuffle run to run is unreadable and undiffable.

    The pool is capped at ``min(len(items), workers)`` — never one thread per item. An empty
    ``items`` returns ``[]`` without constructing a pool (``ThreadPoolExecutor(max_workers=0)``
    raises).

    Exceptions propagate from the first failing item, as ``Executor.map`` does. A caller that
    wants one hive's failure to be that hive's report rather than the whole pass's must catch
    inside ``fn`` — which is what every current caller does.
    """
    materialized = list(items)
    if not materialized:
        return []
    with ThreadPoolExecutor(max_workers=min(len(materialized), workers)) as pool:
        return list(pool.map(fn, materialized))


# ---- shape A: the bulk cross-hive transport ---------------------------------
# `bd -C <store> sql -q <query> --json` executes against the SHARED SERVER every server-mode
# hive's tables live on, including another database by qualified name (`<other_db>.<table>`).
# So one connection reads the whole fleet and no MySQL driver is added — established and
# measured by hub_bulk (bh-z4z52, ~398x on the copy path); this is that transport promoted out
# of hub_bulk so the READ path can use it too (bh-0gvs3).

SQL_TIMEOUT = 60.0  # seconds per `bd sql` call — a local loopback query, generously bounded


def sql(store: Path, query: str, *, timeout: float = SQL_TIMEOUT):
    """``bd -C <store> sql -q <query> --json``. Never raises; the caller reads ``returncode``."""
    return run(
        ["bd", "-C", str(store), "sql", "-q", query, "--json"],
        check=False,
        capture=True,
        timeout=timeout,
    )


def sql_rows(res):
    """Parsed JSON body of a :func:`sql` result, or ``None`` on a failed call or unparseable
    output — never raises, matching ``bd.json``'s own None-on-failure contract. ``None`` is the
    signal to FALL BACK to shape B, never to report an empty fleet."""
    if res.returncode != 0:
        return None
    try:
        return _json.loads(res.stdout or "null")
    except ValueError:
        return None
