"""Unit tests for ws.safety — all-branch repo safety scan engine.

Each test creates real temporary git repos (no mocks) to exercise exactly one
``Category``.  Bare repos serve as remote "origins" so ahead/behind tracking
and push scenarios are fully hermetic.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ws.safety import (
    MATURITY_EASY_COMMITS,
    MATURITY_HARD_COMMITS,
    BackupResult,
    BranchInfo,
    Category,
    DifficultyResult,
    RetireResult,
    RetireVerdict,
    ScanResult,
    _default_branch,
    _parse_worktrees,
    assess_retire,
    backup_unpushed,
    difficulty,
    on_default_branch,
    scan,
)

# Scrub GIT_DIR/GIT_INDEX_FILE/GIT_WORK_TREE so our -C calls always win when
# the suite is invoked inside a git hook (same pattern as test_worktree.py).
_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path | str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env=_ENV,
    )


def _init(path: Path, branch: str = "main") -> None:
    """Initialise a local repo with a fixed identity so commits succeed."""
    _git("init", "-b", branch, cwd=path)
    _git("config", "user.email", "test@ws.dev", cwd=path)
    _git("config", "user.name", "WS Test", cwd=path)


def _commit(path: Path, msg: str = "init", fname: str = "file.txt") -> None:
    (path / fname).write_text(msg)
    _git("add", ".", cwd=path)
    _git("commit", "-m", msg, cwd=path)


def _bare(tmp_path: Path, name: str = "remote.git") -> Path:
    remote = tmp_path / name
    remote.mkdir()
    _git("init", "--bare", "-b", "main", cwd=remote)
    return remote


def _with_origin(tmp_path: Path) -> tuple[Path, Path]:
    """Return (repo, remote) with one commit pushed; main tracks origin/main."""
    remote = _bare(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    _commit(repo)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-u", "origin", "main", cwd=repo)
    return repo, remote


# ---------------------------------------------------------------------------
# _parse_worktrees unit tests
# ---------------------------------------------------------------------------


def test_parse_worktrees_empty_output() -> None:
    assert _parse_worktrees("") == []


def test_parse_worktrees_main_only() -> None:
    porcelain = "worktree /some/path\nHEAD abc123\nbranch refs/heads/main\n\n"
    assert _parse_worktrees(porcelain) == []


def test_parse_worktrees_linked() -> None:
    porcelain = (
        "worktree /main\nHEAD abc\nbranch refs/heads/main\n\n"
        "worktree /linked1\nHEAD def\nbranch refs/heads/feat\n\n"
        "worktree /linked2\nHEAD ghi\nbranch refs/heads/fix\n\n"
    )
    result = _parse_worktrees(porcelain)
    assert result == ["/linked1", "/linked2"]


# ---------------------------------------------------------------------------
# NOT_A_REPO
# ---------------------------------------------------------------------------


def test_not_a_repo(tmp_path: Path) -> None:
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    result = scan(plain_dir)

    assert result.category == Category.NOT_A_REPO
    assert result.has_origin is False
    assert result.stash_count == 0
    assert result.branches == []
    assert result.worktrees == []


# ---------------------------------------------------------------------------
# NO_ORIGIN_EMPTY
# ---------------------------------------------------------------------------


def test_no_origin_empty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    # No commits — HEAD doesn't exist yet.
    result = scan(repo)

    assert result.category == Category.NO_ORIGIN_EMPTY
    assert result.has_origin is False
    assert result.stash_count == 0
    # No commits → no branches yet
    assert result.branches == []


# ---------------------------------------------------------------------------
# NO_ORIGIN_CLEAN
# ---------------------------------------------------------------------------


def test_no_origin_clean(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    _commit(repo)
    result = scan(repo)

    assert result.category == Category.NO_ORIGIN_CLEAN
    assert result.has_origin is False
    assert len(result.branches) == 1
    assert result.branches[0].name == "main"
    assert result.branches[0].has_upstream is False
    assert result.branches[0].dirty is False


# ---------------------------------------------------------------------------
# NO_ORIGIN_DIRTY
# ---------------------------------------------------------------------------


def test_no_origin_dirty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    _commit(repo)
    (repo / "dirty.txt").write_text("unsaved work")
    result = scan(repo)

    assert result.category == Category.NO_ORIGIN_DIRTY
    assert result.has_origin is False
    # dirty=True for the checked-out branch
    main_info = next(b for b in result.branches if b.name == "main")
    assert main_info.dirty is True


# ---------------------------------------------------------------------------
# READY
# ---------------------------------------------------------------------------


def test_ready(tmp_path: Path) -> None:
    repo, _ = _with_origin(tmp_path)
    result = scan(repo)

    assert result.category == Category.READY
    assert result.has_origin is True
    assert len(result.branches) == 1
    main_info = result.branches[0]
    assert main_info.name == "main"
    assert main_info.ahead == 0
    assert main_info.behind == 0
    assert main_info.has_upstream is True
    assert main_info.dirty is False


# ---------------------------------------------------------------------------
# PUSH_NEEDED
# ---------------------------------------------------------------------------


def test_push_needed(tmp_path: Path) -> None:
    repo, _ = _with_origin(tmp_path)
    # Add a commit after the push — main is now 1 ahead of origin/main
    _commit(repo, msg="unpushed change", fname="extra.txt")
    result = scan(repo)

    assert result.category == Category.PUSH_NEEDED
    assert result.has_origin is True
    main_info = next(b for b in result.branches if b.name == "main")
    assert main_info.ahead == 1
    assert main_info.behind == 0
    assert main_info.dirty is False


# ---------------------------------------------------------------------------
# WIP_DIRTY
# ---------------------------------------------------------------------------


def test_wip_dirty(tmp_path: Path) -> None:
    repo, _ = _with_origin(tmp_path)
    # Dirty worktree but not ahead of upstream
    (repo / "wip.txt").write_text("in-progress")
    result = scan(repo)

    assert result.category == Category.WIP_DIRTY
    assert result.has_origin is True
    main_info = next(b for b in result.branches if b.name == "main")
    assert main_info.ahead == 0
    assert main_info.dirty is True


# ---------------------------------------------------------------------------
# WIP_AND_AHEAD
# ---------------------------------------------------------------------------


def test_wip_and_ahead(tmp_path: Path) -> None:
    repo, _ = _with_origin(tmp_path)
    # Ahead of upstream AND dirty worktree
    _commit(repo, msg="unpushed", fname="unpushed.txt")
    (repo / "wip.txt").write_text("in-progress")
    result = scan(repo)

    assert result.category == Category.WIP_AND_AHEAD
    assert result.has_origin is True
    main_info = next(b for b in result.branches if b.name == "main")
    assert main_info.ahead == 1
    assert main_info.dirty is True


# ---------------------------------------------------------------------------
# NO_UPSTREAM  (new local branch without a remote tracking ref)
# ---------------------------------------------------------------------------


def test_no_upstream(tmp_path: Path) -> None:
    repo, _ = _with_origin(tmp_path)
    # Create a local branch with no upstream — leaves main READY, feature NO_UPSTREAM
    _git("checkout", "-b", "feature/new", cwd=repo)
    result = scan(repo)

    assert result.category == Category.NO_UPSTREAM
    assert result.has_origin is True
    feature_info = next(b for b in result.branches if b.name == "feature/new")
    assert feature_info.has_upstream is False
    main_info = next(b for b in result.branches if b.name == "main")
    assert main_info.has_upstream is True


# ---------------------------------------------------------------------------
# Additional: stash_count
# ---------------------------------------------------------------------------


def test_stash_count(tmp_path: Path) -> None:
    repo, _ = _with_origin(tmp_path)
    # git stash only captures tracked changes; modify the already-committed file twice.
    (repo / "file.txt").write_text("wip1")
    _git("stash", cwd=repo)
    (repo / "file.txt").write_text("wip2")
    _git("stash", cwd=repo)
    result = scan(repo)

    assert result.stash_count == 2


# ---------------------------------------------------------------------------
# Additional: worktrees list
# ---------------------------------------------------------------------------


def test_worktrees_linked(tmp_path: Path) -> None:
    repo, _ = _with_origin(tmp_path)
    # Create a linked worktree on a new branch
    wt_path = tmp_path / "my-worktree"
    _git("worktree", "add", "-b", "wt/test", str(wt_path), cwd=repo)

    result = scan(repo)

    assert any(str(wt_path) in p for p in result.worktrees)


def test_worktrees_empty_when_no_linked(tmp_path: Path) -> None:
    repo, _ = _with_origin(tmp_path)
    result = scan(repo)

    assert result.worktrees == []


# ---------------------------------------------------------------------------
# Additional: multi-branch worst-case escalation
# ---------------------------------------------------------------------------


def test_worst_category_wins_across_branches(tmp_path: Path) -> None:
    """When some branches are READY and one is PUSH_NEEDED, overall is PUSH_NEEDED."""
    repo, remote = _with_origin(tmp_path)

    # Create a second branch, push it so it has an upstream
    _git("checkout", "-b", "feat/pushed", cwd=repo)
    _commit(repo, msg="feat commit", fname="feat.txt")
    _git("push", "-u", "origin", "feat/pushed", cwd=repo)
    # Add an extra commit (not pushed) → feat/pushed is PUSH_NEEDED
    _commit(repo, msg="extra", fname="extra.txt")

    # Switch back to main (READY)
    _git("checkout", "main", cwd=repo)
    result = scan(repo)

    assert result.category == Category.PUSH_NEEDED
    feat_info = next(b for b in result.branches if b.name == "feat/pushed")
    assert feat_info.ahead == 1


# ---------------------------------------------------------------------------
# Additional: ScanResult is a dataclass (not mutated)
# ---------------------------------------------------------------------------


def test_scan_result_is_dataclass(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    _commit(repo)
    result = scan(repo)

    assert isinstance(result, ScanResult)
    assert isinstance(result.branches, list)
    assert isinstance(result.worktrees, list)


def test_branch_info_fields(tmp_path: Path) -> None:
    repo, _ = _with_origin(tmp_path)
    result = scan(repo)

    assert len(result.branches) >= 1
    b = result.branches[0]
    assert isinstance(b, BranchInfo)
    assert isinstance(b.name, str)
    assert isinstance(b.ahead, int)
    assert isinstance(b.behind, int)
    assert isinstance(b.has_upstream, bool)
    assert isinstance(b.dirty, bool)


# ---------------------------------------------------------------------------
# Additional: disk_bytes measurement
# ---------------------------------------------------------------------------


def test_disk_bytes_populated_ready(tmp_path: Path) -> None:
    repo, _ = _with_origin(tmp_path)
    result = scan(repo)

    assert isinstance(result.disk_bytes, int)
    assert result.disk_bytes >= 0
    # A repo with at least one commit should have some disk usage
    assert result.disk_bytes > 0


def test_disk_bytes_populated_no_origin(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    _commit(repo)
    result = scan(repo)

    assert isinstance(result.disk_bytes, int)
    assert result.disk_bytes >= 0
    assert result.disk_bytes > 0


def test_disk_bytes_empty_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    # No commits — brand new repo
    result = scan(repo)

    assert isinstance(result.disk_bytes, int)
    assert result.disk_bytes >= 0


def test_disk_bytes_not_a_repo(tmp_path: Path) -> None:
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    # Create a dummy file to have some content
    (plain_dir / "file.txt").write_text("content")
    result = scan(plain_dir)

    assert isinstance(result.disk_bytes, int)
    assert result.disk_bytes >= 0
    # os.walk fallback should measure the file
    assert result.disk_bytes > 0


def test_disk_bytes_scales_with_content(tmp_path: Path) -> None:
    # Create subdirectory first
    repo_dir = tmp_path / "repos"
    repo_dir.mkdir()
    repo1, _ = _with_origin(repo_dir)
    # Small repo
    result1 = scan(repo1)
    small_size = result1.disk_bytes

    # Add a large file
    large_file = repo1 / "large.bin"
    large_file.write_bytes(b"x" * 1_000_000)  # 1 MB
    _git("add", ".", cwd=repo1)
    _git("commit", "-m", "add large file", cwd=repo1)

    result2 = scan(repo1)
    large_size = result2.disk_bytes

    # Size should increase significantly
    assert large_size > small_size
    assert large_size >= small_size + 1_000_000 - 100000  # Allow some overhead/compression


# ---------------------------------------------------------------------------
# difficulty() — table-driven verdict tests (cleanliness signals only)
# ---------------------------------------------------------------------------
#
# These tests construct ScanResult objects directly (no repo_path) so they
# exercise only the cleanliness dimension of difficulty().


def _ready_record(**overrides) -> ScanResult:
    """Minimal READY ScanResult with one clean tracked branch."""
    branch = BranchInfo(name="main", ahead=0, behind=0, has_upstream=True, dirty=False)
    base = dict(
        category=Category.READY,
        has_origin=True,
        stash_count=0,
        branches=[branch],
    )
    base.update(overrides)
    return ScanResult(**base)


@pytest.mark.parametrize(
    "record,kwargs,expected_verdict",
    [
        # READY with no maturity data → 1 easy signal (READY) < 2 → medium
        pytest.param(
            _ready_record(),
            {},
            "medium",
            id="ready_no_maturity",
        ),
        # Explicitly excluded → not-a-candidate regardless of record
        pytest.param(
            _ready_record(),
            {"classify": "excluded"},
            "not-a-candidate",
            id="excluded",
        ),
        # WIP_DIRTY → hard (dirty worktree)
        pytest.param(
            ScanResult(
                category=Category.WIP_DIRTY,
                has_origin=True,
                stash_count=0,
                branches=[BranchInfo("main", 0, 0, True, True)],
            ),
            {},
            "hard",
            id="wip_dirty",
        ),
        # WIP_AND_AHEAD → hard
        pytest.param(
            ScanResult(
                category=Category.WIP_AND_AHEAD,
                has_origin=True,
                stash_count=0,
                branches=[BranchInfo("main", 3, 0, True, True)],
            ),
            {},
            "hard",
            id="wip_and_ahead",
        ),
        # PUSH_NEEDED → medium (not easy, not hard)
        pytest.param(
            ScanResult(
                category=Category.PUSH_NEEDED,
                has_origin=True,
                stash_count=0,
                branches=[BranchInfo("main", 2, 0, True, False)],
            ),
            {},
            "medium",
            id="push_needed",
        ),
        # NO_UPSTREAM → medium
        pytest.param(
            ScanResult(
                category=Category.NO_UPSTREAM,
                has_origin=True,
                stash_count=0,
                branches=[BranchInfo("feat", 0, 0, False, False)],
            ),
            {},
            "medium",
            id="no_upstream",
        ),
        # NO_ORIGIN_DIRTY → hard
        pytest.param(
            ScanResult(
                category=Category.NO_ORIGIN_DIRTY,
                has_origin=False,
                stash_count=0,
                branches=[BranchInfo("main", 0, 0, False, True)],
            ),
            {},
            "hard",
            id="no_origin_dirty",
        ),
        # NO_ORIGIN_EMPTY → hard
        pytest.param(
            ScanResult(
                category=Category.NO_ORIGIN_EMPTY,
                has_origin=False,
                stash_count=0,
                branches=[],
            ),
            {},
            "hard",
            id="no_origin_empty",
        ),
        # NOT_A_REPO → hard
        pytest.param(
            ScanResult(
                category=Category.NOT_A_REPO,
                has_origin=False,
                stash_count=0,
                branches=[],
            ),
            {},
            "hard",
            id="not_a_repo",
        ),
    ],
)
def test_difficulty_cleanliness_table(record, kwargs, expected_verdict) -> None:
    """Table-driven verdict checks using constructed ScanResult objects."""
    result = difficulty(record, **kwargs)
    assert isinstance(result, DifficultyResult)
    assert result.verdict == expected_verdict
    assert isinstance(result.reasons, list)
    assert len(result.reasons) >= 1


# ---------------------------------------------------------------------------
# difficulty() — not-a-candidate short-circuit
# ---------------------------------------------------------------------------


def test_difficulty_excluded_short_circuits() -> None:
    """excluded classify must return not-a-candidate, ignoring all other signals."""
    record = _ready_record()
    result = difficulty(record, classify="excluded")
    assert result.verdict == "not-a-candidate"
    assert result.reasons == ["registry: excluded"]


def test_difficulty_non_excluded_classify_proceeds() -> None:
    """Non-excluded classify values (org-native, personal-or-prototype) proceed normally."""
    for cls in ("org-native", "personal-or-prototype", "fork upstream=acme/lib"):
        record = _ready_record()
        result = difficulty(record, classify=cls)
        # Should evaluate normally (READY, no maturity → medium)
        assert result.verdict != "not-a-candidate"


# ---------------------------------------------------------------------------
# difficulty() — maturity signals (real temporary repos)
# ---------------------------------------------------------------------------


def test_difficulty_maturity_immature(tmp_path: Path) -> None:
    """Repo with fewer than MATURITY_HARD_COMMITS commits → hard (immature)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    _commit(repo, msg="only commit")
    # 1 commit < MATURITY_HARD_COMMITS → immature hard signal
    record = scan(repo)
    result = difficulty(record, repo_path=str(repo))
    assert result.verdict == "hard"
    assert any("immature" in r for r in result.reasons)


