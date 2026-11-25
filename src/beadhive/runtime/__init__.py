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

import importlib.metadata
from collections.abc import Callable
from typing import Any

from ..config_consumer_ports import work_settings as config
from ..gate_resolution import GateResolution as GateResolution
from ..modules.config.domain import runtime_names
from ..seatrun import ScheduleRefused as ScheduleRefused
from .protocol import (
    CancellableRuntime as CancellableRuntime,
)
from .protocol import (
    ClosableRuntime as ClosableRuntime,
)
from .protocol import (
    NotifiableRuntime as NotifiableRuntime,
)
from .protocol import (
    RoleHandle as RoleHandle,
)
from .protocol import (
    RoleOutcome as RoleOutcome,
)
from .protocol import (
    Runtime as Runtime,
)
from .protocol import (
    can_cancel as can_cancel,
)
from .protocol import (
    can_close as can_close,
)
from .protocol import (
    can_notify as can_notify,
)
from .protocol import (
    is_adoptable as is_adoptable,
)
from .protocol import (
    schedule_accepts as schedule_accepts,
)

# The protocol surface (RoleHandle, RoleOutcome, Runtime, the optional capabilities) lives in the
# leaf module `.protocol` (bh-03rvd) and is re-exported above, so `from beadhive.runtime import
# RoleOutcome` is unchanged.


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


# The runtime registry: named factories, resolved by `work.runtime`. The name table lives in
# `modules.config.domain.runtime_names` (so the config schema can validate against it without
# importing this seam); this module is its public face. Mirrors `claim_authority`'s authority
# registry (`register_authority` / `available_authorities` / `get_authority`): lookup is by name,
# and a plugin that wants to be selectable either calls `register_runtime` itself or declares a
# `beadhive.runtimes` entry point, which `get_runtime` imports lazily — only the one named by
# `work.runtime` (`runtime_names.load_entry_point` documents the declaration format). A factory
# takes `(cfg, entry)` — the loaded config and the hive entry, either of which may be `None` —
# and returns a :class:`Runtime`.
RuntimeFactory = Callable[[Any, Any], Runtime]

_RUNTIMES = runtime_names.RUNTIMES
register_runtime = runtime_names.register_runtime
available_runtimes = runtime_names.available_runtimes
validate_runtime_name = runtime_names.validate_runtime_name


def _claude_factory(cfg, entry) -> Runtime:
    return ClaudeRuntime()


def _local_factory(cfg, entry) -> Runtime:
    # Imported lazily: `localloop` pulls in asyncio + the coordination surface, and
    # `get_runtime` is reached from config-reading paths that must stay import-light.
    from .. import localloop

    return localloop.runtime_from_config(cfg, entry)


def _temporal_factory(cfg, entry) -> Runtime:
    raise NotImplementedError(
        "work.runtime=temporal has no implementation yet — it ships in bh-c6dk.4"
    )


register_runtime("claude", _claude_factory, replace=True)
register_runtime("local", _local_factory, replace=True)
register_runtime("temporal", _temporal_factory, replace=True)


def get_runtime(cfg=None, entry=None, *, entry_points=importlib.metadata.entry_points) -> Runtime:
    """The configured runtime (`work.runtime`, default `local`) for `cfg` (loads config when
    omitted, falling back to `local`'s config-value resolution when none is loadable yet —
    e.g. before `bh config init` — matching `engine.get_engine`'s same-shaped fallback).

    `entry` is the hive's managed-repo entry: its `work.runtime` takes precedence over the host
    value, so each hive selects its own runtime. Resolution is by registered name
    (:func:`register_runtime`); on a miss the `beadhive.runtimes` entry point of that name — and
    only that one — is imported (`entry_points` is injectable for tests). An unregistered name
    raises `ValueError` listing the registered ones (fails closed).

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
    name = config.work_runtime(cfg, entry) if cfg is not None else "local"
    return runtime_names.resolve_factory(name, entry_points=entry_points)(cfg, entry)
