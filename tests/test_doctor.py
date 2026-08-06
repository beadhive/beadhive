"""`ws doctor` self-checks.

Real git in tmp_path + a faked `bd`, same seam as test_work.py: `bd` is reached only through
`ws.bd._run` (doctor's bd queries run via `bd.show` → `bd.json`), so patching that one symbol fakes
Beads while every git op runs for real. The `hive`/`fakebd` fixtures and `_git` helper are reused
from test_work (noqa F811: pytest resolves the imported fixtures by name in the test signature).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from beadhive import config, doctor, hitch_plugin, safety, worktree
from beadhive.metadata import RepoMetadata
from beadhive.safety import Category
from test_work import _git, fakebd, hive  # noqa: F401 — fixtures resolved by name


def _mol_branch(main, epic):
    """Create a wt/bead/epic/<epic> container branch in the main clone (only the ref matters)."""
    _git("branch", f"{worktree._BEAD_PREFIX}epic/{epic}", cwd=main)


def test_orphan_lists_closed_epic_branch_not_open(hive, fakebd):  # noqa: F811
    # Arrange: two container branches — one epic closed (orphaned), one still open (active).
    _mol_branch(hive.main, "mr-1")
    _mol_branch(hive.main, "mr-2")
    fakebd.seed("mr-1", status="closed")
    fakebd.seed("mr-2", status="open")

    # Act
    orphans = doctor._orphan_container_branches(config.load())

    # Assert: only the closed-epic branch is reported.
    assert orphans == [("mr", "wt/bead/epic/mr-1")]


def test_orphan_empty_when_no_mol_branches(hive, fakebd):  # noqa: F811
    assert doctor._orphan_container_branches(config.load()) == []


def test_section_renders_clean_line_when_none(hive, fakebd, capsys):  # noqa: F811
    doctor._section_molecules(config.load())
    out = capsys.readouterr().out
    assert "# Molecule branches (0 orphaned)" in out
    assert "✓ none" in out


def test_section_lists_orphan(hive, fakebd, capsys):  # noqa: F811
    _mol_branch(hive.main, "mr-1")
    fakebd.seed("mr-1", status="closed")
    doctor._section_molecules(config.load())
    out = capsys.readouterr().out
    assert "# Molecule branches (1 orphaned)" in out
    assert "wt/bead/epic/mr-1" in out
    assert "delete manually" in out


# ---- prefix mismatches section (bh-6h1m) ------------------------------------


@pytest.fixture
def prefix_hive(tmp_path, monkeypatch):
    """A registered hive with a real `.beads/` dir (no git needed — `_data_prefix_mismatches`
    only stats the directory and shells to `bd`, both faked/real here)."""
    ws_root = tmp_path / "ws"
    main = ws_root / "github" / "myorg" / "myrepo"
    (main / ".beads").mkdir(parents=True)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    return SimpleNamespace(main=main)


def _cfg_one_hive(prefix="mr"):
    return {
        "managed_repos": [
            {
                "provider": "github",
                "org": "myorg",
                "repo": "myrepo",
                "prefix": prefix,
                "kind": "personal",
            },
        ]
    }


def test_data_prefix_mismatches_reports_divergence(prefix_hive, monkeypatch):
    monkeypatch.setattr(doctor.bd, "json", lambda args, cwd: {"value": "ah2-"})
    data = doctor._data_prefix_mismatches(_cfg_one_hive("ah"))
    assert data == [
        {
            "hive": "github/myorg/myrepo",
            "registry_prefix": "ah",
            "db_prefix": "ah2",
            "remediation": "bh hive repair --hive github/myorg/myrepo --prefix ah --yes",
        }
    ]


def test_data_prefix_mismatches_empty_when_consistent(prefix_hive, monkeypatch):
    monkeypatch.setattr(doctor.bd, "json", lambda args, cwd: {"value": "mr"})
    assert doctor._data_prefix_mismatches(_cfg_one_hive("mr")) == []


def test_data_prefix_mismatches_skips_missing_beads_dir(tmp_path, monkeypatch):
    """No local checkout under the hive path — nothing to compare, so it's silently skipped
    (the generic Warnings section already flags a missing checkout)."""
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "ws"))
    called = []
    monkeypatch.setattr(doctor.bd, "json", lambda args, cwd: called.append(cwd) or {"value": "x"})
    assert doctor._data_prefix_mismatches(_cfg_one_hive("mr")) == []
    assert called == []  # never even asked bd — the .beads/ check short-circuits first


def test_data_prefix_mismatches_skips_unreadable_db_prefix(prefix_hive, monkeypatch):
    monkeypatch.setattr(doctor.bd, "json", lambda args, cwd: None)
    assert doctor._data_prefix_mismatches(_cfg_one_hive("mr")) == []


def test_data_prefix_mismatches_skips_unparseable_prefix(prefix_hive, monkeypatch):
    """An invalid prefix on either side isn't this check's problem — `normalize_prefix` raising
    is swallowed rather than propagated."""
    monkeypatch.setattr(doctor.bd, "json", lambda args, cwd: {"value": ""})
    assert doctor._data_prefix_mismatches(_cfg_one_hive("mr")) == []


def test_render_prefix_mismatches_clean_when_empty(capsys):
    doctor._render_prefix_mismatches([])
    out = capsys.readouterr().out
    assert "# Prefix mismatches (0)" in out
    assert "✓ none" in out


def test_render_prefix_mismatches_shows_fix_command(capsys):
    doctor._render_prefix_mismatches(
        [
            {
                "hive": "github/myorg/myrepo",
                "registry_prefix": "ah",
                "db_prefix": "ah2",
                "remediation": "bh hive repair --hive github/myorg/myrepo --prefix ah --yes",
            }
        ]
    )
    out = capsys.readouterr().out
    assert "# Prefix mismatches (1)" in out
    assert "github/myorg/myrepo" in out
    assert "registry='ah'" in out
    assert "db='ah2'" in out
    assert "fix: bh hive repair --hive github/myorg/myrepo --prefix ah --yes" in out


def test_section_prefix_mismatches_renders_end_to_end(prefix_hive, monkeypatch, capsys):
    monkeypatch.setattr(doctor.bd, "json", lambda args, cwd: {"value": "ah2"})
    doctor._section_prefix_mismatches(_cfg_one_hive("ah"))
    out = capsys.readouterr().out
    assert "# Prefix mismatches (1)" in out
    assert "db='ah2'" in out


def test_collect_includes_prefix_mismatches(hive, fakebd):  # noqa: F811
    """No local `.beads/` checkout in the plain `hive` fixture, so the section is empty — this
    just pins that `_collect` wires the key through end to end."""
    payload = doctor.doctor_payload()
    assert payload["prefix_mismatches"] == []


# ---- store engine section (bh-areg.3) ----------------------------------------


def _write_dolt_metadata(hive_dir, **fields):
    (hive_dir / ".beads").mkdir(parents=True, exist_ok=True)
    (hive_dir / ".beads" / "metadata.json").write_text(json.dumps(fields))


def test_data_store_engine_silent_when_no_beads_dir(hive, fakebd, monkeypatch):  # noqa: F811
    """The plain `hive` fixture has no `.beads/` checkout — nothing to report, matching the
    acceptance bar's silent-by-default rule."""
    monkeypatch.delenv("BEADS_DOLT_SHARED_SERVER", raising=False)
    assert doctor._data_store_engine(config.load()) == {"relevant": False}


