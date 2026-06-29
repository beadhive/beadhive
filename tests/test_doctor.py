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
from ws import config, doctor, worktree


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