def test_difficulty_maturity_hard_boundary(tmp_path: Path) -> None:
    """Exactly MATURITY_HARD_COMMITS - 1 commits → still immature (hard signal)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    for i in range(MATURITY_HARD_COMMITS - 1):
        _commit(repo, msg=f"commit {i}", fname=f"f{i}.txt")
    record = scan(repo)
    result = difficulty(record, repo_path=str(repo))
    assert result.verdict == "hard"
    assert any("immature" in r for r in result.reasons)


def test_difficulty_maturity_easy_boundary(tmp_path: Path) -> None:
    """Exactly MATURITY_EASY_COMMITS commits + recently active → easy signals."""
    repo, _ = _with_origin(tmp_path)
    # Add commits to reach the mature threshold (already has 1 from _with_origin)
    for i in range(MATURITY_EASY_COMMITS - 1):
        _commit(repo, msg=f"commit {i}", fname=f"f{i}.txt")
    record = scan(repo)
    result = difficulty(record, repo_path=str(repo))
    # READY + mature (≥ MATURITY_EASY_COMMITS) + recent → ≥ 2 easy signals → easy
    assert result.verdict == "easy"
    assert any("mature" in r for r in result.reasons)
    assert any("active" in r for r in result.reasons)


def test_difficulty_easy_full_signals(tmp_path: Path) -> None:
    """READY repo with mature + recent activity scores easy."""
    repo, _ = _with_origin(tmp_path)
    for i in range(MATURITY_EASY_COMMITS):
        _commit(repo, msg=f"chore: bump {i}", fname=f"c{i}.txt")
    record = scan(repo)
    result = difficulty(record, repo_path=str(repo))
    assert result.verdict == "easy"


def test_difficulty_medium_middling_commits(tmp_path: Path) -> None:
    """Commit count between hard and easy thresholds + READY → medium (1 easy signal)."""
    repo, _ = _with_origin(tmp_path)
    mid = (MATURITY_HARD_COMMITS + MATURITY_EASY_COMMITS) // 2
    # Already has 1 commit; add enough to reach mid
    for i in range(mid - 1):
        _commit(repo, msg=f"commit {i}", fname=f"m{i}.txt")
    record = scan(repo)
    result = difficulty(record, repo_path=str(repo))
    # READY → 1 easy signal; mid commits → 0 easy signals; recent → 1 easy signal
    # Total: 2 easy signals → easy ... unless mid < MATURITY_EASY_COMMITS
    # mid is between the two thresholds, so commits give 0 easy. READY + recent = 2 → easy
    # Both READY (easy) and recent (easy) fire, so verdict is easy
    assert result.verdict in ("easy", "medium")
    # Ensure it's not hard
    assert result.verdict != "hard"


def test_difficulty_reasons_populated(tmp_path: Path) -> None:
    """DifficultyResult always carries at least one reason per signal evaluated."""
    repo, _ = _with_origin(tmp_path)
    record = scan(repo)
    result = difficulty(record, repo_path=str(repo))
    # With repo_path: maturity (2 signals) + cleanliness (1 signal) = 3 reasons
    assert len(result.reasons) == 3
    assert all(isinstance(r, str) for r in result.reasons)


def test_difficulty_no_commits_reason(tmp_path: Path) -> None:
    """Empty repo (no commits) produces 'recency: no commits' reason, never 'infd'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    # No commits — age is infinite
    record = scan(repo)
    result = difficulty(record, repo_path=str(repo))
    assert any("no commits" in r for r in result.reasons)
    assert not any("infd" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# assess_retire() — verdict tests
# ---------------------------------------------------------------------------


def test_assess_retire_safe(tmp_path: Path) -> None:
    """READY repo with no stashes, no detached HEAD → SAFE with no reasons."""
    repo, _ = _with_origin(tmp_path)
    result = assess_retire(repo)

    assert isinstance(result, RetireResult)
    assert result.verdict == RetireVerdict.SAFE
    assert result.reasons == []


def test_assess_retire_returns_retire_result(tmp_path: Path) -> None:
    """Return type is RetireResult with typed fields."""
    repo, _ = _with_origin(tmp_path)
    result = assess_retire(repo)

    assert isinstance(result, RetireResult)
    assert isinstance(result.verdict, RetireVerdict)
    assert isinstance(result.reasons, list)


def test_assess_retire_not_a_repo(tmp_path: Path) -> None:
    """Non-repo directory → BLOCKED."""
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    result = assess_retire(plain_dir)

    assert result.verdict == RetireVerdict.BLOCKED
    assert any("not a git repository" in r for r in result.reasons)


def test_assess_retire_no_origin_empty(tmp_path: Path) -> None:
    """Empty repo with no origin (NO_ORIGIN_EMPTY) → BLOCKED."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    # No commits
    result = assess_retire(repo)

    assert result.verdict == RetireVerdict.BLOCKED
    assert len(result.reasons) >= 1
    assert any("no origin" in r for r in result.reasons)


def test_assess_retire_no_origin_clean(tmp_path: Path) -> None:
    """Repo with commits but no origin (NO_ORIGIN_CLEAN) → NEEDS_BACKUP."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    _commit(repo)
    result = assess_retire(repo)

    assert result.verdict == RetireVerdict.NEEDS_BACKUP
    assert any("no origin" in r for r in result.reasons)


def test_assess_retire_no_origin_dirty(tmp_path: Path) -> None:
    """No origin + dirty (NO_ORIGIN_DIRTY) → NEEDS_BACKUP with reasons for both."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    _commit(repo)
    (repo / "dirty.txt").write_text("unsaved")
    result = assess_retire(repo)

    assert result.verdict == RetireVerdict.NEEDS_BACKUP
    assert any("no origin" in r for r in result.reasons)
    assert any("uncommitted" in r for r in result.reasons)


def test_assess_retire_push_needed(tmp_path: Path) -> None:
    """Unpushed commits (PUSH_NEEDED) → NEEDS_BACKUP with unpushed reason."""
    repo, _ = _with_origin(tmp_path)
    _commit(repo, msg="unpushed", fname="extra.txt")
    result = assess_retire(repo)

    assert result.verdict == RetireVerdict.NEEDS_BACKUP
    assert any("unpushed" in r for r in result.reasons)


def test_assess_retire_wip_dirty(tmp_path: Path) -> None:
    """Dirty worktree but not ahead (WIP_DIRTY) → NEEDS_BACKUP."""
    repo, _ = _with_origin(tmp_path)
    (repo / "wip.txt").write_text("in-progress")
    result = assess_retire(repo)

    assert result.verdict == RetireVerdict.NEEDS_BACKUP
    assert any("uncommitted" in r for r in result.reasons)


def test_assess_retire_wip_and_ahead(tmp_path: Path) -> None:
    """Unpushed commits + dirty (WIP_AND_AHEAD) → NEEDS_BACKUP with both reasons."""
    repo, _ = _with_origin(tmp_path)
    _commit(repo, msg="unpushed", fname="unpushed.txt")
    (repo / "wip.txt").write_text("in-progress")
    result = assess_retire(repo)

    assert result.verdict == RetireVerdict.NEEDS_BACKUP
    assert any("unpushed" in r for r in result.reasons)
    assert any("uncommitted" in r for r in result.reasons)


def test_assess_retire_no_upstream(tmp_path: Path) -> None:
    """Branch with no upstream tracking ref (NO_UPSTREAM) → NEEDS_BACKUP."""
    repo, _ = _with_origin(tmp_path)
    _git("checkout", "-b", "feature/orphan", cwd=repo)
    result = assess_retire(repo)

    assert result.verdict == RetireVerdict.NEEDS_BACKUP
    assert any("no upstream" in r for r in result.reasons)


def test_assess_retire_stash_escalates_ready(tmp_path: Path) -> None:
    """READY repo with stash entries → NEEDS_BACKUP (stash forces escalation)."""
    repo, _ = _with_origin(tmp_path)
    (repo / "file.txt").write_text("stashed work")
    _git("stash", cwd=repo)
    result = assess_retire(repo)

    assert result.verdict == RetireVerdict.NEEDS_BACKUP
    assert any("stash" in r for r in result.reasons)


def test_assess_retire_stash_count_in_reason(tmp_path: Path) -> None:
    """Stash count is reported in the reason text."""
    repo, _ = _with_origin(tmp_path)
    (repo / "file.txt").write_text("wip1")
    _git("stash", cwd=repo)
    (repo / "file.txt").write_text("wip2")
    _git("stash", cwd=repo)
    result = assess_retire(repo)

    assert result.verdict == RetireVerdict.NEEDS_BACKUP
    assert any("2" in r and "stash" in r for r in result.reasons)


def test_assess_retire_detached_head_clean(tmp_path: Path) -> None:
    """Detached HEAD with clean tree → NEEDS_BACKUP (commits may be gc'd)."""
    repo, _ = _with_origin(tmp_path)
    _git("checkout", "--detach", "HEAD", cwd=repo)
    result = assess_retire(repo)

    assert result.verdict == RetireVerdict.NEEDS_BACKUP
    assert any("detached" in r for r in result.reasons)


def test_assess_retire_detached_head_dirty(tmp_path: Path) -> None:
    """Detached HEAD with uncommitted changes → NEEDS_BACKUP with dirty reason."""
    repo, _ = _with_origin(tmp_path)
    _git("checkout", "--detach", "HEAD", cwd=repo)
    (repo / "wip.txt").write_text("dirty detached work")
    result = assess_retire(repo)

    assert result.verdict == RetireVerdict.NEEDS_BACKUP
    assert any("detached" in r for r in result.reasons)
    assert any("uncommitted" in r for r in result.reasons)


def test_assess_retire_is_pure_readonly(tmp_path: Path) -> None:
    """assess_retire must not modify the repository state."""
    repo, _ = _with_origin(tmp_path)
    _commit(repo, msg="unpushed", fname="extra.txt")

    log_before = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=str(repo), capture_output=True, text=True, env=_ENV,
    ).stdout

    assess_retire(repo)

    log_after = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=str(repo), capture_output=True, text=True, env=_ENV,
    ).stdout

    assert log_before == log_after


