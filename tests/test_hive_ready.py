"""`ws hive ready` — read-only AGF readiness verdict + breakdown.

Required core-AGF checks fail the command (exit 1); optional integrations are shown but
never fail. With otel disabled (the test default) observaloop is N/A — no live probe runs.
"""

from __future__ import annotations

import json

import pytest
import typer

from beadhive import config, hive_ready
from harness.world import git


def _make_repo(world, *, org="myorg", repo="myrepo"):
    main = world.ws_root / "github" / org / repo
    main.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=main)
    (main / ".beads").mkdir()
    world.chdir(main)
    return main


def _register(
    world, *, org="myorg", repo="myrepo", prefix="mr", kind="personal", furnish="", hq=True
):
    cfg = config.load()
    entry = {"provider": "github", "org": org, "repo": repo, "prefix": prefix, "kind": kind}
    if furnish:
        entry["furnish"] = furnish
    cfg.setdefault("managed_repos", []).append(entry)
    if hq:
        # A registered escalation parent (kind=hq) is a required check (bh-ufne).
        cfg["managed_repos"].append(
            {
                "provider": "local",
                "org": "factory",
                "repo": "hq",
                "prefix": "hq",
                "kind": "hq",
            }
        )
    config.save(cfg)


def _fake_plugin(world):
    """Point BH_PLUGIN_DIR at a minimal plugin tree — the plugin is no longer vendored in-repo
    (beadhive/claude-plugin is canonical), so tests supply their own skills/agents source."""
    root = world.tmp / "fake-plugin"
    (root / "skills" / "demo-skill").mkdir(parents=True)
    (root / "skills" / "demo-skill" / "SKILL.md").write_text("skill\n")
    (root / "agents").mkdir()
    (root / "agents" / "developer.md").write_text("agent\n")
    world._monkeypatch.setenv("BH_PLUGIN_DIR", str(root))
    return root


def _make_ready(world, *, hq=True):
    """Fully-set-up core-AGF hive: registered (furnished) + claude settings + skills + agents."""
    _fake_plugin(world)
    main = _make_repo(world)
    _register(world, hq=hq)
    (main / ".claude").mkdir()
    (main / ".claude" / "settings.json").write_text("{}\n")
    # one real bundled skill name so the skills check resolves
    name = next(p.name for p in config.skills_src().iterdir() if p.is_dir())
    (main / "skills" / name).mkdir(parents=True)
    # one real bundled agent def so the agents check resolves
    agent_name = next(p.name for p in config.agents_src().iterdir() if p.suffix == ".md")
    (main / ".claude" / "agents").mkdir(parents=True)
    (main / ".claude" / "agents" / agent_name).write_text("agent\n")
    return main


def _run(verbose=False):
    """Call run_check, returning the typer.Exit code (0 ready / 1 not)."""
    with pytest.raises(typer.Exit) as exc:
        hive_ready.run_check(verbose)
    return exc.value.exit_code


def test_unregistered_repo_not_ready(world, capsys):
    _make_repo(world)  # git repo but never `hive init`ed

    assert _run() == 1
    out = capsys.readouterr().out
    assert "not ready" in out


def test_fully_set_up_hive_is_ready(world, capsys):
    _make_ready(world)

    assert _run() == 0
    assert "ready for AGF" in capsys.readouterr().out


def test_missing_required_fails(world):
    main = _make_ready(world)
    # A furnished hive (missing `furnish` key + non-fork kind infers "full") requires the
    # tracked claude settings.
    (main / ".claude" / "settings.json").unlink()

    assert _run() == 1


def test_zero_footprint_hive_is_ready_without_repo_files(world):
    """A declared zero-footprint hive is green with no tracked furniture at all."""
    _fake_plugin(world)
    _make_repo(world)
    _register(world, furnish="none")

    assert _run() == 0


def test_missing_hq_escalation_parent_fails(world, capsys):
    """No kind=hq hive registered → the required escalation-parent check fails the gate,
    pointing at `bh hq init` (bh-ufne)."""
    _make_ready(world, hq=False)

    assert _run(verbose=True) == 1
    out = capsys.readouterr().out
    assert "✗ escalation parent" in out
    assert "hq init" in out


