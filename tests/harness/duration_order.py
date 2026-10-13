"""Longest-processing-time-first test ordering from a checked-in durations file.

Late-scheduled 300-400 s tests leave one xdist worker running while the rest idle.  Ordering the
collection by recorded duration (descending) lets ``--dist worksteal`` hand the long tests out
first and fill the tail with short ones.  The durations file is regenerated from validation JUnit
reports by ``scripts/refresh_test_durations.py`` (``just refresh-test-durations``).  A test the
file does not list takes its module's median from the ``modules`` table, then the global median.

Opt out with ``BH_TEST_DURATION_ORDER=0``.  ``BH_TEST_DURATIONS_FILE`` points at another file.
Ordering is a pure sort: stable, deterministic, and it never adds, drops, or deselects an item.

WORKSTEAL PACKING (bh-xqtp8, opt-in with ``BH_TEST_WORKSTEAL_PACK=1``). A plain longest-first sort
is defeated by ``--dist worksteal``: its first ``schedule()`` hands worker *i* the *i*-th
CONTIGUOUS chunk of ``len(pending) // (workers - i)`` items, so one worker receives the ~17
longest tests and the rest get progressively shorter ones. Stealing then moves the tail half of a
queue by COUNT, not duration, and the long tests it moves start late on another worker. Measured
on the integration lane (16 workers, 12 Dolt slots): ~100 s of idle tail per worker, and a
simulation of the real scheduler over the measured durations reproduces the 580 s wall.
:func:`pack` instead lays the selection out as those exact chunks, each one a
longest-processing-time bin, so every worker's initial queue carries about the same work. Within a
chunk tests run longest first, so a steal (which takes a queue's tail) moves only short ones.

The packing is also Dolt-slot aware. With ``slots`` < ``workers`` (``BH_DOLT_SLOTS=12`` on 16), at
most ``slots`` workers can run a slot-holding test at once, so slot work is binned first, onto
``slots`` chunks only; otherwise the slot-free workers would also queue for a slot and the
slot-holding chunks would finish unevenly. Slot-free work is then binned onto whichever chunk is
least loaded, which fills the slot-free chunks first. It must not be confined to the
``workers - slots`` slot-free chunks: the stateful lane holds almost no slots (bh-ey9wj), and
confining it would hand those four chunks the ~2,700 longest of its ~10,700 tests.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

ENABLE_ENV = "BH_TEST_DURATION_ORDER"
PACK_ENV = "BH_TEST_WORKSTEAL_PACK"
#: A test requesting one of these holds a run-wide Dolt slot for its whole duration
#: (``stateful_fixtures._rewound``), exactly like a ``dolt_server``-marked test. Measured: this
#: and the marker classify all 202 slot-holding integration tests with no false positive.
SLOT_FIXTURES = frozenset({"rewound_cluster", "rewound_world", "rewound_custom"})
FILE_ENV = "BH_TEST_DURATIONS_FILE"
DEFAULT_FILE = Path(__file__).with_name("test_durations.json")
_OFF = frozenset({"0", "false", "no", "off"})


def enabled(env: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if env is None else env
    return environ.get(ENABLE_ENV, "1").strip().lower() not in _OFF


def durations_path(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    override = environ.get(FILE_ENV)
    return Path(override) if override else DEFAULT_FILE


def junit_key(nodeid: str) -> str:
    """Map a pytest nodeid onto the ``classname::name`` form JUnit reports use."""
    path, _, rest = nodeid.partition("::")
    module = path.removesuffix(".py").replace("/", ".")
    # A parametrize id may itself contain "::" (``[[::1]:80]``); only the part before "[" is a path.
    head, bracket, params = rest.partition("[")
    *classes, name = head.split("::") if head else [""]
    name += bracket + params
    return f"{'.'.join([module, *classes])}::{name}"


def load(path: Path) -> tuple[dict[str, float], float]:
    """Return ``(durations, fallback)``; a missing or malformed file yields no ordering signal."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tests = {str(key): float(value) for key, value in data["tests"].items()}
        return tests, float(data.get("median", 0.0))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}, 0.0


