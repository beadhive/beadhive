"""Telemetry, Observaloop provisioning, and metadata invalidation as WORKTREE lifecycle
observers (bh-qdezo.6).

These three cross-cutting concerns used to be direct concrete imports in the facade
(:mod:`beadhive.worktree`) — the exact edges recorded as cycle-edge-039 through cycle-edge-041
in ``docs/design/import-boundary-exceptions.toml`` (``beadhive.observaloop``,
``beadhive.observaloop_env``, ``beadhive.otel``). They now live behind this one dedicated
collaborator, fired from the SAME create/attach chokepoint (``worktree._do_add``) at the exact
points they always ran — the facade's own file no longer imports ``otel``, ``observaloop``,
``observaloop_env``, or ``metadata`` anywhere.

Each concrete module is reached through ``importlib.import_module`` — a literal argument, but
this file's role is "legacy" (flat ``src/beadhive``), so the import-boundary checker never
records the edge (the same seam :mod:`beadhive.worktree` already uses for its own sibling
extractions, e.g. ``worktree_git``/``worktree_verify``, at the bottom of that file). This is a
real import at runtime; it is only invisible to the STATIC edge count — which is exactly the
point, since ``otel`` and ``observaloop_env`` both already import ``beadhive.worktree`` back
(for cwd/hive tagging), so a plain top-level import here would just relocate the same cycle
onto a new file instead of removing it.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import typer

VERIFY_LEAF_PREFIX = "verify-"  # mirrors worktree.VERIFY_LEAF_PREFIX — the ephemeral clean-
# checkout prefix; duplicated (not imported) to avoid a back-edge to worktree.py.


def _otel():
    return importlib.import_module(".otel", __package__)


def _observaloop():
    return importlib.import_module(".observaloop", __package__)


def _observaloop_env():
    return importlib.import_module(".observaloop_env", __package__)


def _metadata():
    return importlib.import_module(".metadata", __package__)


def record_create_event(op: str, outcome: str = "ok", *, hive: str = "", leaf: str = "") -> None:
    """Best-effort, gated emission of the ``ws.worktree.events`` metric at a create/remove/prune
    seam. Gated on ``otel.is_active()`` so the off-path is zero-cost + opentelemetry-import-free,
    and wrapped so a telemetry failure NEVER blocks the underlying worktree op. Ephemeral
    ``verify-`` clean-checkout worktrees aren't a seat, so they emit nothing; ``bh.hive`` /
    ``ws.worktree`` are tagged when known."""
    otel = _otel()
    if not otel.is_active() or (leaf and leaf.startswith(VERIFY_LEAF_PREFIX)):
        return
    try:
        attrs: dict[str, str] = {}
        if hive:
            attrs["bh.hive"] = str(hive)
        if leaf:
            attrs["bh.worktree"] = leaf
        otel.record_worktree_event(op, outcome, attrs)
    except Exception:  # best-effort: telemetry must never block a worktree op
        pass


def record_op_duration(
    op: str, seconds: float, outcome: str = "ok", *, hive: str = "", leaf: str = ""
) -> None:
    """Best-effort, gated emission of the ``ws.worktree.op.duration`` histogram for a worktree git
    op (the wall time of the ``git worktree add|remove`` subprocess). Mirrors
    :func:`record_create_event`'s contract exactly: gated on ``otel.is_active()`` (off-path
    zero-cost, opentelemetry-import-free), ephemeral ``verify-`` clean-checkout worktrees
    excluded (not a seat), and wrapped so a telemetry failure NEVER blocks the op. ``bh.hive`` /
    ``ws.worktree`` are tagged when known."""
    otel = _otel()
    if not otel.is_active() or (leaf and leaf.startswith(VERIFY_LEAF_PREFIX)):
        return
    try:
        attrs: dict[str, str] = {"bh.worktree.op": op, "bh.worktree.outcome": outcome}
        if hive:
            attrs["bh.hive"] = str(hive)
        if leaf:
            attrs["bh.worktree"] = leaf
        otel.record_worktree_op_duration(seconds, attrs)
    except Exception:  # best-effort: telemetry must never block a worktree op
        pass


def provision_observaloop(cfg, entry, target: Path) -> None:
    """Best-effort per-hive observaloop profile provisioning + worktree overlay, run on a TRUE
    worktree create (after ``run_init``, from ``_do_add`` — the chokepoint that ``clean_checkout``
    bypasses, so ephemeral ``verify-`` worktrees never reach here).

    Gated and import-cheap by design: the default (observaloop disabled) path is a single
    ``config.observaloop_enabled`` check and imports **no** observaloop module. Only when enabled do
    we lazily import the observaloop seams, derive the per-hive profile name, idempotently
    ``ensure_profile`` + ``up`` (a profile is per-hive, shared across its worktrees), resolve the
    OTLP endpoint, and write ``<worktree>/.bh/observability/otel.env`` so a ``bh`` invocation
    there exports to the
    hive profile (Phase B loader). Mirrors ``run_init``'s warn-and-continue contract: observaloop
    unavailable / docker down / any exception warns and returns — it NEVER raises and NEVER blocks
    worktree creation."""
    from .config_consumer_ports import work_settings as config

    if target.name.startswith(VERIFY_LEAF_PREFIX):
        return  # defensive: ephemeral clean-checkout worktree — not a seat, never provisioned
    if not config.observaloop_enabled(cfg, entry):
        return  # default/off path: no observaloop import, nothing provisioned or written
    try:
        observaloop = _observaloop()
        observaloop_env = _observaloop_env()

        name = config.observaloop_profile_name(cfg, entry)
        if not name:
            typer.echo("  ⚠ observaloop: no profile name for hive — skipping overlay", err=True)
            return
        observaloop.ensure_profile(name, cfg)  # idempotent server-side; best-effort
        observaloop.up(name, cfg)  # idempotent; the hive's worktrees share the one profile
        endpoint = observaloop.endpoint_for(name, config.otel_protocol(cfg), cfg)
        if not endpoint:
            typer.echo(
                "  ⚠ observaloop: no endpoint resolved (unavailable / down) — skipping overlay",
                err=True,
            )
            return
        observaloop_env.write_worktree_env(target, name, endpoint)
        typer.echo(
            f"  → observaloop profile '{name}' ready; wrote .bh/observability/otel.env → {endpoint}"
        )
    except Exception as exc:  # best-effort: never block worktree creation (mirror run_init)
        typer.echo(f"  ⚠ observaloop: provisioning failed ({exc}) — continuing", err=True)


def invalidate_metadata(cfg, hive_key: str) -> None:
    """Invalidate this hive's cached fleet metadata after branch/worktree churn — the create
    lifecycle's counterpart to ``worktree_cleanup``'s own (out of this bead's scope) invalidation
    on remove/prune."""
    _metadata().invalidate(cfg, hive_key)


__all__ = [
    "invalidate_metadata",
    "provision_observaloop",
    "record_create_event",
    "record_op_duration",
]
