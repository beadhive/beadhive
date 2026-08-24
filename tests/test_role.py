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

harness_env / _bh_bin_dir (bh-og0q.2):
  - bh's own bin dir (sys.argv[0]'s parent) is added back to the launched harness's PATH when
    a stripped, systemd-service-style PATH omits it
  - no duplication when it's already present; degrades to a bare os.environ copy when the bin
    dir can't be resolved at all
"""

from __future__ import annotations

import io
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from beadhive import role
from beadhive.cli import app

cli_runner = CliRunner()

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
# statusline: hive cwd-derived fallback when repo block absent
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
    # Even if _cwd_hive blows up and stdin throws
    with (
        patch("beadhive.role._cwd_hive", side_effect=RuntimeError("boom")),
        patch("sys.stdin", io.StringIO("{}")),  # triggers _cwd_hive call
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
        # See test_launch_local_override_uses_bare_agent_arg below for why this stub is required
        # (bh-pc2a.36's PATH guard). Missing here until bh-nvv66: this test read the AMBIENT
        # claude install, so it passed on a machine that had one and failed in the fence, in CI
        # and on any fresh host — a unit test whose verdict depended on the developer's laptop.
        patch("beadhive.harness.installed_path", return_value="/usr/local/bin/claude"),
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
        # These tests are about the ENV/PATH handed to the harness, not about whether it is
        # installed — and launch() now refuses a harness missing from PATH before calling run()
        # (bh-pc2a.36). Without this stub they would pass vacuously: the guard also raises
        # SystemExit, so `pytest.raises` stays green while run() is never reached.
        patch("beadhive.harness.installed_path", return_value="/usr/local/bin/claude"),
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
        # Stubbed for the same reason as its siblings — the ambient claude install is not this
        # test's subject (bh-nvv66).
        patch("beadhive.harness.installed_path", return_value="/usr/local/bin/claude"),
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
        # Without this the guard's own SystemExit(1) is what `pytest.raises` catches, so the
        # assertion below reads 1 instead of the propagated 42 (bh-nvv66).
        patch("beadhive.harness.installed_path", return_value="/usr/local/bin/claude"),
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
        # These tests are about the ENV/PATH handed to the harness, not about whether it is
        # installed — and launch() now refuses a harness missing from PATH before calling run()
        # (bh-pc2a.36). Without this stub they would pass vacuously: the guard also raises
        # SystemExit, so `pytest.raises` stays green while run() is never reached.
        patch("beadhive.harness.installed_path", return_value="/usr/local/bin/claude"),
        patch("beadhive.role.run", return_value=mock_result) as mock_run,
    ):
        with pytest.raises(SystemExit):
            role.launch("developer")

    _, call_kwargs = mock_run.call_args
    env = call_kwargs.get("env", {})
    assert env.get("BH_ROLE") == "developer"
    assert env.get("SOME_EXISTING_VAR") == "hello"


# ---------------------------------------------------------------------------
# harness_env / _bh_bin_dir — the bh-og0q.2 regression: a harness bh launches must still
# resolve bh's own binaries by name even when the ambient PATH (e.g. a systemd service
# environment) omits the account's user bin dir.
# ---------------------------------------------------------------------------

# The service environment observed in the field (Context (3) of the managed-harness-config
# ADR): a systemd unit's PATH with no user bin dir on it at all.
_STRIPPED_SERVICE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"


def test_bh_bin_dir_resolves_argv0_parent(tmp_path, monkeypatch):
    """_bh_bin_dir() is the directory containing the exec'd bh shim (sys.argv[0])."""
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "bh"
    shim.write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "argv", [str(shim), "role", "developer"])

    assert role._bh_bin_dir() == bin_dir


def test_bh_bin_dir_none_when_argv0_is_not_a_file(monkeypatch):
    """python -c / an empty argv[0] can't name a bin dir — must degrade to None, not raise."""
    monkeypatch.setattr(sys, "argv", ["-c"])
    assert role._bh_bin_dir() is None


def test_bh_bin_dir_none_when_argv_empty(monkeypatch):
    monkeypatch.setattr(sys, "argv", [])
    assert role._bh_bin_dir() is None


