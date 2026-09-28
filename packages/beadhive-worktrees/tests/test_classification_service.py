"""Concurrency scheduling, batch evidence, and legacy-root flagging (bh-qdezo.7).

Moved from ``tests/test_worktree_inventory_boundaries.py`` (root):
``test_concurrent_classification_streams_completion_order_but_flattens_entry_order`` monkeypatched
away ``worktree._classify_entry`` entirely, so it never touched root's ``store_probe_cache``
wrapping — it was purely exercising the scheduler now living here as
``classify_entries_concurrently``, plus ``ordered_statuses``. Root keeps no equivalent test: the
`store_probe_cache`-per-worker wrapping is proved indirectly by the store-probe-cache tests
already covering that context manager, and root's own ``impl__classify_entries`` is now a
one-line forward with nothing left to characterize on its own.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from beadhive_worktrees import WtStatus
from beadhive_worktrees.application.classification_service import (
    classify_entries_concurrently,
    flag_legacy_root,
    resolve_batch_evidence,
)
from beadhive_worktrees.application.status_presentation import ordered_statuses


def test_concurrent_classification_streams_completion_order_but_flattens_entry_order() -> None:
    entries = [{"prefix": "first"}, {"prefix": "second"}, {"prefix": "empty"}]
    rows_by_prefix = {
        "first": [("first", "/wt/first", "wt/bead/issue/first")],
        "second": [("second", "/wt/second", "wt/bead/issue/second")],
    }
    release_first = threading.Event()
    first_started = threading.Event()
    completion_order: list[str] = []

    def classify_one(entry, _rows):
        if entry["prefix"] == "first":
            first_started.set()
            assert release_first.wait(timeout=2)
        else:
            assert first_started.wait(timeout=2)
        return [entry["prefix"]]

    def completed(prefix, _statuses):
        completion_order.append(prefix)
        if prefix == "second":
            release_first.set()

    statuses_by_prefix = classify_entries_concurrently(
        entries, rows_by_prefix, classify_one, on_complete=completed
    )

    assert completion_order == ["second", "first"]
    assert set(statuses_by_prefix) == {"first", "second"}
    assert ordered_statuses(entries, statuses_by_prefix) == ["first", "second"]


def test_no_populated_hive_returns_empty_without_calling_classify_one() -> None:
    called = []

    def classify_one(entry, rows):
        called.append(entry)
        return []

    result = classify_entries_concurrently([{"prefix": "empty"}], {"empty": []}, classify_one)

    assert result == {}
    assert called == []


def test_single_populated_hive_runs_inline_without_a_thread_pool() -> None:
    entries = [{"prefix": "only"}]
    rows_by_prefix = {"only": [("only", "/wt/only", "wt/bead/issue/only")]}
    seen_thread = []

    def classify_one(entry, rows):
        seen_thread.append(threading.current_thread())
        return ["result"]

    result = classify_entries_concurrently(entries, rows_by_prefix, classify_one)

    assert result == {"only": ["result"]}
    assert seen_thread == [threading.current_thread()]


@pytest.mark.parametrize(
    "issues",
    [
        None,
        [],
        [{"id": "mr-1.1", "status": "closed", "labels": ["batch:g", "batch:x"]}],
        [
            {"id": "mr-1.1", "status": "closed", "labels": ["batch:g"]},
            {"id": "mr-2.1", "status": "closed", "labels": ["batch:g"]},
        ],
    ],
)
def test_resolve_batch_evidence_fails_closed_on_missing_ambiguous_or_mixed_parent_data(
    issues,
) -> None:
    class FakeLookup:
        def all_issues(self, main):
            return issues

    rows = [("mr", "/wt/batch-g", "wt/batch/g")]
    # A mixed/ambiguous parent per bead — every case here must resolve to no evidence.
    parent_by_bead = {"mr-1.1": "wt/bead/epic/mr-1", "mr-2.1": "wt/bead/epic/mr-2"}
    evidence = resolve_batch_evidence(
        {"prefix": "mr"},
        rows,
        "main",
        lookup=FakeLookup(),
        main=Path("/repo"),
        integration_base_fn=lambda _entry, bead, integration: parent_by_bead.get(bead, integration),
    )

    assert evidence == {}


def test_resolve_batch_evidence_omits_groups_without_a_single_shared_parent() -> None:
    class FakeLookup:
        def all_issues(self, main):
            return [
                {"id": "mr-1.1", "status": "closed", "labels": ["batch:g"]},
                {"id": "mr-2.1", "status": "closed", "labels": ["batch:g"]},
            ]

    rows = [("mr", "/wt/batch-g", "wt/batch/g")]
    evidence = resolve_batch_evidence(
        {"prefix": "mr"},
        rows,
        "main",
        lookup=FakeLookup(),
        main=Path("/repo"),
        integration_base_fn=lambda _entry, bead, integration: {
            "mr-1.1": "main",
            "mr-2.1": "other",
        }.get(bead, integration),
    )

    assert evidence == {}


def test_resolve_batch_evidence_resolves_the_one_shared_parent() -> None:
    class FakeLookup:
        def all_issues(self, main):
            return [
                {"id": "mr-1.1", "status": "closed", "labels": ["batch:g"]},
                {"id": "mr-2.1", "status": "closed", "labels": ["batch:g"]},
            ]

    rows = [("mr", "/wt/batch-g", "wt/batch/g")]
    evidence = resolve_batch_evidence(
        {"prefix": "mr"},
        rows,
        "main",
        lookup=FakeLookup(),
        main=Path("/repo"),
        integration_base_fn=lambda _entry, _bead, integration: integration,
    )

    assert set(evidence) == {"wt/batch/g"}
    assert evidence["wt/batch/g"].parent == "main"
    assert evidence["wt/batch/g"].all_closed is True


def test_flag_legacy_root_marks_only_rows_outside_the_active_root(tmp_path) -> None:
    active_root = tmp_path / "active"
    active_root.mkdir()
    inside = WtStatus(
        hive="h",
        leaf="a",
        branch="wt/bead/issue/a",
        path=str(active_root / "a"),
        bead_id="a",
        classification="active",
        merged=False,
        dirty=False,
        safe=False,
    )
    outside = WtStatus(
        hive="h",
        leaf="b",
        branch="wt/bead/issue/b",
        path=str(tmp_path / "elsewhere" / "b"),
        bead_id="b",
        classification="active",
        merged=False,
        dirty=False,
        safe=False,
    )

    flagged = flag_legacy_root([inside, outside], active_root)

    assert [s.legacy_root for s in flagged] == [False, True]


@pytest.mark.parametrize("value", ["not-a-status"])
def test_flag_legacy_root_leaves_non_status_values_untouched(value) -> None:
    assert flag_legacy_root([value], Path("/anything")) == [value]
