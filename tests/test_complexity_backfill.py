"""Full-corpus complexity migration planning, safety, and convergence."""

from __future__ import annotations

import copy
import json

import pytest

from beadhive import complexity_backfill


def _row(
    iid: str,
    *,
    issue_type: str = "task",
    status: str = "open",
    title: str = "Implement a Python API and unit test",
    labels: list[str] | None = None,
) -> dict:
    return {
        "id": iid,
        "issue_type": issue_type,
        "status": status,
        "title": title,
        "description": "",
        "design": "",
        "acceptance_criteria": "",
        "labels": list(labels or []),
    }


class Store:
    def __init__(self, rows: list[dict]):
        self.rows = copy.deepcopy(rows)
        self.exported = False
        self.calls: list[tuple[str, str, str]] = []
        self.fail: tuple[str, str] | None = None
        self.interrupt: tuple[str, str] | None = None

    def load(self) -> list[dict]:
        return copy.deepcopy(self.rows)

    def export(self) -> None:
        self.exported = True

    def mutate(self, operation: str, iid: str, label: str) -> None:
        assert self.exported, "the recoverable export must precede every mutation"
        self.calls.append((operation, iid, label))
        if self.interrupt == (iid, label):
            self.interrupt = None
            raise KeyboardInterrupt
        if self.fail == (iid, label):
            self.fail = None
            raise RuntimeError("injected write failure")
        row = next(item for item in self.rows if item["id"] == iid)
        if operation == "remove":
            row["labels"] = [value for value in row["labels"] if value != label]
        elif operation == "add" and label not in row["labels"]:
            row["labels"].append(label)


def test_plan_covers_history_epics_fallbacks_and_counts_exclusions():
    rows = [
        _row("h-closed", status="closed", title="Polish wording"),
        _row("h-epic", issue_type="epic", status="in_progress"),
        _row("h-blocked", issue_type="bug", status="blocked"),
        _row("h-deferred", issue_type="chore", status="deferred"),
        _row("h-gate", issue_type="gate", labels=["model:opus"]),
        _row("h-event", issue_type="event"),
        _row("h-internal", issue_type="molecule"),
        _row("h-unknown-status", status="tombstoned"),
    ]

    plan = complexity_backfill.build_plan(rows)

    assert plan["totals"] == {"records": 8, "in_scope": 4, "excluded": 4, "changes": 4}
    assert plan["by_status"] == {"blocked": 1, "closed": 1, "deferred": 1, "in_progress": 1}
    assert plan["by_type"] == {"bug": 1, "chore": 1, "epic": 1, "task": 1}
    assert plan["unknown_to_medium_fallbacks"] == ["h-closed"]
    assert plan["excluded_by_reason"] == {
        "non_routable_status:tombstoned": 1,
        "non_routable_type:event": 1,
        "non_routable_type:gate": 1,
        "non_routable_type:molecule": 1,
    }


def test_merge_slot_is_counted_but_never_becomes_a_migration_target():
    title = "Reserved coordination record"
    merge_slot = _row("bh-merge-slot", title=title, labels=["gt:slot"])
    ordinary = _row("h-ordinary", title=title)

    plan = complexity_backfill.build_plan([merge_slot, ordinary])
    repeated = complexity_backfill.build_plan([merge_slot, ordinary])

    assert plan == repeated
    assert plan["totals"] == {"records": 2, "in_scope": 1, "excluded": 1, "changes": 1}
    assert plan["excluded_by_reason"] == {"system_artifact:gt:slot": 1}
    assert [entry["id"] for entry in plan["entries"]] == ["h-ordinary"]
    assert plan["unknown_to_medium_fallbacks"] == ["h-ordinary"]
    assert complexity_backfill.corpus_hash([merge_slot, ordinary]) == (
        complexity_backfill.corpus_hash([ordinary])
    )

    target = plan["entries"][0]["target_label"]
    converged = _row("h-ordinary", title=title, labels=[target])
    second_dry_run = complexity_backfill.build_plan([merge_slot, converged])
    assert second_dry_run["totals"]["changes"] == 0
    assert [entry["id"] for entry in second_dry_run["entries"]] == ["h-ordinary"]


