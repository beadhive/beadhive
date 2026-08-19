"""The TWO shapes a cross-hive dataset is read with — and the rule for choosing between them.

Before this module every cross-hive read invented its own shape: five hand-rolled
``ThreadPoolExecutor`` blocks with three different worker bounds (bh-1qxjn). The problem was
never any one of them; it was that a SIXTH dataset had nothing to be added to, so it invented a
seventh shape. There are two shapes here and adding a dataset means picking one.

## Shape A — bulk cross-hive read (:mod:`beadhive.fleet_sql`)

ONE query against the shared Dolt server, reading every hive's database by qualified name. Its
cost does not scale with fleet size. Sound ONLY when the value is something the server itself
holds:

* a stored column (``issues.status``, ``schema_migrations`` version, a config row), or
* a value bd exposes as a server-side VIEW (``ready_issues`` / ``blocked_issues``).

NOT sound when the value is derived in bd's own Go code with no server-side equivalent. bd is
not a thin wrapper over its tables — reimplementing a derivation as hand-written SQL produces a
number that looks right, and then DRIFTS every time bd's resolver changes upstream with nothing
to catch it. When in doubt the answer is shape B, which asks bd.

## Shape B — bounded per-hive fan-out (:func:`fanout`)

N independent calls, one per hive, run concurrently under a worker cap. Its cost still scales
with fleet size, divided by the cap. The right shape whenever A is unsound, and the ONLY shape
for anything that is not a bead-store read at all: hitch preflights, MCP round-trips, git and
filesystem probes, and every embedded-mode hive (which has no shared-server database to qualify).

## Choosing, for a new dataset

1. Read bd's own implementation of the value and write down what it MEANS. Do this first; it is
   the step that decides the rest, and it is the step that gets skipped.
2. Stored column or server-side view -> shape A.
3. Derived in bd's code, or not a bead-store read at all -> shape B. Say so in the bead.
4. Never hand-roll a third shape. If neither fits, that is a finding worth a bead, not a local
   ``ThreadPoolExecutor``.

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

from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")

# The cap every fanned-out read shares. Per-hive reads spawn store-bound subprocesses (`bd`,
# `dolt`, `git`), so one-thread-per-hive is not "more parallel", it is an unbounded process
# count at fleet scale. 16 is what the doctor sections measured well at; the I/O-heavy network
# passes pass a smaller cap of their own.
MAX_WORKERS = 16


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