def test_data_store_engine_silent_for_an_all_embedded_fleet(prefix_hive, monkeypatch):
    """No new noise for users who never migrate (bh-areg.3's own acceptance bar)."""
    _write_dolt_metadata(prefix_hive.main, dolt_mode="embedded")
    monkeypatch.delenv("BEADS_DOLT_SHARED_SERVER", raising=False)

    assert doctor._data_store_engine(_cfg_one_hive("mr")) == {"relevant": False}


def test_data_store_engine_reports_a_reachable_server_mode_hive(prefix_hive, monkeypatch):
    _write_dolt_metadata(prefix_hive.main, dolt_mode="server")
    monkeypatch.delenv("BEADS_DOLT_SHARED_SERVER", raising=False)
    monkeypatch.setattr(
        doctor.dolt_health,
        "probe_shared_server",
        lambda **k: doctor.dolt_health.ProbeResult(True, "127.0.0.1:3308 reachable"),
    )

    data = doctor._data_store_engine(_cfg_one_hive("mr"))

    assert data["relevant"] is True
    assert data["server_mode_hives"] == ["mr"]
    assert data["reachable"] is True
    assert data["mismatches"] == []


def test_data_store_engine_reports_an_unreachable_server_mode_hive(prefix_hive, monkeypatch):
    _write_dolt_metadata(prefix_hive.main, dolt_mode="server")
    monkeypatch.setattr(
        doctor.dolt_health,
        "probe_shared_server",
        lambda **k: doctor.dolt_health.ProbeResult(False, "127.0.0.1:3308 refused"),
    )

    data = doctor._data_store_engine(_cfg_one_hive("mr"))

    assert data["reachable"] is False
    assert "refused" in data["detail"]


def test_data_store_engine_reports_engine_metadata_mismatch(prefix_hive, monkeypatch):
    """Newly-in-scope surfacing (bh-areg.1's review): metadata pins embedded but the shared
    server is actually active for this run — bd's own main.go warns about exactly this."""
    _write_dolt_metadata(prefix_hive.main, dolt_mode="embedded")
    monkeypatch.setenv("BEADS_DOLT_SHARED_SERVER", "1")

    data = doctor._data_store_engine(_cfg_one_hive("mr"))

    assert data["relevant"] is True
    assert data["server_mode_hives"] == []  # persisted mode is still "embedded"
    assert len(data["mismatches"]) == 1
    assert data["mismatches"][0]["prefix"] == "mr"
    assert "embedded" in data["mismatches"][0]["reason"]


def test_render_store_engine_silent_when_not_relevant(capsys):
    doctor._render_store_engine({"relevant": False})
    assert capsys.readouterr().out == ""


def test_render_store_engine_shows_reachable(capsys):
    doctor._render_store_engine(
        {
            "relevant": True,
            "endpoint": {"host": "127.0.0.1", "port": 3308},
            "server_mode_hives": ["mr"],
            "reachable": True,
            "detail": "127.0.0.1:3308 reachable",
            "mismatches": [],
        }
    )
    out = capsys.readouterr().out
    assert "# Store Engine" in out
    assert "✓ reachable" in out
    assert "mr" in out


def test_render_store_engine_shows_unreachable_loudly_with_the_remedy(capsys):
    doctor._render_store_engine(
        {
            "relevant": True,
            "endpoint": {"host": "127.0.0.1", "port": 3308},
            "server_mode_hives": ["mr"],
            "reachable": False,
            "detail": "127.0.0.1:3308 refused the connection — nothing listening",
            "mismatches": [],
        }
    )
    out = capsys.readouterr().out
    assert "✗ UNREACHABLE" in out
    assert "bd dolt start" in out
    assert "does not auto-start" in out or "fall back to embedded" in out


def test_render_store_engine_shows_mismatch_warning(capsys):
    doctor._render_store_engine(
        {
            "relevant": True,
            "endpoint": {"host": "127.0.0.1", "port": 3308},
            "server_mode_hives": [],
            "reachable": None,
            "detail": None,
            "mismatches": [{"prefix": "mr", "reason": "shared-server mode is active but ..."}],
        }
    )
    out = capsys.readouterr().out
    assert "⚠ mr:" in out


def test_section_store_engine_renders_end_to_end(prefix_hive, monkeypatch, capsys):
    _write_dolt_metadata(prefix_hive.main, dolt_mode="server")
    monkeypatch.setattr(
        doctor.dolt_health,
        "probe_shared_server",
        lambda **k: doctor.dolt_health.ProbeResult(True, "127.0.0.1:3308 reachable"),
    )

    doctor._section_store_engine(_cfg_one_hive("mr"))

    out = capsys.readouterr().out
    assert "# Store Engine" in out
    assert "✓ reachable" in out


def test_section_mcp_available(capsys):
    """When fastmcp is importable, doctor reports it as available."""
    pytest.importorskip("fastmcp")
    doctor._section_mcp()
    out = capsys.readouterr().out
    assert "# MCP" in out
    assert "available" in out