def test_hq_escalation_parent_check_line_ok(world, capsys):
    """With a registered HQ the escalation-parent line is green under # Required."""
    _make_ready(world)

    assert _run(verbose=True) == 0
    assert "✓ escalation parent" in capsys.readouterr().out


def test_prime_md_presence_warns_but_never_fails(world, capsys):
    main = _make_ready(world)
    (main / ".beads" / "PRIME.md").write_text("legacy\n")

    assert _run(verbose=True) == 0  # warn-level only
    assert "deprecated" in capsys.readouterr().out


def test_bd_claude_block_presence_warns_but_never_fails(world, capsys):
    main = _make_ready(world)
    (main / "CLAUDE.md").write_text(
        "<!-- BEGIN BEADS INTEGRATION v:1 profile:full hash:6cd5cc61 -->\nstale\n"
    )

    assert _run(verbose=True) == 0  # warn-level only
    assert "BEADS INTEGRATION block present" in capsys.readouterr().out


def test_verbose_breakdown_sections_and_optional_na(world, capsys):
    _make_ready(world)

    assert _run(verbose=True) == 0
    out = capsys.readouterr().out
    assert "# Required" in out and "# Optional" in out
    assert "✓ hive registered" in out
    # otel off → observaloop is N/A (-), never probed; hints absent → optional •
    assert "- observaloop profile" in out
    assert "• AGENTS.md hint" in out


def test_cli_exit_codes(world):
    from typer.testing import CliRunner

    from beadhive.cli import app

    _make_ready(world)
    assert CliRunner().invoke(app, ["hive", "ready"]).exit_code == 0

    (world.ws_root / "github" / "myorg" / "myrepo" / ".claude" / "settings.json").unlink()
    assert CliRunner().invoke(app, ["hive", "ready"]).exit_code == 1


# ---------------------------------------------------------------------------
# Generic plugin readiness line (bead .8) — orca is N/A when disabled,
# ok/missing (live list_repos probe) when enabled.
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from beadhive import hive_ready as _rr  # noqa: E402
from beadhive import orca  # noqa: E402

_ENTRY = {"provider": "github", "org": "acme", "repo": "api", "prefix": "a-api"}


def test_plugin_line_na_when_orca_disabled(world, monkeypatch):
    # default config: git_workspace off → orca_enabled False → N/A (never probed).
    monkeypatch.setattr(config, "orca_enabled", lambda cfg, e=None: False)
    checks = _rr._plugin_checks({}, _ENTRY)
    line = next(c for c in checks if c.label == "orca")
    assert line.state == "na"
    assert line.detail == "disabled"


def test_plugin_line_ok_when_registered(world, monkeypatch):
    monkeypatch.setattr(config, "orca_enabled", lambda cfg, e=None: True)
    clone = Path(orca.workspace_root()) / "github" / "acme" / "api"
    monkeypatch.setattr(orca, "list_repos", lambda cfg=None: [{"path": str(clone)}])
    line = next(c for c in _rr._plugin_checks({}, _ENTRY) if c.label == "orca")
    assert line.state == "ok"
    assert line.detail == "registered"


def test_plugin_line_missing_when_not_registered(world, monkeypatch):
    monkeypatch.setattr(config, "orca_enabled", lambda cfg, e=None: True)
    monkeypatch.setattr(orca, "list_repos", lambda cfg=None: [])
    line = next(c for c in _rr._plugin_checks({}, _ENTRY) if c.label == "orca")
    assert line.state == "missing"


# ---------------------------------------------------------------------------
# validate_cmd nudge (bh-l44i, reworked): unconfigured + RESOLVED-test-free warns; a named
# override (even a compile-only one), a resolved test-runner, or anything unresolvable
# (no justfile, non-`just` command) stays ok. See validate_probe / test_validate_probe.py for
# the resolution itself — these pin the Check wiring on top of it.
# ---------------------------------------------------------------------------

