"""Measured per-phase timings of one intake batch command (``bh-17g6b``).

``bh work labels sync``, ``labels approve --batch``, ``outbox send-approved`` and ``outbox send``
add a ``timings`` block to their ``--json`` output, so every real factory batch measures where it
spent its time instead of leaving it to a model. The block is the command's own output; nothing
here is stored in an outbox record or receipt, so no wire shape changes.

A :class:`Timings` is a small collector the command creates and installs for its own duration
with :func:`recording`. The instrumented seams (the intake lock, the report store's I/O, the
tracker adapter's requests) look it up with :func:`current` and do nothing when there is none, so
a command that is not measuring (and every daemon iteration) pays one context-variable read.
The collector lives in a :class:`~contextvars.ContextVar` that :func:`recording` resets on exit:
no state outlives the command that created it.

Every duration is read from ONE monotonic clock (``time.monotonic`` unless one is injected, which
is how the tests exercise the shape without waiting). A phase's time is its own: while a phase is
open, a phase started inside it is subtracted from it, so the phases are disjoint and
``total_ms`` less their sum is the time no phase claims.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "HOLD_LIMIT",
    "PHASES",
    "SCHEMA",
    "Timings",
    "current",
    "interpreter_age_seconds",
    "phase",
    "recording",
    "timed_api",
    "timed_routes",
    "timed_selector",
]

#: The ``timings`` block's own version: bumped only when a key is renamed or removed.
SCHEMA = 1

#: Every phase the block always carries (zero count and zero ms when it did not happen), in the
#: order a command meets them.
PHASES: tuple[str, ...] = (
    "config_load",  # ``config.load()``: the effective configuration (the SQL load)
    "wiring",  # the plugin binding: adapter, identity and marker codec, before any request
    "host_check",  # the primary-host check and epoch read, when actually evaluated
    "host_check_reused",  # a hold that reused a batch's earlier check (count only: no time)
    "intake_lock",  # taking the per-binding flock
    "store_open",  # choosing and opening the store route (the Beads service session)
    "store_read",  # one read round trip of the report store
    "store_write",  # one write round trip of the report store
    "github_token",  # resolving the credential (``gh auth token``), once per GitHub operation
    "github_client",  # building the HTTP client, once per GitHub operation
    "github_request",  # one HTTP request, retries counted each
)

#: At most this many per-hold entries are listed (the rest are counted in ``holds_omitted``).
HOLD_LIMIT = 500

_READ_ROUTES = frozenset(
    {"issue_list", "issue_show_raw", "gate_list", "dependency_list", "find_duplicates"}
)
_READ_API = frozenset({"issue", "dependents", "issues"})


def interpreter_age_seconds() -> float | None:
    """Seconds since this process started, from the kernel's start time (Linux ``/proc``), or
    ``None`` where that is not available. Uses ``CLOCK_BOOTTIME``, the clock ``starttime`` counts
    on, for both ends."""
    try:
        with open("/proc/self/stat", "rb") as handle:
            raw = handle.read()
        # field 22 (starttime, clock ticks since boot); the command name in field 2 may hold
        # spaces, so count from the last closing parenthesis
        fields = raw[raw.rindex(b")") + 2 :].split()
        started = int(fields[19]) / os.sysconf("SC_CLK_TCK")
        return max(0.0, time.clock_gettime(time.CLOCK_BOOTTIME) - started)
    except (OSError, ValueError, IndexError, AttributeError):
        return None


#: Taken when this module is first imported: the ``bh`` entry point imports it before the command
#: tree, so what follows is the import cost of the CLI.
ENTRY_MONOTONIC = time.monotonic()
#: The interpreter's age at that moment, or ``None`` where the kernel does not say.
ENTRY_AGE_SECONDS = interpreter_age_seconds()


class _Unset:
    """The default of an argument whose absence means "this process's own"."""


_UNSET = _Unset()


@dataclass(eq=False)
class _Frame:
    name: str
    started: float
    child: float = 0.0


@dataclass
class _Hold:
    binding: str
    started: float
    phases: dict[str, list[float]] = field(default_factory=dict)


class Timings:
    """The collector of one command (see the module docstring). Not thread-safe: one command's
    one thread of work, like the store session it measures."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        entry: float | None | _Unset = _UNSET,
        interpreter_age: float | None | _Unset = _UNSET,
    ) -> None:
        """``clock`` is the one monotonic clock every duration is read from. ``entry`` is the
        ``clock`` reading when the process entered the CLI and ``interpreter_age`` the seconds
        the interpreter had run by then; they default to this process's own marks when ``clock``
        is the real one, and to unknown (``None``) under an injected clock."""
        real = clock is time.monotonic
        self._clock = clock
        self._started = clock()
        self._totals: dict[str, list[float]] = {name: [0, 0.0] for name in PHASES}
        self._stack: list[_Frame] = []
        self._hold: _Hold | None = None
        self._holds: list[dict[str, Any]] = []
        self._holds_omitted = 0
        if isinstance(entry, _Unset):
            entry = ENTRY_MONOTONIC if real else None
        if isinstance(interpreter_age, _Unset):
            interpreter_age = ENTRY_AGE_SECONDS if real else None
        self._entry_gap = None if entry is None else self._started - entry
        self._interpreter_age = interpreter_age

    # -- recording --------------------------------------------------------------------------

    def begin(self, name: str) -> _Frame:
        frame = _Frame(name, self._clock())
        self._stack.append(frame)
        return frame

    def end(self, frame: _Frame) -> None:
        elapsed = self._clock() - frame.started
        if frame in self._stack:
            while self._stack and self._stack.pop() is not frame:
                pass  # a frame left open inside this one is dropped, never double counted
        if self._stack:
            self._stack[-1].child += elapsed
        self.record(frame.name, max(0.0, elapsed - frame.child))

    def record(self, name: str, seconds: float, count: int = 1) -> None:
        """Add ``count`` occurrences of ``name`` totalling ``seconds``."""
        for table in (self._totals, self._hold.phases if self._hold else None):
            if table is None:
                continue
            entry = table.setdefault(name, [0, 0.0])
            entry[0] += count
            entry[1] += seconds

    @contextlib.contextmanager
    def phase(self, name: str) -> Iterator[None]:
        frame = self.begin(name)
        try:
            yield
        finally:
            self.end(frame)

    @contextlib.contextmanager
    def hold(self, binding: str) -> Iterator[None]:
        """One hold of the intake lock: its phases are also listed under ``holds``, so a batch
        shows what each report's operations cost (a report's operations share one hold)."""
        outer, mine = self._hold, _Hold(binding, self._clock())
        self._hold = mine
        try:
            yield
        finally:
            self._hold = outer
            if len(self._holds) >= HOLD_LIMIT:
                self._holds_omitted += 1
            else:
                self._holds.append(
                    {
                        "binding": mine.binding,
                        "ms": _ms(self._clock() - mine.started),
                        "phases": {
                            name: {"count": int(count), "ms": _ms(seconds)}
                            for name, (count, seconds) in mine.phases.items()
                        },
                    }
                )

    # -- the document -----------------------------------------------------------------------

    def document(self) -> dict[str, Any]:
        """The ``timings`` block: the shape documented in ``docs/CONFIGURATION.md``."""
        total = self._clock() - self._started
        phases = {
            name: {"count": int(count), "ms": _ms(seconds)}
            for name, (count, seconds) in self._totals.items()
        }
        claimed = sum(seconds for _count, seconds in self._totals.values())
        entry_to_work = self._entry_gap
        interpreter_to_entry = self._interpreter_age if entry_to_work is not None else None
        startup: dict[str, Any] = {
            "source": "interpreter" if interpreter_to_entry is not None else "entry",
            "ms": None,
            "interpreter_to_entry_ms": None,
            "entry_to_first_work_ms": None,
        }
        if entry_to_work is not None:
            startup["entry_to_first_work_ms"] = _ms(entry_to_work)
            startup["ms"] = _ms(entry_to_work + (interpreter_to_entry or 0.0))
            if interpreter_to_entry is not None:
                startup["interpreter_to_entry_ms"] = _ms(interpreter_to_entry)
        return {
            "schema": SCHEMA,
            "clock": "monotonic",
            "startup": startup,
            "total_ms": _ms(total),
            "unaccounted_ms": _ms(max(0.0, total - claimed)),
            "phases": phases,
            "holds": list(self._holds),
            "holds_omitted": self._holds_omitted,
        }


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 3)


