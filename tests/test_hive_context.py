"""`bh rig context` — registry-driven AGF steering payload for session hooks.

Inside a registered rig it emits steering text (or the SessionStart hook JSON envelope with
--hook-json); outside a rig or in an unregistered repo it prints nothing and exits 0 — hook
consumers must never break a session start.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from beadhive import config, hive
from harness.world import git


def _make_repo(world, *, org="myorg", repo="myrepo"):
    main = world.ws_root / "github" / org / repo
    main.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=main)
    world.chdir(main)
    return main


def _register(world, *, org="myorg", repo="myrepo", prefix="mr", kind="personal", furnish=""):
    cfg = config.load()
    entry = {"provider": "github", "org": org, "repo": repo, "prefix": prefix, "kind": kind}
    if furnish:
        entry["furnish"] = furnish
    cfg.setdefault("managed_repos", []).append(entry)
    config.save(cfg)


def test_agf_context_in_registered_hive_carries_steering_and_facts(world):
    _make_repo(world)
    _register(world, furnish="none")

    payload = hive.agf_context()

    assert payload is not None
    assert payload["prefix"] == "mr"
    assert payload["kind"] == "personal"
    assert payload["furnish"] == "none"
    assert "AGF" in payload["text"]
    assert "`mr`" in payload["text"] and "footprint `none`" in payload["text"]
    assert "<!--" not in payload["text"]  # managed markers stay out of the hook payload


def test_agf_context_none_when_unregistered_or_outside(world):
    _make_repo(world)  # git repo under $GIT_WORKSPACE but never registered
    assert hive.agf_context() is None

    world.chdir(world.ws_root)  # not a git repo at all
    assert hive.agf_context() is None


def test_cli_hook_json_envelope(world):
    from beadhive.cli import app

    _make_repo(world)
    _register(world)

    res = CliRunner().invoke(app, ["hive", "context", "--hook-json"])

    assert res.exit_code == 0
    envelope = json.loads(res.output)
    hso = envelope["hookSpecificOutput"]
    assert hso["hookEventName"] == "SessionStart"
    assert "AGF" in hso["additionalContext"]


def test_cli_silent_zero_exit_outside_a_hive(world):
    from beadhive.cli import app

    _make_repo(world)  # unregistered repo

    res = CliRunner().invoke(app, ["hive", "context", "--hook-json"])

    assert res.exit_code == 0
    assert res.output.strip() == ""


def test_cli_silent_zero_exit_on_internal_error(world, monkeypatch):
    # Hook safety: ANY failure inside the payload builder degrades to silent exit 0.
    from beadhive.cli import app

    _make_repo(world)
    monkeypatch.setattr(
        hive, "agf_context", lambda cwd=None: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    res = CliRunner().invoke(app, ["hive", "context"])

    assert res.exit_code == 0
    assert res.output.strip() == ""
