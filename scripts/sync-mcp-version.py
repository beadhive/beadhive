#!/usr/bin/env python3
"""Keep MCP registry and explicit client metadata on the Python release version."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLISHER_META = "io.modelcontextprotocol.registry/publisher-provided"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without changing metadata")
    parser.add_argument("--version", help="require the project version to match this release tag")
    args = parser.parse_args()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    version = project["version"]
    if args.version is not None and args.version != version:
        parser.error(f"release version {args.version!r} does not match project version {version!r}")
    path = ROOT / "server.json"
    original = json.loads(path.read_text())
    expected = json.loads(json.dumps(original))
    expected["version"] = version
    package = expected["packages"][0]
    package["version"] = version
    package["identifier"] = project["name"]
    publisher = expected["_meta"][PUBLISHER_META]
    installed = publisher["clientConfiguration"]["mcpServers"]["bh"]
    installed["command"] = "bh-mcp"
    installed["args"] = []
    client = publisher["uvxClientConfiguration"]["mcpServers"]["bh"]
    client["command"] = "uvx"
    client["args"] = ["--from", f"{project['name']}=={version}", "bh-mcp"]
    if args.check:
        if original != expected:
            parser.error("server.json is stale; run python3 scripts/sync-mcp-version.py")
    else:
        path.write_text(json.dumps(expected, indent=2) + "\n")
    print(f"MCP release metadata matches beadhive {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
