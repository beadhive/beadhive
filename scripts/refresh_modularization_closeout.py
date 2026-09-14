#!/usr/bin/env python3
"""Check or refresh current-candidate artifact digests in the modularization closeout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs/proof/bh-j5uyb.1-modularization-closeout.json"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


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


def _digests(root: Path, rows: list[dict[str, object]]) -> tuple[dict[str, str], list[str]]:
    digests: dict[str, str] = {}
    errors: list[str] = []
    for row in rows:
        relative = str(row["path"])
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            errors.append(f"{relative}: path escapes repository root")
            continue
        if relative in digests:
            errors.append(f"{relative}: duplicate current-candidate artifact path")
            continue
        if not candidate.is_file():
            errors.append(f"{relative}: artifact is missing")
            continue
        digests[relative] = hashlib.sha256(candidate.read_bytes()).hexdigest()
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


def refresh(root: Path, report_path: Path, *, write: bool) -> tuple[int, list[str]]:
    original = report_path.read_text(encoding="utf-8")
    report = json.loads(original)
    rows = list(_artifact_rows(report))
    if not rows:
        return 1, ["closeout report declares no current-candidate artifact digests"]

    digests, errors = _digests(root, rows)
    if errors:
        return 1, errors

    stale = [str(row["path"]) for row in rows if row.get("sha256") != digests[str(row["path"])]]
    if not stale:
        return 0, []
    if not write:
        return 1, [f"{relative}: stale artifact digest" for relative in stale]

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
    mode.add_argument("--check", action="store_true", help="report every stale artifact")
    mode.add_argument("--write", action="store_true", help="atomically refresh stale digests")
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
