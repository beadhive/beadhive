"""Complexity as pass #1 on the engine, against a stand-in for root's ``ComplexityRules``.

The published JSON-plan contract is pinned by root ``tests/test_complexity_backfill.py`` (the
real classifier); these tests pin the pass itself: the engine-native plan agrees with the JSON
plan, recorded targets are never re-classified at apply time, and the JSON audit is rendered
from the engine's checkpoints.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

import pytest

from beadhive_backfill import BackfillEngine, BackfillError
from beadhive_backfill.complexity import (
    ROW_GROUP,
    Classification,
    ComplexityPass,
    apply_plan,
    build_plan,
    verify_plan,
)
from beadlog import Group
from beadlog_sources import baseline_events

TIERS = ("SIMPLE", "MEDIUM", "COMPLEX", "REASONING")


class Rules:
    """Tier ``REASONING`` for titles mentioning "prove", ``SIMPLE`` for "typo", else a
    MEDIUM fallback; ``calls`` counts classifications."""

    label_prefix = "complexity:"
    model_prefix = "model:"
    source = "test/rules"
    version = "1"

    def __init__(self) -> None:
        self.calls = 0
        self.broken = False

    def parse_label(self, label: str) -> str:
        tier = label.removeprefix(self.label_prefix)
        if not label.startswith(self.label_prefix) or tier not in TIERS:
            raise ValueError(label)
        return tier

    def label(self, tier: str) -> str:
        return f"{self.label_prefix}{tier}"

    def routable_type(self, issue_type: str) -> bool:
        return issue_type in {"task", "bug", "feature", "chore", "epic"}

    def valid_model(self, value: str) -> bool:
        return value.count("/") == 1

    def classify(self, bead: Mapping[str, Any]) -> Classification:
        if self.broken:
            raise AssertionError("apply must not re-classify a recorded target")
        self.calls += 1
        title = str(bead.get("title") or "").lower()
        tier = "REASONING" if "prove" in title else "SIMPLE" if "typo" in title else "MEDIUM"
        return Classification(tier, 0.5, self.source, self.version, tier == "MEDIUM")


def row(iid: str, title: str = "Refactor the loader", **extra: Any) -> dict[str, Any]:
    return {
        "id": iid,
        "issue_type": "task",
        "status": "open",
        "title": title,
        "labels": [],
        "created_at": "2026-07-01T00:00:00Z",
        "updated_at": "2026-07-01T00:00:00Z",
        **extra,
    }


CORPUS = [
    row("c-1", "Prove the fold is associative"),
    row("c-2", "Fix a typo", labels=["complexity:REASONING", "complexity:bogus"]),
    row("c-3", labels=["complexity:COMPLEX", "model:legacy"]),
    row("c-gate", issue_type="gate"),
    row("c-slot", labels=["gt:slot"]),
]


class Clock:
    def __init__(self) -> None:
        self.ticks = 0

    def __call__(self) -> str:
        self.ticks += 1
        return f"2026-10-31T00:00:{self.ticks:02d}Z"


class RowStore:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = copy.deepcopy(rows)
        self.exported = False
        self.calls: list[tuple[str, str, str]] = []

    def load(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.rows)

    def export(self) -> None:
        self.exported = True

    def mutate(self, operation: str, iid: str, label: str) -> None:
        assert self.exported
        self.calls.append((operation, iid, label))
        labels = next(item for item in self.rows if item["id"] == iid)["labels"]
        if operation == "remove":
            labels.remove(label)
        elif label not in labels:
            labels.append(label)


def test_the_engine_plan_agrees_with_the_json_plan() -> None:
    rules = Rules()
    reader_rows = baseline_events(CORPUS, group=ROW_GROUP, extracted_at="2026-10-31T00:00:00Z")

    class Reader:
        def export(self):
            return reader_rows

    engine_plan = BackfillEngine(groups=[Group(ROW_GROUP, "rows")], clock=Clock()).plan(
        ComplexityPass(rules), Reader()
    )
    json_plan = build_plan(CORPUS, rules)

    assert engine_plan.changes == json_plan["totals"]["changes"] == 2
    for key in ("by_tier", "by_status", "existing_complexity", "unknown_to_medium_fallbacks"):
        assert engine_plan.report[key] == json_plan[key], key
    assert engine_plan.totals["excluded"] == json_plan["totals"]["excluded"] == 2
    assert engine_plan.engine["excluded_by_reason"] == json_plan["excluded_by_reason"]
    labels = {
        (event.bead.id, str(event.kind), event.data["label"])
        for event in engine_plan.applicable_events()
    }
    assert labels == {
        ("c-1", "label_add", "complexity:REASONING"),
        ("c-2", "label_remove", "complexity:REASONING"),
        ("c-2", "label_remove", "complexity:bogus"),
        ("c-2", "label_add", "complexity:SIMPLE"),
    }


def test_apply_uses_recorded_targets_and_renders_the_json_audit(tmp_path) -> None:
    rules = Rules()
    store = RowStore(CORPUS)
    plan = build_plan(store.load(), rules)
    verify_plan(plan)
    rules.broken = True  # every target is now a recorded decision

    audit = apply_plan(
        plan,
        rules=rules,
        load_records=store.load,
        mutate=store.mutate,
        export_pre_state=store.export,
        audit_path=tmp_path / "audit.json",
        pre_state_artifact="recovery.jsonl",
        clock=Clock(),
    )

    assert audit["state"] == "applied"
    assert audit["planned_updates"] == audit["completed"] == ["c-1", "c-2"]
    assert audit["progress"] == {
        "planned_count": 2,
        "attempted_count": 2,
        "completed_count": 2,
        "current_bead": None,
        "next_index": 2,
    }
    assert audit["recovery"]["pre_state_artifact"] == "recovery.jsonl"
    assert audit["second_dry_run_changes"] == 0
    assert json.loads((tmp_path / "audit.json").read_text()) == audit
    # Removals precede the target, one label per write; model and system labels are untouched.
    assert store.calls[0] == ("add", "c-1", "complexity:REASONING")
    assert set(store.calls[1:3]) == {
        ("remove", "c-2", "complexity:REASONING"),
        ("remove", "c-2", "complexity:bogus"),
    }
    assert store.calls[3:] == [("add", "c-2", "complexity:SIMPLE")]
    rules.broken = False
    assert build_plan(store.load(), rules)["totals"]["changes"] == 0


def test_a_failed_write_rolls_back_every_touched_bead(tmp_path) -> None:
    rules = Rules()
    store = RowStore(CORPUS)
    plan = build_plan(store.load(), rules)
    original = store.load()
    healthy = store.mutate

    def mutate(operation: str, iid: str, label: str) -> None:
        if (operation, iid, label) == ("add", "c-2", "complexity:SIMPLE"):
            raise RuntimeError("injected")
        healthy(operation, iid, label)

    with pytest.raises(BackfillError, match="apply failed and was audited"):
        apply_plan(
            plan,
            rules=rules,
            load_records=store.load,
            mutate=mutate,
            export_pre_state=store.export,
            audit_path=tmp_path / "audit.json",
            clock=Clock(),
        )

    assert [sorted(item["labels"]) for item in store.load()] == [
        sorted(item["labels"]) for item in original
    ]
    audit = json.loads((tmp_path / "audit.json").read_text())
    assert audit["state"] == "rolled_back"
    assert audit["recovery"]["rollback_complete"] is True
    assert "injected" in audit["error"]