def test_harness_env_adds_bin_dir_missing_from_stripped_service_path(tmp_path, monkeypatch):
    """The core regression: a systemd-style PATH that omits the user bin dir must still let the
    launched harness resolve bh's own binaries — harness_env() must add that dir back."""
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "bh"
    shim.write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "argv", [str(shim), "role", "developer"])
    monkeypatch.setenv("PATH", _STRIPPED_SERVICE_PATH)

    env = role.harness_env("developer")

    path_dirs = env["PATH"].split(os.pathsep)
    assert str(bin_dir) in path_dirs
    assert env["BH_ROLE"] == "developer"


def test_harness_env_does_not_duplicate_bin_dir_already_on_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "bh"
    shim.write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "argv", [str(shim), "role", "developer"])
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{_STRIPPED_SERVICE_PATH}")

    env = role.harness_env("developer")

    assert env["PATH"].split(os.pathsep).count(str(bin_dir)) == 1


def test_harness_env_leaves_path_untouched_when_bin_dir_unresolvable(monkeypatch):
    """When bh's own bin dir can't be determined, behavior is the old bare os.environ copy."""
    monkeypatch.setattr(sys, "argv", ["-c"])
    monkeypatch.setenv("PATH", _STRIPPED_SERVICE_PATH)

    env = role.harness_env("developer")

    assert env["PATH"] == _STRIPPED_SERVICE_PATH


def test_launch_resolves_bin_dir_env_end_to_end(tmp_path, monkeypatch):
    """launch() itself (not just harness_env()) must pass the repaired PATH to run()."""
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "bh"
    shim.write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "argv", [str(shim), "role", "developer"])
    monkeypatch.setenv("PATH", _STRIPPED_SERVICE_PATH)

    mock_result = SimpleNamespace(returncode=0)
    with (
        patch("beadhive.role._known_seats", return_value=["developer"]),
        patch("beadhive.role._local_agent_override", return_value=False),
        patch("beadhive.role._plugin_name", return_value="bh"),
        # These tests are about the ENV/PATH handed to the harness, not about whether it is
        # installed — and launch() now refuses a harness missing from PATH before calling run()
        # (bh-pc2a.36). Without this stub they would pass vacuously: the guard also raises
        # SystemExit, so `pytest.raises` stays green while run() is never reached.
        patch("beadhive.harness.installed_path", return_value="/usr/local/bin/claude"),
        patch("beadhive.role.run", return_value=mock_result) as mock_run,
    ):
        with pytest.raises(SystemExit):
            role.launch("developer")

    _, call_kwargs = mock_run.call_args
    env = call_kwargs.get("env", {})
    assert str(bin_dir) in env["PATH"].split(os.pathsep)


# ---------------------------------------------------------------------------
# _resolve_agent_arg — pure unit tests
# ---------------------------------------------------------------------------


def test_resolve_agent_arg_scoped_when_no_local_override():
    with patch("beadhive.role._local_agent_override", return_value=False):
        assert role._resolve_agent_arg("dispatcher", "bh") == "bh:dispatcher"


def test_resolve_agent_arg_bare_when_local_override():
    with patch("beadhive.role._local_agent_override", return_value=True):
        assert role._resolve_agent_arg("dispatcher", "bh") == "dispatcher"


# ---------------------------------------------------------------------------
# _local_agent_override — checks both .claude/agents and .opencode/agents
# ---------------------------------------------------------------------------


def test_local_agent_override_true_for_claude_dir(tmp_path, monkeypatch):
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    (tmp_path / ".claude" / "agents" / "developer.md").touch()
    monkeypatch.chdir(tmp_path)
    assert role._local_agent_override("developer") is True


def test_local_agent_override_true_for_opencode_dir(tmp_path, monkeypatch):
    (tmp_path / ".opencode" / "agents").mkdir(parents=True)
    (tmp_path / ".opencode" / "agents" / "developer.md").touch()
    monkeypatch.chdir(tmp_path)
    assert role._local_agent_override("developer") is True


def test_local_agent_override_false_when_neither_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert role._local_agent_override("developer") is False


# ---------------------------------------------------------------------------
# harness argv builder + launch() per-harness seam (bh-73rz.1)
# ---------------------------------------------------------------------------


def test_harness_argv_claude_unchanged():
    with patch("beadhive.role._local_agent_override", return_value=False):
        assert role._harness_argv("claude", "developer") == ["claude", "--agent", "bh:developer"]


def test_harness_argv_opencode_uses_bare_agent():
    assert role._harness_argv("opencode", "developer") == ["opencode", "--agent", "developer"]


