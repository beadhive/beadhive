"""Characterization tests for `beadhive.cli` — the Typer app wiring itself.

cli.py is a 32-dependent, zero-test-file hotspot (Repowise `untested_hotspot`, bh-3oq2.3).
Per-command coverage for most verbs already lives in topic-specific files (test_setup.py for
the setup gate, test_otel_cli_span.py / test_otel_cli_instrument.py for the otel
instrumentation inside `_root`, test_config.py for the migration/staleness-warning call
sites, test_hive_archive.py / test_mcp_install.py / test_config_validate_cli.py for their
respective verbs). This file locks in the remaining gaps ahead of the extract_method
refactor of `_root`, `hive_onboard`/`hive_init`'s shared conflict guard:

- `_root`'s -a/--hive routing-flag gate: both flags, both the reject and the accept path.
- `_root`'s per-step best-effort try/except swallowing (a raising migration/otel-init step
  must never crash the CLI).
- `hive_onboard`'s --claude/--skills plugin-mode conflict guard (hive_init's twin is already
  covered by test_hive_plugin_installer.py) and each command's kwarg forwarding into
  `hive.onboard` / `hive.init`, so the extract_method pass can't silently drop or reorder
  a flag.

No behavior is asserted beyond what the current code does — this is a safety net, not a spec
for new behavior.
"""

from __future__ import annotations

from typer.testing import CliRunner

from beadhive import cli, config, home_migration
from beadhive.cli import app

runner = CliRunner()


# ---- _root: -a/--hive routing-flag gate ---------------------------------------


def test_dash_a_rejected_on_nonpassthrough_command():
    """-a only applies to `bd`/`git`; any other verb is rejected before it runs."""
    result = runner.invoke(app, ["-a", "doctor"])
    assert result.exit_code == 1
    assert "-a/--all and --hive only apply to" in result.output


def test_hive_flag_rejected_on_nonpassthrough_command():
    """--hive is rejected the same way -a is (currently only -a had a direct cli.py test)."""
    result = runner.invoke(app, ["--hive", "myrepo", "doctor"])
    assert result.exit_code == 1
    assert "-a/--all and --hive only apply to" in result.output


def test_dash_a_accepted_on_bd_passthrough():
    """-a is valid ahead of `bd`/`git` — the gate must not fire, and ctx.obj carries mode='all'."""
    result = runner.invoke(app, ["-a", "bd", "--help"])
    assert "-a/--all and --hive only apply to" not in result.output


def test_hive_flag_accepted_on_git_passthrough():
    result = runner.invoke(app, ["--hive", "myrepo", "git", "--help"])
    assert "-a/--all and --hive only apply to" not in result.output


def test_no_routing_flags_is_a_plain_cwd_invocation():
    """Baseline: no -a/--hive at all never trips the gate on any verb."""
    result = runner.invoke(app, ["doctor"])
    assert "-a/--all and --hive only apply to" not in result.output


# ---- _root: best-effort steps must never crash the CLI ------------------------


def test_root_survives_a_raising_home_migration(monkeypatch):
    """migrate_home_if_needed is wrapped in a bare try/except in `_root` — a raise there must
    not take down the CLI (bh-sn9q placement rule: best-effort, never blocks a real command)."""

    def _boom():
        raise RuntimeError("disk full")

    monkeypatch.setattr(home_migration, "migrate_home_if_needed", _boom)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0


def test_root_survives_a_raising_hive_key_migration(monkeypatch):
    def _boom():
        raise RuntimeError("bad config")

    monkeypatch.setattr(config, "migrate_hive_keys_if_needed", _boom)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0


def test_root_survives_a_raising_otel_init(monkeypatch):
    """config.load()/observaloop_env/otel.init are wrapped together in `_root` — telemetry
    init failing must degrade to telemetry-off, not break the invoked command."""

    def _boom(_cfg):
        raise RuntimeError("otel exporter unreachable")

    monkeypatch.setattr(cli.otel, "init", _boom)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0


# ---- hive_onboard: --claude/--skills conflict guard ----------------------------


def test_hive_onboard_rejects_claude_and_skills_in_plugin_mode(monkeypatch):
    """hive_onboard carries the same plugin-mode --claude/--skills guard as hive_init
    (currently duplicated inline in both commands) — characterize both call sites before any
    dedup extraction."""
    monkeypatch.setattr(config, "load", lambda: {"claude": {"source": "plugin"}})
    monkeypatch.setattr(config, "claude_source", lambda _cfg: "plugin")

    result = runner.invoke(
        app, ["hive", "onboard", "github/acme/widget", "--claude", "--skills"]
    )

    assert result.exit_code != 0
    combined = result.output.lower()
    assert "plugin" in combined or "skills" in combined


def test_hive_onboard_allows_claude_and_skills_outside_plugin_mode(monkeypatch):
    """The guard is plugin-mode-specific: --claude --skills together is fine on the default
    (copy) source."""
    captured = {}

    def _fake_onboard(hive_id, **kwargs):
        captured["hive_id"] = hive_id
        captured.update(kwargs)

    monkeypatch.setattr(config, "load", lambda: {"claude": {"source": "copy"}})
    monkeypatch.setattr("beadhive.hive.onboard", _fake_onboard)

    result = runner.invoke(
        app,
        ["hive", "onboard", "github/acme/widget", "--claude", "--skills", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert captured["claude"] is True
    assert captured["skills"] is True


# ---- hive_onboard / hive_init: kwarg forwarding into hive.onboard / hive.init --


def test_hive_onboard_forwards_flags_to_hive_onboard(monkeypatch):
    captured = {}

    def _fake_onboard(hive_id, **kwargs):
        captured["hive_id"] = hive_id
        captured.update(kwargs)

    monkeypatch.setattr("beadhive.hive.onboard", _fake_onboard)

    result = runner.invoke(
        app,
        [
            "hive",
            "onboard",
            "github/acme/widget",
            "--clone-url",
            "https://example.com/acme/widget.git",
            "-f",
            "--yes",
            "--kind",
            "external",
            "--prefix",
            "wg",
            "--plugin",
            "orca",
            "--dry-run",
            "--skip-check",
            "dirty-tree",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["hive_id"] == "github/acme/widget"
    assert captured["clone_url"] == "https://example.com/acme/widget.git"
    assert captured["force"] is True
    assert captured["yes"] is True
    assert captured["kind"] == "external"
    assert captured["prefix"] == "wg"
    assert captured["plugins"] == ["orca"]
    assert captured["dry_run"] is True
    assert captured["skip_check"] == "dirty-tree"


def test_hive_init_forwards_flags_to_hive_init(monkeypatch):
    captured = {}

    def _fake_init(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("beadhive.hive.init", _fake_init)

    result = runner.invoke(
        app,
        [
            "hive",
            "init",
            "-f",
            "--yes",
            "--kind",
            "external",
            "--prefix",
            "wg",
            "--plugin",
            "orca",
            "--dry-run",
            "--skip-check",
            "dirty-tree",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["force"] is True
    assert captured["yes"] is True
    assert captured["kind"] == "external"
    assert captured["prefix"] == "wg"
    assert captured["plugins"] == ["orca"]
    assert captured["dry_run"] is True
    assert captured["skip_check"] == "dirty-tree"
