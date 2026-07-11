"""bh plugin CLI tree + rig --plugin flag (bead .9).

Drives the app via Typer's CliRunner (in-process import of beadhive — NOT the installed bh
binary). Verifies:

- ``bh plugin orca sync --dry-run`` runs end-to-end over a populated fake $GIT_WORKSPACE.
- ``rig onboard <triplet> --plugin orca --dry-run`` shows the ``plugin-orca`` step in the plan.
- ``rig enable orca <rig>`` / ``rig disable orca <rig>`` round-trip (generic feature-flag verbs,
  no orca-specific CLI code).
"""

from __future__ import annotations

from typer.testing import CliRunner

from beadhive import config, orca, registry
from beadhive.cli import app
from harness.world import git

runner = CliRunner()


def _register(world, *, prefix="mr", org="myorg", repo="myrepo"):
    cfg = config.load()
    cfg.setdefault("managed_repos", []).append(
        {"provider": "github", "org": org, "repo": repo, "prefix": prefix, "kind": "personal"}
    )
    config.save(cfg)


def _make_repo(world, *, org="acme", repo="widget"):
    target = world.ws_root / "github" / org / repo
    target.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=target)
    git("config", "user.email", "t@ws.dev", cwd=target)
    git("config", "user.name", "T", cwd=target)
    (target / "README.md").write_text("hi")
    git("add", ".", cwd=target)
    git("commit", "-q", "-m", "init", cwd=target)
    (target / ".beads").mkdir()
    return target


# ---- bh plugin orca sync ----------------------------------------------------


def test_plugin_orca_sync_dry_run(world, monkeypatch):
    clone = world.ws_root / "github" / "acme" / "api"
    clone.mkdir(parents=True)
    (clone / ".git").mkdir()
    monkeypatch.setattr(orca, "is_available", lambda cfg=None: True)
    monkeypatch.setattr(orca, "list_repos", lambda cfg=None: [])

    result = runner.invoke(app, ["plugin", "orca", "sync", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "would register" in result.output
    assert str(clone) in result.output


def test_plugin_tree_help_lists_orca(world):
    result = runner.invoke(app, ["plugin", "--help"])
    assert result.exit_code == 0
    assert "orca" in result.output


# ---- bh plugin orca fix-settings ---------------------------------------------


def test_plugin_orca_fix_settings_refuses_when_runtime_up(world, monkeypatch):
    monkeypatch.setattr(orca, "_runtime_ready", lambda cfg=None: True)

    result = runner.invoke(app, ["plugin", "orca", "fix-settings"])

    assert result.exit_code != 0
    assert "Settings UI" in result.output


def test_plugin_orca_fix_settings_flips_value_when_runtime_down(world, monkeypatch, tmp_path):
    monkeypatch.setattr(orca, "_runtime_ready", lambda cfg=None: False)
    data = tmp_path / "orca-data.json"
    data.write_text('{"settings": {"autoRenameBranchFromWork": true}, "repos": []}')
    monkeypatch.setattr(config, "orca_data_path", lambda cfg=None: data)

    result = runner.invoke(app, ["plugin", "orca", "fix-settings"])

    assert result.exit_code == 0, result.output
    import json as _json

    assert _json.loads(data.read_text())["settings"]["autoRenameBranchFromWork"] is False


# ---- rig onboard --plugin ---------------------------------------------------


def test_rig_onboard_plugin_flag_shows_step(world, monkeypatch):
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "prototype")
    _make_repo(world)

    result = runner.invoke(
        app, ["rig", "onboard", "github/acme/widget", "--plugin", "orca", "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert "plugin-orca" in result.output


# ---- rig enable/disable orca (generic feature-flag verbs) -------------------


def _orca_flag(entry) -> object:
    return (entry.get("orca") or {}).get("enabled")


def test_rig_enable_disable_orca_roundtrip(world):
    _register(world)

    r1 = runner.invoke(app, ["rig", "enable", "orca", "mr"])
    assert r1.exit_code == 0, r1.output
    entry = next(e for e in config.load()["managed_repos"] if e["prefix"] == "mr")
    assert _orca_flag(entry) is True

    r2 = runner.invoke(app, ["rig", "disable", "orca", "mr"])
    assert r2.exit_code == 0, r2.output
    entry = next(e for e in config.load()["managed_repos"] if e["prefix"] == "mr")
    assert _orca_flag(entry) is False
