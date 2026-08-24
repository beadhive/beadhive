"""Characterization matrices for worktree inventory and cleanup extraction."""

from __future__ import annotations

import ast
import inspect
import threading
from types import SimpleNamespace

import pytest

from beadhive import (
    metadata,
    worktree,
    worktree_cleanup,
    worktree_inventory,
    worktree_merge,
    wt_status,
)

INVENTORY_OPERATIONS = (
    "_managed_for_entry",
    "managed",
    "_emit",
    "_worktree_branch",
    "unregistered_worktrees",
    "list_cmd",
    "_classify_entries",
    "_wt_dirty",
    "store_probe_cache",
    "_store_readable",
    "_probe_store",
    "_bead_statuses_for_entry",
    "_classify_entry",
    "_status_tags",
    "_render_status",
    "_warn_untrustworthy",
    "_status_scope",
    "_status_classifications",
    "_ordered_statuses",
    "status_rows",
    "_warn_unregistered",
    "_render_status_multi",
    "status_cmd",
)

CLEANUP_OPERATIONS = (
    "_rmdir_empty_parents",
    "_refuse_unknown_removal",
    "remove",
    "_prune_load_entries",
    "_prune_sweep_orphans",
    "_prune_classify",
    "_prune_withhold_untrustworthy",
    "_prune_report_skipped",
    "_prune_remove_one",
    "_prune_remove_all",
    "prune",
)


@pytest.mark.parametrize(
    ("module", "facade_attr", "operation"),
    [
        *((worktree_inventory, "_worktree_inventory", name) for name in INVENTORY_OPERATIONS),
        *((worktree_cleanup, "_worktree_cleanup", name) for name in CLEANUP_OPERATIONS),
    ],
)
def test_inventory_and_cleanup_have_one_implementation_behind_the_facade(
    module, facade_attr, operation
):
    implementation = getattr(module, f"impl_{operation}")
    facade = getattr(worktree, operation)
    facade_source = inspect.getsource(facade)

    assert implementation.__module__ == module.__name__
    assert f"{facade_attr}.impl_{operation}" in facade_source
    facade_node = ast.parse(facade_source).body[0]
    assert len(facade_node.body) == 2
    assert isinstance(facade_node.body[-1], ast.Return)


def test_related_policy_and_creation_boundaries_remain_owned_by_their_existing_modules():
    for operation in ("add", "ensure", "mark_landed"):
        assert getattr(worktree, operation).__module__ == "beadhive.worktree"
        assert not hasattr(worktree_inventory, operation)
        assert not hasattr(worktree_cleanup, operation)

    assert worktree.wt_status.classify is wt_status.classify
    assert worktree.merge_no_ff is worktree_merge.merge_no_ff


@pytest.mark.parametrize(
    (
        "metadata_rows",
        "bead_state",
        "dirty_paths",
        "expected_branches",
        "expected_statuses",
        "expected_unknown",
        "expected_store_reason",
    ),
    [
        (
            {"github/acme/repo": SimpleNamespace(branches=["main", "topic-b", "topic-a"])},
            ({"a": "closed", "b": "open"}, {"a": "merged", "b": ""}, {}, ""),
            {"/wt/a"},
            ["main", "topic-b", "topic-a"],
            {"a": "closed", "b": "open"},
            {},
            "",
        ),
        (
            {},
            (
                {"a": "closed", "b": ""},
                {"a": "merged", "b": ""},
                {"b": "bead b is missing"},
                "",
            ),
            {"/wt/b"},
            [],
            {"a": "closed", "b": ""},
            {"b": "bead b is missing"},
            "",
        ),
        (
            {},
            ({"a": "", "b": ""}, {"a": "", "b": ""}, {}, "store unavailable"),
            set(),
            [],
            {"a": "", "b": ""},
            {},
            "store unavailable",
        ),
    ],
)
def test_classify_entry_partial_state_outcome_matrix(
    monkeypatch,
    metadata_rows,
    bead_state,
    dirty_paths,
    expected_branches,
    expected_statuses,
    expected_unknown,
    expected_store_reason,
):
    entry = {"prefix": "mr"}
    rows = [
        ("mr", "/wt/a", "wt/bead/issue/a"),
        ("mr", "/wt/b", "wt/bead/issue/b"),
    ]
    captured = {}

    monkeypatch.setattr(worktree.registry, "hive_key", lambda _entry: "github/acme/repo")
    monkeypatch.setattr(metadata, "read_fleet", lambda cfg, keys, ttl: metadata_rows)
    monkeypatch.setattr(worktree.config, "integration_branch", lambda cfg, _entry: "main")
    monkeypatch.setattr(worktree, "_bead_statuses_for_entry", lambda _entry, _rows: bead_state)
    monkeypatch.setattr(worktree, "_wt_dirty", lambda path: path in dirty_paths)
    monkeypatch.setattr(worktree, "is_merged", lambda _entry, branch, base: (branch, base))
    monkeypatch.setattr(
        worktree,
        "bead_and_parent",
        lambda _entry, path, integration, branch="": (path, integration, branch),
    )
    monkeypatch.setattr(
        worktree,
        "is_landed",
        lambda _entry, branch, base, reason: (branch, base, reason),
    )

    def classify(**kwargs):
        captured.update(kwargs)
        captured["merged_result"] = kwargs["is_merged_fn"](None, "topic", "main")
        captured["parent_result"] = kwargs["parent_fn"](None, "/wt/a", "main", "topic")
        captured["landed_result"] = kwargs["is_landed_fn"](None, "topic", "main", "merged")
        return ["classification-result"]

    monkeypatch.setattr(worktree.wt_status, "classify", classify)

    assert worktree._classify_entry(entry, rows, {"cfg": True}) == ["classification-result"]
    assert captured["managed_rows"] == rows
    assert captured["meta_branches"] == expected_branches
    assert captured["bead_statuses"] == expected_statuses
    assert captured["bead_unknown_reasons"] == expected_unknown
    assert captured["store_unreadable_reason"] == expected_store_reason
    assert captured["dirty_by_path"] == {path: path in dirty_paths for _, path, _ in rows}
    assert captured["merged_result"] == ("topic", "main")
    assert captured["parent_result"] == ("/wt/a", "main", "topic")
    assert captured["landed_result"] == ("topic", "main", "merged")


def test_concurrent_classification_streams_completion_order_but_flattens_entry_order(monkeypatch):
    entries = [{"prefix": "first"}, {"prefix": "second"}, {"prefix": "empty"}]
    rows_by_prefix = {
        "first": [("first", "/wt/first", "wt/bead/issue/first")],
        "second": [("second", "/wt/second", "wt/bead/issue/second")],
    }
    release_first = threading.Event()
    first_started = threading.Event()
    completion_order = []

    def classify(entry, _rows, _cfg):
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

    monkeypatch.setattr(worktree, "_classify_entry", classify)

    statuses_by_prefix = worktree._classify_entries(
        {}, entries, rows_by_prefix, on_complete=completed
    )

    assert completion_order == ["second", "first"]
    assert set(statuses_by_prefix) == {"first", "second"}
    assert worktree._ordered_statuses(entries, statuses_by_prefix) == ["first", "second"]