def test_assess_retire_multi_branch_worst_wins(tmp_path: Path) -> None:
    """When main is READY but another branch has unpushed commits, verdict is NEEDS_BACKUP."""
    repo, _ = _with_origin(tmp_path)

    _git("checkout", "-b", "feat/ahead", cwd=repo)
    _commit(repo, msg="feat commit", fname="feat.txt")
    _git("push", "-u", "origin", "feat/ahead", cwd=repo)
    # Add an extra commit that is not pushed
    _commit(repo, msg="unpushed feat", fname="extra.txt")

    _git("checkout", "main", cwd=repo)
    result = assess_retire(repo)

    assert result.verdict == RetireVerdict.NEEDS_BACKUP
    assert any("unpushed" in r for r in result.reasons)
    assert any("feat/ahead" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# backup_unpushed() — unit + integration tests
# ---------------------------------------------------------------------------


def _ref_exists(repo: Path, ref: str) -> bool:
    """Return True iff a local branch *ref* exists in *repo*."""
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{ref}"],
        cwd=str(repo),
        capture_output=True,
        env=_ENV,
    )
    return result.returncode == 0


def _remote_ref_exists(remote: Path, ref: str) -> bool:
    """Return True iff branch *ref* exists in bare *remote* repo."""
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{ref}"],
        cwd=str(remote),
        capture_output=True,
        env=_ENV,
    )
    return result.returncode == 0


