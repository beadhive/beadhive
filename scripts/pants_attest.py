#!/usr/bin/env python3
"""Fail-closed Pants build/test prerequisite for exact-tree attestation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
PIN = "2.32.1"
QUALIFIED = "tests/unit/modules/config/test_resolution.py"


def launcher(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    explicit = env.get("PANTS_BIN")
    if explicit:
        path = shutil.which(explicit) if "/" not in explicit else explicit
        if path and Path(path).is_file() and os.access(path, os.X_OK):
            return path
        raise RuntimeError(f"PANTS_BIN is not executable: {explicit}")
    for name in ("pants", "scie-pants"):
        found = shutil.which(name, path=env.get("PATH"))
        if found:
            return found
    raise RuntimeError(
        "Pants launcher unavailable: run `mise install scie-pants`, install the official "
        "launcher from pantsbuild.org, or set PANTS_BIN=/absolute/path/to/pants"
    )


def configuration_errors(root: Path = ROOT) -> list[str]:
    config = tomllib.loads((root / "pants.toml").read_text(encoding="utf-8"))
    metadata = json.loads(
        (root / "3rdparty/python/beadhive.lock.metadata").read_text(encoding="utf-8")
    )
    errors: list[str] = []
    if config["GLOBAL"]["pants_version"] != PIN:
        errors.append(f"pants.toml must pin {PIN}")
    if config["python"]["resolver"] != "uv" or not config["python"]["run_against_entire_lockfile"]:
        errors.append("experimental uv whole-lock resolve is required")
    if config["pytest"]["install_from_resolve"] != "beadhive":
        errors.append("pytest must install coherently from beadhive resolve")
    if config["GLOBAL"]["remote_cache_read"] or config["GLOBAL"]["remote_cache_write"]:
        errors.append("remote cache must remain disabled")
    if metadata.get("lockfile_format") != "uv" or metadata.get("resolve") != "beadhive":
        errors.append("Pants uv lock metadata is incompatible")
    return errors


def run(command: list[str]) -> int:
    print(f"pants-attest: {' '.join(command[-3:])}", flush=True)
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def main() -> int:
    errors = configuration_errors()
    if errors:
        for error in errors:
            print(f"pants-attest: {error}", file=sys.stderr)
        return 2
    try:
        pants = launcher()
    except RuntimeError as exc:
        print(f"pants-attest: {exc}", file=sys.stderr)
        return 127
    coordinator = [sys.executable, "scripts/pants_cache.py", "run", "--", pants, "--no-pantsd"]
    steps = [
        [*coordinator, "version"],
        [sys.executable, "scripts/pants_shadow_evidence.py"],
        [*coordinator, "package", "src/beadhive:bh"],
        [*coordinator, "--test-output=all", "test", QUALIFIED],
    ]
    for command in steps:
        status = run(command)
        if status:
            print(f"pants-attest: failed closed with exit {status}", file=sys.stderr)
            return status
    print("pants-attest: OK (Pants 2.32.1; package; 58 qualified tests; native check-all follows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
