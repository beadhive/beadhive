"""Canonical private-root resolution is shared by linked worktrees, never implicit writes."""

from __future__ import annotations

import json
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
        ".bh/testreport/<tree>": (
            target("repo", Path("validation/runs/<run-id>/reports")),
            target("repo", Path("validation/runs/.summary/<tree>/results.json")),
        ),
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


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n")


def test_clean_upgraded_hive_inventory_is_empty_and_creation_free(tmp_path):
    main, linked = _repo(tmp_path)

    assert private_paths.inventory_private_state(linked) == ()
    assert not (main / ".git/bh").exists()
    assert not (main / ".bh").exists()
    assert not (linked / ".bh").exists()
    assert not list((main / ".git").glob("bh-*"))


def test_inventory_finds_every_cleanup_class_without_deleting_it(tmp_path):
    main, linked = _repo(tmp_path)
    common = main / ".git"
    abandoned_artifacts = main / ".bh/validation/runs/run-abandoned"
    orphan_artifacts = main / ".bh/validation/runs/run-orphan"
    abandoned_artifacts.mkdir(parents=True)
    orphan_artifacts.mkdir(parents=True)
    _write_json(
        common / "bh/validation/runs/run-abandoned/manifest.json",
        {
            "run_id": "run-abandoned",
            "lifecycle": "abandoned",
            "artifacts": {"directory": str(abandoned_artifacts)},
        },
    )
    _write_json(common / "bh/worktrees/gone-token/claim.json", {"bead": "bh-x"})
    _write_json(
        common / "bh/validation/active/verify-old.json",
        {"run_id": "run-abandoned"},
    )
    (common / "bh/build/build-99999999-dead").mkdir(parents=True)
    _write_json(
        common / "bh/release/bump-gate.json",
        {"tree": "old-tree", "pid": 99999999},
    )
    release_log = main / ".bh/release/bump-gates/old-tree/gate.log"
    release_log.parent.mkdir(parents=True)
    release_log.write_text("old\n")
    (common / "bh-validation-ledger.json").write_text("[]\n")
    legacy_overlay = linked / ".bh/otel.env"
    legacy_overlay.parent.mkdir(parents=True)
    legacy_overlay.write_text("OTEL_EXPORTER_OTLP_ENDPOINT=http://old\n")

    before = {p: p.stat().st_mtime_ns for p in (abandoned_artifacts, orphan_artifacts, release_log)}
    findings = private_paths.inventory_private_state(linked)

    kinds = {finding.kind for finding in findings}
    assert {
        "legacy_path",
        "abandoned_validation_run",
        "abandoned_validation_artifact",
        "orphaned_validation_artifact",
        "stale_claim",
        "build_leftover",
        "release_leftover",
        "release_artifact_leftover",
    } <= kinds
    assert {p: p.stat().st_mtime_ns for p in before} == before
    assert all(path.exists() for path in before)


def test_inventory_protects_live_validation_claim_build_and_release_state(tmp_path):
    main, _ = _repo(tmp_path)
    common = main / ".git"
    artifacts = main / ".bh/validation/runs/run-live"
    artifacts.mkdir(parents=True)
    _write_json(
        common / "bh/validation/runs/run-live/manifest.json",
        {
            "run_id": "run-live",
            "lifecycle": "running",
            "artifacts": {"directory": str(artifacts)},
        },
    )
    _write_json(common / "bh/worktrees/main/claim.json", {"bead": "bh-live"})
    live_build = common / f"bh/build/build-{os.getpid()}-live"
    live_build.mkdir(parents=True)
    _write_json(
        common / "bh/release/bump-gate.json",
        {"tree": "live-tree", "pid": os.getpid()},
    )
    live_log = main / ".bh/release/bump-gates/live-tree/gate.log"
    live_log.parent.mkdir(parents=True)
    live_log.write_text("running\n")

    findings = private_paths.inventory_private_state(main)

    assert findings == ()
    assert artifacts.is_dir()
    assert live_build.is_dir()
    assert live_log.is_file()


def _claim(main: Path, name: str) -> Path:
    record = main / ".git" / "bh" / "worktrees" / name / "claim.json"
    _write_json(record, {"bead": "bh-live"})
    return record