# --- nothing to do ---


def test_backup_unpushed_ready_returns_nothing_to_do(tmp_path: Path) -> None:
    """READY repo → BackupResult with nothing_to_do=True, no branches pushed."""
    repo, _ = _with_origin(tmp_path)
    result = backup_unpushed(repo)

    assert isinstance(result, BackupResult)
    assert result.nothing_to_do is True
    assert result.wip_branches_pushed == []
    assert result.repo_published is False
    assert result.dry_run is False
    assert len(result.actions) >= 1


def test_backup_unpushed_no_origin_empty_returns_nothing_to_do(tmp_path: Path) -> None:
    """Empty repo with no origin → nothing to back up."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    # No commits — NO_ORIGIN_EMPTY
    result = backup_unpushed(repo)

    assert result.nothing_to_do is True
    assert result.wip_branches_pushed == []
    assert result.repo_published is False


def test_backup_unpushed_not_a_repo_raises(tmp_path: Path) -> None:
    """Non-repo directory → ValueError."""
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError, match="Not a git repository"):
        backup_unpushed(plain)


# --- dirty branch → snapshot WIP ---


def test_backup_unpushed_dirty_branch_creates_wip_and_pushes(tmp_path: Path) -> None:
    """Dirty working tree → wip/retire-<date> branch created and pushed to origin."""
    repo, remote = _with_origin(tmp_path)
    # Make the working tree dirty
    (repo / "wip.txt").write_text("in-progress")

    result = backup_unpushed(repo)

    assert result.nothing_to_do is False
    assert result.repo_published is False
    assert result.dry_run is False
    # At least one WIP branch was pushed
    assert len(result.wip_branches_pushed) >= 1
    wip_branch = result.wip_branches_pushed[0]
    assert wip_branch.startswith("wip/retire-")
    # Branch exists in the remote (bare repo)
    assert _remote_ref_exists(remote, wip_branch), (
        f"Expected branch {wip_branch} in remote"
    )
    # Original branch (main) is still at its original tip (== origin/main).
    main_tip = subprocess.run(
        ["git", "rev-parse", "main"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_ENV,
    ).stdout.strip()
    origin_main = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_ENV,
    ).stdout.strip()
    # main tip is the same as origin/main (the wip commit was NOT added to main)
    assert main_tip == origin_main


# --- ahead branch (not dirty) → WIP at tip ---


def test_backup_unpushed_ahead_branch_creates_wip_at_tip(tmp_path: Path) -> None:
    """Branch with unpushed commits → wip/retire-<date>/<branch> pushed to origin."""
    repo, remote = _with_origin(tmp_path)
    # Add an unpushed commit to main
    _commit(repo, msg="unpushed change", fname="extra.txt")

    result = backup_unpushed(repo)

    assert result.nothing_to_do is False
    assert len(result.wip_branches_pushed) >= 1
    wip_branch = result.wip_branches_pushed[0]
    assert "wip/retire-" in wip_branch
    # Branch exists in remote
    assert _remote_ref_exists(remote, wip_branch), (
        f"Expected branch {wip_branch} in remote"
    )
    # Original main is NOT pushed (backup_unpushed never pushes the source branch).
    ahead_count = subprocess.run(
        ["git", "rev-list", "--count", "origin/main..main"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_ENV,
    ).stdout.strip()
    assert ahead_count == "1", "main should still be 1 ahead of origin/main"


# --- dry_run=True --- no refs created ---


def test_backup_unpushed_dry_run_dirty_no_refs_created(tmp_path: Path) -> None:
    """dry_run=True with dirty branch → actions listed but no WIP branch created."""
    repo, remote = _with_origin(tmp_path)
    (repo / "wip.txt").write_text("in-progress")

    result = backup_unpushed(repo, dry_run=True)

    assert result.dry_run is True
    assert result.nothing_to_do is False
    # No branches were actually pushed
    assert result.wip_branches_pushed == []
    # Actions describe what WOULD happen
    assert any("wip/retire-" in a for a in result.actions)
    # Verify no WIP branch was created locally
    rc = subprocess.run(
        ["git", "branch", "--list", "wip/retire-*"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_ENV,
    )
    assert rc.stdout.strip() == "", "No WIP branch should exist after dry_run"


def test_backup_unpushed_dry_run_ahead_no_refs_created(tmp_path: Path) -> None:
    """dry_run=True with ahead branch → actions listed but no WIP branch created."""
    repo, remote = _with_origin(tmp_path)
    _commit(repo, msg="unpushed change", fname="extra.txt")

    result = backup_unpushed(repo, dry_run=True)

    assert result.dry_run is True
    assert result.wip_branches_pushed == []
    assert any("wip/retire-" in a for a in result.actions)
    # No local wip branch
    rc = subprocess.run(
        ["git", "branch", "--list", "wip/*"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_ENV,
    )
    assert rc.stdout.strip() == ""


# --- NO_ORIGIN (gh mocked) ---


def test_backup_unpushed_no_origin_clean_publishes(tmp_path: Path) -> None:
    """NO_ORIGIN_CLEAN repo → publish wires a real origin + pushes EVERY branch durably.

    ``_publish_no_origin`` is stubbed with a side_effect that does what ``gh repo create``
    would do — create a bare remote, add it as ``origin``, and push the current branch — so
    backup_unpushed's post-condition (every branch reachable on the remote) is verified for
    real without needing the ``gh`` CLI.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    _commit(repo)
    # A second local branch with its own commit — gh repo create --push would NOT push it;
    # backup_unpushed must push it too (C5).
    _git("branch", "feature", cwd=repo)
    _git("switch", "feature", cwd=repo)
    _commit(repo, msg="feature work", fname="feat.txt")
    _git("switch", "main", cwd=repo)

    remote = _bare(tmp_path, name="published.git")
    gh_action = "gh repo create --source=. --push --remote=origin"

    def _fake_publish(path: str, dry_run: bool) -> list[str]:
        # Mirror `gh repo create --source=. --push --remote=origin`: wire origin + push HEAD.
        _git("remote", "add", "origin", str(remote), cwd=path)
        _git("push", "-u", "origin", "main", cwd=path)
        return [gh_action]

    with patch("ws.safety._publish_no_origin", side_effect=_fake_publish) as mock_pub:
        result = backup_unpushed(repo)

    assert result.nothing_to_do is False
    assert result.repo_published is True
    mock_pub.assert_called_once()
    assert any("gh repo create" in a for a in result.actions)
    # EVERY branch reached the remote (not just the published HEAD) — C5.
    assert _remote_ref_exists(remote, "main")
    assert _remote_ref_exists(remote, "feature"), "non-HEAD branch must be pushed too"


