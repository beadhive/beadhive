"""`ws doctor` self-checks.

Real git in tmp_path + a faked `bd`, same seam as test_work.py: `bd` is reached only through
`ws.bd._run` (doctor's bd queries run via `bd.show` → `bd.json`), so patching that one symbol fakes
Beads while every git op runs for real. The `rig`/`fakebd` fixtures and `_git` helper are reused
from test_work (noqa F811: pytest resolves the imported fixtures by name in the test signature).
"""

from __future__ import annotations

import sys

import pytest

from beadhive import config, doctor, safety, worktree
from beadhive.metadata import RepoMetadata
from beadhive.safety import Category
from test_work import _git, fakebd, rig  # noqa: F401 — fixtures resolved by name


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
    """When fastmcp is absent (broken install), doctor reports unavailable with a reinstall hint."""
    monkeypatch.setitem(sys.modules, "fastmcp", None)
    doctor._section_mcp()
    out = capsys.readouterr().out
    assert "# MCP" in out
    assert "unavailable" in out
    assert "ws[otel]" in out
    assert "ws[otel,mcp]" not in out


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


# ---- doctor_payload structured dict -----------------------------------------

# The section keys beadhive://doctor exposes; asserted here and in the MCP resource test.
_DOCTOR_SECTIONS = {
    "config",
    "providers",
    "orgs",
    "rigs",
    "inventory",
    "disk_usage",
    "fleet_health",
    "worktrees",
    "molecules",
    "mcp",
    "observability",
    "warnings",
}


def test_doctor_payload_has_all_section_keys(rig, fakebd):  # noqa: F811
    """doctor_payload() returns a structured dict keyed by every diagnostics section."""
    payload = doctor.doctor_payload()
    assert set(payload.keys()) == _DOCTOR_SECTIONS


def test_doctor_payload_sections_are_structured(rig, fakebd):  # noqa: F811
    """Section fragments carry structured shapes, not rendered strings."""
    payload = doctor.doctor_payload()
    assert payload["config"]["git_workspace"]["enabled"] in (True, False)
    assert isinstance(payload["providers"], list)
    assert isinstance(payload["inventory"]["git_repos_on_disk"], int)
    assert set(payload["fleet_health"]) >= {"repos_scanned", "dirty", "reclaimable_bytes"}
    assert isinstance(payload["warnings"], list)


# ---- _data_mcp new keys (doctor-keys) -------------------


def test_data_mcp_extra_present(monkeypatch):
    """_data_mcp returns mcp_extra=True when fastmcp is importable."""
    import types

    monkeypatch.setitem(sys.modules, "fastmcp", types.ModuleType("fastmcp"))
    monkeypatch.setattr(doctor, "_plugin_declares_server", lambda cfg: False)
    d = doctor._data_mcp({})
    assert d["mcp_extra"] is True
    assert d["fastmcp_available"] is True  # backward-compat alias


def test_data_mcp_extra_absent(monkeypatch):
    """_data_mcp returns mcp_extra=False when fastmcp is not installed."""
    monkeypatch.setitem(sys.modules, "fastmcp", None)
    monkeypatch.setattr(doctor, "_plugin_declares_server", lambda cfg: False)
    d = doctor._data_mcp({})
    assert d["mcp_extra"] is False
    assert d["fastmcp_available"] is False  # backward-compat alias


def test_data_mcp_plugin_declares_server_true(monkeypatch):
    """_data_mcp returns plugin_declares_server=True when the .mcp.json exists."""
    monkeypatch.setattr(doctor, "_plugin_declares_server", lambda cfg: True)
    d = doctor._data_mcp({})
    assert d["plugin_declares_server"] is True


def test_data_mcp_plugin_declares_server_false(monkeypatch):
    """_data_mcp returns plugin_declares_server=False when .mcp.json is absent."""
    monkeypatch.setattr(doctor, "_plugin_declares_server", lambda cfg: False)
    d = doctor._data_mcp({})
    assert d["plugin_declares_server"] is False


def test_render_mcp_extra_absent_shows_hint(monkeypatch, capsys):
    """When mcp_extra=False, render shows unavailable + bundled-server silent-fail hint."""
    d = {"mcp_extra": False, "plugin_declares_server": True, "fastmcp_available": False}
    doctor._render_mcp(d)
    out = capsys.readouterr().out
    assert "# MCP" in out
    assert "unavailable" in out
    assert "ws[otel]" in out
    assert "ws[otel,mcp]" not in out
    assert "silently fail" in out


def test_render_mcp_both_healthy(monkeypatch, capsys):
    """When mcp_extra=True and plugin_declares_server=True, render shows both healthy."""
    d = {"mcp_extra": True, "plugin_declares_server": True, "fastmcp_available": True}
    doctor._render_mcp(d)
    out = capsys.readouterr().out
    assert "fastmcp: available" in out
    assert "plugin declares server: yes" in out


def test_plugin_declares_server_reads_mcp_json(tmp_path):
    """_plugin_declares_server returns True when .mcp.json declares mcpServers.bh."""
    import json as _json

    manifest = tmp_path / ".claude-plugin" / "marketplace.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(_json.dumps({"plugins": [{"name": "bh", "source": "./bh"}]}))
    mcp_path = tmp_path / "bh" / ".mcp.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text(
        _json.dumps({"mcpServers": {"bh": {"command": "bh-mcp", "args": []}}})
    )
    monkeypatch_cfg = {"managed_repos": []}  # force fallback to package anchor
    # Patch _marketplace_root to return our tmp_path
    import beadhive.config as cfg_mod
    original = cfg_mod._marketplace_root
    cfg_mod._marketplace_root = lambda cfg, plugin: tmp_path
    try:
        result = doctor._plugin_declares_server(monkeypatch_cfg)
    finally:
        cfg_mod._marketplace_root = original
    assert result is True


def test_plugin_declares_server_false_when_absent(tmp_path):
    """_plugin_declares_server returns False when no .mcp.json exists at the root."""
    import beadhive.config as cfg_mod

    original = cfg_mod._marketplace_root
    cfg_mod._marketplace_root = lambda cfg, plugin: tmp_path
    try:
        result = doctor._plugin_declares_server({})
    finally:
        cfg_mod._marketplace_root = original
    assert result is False