def test_section_mcp_unavailable_shows_install_hint(monkeypatch, capsys):
    """When fastmcp is absent (broken install), doctor reports unavailable with a repair hint.

    The hint is PLANE-DERIVED since bh-jmw0, so the plane is pinned here rather than asserting a
    literal command: this used to assert `beadhive[otel]` unconditionally, which is the bug —
    that command is wrong on an editable checkout and inside the image."""
    monkeypatch.setitem(sys.modules, "fastmcp", None)
    monkeypatch.setattr(doctor.install_plane, "detect", lambda **k: doctor.install_plane.PYPI)

    doctor._section_mcp()

    out = capsys.readouterr().out
    assert "# MCP" in out
    assert "unavailable" in out
    assert "uv tool upgrade beadhive" in out
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
    assert "beadhive[otel]" in out


# ---- fleet health section ---------------------------------------------------


def _make_meta(
    *,
    category: Category,
    has_origin: bool = True,
    disk_bytes: int = 1000,
    dirty: bool = False,
    ahead: int = 0,
    age_days: float | None = 10.0,
    dolt_status: str = "absent",
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
        dolt_ref={"status": dolt_status, "ahead": 0, "behind": 0},
    )


def test_section_fleet_health_empty(capsys):
    """With no repos, fleet health shows all zeros."""
    doctor._section_fleet_health({}, set())
    out = capsys.readouterr().out
    assert "# Fleet Health (0 repos scanned)" in out
    assert "dirty repos:          0" in out
    assert "unpushed branches:    0" in out
    assert "unpushed dolt state:  0" in out
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
        "github/org/clean": _make_meta(category=Category.READY, has_origin=True, disk_bytes=500),
    }

    # Act
    doctor._section_fleet_health(records, git_repos)
    out = capsys.readouterr().out

    # Assert counts
    assert "# Fleet Health (5 repos scanned)" in out
    assert "dirty repos:          1" in out
    assert "unpushed branches:    1" in out
    assert "unpushed dolt state:  0" in out
    assert "no-origin repos:      1" in out
    assert "stale clones:         1" in out
    # reclaimable = no-origin (3000) + stale (4000) = 7000 bytes = 6.8 KB
    assert "reclaimable space:    6.8 KB" in out
    assert "no-origin or stale" in out


def test_section_fleet_health_dolt_unpushed_counted_distinctly_from_git_unpushed(capsys):
    """A repo with clean git branches but unpushed Dolt state (refs/dolt/data) counts toward
    dolt-unpushed WITHOUT counting toward git-unpushed — the two tallies are independent
    (bh-59q1.1: a hive can read SAFE on git alone while its Beads state is unbacked)."""
    git_repos = {"github/org/dolt-ahead", "github/org/dolt-no-remote", "github/org/clean"}

    records = {
        "github/org/dolt-ahead": _make_meta(
            category=Category.READY, has_origin=True, dolt_status="ahead"
        ),
        "github/org/dolt-no-remote": _make_meta(
            category=Category.READY, has_origin=True, dolt_status="no-remote"
        ),
        "github/org/clean": _make_meta(
            category=Category.READY, has_origin=True, dolt_status="clean"
        ),
    }

    doctor._section_fleet_health(records, git_repos)
    out = capsys.readouterr().out

    assert "# Fleet Health (3 repos scanned)" in out
    assert "unpushed branches:    0" in out
    assert "unpushed dolt state:  2" in out


def test_section_fleet_health_counts_embedded_dolt_engine_unknown_status(capsys):
    """A dolt_ref status of 'unknown' (genuinely unverifiable, see safety.DoltRefInfo.reason)
    counts in its own 'unknown state' bucket — neither silently treated as clean nor
    rendered as confirmed unpushed."""
    git_repos = {"github/org/dolt-unknown", "github/org/clean"}

    records = {
        "github/org/dolt-unknown": _make_meta(
            category=Category.READY, has_origin=True, dolt_status="unknown"
        ),
        "github/org/clean": _make_meta(
            category=Category.READY, has_origin=True, dolt_status="clean"
        ),
    }

    doctor._section_fleet_health(records, git_repos)
    out = capsys.readouterr().out

    assert "# Fleet Health (2 repos scanned)" in out
    assert "unpushed dolt state:  0" in out
    assert "unknown state:        1" in out


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


def test_section_fleet_health_counts_missing_record_as_unknown(capsys):
    """A repo key with no cache record (e.g. path vanished after scan) counts as unknown
    state instead of vanishing from the tally — unverifiable is never silently green."""
    git_repos = {"github/org/ghost"}  # no record supplied

    doctor._section_fleet_health({}, git_repos)
    out = capsys.readouterr().out

    assert "# Fleet Health (1 repos scanned)" in out
    assert "dirty repos:          0" in out
    assert "unknown state:        1" in out


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
    "hives",
    "inventory",
    "disk_usage",
    "fleet_health",
    "worktrees",
    "molecules",
    "prefix_mismatches",
    "store_engine",
    "group_auth",
    "mcp",
    "seats",
    "install",
    "observability",
    "warnings",
}


def test_doctor_payload_has_all_section_keys(hive, fakebd):  # noqa: F811
    """doctor_payload() returns a structured dict keyed by every diagnostics section."""
    payload = doctor.doctor_payload()
    assert set(payload.keys()) == _DOCTOR_SECTIONS


def test_doctor_payload_sections_are_structured(hive, fakebd):  # noqa: F811
    """Section fragments carry structured shapes, not rendered strings."""
    payload = doctor.doctor_payload()
    assert payload["config"]["git_workspace"]["enabled"] in (True, False)
    assert isinstance(payload["providers"], list)
    assert isinstance(payload["inventory"]["git_repos_on_disk"], int)
    assert set(payload["fleet_health"]) >= {
        "repos_scanned",
        "dirty",
        "unknown",
        "reclaimable_bytes",
    }
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
    """When mcp_extra=False, render shows unavailable + bundled-server silent-fail hint.

    Plane pinned rather than asserting a literal command (bh-jmw0) — see the note on
    `test_section_mcp_unavailable_shows_install_hint`."""
    monkeypatch.setattr(
        doctor.install_plane, "detect", lambda **k: doctor.install_plane.PROVISIONED
    )
    d = {"mcp_extra": False, "plugin_declares_server": True, "fastmcp_available": False}

    doctor._render_mcp(d)

    out = capsys.readouterr().out
    assert "# MCP" in out
    assert "unavailable" in out
    assert "beadhive[otel]" in out
    assert "nix profile upgrade" in out  # the toolchain half, on a provisioned host
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
    mcp_path.write_text(_json.dumps({"mcpServers": {"bh": {"command": "bh-mcp", "args": []}}}))
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


