"""Characterization matrices for worktree inventory and cleanup extraction."""

from __future__ import annotations

import ast
import inspect
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from beadhive import (
    metadata,
    registry,
    worktree,
    worktree_cleanup,
    worktree_inventory,
    worktree_merge,
    worktree_state_adapters,
    wt_status,
)
from beadhive_worktrees.testing import InMemoryBeadStateLookup

_MAIN = Path("/repo")


def _use_store(monkeypatch, lookup: InMemoryBeadStateLookup) -> InMemoryBeadStateLookup:
    """Substitute the hive's bead store at the `BeadStateLookup` port (bh-qdezo.9) and pin the
    registry's main-clone lookup, rather than patching the facade's private bead-state helpers."""
    monkeypatch.setattr(registry, "hive_dir", lambda _entry: _MAIN)
    monkeypatch.setattr(worktree_state_adapters, "BEAD_STATE_LOOKUP", lookup)
    return lookup


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
    "_bead_disposition_relations_for_entry",
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
    for operation in ("add", "ensure", "mark_landed", "mark_abandoned"):
        assert getattr(worktree, operation).__module__ == "beadhive.worktree"
        assert not hasattr(worktree_inventory, operation)
        assert not hasattr(worktree_cleanup, operation)

    assert worktree.wt_status.classify is wt_status.classify
    assert worktree.merge_no_ff is worktree_merge.merge_no_ff


_CLOSED = {"status": "closed", "close_reason": "merged"}


@pytest.mark.parametrize(
    (
        "metadata_rows",
        "store",
        "dirty_paths",
        "expected_branches",
        "expected_statuses",
        "expected_close_reasons",
        "expected_unknown",
        "store_unreadable",
    ),
    [
        (
            {"github/acme/repo": SimpleNamespace(branches=["main", "topic-b", "topic-a"])},
            InMemoryBeadStateLookup({"a": _CLOSED, "b": {"status": "open"}}),
            {"/wt/a"},
            ["main", "topic-b", "topic-a"],
            {"a": "closed", "b": "open"},
            {"a": "merged", "b": ""},
            set(),
            False,
        ),
        (
            {},
            InMemoryBeadStateLookup({"a": _CLOSED}),
            {"/wt/b"},
            [],
            {"a": "closed", "b": ""},
            {"a": "merged", "b": ""},
            {"b"},
            False,
        ),
        (
            {},
            InMemoryBeadStateLookup({"a": _CLOSED}, readable=False),
            set(),
            [],
            {"a": "", "b": ""},
            {"a": "", "b": ""},
            set(),
            True,
        ),
    ],
)
def test_classify_entry_partial_state_outcome_matrix(
    monkeypatch,
    metadata_rows,
    store,
    dirty_paths,
    expected_branches,
    expected_statuses,
    expected_close_reasons,
    expected_unknown,
    store_unreadable,
):
    entry = {"prefix": "mr"}
    rows = [
        ("mr", "/wt/a", "wt/bead/issue/a"),
        ("mr", "/wt/b", "wt/bead/issue/b"),
    ]
    captured = {}
    fleet_reads = []

    def read_fleet(cfg, keys, ttl):
        fleet_reads.append((cfg, keys, ttl))
        return metadata_rows

    monkeypatch.setattr(worktree.registry, "hive_key", lambda _entry: "github/acme/repo")
    monkeypatch.setattr(metadata, "read_fleet", read_fleet)
    monkeypatch.setattr(worktree.config, "integration_branch", lambda cfg, _entry: "main")
    _use_store(monkeypatch, store)
    monkeypatch.setattr(worktree, "_wt_dirty", lambda path: path in dirty_paths)
    monkeypatch.setattr(worktree.config, "precious_globs", lambda _cfg, _entry: [".secret"])
    monkeypatch.setattr(worktree.config, "junk_globs", lambda _cfg, _entry: ["cache/**"])
    monkeypatch.setattr(worktree.config, "precious_min_bytes", lambda _cfg, _entry: 42)
    scan_calls = []

    def scan_precious(path, **kwargs):
        scan_calls.append((path, kwargs))
        return []

    monkeypatch.setattr(worktree.precious, "scan_precious", scan_precious)
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
    assert fleet_reads == [({"cfg": True}, ["github/acme/repo"], 0)]
    assert captured["hive_prefix"] == "mr"
    assert captured["integration"] == "main"
    assert captured["managed_rows"] == rows
    assert captured["meta_branches"] == expected_branches
    assert captured["bead_statuses"] == expected_statuses
    assert captured["bead_close_reasons"] == expected_close_reasons
    assert set(captured["bead_unknown_reasons"]) == expected_unknown
    assert all(captured["bead_unknown_reasons"].values())
    assert bool(captured["store_unreadable_reason"]) is store_unreadable
    assert captured["bead_disposition_relations"] == {}
    # The store is read at the port: one readability probe, then one show per bead id.
    assert store.probes == [_MAIN]
    assert [bead_id for bead_id, _main in store.shows] == ["a", "b"]
    assert captured["dirty_by_path"] == {path: path in dirty_paths for _, path, _ in rows}
    assert captured["precious_by_path"] == {path: [] for _, path, _ in rows}
    assert scan_calls == [
        (
            path,
            {
                "precious_globs": [".secret"],
                "junk_globs": ["cache/**"],
                "min_bytes": 42,
            },
        )
        for _, path, _ in rows
    ]
    assert captured["merged_result"] == ("topic", "main")
    assert captured["parent_result"] == ("/wt/a", "main", "topic")
    assert captured["landed_result"] == ("topic", "main", "merged")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def test_classify_entry_scans_real_configured_ignored_content(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text(".env\n")
    _git(repo, "add", ".gitignore")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "test: ignore environment",
    )
    (repo / ".env").write_text("TOKEN=secret\n")
    entry = {"prefix": "mr"}
    rows = [("mr", str(repo), "wt/bead/issue/a")]
    cfg = {
        "work": {
            "precious_globs": [".env"],
            "junk_globs": [],
            "precious_min_bytes": 1024,
        }
    }

    monkeypatch.setattr(worktree.registry, "hive_key", lambda _entry: "github/acme/repo")
    monkeypatch.setattr(metadata, "read_fleet", lambda _cfg, _keys, ttl: {})
    monkeypatch.setattr(worktree.config, "integration_branch", lambda _cfg, _entry: "main")
    _use_store(monkeypatch, InMemoryBeadStateLookup({"a": _CLOSED}))
    monkeypatch.setattr(worktree, "_wt_dirty", lambda _path: False)
    monkeypatch.setattr(worktree, "is_merged", lambda _entry, _branch, _base: True)
    monkeypatch.setattr(
        worktree,
        "bead_and_parent",
        lambda _entry, _path, integration, branch="": ("a", integration),
    )

    [status] = worktree._classify_entry(entry, rows, cfg)

    assert status.classification is wt_status.WtClassification.HELD
    assert status.underlying is wt_status.WtClassification.SAFE
    assert status.safe is False
    assert status.precious == (worktree.precious.PreciousFile(".env", 13, "precious", ".env"),)