# Same shape as this repo's own justfile: `check` transitively reaches a real test runner.
_TESTED_JUSTFILE = "check: lint test\n\nlint:\n    ruff check\n\ntest:\n    uv run pytest\n"
# `check` never reaches anything test-shaped — genuinely compile-only.
_COMPILE_ONLY_JUSTFILE = "check: lint typecheck\n\nlint:\n    ruff check\n\ntypecheck:\n    mypy\n"


def test_validate_cmd_check_warns_on_unconfigured_resolved_test_free_default(tmp_path):
    (tmp_path / "justfile").write_text(_COMPILE_ONLY_JUSTFILE)
    line = _rr._validate_cmd_check({}, {}, tmp_path)
    assert line.state == "warn"
    assert "just check" in line.detail
    assert "does not look like it runs tests" in line.detail


def test_validate_cmd_check_ok_when_explicitly_configured(tmp_path):
    cfg = {"work": {"validate_cmd": "just check"}}  # same text, but a deliberate choice
    (tmp_path / "justfile").write_text(_COMPILE_ONLY_JUSTFILE)  # would warn if it were consulted
    line = _rr._validate_cmd_check(cfg, {}, tmp_path)
    assert line.state == "ok"
    assert "configured" in line.detail


def test_validate_cmd_check_ok_when_default_resolves_to_tests(tmp_path):
    (tmp_path / "justfile").write_text(_TESTED_JUSTFILE)
    line = _rr._validate_cmd_check({}, {}, tmp_path)
    assert line.state == "ok"
    assert "runs tests" in line.detail


def test_validate_cmd_check_ok_when_default_looks_like_tests_directly(monkeypatch, tmp_path):
    monkeypatch.setattr(
        config, "validate_cmd", lambda cfg, e, phase=None, main_gate=False: "just test"
    )
    line = _rr._validate_cmd_check({}, {}, tmp_path)
    assert line.state == "ok"


def test_validate_cmd_check_ok_when_unresolvable(tmp_path):
    """PINNED (bh-l44i rework): no justfile at all -> can't resolve -> never warn. An
    unconfirmed guess ("compile-only") is exactly the false-positive this rework removes."""
    line = _rr._validate_cmd_check({}, {}, tmp_path)
    assert line.state == "ok"


def test_scan_includes_validate_cmd_line_ok_when_resolved_to_tests(world, monkeypatch):
    """PINNED (bh-l44i rework acceptance): `just check` with a justfile whose `check` recipe
    transitively runs pytest — this repo's own dominant shape — must NOT warn."""
    main = _make_ready(world)
    (main / "justfile").write_text(_TESTED_JUSTFILE)
    cfg = config.load()
    entry = {
        "provider": "github",
        "org": "myorg",
        "repo": "myrepo",
        "prefix": "mr",
        "kind": "personal",
    }
    checks = hive_ready.scan(cfg, ("github", "myorg", "myrepo"), entry, main)
    line = next(c for c in checks if c.label == "validate_cmd")
    assert line.state == "ok"


def test_scan_includes_validate_cmd_line_warns_when_resolved_test_free(world, monkeypatch):
    main = _make_ready(world)
    (main / "justfile").write_text(_COMPILE_ONLY_JUSTFILE)
    cfg = config.load()
    entry = {
        "provider": "github",
        "org": "myorg",
        "repo": "myrepo",
        "prefix": "mr",
        "kind": "personal",
    }
    checks = hive_ready.scan(cfg, ("github", "myorg", "myrepo"), entry, main)
    line = next(c for c in checks if c.label == "validate_cmd")
    assert line.state == "warn"


def test_scan_includes_validate_cmd_line_ok_when_no_justfile(world, monkeypatch):
    """No justfile at all (the un-augmented `_make_ready` fixture) -> unresolvable -> ok, not
    warn — this is the exact fleet-wide false positive the coordinator flagged."""
    main = _make_ready(world)
    cfg = config.load()
    entry = {
        "provider": "github",
        "org": "myorg",
        "repo": "myrepo",
        "prefix": "mr",
        "kind": "personal",
    }
    checks = hive_ready.scan(cfg, ("github", "myorg", "myrepo"), entry, main)
    line = next(c for c in checks if c.label == "validate_cmd")
    assert line.state == "ok"