def module_of_key(key: str) -> str:
    """The module part of a ``classname::name`` key: the leading segments before a class name.

    Module and package directory names are lowercase here; test classes are capitalised, so the
    module ends where the first capitalised segment of the classname starts.
    """
    segments: list[str] = []
    for segment in key.partition("::")[0].split("."):
        if segment[:1].isupper():
            break
        segments.append(segment)
    return ".".join(segments)


def load_modules(path: Path) -> dict[str, float]:
    """Per-module median seconds (the ``modules`` table); empty when absent or malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(key): float(value) for key, value in data["modules"].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def predicted_seconds(
    nodeid: str,
    durations: Mapping[str, float],
    fallback: float,
    modules: Mapping[str, float] | None = None,
) -> float:
    """Recorded seconds, else the test's module median, else the global *fallback*."""
    key = junit_key(nodeid)
    known = durations.get(key)
    if known is not None:
        return known
    return (modules or {}).get(module_of_key(key), fallback)


def order_items(
    items: Sequence,
    durations: Mapping[str, float],
    fallback: float,
    modules: Mapping[str, float] | None = None,
) -> list:
    """Stable descending sort; unknown tests take their module median, else ``fallback``."""
    if not durations:
        return list(items)
    return sorted(
        items, key=lambda item: -predicted_seconds(item.nodeid, durations, fallback, modules)
    )


def apply(items: Sequence, env: Mapping[str, str] | None = None) -> list:
    if not enabled(env):
        return list(items)
    path = durations_path(env)
    durations, fallback = load(path)
    return order_items(items, durations, fallback, load_modules(path))


def pack_enabled(env: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if env is None else env
    return environ.get(PACK_ENV, "0").strip().lower() not in _OFF | {""}


def holds_dolt_slot(item) -> bool:
    """True when *item* will hold a ``harness.world.dolt_server_slot`` for its whole run."""
    if item.get_closest_marker("dolt_server") is not None:
        return True
    return not SLOT_FIXTURES.isdisjoint(getattr(item, "fixturenames", ()))


def chunk_sizes(count: int, workers: int) -> list[int]:
    """The initial chunk sizes ``--dist worksteal`` hands out when every worker is idle."""
    sizes, left = [], count
    for index in range(workers):
        size = left // (workers - index)
        sizes.append(size)
        left -= size
    return sizes


def pack(
    items: Sequence,
    durations: Mapping[str, float],
    fallback: float,
    workers: int,
    slots: int,
    holds: Callable[[object], bool],
    modules: Mapping[str, float] | None = None,
) -> list:
    """Lay *items* out as ``workers`` worksteal chunks of balanced predicted work.

    Longest first, each test goes to the least-loaded chunk that still has room. With the split,
    slot-holding tests are placed first and only onto the first ``slots`` chunks (falling back to
    any chunk with room); slot-free tests follow onto any chunk, least loaded first. ``slots <= 0``
    (unbounded) or ``slots >= workers`` disables the split, and so does a selection with no
    slot-holding test. Stable and deterministic, so every xdist worker computes the same
    collection order.
    """
    if workers <= 1 or not items:
        return list(items)
    sizes = chunk_sizes(len(items), workers)
    weight = {
        id(item): predicted_seconds(item.nodeid, durations, fallback, modules) for item in items
    }
    ordered = sorted(items, key=lambda i: -weight[id(i)])
    everywhere = range(workers)
    phases: list[tuple[list, range]] = [(ordered, everywhere)]
    if 0 < slots < workers:
        holding = [item for item in ordered if holds(item)]
        if holding:
            free = [item for item in ordered if not holds(item)]
            phases = [(holding, range(slots)), (free, everywhere)]
    bins: list[list] = [[] for _ in range(workers)]
    loads = [0.0] * workers
    for group, preferred in phases:
        for item in group:
            room = [b for b in everywhere if len(bins[b]) < sizes[b]]
            target = min([b for b in room if b in preferred] or room, key=lambda b: (loads[b], b))
            bins[target].append(item)
            loads[target] += weight[id(item)]
    return [item for chunk in bins for item in sorted(chunk, key=lambda i: -weight[id(i)])]


def apply_pack(items: Sequence, workers: int, slots: int, env: Mapping[str, str] | None = None):
    path = durations_path(env)
    durations, fallback = load(path)
    return pack(items, durations, fallback, workers, slots, holds_dolt_slot, load_modules(path))