def test_classify_entry_propagates_precious_scan_failure_before_classification(monkeypatch):
    entry = {"prefix": "mr"}
    rows = [("mr", "/not-a-repository", "wt/bead/issue/a")]
    error = subprocess.CalledProcessError(128, ["git", "status"])

    monkeypatch.setattr(worktree.registry, "hive_key", lambda _entry: "github/acme/repo")
    monkeypatch.setattr(metadata, "read_fleet", lambda _cfg, _keys, ttl: {})
    monkeypatch.setattr(worktree.config, "integration_branch", lambda _cfg, _entry: "main")
    _use_store(monkeypatch, InMemoryBeadStateLookup({"a": _CLOSED}))
    monkeypatch.setattr(worktree, "_wt_dirty", lambda _path: False)
    monkeypatch.setattr(
        worktree.precious, "scan_precious", lambda *_args, **_kwargs: (_ for _ in ()).throw(error)
    )
    monkeypatch.setattr(
        worktree.wt_status,
        "classify",
        lambda **_kwargs: pytest.fail(
            "classification must not run after an incomplete safety scan"
        ),
    )

    with pytest.raises(subprocess.CalledProcessError) as caught:
        worktree._classify_entry(entry, rows, {})

    assert caught.value is error


def test_disposition_relation_readback_normalizes_exact_storage_directions(monkeypatch):
    entry = {"prefix": "mr"}
    reasons = {
        "old-retained": wt_status.format_disposition("retained", "pivot", "consumer"),
        "old-superseded": wt_status.format_disposition("superseded", "superseded", "replacement"),
    }
    records = {
        "old-retained": {"dependencies": [{"depends_on_id": "consumer", "type": "relates-to"}]},
        "replacement": {
            "dependencies": [{"depends_on_id": "old-superseded", "type": "supersedes"}]
        },
    }
    store = _use_store(monkeypatch, InMemoryBeadStateLookup(records))

    result = worktree._bead_disposition_relations_for_entry(entry, reasons)

    assert result == {
        "old-retained": frozenset({("retained", "consumer")}),
        "old-superseded": frozenset({("superseded", "replacement")}),
    }
    assert store.shows == [("old-retained", _MAIN), ("replacement", _MAIN)]


def test_batch_evidence_uses_one_complete_label_snapshot_and_shared_member_parent(monkeypatch):
    entry = {"prefix": "mr"}
    rows = [("mr", "/wt/batch-g", "wt/batch/g")]
    store = _use_store(
        monkeypatch,
        InMemoryBeadStateLookup(
            {
                "mr-1.2": {"status": "closed", "labels": ["batch:g"]},
                "mr-1.1": {"status": "closed", "labels": ["batch:g", "size:s"]},
                "mr-2.1": {"status": "open", "labels": ["batch:other"]},
            }
        ),
    )
    monkeypatch.setattr(
        worktree,
        "integration_base",
        lambda _entry, bead, integration: "wt/bead/epic/mr-1",
    )

    evidence = worktree_inventory._batch_evidence_for_entry(entry, rows, "main")

    assert store.listings == [_MAIN]  # one complete label snapshot for the whole hive
    assert evidence == {
        "wt/batch/g": wt_status.BatchEvidence(
            member_statuses=(("mr-1.1", "closed"), ("mr-1.2", "closed")),
            parent="wt/bead/epic/mr-1",
        )
    }


# The remaining fail-closed edge cases (None/empty/ambiguous-labels/mixed-parent issues) and
# the concurrent-classification scheduling test both moved to
# packages/beadhive-worktrees/tests/test_classification_service.py (bh-qdezo.7): they exercised
# beadhive_worktrees.resolve_batch_evidence / classify_entries_concurrently / ordered_statuses
# on fakes with no root-specific behavior left to characterize here. The happy-path test above
# stays as the one root wiring proof (registry.hive_dir + the composed BeadStateLookup port +
# the integration_base callable seam); the argv adapter's exact bd.json shape is proven once,
# directly, in tests/test_worktree_state_adapters.py (bh-qdezo.9).
