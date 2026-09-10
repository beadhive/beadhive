"""Deterministic compatibility evidence for the official v1 contract release.

The report deliberately treats the manifests under ``docs/schemas/wire`` as the complete
historical publication ledger.  It compares every manifest-declared artifact observation with
the package-owned candidate, without network access or runtime service composition.  Historical
artifacts that are not promoted into the new bundle remain explicitly retained rather than being
misreported as removals.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any

from .contract_release import (
    RELEASE_VERSION,
    _open_directory_no_follow,
    _read_snapshot_file,
    build_release,
    compatibility_errors,
    render_release,
)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


COMPATIBILITY_REPORT_PATH = (
    _repository_root() / "docs" / "proof" / "official-v1-contract-compatibility.json"
)
RELEASE_NOTES_PATH = _repository_root() / "docs" / "releases" / "official-contracts-v1.0.0.md"


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _safe_ledger_path(value: object, label: str) -> Path:
    """Return one exact POSIX path that cannot leave its declared anchor."""

    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}: path must be a non-empty string")
    pure = PurePosixPath(value)
    raw_parts = value.split("/")
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in raw_parts)
        or "\\" in value
        or pure.as_posix() != value
    ):
        raise ValueError(f"{label}: absolute or traversal path is forbidden: {value!r}")
    return Path(*pure.parts)


def _read_ledger_json(wire_descriptor: int, relative: Path, label: str) -> tuple[object, bytes]:
    """Read one stable regular JSON file without following any path component."""

    payload, error = _read_snapshot_file(wire_descriptor, relative, label)
    if error is not None or payload is None:
        raise ValueError(error or f"{label}: cannot read JSON")
    try:
        return json.loads(payload), payload
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}: invalid JSON: {exc}") from exc


def _json_pointer(path: tuple[str, ...]) -> str:
    if not path:
        return ""
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in path)


def _document_differences(
    old: object, candidate: object, path: tuple[str, ...] = ()
) -> list[dict[str, object]]:
    """Return a deterministic, disjoint classification of all JSON value differences.

    Objects are compared recursively.  A list is one ordered contract value, so a changed list is
    recorded as one sequence difference with both canonical digests and item counts.  This avoids
    thousands of misleading positional changes when one catalog member is inserted.
    """

    if type(old) is not type(candidate):
        return [
            {
                "path": _json_pointer(path),
                "kind": "type-changed",
                "before_type": type(old).__name__,
                "after_type": type(candidate).__name__,
            }
        ]
    if isinstance(old, dict):
        assert isinstance(candidate, dict)
        differences: list[dict[str, object]] = []
        for key in sorted(old.keys() - candidate.keys()):
            differences.append({"path": _json_pointer((*path, str(key))), "kind": "removed"})
        for key in sorted(candidate.keys() - old.keys()):
            differences.append({"path": _json_pointer((*path, str(key))), "kind": "added"})
        for key in sorted(old.keys() & candidate.keys()):
            differences.extend(_document_differences(old[key], candidate[key], (*path, str(key))))
        return differences
    if isinstance(old, list):
        assert isinstance(candidate, list)
        if old == candidate:
            return []
        return [
            {
                "path": _json_pointer(path),
                "kind": "sequence-changed",
                "before_items": len(old),
                "after_items": len(candidate),
                "before_sha256": _digest(_canonical_bytes(old)),
                "after_sha256": _digest(_canonical_bytes(candidate)),
            }
        ]
    if old == candidate:
        return []
    return [
        {
            "path": _json_pointer(path),
            "kind": "value-changed",
            "before_sha256": _digest(_canonical_bytes(old)),
            "after_sha256": _digest(_canonical_bytes(candidate)),
        }
    ]


def _candidate_checksums(candidate: dict[str, Any]) -> dict[str, str]:
    rendered = render_release(candidate)
    inventory = json.loads(rendered[Path("inventory.json")])
    return {row["id"]: row["sha256"] for row in inventory["artifacts"]}


def _policy_comparison(old_document: object, candidate_row: dict[str, Any]) -> list[str]:
    old_row = {
        field: deepcopy(candidate_row[field])
        for field in ("id", "family", "version", "kind", "path", "compatibility_policy")
    }
    old_row["document"] = old_document
    return compatibility_errors(
        {"artifacts": [old_row]},
        {"artifacts": [deepcopy(candidate_row)]},
    )


def build_compatibility_report(root: Path | None = None) -> dict[str, Any]:
    """Compare the candidate with every manifest-declared checked-in wire artifact."""

    repository = _repository_root() if root is None else root.resolve()
    wire_root = repository / "docs" / "schemas" / "wire"
    index_path = Path("docs/schemas/wire/index.json")
    try:
        wire_descriptor = _open_directory_no_follow(wire_root)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError("historical wire root: symlink path is forbidden") from exc
        raise ValueError(f"historical wire root: cannot open directory: {exc}") from exc
    try:
        index, _index_payload = _read_ledger_json(
            wire_descriptor, Path("index.json"), "historical index"
        )
        if not isinstance(index, dict):
            raise ValueError("historical index: JSON root must be an object")
        candidate = build_release()
        candidate_by_id = {row["id"]: row for row in candidate["artifacts"]}
        if len(candidate_by_id) != len(candidate["artifacts"]):
            raise ValueError("candidate contains duplicate artifact identities")
        candidate_checksums = _candidate_checksums(candidate)
        comparisons: list[dict[str, Any]] = []
        historical_ids: set[str] = set()
        historical_release_count = 0

        releases = index.get("releases")
        if not isinstance(releases, list):
            raise ValueError("historical index: releases must be an array")
        for release_entry in releases:
            if not isinstance(release_entry, dict):
                raise ValueError("historical index: release entries must be objects")
            version = release_entry.get("version")
            manifest_value = release_entry.get("manifest")
            if not isinstance(version, str):
                raise ValueError("wire index release entries require string version")
            manifest_relative = _safe_ledger_path(manifest_value, "historical manifest")
            manifest, _manifest_payload = _read_ledger_json(
                wire_descriptor, manifest_relative, "historical manifest"
            )
            if not isinstance(manifest, dict):
                raise ValueError(f"{manifest_relative}: manifest root must be an object")
            if manifest.get("release_version") != version:
                raise ValueError(f"{manifest_relative}: release version does not match index")
            historical_release_count += 1
            artifacts = manifest.get("artifacts")
            if not isinstance(artifacts, list):
                raise ValueError(f"{manifest_relative}: artifacts must be an array")
            for historical_row in artifacts:
                if not isinstance(historical_row, dict):
                    raise ValueError(f"{manifest_relative}: artifact rows must be objects")
                artifact_id = historical_row.get("id")
                if not isinstance(artifact_id, str):
                    raise ValueError(f"{manifest_relative}: artifact requires string id")
                artifact_relative = _safe_ledger_path(
                    historical_row.get("path"), "historical artifact"
                )
                artifact_wire_relative = manifest_relative.parent / artifact_relative
                document, payload = _read_ledger_json(
                    wire_descriptor, artifact_wire_relative, "historical artifact"
                )
                historical_ids.add(artifact_id)
                embedded_id = document.get("$id") if isinstance(document, dict) else None
                if embedded_id is not None and embedded_id != artifact_id:
                    raise ValueError(f"{artifact_wire_relative}: historical artifact id mismatch")
                candidate_row = candidate_by_id.get(artifact_id)
                differences: list[dict[str, object]]
                policy_errors: list[str]
                candidate_sha256: str | None
                if candidate_row is None:
                    classification = "legacy-only-retained"
                    candidate_sha256 = None
                    policy_errors = []
                    differences = [{"path": "", "kind": "not-promoted"}]
                else:
                    candidate_sha256 = candidate_checksums[artifact_id]
                    differences = _document_differences(document, candidate_row["document"])
                    policy_errors = _policy_comparison(document, candidate_row)
                    if not differences:
                        classification = "identical"
                    elif not policy_errors:
                        classification = "compatible-additive"
                    else:
                        classification = "policy-divergence-retained"
                comparisons.append(
                    {
                        "historical_release": version,
                        "historical_manifest": (
                            Path("docs/schemas/wire") / manifest_relative
                        ).as_posix(),
                        "historical_path": (
                            Path("docs/schemas/wire") / artifact_wire_relative
                        ).as_posix(),
                        "artifact_id": artifact_id,
                        "historical_sha256": _digest(payload),
                        "candidate_sha256": candidate_sha256,
                        "classification": classification,
                        "differences": differences,
                        "policy_errors": policy_errors,
                    }
                )
    finally:
        os.close(wire_descriptor)

    candidate_rows = [
        {
            "artifact_id": row["id"],
            "family": row["family"],
            "path": row["path"],
            "sha256": candidate_checksums[row["id"]],
            "source_owner": row["source_owner"],
            "compatibility_policy": row["compatibility_policy"],
            "classification": (
                "previously-published" if row["id"] in historical_ids else "new-official-artifact"
            ),
        }
        for row in candidate["artifacts"]
    ]
    counts: dict[str, int] = {}
    for row in comparisons:
        classification = row["classification"]
        counts[classification] = counts.get(classification, 0) + 1
    return {
        "format_version": 1,
        "release_version": RELEASE_VERSION,
        "historical_index": index_path.as_posix(),
        "candidate_inventory": f"src/beadhive/schemas/contracts/v{RELEASE_VERSION}/inventory.json",
        "policy": {
            "historical_absence": (
                "An artifact absent from the candidate remains supported only in its immutable "
                "docs/schemas/wire release; absence is not silently classified as removal."
            ),
            "policy_divergence": (
                "A shared identity that fails its candidate policy is retained in its historical "
                "release and must not be treated as superseded by the candidate."
            ),
        },
        "summary": {
            "historical_releases": historical_release_count,
            "historical_artifact_observations": len(comparisons),
            "candidate_artifacts": len(candidate_rows),
            "classifications": dict(sorted(counts.items())),
        },
        "historical_comparisons": comparisons,
        "candidate_artifacts": candidate_rows,
    }


def validate_compatibility_report(
    path: Path = COMPATIBILITY_REPORT_PATH, root: Path | None = None
) -> tuple[str, ...]:
    """Return deterministic drift errors for the checked compatibility report."""

    expected = _canonical_bytes(build_compatibility_report(root))
    try:
        actual = path.read_bytes()
    except FileNotFoundError:
        return (f"{path}: compatibility report is missing",)
    if actual != expected:
        return (f"{path}: compatibility report drifted; regenerate it",)
    return ()


def write_compatibility_report(
    path: Path = COMPATIBILITY_REPORT_PATH, root: Path | None = None
) -> None:
    """Write the canonical report, creating only its immediate parent directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_bytes(build_compatibility_report(root)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail if checked evidence drifted")
    mode.add_argument("--write", action="store_true", help="write deterministic evidence")
    parser.add_argument("--output", type=Path, default=COMPATIBILITY_REPORT_PATH)
    args = parser.parse_args(argv)
    if args.check:
        errors = validate_compatibility_report(args.output)
        for error in errors:
            print(f"official contract release evidence: {error}")
        if errors:
            return 1
        print(f"official contract release evidence v{RELEASE_VERSION}: current")
        return 0
    write_compatibility_report(args.output)
    print(f"official contract release evidence v{RELEASE_VERSION}: generated")
    return 0


__all__ = (
    "COMPATIBILITY_REPORT_PATH",
    "RELEASE_NOTES_PATH",
    "build_compatibility_report",
    "main",
    "validate_compatibility_report",
    "write_compatibility_report",
)
