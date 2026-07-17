"""`bh toolchain` group + shared payload producers (bh-d0kb, knowledge-only).

Everything runs against faked seams — config.load pinned, the registry cwd/hive resolvers
stubbed, and the single run() seam faked (no just/npm/make binaries needed). Verified:

- `list --json` payload ({declared, registry}) + the human render marking declared names;
- `show` runs the template's entrypoints_cmd through run() in the hive main clone and
  bundles the raw listing with the propose-only suggestions;
- `show` on an unknown toolchain errors cleanly (exit 1, named in the message);
- `exec` passes argv + cwd through run() and the exit code through as bh's own;
- `exec` refuses an empty argv (nothing invoked) and maps a missing binary to ✗ exit 1.
"""

from __future__ import annotations

import json
from collections import namedtuple
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import config, toolchain
from beadhive import registry as registry_mod
from beadhive.cli import app

runner = CliRunner()

_CP = namedtuple("CP", "returncode stdout stderr")
_CFG = {"worktrees": {"toolchain": "just"}}


@pytest.fixture
def stubbed(monkeypatch):
    """Pin config + the hive resolvers so no real workspace/hive is needed."""
    monkeypatch.setattr(config, "load", lambda: _CFG)
    monkeypatch.setattr(registry_mod, "current_hive", lambda cfg: {})
    monkeypatch.setattr(registry_mod, "hive_dir_for", lambda cfg, hive="": Path("/fake/hive"))


def _fake_run(monkeypatch, returncode=0, stdout=""):
    """Fake toolchain's run() seam; returns the recorded (cmd, kwargs) calls."""
    calls: list[tuple[list, dict]] = []

    def fake(cmd, **kw):
        calls.append((cmd, kw))
        return _CP(returncode, stdout, "")

    monkeypatch.setattr(toolchain, "run", fake)
    return calls


# ---- bh toolchain list -------------------------------------------------------


def test_list_json_payload(stubbed):
    res = runner.invoke(app, ["toolchain", "list", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.stdout)
    assert payload["declared"] == ["just"]
    assert set(payload["registry"]) >= {"just", "uv", "npm", "make"}
    assert payload["registry"]["just"]["entrypoints_cmd"] == "just --list"


def test_list_human_render_marks_declared_names(stubbed):
    res = runner.invoke(app, ["toolchain", "list"])
    assert res.exit_code == 0
    assert "declared: just" in res.output
    assert "● just" in res.output  # declared
    assert "○ npm" in res.output  # in the registry, not declared


# ---- bh toolchain show -------------------------------------------------------


def test_show_runs_entrypoints_cmd_in_hive_main_clone(stubbed, monkeypatch):
    calls = _fake_run(monkeypatch, stdout="check\nlint\n")
    res = runner.invoke(app, ["toolchain", "show", "just", "--json"])
    assert res.exit_code == 0
    ((cmd, kw),) = calls
    assert cmd == ["just", "--list"]
    assert kw["cwd"] == str(Path("/fake/hive"))
    payload = json.loads(res.stdout)
    assert payload["name"] == "just"
    assert payload["entrypoints"] == "check\nlint\n"
    assert payload["exit_code"] == 0
    assert payload["suggestions"]["validate_cmd"] == "just check"
    (init_rule,) = payload["suggestions"]["init"]
    assert init_rule["if_exists"] == "justfile"


def test_show_unknown_toolchain_errors_cleanly(stubbed, monkeypatch):
    calls = _fake_run(monkeypatch)
    res = runner.invoke(app, ["toolchain", "show", "gradle"])
    assert res.exit_code == 1
    assert "unknown toolchain 'gradle'" in res.output
    assert not calls  # nothing ran


def test_show_command_not_found_errors_cleanly(stubbed, monkeypatch):
    def fake(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(toolchain, "run", fake)
    res = runner.invoke(app, ["toolchain", "show", "just"])
    assert res.exit_code == 1
    assert "command not found" in res.output


# ---- bh toolchain exec -------------------------------------------------------


def test_exec_passes_argv_and_exit_code_through(stubbed, monkeypatch):
    calls = _fake_run(monkeypatch, returncode=3)
    res = runner.invoke(app, ["toolchain", "exec", "--", "npm", "run", "lint"])
    assert res.exit_code == 3  # the entrypoint's exit code passes through
    ((cmd, kw),) = calls
    assert cmd == ["npm", "run", "lint"]
    assert kw["cwd"] == str(Path("/fake/hive"))


def test_exec_refuses_empty_argv(stubbed, monkeypatch):
    calls = _fake_run(monkeypatch)
    res = runner.invoke(app, ["toolchain", "exec"])
    assert res.exit_code == 1
    assert "empty argv" in res.output
    assert not calls  # nothing invoked


def test_exec_command_not_found_errors_cleanly(stubbed, monkeypatch):
    def fake(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(toolchain, "run", fake)
    res = runner.invoke(app, ["toolchain", "exec", "--", "nosuchtool"])
    assert res.exit_code == 1
    assert "command not found" in res.output
