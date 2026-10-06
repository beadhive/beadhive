"""Every bh process entrypoint that can reach the guard, lease or adopt paths registers the
product in-data fence resolver before use (bh-uz46l).

``beadhive.fence_data_port`` is a leaf whose built-in default is "no adapter";
``beadhive.fence_data`` registers :func:`~beadhive.fence_data.resolve_cut_over` as the default
when imported. Each console script (``pyproject.toml`` ``[project.scripts]``) and each
``python -m`` process entry is driven here with its server or app stubbed, starting from a port
reset to the built-in default, and must leave the product resolver registered.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

from beadhive import fence_data, fence_data_port

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fresh_port(monkeypatch):
    """The port as a process that has not imported the product adapter yet sees it."""
    monkeypatch.setattr(fence_data_port, "_default_resolver", fence_data_port._no_fence_data)
    monkeypatch.setattr(fence_data_port, "_fence_data_resolver", fence_data_port._no_fence_data)
    assert fence_data_port.fence_data_for("fx", Path("/nonexistent")) is None
    yield
    assert fence_data_port.default_resolver() is fence_data.resolve_cut_over
    assert fence_data_port._fence_data_resolver is fence_data.resolve_cut_over


def _stub_authority(monkeypatch):
    from beadhive import hq_authority_enforce

    monkeypatch.setattr(hq_authority_enforce, "emit_banner", lambda: None)


def test_bh_cli(fresh_port, monkeypatch):
    from beadhive import cli
    from beadhive.bootstrap import cli as boot

    monkeypatch.setattr(cli, "app", lambda: None)
    monkeypatch.setattr(sys, "argv", ["bh", "work", "ready"])
    boot.main()


def test_python_m_beadhive_cli(fresh_port, monkeypatch):
    from beadhive import cli

    monkeypatch.setattr(cli, "app", lambda: None)
    cli.main()


def test_bh_mcp(fresh_port, monkeypatch):
    from beadhive import mcp
    from beadhive.bootstrap import mcp as boot

    def unavailable():
        raise mcp.MCPUnavailable("stubbed")

    monkeypatch.setattr(mcp, "_init_stdio_telemetry_best_effort", lambda: None)
    monkeypatch.setattr(mcp, "_shutdown_stdio_telemetry_best_effort", lambda cfg: None)
    monkeypatch.setattr(mcp, "serve", unavailable)
    assert boot.main() == 1


def test_bh_host_daemon(fresh_port, monkeypatch):
    from beadhive import host_daemon
    from beadhive.bootstrap import host as boot

    _stub_authority(monkeypatch)
    monkeypatch.setattr(host_daemon, "serve", lambda **_kw: None)
    monkeypatch.setattr(sys, "argv", ["bh-host-daemon"])
    boot.main()


def test_beadhive_frame_bridge(fresh_port, monkeypatch):
    import uvicorn

    from beadhive.bootstrap import frame_bridge as boot

    _stub_authority(monkeypatch)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["beadhive-frame-bridge"])
    boot.main()


def test_python_m_heartbeat_sender(fresh_port):
    from beadhive import heartbeat_sender

    with pytest.raises(SystemExit):
        heartbeat_sender.main(["--no-such-verb"])  # registration runs before argument parsing


def test_every_console_script_is_covered():
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert set(scripts.values()) == {
        "beadhive.bootstrap.cli:main",
        "beadhive.bootstrap.mcp:main",
        "beadhive.bootstrap.host:main",
        "beadhive.bootstrap.frame_bridge:main",
    }
