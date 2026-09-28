"""Orchestration around the pure worktree safety classifier (bh-qdezo.7).

Moved from ``worktree_inventory.py``'s ``impl__classify_entries`` (concurrent per-hive
scheduling), ``_batch_evidence_for_entry`` (batch-label lifecycle evidence), and the tail of
``impl__classify_entry`` (the ``legacy_root`` post-processing that used to run separately, right
after calling ``beadhive_worktrees.policy.classification.classify``).

This module never resolves bead ids from branch names, hive registry paths, or fetches
metadata/config/git/precious facts itself — those stay root-shaped I/O per bh-qdezo.5's own
scope note (naming policy and registry lookups are root concerns even for the classifier's own
bead-state assembly). It also never calls ``classify()`` itself: ``beadhive.worktree`` is the
established "stable facade and collaborator patch boundary" for that call (its own module
docstring, and existing tests patch the classifier there), so root keeps calling
``wt_status.classify(...)`` directly and only reaches into this module for the surrounding
scheduling and post-processing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..contracts.state_ports import BeadStateLookup
from ..policy.classification import BatchEvidence, WtStatus

#: Historical default worker cap for concurrent per-hive classification (unchanged from
#: ``worktree.py``'s ``_CLASSIFY_MAX_WORKERS``; root still owns the actual constant value and
#: passes it in, so a config-driven override stays a root concern).
DEFAULT_MAX_WORKERS = 8


def classify_entries_concurrently(
    entries: Sequence[Any],
    rows_by_prefix: dict[str, list[tuple[str, str, str]]],
    classify_one: Callable[[Any, list[tuple[str, str, str]]], list[WtStatus]],
    *,
    on_complete: Callable[[str, list[WtStatus]], None] | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict[str, list[WtStatus]]:
    """Classify populated hives concurrently, optionally reporting each completed hive.

    Every classification has independent git and bead-store I/O, so waiting for one hive before
    starting the next only delays the fleet view. ``classify_one`` is root-supplied and owns its
    own per-worker cache scoping (a ``store_probe_cache``-shaped context manager, entered once
    per worker since a memo shared across threads would break the one-probe-per-hive contract).

    Results are keyed rather than appended in completion order. Callers that return structured
    data can then retain their established deterministic entry ordering, while a human status
    renderer uses ``on_complete`` to show a hive as soon as it is ready.

    A single populated hive runs inline (no thread pool) — the exact fast path
    ``worktree_inventory.py``'s ``impl__classify_entries`` used, preserved so a one-hive command
    never pays thread-pool overhead or reorders its own single result.
    """
    jobs = [
        (str(entry.get("prefix", "")), entry, rows_by_prefix.get(str(entry.get("prefix", "")), []))
        for entry in entries
        if rows_by_prefix.get(str(entry.get("prefix", "")), [])
    ]
    if not jobs:
        return {}

    statuses_by_prefix: dict[str, list[WtStatus]] = {}
    if len(jobs) == 1:
        prefix, entry, entry_rows = jobs[0]
        statuses = classify_one(entry, entry_rows)
        statuses_by_prefix[prefix] = statuses
        if on_complete is not None:
            on_complete(prefix, statuses)
        return statuses_by_prefix

    with ThreadPoolExecutor(
        max_workers=min(max_workers, len(jobs)),
        thread_name_prefix="bh-worktree-classify",
    ) as executor:
        futures = {
            executor.submit(classify_one, entry, entry_rows): prefix
            for prefix, entry, entry_rows in jobs
        }
        for future in as_completed(futures):
            prefix = futures[future]
            statuses = future.result()
            statuses_by_prefix[prefix] = statuses
            if on_complete is not None:
                on_complete(prefix, statuses)

    return statuses_by_prefix


def resolve_batch_evidence(
    entry: Any,
    rows: list[tuple[str, str, str]],
    integration: str,
    *,
    lookup: BeadStateLookup,
    main: Path,
    integration_base_fn: Callable[[Any, str, str], str],
) -> dict[str, BatchEvidence]:
    """Resolve exact batch-label membership and one shared parent from one bounded snapshot.

    Batch branches deliberately have no bead id. Their lifecycle authority is instead the set of
    issues carrying the exact ``batch:<group>`` label. Any unreadable or contradictory shape is
    omitted so the pure classifier keeps the worktree ABANDONED rather than making a pruning
    decision from partial evidence.

    ``integration_base_fn`` stays root-supplied: resolving one bead's integration base walks
    branch/history mechanics that are explicitly out of this package's scope (bh-7oo93.8).
    """
    branches = tuple(
        dict.fromkeys(
            branch
            for _prefix, _path, branch in rows
            if branch.startswith("wt/batch/") and branch.removeprefix("wt/batch/")
        )
    )
    if not branches:
        return {}

    issues = lookup.all_issues(main)
    if not isinstance(issues, list):
        return {}

    requested = {branch.removeprefix("wt/batch/"): branch for branch in branches}
    members: dict[str, dict[str, str]] = {group: {} for group in requested}
    invalid: set[str] = set()

    for issue in issues:
        if not isinstance(issue, dict):
            continue
        labels = [str(label) for label in (issue.get("labels") or [])]
        issue_groups = [
            label.removeprefix("batch:") for label in labels if label.startswith("batch:")
        ]
        matching_groups = [group for group in issue_groups if group in requested]
        if not matching_groups:
            continue
        bead_id = str(issue.get("id") or "")
        if not bead_id or len(issue_groups) != 1:
            invalid.update(matching_groups)
            continue
        group = matching_groups[0]
        status = str(issue.get("status") or "")
        previous = members[group].get(bead_id)
        if previous is not None and previous != status:
            invalid.add(group)
            continue
        members[group][bead_id] = status

    evidence: dict[str, BatchEvidence] = {}
    for group, branch in requested.items():
        group_members = members[group]
        if group in invalid or not group_members:
            continue
        try:
            parents = {
                str(integration_base_fn(entry, bead_id, integration)) for bead_id in group_members
            }
        except Exception:
            continue
        parents.discard("")
        if len(parents) != 1:
            continue
        evidence[branch] = BatchEvidence(
            member_statuses=tuple(sorted(group_members.items())),
            parent=next(iter(parents)),
        )
    return evidence


def flag_legacy_root(statuses: list[WtStatus], active_root: Path) -> list[WtStatus]:
    """Flag any row registered outside ``active_root`` — moved verbatim from the tail of
    ``worktree_inventory.py``'s ``impl__classify_entry`` (the comprehension that used to run
    immediately after its own direct ``wt_status.classify(...)`` call).

    Root still calls ``wt_status.classify(...)`` itself rather than through this package —
    ``beadhive.worktree`` is the established "stable facade and collaborator patch boundary"
    (its own module docstring), and existing tests patch the classifier at that exact seam.
    """
    resolved_root = active_root.resolve()
    return [
        replace(status, legacy_root=not Path(status.path).resolve().is_relative_to(resolved_root))
        if hasattr(status, "path")
        else status
        for status in statuses
    ]


__all__ = [
    "DEFAULT_MAX_WORKERS",
    "classify_entries_concurrently",
    "flag_legacy_root",
    "resolve_batch_evidence",
]
