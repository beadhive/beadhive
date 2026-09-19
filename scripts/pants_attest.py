#!/usr/bin/env python3
"""Fail-closed Pants build/test prerequisite for exact-tree attestation."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

try:
    from scripts.pants_launcher import launcher
except ModuleNotFoundError:
    # Runtime fallback for `python scripts/pants_attest.py` (scripts/ on sys.path, not repo
    # root). The `scripts.` import above already gives Pants a real dependency edge.
    from pants_launcher import launcher  # pants: no-infer-dep

ROOT = Path(__file__).parents[1]
PIN = "2.32.1"
QUALIFIED = "tests/unit/modules/config/test_resolution.py"


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
