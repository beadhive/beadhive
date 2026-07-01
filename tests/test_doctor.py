"""`ws doctor` self-checks.

Real git in tmp_path + a faked `bd`, same seam as test_work.py: `bd` is reached only through
`ws.work.run` (doctor's bd queries go through `work._show`), so patching that one symbol fakes
Beads while every git op runs for real. The `rig`/`fakebd` fixtures and `_git` helper are reused
from test_work (noqa F811: pytest resolves the imported fixtures by name in the test signature).
"""

from __future__ import annotations

import sys

import pytest

from test_work import _git, fakebd, rig  # noqa: F401 — fixtures resolved by name
from ws import config, doctor, safety, worktree
from ws.metadata import RepoMetadata
from ws.safety import Category


def _mol_branch(main, epic):
    """Create a wt/bead/epic/<epic> container branch in the main clone (only the ref matters)."""
    _git("branch", f"{worktree._BEAD_PREFIX}epic/{epic}", cwd=main)


def test_orphan_lists_closed_epic_branch_not_open(rig, fakebd):  # noqa: F811
    # Arrange: two container branches — one epic closed (orphaned), one still open (active).
    _mol_branch(rig.main, "mr-1")
    _mol_branch(rig.main, "mr-2")
    fakebd.seed("mr-1", status="closed")
    fakebd.seed("mr-2", status="open")

    # Act
    orphans = doctor._orphan_container_branches(config.load())

    # Assert: only the closed-epic branch is reported.
    assert orphans == [("mr", "wt/bead/epic/mr-1")]


def test_orphan_empty_when_no_mol_branches(rig, fakebd):  # noqa: F811
    assert doctor._orphan_container_branches(config.load()) == []


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
    assert "wt/bead/epic/mr-1" in out
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


def _make_meta(
    *,
    category: Category,
    has_origin: bool = True,
    disk_bytes: int = 1000,
    dirty: bool = False,
    ahead: int = 0,
    age_days: float | None = 10.0,
) -> RepoMetadata:
    """Build a metadata-cache record with a single branch, as the Fleet Health rollup consumes it.

    Fleet Health now reads pre-measured ``metadata.RepoMetadata`` records (not ``safety.scan``), so
    tests feed records directly instead of monkeypatching the scan/age path.
    """
    return RepoMetadata(
        git_head="deadbeef",
        git_mtime=0.0,
        measured_at="2026-01-01T00:00:00Z",
        category=str(category),
        has_origin=has_origin,
        stash_count=0,
        disk_bytes=disk_bytes,
        commit_count=1,
        age_days=age_days,
        last_commit=None if age_days is None else "2026-01-01",
        branches=[
            {
                "name": "main",
                "ahead": ahead,
                "behind": 0,
                "has_upstream": has_origin,
                "dirty": dirty,
            }
        ],
        worktrees=[],
    )


def test_section_fleet_health_empty(capsys):
    """With no repos, fleet health shows all zeros."""
    doctor._section_fleet_health({}, set())
    out = capsys.readouterr().out
    assert "# Fleet Health (0 repos scanned)" in out
    assert "dirty repos:          0" in out
    assert "unpushed branches:    0" in out
    assert "no-origin repos:      0" in out
    assert "stale clones:         0" in out
    assert "reclaimable space:    0 B" in out


def test_section_fleet_health_counts(capsys):
    """Fleet health correctly counts dirty, unpushed, no-origin, and stale repos."""
    git_repos = {
        "github/org/dirty",
        "github/org/unpushed",
        "github/org/no-origin",
        "github/org/stale",
        "github/org/clean",
    }

    records = {
        "github/org/dirty": _make_meta(
            category=Category.WIP_DIRTY, has_origin=True, disk_bytes=1000, dirty=True
        ),
        "github/org/unpushed": _make_meta(
            category=Category.PUSH_NEEDED, has_origin=True, disk_bytes=2000, ahead=2
        ),
        "github/org/no-origin": _make_meta(
            category=Category.NO_ORIGIN_CLEAN, has_origin=False, disk_bytes=3000
        ),
        "github/org/stale": _make_meta(
            category=Category.READY, has_origin=True, disk_bytes=4000, age_days=400.0
        ),  # > MATURITY_STALE_DAYS (365)
        "github/org/clean": _make_meta(
            category=Category.READY, has_origin=True, disk_bytes=500
        ),
    }

    # Act
    doctor._section_fleet_health(records, git_repos)
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


def test_section_fleet_health_reclaimable_no_double_count(capsys):
    """A repo that is both no-origin and stale is counted in disk space only once."""
    git_repos = {"github/org/old-no-origin"}
    records = {
        "github/org/old-no-origin": _make_meta(
            category=Category.NO_ORIGIN_CLEAN, has_origin=False, disk_bytes=5000, age_days=400.0
        )
    }

    doctor._section_fleet_health(records, git_repos)
    out = capsys.readouterr().out

    assert "no-origin repos:      1" in out
    assert "stale clones:         1" in out
    # 5000 bytes counted once: 5000 / 1024 = 4.9 KB
    assert "reclaimable space:    4.9 KB" in out


def test_section_fleet_health_no_commits_is_stale(capsys):
    """A no-commit repo (cache age_days=None ⇒ inf) counts as stale, matching the prior inf>=365."""
    git_repos = {"github/org/empty"}
    records = {
        "github/org/empty": _make_meta(
            category=Category.NO_ORIGIN_EMPTY, has_origin=False, disk_bytes=2048, age_days=None
        )
    }

    doctor._section_fleet_health(records, git_repos)
    out = capsys.readouterr().out

    assert "stale clones:         1" in out
    assert "no-origin repos:      1" in out


def test_section_fleet_health_skips_missing_record(capsys):
    """A repo key with no cache record (e.g. path vanished after scan) is silently skipped."""
    git_repos = {"github/org/ghost"}  # no record supplied

    doctor._section_fleet_health({}, git_repos)
    out = capsys.readouterr().out

    # Count still reflects the discovered universe, but the record-less repo contributes nothing.
    assert "# Fleet Health (1 repos scanned)" in out
    assert "dirty repos:          0" in out


def test_section_fleet_health_stale_threshold_in_output(capsys):
    """The stale threshold (MATURITY_STALE_DAYS) appears in the stale-clones row."""
    doctor._section_fleet_health({}, set())
    out = capsys.readouterr().out

    stale_days = f"{safety.MATURITY_STALE_DAYS:.0f}d"
    assert stale_days in out