# ---- group_auth section (bh-4y0r.3) ------------------------------------------


@pytest.fixture
def global_gitconfig(tmp_path, monkeypatch):
    cfg_file = tmp_path / "gitconfig-global"
    cfg_file.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg_file))
    return cfg_file


def test_data_group_auth_reports_rows_and_warnings(tmp_path, monkeypatch, global_gitconfig):
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path))
    (tmp_path / "workspace.toml").write_text(
        '[[provider]]\nprovider = "github"\nname = "acme"\npath = "github"\n'
    )
    data = doctor._data_group_auth({})
    assert data["groups"][0]["path"] == "github"
    assert any("no scoped identity" in w for w in data["warnings"])


def test_render_group_auth_smoke(capsys):
    d = {
        "groups": [
            {
                "path": "github",
                "account": "acme",
                "name": "",
                "email": "",
                "signingkey": "",
                "scoped": False,
                "insteadof_alias": None,
            }
        ],
        "warnings": [
            "repo group 'github' has no scoped identity (no includeIf gitdir: block) "
            "— falling back to the global user.name/email"
        ],
    }
    doctor._render_group_auth(d)
    out = capsys.readouterr().out
    assert "Repo-group auth" in out
    assert "github/acme" in out
    assert "no scoped identity" in out


def test_collect_group_auth_empty_when_no_repo_groups(hive, fakebd):  # noqa: F811
    """bh-hsus.4: git-workspace has no `enabled` flag to gate `_data_group_auth` on any more
    (it's always called) — this hermetic env just has no workspace*.toml, so `groups(cfg)` is
    empty and the section comes back empty on its own."""
    payload = doctor.doctor_payload()
    assert payload["group_auth"] == {"groups": [], "warnings": []}


# ---- seats section (bh-og0q.4) -----------------------------------------------
# "Which seats can this host run" rides hitch_plugin's Plugin.readiness hook — the acceptance
# bar is total silence (no header, no line) when hitch is disabled/absent, and a per-seat
# breakdown (hard blocker vs reduced capability vs runnable) when it's enabled.


def test_data_seats_none_when_hitch_disabled():
    """Disabled (or entirely absent from config) is the default — _data_seats returns None."""
    assert doctor._data_seats({}) is None
    assert doctor._data_seats({"hitch": {"enabled": False}}) is None


def test_render_seats_silent_when_none(capsys):
    """No header, no line at all — stronger than `bh hive ready`'s own 'na' convention."""
    doctor._render_seats(None)
    assert capsys.readouterr().out == ""


def test_section_seats_silent_when_hitch_disabled(capsys):
    doctor._section_seats({})
    assert capsys.readouterr().out == ""


def test_data_seats_reads_the_plugin_readiness_hook(monkeypatch):
    """_data_seats rides hitch_plugin.PLUGIN — the SAME (state, detail) hook `bh hive ready`
    consumes — rather than a bespoke doctor-only capability check. Swaps the whole PLUGIN
    object (a frozen dataclass instance can't have one field patched in place) so `_data_seats`
    is provably going through `.enabled`/`.readiness`, not calling `hitch_plugin._readiness`
    directly."""
    cfg = {"hitch": {"enabled": True}}
    fake_plugin = SimpleNamespace(
        enabled=lambda cfg, entry: True,
        readiness=lambda cfg, entry: ("warn", "dispatcher: cannot run — x"),
    )
    monkeypatch.setattr(hitch_plugin, "PLUGIN", fake_plugin)
    d = doctor._data_seats(cfg)
    assert d == {"state": "warn", "detail": "dispatcher: cannot run — x"}


def test_render_seats_shows_per_seat_breakdown(capsys):
    d = {
        "state": "warn",
        "detail": "hitch on PATH; repo /r; seats -\n  dispatcher: runnable\n  "
        "developer: cannot run — beadhive: required binary 'example-tool' not found in PATH",
    }
    doctor._render_seats(d)
    out = capsys.readouterr().out
    assert "# Seats (hitch)" in out
    assert "dispatcher: runnable" in out
    assert "developer: cannot run" in out
    assert "example-tool" in out


def test_doctor_render_includes_seats_section(monkeypatch, hive, fakebd, capsys):  # noqa: F811
    """End-to-end: `doctor()` calls `_render_seats` with the collected payload's `seats` key."""
    monkeypatch.setattr(doctor, "_data_seats", lambda cfg: {"state": "ok", "detail": "x: runnable"})
    doctor.doctor()
    out = capsys.readouterr().out
    assert "# Seats (hitch)" in out
    assert "x: runnable" in out


# ---- install-staleness section (bh-9plr) ------------------------------------


def _write_pkg(pkg_dir, marker):
    """Materialize a minimal src/beadhive package with a marker line, return its dir."""
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text(f"# {marker}\n")
    return pkg_dir


def test_install_from_source_is_never_stale(tmp_path, monkeypatch):
    """When the running package IS the self-hive source dir, staleness is not flagged."""
    src = _write_pkg(tmp_path / "src" / "beadhive", "v1")
    monkeypatch.setattr(doctor, "_running_pkg_dir", lambda: src.resolve())
    monkeypatch.setattr(doctor, "_source_pkg_dir", lambda cfg: src.resolve())
    d = doctor._data_install({})
    assert d["from_source"] is True
    assert d["stale"] is False


def test_install_stale_when_snapshot_diverges(tmp_path, monkeypatch):
    """An installed snapshot whose .py differs from the self-hive source is flagged stale."""
    installed = _write_pkg(tmp_path / "installed" / "beadhive", "OLD")
    source = _write_pkg(tmp_path / "src" / "beadhive", "NEW")
    monkeypatch.setattr(doctor, "_running_pkg_dir", lambda: installed.resolve())
    monkeypatch.setattr(doctor, "_source_pkg_dir", lambda cfg: source.resolve())
    d = doctor._data_install({})
    assert d["from_source"] is False
    assert d["stale"] is True