def test_symlinked_real_git_worktree_registry_protects_linked_claim(tmp_path):
    main, _ = _repo(tmp_path)
    registry = main / ".git" / "worktrees"
    [admin] = list(registry.iterdir())
    (admin / "config.worktree").write_text("[bh]\n\tclaimIncarnation = live-token\n")
    linked_claim = _claim(main, f"{admin.name}-live-token")
    main_claim = _claim(main, "main")
    real_registry = tmp_path / "real-worktree-registry"
    registry.rename(real_registry)
    registry.symlink_to(real_registry, target_is_directory=True)

    result = subprocess.run(
        ["git", "-C", str(main), "worktree", "list", "--porcelain"],
        check=False,
        env=_CLEAN_ENV,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    findings = private_paths.inventory_private_state(main)

    assert any(
        finding.kind == "protected_claim" and finding.path == linked_claim for finding in findings
    )
    assert not any(
        finding.kind in {"protected_claim", "stale_claim"} and finding.path == main_claim
        for finding in findings
    )
    assert not any(
        finding.kind == "stale_claim" and finding.path == linked_claim for finding in findings
    )


def test_symlinked_real_git_worktree_admin_protects_linked_claim(tmp_path):
    main, _ = _repo(tmp_path)
    registry = main / ".git" / "worktrees"
    [admin] = list(registry.iterdir())
    (admin / "config.worktree").write_text("[bh]\n\tclaimIncarnation = live-token\n")
    linked_claim = _claim(main, f"{admin.name}-live-token")
    main_claim = _claim(main, "main")
    real_admin = tmp_path / "real-linked-admin"
    admin.rename(real_admin)
    admin.symlink_to(real_admin, target_is_directory=True)

    result = subprocess.run(
        ["git", "-C", str(main), "worktree", "list", "--porcelain"],
        check=False,
        env=_CLEAN_ENV,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    findings = private_paths.inventory_private_state(main)

    assert any(
        finding.kind == "protected_claim" and finding.path == linked_claim for finding in findings
    )
    assert not any(
        finding.kind in {"protected_claim", "stale_claim"} and finding.path == main_claim
        for finding in findings
    )
    assert not any(
        finding.kind == "stale_claim" and finding.path == linked_claim for finding in findings
    )


@pytest.mark.parametrize("admin_shape", ["regular", "fifo", "unreadable"])
def test_uninspectable_git_worktree_admin_protects_matching_claim(tmp_path, admin_shape):
    main, _ = _repo(tmp_path)
    registry = main / ".git" / "worktrees"
    [admin] = list(registry.iterdir())
    linked_claim = _claim(main, f"{admin.name}-live-token")
    unrelated_claim = _claim(main, "unrelated-stale-token")
    main_claim = _claim(main, "main")
    saved_admin = tmp_path / "saved-linked-admin"
    admin.rename(saved_admin)
    if admin_shape == "regular":
        admin.write_text("not an admin directory\n")
    elif admin_shape == "fifo":
        os.mkfifo(admin)
    else:
        admin.mkdir()
        admin.chmod(0)
    try:
        findings = private_paths.inventory_private_state(main)
    finally:
        if admin_shape == "unreadable":
            admin.chmod(0o755)

    assert any(
        finding.kind == "protected_claim" and finding.path == linked_claim for finding in findings
    )
    assert any(
        finding.kind == "stale_claim" and finding.path == unrelated_claim for finding in findings
    )
    assert not any(
        finding.kind in {"protected_claim", "stale_claim"} and finding.path == main_claim
        for finding in findings
    )
    assert not any(
        finding.kind == "stale_claim" and finding.path == linked_claim for finding in findings
    )


@pytest.mark.parametrize("registry_shape", ["regular", "fifo", "unreadable"])
def test_uninspectable_git_worktree_registry_protects_non_main_claim(tmp_path, registry_shape):
    main, _ = _repo(tmp_path)
    registry = main / ".git" / "worktrees"
    linked_claim = _claim(main, "linked-live-token")
    main_claim = _claim(main, "main")
    saved_registry = tmp_path / "saved-worktree-registry"
    registry.rename(saved_registry)
    if registry_shape == "regular":
        registry.write_text("not a registry\n")
    elif registry_shape == "fifo":
        os.mkfifo(registry)
    else:
        registry.mkdir()
        registry.chmod(0)
    try:
        findings = private_paths.inventory_private_state(main)
    finally:
        if registry_shape == "unreadable":
            registry.chmod(0o755)

    assert any(
        finding.kind == "protected_claim" and finding.path == linked_claim for finding in findings
    )
    assert not any(
        finding.kind in {"protected_claim", "stale_claim"} and finding.path == main_claim
        for finding in findings
    )
    assert not any(
        finding.kind == "stale_claim" and finding.path == linked_claim for finding in findings
    )


def test_inventory_ignores_outside_manifest_artifact_path_and_protects_in_root_raw(tmp_path):
    main, _ = _repo(tmp_path)
    outside = tmp_path / "outside-artifacts"
    outside.mkdir()
    raw = main / ".bh/validation/runs/run-hostile"
    raw.mkdir(parents=True)
    manifest = main / ".git/bh/validation/runs/run-hostile/manifest.json"
    _write_json(
        manifest,
        {
            "run_id": "run-hostile",
            "lifecycle": "abandoned",
            "artifacts": {"directory": str(outside)},
        },
    )

    findings = private_paths.inventory_private_state(main)

    assert "protected_validation_state" in {finding.kind for finding in findings}
    assert "protected_validation_artifact" in {finding.kind for finding in findings}
    assert "abandoned_validation_artifact" not in {finding.kind for finding in findings}
    assert "orphaned_validation_artifact" not in {finding.kind for finding in findings}
    assert outside not in {finding.path for finding in findings}
    assert all(finding.path.is_relative_to(main) for finding in findings), (
        "inventory must never emit an untrusted manifest path"
    )


@pytest.mark.parametrize(
    "manifest_shape", ["corrupt", "mismatched", "unreadable", "symlink", "fifo"]
)
def test_unmeasurable_manifest_protects_matching_raw_artifact(tmp_path, manifest_shape):
    main, _ = _repo(tmp_path)
    raw = main / ".bh/validation/runs/run-unsafe"
    raw.mkdir(parents=True)
    manifest = main / ".git/bh/validation/runs/run-unsafe/manifest.json"
    manifest.parent.mkdir(parents=True)
    if manifest_shape == "corrupt":
        manifest.write_text("{not-json\n")
    elif manifest_shape == "mismatched":
        _write_json(manifest, {"run_id": "run-other", "lifecycle": "abandoned"})
    elif manifest_shape == "unreadable":
        manifest.write_text('{"run_id":"run-unsafe","lifecycle":"abandoned"}\n')
        manifest.chmod(0)
    elif manifest_shape == "symlink":
        outside = tmp_path / "outside-manifest"
        outside.write_text('{"run_id":"run-unsafe","lifecycle":"abandoned"}\n')
        manifest.symlink_to(outside)
    else:
        os.mkfifo(manifest)

    findings = private_paths.inventory_private_state(main)
    kinds = {finding.kind for finding in findings}

    assert "protected_validation_state" in kinds
    assert "protected_validation_artifact" in kinds
    assert "orphaned_validation_artifact" not in kinds
    assert "abandoned_validation_artifact" not in kinds
    assert raw.exists()


@pytest.mark.parametrize("artifact_shape", ["regular", "symlink", "fifo"])
def test_special_or_symlink_raw_artifact_is_protected_not_orphaned(tmp_path, artifact_shape):
    main, _ = _repo(tmp_path)
    raw = main / ".bh/validation/runs/run-special"
    raw.parent.mkdir(parents=True)
    if artifact_shape == "regular":
        raw.write_text("not-a-directory\n")
    elif artifact_shape == "symlink":
        outside = tmp_path / "outside-raw"
        outside.mkdir()
        raw.symlink_to(outside, target_is_directory=True)
    else:
        os.mkfifo(raw)

    findings = private_paths.inventory_private_state(main)

    assert any(
        finding.kind == "protected_validation_artifact" and finding.path == raw
        for finding in findings
    )
    assert not any(finding.kind == "orphaned_validation_artifact" for finding in findings)


@pytest.mark.parametrize("marker_shape", ["corrupt", "unreadable", "symlink", "fifo"])
def test_unmeasurable_release_marker_protects_all_gate_logs(tmp_path, marker_shape):
    main, _ = _repo(tmp_path)
    marker = main / ".git/bh/release/bump-gate.json"
    marker.parent.mkdir(parents=True)
    if marker_shape == "corrupt":
        marker.write_text("not-json\n")
    elif marker_shape == "unreadable":
        marker.write_text('{"tree":"old","pid":99999999}\n')
        marker.chmod(0)
    elif marker_shape == "symlink":
        outside = tmp_path / "outside-marker"
        outside.write_text('{"tree":"old","pid":99999999}\n')
        marker.symlink_to(outside)
    else:
        os.mkfifo(marker)
    log = main / ".bh/release/bump-gates/old/gate.log"
    log.parent.mkdir(parents=True)
    log.write_text("must survive\n")

    findings = private_paths.inventory_private_state(main)
    kinds = {finding.kind for finding in findings}

    assert "protected_release_state" in kinds
    assert "release_artifact_leftover" not in kinds
    assert log.read_text() == "must survive\n"


@pytest.mark.parametrize("root", ["repo-file", "repo", "repo-validation", "git-validation"])
def test_symlinked_private_root_or_ancestor_is_never_traversed(tmp_path, root):
    main, _ = _repo(tmp_path)
    outside = tmp_path / f"outside-{root}"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("outside\n")
    if root == "repo-file":
        (main / ".bh").write_text("not-a-directory\n")
    elif root == "repo":
        (main / ".bh").symlink_to(outside, target_is_directory=True)
    elif root == "repo-validation":
        (main / ".bh").mkdir()
        (main / ".bh/validation").symlink_to(outside, target_is_directory=True)
    else:
        (main / ".git/bh").mkdir()
        (main / ".git/bh/validation").symlink_to(outside, target_is_directory=True)

    findings = private_paths.inventory_private_state(main)

    assert any(finding.kind.startswith("protected_") for finding in findings)
    assert sentinel not in {finding.path for finding in findings}
    assert sentinel.read_text() == "outside\n"
