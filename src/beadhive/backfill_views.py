"""Read-only review views over a history run: ``bh backfill view unit|conflict|xref|cost|verdicts``.

The input is a run directory (``bh backfill history``), its ``collation.json`` or any
``bdlg.collation/1`` document. Each view is one JSON object (``beadhive.backfill.view/1``) plus a
text rendering; nothing here writes a collation, a plan or a bead. The judge verdicts view is
:mod:`beadhive.backfill_verdicts` (``bh backfill verdicts``), reused rather than duplicated.

A unit's **decision** is the class of its last verdict that answered (``NEW``, ``PRESENT`` or
``NOISE``); a unit no step decided is ``residual``, left for a human.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from beadlog_sources import Collation, Lifecycle, conflicts_of, has_no_moment, read_collation

from . import backfill_verdicts

SCHEMA = "beadhive.backfill.view/1"
VIEWS = ("unit", "conflict", "xref", "cost", "verdicts")
COLLATION_FILE = "collation.json"
RESIDUAL = "residual"
_DECISIONS = ("NEW", "PRESENT", "NOISE", RESIDUAL)


class ViewInputError(ValueError):
    """The path is neither a run directory nor a collation document."""


def collation_path(path: Path) -> Path:
    """``path`` itself, or its ``collation.json`` when it is a run directory."""
    target = path / COLLATION_FILE if path.is_dir() else path
    if not target.is_file():
        raise ViewInputError(
            f"{path}: no collation (run `bh backfill collate` or `bh backfill history` first)"
        )
    return target


def load(path: Path) -> Collation:
    try:
        return read_collation(collation_path(path))
    except ViewInputError:
        raise
    except (OSError, ValueError) as exc:
        raise ViewInputError(f"{path}: not a bdlg.collation/1 document ({exc})") from exc


def decisions(collation: Collation) -> dict[str, dict[str, Any]]:
    """Unit key to its deciding verdict (the last one whose class is set)."""
    decided: dict[str, dict[str, Any]] = {}
    for record in collation.verdicts:
        if record["class"] is not None:
            decided[record["unit"]] = dict(record)
    return decided


def _envelope(view: str, path: Path, **body: Any) -> dict[str, Any]:
    return {"schema": SCHEMA, "view": view, "path": str(path), **body}


def unit_view(path: Path, *, decision: Sequence[str] = ()) -> dict[str, Any]:
    """Every unit with its decision, matched bead, status and dates; ``decision`` filters."""
    collation = load(path)
    decided = decisions(collation)
    life = {record["unit"]: record for record in collation.lifecycle}
    wanted = {d.upper() if d.lower() != RESIDUAL else RESIDUAL for d in decision}
    units = []
    for unit in collation.units:
        record = decided.get(unit.key)
        cls = record["class"] if record else RESIDUAL
        if wanted and cls not in wanted:
            continue
        status = life.get(unit.key) or {}
        no_moment = cls == "NEW" and bool(status) and has_no_moment(Lifecycle.from_json(status))
        units.append(
            {
                "key": unit.key,
                "kind": unit.kind,
                "rule": unit.rule,
                "title": unit.title,
                "decision": cls,
                "bead": (record or {}).get("bead") or unit.bead,
                "step": (record or {}).get("step"),
                "review": (record or {}).get("review"),
                "parent": unit.parent,
                "artifacts": len(unit.artifacts),
                "docs": len(collation.docs_of(unit.key)),
                "status": status.get("status"),
                "closed": (status.get("closed") or {}).get("at"),
                "skipped": "no evidence moment" if no_moment else None,
            }
        )
    counts = Counter(u["decision"] for u in units)
    return _envelope(
        "unit",
        path,
        counts={name: counts.get(name, 0) for name in _DECISIONS},
        units=units,
    )


def conflict_view(path: Path) -> dict[str, Any]:
    """Every status conflict (B6), both sources shown, none resolved."""
    collation = load(path)
    conflicts = [
        {
            "rule": c.rule,
            "unit": c.unit,
            "status": c.status,
            "detail": c.detail,
            "sources": [
                {"artifact": s.artifact, "field": s.field, "raw": s.raw, "value": s.value}
                for s in c.sources
            ],
        }
        for c in conflicts_of(collation)
    ]
    return _envelope(
        "conflict",
        path,
        counts=dict(sorted(Counter(c["rule"] for c in conflicts).items())),
        conflicts=conflicts,
    )


def xref_view(path: Path) -> dict[str, Any]:
    """The resolved refs that point outside the unit: foreign ledgers and tracker artifacts."""
    collation = load(path)
    xrefs = []
    for ref in collation.refs:
        if ref.outcome in ("foreign", "ambiguous") or (
            ref.outcome == "artifact" and any(t.startswith("gh:") for t in ref.targets)
        ):
            xrefs.append(
                {
                    "source": ref.source,
                    "raw": ref.ref.raw,
                    "outcome": ref.outcome,
                    "ledger": ref.ledger,
                    "candidates": list(ref.candidates),
                    "targets": list(ref.targets),
                }
            )
    by_ledger = Counter(x["ledger"] or "(none)" for x in xrefs)
    return _envelope(
        "xref",
        path,
        counts={"total": len(xrefs), "by_ledger": dict(sorted(by_ledger.items()))},
        xrefs=xrefs,
    )


def cost_view(path: Path, cost_per_call_usd: float | None) -> dict[str, Any]:
    """The judge calls the collation recorded and their estimated cost."""
    collation = load(path)
    items = backfill_verdicts.load_items(collation_path(path))[1]
    calls = sum(1 for item in items if backfill_verdicts.is_call(item))
    engines = Counter(str(item["engine"]) for item in items)
    estimate = None if cost_per_call_usd is None else round(calls * cost_per_call_usd, 6)
    return _envelope(
        "cost",
        path,
        judge_calls=calls,
        verdicts=len(items),
        residual=len(collation.residual),
        by_engine=dict(sorted(engines.items())),
        cost_per_call_usd=cost_per_call_usd,
        estimated_usd=estimate,
    )


def verdicts_view(path: Path, cost_per_call_usd: float | None) -> dict[str, Any]:
    """``bh backfill verdicts`` over the run's collation (reused, not duplicated)."""
    return backfill_verdicts.build_view(
        collation_path(path),
        classes=[],
        engine=None,
        min_confidence=None,
        max_confidence=None,
        cost_per_call_usd=cost_per_call_usd,
    )