def test_install_in_sync_not_stale(tmp_path, monkeypatch):
    """Identical .py content (a fresh install) hashes equal and is not stale."""
    installed = _write_pkg(tmp_path / "installed" / "beadhive", "SAME")
    source = _write_pkg(tmp_path / "src" / "beadhive", "SAME")
    monkeypatch.setattr(doctor, "_running_pkg_dir", lambda: installed.resolve())
    monkeypatch.setattr(doctor, "_source_pkg_dir", lambda cfg: source.resolve())
    d = doctor._data_install({})
    assert d["stale"] is False


def test_install_no_source_checkout_skips_check(tmp_path, monkeypatch):
    """With no self-hive source found, staleness cannot be judged and stays False."""
    installed = _write_pkg(tmp_path / "installed" / "beadhive", "x")
    monkeypatch.setattr(doctor, "_running_pkg_dir", lambda: installed.resolve())
    monkeypatch.setattr(doctor, "_source_pkg_dir", lambda cfg: None)
    d = doctor._data_install({})
    assert d["source_dir"] is None
    assert d["stale"] is False


def test_section_install_renders_stale_reinstall_command(tmp_path, monkeypatch, capsys):
    """The command is now PLANE-DERIVED (bh-jmw0), so this pins the shape rather than one literal:
    a stale snapshot still tells the operator how to repair it, via whichever plane it is on."""
    installed = _write_pkg(tmp_path / "installed" / "beadhive", "OLD")
    source = _write_pkg(tmp_path / "src" / "beadhive", "NEW")
    monkeypatch.setattr(doctor, "_running_pkg_dir", lambda: installed.resolve())
    monkeypatch.setattr(doctor, "_source_pkg_dir", lambda cfg: source.resolve())
    monkeypatch.setattr(doctor.install_plane, "detect", lambda **k: doctor.install_plane.PYPI)

    doctor._section_install({})

    out = capsys.readouterr().out
    assert "# Install" in out
    assert "STALE" in out
    assert "upgrade:" in out


def test_section_install_pins_the_reinstall_to_the_source_version(tmp_path, monkeypatch, capsys):
    """A provisioned host must be told to reinstall AT A VERSION (bh-jmw0). The old hint was
    unpinned, so following it moved the host off the pin `install.sh` derived from the tag —
    undoing the mechanism release-pin.sh exists to make unforgeable."""
    installed = _write_pkg(tmp_path / "installed" / "beadhive", "OLD")
    source = _write_pkg(tmp_path / "hive" / "src" / "beadhive", "NEW")
    (source.parents[1] / "pyproject.toml").write_text('[project]\nversion = "9.9.9"\n')
    monkeypatch.setattr(doctor, "_running_pkg_dir", lambda: installed.resolve())
    monkeypatch.setattr(doctor, "_source_pkg_dir", lambda cfg: source.resolve())
    monkeypatch.setattr(
        doctor.install_plane, "detect", lambda **k: doctor.install_plane.PROVISIONED
    )

    doctor._section_install({})

    out = capsys.readouterr().out
    assert "beadhive[otel]==9.9.9" in out, f"unpinned reinstall would unpin the host: {out}"
    assert "nix profile upgrade" in out, "the toolchain half of the upgrade must not go unsaid"


def test_section_install_never_tells_a_container_to_reinstall(tmp_path, monkeypatch, capsys):
    """Inside the image bh comes from a wheel at BUILD time, so a reinstall is discarded by the
    next `docker compose up` — the bh-h5if disappearing act. Say rebuild, or say nothing."""
    installed = _write_pkg(tmp_path / "installed" / "beadhive", "OLD")
    source = _write_pkg(tmp_path / "src" / "beadhive", "NEW")
    monkeypatch.setattr(doctor, "_running_pkg_dir", lambda: installed.resolve())
    monkeypatch.setattr(doctor, "_source_pkg_dir", lambda cfg: source.resolve())
    monkeypatch.setattr(doctor.install_plane, "detect", lambda **k: doctor.install_plane.CONTAINER)

    doctor._section_install({})

    out = capsys.readouterr().out
    assert "uv tool install" not in out
    assert "rebuild the image" in out


# ---- furnish drift (declared zero-footprint vs tracked .beads) ---------------


def _furnish_drift_repo(tmp_path, *, track_beads: bool):
    root = tmp_path / "ws"
    repo = root / "github" / "acme" / "zf"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@ws.dev", cwd=repo)
    _git("config", "user.name", "T", cwd=repo)
    (repo / ".beads").mkdir()
    (repo / ".beads" / "config.yaml").write_text("prefix: zf\n")
    if track_beads:
        _git("add", ".beads", cwd=repo)
        _git("commit", "-q", "-m", "scaffold", cwd=repo)
    return root


def _furnish_warns(root, entry):
    return doctor._data_warnings({}, root, [entry], set(), set(), set(), set())


def test_furnish_drift_warns_on_tracked_beads(tmp_path):
    root = _furnish_drift_repo(tmp_path, track_beads=True)
    entry = {
        "provider": "github",
        "org": "acme",
        "repo": "zf",
        "prefix": "zf",
        "kind": "prototype",
        "furnish": "none",
    }
    warns = _furnish_warns(root, entry)
    assert any("declared zero-footprint" in w for w in warns)


def test_no_furnish_drift_warning_when_untracked(tmp_path):
    root = _furnish_drift_repo(tmp_path, track_beads=False)
    entry = {
        "provider": "github",
        "org": "acme",
        "repo": "zf",
        "prefix": "zf",
        "kind": "prototype",
        "furnish": "none",
    }
    warns = _furnish_warns(root, entry)
    assert not any("declared zero-footprint" in w for w in warns)


def test_no_furnish_drift_warning_for_furnished_hive(tmp_path):
    root = _furnish_drift_repo(tmp_path, track_beads=True)
    entry = {
        "provider": "github",
        "org": "acme",
        "repo": "zf",
        "prefix": "zf",
        "kind": "prototype",
        "furnish": "full",
    }
    warns = _furnish_warns(root, entry)
    assert not any("declared zero-footprint" in w for w in warns)