def test_launch_opencode_harness_execs_opencode(monkeypatch):
    """`bh role developer` with harness=opencode execs `opencode --agent developer`."""
    mock_result = SimpleNamespace(returncode=0)
    with (
        patch("beadhive.role._known_seats", return_value=["developer"]),
        # opencode has no bh-known install route (it is not a key in `harness.HARNESSES`), so
        # this stub is what exercises the presence guard's "installed" branch rather than the
        # real, environment-dependent `shutil.which("opencode")` (bh-hsus.5).
        patch("beadhive.harness.installed_path", return_value="/usr/local/bin/opencode"),
        patch("beadhive.role.run", return_value=mock_result) as mock_run,
    ):
        with pytest.raises(SystemExit) as exc_info:
            role.launch("developer", harness="opencode")

    assert exc_info.value.code == 0
    call_args, call_kwargs = mock_run.call_args
    assert call_args[0] == ["opencode", "--agent", "developer"]
    assert call_kwargs.get("env", {}).get("BH_ROLE") == "developer"


def test_launch_opencode_missing_prints_missing_hint_not_bare_exec_failure(monkeypatch, capsys):
    """bh-hsus.5: the acceptance case. The guard used to key off `harness.HARNESSES`, an
    install-route registry opencode is never a member of (bh cannot install or authenticate
    it), so it skipped itself for opencode entirely and fell through to `run()` — a real
    `opencode: command not found` from the exec, the exact bh-pc2a.33 failure mode this guard
    exists to prevent, reproduced one call site over. It must now fire for opencode too."""
    with (
        patch("beadhive.role._known_seats", return_value=["developer"]),
        patch("beadhive.harness.installed_path", return_value=None),
        patch("beadhive.role.run", side_effect=AssertionError("must not exec a missing harness")),
    ):
        with pytest.raises(SystemExit) as exc_info:
            role.launch("developer", harness="opencode")

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "opencode" in err
    assert "not installed" in err


def test_launch_claude_harness_behavior_unchanged(monkeypatch):
    """harness=claude behaves exactly like the pre-existing default (no regression)."""
    mock_result = SimpleNamespace(returncode=0)
    with (
        patch("beadhive.role._known_seats", return_value=["developer"]),
        patch("beadhive.role._local_agent_override", return_value=False),
        patch("beadhive.role._plugin_name", return_value="bh"),
        # The subject is the ARGV built for harness=claude, not whether this box has claude
        # installed — stubbed so the answer is the same everywhere (bh-nvv66).
        patch("beadhive.harness.installed_path", return_value="/usr/local/bin/claude"),
        patch("beadhive.role.run", return_value=mock_result) as mock_run,
    ):
        with pytest.raises(SystemExit) as exc_info:
            role.launch("developer", harness="claude")

    assert exc_info.value.code == 0
    call_args, _ = mock_run.call_args
    assert call_args[0] == ["claude", "--agent", "bh:developer"]


def test_launch_unknown_harness_exits_nonzero(capsys):
    with patch("beadhive.role._known_seats", return_value=["developer"]):
        with pytest.raises(SystemExit) as exc_info:
            role.launch("developer", harness="bogus-harness")

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "unknown harness" in err, "a name bh has never heard of really IS unknown"
    assert "bogus-harness" in err
    assert "claude" in err
    assert "opencode" in err


def test_launch_codex_is_rejected_without_being_called_unknown(capsys):
    """bh-hsus.6: the refusal used to read "unknown harness 'codex'. Known harnesses: claude,
    opencode". codex is NOT unknown to bh — it is a row in the dep table with a documented
    install route and a credential probe; it simply cannot exec a seat. "Unknown" sends the
    operator off to check their spelling, which is the same correct-but-misdirecting shape
    (bh-pc2a.33) the rest of this epic removes."""
    with patch("beadhive.role._known_seats", return_value=["developer"]):
        with pytest.raises(SystemExit):
            role.launch("developer", harness="codex")

    err = capsys.readouterr().err
    assert "unknown" not in err.lower()
    assert "cannot run a seat" in err