def test_backup_unpushed_no_origin_dry_run_lists_gh_action(tmp_path: Path) -> None:
    """NO_ORIGIN_CLEAN + dry_run → gh repo create appears in actions, no call made."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    _commit(repo)

    with patch("ws.safety._gh_authenticated", return_value=True):
        result = backup_unpushed(repo, dry_run=True)

    assert result.dry_run is True
    assert result.repo_published is False
    assert any("gh repo create" in a for a in result.actions)


def test_backup_unpushed_no_origin_gh_not_auth_raises(tmp_path: Path) -> None:
    """NO_ORIGIN repo when gh is not authenticated → RuntimeError."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    _commit(repo)

    with patch("ws.safety._gh_authenticated", return_value=False):
        with pytest.raises(RuntimeError, match="gh CLI"):
            backup_unpushed(repo)


# --- BackupResult is a dataclass ---


def test_backup_result_is_dataclass(tmp_path: Path) -> None:
    """BackupResult is a proper dataclass with expected fields."""
    repo, _ = _with_origin(tmp_path)
    result = backup_unpushed(repo)

    assert isinstance(result, BackupResult)
    assert isinstance(result.nothing_to_do, bool)
    assert isinstance(result.wip_branches_pushed, list)
    assert isinstance(result.repo_published, bool)
    assert isinstance(result.dry_run, bool)
    assert isinstance(result.actions, list)


