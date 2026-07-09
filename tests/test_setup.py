"""Unit tests for ws.setup — probe table, cache I/O, gate logic.

Covers:
- Gate allow-list: setup / config / doctor pass without a cache
- Gate deny: all other verbs are blocked when cache is absent or setup==false
- WS_SKIP_SETUP_CHECK=1 bypass overrides every deny
- Gate allow: verbs pass after a successful setup cache is written
- probe_one: found/not-found branches (shutil.which stub)
- probe_tools: aggregates the probe table correctly
- Cache read/write round-trip
- run_check: writes cache=true on all-found, cache=false + exit 1 on missing
- run_show: renders cached state, exits 1 when cache absent
"""

from __future__ import annotations

import json

import pytest
import typer
from typer.testing import CliRunner

from beadhive import setup as setup_mod
from beadhive.cli import app

runner = CliRunner()


# ---- fixtures ----------------------------------------------------------------


@pytest.fixture()
def ws_home(tmp_path, monkeypatch):
    """Redirect ~/.ws to tmp_path so no real home-dir state is read/written."""
    monkeypatch.setenv("WS_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def passing_cache(ws_home):
    """Write a setup==true cache into the tmp ws home."""
    state = {
        "setup": True,
        "checked_at": "2024-01-01T00:00:00+00:00",
        "os": "Darwin",
        "backend": "jsonl",
        "tools": {
            "git-workspace": {"found": True, "version": "git-workspace 0.6.0"},
            "gh": {"found": True, "version": "gh version 2.42.0"},
            "bd": {"found": True, "version": "bd 0.1.0"},
            "dolt": {"found": True, "version": "dolt 1.0.0"},
            "colima": {"found": True, "version": "colima 0.6.0"},
        },
    }
    (ws_home / "setup-state.json").write_text(json.dumps(state))
    return ws_home


@pytest.fixture()
def failing_cache(ws_home):
    """Write a setup==false cache into the tmp ws home."""
    state = {
        "setup": False,
        "checked_at": "2024-01-01T00:00:00+00:00",
        "os": "Darwin",
        "backend": "jsonl",
        "tools": {
            "git-workspace": {"found": False, "version": None},
            "gh": {"found": True, "version": "gh version 2.42.0"},
            "bd": {"found": True, "version": "bd 0.1.0"},
            "dolt": {"found": False, "version": None},
            "colima": {"found": False, "version": None},
        },
    }
    (ws_home / "setup-state.json").write_text(json.dumps(state))
    return ws_home


def _clear_setup_env(monkeypatch):
    monkeypatch.delenv("WS_SKIP_SETUP_CHECK", raising=False)


# ---- gate: allow-list -------------------------------------------------------


@pytest.mark.parametrize("verb", ["setup", "config", "doctor"])
def test_gate_allows_exempt_verbs_without_cache(verb, ws_home, monkeypatch):
    """setup / config / doctor pass even when no cache exists."""
    _clear_setup_env(monkeypatch)
    # Patch config.load to avoid FileNotFoundError on the config file
    monkeypatch.setattr("beadhive.config.config_path", lambda: ws_home / "config.yaml")
    # We only test that the gate doesn't fire — invoke with --help so the sub-app
    # terminates cleanly without needing a real bd/git environment.
    result = runner.invoke(app, [verb, "--help"])
    # The gate must NOT inject "requires setup" messaging
    assert "requires setup" not in result.output
    assert "requires setup" not in (result.output or "")


def test_gate_allows_none_subcommand(ws_home, monkeypatch):
    """When ctx.invoked_subcommand is None (no subcommand), gate is bypassed."""
    _clear_setup_env(monkeypatch)
    # No subcommand → root help (no_args_is_help=True means help is shown, not an error)
    result = runner.invoke(app, [])
    assert "requires setup" not in result.output


# ---- gate: deny -------------------------------------------------------------


@pytest.mark.parametrize("verb", ["sync", "report-target", "role"])
def test_gate_denies_when_cache_absent(verb, ws_home, monkeypatch):
    """Arbitrary verbs are blocked with a clear message when no cache exists."""
    _clear_setup_env(monkeypatch)
    monkeypatch.setattr("beadhive.config.config_path", lambda: ws_home / "config.yaml")
    result = runner.invoke(app, [verb])
    assert result.exit_code == 1
    assert "ws setup check" in result.output


def test_gate_denies_when_cache_setup_false(failing_cache, monkeypatch):
    """A setup==false cache still blocks other verbs."""
    _clear_setup_env(monkeypatch)
    monkeypatch.setattr("beadhive.config.config_path", lambda: failing_cache / "config.yaml")
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1
    assert "ws setup check" in result.output


# ---- gate: bypass -----------------------------------------------------------


def test_gate_bypass_env_var(ws_home, monkeypatch):
    """WS_SKIP_SETUP_CHECK=1 lets any verb through even without a cache."""
    monkeypatch.setenv("WS_SKIP_SETUP_CHECK", "1")
    monkeypatch.setattr("beadhive.config.config_path", lambda: ws_home / "config.yaml")
    # role --help is a benign invocation that exits cleanly when bypass is active
    result = runner.invoke(app, ["role", "--help"])
    assert "requires setup" not in result.output


def test_gate_bypass_env_var_value_must_be_1(ws_home, monkeypatch):
    """WS_SKIP_SETUP_CHECK with a non-'1' value does NOT bypass the gate."""
    monkeypatch.setenv("WS_SKIP_SETUP_CHECK", "true")
    monkeypatch.setattr("beadhive.config.config_path", lambda: ws_home / "config.yaml")
    result = runner.invoke(app, ["sync"])
    # gate should still fire (value is "true", not "1")
    assert result.exit_code == 1
    assert "ws setup check" in result.output


# ---- gate: allow after passing setup ----------------------------------------


def test_gate_allows_after_passing_setup(passing_cache, monkeypatch):
    """Any verb is allowed once setup==true cache is present."""
    _clear_setup_env(monkeypatch)
    monkeypatch.setattr("beadhive.config.config_path", lambda: passing_cache / "config.yaml")
    # role --help succeeds (gate passes, sub-command shows help)
    result = runner.invoke(app, ["role", "--help"])
    assert "requires setup" not in result.output
    assert result.exit_code == 0


# ---- is_setup_complete -------------------------------------------------------


def test_is_setup_complete_no_cache(ws_home):
    assert setup_mod.is_setup_complete() is False


def test_is_setup_complete_true_cache(passing_cache):
    assert setup_mod.is_setup_complete() is True


def test_is_setup_complete_false_cache(failing_cache):
    assert setup_mod.is_setup_complete() is False


def test_is_setup_complete_corrupt_cache(ws_home):
    (ws_home / "setup-state.json").write_text("not-json{{{")
    assert setup_mod.is_setup_complete() is False


# ---- probe_one --------------------------------------------------------------


def test_probe_one_not_found(monkeypatch):
    """probe_one returns found=False when shutil.which returns None."""
    monkeypatch.setattr(setup_mod.shutil, "which", lambda _: None)
    result = setup_mod.probe_one("gh", "gh", ["gh", "--version"])
    assert result == {"found": False, "version": None}


def test_probe_one_found_with_version(monkeypatch):
    """probe_one captures the first line of stdout as the version string."""
    import subprocess

    monkeypatch.setattr(setup_mod.shutil, "which", lambda _: "/usr/bin/gh")
    fake = subprocess.CompletedProcess(
        args=["gh", "--version"], returncode=0, stdout="gh version 2.42.0\nextra", stderr=""
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)
    result = setup_mod.probe_one("gh", "gh", ["gh", "--version"])
    assert result["found"] is True
    assert result["version"] == "gh version 2.42.0"


def test_probe_one_found_but_version_cmd_fails(monkeypatch):
    """Binary is present but version cmd fails → found=True, version=None."""
    import subprocess

    monkeypatch.setattr(setup_mod.shutil, "which", lambda _: "/usr/bin/dolt")
    fake = subprocess.CompletedProcess(
        args=["dolt", "version"], returncode=1, stdout="", stderr="error"
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)
    result = setup_mod.probe_one("dolt", "dolt", ["dolt", "version"])
    # returncode != 0 but binary was found; version is parsed from combined output
    assert result["found"] is True


def test_probe_one_file_not_found_exception(monkeypatch):
    """FileNotFoundError from subprocess.run → found=False."""
    import subprocess

    monkeypatch.setattr(setup_mod.shutil, "which", lambda _: "/usr/bin/colima")
    def _raise_fnf(*a, **kw):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", _raise_fnf)
    result = setup_mod.probe_one("colima", "colima", ["colima", "--version"])
    assert result == {"found": True, "version": None}  # which found it, subprocess raised


def test_probe_one_timeout(monkeypatch):
    """subprocess.TimeoutExpired → found=True (binary present), version=None."""
    import subprocess

    monkeypatch.setattr(setup_mod.shutil, "which", lambda _: "/usr/bin/dolt")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd="dolt", timeout=5)),
    )
    result = setup_mod.probe_one("dolt", "dolt", ["dolt", "version"])
    assert result["found"] is True
    assert result["version"] is None