def render_text(view: Mapping[str, Any]) -> list[str]:
    """The text form of a view (the JSON is the machine form)."""
    kind = view.get("view")
    if kind == "unit":
        counts = view["counts"]
        lines = ["decisions: " + ", ".join(f"{k}={counts[k]}" for k in _DECISIONS)]
        for unit in view["units"]:
            bead = unit["bead"] or "-"
            lines.append(
                f"{unit['decision']:<8} {unit['key']}  [{unit['kind']}/{unit['rule']}] "
                f"bead={bead} status={unit['status'] or '-'}  {unit['title']}"
                + (
                    f"  [skipped: {unit['skipped']} under create=propose]"
                    if unit["skipped"]
                    else ""
                )
            )
        return lines
    if kind == "conflict":
        lines = [f"conflicts: {len(view['conflicts'])}"]
        for item in view["conflicts"]:
            lines.append(f"{item['rule']} {item['unit']}: {item['detail']}")
            lines.extend(f"    {s['artifact']} {s['field']}={s['raw']!r}" for s in item["sources"])
        return lines
    if kind == "xref":
        lines = [f"cross references: {view['counts']['total']}"]
        lines.extend(
            f"{x['outcome']:<9} {x['source']} -> {x['raw']} (ledger {x['ledger'] or '-'})"
            for x in view["xrefs"]
        )
        return lines
    if kind == "cost":
        money = "$?" if view["estimated_usd"] is None else f"${view['estimated_usd']:.2f}"
        return [
            f"{view['judge_calls']} judge calls recorded ≈ {money} "
            + (
                "(backfill.judge.cost_per_call_usd)"
                if view["cost_per_call_usd"] is not None
                else "(no backfill.judge.cost_per_call_usd configured)"
            ),
            f"verdicts: {view['verdicts']}; residual: {view['residual']}",
        ]
    return backfill_verdicts.render_text(view)


__all__ = [
    "SCHEMA",
    "VIEWS",
    "ViewInputError",
    "collation_path",
    "conflict_view",
    "cost_view",
    "decisions",
    "load",
    "render_text",
    "unit_view",
    "verdicts_view",
    "xref_view",
]
