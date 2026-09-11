#!/usr/bin/env python3
"""Reproduce source, wheel, installed-surface, and live Frame Bridge parity.

This is deliberately a release-validation driver rather than a product entry point.  It builds
the current tree offline, installs it into a disposable environment, compares representative
pre-refactor public bytes and process results, then runs the real daemon/Frame Bridge harness
through the installed console scripts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

ROOT = Path(__file__).resolve().parents[1]
BASELINE_REVISION = "739349806ead27c94282219b1befb796ba73b583"
LIVE_HARNESS = ROOT / "tests" / "harness" / "frame_bridge_live.py"
TIMEOUT = 120
STABLE_CONSOLE_SCRIPTS = {"bh", "bh-host-daemon", "bh-mcp"}
EXPECTED_CONSOLE_SCRIPTS = STABLE_CONSOLE_SCRIPTS | {"beadhive-frame-bridge"}
BYTE_STABLE_FIXTURES = (
    "src/beadhive/catalog/gateway-read-v1-development.json",
    "src/beadhive/catalog/gateway-read-v1-development.manifest.json",
    "tests/fixtures/live_ingress/v1/L1.json",
    "tests/fixtures/live_ingress/v1/L2.json",
    "tests/fixtures/live_ingress/v1/L3.json",
    "tests/fixtures/live_ingress/v1/L4.json",
    "tests/fixtures/live_ingress/v1/report.json",
    "tests/fixtures/operator-event-ui-conformance.sse",
    "tests/fixtures/stub_seat.py",
)
PLUGIN_GROUPS = ("orca", "observaloop", "hitch", "herdr", "repowise", "git-workspace")


def _run(
    argv: list[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
    timeout: int = TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=True,
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _normalized_help(payload: str) -> str:
    """Ignore only the argv[0] spelling introduced by the baseline ``python -c`` runner."""

    return re.sub(r"(?m)^( Usage: )\S+", r"\1<program>", payload, count=1)


def _baseline_tree(destination: Path) -> Path:
    archive = destination / "baseline.tar"
    source = destination / "baseline"
    source.mkdir()
    _run(
        ["git", "archive", "--format=tar", f"--output={archive}", BASELINE_REVISION],
    )
    with tarfile.open(archive) as stream:
        stream.extractall(source, filter="data")
    return source


def _fixture_parity() -> dict[str, str]:
    digests: dict[str, str] = {}
    for relative in BYTE_STABLE_FIXTURES:
        before = subprocess.run(
            ["git", "show", f"{BASELINE_REVISION}:{relative}"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
        after = (ROOT / relative).read_bytes()
        if after != before:
            raise AssertionError(f"pre-refactor fixture changed: {relative}")
        digests[relative] = _sha256(after)
    return digests


def _installed_entry_points(python: Path) -> dict[str, str]:
    probe = """
import json
from importlib.metadata import entry_points
print(json.dumps({item.name: item.value for item in entry_points(group='console_scripts')
                  if item.name in {'bh', 'bh-mcp', 'bh-host-daemon',
                                   'beadhive-frame-bridge', 'beadhive-gateway'}}, sort_keys=True))
