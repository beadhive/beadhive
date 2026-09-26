"""Immutable Development Frame Bridge handoff guardrails."""

from __future__ import annotations

import configparser
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def test_frame_bridge_service_is_capability_free_and_loopback_network_only() -> None:
    unit = (ROOT / "deploy/systemd/beadhive-frame-bridge-dev.service.example").read_text(
        encoding="utf-8"
    )
    assert "CapabilityBoundingSet=\n" in unit
    assert "IPAddressDeny=any" in unit
    assert "IPAddressAllow=localhost" in unit
    assert "NoNewPrivileges=true" in unit


def test_frame_bridge_is_the_only_core_bridge_process_command() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = project["project"]["scripts"]
    target = "beadhive.bootstrap.frame_bridge:main"
    assert scripts["beadhive-frame-bridge"] == target
    assert "beadhive-gateway" not in scripts

    contract = (ROOT / "docs/FRAME_BRIDGE_V1.md").read_text(encoding="utf-8")
    assert "multi-frame" in contract
    assert "**Beadhive Gateway**" in contract
    assert "sibling `beadhive-gateway` repository" in contract


@pytest.mark.skipif(shutil.which("uv") is None, reason="wheel build needs the uv binary")
def test_wheel_contains_only_the_frame_bridge_core_entry_point(tmp_path: Path) -> None:
    """Prove the installed artifact, not only the source manifest, carries the rename."""
    output = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--offline", "--wheel", "--out-dir", str(output)],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    (wheel,) = output.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        shipped = set(archive.namelist())
        (entry_points_path,) = (
            path for path in shipped if path.endswith(".dist-info/entry_points.txt")
        )
        parser = configparser.ConfigParser()
        parser.read_string(archive.read(entry_points_path).decode())

    assert {
        "beadhive/bootstrap/frame_bridge.py",
        "beadhive/frame_bridge.py",
        "beadhive/frame_bridge_runtime.py",
    } <= shipped
    assert "beadhive/remote_gateway.py" not in shipped
    assert "beadhive/remote_gateway_runtime.py" not in shipped
    assert parser["console_scripts"]["beadhive-frame-bridge"] == (
        "beadhive.bootstrap.frame_bridge:main"
    )
    assert "beadhive-gateway" not in parser["console_scripts"]


def test_legacy_gateway_modules_and_environment_aliases_are_absent() -> None:
    assert not (ROOT / "src/beadhive/remote_gateway.py").exists()
    assert not (ROOT / "src/beadhive/remote_gateway_runtime.py").exists()

    runtime = (ROOT / "src/beadhive/frame_bridge_runtime.py").read_text(encoding="utf-8")
    assert "BEADHIVE_GATEWAY_JWKS_FILE" not in runtime
    assert "BEADHIVE_GATEWAY_SUBJECTS_FILE" not in runtime


def test_gateway_wire_identifiers_survive_the_component_rename() -> None:
    from beadhive import frame_bridge, gateway_read

    assert frame_bridge.CONTRACT_VERSION == "gateway.v1"
    assert gateway_read.CONTRACT_VERSION == "gateway.read.v1"
    assert frame_bridge.DEVELOPMENT_ISSUER == "https://rapid-snail-6758.clerk.accounts.dev"