def test_launch_codex_rejected_because_it_cannot_run_a_seat(capsys):
    """The first live defect this bead fixes. codex is installable and authenticatable
    (`deps.has_install_route()`) but cannot exec a seat — Q1 of
    docs/spikes/bh-hsus.2-dependency-table.md re-verified empirically against codex 0.146.0:
    `codex --agent <seat>` exits `unexpected argument '--agent' found`, and no flag in the full
    6,116-line `codex completion bash` sweep is a seat/persona selector. `bh role --harness
    codex` is still rejected — that is CORRECT, not the bug — but bh-hsus.5 makes it rejected
    FOR THAT REASON: `KNOWN_HARNESSES` is derived from `deps.seat_runners()` (`d.runs_seats`),
    not a hand-written tuple that happened to agree with it."""
    from beadhive import deps

    assert "codex" not in role.KNOWN_HARNESSES
    assert deps.by_name("codex").runs_seats is False
    assert role.KNOWN_HARNESSES == tuple(d.name for d in deps.seat_runners())

    with patch("beadhive.role._known_seats", return_value=["developer"]):
        with pytest.raises(SystemExit) as exc_info:
            role.launch("developer", harness="codex")

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "codex" in err
    assert "claude" in err
    assert "opencode" in err


def test_launch_defaults_harness_from_config_when_not_passed():
    """No explicit harness arg → resolved via config.harness_name() (BH_HARNESS env wins)."""
    mock_result = SimpleNamespace(returncode=0)
    with (
        patch("beadhive.role._known_seats", return_value=["developer"]),
        patch("beadhive.role._harness_name", return_value="opencode"),
        # bh-hsus.5: opencode has no bh-known install route, so this test was previously
        # (accidentally) environment-dependent — it passed only on a host that happened to
        # have opencode on PATH, and failed on the Linux test-bed, which does not. Stub
        # presence explicitly rather than relying on the real machine's state.
        patch("beadhive.harness.installed_path", return_value="/usr/local/bin/opencode"),
        patch("beadhive.role.run", return_value=mock_result) as mock_run,
    ):
        with pytest.raises(SystemExit):
            role.launch("developer")

    call_args, _ = mock_run.call_args
    assert call_args[0] == ["opencode", "--agent", "developer"]


def test_harness_name_reads_config(monkeypatch):
    """role._harness_name() delegates to config.harness_name(config.load())."""
    from beadhive import config

    monkeypatch.setattr(config, "load", lambda: {"harness": "opencode"})
    assert role._harness_name() == "opencode"


def test_harness_name_falls_back_to_claude_on_error(monkeypatch):
    from beadhive import config

    def _boom():
        raise RuntimeError("no config")

    monkeypatch.setattr(config, "load", _boom)
    assert role._harness_name() == "claude"


# ---------------------------------------------------------------------------
# cli.py role_cmd --harness flag
# ---------------------------------------------------------------------------


def test_cli_role_harness_flag_passed_through(monkeypatch):
    """`bh role <seat> --harness opencode` threads the flag into role.launch()."""
    monkeypatch.setenv("BH_SKIP_SETUP_CHECK", "1")
    with patch("beadhive.role.launch") as mock_launch:
        result = cli_runner.invoke(app, ["role", "developer", "--harness", "opencode"])

    assert result.exit_code == 0
    mock_launch.assert_called_once_with("developer", harness="opencode")


def test_cli_role_no_harness_flag_passes_none(monkeypatch):
    """Omitting --harness passes harness=None so launch() falls back to config resolution."""
    monkeypatch.setenv("BH_SKIP_SETUP_CHECK", "1")
    with patch("beadhive.role.launch") as mock_launch:
        result = cli_runner.invoke(app, ["role", "developer"])

    assert result.exit_code == 0
    mock_launch.assert_called_once_with("developer", harness=None)


def test_cli_role_no_hitch_flag_threaded_through(monkeypatch):
    """`bh role <seat> --no-hitch` reaches hitch_plugin.route(no_hitch=True) (bh-6t49w.3)."""
    from beadhive import hitch_plugin

    monkeypatch.setenv("BH_SKIP_SETUP_CHECK", "1")
    with patch.object(hitch_plugin, "route") as mock_route:
        result = cli_runner.invoke(app, ["role", "developer", "--no-hitch"])

    assert result.exit_code == 0
    mock_route.assert_called_once_with("developer", harness=None, no_hitch=True, full_seats=False)


def test_cli_role_seats_flag_threaded_through(monkeypatch):
    """`bh role --seats` reaches hitch_plugin.route(full_seats=True) (bh-6t49w.5)."""
    from beadhive import hitch_plugin

    monkeypatch.setenv("BH_SKIP_SETUP_CHECK", "1")
    with patch.object(hitch_plugin, "route") as mock_route:
        result = cli_runner.invoke(app, ["role", "--seats"])

    assert result.exit_code == 0
    mock_route.assert_called_once_with("", harness=None, no_hitch=False, full_seats=True)


