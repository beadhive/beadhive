"""Runtime seam — the swappable scheduler that wakes a role binary for a bead.

`docs/design/work-runtime-tiers-adr.md` draws the boundary this module implements: **beads
owns lifecycle state, the runtime owns process scheduling only** (Decision 1). This module is
that seam: a `Runtime` protocol naming exactly the three things any scheduler needs to do —
schedule a role binary for a bead, observe whether it finished, and react when a gate it was
waiting on resolves — not a general orchestration abstraction. Modeled on `engine.py`'s
`beads.engine` seam (Decision 2's own words: "reusing it costs a config key and buys
consistency; inventing a second, richer extension mechanism ... would be the drift `engine.py`
was written to avoid"): a config key (`work.runtime`) selects a thin implementation, not a
plugin framework.

This bead (bh-c6dk.1) lands the seam only:

- The `claude` tier is DOCUMENTED, not developed (ADR Decision 2) — it is today's Task-tool
  sub-agent fanout, already works, and is the only tier bound to a specific harness. bh itself
  never calls a `Runtime` object in this tier: a dispatcher session issues Task-tool calls
  directly, entirely outside this seam. `ClaudeRuntime` exists so `get_runtime()` has something
  concrete to return and so the tier has one visible, testable anchor in code, not because it
  schedules anything itself.
- The `local` tier (poll loop + subprocess supervision) LANDED in bh-c6dk.5 and lives in
  `beadhive.localloop` — `get_runtime()` returns its `LocalRuntime` for `work.runtime: local`
  (the default). It is kept in its own module because the tier is process supervision, which is
  a great deal more than a protocol implementation; this file stays the seam.
- The `temporal` tier (`DeveloperWorkflow` + `run_role` activity) is bh-c6dk.4, a separate bead,
  and its registered factory still raises `NotImplementedError` — an honest gap, not a silent
  no-op, exactly as `engine.get_engine` raises for any `beads.engine` value besides `bd` until
  its sibling beads land theirs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from .config_consumer_ports import work_settings as config

# The bead's own no-runtime-only-state invariant (ADR Decision 1) means nothing in this module
# may persist scheduling state anywhere but beads. `RoleHandle`/`RoleOutcome` below are transient
# in-process values a caller holds for the lifetime of one `schedule`/`observe` pair — never
# written to disk, never the source of truth for whether a bead is claimed, blocked, approved,
# or done. That answer always comes from `bd` (gates, leases, the merge slot).


@dataclass(frozen=True)
class RoleHandle:
    """Opaque token identifying one dispatched role run, returned by `schedule()` and passed
    back into `observe()`. Deliberately carries no process internals (no PID, no asyncio Task,
    no Temporal `workflow_id`) — those are tier-specific and must not leak into the protocol
    surface; a tier that needs to correlate a handle to its own bookkeeping does so on its own
    concrete type, which callers only ever see through this seam."""

    bead_id: str
    session_id: str


@dataclass(frozen=True)
class RoleOutcome:
    """Outcome of one role run, as observed via `observe()`. `status` mirrors the vocabulary
    the role-binary contract settles on (`docs/design/work-runtime-tiers-adr.md` Amendment 2
    §1's `SeatRun`/`RoleOutcome.status`: done | blocked | handoff) plus `running`, the
    not-finished-yet case every poll-based tier needs to express, and `failed` for the
    other-exit-code / infra-failure case (Decision 4's *failure vs judgment* split). This is
    bh's OWN result type, not a re-export of baml-harness's `RoleOutcome` — the two are allowed
    to diverge; nothing here depends on baml-harness being installed."""

    status: str  # "running" | "done" | "blocked" | "handoff" | "failed"
    summary: str = ""
    routing: dict | None = None


@dataclass(frozen=True)
class GateResolution:
    """One review gate observed resolving, handed to `Runtime.on_gate_resolved`
    (gas-city-runtime-seam-adr.md Decision 3). These are the bounce and feedback points.

    `verdict` is `approved` or `changes_requested`. `feedback_ref` is `None` for an approval and,
    for a bounce, a pointer a runtime can resolve to the reviewer's feedback: the id of the gate
    that was resolved (its close reason carries the feedback text; the bead also holds it as a
    comment). A runtime may use the hook to wind a session down or keep it warm and attach the
    feedback. It must NOT re-dispatch: changes-requested work is re-scheduled by the loop's
    `resume-changes-requested` decision, which calls `schedule()` again."""

    gate_id: str
    bead_id: str
    verdict: Literal["approved", "changes_requested"]
    feedback_ref: str | None = None


@runtime_checkable
class Runtime(Protocol):
    """The operations `bh` needs from a scheduler. Three verbs, matching the ADR's framing of
    the gap a runtime fills (Evidence 2: "which processes should be running right now, and
    what happens when one dies?") plus the one push bh needs when beads state changes out from
    under a sleeping scheduler. Nothing else — no retry policy, no budget governance, no
    workflow modeling; those stay out of scope for the same reason `engine.Engine` doesn't grow
    a method per tracker verb."""

    name: str

    def schedule(
        self,
        bead_id: str,
        role: str,
        *,
        workspace,
        instructions,
        session_id: str,
        model: str | None = None,
        decision=None,
    ) -> RoleHandle:
        """Start (or hand off to a worker that will start) the role binary
        (`bh-<role> --workspace <workspace> --bead <bead_id> --instructions <instructions>
        --session_id <session_id>`, the contract `docs/design/work-runtime-tiers-adr.md`
        Amendment 2 §1 settles) against `bead_id`, and return a handle to observe it by.
        ``decision`` carries the canonical shared routing verdict when the caller has resolved
        one; concrete runtimes translate its model only at their harness launch boundary.
        Idempotent on the caller's side the same way the contract requires the binary itself to
        be: scheduling an already-advanced bead is a no-op a tier is free to detect however it
        likes (immediately-`done` `observe()`, a dedup on `session_id`, ...)."""
        ...

    def observe(self, handle: RoleHandle) -> RoleOutcome:
        """Report what is known right now about the run `handle` identifies. `running` is a
        legitimate, expected answer for a poll-based tier — this is a status read, not a
        blocking wait. Never authoritative about bead lifecycle state; a caller that needs to
        know whether the bead itself is claimed/blocked/approved/done asks `bd`, not this."""
        ...

    def on_gate_resolved(self, resolution: GateResolution) -> None:
        """React to a review gate having resolved (approved or bounced) — e.g. wake a sleeping
        poll loop early, wind a session down, or keep a long-lived one warm and attach
        `resolution.feedback_ref`. The loop's `gate_check` pass calls this for each gate it
        observes resolving: delivery is AT-LEAST-ONCE (an implementation must be idempotent) and
        latency is bounded by `work.dispatch.poll_interval` — there is no push doorbell. A hook
        that raises is logged and never breaks the pass. Implementations must not re-dispatch:
        re-scheduling bounced work is the loop's decision, never the runtime's. A tier that has
        no use for the signal may implement this as a no-op; the point of naming it in the
        protocol is that EVERY tier gets asked, not that every tier must act."""
        ...


class ClaudeRuntime:
    """The `claude` tier's anchor in code — documented, not developed (ADR Decision 2). In this
    tier bh never schedules anything itself: a human (or the Claude Code harness's own
    Task-tool sub-agent fanout) drives the dispatcher session that issues `bh work` verbs
    directly, and there is no in-process scheduler for `get_runtime()`'s caller to hand work to.
    Every method below raises, on purpose — a silent no-op here would look like a working
    scheduler that simply never does anything, which is a worse failure mode than a loud
    "this tier has no runtime, read the docs" error. See `docs/WORK.md#runtime-tiers`.
    """

    name = "claude"

    _MSG = (
        "work.runtime=claude is the documented Task-tool sub-agent fanout, driven by the "
        "harness session directly — bh does not schedule role binaries for it. See "
        "docs/WORK.md#runtime-tiers."
    )

    def schedule(
        self, bead_id, role, *, workspace, instructions, session_id, model=None, decision=None
    ):
        raise NotImplementedError(self._MSG)

    def observe(self, handle):
        raise NotImplementedError(self._MSG)

    def on_gate_resolved(self, resolution):
        raise NotImplementedError(self._MSG)


# The runtime registry: named factories, resolved by `work.runtime`. Mirrors
# `claim_authority`'s authority registry (`register_authority` / `available_authorities` /
# `get_authority`): lookup is by name, nothing is auto-discovered, and a plugin that wants to be
# selectable calls `register_runtime` itself. A factory takes `(cfg, entry)` — the loaded config
# and the hive entry, either of which may be `None` — and returns a :class:`Runtime`.
RuntimeFactory = Callable[[Any, Any], Runtime]

_RUNTIMES: dict[str, RuntimeFactory] = {}


def register_runtime(name: str, factory: RuntimeFactory, *, replace: bool = False) -> None:
    """Register `factory` under `name` so `work.runtime: <name>` resolves it.

    The plugin seam (docs/design/gas-city-runtime-seam-adr.md Decision 1): a third-party runtime
    (e.g. Gas City) calls this to become selectable by name. It is NOT plugin discovery — nothing
    auto-calls it. Registering an already-registered name raises `ValueError` unless
    ``replace=True``, so two plugins cannot silently shadow each other (or a built-in)."""
    if not name or not isinstance(name, str):
        raise ValueError("a runtime name must be a non-empty string")
    if name in _RUNTIMES and not replace:
        raise ValueError(f"runtime {name!r} is already registered (pass replace=True to override)")
    _RUNTIMES[name] = factory


def available_runtimes() -> list[str]:
    """The names the registry can resolve, sorted — surfaced in the unknown-runtime error."""
    return sorted(_RUNTIMES)


def _unknown_runtime(name: str) -> ValueError:
    return ValueError(
        f"unknown work runtime {name!r}; available: {', '.join(available_runtimes())}"
    )


def validate_runtime_name(name: str) -> str:
    """`name` itself when registered, else `ValueError` listing the available names — the check
    the config schema runs when `work.runtime` loads."""
    if name not in _RUNTIMES:
        raise _unknown_runtime(name)
    return name


def _claude_factory(cfg, entry) -> Runtime:
    return ClaudeRuntime()


def _local_factory(cfg, entry) -> Runtime:
    # Imported lazily: `localloop` pulls in asyncio + the coordination surface, and
    # `get_runtime` is reached from config-reading paths that must stay import-light.
    from . import localloop

    return localloop.runtime_from_config(cfg, entry)


def _temporal_factory(cfg, entry) -> Runtime:
    raise NotImplementedError(
        "work.runtime=temporal has no implementation yet — it ships in bh-c6dk.4"
    )


register_runtime("claude", _claude_factory)
register_runtime("local", _local_factory)
register_runtime("temporal", _temporal_factory)


def get_runtime(cfg=None) -> Runtime:
    """The configured runtime (`work.runtime`, default `local`) for `cfg` (loads config when
    omitted, falling back to `local`'s config-value resolution when none is loadable yet —
    e.g. before `bh config init` — matching `engine.get_engine`'s same-shaped fallback).

    Resolution is by registered name (:func:`register_runtime`); an unregistered name raises
    `ValueError` listing the available ones (fails closed).

    `local` is the real, running default (bh-c6dk.5, `beadhive.localloop.LocalRuntime`).
    `claude` is documented, not developed — its anchor exists to name the tier and raises on
    every method (see `ClaudeRuntime`). `temporal` is still a load-bearing gap: it ships in
    bh-c6dk.4 and its factory raises `NotImplementedError` naming it until it does, the same
    shape `engine.get_engine` uses for any `beads.engine` value besides `bd`."""
    if cfg is None:
        try:
            cfg = config.load()
        except FileNotFoundError:
            cfg = None
    entry = None
    name = config.work_runtime(cfg, entry) if cfg is not None else "local"
    try:
        factory = _RUNTIMES[name]
    except KeyError:
        raise _unknown_runtime(name) from None
    return factory(cfg, entry)