def test_scan_includes_orca_line(world, monkeypatch):
    main = _make_ready(world)
    monkeypatch.setattr(config, "orca_enabled", lambda cfg, e=None: False)
    cfg = config.load()
    entry = {
        "provider": "github",
        "org": "myorg",
        "repo": "myrepo",
        "prefix": "mr",
        "kind": "personal",
    }
    checks = hive_ready.scan(cfg, ("github", "myorg", "myrepo"), entry, main)
    assert any(c.label == "orca" for c in checks)


# ---- dolt server check (bh-areg.3) -------------------------------------------
# Advisory only (never `missing`/required — never flips the gate's exit code), copying
# `dolt_fix_advisory`'s "informs without blocking" shape per this bead's own DESIGN note: a
# down server is an operational fact that changes hour to hour, not a structural AGF-setup gap.


def _write_dolt_metadata(hive_dir, **fields):
    (hive_dir / ".beads").mkdir(parents=True, exist_ok=True)
    (hive_dir / ".beads" / "metadata.json").write_text(json.dumps(fields))


def test_dolt_server_check_na_for_embedded(tmp_path, monkeypatch):
    """The common case today: embedded mode has no liveness question — 'na', never a probe."""
    _write_dolt_metadata(tmp_path, dolt_mode="embedded")
    monkeypatch.delenv("BEADS_DOLT_SHARED_SERVER", raising=False)

    check = hive_ready._dolt_server_check(tmp_path)

    assert check.state == "na"
    assert check.required is False


def test_dolt_server_check_na_when_no_metadata_at_all(tmp_path, monkeypatch):
    monkeypatch.delenv("BEADS_DOLT_SHARED_SERVER", raising=False)

    check = hive_ready._dolt_server_check(tmp_path)

    assert check.state == "na"


def test_dolt_server_check_ok_when_reachable(tmp_path, monkeypatch):
    _write_dolt_metadata(tmp_path, dolt_mode="server")
    monkeypatch.setattr(
        hive_ready.dolt_health,
        "probe_shared_server",
        lambda **k: hive_ready.dolt_health.ProbeResult(True, "127.0.0.1:3308 reachable"),
    )

    check = hive_ready._dolt_server_check(tmp_path)

    assert check.state == "ok"
    assert check.required is False


def test_dolt_server_check_warns_never_fails_when_unreachable(tmp_path, monkeypatch):
    """Down is real and worth showing, but must never block `bh hive ready`'s exit code —
    it's an operational fact bh does not own (no daemon in the dolt lifecycle path)."""
    _write_dolt_metadata(tmp_path, dolt_mode="server")
    monkeypatch.setattr(
        hive_ready.dolt_health,
        "probe_shared_server",
        lambda **k: hive_ready.dolt_health.ProbeResult(
            False, "127.0.0.1:3308 refused the connection — nothing listening"
        ),
    )

    check = hive_ready._dolt_server_check(tmp_path)

    assert check.state == "warn"
    assert check.required is False
    assert "bd dolt start" in check.detail


def test_dolt_server_check_warns_on_engine_metadata_mismatch(tmp_path, monkeypatch):
    _write_dolt_metadata(tmp_path, dolt_mode="embedded")
    monkeypatch.setenv("BEADS_DOLT_SHARED_SERVER", "1")

    check = hive_ready._dolt_server_check(tmp_path)

    assert check.state == "warn"
    assert check.required is False
    assert "embedded" in check.detail


def test_scan_includes_dolt_server_line(world, monkeypatch):
    """End to end through `scan()`: an unmigrated (embedded) hive's line is 'na', never
    required — the gate's exit code is unaffected."""
    main = _make_ready(world)
    cfg = config.load()
    entry = {
        "provider": "github",
        "org": "myorg",
        "repo": "myrepo",
        "prefix": "mr",
        "kind": "personal",
    }
    checks = hive_ready.scan(cfg, ("github", "myorg", "myrepo"), entry, main)
    line = next(c for c in checks if c.label == "dolt server")
    assert line.state == "na"
    assert line.required is False