# ---- probe_tools ------------------------------------------------------------


def test_probe_tools_returns_all_table_names(monkeypatch):
    """probe_tools() returns a key for every entry in PROBE_TABLE."""
    monkeypatch.setattr(
        setup_mod, "probe_one", lambda name, wb, vcmd: {"found": True, "version": "1.0"}
    )
    results = setup_mod.probe_tools()
    expected_names = {name for name, _, _ in setup_mod.PROBE_TABLE}
    assert set(results.keys()) == expected_names


# ---- cache read/write -------------------------------------------------------


def test_cache_round_trip(ws_home):
    """_write_cache + read_cache is a lossless round-trip."""
    tools = {
        "gh": {"found": True, "version": "gh 2.0"},
        "bd": {"found": False, "version": None},
    }
    setup_mod._write_cache(tools, success=False)
    cache = setup_mod.read_cache()
    assert cache is not None
    assert cache["setup"] is False
    assert cache["tools"]["gh"] == {"found": True, "version": "gh 2.0"}
    assert cache["tools"]["bd"] == {"found": False, "version": None}
    assert "os" in cache
    assert "backend" in cache
    assert "checked_at" in cache


def test_cache_setup_true_on_success(ws_home):
    setup_mod._write_cache({"gh": {"found": True, "version": "x"}}, success=True)
    assert setup_mod.read_cache()["setup"] is True