# ---------------------------------------------------------------------------
# C1 — backup_unpushed must cover EVERY signal assess_retire escalates on:
# no-upstream branches, detached HEAD, and stash entries (not just ahead>0).
# Each test asserts the work reached the bare remote AND the repo is SAFE after.
# ---------------------------------------------------------------------------


def _commit_sha(repo: Path, rev: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "rev-parse", rev],
        cwd=str(repo), capture_output=True, text=True, env=_ENV,
    ).stdout.strip()


def _remote_has_commit(remote: Path, sha: str) -> bool:
    """True iff *sha* is present as an object in the bare *remote*."""
    return subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=str(remote), capture_output=True, env=_ENV,
    ).returncode == 0


def test_backup_unpushed_no_upstream_branch_is_backed_up(tmp_path: Path) -> None:
    """A no-upstream branch carrying a real commit (ahead==0) is backed up to the remote,
    and the repo assesses SAFE afterward (the ``ahead>0`` filter alone would have missed it).
    """
    repo, remote = _with_origin(tmp_path)
    # A branch that never set an upstream, carrying a unique commit.
    _git("switch", "-c", "feature/x", cwd=repo)
    _commit(repo, msg="no-upstream work", fname="nu.txt")
    target = _commit_sha(repo, "feature/x")

    # Precondition: assess_retire flags this as NEEDS_BACKUP (no-upstream branch).
    assert assess_retire(repo).verdict == RetireVerdict.NEEDS_BACKUP

    result = backup_unpushed(repo)

    assert result.nothing_to_do is False
    assert len(result.wip_branches_pushed) >= 1
    # The commit itself is reachable on the bare remote.
    assert _remote_has_commit(remote, target), "no-upstream commit must reach the remote"
    # Invariant: the repo is now SAFE to retire (no residual unbacked work).
    assert assess_retire(repo).verdict == RetireVerdict.SAFE


