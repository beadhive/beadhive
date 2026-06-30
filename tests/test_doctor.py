"""`ws doctor` self-checks.

Real git in tmp_path + a faked `bd`, same seam as test_work.py: `bd` is reached only through
`ws.work.run` (doctor's bd queries go through `work._show`), so patching that one symbol fakes
Beads while every git op runs for real. The `rig`/`fakebd` fixtures and `_git` helper are reused
from test_work (noqa F811: pytest resolves the imported fixtures by name in the test signature).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from test_work import _git, fakebd, rig  # noqa: F401 — fixtures resolved by name
from ws import config, doctor, safety, worktree
from ws.safety import BranchInfo, Category, ScanResult


def _mol_branch(main, epic):
    """Create a mol/<epic> branch in the main clone (only the ref matters, not its commits)."""
    _git("branch", f"{worktree.MOL_PREFIX}{epic}", cwd=main)


def test_orphan_lists_closed_epic_branch_not_open(rig, fakebd):  # noqa: F811
    # Arrange: two molecule branches — one epic closed (orphaned), one still open (active).
    _mol_branch(rig.main, "mr-1")
    _mol_branch(rig.main, "mr-2")
    fakebd.seed("mr-1", status="closed")
    fakebd.seed("mr-2", status="open")

    # Act
    orphans = doctor._orphan_mol_branches(config.load())

    # Assert: only the closed-epic branch is reported.
    assert orphans == [("mr", "mol/mr-1")]


def test_orphan_empty_when_no_mol_branches(rig, fakebd):  # noqa: F811
    assert doctor._orphan_mol_branches(config.load()) == []


def test_section_renders_clean_line_when_none(rig, fakebd, capsys):  # noqa: F811
    doctor._section_molecules(config.load())
    out = capsys.readouterr().out
    assert "# Molecule branches (0 orphaned)" in out
    assert "✓ none" in out


def test_section_lists_orphan(rig, fakebd, capsys):  # noqa: F811
    _mol_branch(rig.main, "mr-1")
    fakebd.seed("mr-1", status="closed")
    doctor._section_molecules(config.load())
    out = capsys.readouterr().out
    assert "# Molecule branches (1 orphaned)" in out
    assert "mol/mr-1" in out
    assert "delete manually" in out


def test_section_mcp_available(capsys):
    """When fastmcp is importable, doctor reports it as available."""
    pytest.importorskip("fastmcp")
    doctor._section_mcp()
    out = capsys.readouterr().out
    assert "# MCP" in out
    assert "available" in out


def test_section_mcp_unavailable_shows_install_hint(monkeypatch, capsys):
    """When fastmcp is absent, doctor reports unavailable with an install hint."""
    monkeypatch.setitem(sys.modules, "fastmcp", None)
    doctor._section_mcp()
    out = capsys.readouterr().out
    assert "# MCP" in out
    assert "unavailable" in out
    assert "ws[mcp]" in out


def test_section_observability_defaults(capsys):
    """Default config: log.format=auto, log.level=info, otel disabled."""
    cfg: dict = {}
    doctor._section_observability(cfg)
    out = capsys.readouterr().out
    assert "# Observability" in out
    assert "log.format: auto" in out
    assert "log.level: info" in out
    assert "otel.enabled: false" in out
    assert "endpoint: (not set)" in out


def test_section_observability_otel_enabled(capsys):
    """When otel is enabled and endpoint is set, both appear in output."""
    cfg = {"otel": {"enabled": True, "endpoint": "http://localhost:4317"}}
    doctor._section_observability(cfg)
    out = capsys.readouterr().out
    assert "otel.enabled: true" in out
    assert "http://localhost:4317" in out


def test_section_observability_otel_libs_absent(monkeypatch, capsys):
    """When opentelemetry is not installed, doctor shows unavailable + install hint."""
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    cfg: dict = {}
    doctor._section_observability(cfg)
    out = capsys.readouterr().out
    assert "unavailable" in out
    assert "ws[otel]" in out


# ---- fleet health section ---------------------------------------------------


def _make_scan_result(
    *,
    category: Category,
    has_origin: bool = True,
    disk_bytes: int = 1000,
    dirty: bool = False,
    ahead: int = 0,
) -> ScanResult:
    """Build a ScanResult with a single branch for testing."""
    return ScanResult(
        category=category,
        has_origin=has_origin,
        stash_count=0,
        disk_bytes=disk_bytes,
        branches=[
            BranchInfo(
                name="main",
                ahead=ahead,
                behind=0,
                has_upstream=has_origin,
                dirty=dirty,
            )
        ],
    )


def test_section_fleet_health_empty(tmp_path, capsys):
    """With no repos, fleet health shows all zeros."""
    doctor._section_fleet_health(tmp_path, set())
    out = capsys.readouterr().out
    assert "# Fleet Health (0 repos scanned)" in out
    assert "dirty repos:          0" in out
    assert "unpushed branches:    0" in out
    assert "no-origin repos:      0" in out
    assert "stale clones:         0" in out
    assert "reclaimable space:    0 B" in out


def test_section_fleet_health_counts(tmp_path, capsys, monkeypatch):
    """Fleet health correctly counts dirty, unpushed, no-origin, and stale repos."""
    # Arrange: create repo dirs so path.exists() returns True.
    for name in ["dirty", "unpushed", "no-origin", "stale", "clean"]:
        (tmp_path / "github" / "org" / name).mkdir(parents=True)

    git_repos = {
        "github/org/dirty",
        "github/org/unpushed",
        "github/org/no-origin",
        "github/org/stale",
        "github/org/clean",
    }

    scan_map = {
        "github/org/dirty": _make_scan_result(
            category=Category.WIP_DIRTY,
            has_origin=True,
            disk_bytes=1000,
            dirty=True,
        ),
        "github/org/unpushed": _make_scan_result(
            category=Category.PUSH_NEEDED,
            has_origin=True,
            disk_bytes=2000,
            ahead=2,
        ),
        "github/org/no-origin": _make_scan_result(
            category=Category.NO_ORIGIN_CLEAN,
            has_origin=False,
            disk_bytes=3000,
        ),
        "github/org/stale": _make_scan_result(
            category=Category.READY,
            has_origin=True,
            disk_bytes=4000,
        ),
        "github/org/clean": _make_scan_result(
            category=Category.READY,
            has_origin=True,
            disk_bytes=500,
        ),
    }
    age_map = {
        "github/org/dirty": 10.0,
        "github/org/unpushed": 10.0,
        "github/org/no-origin": 10.0,
        "github/org/stale": 400.0,  # > MATURITY_STALE_DAYS (365)
        "github/org/clean": 10.0,
    }

    def fake_scan(path):
        key = str(Path(path).relative_to(tmp_path))
        return scan_map[key]

    def fake_age(path):
        key = str(Path(path).relative_to(tmp_path))
        return age_map[key]

    monkeypatch.setattr(safety, "scan", fake_scan)
    monkeypatch.setattr(safety, "last_commit_age_days", fake_age)

    # Act
    doctor._section_fleet_health(tmp_path, git_repos)
    out = capsys.readouterr().out

    # Assert counts
    assert "# Fleet Health (5 repos scanned)" in out
    assert "dirty repos:          1" in out
    assert "unpushed branches:    1" in out
    assert "no-origin repos:      1" in out
    assert "stale clones:         1" in out
    # reclaimable = no-origin (3000) + stale (4000) = 7000 bytes = 6.8 KB
    assert "reclaimable space:    6.8 KB" in out
    assert "no-origin or stale" in out


def test_section_fleet_health_reclaimable_no_double_count(tmp_path, capsys, monkeypatch):
    """A repo that is both no-origin and stale is counted in disk space only once."""
    (tmp_path / "github" / "org" / "old-no-origin").mkdir(parents=True)
    git_repos = {"github/org/old-no-origin"}

    result = _make_scan_result(
        category=Category.NO_ORIGIN_CLEAN,
        has_origin=False,
        disk_bytes=5000,
    )
    monkeypatch.setattr(safety, "scan", lambda _: result)
    monkeypatch.setattr(safety, "last_commit_age_days", lambda _: 400.0)

    doctor._section_fleet_health(tmp_path, git_repos)
    out = capsys.readouterr().out

    assert "no-origin repos:      1" in out
    assert "stale clones:         1" in out
    # 5000 bytes counted once: 5000 / 1024 = 4.9 KB
    assert "reclaimable space:    4.9 KB" in out


def test_section_fleet_health_skips_missing_path(tmp_path, capsys, monkeypatch):
    """Repos whose path does not exist on disk are silently skipped."""
    git_repos = {"github/org/ghost"}  # directory never created

    scan_called = []
    monkeypatch.setattr(safety, "scan", lambda p: scan_called.append(p) or _make_scan_result(
        category=Category.READY
    ))
    monkeypatch.setattr(safety, "last_commit_age_days", lambda _: 10.0)

    doctor._section_fleet_health(tmp_path, git_repos)
    out = capsys.readouterr().out

    # scan() was never called because the path does not exist
    assert not scan_called
    assert "# Fleet Health (1 repos scanned)" in out
    assert "dirty repos:          0" in out


def test_section_fleet_health_stale_threshold_in_output(tmp_path, capsys, monkeypatch):
    """The stale threshold (MATURITY_STALE_DAYS) appears in the stale-clones row."""
    monkeypatch.setattr(safety, "scan", lambda _: _make_scan_result(category=Category.READY))
    monkeypatch.setattr(safety, "last_commit_age_days", lambda _: 10.0)

    doctor._section_fleet_health(tmp_path, set())
    out = capsys.readouterr().out

    stale_days = f"{safety.MATURITY_STALE_DAYS:.0f}d"
    assert stale_days in out