"""
    return json.loads(_run([str(python), "-c", probe]).stdout)


async def _mcp_surface(
    command: Path,
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_file: Path,
) -> dict[str, Any]:
    transport = StdioTransport(
        command=str(command),
        args=args,
        cwd=str(cwd),
        env=env,
        keep_alive=False,
        log_file=log_file,
    )
    async with Client(transport, timeout=30) as client:
        tools = await client.list_tools()
    return {tool.name: tool.inputSchema for tool in sorted(tools, key=lambda item: item.name)}


def _public_process_parity(
    *,
    baseline: Path,
    python: Path,
    executable_dir: Path,
    env: dict[str, str],
    scratch: Path,
) -> tuple[dict[str, str], list[str]]:
    old_env = {**env, "PYTHONPATH": str(baseline / "src")}
    old_cli = [str(python), "-c", "from beadhive.cli import main; main()"]
    current_cli = [str(executable_dir / "bh")]
    help_digests: dict[str, str] = {}
    for suffix in (["--help"], *(["plugin", name, "--help"] for name in PLUGIN_GROUPS)):
        before = _run(old_cli + list(suffix), cwd=baseline, env=old_env).stdout
        after = _run(current_cli + list(suffix), env=env).stdout
        normalized_before = _normalized_help(before)
        normalized_after = _normalized_help(after)
        if normalized_after != normalized_before:
            label = " ".join(suffix)
            raise AssertionError(f"public CLI help changed beyond argv[0]: {label}")
        help_digests[" ".join(suffix)] = _sha256(normalized_after.encode())

    old_server = "from beadhive.mcp import main; raise SystemExit(main())"

    async def compare_mcp() -> tuple[dict[str, Any], dict[str, Any]]:
        before = await _mcp_surface(
            python,
            ["-c", old_server],
            cwd=baseline,
            env=old_env,
            log_file=scratch / "mcp-before.log",
        )
        after = await _mcp_surface(
            executable_dir / "bh-mcp",
            [],
            cwd=ROOT,
            env=env,
            log_file=scratch / "mcp-after.log",
        )
        return before, after

    before_mcp, after_mcp = asyncio.run(compare_mcp())
    if after_mcp != before_mcp:
        raise AssertionError("installed MCP schemas changed from the pre-refactor public surface")
    return help_digests, sorted(after_mcp)


def _installed_live_proof(
    *,
    python: Path,
    executable_dir: Path,
    scratch: Path,
    env: dict[str, str],
) -> dict[str, str]:
    if sys.platform != "linux" or shutil.which("unshare") is None or shutil.which("ip") is None:
        raise RuntimeError("the exact live proof requires Linux, unshare, and ip; it never skips")
    completed = _run(
        [
            "unshare",
            "--user",
            "--map-root-user",
            "--net",
            str(python),
            str(LIVE_HARNESS),
            str(scratch / "live"),
            str(executable_dir),
        ],
        env=env,
    )
    result = json.loads(completed.stdout.strip())
    if result != {"status": "ok", "contractVersion": "gateway.v1"}:
        raise AssertionError(f"unexpected live proof result: {result!r}")
    return result


def main() -> int:
    fixtures = _fixture_parity()
    source_commit = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    source_tree = _run(["git", "rev-parse", "HEAD^{tree}"]).stdout.strip()

    with tempfile.TemporaryDirectory(prefix="bh-final-refactor-parity-") as raw_scratch:
        scratch = Path(raw_scratch)
        baseline = _baseline_tree(scratch)
        wheel_dir = scratch / "dist"
        wheel_dir.mkdir()
        _run(["uv", "build", "--offline", "--wheel", "--out-dir", str(wheel_dir)])
        (wheel,) = wheel_dir.glob("beadhive-*.whl")

        virtualenv = scratch / "venv"
        _run(["uv", "venv", "--python", sys.executable, str(virtualenv)])
        python = virtualenv / "bin" / "python"
        executable_dir = virtualenv / "bin"
        _run(
            [
                "uv",
                "pip",
                "install",
                "--offline",
                "--python",
                str(python),
                str(wheel),
            ]
        )

        home = scratch / "home"
        workspace = scratch / "workspace"
        home.mkdir()
        workspace.mkdir()
        env = {
            **os.environ,
            "BH_HOME": str(home),
            "GIT_WORKSPACE": str(workspace),
            "OTEL_SDK_DISABLED": "true",
            "NO_COLOR": "1",
        }
        (home / "config.yaml").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "providers": ["github"],
                    "managed_repos": [],
                    "exclude": {"orgs": [], "repos": []},
                    "otel": {"enabled": False, "protocol": "grpc"},
                }
            ),
            encoding="utf-8",
        )

        entry_points = _installed_entry_points(python)
        if set(entry_points) != EXPECTED_CONSOLE_SCRIPTS:
            raise AssertionError(f"unexpected installed console scripts: {entry_points!r}")
        if "beadhive-gateway" in entry_points or (executable_dir / "beadhive-gateway").exists():
            raise AssertionError("the unreleased legacy beadhive-gateway entry point survived")
        for command in EXPECTED_CONSOLE_SCRIPTS:
            if not (executable_dir / command).is_file():
                raise AssertionError(f"installed console script missing: {command}")

        version = _run([str(executable_dir / "bh"), "--version"], env=env).stdout.strip()
        help_digests, mcp_tools = _public_process_parity(
            baseline=baseline,
            python=python,
            executable_dir=executable_dir,
            env=env,
            scratch=scratch,
        )
        _run([str(python), "-m", "beadhive.daemon_openapi", "--check"], env=env)
        _run([str(python), "-m", "beadhive.gateway_contract", "--check"], env=env)
        live = _installed_live_proof(
            python=python,
            executable_dir=executable_dir,
            scratch=scratch,
            env=env,
        )

        print(
            json.dumps(
                {
                    "status": "ok",
                    "source": {"commit": source_commit, "tree": source_tree},
                    "baseline": {
                        "revision": BASELINE_REVISION,
                        "byteStableFixtures": fixtures,
                        "helpDigests": help_digests,
                        "mcpTools": mcp_tools,
                    },
                    "wheel": {
                        "filename": wheel.name,
                        "sha256": _sha256(wheel.read_bytes()),
                        "version": version,
                        "consoleScripts": entry_points,
                        "legacyGatewayAbsent": True,
                    },
                    "canonicalContracts": {
                        "daemonOpenAPI": "current",
                        "gatewayWire": "current",
                    },
                    "live": live,
                },
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