# ---- validate_cmd "does it RESOLVE to running tests" nudge (bh-l44i, reworked) -----------
#
# The naive `"test" in cmd` substring check fired on ~every hive following the fleet-wide
# dominant `just check` -> `check: lint lint-md test` -> `uv run pytest` convention (confirmed:
# ~20/20 hives, none of which are actually compile-only). This resolves the recipe through the
# hive's own justfile instead (validate_probe.probe_validate_cmd) — only a fully-resolved,
# provably test-free graph warns; anything unresolvable (no checkout, no justfile, a non-`just`
# command) stays quiet rather than guess.

_TESTED_JUSTFILE = "check: lint test\n\nlint:\n    ruff check\n\ntest:\n    uv run pytest\n"
_COMPILE_ONLY_JUSTFILE = "check: lint typecheck\n\nlint:\n    ruff check\n\ntypecheck:\n    mypy\n"


def _hive_checkout(tmp_path, *, justfile_text=None):
    """A minimal on-disk checkout at the path `_data_warnings` derives for a github/acme/zf
    entry, optionally seeded with a justfile — `probe_validate_cmd` needs a real path to read."""
    path = tmp_path / "github" / "acme" / "zf"
    path.mkdir(parents=True)
    if justfile_text is not None:
        (path / "justfile").write_text(justfile_text)
    (path / ".beads").mkdir()  # avoid tripping the separate "no .beads/" warning in these tests
    return path


def test_validate_cmd_warns_when_unconfigured_and_resolved_test_free(tmp_path):
    entry = {"provider": "github", "org": "acme", "repo": "zf", "prefix": "zf", "kind": "personal"}
    _hive_checkout(tmp_path, justfile_text=_COMPILE_ONLY_JUSTFILE)
    warns = doctor._data_warnings({}, tmp_path, [entry], set(), set(), set(), set())
    assert any(
        "validate_cmd defaults to" in w and "does not look like it runs tests" in w for w in warns
    )


def test_validate_cmd_silent_when_resolved_to_tests(tmp_path):
    """PINNED (bh-l44i rework acceptance): `just check` with a justfile whose `check` recipe
    transitively runs pytest — this repo's own dominant shape — must NOT warn."""
    entry = {"provider": "github", "org": "acme", "repo": "zf", "prefix": "zf", "kind": "personal"}
    _hive_checkout(tmp_path, justfile_text=_TESTED_JUSTFILE)
    warns = doctor._data_warnings({}, tmp_path, [entry], set(), set(), set(), set())
    assert not any("validate_cmd defaults to" in w for w in warns)


def test_validate_cmd_silent_when_unresolvable_no_justfile(tmp_path):
    """No justfile at all -> unresolvable -> silent, not a guessed warning — this is the exact
    fleet-wide false positive the coordinator flagged (bh doctor firing on ~20/20 hives)."""
    entry = {"provider": "github", "org": "acme", "repo": "zf", "prefix": "zf", "kind": "personal"}
    _hive_checkout(tmp_path, justfile_text=None)
    warns = doctor._data_warnings({}, tmp_path, [entry], set(), set(), set(), set())
    assert not any("validate_cmd defaults to" in w for w in warns)


def test_validate_cmd_silent_when_no_local_checkout(tmp_path):
    """No checkout on disk at all -> probe gets no root to read -> unresolvable -> silent."""
    entry = {"provider": "github", "org": "acme", "repo": "zf", "prefix": "zf", "kind": "personal"}
    warns = doctor._data_warnings({}, tmp_path, [entry], set(), set(), set(), set())
    assert not any("validate_cmd defaults to" in w for w in warns)


def test_validate_cmd_silent_when_explicitly_configured(tmp_path):
    entry = {"provider": "github", "org": "acme", "repo": "zf", "prefix": "zf", "kind": "personal"}
    _hive_checkout(tmp_path, justfile_text=_COMPILE_ONLY_JUSTFILE)  # would warn if consulted
    cfg = {"work": {"validate_cmd": "just check"}}  # same text, but a named/deliberate choice
    warns = doctor._data_warnings(cfg, tmp_path, [entry], set(), set(), set(), set())
    assert not any("validate_cmd defaults to" in w for w in warns)


def test_validate_cmd_silent_when_per_hive_override_configured(tmp_path):
    entry = {
        "provider": "github",
        "org": "acme",
        "repo": "zf",
        "prefix": "zf",
        "kind": "personal",
        "work": {"validate_cmd": "sh -c 'just check && just test'"},
    }
    warns = doctor._data_warnings({}, tmp_path, [entry], set(), set(), set(), set())
    assert not any("validate_cmd defaults to" in w for w in warns)


# ---- "N local commits made while not primary" (bh-ytbb.12) -------------------
#
# The doctor-side twin of the pre-push fence hook (test_prepush.py): reuses the SAME
# `guard.primary_state` cached-lease read, so the two can never disagree about who is
# primary. HQ/lease fixtures mirror test_guard_primary.py's — a scratch HQ clone under
# tmp_path with BH_HQ pointed at it, never the operator's real HQ.

import subprocess  # noqa: E402

from beadhive import gitref, guard, host, host_fence, host_lease  # noqa: E402
from test_work import _CLEAN_ENV  # noqa: E402,F401 — reused for the dated-commit helper below

_PREFIX = "zf"
_THIS_HOST = "33333333-3333-4333-8333-333333333333"
_OTHER_HOST = "44444444-4444-4444-8444-444444444444"
_T0 = 1_800_000_000.0


@pytest.fixture
def commits_hq(tmp_path, monkeypatch):
    path = tmp_path / "hq"
    path.mkdir()
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "t@example.invalid", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    monkeypatch.setenv("BH_HQ", str(path))
    return path


@pytest.fixture
def commits_this_host(monkeypatch):
    monkeypatch.setattr(host, "host_id", lambda: _THIS_HOST)
    return _THIS_HOST


def _commits_record_lease(hq_dir, lease):
    sha = gitref.write_object(lease.to_record(), cwd=hq_dir)
    gitref.set_local(host_lease.lease_ref(_PREFIX), sha, cwd=hq_dir)