# ---------------------------------------------------------------------------
# cli.py --bead/--hive workspace resolution (bh-6t49w.4)
# ---------------------------------------------------------------------------


def test_bead_hive_prefix_extracts_leading_token():
    from beadhive import cli

    assert cli._role_bead_hive_prefix("bh-6t49w.4") == "bh"
    assert cli._role_bead_hive_prefix("noprefix") == "noprefix"


def test_apply_role_workspace_noop_when_neither_given(monkeypatch):
    from beadhive import cli

    chdir_calls = []
    monkeypatch.setattr(os, "chdir", lambda p: chdir_calls.append(p))
    cli._apply_role_workspace("", "")
    assert chdir_calls == []


def test_apply_role_workspace_hive_only_chdirs_to_hive_root(monkeypatch, tmp_path):
    from beadhive import cli

    entry = {"provider": "github", "org": "acme", "repo": "core", "prefix": "bh"}
    hive_root = tmp_path / "hive"
    hive_root.mkdir()
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(cli.registry, "resolve_hive", lambda cfg, hive_id: entry)
    monkeypatch.setattr(cli.registry, "hive_dir", lambda e: hive_root)

    monkeypatch.chdir(tmp_path)
    cli._apply_role_workspace("", "bh")

    assert os.getcwd() == str(hive_root.resolve())


def test_apply_role_workspace_bead_only_resolves_hive_from_prefix_and_claims(monkeypatch, tmp_path):
    from beadhive import cli

    entry = {"provider": "github", "org": "acme", "repo": "core", "prefix": "bh"}
    resolve_calls = []
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(
        cli.registry,
        "resolve_hive",
        lambda cfg, hive_id: resolve_calls.append(hive_id) or entry,
    )
    monkeypatch.setattr(cli.registry, "hive_key", lambda e: f"{e['org']}/{e['repo']}")

    claim_calls = []
    monkeypatch.setattr(cli.work, "claim", lambda **kw: claim_calls.append(kw))

    from beadhive import worktree

    workspace = tmp_path / "wt"
    workspace.mkdir()
    monkeypatch.setattr(
        worktree,
        "locate",
        lambda cfg, hive, bead="": (entry, tmp_path, workspace, "br"),
    )

    cli._apply_role_workspace("bh-6t49w.4", "")

    assert resolve_calls == ["bh"]  # hive resolved from the bead's own leading prefix
    assert claim_calls == [
        {"bead": "bh-6t49w.4", "as_": "", "group": "", "collapse": "", "hive": "bh"}
    ]
    assert os.getcwd() == str(workspace.resolve())


def test_apply_role_workspace_bead_and_hive_disagree_refuses_loudly(monkeypatch):
    from beadhive import cli

    bead_entry = {"provider": "github", "org": "acme", "repo": "core", "prefix": "bh"}
    hive_entry = {"provider": "github", "org": "other", "repo": "y", "prefix": "y"}

    def _resolve(cfg, hive_id):
        return hive_entry if hive_id == "other/y" else bead_entry

    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(cli.registry, "resolve_hive", _resolve)
    monkeypatch.setattr(cli.registry, "hive_key", lambda e: f"{e['org']}/{e['repo']}")

    import typer as typer_mod

    with pytest.raises(typer_mod.Exit) as exc:
        cli._apply_role_workspace("bh-6t49w.4", "other/y")
    assert exc.value.exit_code == 1


def test_cli_role_bead_flag_resolves_workspace_before_route(monkeypatch):
    """`bh role <seat> --bead <id>` resolves the workspace before `hitch_plugin.route` runs,
    for both backends (route dispatches to native/hitch, chdir already happened)."""
    from beadhive import cli, hitch_plugin

    monkeypatch.setenv("BH_SKIP_SETUP_CHECK", "1")
    calls = []
    monkeypatch.setattr(cli, "_apply_role_workspace", lambda bead, hive: calls.append((bead, hive)))
    with patch.object(hitch_plugin, "route") as mock_route:
        result = cli_runner.invoke(app, ["role", "developer", "--bead", "bh-6t49w.4"])

    assert result.exit_code == 0
    assert calls == [("bh-6t49w.4", "")]
    mock_route.assert_called_once_with("developer", harness=None, no_hitch=False, full_seats=False)


