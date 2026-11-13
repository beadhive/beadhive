"""Read-only judge verdict review for ``bh backfill inspect --verdicts``.

Collects the verdicts recorded in a ``.bdlg`` plan or extract archive (the ``bdlg.reconcile``
entries and the ``beadhive.backfill`` pass judgments, both written as
``beadlog_sources.reconcile.Verdict.to_json``) or listed in a ``bdlg.collation/1`` JSON file's
``verdicts`` member (and its ``residual`` list), normalizes them to one item shape, filters them
and renders groups by
class. Nothing here writes a plan, an archive or a bead.

A collation's ``verdicts`` are flat records (``unit``, ``step``, ``class`` or null, ``bead``,
``provenance``, ``review``, ``score``, ``confidence_permille`` ... ``fallback``), one per step
that answered; a null class is shown as ``abstain``. Its ``residual`` items (``unit``,
``reason``, ``score``, ``query_hash``, ``candidates``) are listed under ``residual``. Reading is
tolerant: unknown members are ignored and a missing or malformed member gives an empty view.
"""

from __future__ import annotations

import json
import zipfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from beadlog import ArchiveReader

SCHEMA = "beadhive.backfill.verdicts/1"
RECONCILE_ANNOTATION = "bdlg.reconcile"
ENGINE_ANNOTATION = "beadhive.backfill"
COLLATION_SCHEMA_PREFIX = "bdlg.collation/"

CLASS_ORDER = ("NEW", "PRESENT", "NOISE", "SKIP", "DRIFT", "abstain", "residual", "other")
# Engines that answer without calling a model; a verdict from anywhere else (or a recorded
# fallback after a failed attempt) is counted as a judge call for the cost estimate.
_NO_CALL_ENGINES = frozenset({"deterministic-lexical", "recorded"})
_NO_ATTEMPT_REASONS = frozenset({"max-calls"})


class VerdictInputError(ValueError):
    """The path is neither a BeadLog archive nor a collation JSON document."""


def _outcome_class(outcome: Any) -> str:
    if outcome is None:
        return "abstain"
    head = str(outcome).split(":", 1)[0].lower()
    return {"new": "NEW", "present": "PRESENT", "noise": "NOISE"}.get(head, "other")


def _normalize_class(value: Any, outcome: Any) -> str:
    if isinstance(value, str):
        for known in CLASS_ORDER:
            if value.lower() == known.lower():
                return known
    return _outcome_class(outcome)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _ref(value: Any) -> dict[str, str] | None:
    if isinstance(value, Mapping) and "id" in value:
        return {"group": str(value.get("group", "")), "id": str(value["id"])}
    return None


def _fallback(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        "engine": str(value.get("engine") or ""),
        "reason": str(value.get("reason") or ""),
        "detail": str(value.get("detail") or ""),
    }


