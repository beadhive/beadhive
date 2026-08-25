"""Canonical private-root resolution is shared by linked worktrees, never implicit writes."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from beadhive import private_paths

_CLEAN_ENV = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, env=_CLEAN_ENV, capture_output=True)


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    main, linked = tmp_path / "main", tmp_path / "linked"
    _git("init", "-q", str(main))
    _git("-C", str(main), "config", "user.email", "test@example.invalid")
    _git("-C", str(main), "config", "user.name", "Test")
    (main / "tracked").write_text("x\n")
    _git("-C", str(main), "add", "tracked")
    _git("-C", str(main), "commit", "-qm", "initial")
    _git("-C", str(main), "worktree", "add", "-qb", "linked", str(linked))
    return main, linked


def test_main_and_linked_worktree_share_git_private_root(tmp_path):
    main, linked = _repo(tmp_path)

    expected = (main / ".git" / "bh").resolve()
    assert private_paths.git_private_root(main) == expected
    assert private_paths.git_private_root(linked) == expected
    assert not expected.exists()  # read-only resolution does not create the root


def test_repo_private_root_is_shared_and_worktree_overlay_is_distinct(tmp_path):
    main, linked = _repo(tmp_path)

    assert private_paths.repo_private_root(main) == main / ".bh"
    assert private_paths.repo_private_root(linked) == main / ".bh"
    assert private_paths.worktree_private_root(main) == main / ".bh"
    assert private_paths.worktree_private_root(linked) == linked / ".bh"
    assert not (main / ".bh").exists()
    assert not (linked / ".bh").exists()


def test_ensure_is_the_explicit_write_seam(tmp_path):
    main, _ = _repo(tmp_path)

    git_root = private_paths.ensure_git_private_root(main)
    repo_root = private_paths.ensure_repo_private_root(main)
    assert git_root is not None and git_root.is_dir()
    assert repo_root is not None and repo_root.is_dir()


def test_explicit_worktree_overlay_creation_does_not_create_shared_root(tmp_path):
    main, linked = _repo(tmp_path)

    overlay = private_paths.ensure_worktree_private_root(linked)
    assert overlay == linked / ".bh"
    assert overlay.is_dir()
    assert not (main / ".bh").exists()


@pytest.mark.parametrize("kind", ["bare", "missing", "malformed"])
def test_invalid_git_metadata_is_a_non_destructive_miss(tmp_path, kind):
    hive = tmp_path / kind
    if kind == "bare":
        _git("init", "--bare", "-q", str(hive))
    elif kind == "malformed":
        hive.mkdir()
        (hive / ".git").write_text("not a gitdir pointer\n")

    assert private_paths.git_private_root(hive) is None
    assert private_paths.repo_private_root(hive) is None
    assert private_paths.worktree_private_root(hive) is None
    assert private_paths.ensure_git_private_root(hive) is None
    assert private_paths.ensure_repo_private_root(hive) is None
    assert private_paths.ensure_worktree_private_root(hive) is None
    assert not (hive / ".bh").exists()
    assert not (hive / "bh").exists()


def test_children_are_contained_below_their_root(tmp_path):
    main, _ = _repo(tmp_path)

    assert private_paths.git_private_path(main, "validation", "active") == (
        main / ".git" / "bh" / "validation" / "active"
    )
    with pytest.raises(ValueError, match="stay below"):
        private_paths.repo_private_path(main, "..", "outside")


def test_legacy_inventory_maps_all_current_beadhive_private_locations():
    target = private_paths.PrivatePathTarget
    actual = {
        legacy: migration.targets
        for legacy, migration in private_paths.LEGACY_PRIVATE_PATHS.items()
    }
    assert actual == {
        ".git/bh-validation-ledger.json": (
            target("git", Path("validation/runs/<run-id>/manifest.json")),
            target("git", Path("validation/verdicts/<tree>/<command-hash>.json")),
        ),
        ".git/bh-release-bump-gate.json": (target("git", Path("release/bump-gate.json")),),
        ".git/bh-release-bump-gate.log": (
            target("repo", Path("release/bump-gates/<tree>/gate.log")),
        ),
        ".git/bh/validation/active/<verify-worktree-id>.json": (
            target("git", Path("validation/active/<run-id>.json")),
        ),
        ".git/bh-build": (target("git", Path("build")),),
        ".git/worktrees/<worktree>/bh-claim.json": (
            target("git", Path("worktrees/<worktree-id>/claim.json")),
        ),
        ".bh/testreport/<tree>": (target("repo", Path("validation/runs/<run-id>/reports")),),
        ".bh/otel.env": (target("worktree", Path("observability/otel.env")),),
        ".bh-verify.json": (target("git", Path("validation/active/<run-id>.json")),),
    }


def test_legacy_ledger_mapping_preserves_run_truth_and_derived_index_roles():
    migration = private_paths.LEGACY_PRIVATE_PATHS[".git/bh-validation-ledger.json"]

    assert "authoritative run manifests" in migration.semantics
    assert "reconstructable verdict index" in migration.semantics


def test_active_markers_are_compatibility_inputs_not_run_truth():
    for legacy in (
        ".git/bh/validation/active/<verify-worktree-id>.json",
        ".bh-verify.json",
    ):
        semantics = private_paths.LEGACY_PRIVATE_PATHS[legacy].semantics
        assert "compatibility" in semantics
        assert "index" in semantics
