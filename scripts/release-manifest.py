#!/usr/bin/env python3
"""Emit the release-manifest predicate for a built beadhive wheel.

Computes the ``beadhive-tree-sha256-v1`` measured digest from the wheel's zip entries under
``beadhive/`` using the same pure fold as ``installed_release()``, and prints the predicate
JSON ``{version, measured_digest, algorithm, file_count, wheel_sha256, commit}``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _measurement():
    # Load by path: the module is stdlib-only at import time, so no install is required.
    spec = importlib.util.spec_from_file_location(
        "_release_measurement", ROOT / "src" / "beadhive" / "release_measurement.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def wheel_version(wheel: zipfile.ZipFile) -> str:
    for name in wheel.namelist():
        if name.endswith(".dist-info/METADATA"):
            for line in wheel.read(name).decode().splitlines():
                if line.startswith("Version: "):
                    return line[len("Version: ") :].strip()
    raise SystemExit("wheel has no Version in METADATA")


def build_manifest(wheel_path: Path, commit: str) -> dict[str, object]:
    measurement = _measurement()
    with zipfile.ZipFile(wheel_path) as wheel:
        entries = [
            (info.filename, wheel.read(info)) for info in wheel.infolist() if not info.is_dir()
        ]
        version = wheel_version(wheel)
    digest, count = measurement.fold_digest(entries)
    if not count:
        raise SystemExit("wheel contains no beadhive/ files to measure")
    return {
        "version": version,
        "measured_digest": digest,
        "algorithm": measurement.ALGORITHM,
        "file_count": count,
        "wheel_sha256": hashlib.sha256(wheel_path.read_bytes()).hexdigest(),
        "commit": commit,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--commit", required=True, help="release commit SHA")
    parser.add_argument("--output", type=Path, help="write JSON here instead of stdout")
    args = parser.parse_args(argv)
    text = json.dumps(build_manifest(args.wheel, args.commit), indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
