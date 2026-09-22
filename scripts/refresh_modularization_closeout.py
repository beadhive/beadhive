#!/usr/bin/env python3
"""Check or restore the frozen artifact digests in the modularization closeout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs/proof/bh-j5uyb.1-modularization-closeout.json"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# The closeout remained byte-identical after this accepted source snapshot.  Its
# legacy ``current_candidate`` label describes that snapshot, not the live tree.
FROZEN_EVIDENCE_REVISION = "1e1682e92ffaa004debf958f7926385dbae670d9"


def _artifact_bytes(relative: str, candidate: Path) -> bytes:
    """Hash pyproject structure independently of the release-only version scalar."""
    return _normalized_artifact_bytes(relative, candidate.read_bytes())


def _normalized_artifact_bytes(relative: str, content: bytes) -> bytes:
    if relative == "pyproject.toml":
        content = re.sub(
            rb'(?m)^(version\s*=\s*)"[^"]+"$', rb'\1"<release-version>"', content, count=1
        )
    return content


def _artifact_rows(report: dict[str, object]) -> Iterator[dict[str, object]]:
    current = report["evidence_inventory"]["current_candidate"]

    def walk(value: object) -> Iterator[dict[str, object]]:
        if isinstance(value, dict):
            if isinstance(value.get("path"), str) and "sha256" in value:
                yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    yield from walk(current)


def _digests(
    root: Path,
    rows: list[dict[str, object]],
    frozen_revision: str,
) -> tuple[dict[str, str], list[str]]:
    digests: dict[str, str] = {}
    errors: list[str] = []
    for row in rows:
        relative = str(row["path"])
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            errors.append(f"{relative}: path escapes repository root")
            continue
        if relative in digests:
            errors.append(f"{relative}: duplicate current-candidate artifact path")
            continue
        candidate = subprocess.run(
            ["git", "show", f"{frozen_revision}:{relative}"],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if candidate.returncode != 0:
            errors.append(f"{relative}: artifact is missing at frozen closeout revision")
            continue
        content = _normalized_artifact_bytes(relative, candidate.stdout)
        digests[relative] = hashlib.sha256(content).hexdigest()
    return digests, errors


def _updated_text(
    original: str, report: dict[str, object], rows: list[dict[str, object]], digests: dict[str, str]
) -> str:
    updated = original
    expected = json.loads(json.dumps(report))
    expected_rows = {str(row["path"]): row for row in _artifact_rows(expected)}
    for row in rows:
        relative = str(row["path"])
        old = row.get("sha256")
        if not isinstance(old, str) or not SHA256.fullmatch(old):
            raise ValueError(f"{relative}: recorded sha256 is not a lowercase 64-character digest")
        replacement = digests[relative]
        path_token = json.dumps(relative)
        pattern = re.compile(
            rf'("path"\s*:\s*{re.escape(path_token)}(?:(?!\}}).)*?"sha256"\s*:\s*")'
            rf'{re.escape(old)}(")',
            re.DOTALL,
        )
        updated, count = pattern.subn(rf"\g<1>{replacement}\g<2>", updated, count=1)
        if count != 1:
            raise ValueError(f"{relative}: could not locate its generated sha256 field")
        expected_rows[relative]["sha256"] = replacement

    if json.loads(updated) != expected:
        raise ValueError("refresher would change fields other than current-candidate sha256 values")
    return updated


def refresh(
    root: Path,
    report_path: Path,
    *,
    write: bool,
    frozen_revision: str = FROZEN_EVIDENCE_REVISION,
) -> tuple[int, list[str]]:
    original = report_path.read_text(encoding="utf-8")
    report = json.loads(original)
    rows = list(_artifact_rows(report))
    if not rows:
        return 1, ["closeout report declares no current-candidate artifact digests"]

    digests, errors = _digests(root, rows, frozen_revision)
    for row in rows:
        relative = str(row["path"])
        recorded = row.get("sha256")
        if not isinstance(recorded, str) or not SHA256.fullmatch(recorded):
            errors.append(f"{relative}: recorded sha256 is not a lowercase 64-character digest")
    stale = [
        str(row["path"])
        for row in rows
        if str(row["path"]) in digests
        and isinstance(row.get("sha256"), str)
        and SHA256.fullmatch(str(row["sha256"]))
        and row["sha256"] != digests[str(row["path"])]
    ]
    diagnostics = [*errors, *(f"{relative}: stale artifact digest" for relative in stale)]
    if not diagnostics:
        return 0, []
    if not write or errors:
        return 1, diagnostics

    try:
        updated = _updated_text(original, report, rows, digests)
    except ValueError as exc:
        return 1, [str(exc)]
    fd, temporary = tempfile.mkstemp(prefix=f".{report_path.name}.", dir=report_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, report_path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return 0, [f"{relative}: refreshed artifact digest" for relative in stale]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="check the frozen evidence snapshot")
    mode.add_argument("--write", action="store_true", help="restore frozen evidence digests")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--report", type=Path, default=REPORT, help=argparse.SUPPRESS)
    args = parser.parse_args()

    try:
        code, messages = refresh(args.root.resolve(), args.report.resolve(), write=args.write)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        code, messages = 1, [f"could not refresh closeout digests: {exc}"]
    for message in messages:
        print(message)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