def _commits_lease(host_id, *, adopted_at, ttl=600.0, label="deskmac"):
    return host_lease.HostLease(
        host_id=host_id,
        label=label,
        epoch=1,
        adopted_at=adopted_at,
        expires_at=host_lease.now_stamp(_T0 + ttl),
    )


def _commit_on_dolt_data(repo, message, *, at):
    """A commit on `refs/dolt/data` dated `at` (epoch seconds) — a faithful stand-in for a
    real bd write, same technique test_host_fence.py's `_stage_data` uses. `_git` (imported
    from test_work) has no `env=` seam, so both dates are set directly via subprocess."""
    stamp = host_lease.now_stamp(at)
    env = {**_CLEAN_ENV, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", message],
        cwd=str(repo),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    _git("update-ref", host_fence.DATA_REF, sha, cwd=repo)
    return sha


def _commits_entry(prefix=_PREFIX):
    return {
        "provider": "github",
        "org": "acme",
        "repo": "zf",
        "prefix": prefix,
        "kind": "prototype",
        "furnish": "none",
    }


def test_zero_when_never_adopted(tmp_path):
    """No HQ clone at all on this host — single-host default, nothing to check."""
    n, holder = doctor._local_commits_while_not_primary({}, _commits_entry(), tmp_path)
    assert (n, holder) == (0, "")


def test_zero_when_this_host_is_primary(commits_hq, commits_this_host, monkeypatch, tmp_path):
    monkeypatch.setattr(host_lease.time, "time", lambda: _T0 + 1)
    lease = _commits_lease(_THIS_HOST, adopted_at=host_lease.now_stamp(_T0))
    _commits_record_lease(commits_hq, lease)
    n, holder = doctor._local_commits_while_not_primary({}, _commits_entry(), tmp_path)
    assert (n, holder) == (0, "")


def test_counts_only_commits_after_the_current_holders_adoption(
    commits_hq, commits_this_host, monkeypatch, tmp_path
):
    """The bounded semantics: a commit made BEFORE primacy passed to the current holder does
    not count (ordinary unpushed local work, not evidence of writing while not primary) —
    only commits dated strictly after `adopted_at` do."""
    repo = tmp_path / "hive"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@example.invalid", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _commit_on_dolt_data(repo, "before-handoff", at=_T0 - 3600)  # while this host WAS primary
    _commit_on_dolt_data(repo, "after-handoff-1", at=_T0 + 60)  # after the OTHER host adopted
    _commit_on_dolt_data(repo, "after-handoff-2", at=_T0 + 120)

    monkeypatch.setattr(host_lease.time, "time", lambda: _T0 + 200)
    _commits_record_lease(
        commits_hq, _commits_lease(_OTHER_HOST, adopted_at=host_lease.now_stamp(_T0))
    )

    n, holder = doctor._local_commits_while_not_primary({}, _commits_entry(), repo)
    assert n == 2
    assert holder == _OTHER_HOST


def test_data_warnings_includes_the_new_line(commits_hq, commits_this_host, monkeypatch, tmp_path):
    root = _furnish_drift_repo(tmp_path, track_beads=False)
    repo = root / "github" / "acme" / "zf"
    _commit_on_dolt_data(repo, "after-handoff", at=_T0 + 60)
    monkeypatch.setattr(host_lease.time, "time", lambda: _T0 + 200)
    _commits_record_lease(
        commits_hq, _commits_lease(_OTHER_HOST, adopted_at=host_lease.now_stamp(_T0))
    )

    warns = _furnish_warns(root, _commits_entry())

    assert any("local commits made while not primary" in w for w in warns)
    assert any(_OTHER_HOST in w for w in warns)


def test_data_warnings_silent_when_this_host_is_primary(
    commits_hq, commits_this_host, monkeypatch, tmp_path
):
    root = _furnish_drift_repo(tmp_path, track_beads=False)
    repo = root / "github" / "acme" / "zf"
    _commit_on_dolt_data(repo, "after-handoff", at=_T0 + 60)
    monkeypatch.setattr(host_lease.time, "time", lambda: _T0 + 200)
    _commits_record_lease(
        commits_hq, _commits_lease(_THIS_HOST, adopted_at=host_lease.now_stamp(_T0))
    )

    warns = _furnish_warns(root, _commits_entry())

    assert not any("local commits made while not primary" in w for w in warns)


def test_local_commits_check_reuses_guard_primary_state(monkeypatch, tmp_path):
    """DRY guard: the doctor check must go through `guard.primary_state`, not reinvent its
    own primacy read — else the hook and doctor could silently disagree."""
    calls = []

    def spy(*, cfg=None, entry=None):
        calls.append(entry)
        return None

    monkeypatch.setattr(guard, "primary_state", spy)
    doctor._local_commits_while_not_primary({}, _commits_entry(), tmp_path)
    assert calls == [_commits_entry()]


# ---- home layout drift (bh-cmqp.3) --------------------------------------------------------
#
# `_sandbox_bh_home` (conftest.py, autouse) already isolates `config.home()` to a per-test
# tmpdir seeded with a bare config.yaml — these tests add/remove entries under THAT dir, never
# a real one, and drive `_data_layout`/`_data_warnings` with an explicit `cfg` dict rather than
# `config.load()` so `worktrees.ephemeral`/`worktrees.path` can vary per test without writing
# YAML.


def test_layout_clean_default_host_has_no_findings():
    """A freshly-seeded home (just config.yaml, ephemeral worktrees — the conftest default)
    reports nothing: every fixed/known entry is either absent or accounted for."""
    d = doctor._data_layout({})
    assert d == {"unclassified": [], "legacy_worktrees_root": None}


def test_layout_flags_an_unrecognized_entry():
    (config.home() / "mystery-dir").mkdir()
    d = doctor._data_layout({})
    assert d["unclassified"] == ["mystery-dir"]


def test_layout_known_fixed_entries_are_not_flagged():
    home = config.home()
    dirs = ("hq-backups", "backups", "retros")
    for name in doctor._KNOWN_HOME_ENTRIES:
        (home / name).mkdir() if name in dirs else (home / name).touch()
    d = doctor._data_layout({})
    assert d["unclassified"] == []


def test_layout_configurable_entry_resolved_dynamically_not_hardcoded():
    """hq/hub/cache aren't in the fixed known-entries set — their expected name comes from
    their own accessor, so a differently-named-but-still-under-home() store isn't flagged."""
    home = config.home()
    (home / "hq").mkdir()  # config.hq_dir() default — matches without any cfg override
    d = doctor._data_layout({})
    assert d["unclassified"] == []


def test_layout_persistent_worktrees_root_is_not_flagged_as_unclassified():
    home = config.home()
    (home / "wt").mkdir()
    cfg = {"worktrees": {"ephemeral": False, "path": str(home / "wt")}}
    d = doctor._data_layout(cfg)
    assert d["unclassified"] == []


def test_layout_ephemeral_worktrees_root_still_flagged_when_present():
    """Ephemeral mode's root lives outside home() (OS temp) — a directory under home() with
    that name is NOT the active root and stays unclassified."""
    home = config.home()
    (home / "wt").mkdir()
    d = doctor._data_layout({})  # ephemeral defaults True — worktrees_root() is OS-temp
    assert d["unclassified"] == ["wt"]


def test_legacy_worktrees_root_detected_when_active_root_differs():
    home = config.home()
    (home / "worktrees").mkdir()  # the pre-worktrees.path default fallback, now stale
    cfg = {"worktrees": {"ephemeral": False, "path": str(home / "wt")}}
    d = doctor._data_layout(cfg)
    assert d["legacy_worktrees_root"] == str(home / "worktrees")
    assert d["unclassified"] == []  # gets its own warning, not double-reported as unclassified


def test_legacy_worktrees_root_absent_is_not_reported():
    cfg = {"worktrees": {"ephemeral": False, "path": str(config.home() / "wt")}}
    assert doctor._data_layout(cfg)["legacy_worktrees_root"] is None


def test_legacy_worktrees_root_ignored_when_worktrees_ephemeral():
    (config.home() / "worktrees").mkdir()
    assert doctor._data_layout({})["legacy_worktrees_root"] is None


def test_legacy_worktrees_root_none_when_it_IS_the_active_root():
    home = config.home()
    (home / "worktrees").mkdir()
    cfg = {"worktrees": {"ephemeral": False, "path": str(home / "worktrees")}}
    assert doctor._data_layout(cfg)["legacy_worktrees_root"] is None


def test_data_warnings_includes_layout_findings(tmp_path):
    home = config.home()
    (home / "worktrees").mkdir()
    (home / "mystery-dir").mkdir()
    cfg = {"worktrees": {"ephemeral": False, "path": str(home / "wt")}}

    warns = doctor._data_warnings(cfg, tmp_path, [], set(), set(), set(), set())

    assert any("legacy worktrees root" in w for w in warns)
    assert any("unrecognized ~/.beadhive entry" in w and "mystery-dir" in w for w in warns)


# ---- HQ ahead-of-remote warning (bh-z9hl acceptance: doctor/ready surfaces drift) --------


def _hq_cfg(*, registered: bool = True) -> dict:
    entry = {"provider": "local", "org": "factory", "repo": "hq", "prefix": "hq", "kind": "hq"}
    return {"managed_repos": [entry] if registered else []}


def _wired_hq(tmp_path) -> Path:
    """A real HQ working tree pushed (with `-u`) to a real local bare remote."""
    hq_dir = tmp_path / "hq"
    hq_dir.mkdir()
    _git("init", "-q", "-b", "main", cwd=hq_dir)
    _git("config", "user.email", "t@hq", cwd=hq_dir)
    _git("config", "user.name", "T", cwd=hq_dir)
    (hq_dir / "f.txt").write_text("a\n")
    _git("add", ".", cwd=hq_dir)
    _git("commit", "-qm", "init", cwd=hq_dir)

    remote = tmp_path / "remote.git"
    _git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    _git("remote", "add", "origin", str(remote), cwd=hq_dir)
    _git("push", "-q", "-u", "origin", "main", cwd=hq_dir)
    return hq_dir


def test_hq_ahead_warning_fires_when_main_is_ahead(tmp_path, monkeypatch):
    hq_dir = _wired_hq(tmp_path)
    (hq_dir / "f.txt").write_text("b\n")
    _git("commit", "-aqm", "drift", cwd=hq_dir)
    monkeypatch.setattr(doctor.config, "hq_dir", lambda: hq_dir)

    warns = doctor._hq_ahead_warnings(_hq_cfg())

    assert len(warns) == 1
    assert "1 commit(s) ahead of origin/main" in warns[0]
    assert f"{config.BINARY_ALIAS} hq push" in warns[0]


def test_hq_ahead_warning_silent_when_clean(tmp_path, monkeypatch):
    hq_dir = _wired_hq(tmp_path)
    monkeypatch.setattr(doctor.config, "hq_dir", lambda: hq_dir)

    assert doctor._hq_ahead_warnings(_hq_cfg()) == []


def test_hq_ahead_warning_silent_when_hq_not_registered(tmp_path, monkeypatch):
    hq_dir = _wired_hq(tmp_path)
    (hq_dir / "f.txt").write_text("b\n")
    _git("commit", "-aqm", "drift", cwd=hq_dir)
    monkeypatch.setattr(doctor.config, "hq_dir", lambda: hq_dir)

    assert doctor._hq_ahead_warnings(_hq_cfg(registered=False)) == []


def test_hq_ahead_warning_silent_when_no_local_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.config, "hq_dir", lambda: tmp_path / "nope")

    assert doctor._hq_ahead_warnings(_hq_cfg()) == []


def test_hq_ahead_warning_feeds_data_warnings(tmp_path, monkeypatch):
    """The HQ-ahead check is wired into `_data_warnings` (what `bh doctor` actually renders),
    not just callable in isolation."""
    hq_dir = _wired_hq(tmp_path)
    (hq_dir / "f.txt").write_text("b\n")
    _git("commit", "-aqm", "drift", cwd=hq_dir)
    monkeypatch.setattr(doctor.config, "hq_dir", lambda: hq_dir)

    warns = doctor._data_warnings(_hq_cfg(), tmp_path, [], set(), set(), set(), set())

    assert any("ahead of origin/main" in w for w in warns)