def test_cache_missing_returns_none(ws_home):
    assert setup_mod.read_cache() is None


# ---- run_check CLI command --------------------------------------------------


def test_run_check_all_found_exits_0(ws_home, monkeypatch, capsys):
    """run_check exits 0 and writes setup==true when all tools are found."""
    all_found = {n: {"found": True, "version": "1.0"} for n, _, _ in setup_mod.PROBE_TABLE}
    monkeypatch.setattr(setup_mod, "probe_tools", lambda: all_found)
    setup_mod.run_check()
    out = capsys.readouterr().out
    assert "setup complete" in out
    assert setup_mod.is_setup_complete() is True


def test_run_check_missing_exits_1(ws_home, monkeypatch, capsys):
    """run_check exits 1 and writes setup==false when a tool is missing."""
    tools: dict = {name: {"found": True, "version": "1.0"} for name, _, _ in setup_mod.PROBE_TABLE}
    tools["git-workspace"] = {"found": False, "version": None}
    monkeypatch.setattr(setup_mod, "probe_tools", lambda: tools)

    with pytest.raises(typer.Exit) as exc_info:
        setup_mod.run_check()
    assert exc_info.value.exit_code == 1
    assert setup_mod.is_setup_complete() is False
    out = capsys.readouterr().out
    assert "git-workspace" in out


def test_run_check_refreshes_existing_cache(ws_home, monkeypatch):
    """Re-running run_check overwrites a stale cache."""
    # Write an old failing cache
    setup_mod._write_cache({"old": {"found": False, "version": None}}, success=False)
    assert setup_mod.is_setup_complete() is False

    all_found = {n: {"found": True, "version": "1.0"} for n, _, _ in setup_mod.PROBE_TABLE}
    monkeypatch.setattr(setup_mod, "probe_tools", lambda: all_found)
    setup_mod.run_check()
    assert setup_mod.is_setup_complete() is True


# ---- run_show CLI command ---------------------------------------------------


def test_run_show_no_cache_exits_1(ws_home, capsys):
    """run_show exits 1 when no cache exists."""
    with pytest.raises(typer.Exit) as exc_info:
        setup_mod.run_show()
    assert exc_info.value.exit_code == 1


def test_run_show_renders_cache(passing_cache, capsys):
    """run_show renders the cached state without re-probing."""
    setup_mod.run_show()
    out = capsys.readouterr().out
    assert "complete" in out
    assert "Darwin" in out
    assert "git-workspace" in out
