"""Removal-effect sequencing on fakes, and the empty-parent-dir reclaim climb (bh-qdezo.7).

The climb tests moved from ``tests/test_worktree.py``'s ``test_rmdir_empty_parents_climbs_to_root``
and ``test_rmdir_empty_parents_stops_at_nonempty``: both exercised the pure climb algorithm with
an explicit root, no config or env-var involvement beyond what ``monkeypatch.setenv`` fed into
``config.worktrees_root()`` — root keeps exactly one test proving that env-var-to-root config
wiring plus the ``rmdir_empty: false`` flag threading (``test_rmdir_empty_parents_disabled``).

``execute_removal`` is net-new direct coverage of the claim-record resolve/retire sequencing
around the one removal effect (bh-cb4jo.2) — no prior root test isolated this sequencing from
the real ``WorktreeLifecycleService``/git plumbing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beadhive_worktrees import WorktreeHandle, WorktreeManagerError, WorktreeRemoved
from beadhive_worktrees.application.removal_service import execute_removal, reclaim_empty_parents


class FakeClaimRecords:
    def __init__(self) -> None:
        self.resolved: list[Path] = []
        self.removed: list[Path | None] = []

    def record_path(self, target):
        path = Path(target) / ".claim"
        self.resolved.append(path)
        return path

    def remove_record_path(self, path):
        self.removed.append(path)


def test_execute_removal_retires_the_claim_record_only_after_success(tmp_path) -> None:
    target = tmp_path / "seat"
    claims = FakeClaimRecords()
    handle = WorktreeHandle("seat", tmp_path / "main", target, "wt/bead/issue/x")

    outcome = execute_removal(lambda: WorktreeRemoved(handle), claim_records=claims, target=target)

    assert outcome.ok is True
    assert outcome.removed == WorktreeRemoved(handle)
    assert claims.resolved == [target / ".claim"]
    assert claims.removed == [target / ".claim"]


def test_execute_removal_reports_the_error_and_keeps_the_claim_record_on_failure(
    tmp_path,
) -> None:
    target = tmp_path / "seat"
    claims = FakeClaimRecords()

    def remove_fn():
        raise WorktreeManagerError(target, 7, "git worktree remove failed")

    outcome = execute_removal(remove_fn, claim_records=claims, target=target)

    assert outcome.ok is False
    assert outcome.error == "git worktree remove failed"
    assert outcome.returncode == 7
    assert claims.resolved == [target / ".claim"]
    assert claims.removed == []  # never retired — the worktree was not actually removed


def test_execute_removal_falls_back_to_str_of_the_exception_when_no_error_text() -> None:
    claims = FakeClaimRecords()

    def remove_fn():
        raise WorktreeManagerError(Path("/x"), 3)

    outcome = execute_removal(remove_fn, claim_records=claims, target=Path("/x"))

    assert outcome.ok is False
    assert outcome.returncode == 3
    assert outcome.error  # a fallback message, never empty


def test_reclaim_empty_parents_climbs_to_root(tmp_path) -> None:
    root = tmp_path / "wts"
    leaf = root / "github" / "org" / "repo" / "feat"
    leaf.mkdir(parents=True)
    leaf.rmdir()  # simulate git having removed the worktree dir

    reclaim_empty_parents(leaf, root)

    assert root.exists()  # root itself is never removed
    assert not (root / "github").exists()  # empty triplet dirs climbed away


def test_reclaim_empty_parents_stops_at_nonempty(tmp_path) -> None:
    root = tmp_path / "wts"
    leaf = root / "github" / "org" / "repo" / "feat"
    leaf.mkdir(parents=True)
    sibling = root / "github" / "org" / "other-repo" / "live"
    sibling.mkdir(parents=True)  # another live worktree under the same org
    leaf.rmdir()

    reclaim_empty_parents(leaf, root)

    assert not (root / "github" / "org" / "repo").exists()  # empty repo dir removed
    assert (root / "github" / "org").exists()  # non-empty org stops the climb
    assert sibling.exists()


def test_reclaim_empty_parents_disabled_leaves_the_tree_intact(tmp_path) -> None:
    root = tmp_path / "wts"
    leaf = root / "github" / "org" / "repo" / "feat"
    leaf.mkdir(parents=True)
    leaf.rmdir()

    reclaim_empty_parents(leaf, root, enabled=False)

    assert (root / "github" / "org" / "repo").exists()


@pytest.mark.parametrize("returncode", [0, 1])
def test_worktree_manager_error_carries_its_returncode(returncode) -> None:
    exc = WorktreeManagerError(Path("/x"), returncode, "boom")
    assert exc.returncode == returncode
    assert exc.error == "boom"
