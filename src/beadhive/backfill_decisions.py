"""Per-unit operator decisions of ``bh backfill`` (bh-2sicb; ADR ``backfill-rerun-control``, Q3).

``bh backfill decide`` records ``skip``, ``noise`` or ``link <bead>`` for one unit key as a closed
``decision`` bead in the hive's own bead store (:mod:`beadlog_sources.decisions` owns the record's
shape); identify consults it as step 0 on every later run. This module is the hive side: it checks
the target against the live store, writes through the loader (the same guarded bd write path as
every other backfill write), recovers the ``external_ref`` of a deleted bead through one
``bd history <id>`` read, finds the beads a previous apply created that are gone now
(``backfill.decisions.on_delete``), and builds the ``view decisions`` listing.

Nothing here compacts, flattens or rewrites history: a decision is one more closed bead, and
re-deciding a unit updates that bead's metadata (bd keeps the earlier answer).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from beadhive_backfill import BackfillError, Plan
from beadlog import BeadEvent, BeadRef, fold
from beadlog_sources import (
    DECISION_REF_PREFIX,
    DECISION_SOURCE_LABEL,
    Decision,
    DecisionError,
    decision_events,
    decision_id,
    read_decisions,
)
from beadlog_sources import decisions as _decisions

from . import bd
from .backfill_bd import BdStoreLoader, Clock, system_clock

ON_DELETE_MODES = ("ignore", "report", "skip")
SCHEMA = "bh.backfill.decisions/1"
ORIGIN = "origin:backfill"

HistoryReader = Callable[[Path, str], Sequence[Mapping[str, Any]] | None]


def bead_history(cwd: Path, ident: str) -> Sequence[Mapping[str, Any]] | None:
    """``bd history <id> --json``: the one targeted read that still knows a deleted bead (the seam
    tests replace). ``None`` when bd could not answer."""
    res = bd.routes(cwd).issue_history(ident)
    if res.returncode != 0:
        return None
    try:
        out = json.loads(res.stdout or "null")
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, list) else None


def deleted_ref(cwd: Path, ident: str, history: HistoryReader | None = None) -> str:
    """The ``external_ref`` (the unit key) of the deleted backfilled bead ``ident``."""
    rows = (history or bead_history)(cwd, ident)
    if rows is None:
        raise BackfillError(f"bd history {ident} failed; nothing was recorded")
    for row in rows:  # newest snapshot first; the first one that carries the ref wins
        issue = row.get("Issue") if isinstance(row, Mapping) else None
        if not isinstance(issue, Mapping):
            continue
        ref = issue.get("external_ref")
        if not isinstance(ref, str) or not ref:
            continue
        labels = issue.get("labels")
        if isinstance(labels, list) and ORIGIN not in labels:
            raise BackfillError(f"{ident} is not a backfilled bead (no {ORIGIN} label)")
        if ref.startswith(DECISION_REF_PREFIX):
            raise BackfillError(f"{ident} is a decision record, not a backfilled unit")
        return ref
    raise BackfillError(f"bd history {ident} holds no external_ref; name the unit key instead")


def _check_target(kind: str, target: str | None, states: Mapping[BeadRef, Any]) -> None:
    """``link`` needs an existing live bead, ``noise`` a live epic (reconcile's covering rule)."""
    if kind == _decisions.SKIP:
        return
    live = {
        ref.id: state
        for ref, state in states.items()
        if state.exists and not state.deleted and not state.erased
    }
    state = live.get(str(target))
    if state is None or DECISION_SOURCE_LABEL in state.labels:
        raise DecisionError(f"{target!r} is not a live bead in this hive")
    if kind == _decisions.NOISE and state.type != "epic":
        raise DecisionError(f"noise needs a covering epic; {target} is a {state.type}")


def record_decision(
    loader: BdStoreLoader,
    *,
    prefix: str,
    labels: Sequence[str],
    key: str,
    kind: str,
    target: str | None = None,
    reason: str = "",
    dry_run: bool = False,
    clock: Clock = system_clock,
) -> dict[str, Any]:
    """Record (or, with ``dry_run``, only describe) the decision ``kind`` on unit ``key``.

    The summary names the decision bead exactly (id, labels, fields) so a dry run shows what a real
    one writes; ``action`` is ``create`` for a new record and ``update`` when the unit was decided
    before (the same bead, by its deterministic id)."""
    _decisions.check(kind, target)
    if key.startswith(DECISION_REF_PREFIX):
        raise DecisionError("name the unit key, not a decision record's reference")
    baseline = tuple(loader.export())
    states = fold(baseline)
    _check_target(kind, target, states)
    ident = decision_id(prefix, loader.group, key)
    state = states.get(BeadRef(loader.group, ident))
    live = state is not None and state.exists and not state.deleted and not state.erased
    events = decision_events(
        prefix=prefix,
        group=loader.group,
        key=key,
        kind=kind,
        target=target,
        reason=reason,
        decided_by=str(getattr(loader.port, "actor", "") or ""),
        decided_at=clock(),
        labels=labels,
        existing=state.fields if live and state is not None else None,
    )
    report = loader.load(events, dry_run=dry_run)
    created = next((e for e in events if str(e.kind) == "create"), None)
    fields = dict(created.data["fields"]) if created is not None else _fields(events)
    return {
        "schema": SCHEMA,
        "dry_run": dry_run,
        "action": "update" if live else "create",
        "bead": ident,
        "type": _decisions.BEAD_TYPE,
        "status": "closed",
        "labels": sorted({*labels, DECISION_SOURCE_LABEL}),
        "fields": fields,
        "events": len(events),
        "loss_report": report,
    }


def _fields(events: Sequence[BeadEvent]) -> dict[str, Any]:
    return {e.data["field"]: e.data["value"] for e in events if str(e.kind) == "set"}


def list_decisions(loader: BdStoreLoader) -> dict[str, Any]:
    """The ``view decisions`` body: every live decision record of the hive, in unit-key order."""
    found = read_decisions(loader.export())
    items = [found[key].to_json() for key in sorted(found)]
    counts = {
        kind: sum(1 for item in items if item["decision"] == kind) for kind in _decisions.KINDS
    }
    return {"schema": SCHEMA, "view": "decisions", "counts": counts, "decisions": items}


def render_decisions(view: Mapping[str, Any]) -> list[str]:
    """The text form of :func:`list_decisions`."""
    counts = view["counts"]
    lines = ["decisions: " + ", ".join(f"{k}={counts[k]}" for k in _decisions.KINDS)]
    for item in view["decisions"]:
        target = f" -> {item['target']}" if item["target"] else ""
        reason = f"  {item['reason']}" if item["reason"] else ""
        by = f" by {item['decided_by']}" if item["decided_by"] else ""
        lines.append(f"{item['decision']:<5} {item['unit']}{target}  [{item['bead']}{by}]{reason}")
    return lines


def deleted_units(
    plan_path: Path, baseline: Sequence[BeadEvent], *, applied: bool
) -> list[tuple[str, str]]:
    """``(unit key, bead id)`` of the beads the previous apply created that the baseline no longer
    holds, minus the units already decided: "deleted since last apply" (``on_delete``).

    The previous plan's ``create`` events carry the unit key as ``external_ref`` and the minted
    bead id. Nothing is found when the previous run never applied, or its plan is unreadable (the
    detection is host-local; a missing run directory is not an error)."""
    if not applied or not plan_path.is_file():
        return []
    try:
        plan = Plan.read(plan_path)
    except BackfillError:
        return []
    states = fold(baseline)
    decided = read_decisions(baseline)
    out: list[tuple[str, str]] = []
    for event in plan.applicable_events():
        if str(event.kind) != "create":
            continue
        key = (event.data.get("fields") or {}).get("external_ref")
        if not isinstance(key, str) or not key or key in decided:
            continue
        state = states.get(event.bead)
        if state is None or not state.exists or state.deleted or state.erased:
            out.append((key, event.bead.id))
    return sorted(set(out))


__all__ = [
    "ON_DELETE_MODES",
    "SCHEMA",
    "Decision",
    "bead_history",
    "deleted_ref",
    "deleted_units",
    "list_decisions",
    "record_decision",
    "render_decisions",
]