# ---------------------------------------------------------------------------
# cli.py headless dispatch: --task / -d (bh-6t49w.6)
# ---------------------------------------------------------------------------


def test_role_instructions_point_at_the_bead_brief_not_a_restated_task():
    from beadhive import cli

    text = cli._role_instructions("developer", "bh-6t49w.6", "")
    assert "bh work brief bh-6t49w.6" in text
    assert "## Task" not in text


def test_role_instructions_append_task_when_given():
    from beadhive import cli

    text = cli._role_instructions("developer", "bh-6t49w.6", "only touch role.py")
    assert "bh work brief bh-6t49w.6" in text  # the bead stays the source of truth
    assert text.rstrip().endswith("only touch role.py")


def test_cli_role_headless_refuses_unsuitable_seat_before_touching_the_workspace(monkeypatch):
    """`bh role supervisor --bead <id> -d` refuses immediately — no claim, no launch."""
    from beadhive import cli

    monkeypatch.setenv("BH_SKIP_SETUP_CHECK", "1")
    monkeypatch.setattr(
        cli, "_apply_role_workspace", lambda *a: pytest.fail("workspace resolved before refusal")
    )
    result = cli_runner.invoke(app, ["role", "supervisor", "--bead", "bh-6t49w.6", "-d"])

    assert result.exit_code == 1
    assert "not a headless-capable seat" in result.output
    assert "bh role supervisor" in result.output


def test_cli_role_headless_hitch_backend_forwards_the_brief_pointer_as_task(monkeypatch):
    from beadhive import cli, hitch_plugin

    monkeypatch.setenv("BH_SKIP_SETUP_CHECK", "1")
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(cli.config, "harness_name", lambda cfg: "claude")
    monkeypatch.setattr(cli, "_apply_role_workspace", lambda *a: None)
    monkeypatch.setattr(
        hitch_plugin, "headless_plan", lambda seat, harness, cfg: ("hitch", "hitch profile x")
    )
    with patch.object(hitch_plugin, "up", return_value=0) as mock_up:
        result = cli_runner.invoke(app, ["role", "developer", "--bead", "bh-6t49w.6", "-d"])

    assert result.exit_code == 0, result.output
    kwargs = mock_up.call_args.kwargs
    assert kwargs["detached"] is True
    assert kwargs["role_"] == "developer"
    assert "bh work brief bh-6t49w.6" in kwargs["task"]


def test_cli_role_headless_needs_a_bead_or_a_task(monkeypatch):
    from beadhive import cli, hitch_plugin

    monkeypatch.setenv("BH_SKIP_SETUP_CHECK", "1")
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(cli.config, "harness_name", lambda cfg: "claude")
    monkeypatch.setattr(
        hitch_plugin, "headless_plan", lambda seat, harness, cfg: ("baml", "built bh-developer")
    )
    result = cli_runner.invoke(app, ["role", "developer", "-d"])

    assert result.exit_code == 1
    assert "--bead" in result.output


def test_baml_required_refusal_happens_before_claim_or_spawn(monkeypatch):
    from beadhive import cli, role_execution

    monkeypatch.setenv("BH_SKIP_SETUP_CHECK", "1")
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(
        role_execution,
        "resolve_headless_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            role_execution.RoleLaunchRefused(
                "provider_unavailable", "Codex is not runnable in the checked artifact"
            )
        ),
    )
    monkeypatch.setattr(
        cli, "_apply_role_workspace", lambda *_args: pytest.fail("refusal claimed a workspace")
    )
    monkeypatch.setattr(cli, "run", lambda *_args, **_kwargs: pytest.fail("refusal spawned"))

    result = cli_runner.invoke(
        app,
        [
            "role",
            "developer",
            "--harness",
            "codex",
            "--task",
            "fixture",
            "--baml-required",
        ],
    )

    assert result.exit_code == 1
    assert "provider_unavailable" in result.output


def test_baml_required_never_falls_through_to_attached_launch(monkeypatch):
    from beadhive import cli, hitch_plugin

    monkeypatch.setenv("BH_SKIP_SETUP_CHECK", "1")
    monkeypatch.setattr(
        cli, "_apply_role_workspace", lambda *_args: pytest.fail("attached path claimed a bead")
    )
    monkeypatch.setattr(
        hitch_plugin, "route", lambda *_args, **_kwargs: pytest.fail("attached path launched")
    )

    result = cli_runner.invoke(app, ["role", "developer", "--baml-required"])

    assert result.exit_code == 1
    assert "headless --task/-d" in result.output