def test_merge_slot_never_enters_apply_checkpoints_or_verification(tmp_path):
    merge_slot = _row("bh-merge-slot", title="Reserved coordination record", labels=["gt:slot"])
    ordinary = _row("h-ordinary", title="Reserved coordination record")
    store = Store([merge_slot, ordinary])
    plan = complexity_backfill.build_plan(store.load())

    audit = complexity_backfill.apply_plan(
        plan,
        load_records=store.load,
        mutate=store.mutate,
        export_pre_state=store.export,
        audit_path=tmp_path / "audit.json",
    )

    assert audit["planned_updates"] == ["h-ordinary"]
    assert audit["attempted"] == ["h-ordinary"]
    assert audit["completed"] == ["h-ordinary"]
    assert all(iid == "h-ordinary" for _operation, iid, _label in store.calls)
    slot_after = next(row for row in store.load() if row["id"] == "bh-merge-slot")
    assert slot_after["labels"] == ["gt:slot"]
    assert complexity_backfill._complexity_errors(store.load()) == []
    assert audit["second_dry_run_changes"] == 0


def test_plan_repairs_bad_complexity_but_preserves_and_reports_legacy_model_history():
    rows = [
        _row(
            "h-malformed",
            title="What is a webhook?",
            labels=["complexity:medium", "complexity:REASONING", "model:sonnet"],
        ),
        _row(
            "h-existing",
            title="What is a webhook?",
            labels=["complexity:REASONING", "model:anthropic/claude-opus-4-1"],
        ),
    ]

    plan = complexity_backfill.build_plan(rows)
    malformed = next(item for item in plan["entries"] if item["id"] == "h-malformed")
    existing = next(item for item in plan["entries"] if item["id"] == "h-existing")

    assert malformed["changes"] is True
    assert malformed["target_label"].startswith("complexity:")
    assert plan["existing_complexity"] == {"duplicate": 1, "valid": 1}
    assert plan["duplicate_or_invalid_complexity"] == [
        {
            "id": "h-malformed",
            "labels": ["complexity:medium", "complexity:REASONING"],
        }
    ]
    legacy = next(item for item in plan["preserved_model_hints"] if item["id"] == "h-malformed")
    assert legacy["labels"] == ["model:sonnet"]
    assert legacy["structurally_invalid"] == ["model:sonnet"]
    assert existing["target_label"] == "complexity:REASONING"
    assert existing["changes"] is False
    assert existing["provenance"] == "existing"
    assert any(item["id"] == "h-malformed" for item in plan["model_score_disagreements"])


def test_plan_hash_detects_edits_and_apply_refuses_concurrent_corpus_drift(tmp_path):
    rows = [_row("h-1")]
    plan = complexity_backfill.build_plan(rows)
    edited = copy.deepcopy(plan)
    edited["entries"][0]["target_label"] = "complexity:REASONING"
    with pytest.raises(complexity_backfill.BackfillError, match="plan hash mismatch"):
        complexity_backfill.verify_plan(edited)

    store = Store(rows + [_row("h-concurrent")])
    with pytest.raises(complexity_backfill.BackfillError, match="corpus changed after planning"):
        complexity_backfill.apply_plan(
            plan,
            load_records=store.load,
            mutate=store.mutate,
            export_pre_state=store.export,
            audit_path=tmp_path / "audit.json",
        )
    assert not store.exported
    assert store.calls == []


def test_apply_exports_first_converges_validates_models_and_is_idempotent(tmp_path):
    original = [
        _row("h-1", labels=["model:sonnet"]),
        _row("h-2", status="closed", labels=["complexity:bogus", "model:opus"]),
        _row("h-gate", issue_type="gate", labels=["model:haiku"]),
    ]
    store = Store(original)
    plan = complexity_backfill.build_plan(store.load())
    audit_path = tmp_path / "audit.json"

    audit = complexity_backfill.apply_plan(
        plan,
        load_records=store.load,
        mutate=store.mutate,
        export_pre_state=store.export,
        audit_path=audit_path,
    )

    assert audit["state"] == "applied"
    assert audit["post_apply_complexity_errors"] == []
    assert audit["model_labels_preserved"] is True
    assert audit["second_dry_run_changes"] == 0
    assert json.loads(audit_path.read_text()) == audit
    assert complexity_backfill.build_plan(store.load())["totals"]["changes"] == 0
    assert {
        row["id"]: [label for label in row["labels"] if label.startswith("model:")]
        for row in store.load()
    } == {
        "h-1": ["model:sonnet"],
        "h-2": ["model:opus"],
        "h-gate": ["model:haiku"],
    }
    assert not any(label.startswith("model:") for _, _, label in store.calls)


