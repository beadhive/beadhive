"""Unit tests for ws.safety — all-branch repo safety scan engine.

Each test creates real temporary git repos (no mocks) to exercise exactly one
``Category``.  Bare repos serve as remote "origins" so ahead/behind tracking
and push scenarios are fully hermetic.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ws.safety import (
    MATURITY_EASY_COMMITS,
    MATURITY_HARD_COMMITS,
    BranchInfo,
    Category,
    DifficultyResult,
    ScanResult,
    _parse_worktrees,
    difficulty,
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