def normalize(
    verdict: Mapping[str, Any],
    *,
    source: str,
    cls: Any = None,
    entry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One verdict record (and its optional reconcile entry) in the stable item shape."""
    entry = entry or {}
    outcome = verdict.get("outcome")
    return {
        "source": source,
        "class": _normalize_class(cls, outcome),
        "bead": _ref(entry.get("bead")),
        "match": _ref(entry.get("match")),
        "covered_by": _ref(entry.get("covered_by")),
        "review": entry.get("review"),
        "outcome": None if outcome is None else str(outcome),
        "confidence": _number(verdict.get("confidence")),
        "engine": str(verdict.get("engine") or "unknown"),
        "engine_version": verdict.get("engine_version"),
        "model": verdict.get("model"),
        "fallback": _fallback(verdict.get("fallback")),
        "family": verdict.get("family"),
        "query_hash": verdict.get("query_hash"),
        "rationale": verdict.get("rationale") or "",
        "note": entry.get("note") or "",
        "unit": None,
        "step": None,
        "provenance": None,
        "score": None,
        "reason": None,
        "candidates": None,
    }


def _archive_items(path: Path) -> tuple[list[dict[str, Any]], int]:
    with ArchiveReader(path) as reader:
        data = reader.manifest.to_json()
    annotations = data.get("annotations") or {}
    items: list[dict[str, Any]] = []
    structural = 0
    reconcile = annotations.get(RECONCILE_ANNOTATION) or {}
    for entry in reconcile.get("entries") or ():
        verdict = entry.get("verdict")
        if isinstance(verdict, Mapping):
            items.append(
                normalize(verdict, source="reconcile", cls=entry.get("class"), entry=entry)
            )
        else:
            structural += 1
    engine = annotations.get(ENGINE_ANNOTATION) or {}
    for verdict in engine.get("judgments") or ():
        if isinstance(verdict, Mapping):
            items.append(normalize(verdict, source="judgments"))
    return items, structural


def _bead(value: Any) -> dict[str, str] | None:
    if isinstance(value, str) and value:
        return {"group": "", "id": value}
    return _ref(value)


def _permille(value: Any) -> float | None:
    number = _number(value)
    return None if number is None else number / 1000


def _collation_record(element: Mapping[str, Any]) -> dict[str, Any]:
    """A flat ``verdicts`` record of a collation (one per step that answered)."""
    item = normalize(
        {
            "outcome": element.get("outcome"),
            "confidence": _permille(element.get("confidence_permille")),
            "engine": element.get("engine"),
            "engine_version": element.get("engine_version"),
            "model": element.get("model"),
            "fallback": element.get("fallback"),
            "family": element.get("family"),
            "query_hash": element.get("query_hash"),
            "rationale": element.get("rationale"),
        },
        source="collation",
        cls=element.get("class"),
        entry={"review": element.get("review")},
    )
    if element.get("class") is None:
        item["class"] = "abstain"
    item["bead"] = _bead(element.get("bead"))
    item["unit"] = element.get("unit")
    item["step"] = element.get("step")
    item["provenance"] = element.get("provenance")
    item["score"] = _number(element.get("score"))
    item["is_call"] = element.get("step") == "judge" and item["fallback"] is None
    return item


def _collation_residual(element: Mapping[str, Any]) -> dict[str, Any]:
    """A ``residual`` item: a unit no step could decide, for a human."""
    item = normalize({}, source="collation")
    item.update(
        {
            "class": "residual",
            "engine": "none",
            "unit": element.get("unit"),
            "reason": element.get("reason"),
            "score": _number(element.get("score")),
            "query_hash": element.get("query_hash"),
            "candidates": element.get("candidates"),
            "is_call": False,
        }
    )
    return item


def _collation_items(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for key, build in (("verdicts", _collation_record), ("residual", _collation_residual)):
        raw = document.get(key)
        items.extend(
            build(el) for el in (raw if isinstance(raw, list) else ()) if isinstance(el, Mapping)
        )
    return items


def load_items(path: Path) -> tuple[str, list[dict[str, Any]], int]:
    """``(input kind, items, structural entries without a verdict)`` for an archive or collation."""
    if zipfile.is_zipfile(path):
        items, structural = _archive_items(path)
        return "archive", items, structural
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VerdictInputError(f"{path}: not a BeadLog archive or collation JSON ({exc})") from exc
    schema = document.get("schema") if isinstance(document, Mapping) else None
    is_collation = isinstance(schema, str) and schema.startswith(COLLATION_SCHEMA_PREFIX)
    if not isinstance(document, Mapping) or not (is_collation or "verdicts" in document):
        raise VerdictInputError(f"{path}: not a bdlg.collation/1 document")
    return "collation", _collation_items(document), 0


def select(
    items: Iterable[dict[str, Any]],
    *,
    classes: Sequence[str] = (),
    engine: str | None = None,
    min_confidence: float | None = None,
    max_confidence: float | None = None,
) -> list[dict[str, Any]]:
    """Filter items; a class matches case-insensitively, an engine by name or model."""
    wanted = {c.lower() for c in classes}
    out = []
    for item in items:
        if wanted and item["class"].lower() not in wanted:
            continue
        if engine and engine not in (item["engine"], item["model"]):
            continue
        confidence = item["confidence"]
        if min_confidence is not None and (confidence is None or confidence < min_confidence):
            continue
        if max_confidence is not None and (confidence is None or confidence > max_confidence):
            continue
        out.append(item)
    return out


def is_call(item: Mapping[str, Any]) -> bool:
    """Whether recording this verdict took a judge call (budget-skipped ones did not)."""
    if "is_call" in item:
        return bool(item["is_call"])
    fallback = item["fallback"]
    if fallback is not None and fallback["reason"] in _NO_ATTEMPT_REASONS:
        return False
    return fallback is not None or item["engine"] not in _NO_CALL_ENGINES


def build_view(
    path: Path,
    *,
    classes: Sequence[str] = (),
    engine: str | None = None,
    min_confidence: float | None = None,
    max_confidence: float | None = None,
    cost_per_call_usd: float | None = None,
) -> dict[str, Any]:
    """The JSON-stable verdict view (``schema`` ``beadhive.backfill.verdicts/1``)."""
    kind, items, structural = load_items(path)
    shown = select(
        items,
        classes=classes,
        engine=engine,
        min_confidence=min_confidence,
        max_confidence=max_confidence,
    )
    ordered = sorted(
        shown,
        key=lambda i: (
            CLASS_ORDER.index(i["class"]),
            -1.0 if i["confidence"] is None else i["confidence"],
            i["query_hash"] or "",
        ),
    )
    groups = {
        cls: [item for item in ordered if item["class"] == cls]
        for cls in CLASS_ORDER
        if any(item["class"] == cls for item in ordered)
    }
    calls = sum(is_call(item) for item in shown)
    cost = None if cost_per_call_usd is None else round(calls * cost_per_call_usd, 6)
    return {
        "schema": SCHEMA,
        "path": str(path),
        "input": kind,
        "filters": {
            "class": list(classes),
            "engine": engine,
            "min_confidence": min_confidence,
            "max_confidence": max_confidence,
        },
        "totals": {
            "recorded": len(items),
            "shown": len(shown),
            "without_verdict": structural,
            "by_class": dict(Counter(i["class"] for i in shown)),
            "by_engine": dict(sorted(Counter(i["engine"] for i in shown).items())),
            "fallbacks": sum(i["fallback"] is not None for i in shown),
        },
        "cost": {
            "calls": calls,
            "cost_per_call_usd": cost_per_call_usd,
            "estimated_usd": cost,
        },
        "groups": groups,
    }


def render_text(view: Mapping[str, Any]) -> list[str]:
    """Human lines for a view, grouped by class."""
    totals = view["totals"]
    lines = [
        f"verdicts: {totals['shown']} shown of {totals['recorded']} recorded "
        f"({view['input']}); {totals['without_verdict']} entries decided without a verdict"
    ]
    if not view["groups"]:
        lines.append("no verdicts recorded" if not totals["recorded"] else "no verdicts match")
    for cls, items in view["groups"].items():
        lines.append(f"{cls} ({len(items)})")
        for item in items:
            confidence = "-" if item["confidence"] is None else f"{item['confidence']:.2f}"
            subject = (
                (item["bead"] or {}).get("id") or item["unit"] or (item["query_hash"] or "?")[:19]
            )
            target = item["match"] or item["covered_by"]
            arrow = f" -> {target['id']}" if target else ""
            engine = item["engine"] + (f"/{item['model']}" if item["model"] else "")
            line = f"  {subject}{arrow}  conf={confidence}  engine={engine}"
            if item["step"]:
                line += f"  step={item['step']}"
            if item["provenance"]:
                line += f"  provenance={item['provenance']}"
            if item["reason"]:
                line += f"  reason={item['reason']}"
            if item["fallback"]:
                fb = item["fallback"]
                line += f"  fallback={fb['reason']}"
            if item["review"] and item["review"] != "none":
                line += f"  review={item['review']}"
            lines.append(line)
    cost = view["cost"]
    if cost["cost_per_call_usd"] is not None:
        lines.append(
            f"cost: {cost['calls']} calls x ${cost['cost_per_call_usd']} = "
            f"${cost['estimated_usd']} (backfill.judge.cost_per_call_usd)"
        )
    return lines