def test_backup_unpushed_detached_head_is_backed_up(tmp_path: Path) -> None:
    """Detached HEAD carrying a commit not on any branch → snapshotted to a wip branch,
    pushed to the remote, and the repo assesses SAFE afterward."""
    repo, remote = _with_origin(tmp_path)
    # Detach HEAD and add a commit reachable from no named branch.
    _git("checkout", "--detach", "HEAD", cwd=repo)
    _commit(repo, msg="detached work", fname="det.txt")
    target = _commit_sha(repo, "HEAD")

    assert assess_retire(repo).verdict == RetireVerdict.NEEDS_BACKUP

    result = backup_unpushed(repo)

    assert result.nothing_to_do is False
    assert _remote_has_commit(remote, target), "detached commit must reach the remote"
    assert assess_retire(repo).verdict == RetireVerdict.SAFE


def test_backup_unpushed_ready_with_stash_is_backed_up(tmp_path: Path) -> None:
    """A READY repo (clean, pushed) carrying a stash → stash backed up to a durable remote
    ref and cleared locally; the repo assesses SAFE afterward (no silent stash drop)."""
    repo, remote = _with_origin(tmp_path)
    # Create a stash entry on an otherwise-READY repo.
    (repo / "file.txt").write_text("stashed change")
    _git("stash", "push", "-m", "wip", cwd=repo)
    stash_sha = _commit_sha(repo, "stash@{0}")

    # READY by Category, but NEEDS_BACKUP because of the stash.
    assert scan(repo).category == Category.READY
    assert assess_retire(repo).verdict == RetireVerdict.NEEDS_BACKUP

    result = backup_unpushed(repo)

    assert result.nothing_to_do is False
    # The stash commit object is durably on the remote.
    assert _remote_has_commit(remote, stash_sha), "stash commit must reach the remote"
    # Local stash list is now empty and the repo is SAFE.
    assert _git("stash", "list", cwd=repo).stdout.strip() == ""
    assert assess_retire(repo).verdict == RetireVerdict.SAFE