def test_applying_audit_precedes_first_mutation_and_checkpoints_progress(tmp_path):
    store = Store([_row("h-1"), _row("h-2")])
    plan = complexity_backfill.build_plan(store.load())
    audit_path = tmp_path / "audit.json"
    snapshots: list[dict] = []

    def inspect_then_mutate(operation: str, iid: str, label: str) -> None:
        snapshot = json.loads(audit_path.read_text())
        snapshots.append(snapshot)
        store.mutate(operation, iid, label)

    complexity_backfill.apply_plan(
        plan,
        load_records=store.load,
        mutate=inspect_then_mutate,
        export_pre_state=store.export,
        audit_path=audit_path,
        pre_state_artifact="recovery/issues.jsonl",
    )

    before_first_write = snapshots[0]
    assert before_first_write["state"] == "applying"
    assert before_first_write["pre_state_artifact"] == "recovery/issues.jsonl"
    assert before_first_write["planned_updates"] == ["h-1", "h-2"]
    assert before_first_write["attempted"] == ["h-1"]
    assert before_first_write["completed"] == []
    assert before_first_write["progress"] == {
        "planned_count": 2,
        "attempted_count": 1,
        "completed_count": 0,
        "current_bead": "h-1",
        "next_index": 0,
    }
    assert before_first_write["recovery"]["uncertain_bead"] == "h-1"

    before_second_write = snapshots[1]
    assert before_second_write["attempted"] == ["h-1", "h-2"]
    assert before_second_write["completed"] == ["h-1"]
    assert before_second_write["progress"]["completed_count"] == 1
    assert before_second_write["progress"]["current_bead"] == "h-2"
    assert before_second_write["progress"]["next_index"] == 1
    assert before_second_write["recovery"]["uncertain_bead"] == "h-2"


def test_apply_detects_drift_during_write_window_and_rolls_back(tmp_path):
    original = [_row("h-1")]
    store = Store(original)
    plan = complexity_backfill.build_plan(store.load())
    loads = 0

    def load_with_concurrent_change():
        nonlocal loads
        loads += 1
        rows = store.load()
        if loads == 2:
            rows.append(_row("h-concurrent-task"))
        return rows

    audit_path = tmp_path / "audit.json"
    with pytest.raises(complexity_backfill.BackfillError, match="post-apply verification failed"):
        complexity_backfill.apply_plan(
            plan,
            load_records=load_with_concurrent_change,
            mutate=store.mutate,
            export_pre_state=store.export,
            audit_path=audit_path,
        )

    assert store.load() == original
    audit = json.loads(audit_path.read_text())
    assert audit["state"] == "rolled_back"
    assert "unexpected corpus drift during apply" in audit["error"]


@pytest.mark.parametrize("interrupted", [False, True])
def test_failed_or_interrupted_apply_rolls_back_and_audits(tmp_path, interrupted):
    original = [_row("h-1"), _row("h-2", labels=["complexity:bad", "model:sonnet"])]
    store = Store(original)
    plan = complexity_backfill.build_plan(store.load())
    failing = next(entry for entry in plan["entries"] if entry["id"] == "h-2")
    marker = ("h-2", failing["target_label"])
    if interrupted:
        store.interrupt = marker
    else:
        store.fail = marker
    audit_path = tmp_path / "audit.json"

    expected = KeyboardInterrupt if interrupted else complexity_backfill.BackfillError
    with pytest.raises(expected):
        complexity_backfill.apply_plan(
            plan,
            load_records=store.load,
            mutate=store.mutate,
            export_pre_state=store.export,
            audit_path=audit_path,
        )

    restored = store.load()
    assert [{**row, "labels": sorted(row["labels"])} for row in restored] == [
        {**row, "labels": sorted(row["labels"])} for row in original
    ]
    audit = json.loads(audit_path.read_text())
    assert audit["state"] == "rolled_back"
    assert audit["rollback_failures"] == []


def test_post_apply_validation_ignores_legacy_model_shape_by_design(tmp_path):
    store = Store([_row("h-1", labels=["model:slashless-legacy"])])
    plan = complexity_backfill.build_plan(store.load())

    audit = complexity_backfill.apply_plan(
        plan,
        load_records=store.load,
        mutate=store.mutate,
        export_pre_state=store.export,
        audit_path=tmp_path / "audit.json",
    )

    assert audit["state"] == "applied"
    assert "model:slashless-legacy" in store.load()[0]["labels"]