def test_qualified_baml_launch_propagates_distinct_outer_and_provider_identity(
    monkeypatch, tmp_path
):
    from beadhive import cli, role_execution

    binary = tmp_path / "bh-developer-claude-code"
    binary.write_text("fixture", encoding="utf-8")
    manifest = tmp_path / "bh-developer-claude-code.manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    artifact = role_execution.QualifiedArtifact(
        binary=binary,
        manifest_path=manifest,
        artifact_digest="sha256:" + "a" * 64,
        manifest_digest="sha256:" + "b" * 64,
        seat="developer",
        provider="claude-code",
        manifest={},
    )
    plan = role_execution.RoleLaunchPlan(
        backend="baml", detail="validated fixture", provider="claude-code", artifact=artifact
    )

    class FakeJournal:
        run_id = "outer-attempt-1"

        @staticmethod
        def child_env(env):
            return {
                **env,
                "BH_RUN_ID": "outer-attempt-1",
                "BH_RUN_DRIVER": "baml",
                "BH_RUN_PROVIDER": "claude-code",
                "BH_RUN_MANIFEST_DIGEST": artifact.manifest_digest,
            }

    entry = {"provider": "github", "org": "acme", "repo": "core"}
    journal_calls = []
    run_calls = []
    monkeypatch.setenv("BH_SKIP_SETUP_CHECK", "1")
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(role_execution, "resolve_headless_plan", lambda *_a, **_kw: plan)
    monkeypatch.setattr(cli, "_apply_role_workspace", lambda *_args: None)
    monkeypatch.setattr(cli, "_role_dispatch_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.registry, "entry_for_dir", lambda *_args: entry)
    monkeypatch.setattr(
        role_execution,
        "create_role_journal",
        lambda artifact, **kwargs: journal_calls.append((artifact, kwargs)) or FakeJournal(),
    )
    monkeypatch.setattr(
        cli,
        "run",
        lambda argv, **kwargs: run_calls.append((argv, kwargs)) or SimpleNamespace(returncode=0),
    )

    result = cli_runner.invoke(
        app,
        [
            "role",
            "developer",
            "--harness",
            "claude",
            "--task",
            "secret task text",
            "--baml-required",
        ],
    )

    assert result.exit_code == 0, result.output
    assert journal_calls == [(artifact, {"hive": "github/acme/core", "bead": ""})]
    argv, kwargs = run_calls[0]
    assert argv[0] == str(binary)
    assert "--bundle" not in argv
    assert argv[argv.index("--outer_attempt_id") + 1] == "outer-attempt-1"
    assert argv[argv.index("--artifact_path") + 1] == str(binary)
    assert argv[argv.index("--artifact_manifest") + 1] == str(manifest)
    provider_continuation = argv[argv.index("--session_id") + 1]
    assert provider_continuation != "outer-attempt-1"
    assert kwargs["env"]["BH_RUN_ID"] == "outer-attempt-1"
    assert kwargs["env"]["BH_RUN_PROVIDER"] == "claude-code"
    context = argv[argv.index("--journal_context") + 1]
    assert json.loads(context)["run_id"] == "outer-attempt-1"
    assert "secret task text" not in context


# ---------------------------------------------------------------------------
# cli.py `bh role <seat> --explain`: preview backend + mode, launch nothing (bh-6t49w.7)
# ---------------------------------------------------------------------------


def test_cli_role_explain_is_json_redacted_and_starts_nothing(monkeypatch):
    import subprocess

    from beadhive import cli, identity, role_execution, run_journal, worktree

    entry = {"provider": "github", "org": "beadhive", "repo": "beadhive", "prefix": "bh"}
    plan = role_execution.RoleLaunchPlan(
        backend="baml", provider=None, detail="provider-unspecified compatibility fixture"
    )
    monkeypatch.setenv("BH_SKIP_SETUP_CHECK", "1")
    monkeypatch.setattr(cli.config, "load", lambda: {"managed_repos": [entry]})
    monkeypatch.setattr(role_execution, "resolve_headless_plan", lambda *_a, **_kw: plan)
    monkeypatch.setattr(role_execution.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        cli, "_apply_role_workspace", lambda *_a: pytest.fail("explain claimed a workspace")
    )
    monkeypatch.setattr(cli.work, "claim", lambda *_a, **_kw: pytest.fail("explain claimed a bead"))
    monkeypatch.setattr(cli, "run", lambda *_a, **_kw: pytest.fail("explain started a process"))
    monkeypatch.setattr(
        worktree, "_run_git", lambda *_a, **_kw: pytest.fail("explain probed with a process")
    )
    monkeypatch.setattr(
        identity, "run", lambda *_a, **_kw: pytest.fail("explain probed workspace with Git")
    )
    monkeypatch.setattr(
        subprocess, "Popen", lambda *_a, **_kw: pytest.fail("explain started a process")
    )
    monkeypatch.setattr(
        run_journal.RunJournal,
        "create",
        lambda *_a, **_kw: pytest.fail("explain created a journal"),
    )
    monkeypatch.setattr(
        cli, "_role_dispatch_dir", lambda: pytest.fail("explain wrote a dispatch artifact")
    )

    secret = "task-token-should-never-print"
    result = cli_runner.invoke(
        app,
        [
            "role",
            "developer",
            "--harness",
            "claude",
            "--bead",
            "bh-example.1",
            "--task",
            secret,
            "--explain",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["decision"] == "runnable"
    assert payload["request"]["bead"] == "bh-example.1"
    assert payload["request"]["task"] == {
        "provided": True,
        "content": "<redacted:instructions>",
    }
    assert payload["request"]["workspace"].endswith("/bh-example.1")
    assert secret not in result.output


def test_cli_role_explain_never_claims_the_bead_workspace(monkeypatch):
    """`--explain` must not resolve/claim `--bead`'s worktree — that's a write, and this is a
    read-only preview."""
    from beadhive import cli, hitch_plugin

    monkeypatch.setenv("BH_SKIP_SETUP_CHECK", "1")
    monkeypatch.setattr(
        cli.config,
        "load",
        lambda: {
            "managed_repos": [
                {"provider": "github", "org": "beadhive", "repo": "beadhive", "prefix": "bh"}
            ]
        },
    )
    monkeypatch.setattr(cli.config, "harness_name", lambda cfg: "claude")
    monkeypatch.setattr(
        cli, "_apply_role_workspace", lambda *a: pytest.fail("workspace resolved under --explain")
    )
    monkeypatch.setattr(
        hitch_plugin, "headless_plan", lambda seat, harness, cfg: ("baml", "built bh-developer")
    )

    result = cli_runner.invoke(app, ["role", "developer", "--bead", "bh-6t49w.7", "--explain"])

    assert result.exit_code == 0, result.output
    assert "mode=headless-safe" in result.output
    assert "backend=baml" in result.output
    assert "bh-6t49w.7" in result.output


def test_cli_role_explain_reports_attached_required_for_an_unsuitable_seat(monkeypatch):
    from beadhive import cli

    monkeypatch.setenv("BH_SKIP_SETUP_CHECK", "1")
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(cli.config, "harness_name", lambda cfg: "claude")

    result = cli_runner.invoke(app, ["role", "supervisor", "--explain"])

    assert result.exit_code == 0, result.output
    assert "mode=attached-required" in result.output
    assert "backend=none" in result.output
    assert "not a headless-capable seat" in result.output


def test_cli_role_explain_unrecognized_seat_reports_attached_required():
    """A seat outside `ROLE_FOR_ACTION` (never a headless target, known-and-installed or not)
    still gets a loud, correct `attached-required` answer — no spurious "not installed" error,
    since `--explain` deliberately does not re-check `role._known_seats()`."""
    result = cli_runner.invoke(app, ["role", "nope", "--explain"])

    assert result.exit_code == 0, result.output
    assert "mode=attached-required" in result.output


def test_cli_role_explain_empty_seat_refuses():
    result = cli_runner.invoke(app, ["role", "--explain"])

    assert result.exit_code == 1
    assert "--explain needs a seat" in result.output


def test_cli_role_explain_dry_run_alias_matches_explain(monkeypatch):
    """`--dry-run` is accepted as an alias, mirroring hitch's own `--explain`/`--dry-run`."""
    from beadhive import cli, hitch_plugin

    monkeypatch.setenv("BH_SKIP_SETUP_CHECK", "1")
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(cli.config, "harness_name", lambda cfg: "claude")
    monkeypatch.setattr(
        hitch_plugin, "headless_plan", lambda seat, harness, cfg: ("baml", "built bh-developer")
    )

    result = cli_runner.invoke(app, ["role", "developer", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "mode=headless-safe" in result.output
