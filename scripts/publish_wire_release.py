#!/usr/bin/env python3
"""Publish the live operation catalog as the next immutable wire release.

The live catalog (``beadhive.operation_catalog.document()``) always tracks ``index.json``
``latest``. When the catalog changes -- typically because a CLI verb was registered -- this
command cuts the next minor release: it copies the latest release directory, renders the live
catalog into it, bumps the manifest and conformance ``release_version`` and appends the release
to ``index.json``. Published releases are never edited.

A release this branch has already cut (its version is absent from the integration base's
``index.json``) is not yet published, so re-running the command refreshes that release in place
instead of cutting another minor. The base is the merge base with ``BH_WIRE_SCHEMA_BASE_REF``
(default ``main``), the same ref ``scripts/check_wire_schema_compat.py`` compares against.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WIRE = Path("docs") / "schemas" / "wire"
INDEX = WIRE / "index.json"
CATALOG = "operation-catalog-v1.json"
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_RELEASE_VERSION = re.compile(r'("release_version"\s*:\s*")([^"]+)(")')


@dataclass(frozen=True)
class PublishResult:
    version: str
    action: str  # "cut" | "refreshed" | "current"


def render_catalog(catalog: dict[str, Any] | None = None) -> str:
    """Return the canonical bytes of the live (or supplied) operation catalog."""
    if catalog is None:
        from beadhive.operation_catalog import document

        catalog = document()
    return json.dumps(catalog, indent=2, sort_keys=True) + "\n"


def _semver(value: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid wire release version: {value!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def _load_index(root: Path) -> dict[str, Any]:
    return json.loads((root / INDEX).read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def latest_release_dir(root: Path) -> Path:
    """Return the directory of the release ``index.json`` names as latest."""
    index = _load_index(root)
    latest = str(index["latest"])
    entry = next(row for row in index["releases"] if row["version"] == latest)
    return root / WIRE / Path(entry["manifest"]).parent


def _base_versions(root: Path, base_ref: str) -> set[str] | None:
    """Return the release versions listed at the merge base, or None when there is no base."""
    merge_base = subprocess.run(
        ["git", "merge-base", "HEAD", base_ref],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if merge_base.returncode != 0:
        return None
    shown = subprocess.run(
        ["git", "show", f"{merge_base.stdout.strip()}:{INDEX.as_posix()}"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if shown.returncode != 0:
        return None
    return {str(row["version"]) for row in json.loads(shown.stdout)["releases"]}


def _bump_release_version(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    updated, count = _RELEASE_VERSION.subn(
        lambda match: (
            match.group(1) + new + match.group(3) if match.group(2) == old else match.group(0)
        ),
        text,
        count=1,
    )
    if count != 1 or updated == text:
        raise ValueError(f"{path}: expected release_version {old!r}")
    path.write_text(updated, encoding="utf-8")


def publish(
    root: Path = ROOT,
    *,
    catalog: dict[str, Any] | None = None,
    base_ref: str | None = None,
) -> PublishResult:
    """Make ``index.json`` latest carry the live catalog, cutting a minor release if needed."""
    base_ref = base_ref or os.environ.get("BH_WIRE_SCHEMA_BASE_REF", "main")
    rendered = render_catalog(catalog)
    index = _load_index(root)
    latest = str(index["latest"])
    latest_dir = latest_release_dir(root)
    target = latest_dir / CATALOG
    current = target.read_text(encoding="utf-8") if target.is_file() else None

    published = _base_versions(root, base_ref)
    if published is not None and latest not in published:
        # This branch cut `latest`; it is not published yet, so it stays mutable here.
        if current == rendered:
            return PublishResult(latest, "current")
        target.write_text(rendered, encoding="utf-8")
        return PublishResult(latest, "refreshed")
    if current == rendered:
        return PublishResult(latest, "current")

    major, minor, _patch = _semver(latest)
    version = f"{major}.{minor + 1}.0"
    if any(row["version"] == version for row in index["releases"]):
        raise ValueError(f"wire release {version} is already listed in {INDEX}")
    release_dir = root / WIRE / f"v{version}"
    if release_dir.exists():
        raise ValueError(f"{release_dir.relative_to(root)} already exists")
    shutil.copytree(latest_dir, release_dir)
    manifest = json.loads((release_dir / "release.json").read_text(encoding="utf-8"))
    if manifest.get("release_version") != latest:
        raise ValueError(f"{latest_dir.relative_to(root)}/release.json: expected {latest!r}")
    manifest["release_version"] = version
    manifest.pop("deprecated", None)
    _write_json(release_dir / "release.json", manifest)
    _bump_release_version(release_dir / str(manifest["conformance_fixtures"]), latest, version)
    (release_dir / CATALOG).write_text(rendered, encoding="utf-8")
    index["releases"].append(
        {"version": version, "major": major, "manifest": f"v{version}/release.json"}
    )
    index["latest"] = version
    _write_json(root / INDEX, index)
    return PublishResult(version, "cut")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        result = publish()
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        print(f"wire-publish: {exc}")
        return 1
    messages = {
        "cut": "cut wire release {version} from the live operation catalog",
        "refreshed": "refreshed unpublished wire release {version} from the live operation catalog",
        "current": "wire release {version} already carries the live operation catalog",
    }
    print("wire-publish: " + messages[result.action].format(version=result.version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
