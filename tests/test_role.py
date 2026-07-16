"""Tests for beadhive.role: statusline rendering, seat listing, role validation, and launch exec.

Statusline:
  - happy path with full JSON (agent.name + workspace.repo)
  - cwd-derived fallback when repo block is absent
  - role fallback chain (agent.name → BH_ROLE → "main")
  - malformed / empty stdin → bare ⬡, never raises

Role listing / validation:
  - launch("") prints available seats
  - launch(unknown) exits non-zero with known-seat list in stderr
  - launch(valid_role) calls run() with correct args and BH_ROLE in env
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from beadhive import role

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run_statusline(stdin_text: str, monkeypatch=None, extra_env=None) -> str:
    """Run role.statusline() with faked stdin, return printed output (stripped)."""
    import io as _io

    captured = _io.StringIO()
    fake_stdin = _io.StringIO(stdin_text)

    env_patch = {}
    if extra_env:
        env_patch.update(extra_env)

    with patch("sys.stdin", fake_stdin), patch("sys.stdout", captured):
        if monkeypatch:
            for k, v in env_patch.items():
                monkeypatch.setenv(k, v)
        role.statusline()

    return captured.getvalue().strip()


# ---------------------------------------------------------------------------
# statusline: happy path — JSON with agent.name and workspace.repo
# ---------------------------------------------------------------------------


def test_statusline_full_json(monkeypatch):
    monkeypatch.delenv("BH_ROLE", raising=False)
    payload = json.dumps(
        {
            "agent": {"name": "developer"},
            "workspace": {"repo": {"owner": "briancripe", "name": "workspace"}},
        }
    )
    out = _run_statusline(payload, monkeypatch)
    assert out == "⬡ briancripe/workspace · developer"


def test_statusline_role_from_agent_name(monkeypatch):
    monkeypatch.delenv("BH_ROLE", raising=False)
    payload = json.dumps(
        {
            "agent": {"name": "dispatcher"},
            "workspace": {"repo": {"owner": "acme", "name": "core"}},
        }
    )
    out = _run_statusline(payload, monkeypatch)
    assert out == "⬡ acme/core · dispatcher"


# ---------------------------------------------------------------------------
# statusline: role fallback chain
# ---------------------------------------------------------------------------


def test_statusline_role_falls_back_to_bh_role(monkeypatch):
    monkeypatch.setenv("BH_ROLE", "merger")
    payload = json.dumps(
        {
            "agent": {},  # no name
            "workspace": {"repo": {"owner": "o", "name": "r"}},
        }
    )
    out = _run_statusline(payload, monkeypatch)
    assert out == "⬡ o/r · merger"


def test_statusline_role_falls_back_to_main(monkeypatch):
    monkeypatch.delenv("BH_ROLE", raising=False)
    payload = json.dumps({"workspace": {"repo": {"owner": "o", "name": "r"}}})
    out = _run_statusline(payload, monkeypatch)
    assert out == "⬡ o/r · main"


# ---------------------------------------------------------------------------
# statusline: rig cwd-derived fallback when repo block absent
# ---------------------------------------------------------------------------


def test_statusline_hive_from_cwd(monkeypatch):
    monkeypatch.delenv("BH_ROLE", raising=False)
    payload = json.dumps({"agent": {"name": "developer"}})  # no workspace.repo

    with (
        patch("beadhive.role._cwd_hive", return_value="myorg/myrepo"),
        patch("sys.stdin", io.StringIO(payload)),
        patch("sys.stdout", io.StringIO()) as mock_out,
    ):
        role.statusline()

    mock_out.seek(0)
    out = mock_out.read().strip()
    assert out == "⬡ myorg/myrepo · developer"


def test_statusline_hive_dash_when_outside_workspace(monkeypatch):
    monkeypatch.delenv("BH_ROLE", raising=False)
    payload = json.dumps({"agent": {"name": "developer"}})

    with (
        patch("beadhive.role._cwd_hive", return_value="—"),
        patch("sys.stdin", io.StringIO(payload)),
        patch("sys.stdout", io.StringIO()) as mock_out,
    ):
        role.statusline()

    mock_out.seek(0)
    out = mock_out.read().strip()
    assert out == "⬡ — · developer"


# ---------------------------------------------------------------------------
# statusline: error cases — never raises, always prints ⬡
# ---------------------------------------------------------------------------


def test_statusline_empty_stdin_prints_bare_glyph(monkeypatch):
    monkeypatch.delenv("BH_ROLE", raising=False)
    out = _run_statusline("", monkeypatch)
    assert out == "⬡"


def test_statusline_malformed_json_prints_bare_glyph(monkeypatch):
    monkeypatch.delenv("BH_ROLE", raising=False)
    out = _run_statusline("{not valid json", monkeypatch)
    assert out == "⬡"


def test_statusline_never_raises_on_any_exception(monkeypatch):
    monkeypatch.delenv("BH_ROLE", raising=False)
    # Even if _cwd_rig blows up and stdin throws
    with (
        patch("beadhive.role._cwd_hive", side_effect=RuntimeError("boom")),
        patch("sys.stdin", io.StringIO("{}")),  # triggers _cwd_rig call
        patch("sys.stdout", io.StringIO()) as mock_out,
    ):
        role.statusline()  # must not raise

    mock_out.seek(0)
    out = mock_out.read().strip()
    assert out == "⬡"


# ---------------------------------------------------------------------------
# role listing
# ---------------------------------------------------------------------------


def test_launch_empty_lists_seats(monkeypatch, capsys):
    known = ["analyst", "dispatcher", "developer"]
    with patch("beadhive.role._known_seats", return_value=known):
        role.launch("")

    out = capsys.readouterr().out
    for seat in known:
        assert seat in out


def test_launch_no_role_returns_without_exec(monkeypatch):
    """launch('') must NOT call run() / exec claude."""
    with (
        patch("beadhive.role._known_seats", return_value=["developer"]),
        patch("beadhive.role.run", side_effect=AssertionError("should not exec")),
    ):
        # Should return normally without calling run
        role.launch("")


# ---------------------------------------------------------------------------
# role validation
# ---------------------------------------------------------------------------


def test_launch_unknown_role_exits_nonzero(monkeypatch, capsys):
    with patch("beadhive.role._known_seats", return_value=["developer", "merger"]):
        with pytest.raises(SystemExit) as exc_info:
            role.launch("nonexistent")
    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "nonexistent" in err
    assert "developer" in err
    assert "merger" in err


# ---------------------------------------------------------------------------
# role exec — mock run() so no real claude is spawned
# ---------------------------------------------------------------------------


def test_launch_valid_role_uses_scoped_plugin_arg(monkeypatch):
    """launch(seat) uses 'bh:seat' by default (plugin mode, no local override)."""
    mock_result = SimpleNamespace(returncode=0)
    with (
        patch("beadhive.role._known_seats", return_value=["developer", "dispatcher"]),
        patch("beadhive.role._local_agent_override", return_value=False),
        patch("beadhive.role._plugin_name", return_value="bh"),
        patch("beadhive.role.run", return_value=mock_result) as mock_run,
    ):
        with pytest.raises(SystemExit) as exc_info:
            role.launch("developer")

    assert exc_info.value.code == 0
    mock_run.assert_called_once()
    call_args, call_kwargs = mock_run.call_args
    cmd = call_args[0]
    assert cmd == ["claude", "--agent", "bh:developer"]
    assert call_kwargs.get("capture") is False
    assert call_kwargs.get("check") is False
    env = call_kwargs.get("env", {})
    assert env.get("BH_ROLE") == "developer"


def test_launch_local_override_uses_bare_agent_arg(monkeypatch):
    """When a local .claude/agents/<seat>.md exists, the bare form is used."""
    mock_result = SimpleNamespace(returncode=0)
    with (
        patch("beadhive.role._known_seats", return_value=["developer"]),
        patch("beadhive.role._local_agent_override", return_value=True),
        patch("beadhive.role._plugin_name", return_value="bh"),
        patch("beadhive.role.run", return_value=mock_result) as mock_run,
    ):
        with pytest.raises(SystemExit):
            role.launch("developer")

    call_args, _ = mock_run.call_args
    cmd = call_args[0]
    assert cmd == ["claude", "--agent", "developer"]


def test_launch_respects_configured_plugin_name(monkeypatch):
    """--agent arg uses the configured plugin name, not a hardcoded 'bh'."""
    mock_result = SimpleNamespace(returncode=0)
    with (
        patch("beadhive.role._known_seats", return_value=["dispatcher"]),
        patch("beadhive.role._local_agent_override", return_value=False),
        patch("beadhive.role._plugin_name", return_value="custom"),
        patch("beadhive.role.run", return_value=mock_result) as mock_run,
    ):
        with pytest.raises(SystemExit):
            role.launch("dispatcher")

    call_args, _ = mock_run.call_args
    assert call_args[0] == ["claude", "--agent", "custom:dispatcher"]


def test_launch_propagates_exit_code(monkeypatch):
    mock_result = SimpleNamespace(returncode=42)
    with (
        patch("beadhive.role._known_seats", return_value=["developer"]),
        patch("beadhive.role._local_agent_override", return_value=False),
        patch("beadhive.role._plugin_name", return_value="bh"),
        patch("beadhive.role.run", return_value=mock_result),
    ):
        with pytest.raises(SystemExit) as exc_info:
            role.launch("developer")

    assert exc_info.value.code == 42


def test_launch_bh_role_in_env_inherits_os_environ(monkeypatch):
    """BH_ROLE must be in the env passed to run, alongside existing env vars."""
    monkeypatch.setenv("SOME_EXISTING_VAR", "hello")
    mock_result = SimpleNamespace(returncode=0)
    with (
        patch("beadhive.role._known_seats", return_value=["developer"]),
        patch("beadhive.role._local_agent_override", return_value=False),
        patch("beadhive.role._plugin_name", return_value="bh"),
        patch("beadhive.role.run", return_value=mock_result) as mock_run,
    ):
        with pytest.raises(SystemExit):
            role.launch("developer")

    _, call_kwargs = mock_run.call_args
    env = call_kwargs.get("env", {})
    assert env.get("BH_ROLE") == "developer"
    assert env.get("SOME_EXISTING_VAR") == "hello"


# ---------------------------------------------------------------------------
# _resolve_agent_arg — pure unit tests
# ---------------------------------------------------------------------------


def test_resolve_agent_arg_scoped_when_no_local_override():
    with patch("beadhive.role._local_agent_override", return_value=False):
        assert role._resolve_agent_arg("dispatcher", "bh") == "bh:dispatcher"


def test_resolve_agent_arg_bare_when_local_override():
    with patch("beadhive.role._local_agent_override", return_value=True):
        assert role._resolve_agent_arg("dispatcher", "bh") == "dispatcher"