_CURRENT: contextvars.ContextVar[Timings | None] = contextvars.ContextVar(
    "phase_timings", default=None
)


def current() -> Timings | None:
    """The collector of the command in progress, or ``None`` (not measuring)."""
    return _CURRENT.get()


@contextlib.contextmanager
def recording(timings: Timings | None) -> Iterator[Timings | None]:
    """Install ``timings`` (``None`` installs nothing) for the command, and as the tracker
    adapter's observer, restoring both on exit."""
    if timings is None:
        yield None
        return
    from .modules.tracker.contracts import observing

    token = _CURRENT.set(timings)
    try:
        with observing(timings):
            yield timings
    finally:
        _CURRENT.reset(token)


@contextlib.contextmanager
def phase(name: str) -> Iterator[None]:
    """``name`` as a phase of the command in progress; nothing when not measuring."""
    timings = _CURRENT.get()
    if timings is None:
        yield
        return
    with timings.phase(name):
        yield


# -- the store's I/O ------------------------------------------------------------------------------


class _TimedProxy:
    """``target`` with every public method timed as ``store_read`` (a name in ``reads``) or
    ``store_write``; anything that is not a public method is passed through untouched."""

    def __init__(self, target: Any, reads: frozenset[str], timings: Timings) -> None:
        self._target, self._reads, self._timings = target, reads, timings

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._target, name)
        if name.startswith("_") or not callable(attribute):
            return attribute
        phase_name = "store_read" if name in self._reads else "store_write"
        timings = self._timings

        def timed(*args: Any, **kwargs: Any) -> Any:
            frame = timings.begin(phase_name)
            try:
                return attribute(*args, **kwargs)
            finally:
                timings.end(frame)

        return timed


def timed_routes(routes: Any) -> Any:
    """``routes`` (the ``bd`` CLI routes) with each call timed, or itself when not measuring."""
    timings = _CURRENT.get()
    return routes if timings is None else _TimedProxy(routes, _READ_ROUTES, timings)


def timed_api(api: Any) -> Any:
    """The Beads-service API with each call timed, or itself when ``None`` or not measuring."""
    timings = _CURRENT.get()
    return api if timings is None or api is None else _TimedProxy(api, _READ_API, timings)


def timed_selector(select: Callable[[], Any]) -> Callable[[], Any]:
    """``select`` (the store's route decision: the API, or ``None`` for the CLI) with its first
    call timed as ``store_open`` and the API it returns timed per call; ``select`` itself when not
    measuring."""
    timings = _CURRENT.get()
    if timings is None:
        return select
    state: dict[str, Any] = {"decided": False, "api": None, "proxy": None}

    def selected() -> Any:
        if not state["decided"]:
            with timings.phase("store_open"):
                state["api"] = select()
            state["decided"] = True
        else:
            api = select()
            if api is not state["api"]:
                state["api"], state["proxy"] = api, None
        if state["proxy"] is None:
            state["proxy"] = timed_api(state["api"])
        return state["proxy"]

    return selected