def test_backup_unpushed_stash_no_origin_refuses(tmp_path: Path) -> None:
    """A stash with no origin to push it to → REFUSE (raise) rather than silently drop it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    _commit(repo)
    (repo / "file.txt").write_text("stashed change")
    _git("stash", "push", "-m", "wip", cwd=repo)

    # No-origin publish would run gh; stub it to a no-op so we isolate the stash refusal.
    with patch("ws.safety._publish_no_origin", return_value=["(published)"]):
        with pytest.raises(RuntimeError, match="stash"):
            backup_unpushed(repo)


def test_backup_unpushed_push_failure_raises(tmp_path: Path) -> None:
    """When the backup push CANNOT reach the remote (bogus origin), backup_unpushed RAISES
    instead of reporting success with the work stranded local-only (C2)."""
    repo, _remote = _with_origin(tmp_path)
    _commit(repo, msg="unpushed", fname="extra.txt")  # main now ahead → NEEDS_BACKUP
    # Repoint origin at a non-existent remote so any push fails.
    _git("remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"), cwd=repo)

    with pytest.raises(RuntimeError):
        backup_unpushed(repo)


def test_backup_unpushed_makes_push_needed_repo_safe(tmp_path: Path) -> None:
    """End-to-end invariant: a PUSH_NEEDED repo is SAFE after a successful backup."""
    repo, _remote = _with_origin(tmp_path)
    _commit(repo, msg="unpushed", fname="extra.txt")

    assert assess_retire(repo).verdict == RetireVerdict.NEEDS_BACKUP
    backup_unpushed(repo)
    assert assess_retire(repo).verdict == RetireVerdict.SAFE


# ---------------------------------------------------------------------------
# on_default_branch — onboarding preflight default-branch check
# ---------------------------------------------------------------------------


def test_on_default_branch_via_origin_head(tmp_path: Path) -> None:
    """origin/HEAD advertises the default; HEAD sitting on it → (True, branch)."""
    repo, _remote = _with_origin(tmp_path)
    # Advertise origin's default branch (a fresh clone would set this automatically).
    _git("remote", "set-head", "origin", "main", cwd=repo)

    assert _default_branch(str(repo)) == "main"
    ok, detail = on_default_branch(repo)
    assert ok is True
    assert detail == "main"


def test_on_default_branch_fallback_to_main(tmp_path: Path) -> None:
    """No origin at all → resolution falls through to 'main'; HEAD on main → (True, 'main')."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo, branch="main")
    _commit(repo)

    ok, detail = on_default_branch(repo)
    assert ok is True
    assert detail == "main"


def test_on_default_branch_non_default_branch(tmp_path: Path) -> None:
    """HEAD on a feature branch that is not the default → (False, detail names both)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo, branch="main")
    _commit(repo)
    _git("checkout", "-b", "feature", cwd=repo)

    ok, detail = on_default_branch(repo)
    assert ok is False
    assert "feature" in detail
    assert "main" in detail


def test_on_default_branch_detached_head(tmp_path: Path) -> None:
    """Detached HEAD → (False, detail flags the detachment)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo, branch="main")
    _commit(repo)
    _commit(repo, msg="second", fname="second.txt")
    head = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    _git("checkout", head, cwd=repo)

    ok, detail = on_default_branch(repo)
    assert ok is False
    assert "detached" in detail.lower()


def test_on_default_branch_origin_head_overrides_local_default(tmp_path: Path) -> None:
    """When origin/HEAD points elsewhere, a repo sitting on a different branch is not default."""
    repo, remote = _with_origin(tmp_path)
    # Publish a 'develop' branch and make it origin's advertised default.
    _git("checkout", "-b", "develop", cwd=repo)
    _git("push", "-u", "origin", "develop", cwd=repo)
    _git("remote", "set-head", "origin", "develop", cwd=repo)
    _git("checkout", "main", cwd=repo)

    assert _default_branch(str(repo)) == "develop"
    ok, detail = on_default_branch(repo)
    assert ok is False
    assert "develop" in detail
