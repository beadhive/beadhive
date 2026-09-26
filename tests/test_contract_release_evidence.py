"""Release-evidence contract for the official v1 bundle."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from beadhive.contract_release import RELEASE_VERSION, build_release, load_release, release_root
from beadhive.contract_release_evidence import (
    COMPATIBILITY_REPORT_PATH,
    RELEASE_NOTES_PATH,
    build_compatibility_report,
    validate_compatibility_report,
)

ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _minimal_manifest(artifact_path: str) -> dict[str, object]:
    return {
        "format_version": 1,
        "release_version": "0.1.0",
        "schema_draft": "https://json-schema.org/draft/2020-12/schema",
        "artifacts": [
            {
                "id": "urn:beadhive:wire-schema:historical-probe:1",
                "contract_version": 1,
                "path": artifact_path,
            }
        ],
        "conformance_fixtures": "conformance.json",
    }


def _minimal_wire_root(repository: Path, manifest_path: str) -> Path:
    wire_root = repository / "docs" / "schemas" / "wire"
    _write_json(
        wire_root / "index.json",
        {
            "format_version": 1,
            "latest": "0.1.0",
            "releases": [
                {"version": "0.1.0", "major": 0, "manifest": manifest_path},
            ],
        },
    )
    return wire_root


def _published_rows() -> list[tuple[str, dict[str, object]]]:
    wire_root = ROOT / "docs" / "schemas" / "wire"
    index = json.loads((wire_root / "index.json").read_bytes())
    rows: list[tuple[str, dict[str, object]]] = []
    for release in index["releases"]:
        if release.get("deprecated") is True:
            continue
        manifest = json.loads((wire_root / release["manifest"]).read_bytes())
        rows.extend((release["version"], row) for row in manifest["artifacts"])
    return rows


def test_compatibility_report_accounts_for_every_published_and_candidate_artifact() -> None:
    report = build_compatibility_report()
    published = _published_rows()
    candidate = build_release()["artifacts"]

    assert report["format_version"] == 1
    assert report["release_version"] == "2.0.0"
    assert report["summary"]["historical_artifact_observations"] == len(published) >= 17
    # Only supported (>= 1.5.0) releases are observed; deprecated history is never compared.
    assert "1.5.0" in {version for version, _row in published}
    assert all(tuple(map(int, version.split("."))) >= (1, 5, 0) for version, _row in published)
    assert report["summary"]["candidate_artifacts"] == len(candidate) == 23
    assert len(report["historical_comparisons"]) == len(published)
    assert len(report["candidate_artifacts"]) == len(candidate)
    assert {
        (row["historical_release"], row["artifact_id"]) for row in report["historical_comparisons"]
    } == {(version, row["id"]) for version, row in published}
    assert {row["artifact_id"] for row in report["candidate_artifacts"]} == {
        row["id"] for row in candidate
    }
    assert {row["classification"] for row in report["historical_comparisons"]} <= {
        "identical",
        "compatible-additive",
        "policy-divergence-retained",
        "legacy-only-retained",
    }
    assert {row["classification"] for row in report["candidate_artifacts"]} <= {
        "previously-published",
        "new-official-artifact",
    }


def test_report_classifies_each_nonidentical_comparison_with_policy_evidence() -> None:
    report = build_compatibility_report()
    comparisons = report["historical_comparisons"]

    for row in comparisons:
        assert row["historical_sha256"].startswith("sha256:")
        if row["classification"] == "identical":
            assert row["candidate_sha256"] == row["historical_sha256"]
            assert row["policy_errors"] == []
        elif row["classification"] == "compatible-additive":
            assert row["candidate_sha256"] != row["historical_sha256"]
            assert row["policy_errors"] == []
        elif row["classification"] == "policy-divergence-retained":
            assert row["candidate_sha256"]
            assert row["policy_errors"]
        else:
            assert row["candidate_sha256"] is None
            assert row["policy_errors"] == []

    # Pre-1.5.0 releases are deprecated and never compared (bh-bwnys.5). The supported
    # 1.x operation catalogs retain their explicit version-policy divergence from 2.x.
    catalog_divergences = [
        row
        for row in comparisons
        if row["artifact_id"] == "urn:beadhive:wire-catalog:operations:1"
        and row["classification"] == "policy-divergence-retained"
    ]
    assert {row["historical_release"] for row in catalog_divergences} == {"1.5.0", "1.6.0"}
    assert all(
        row["policy_errors"]
        == [
            "urn:beadhive:wire-catalog:operations:1.catalog_version: append-only catalog "
            "value changed or members were reordered"
        ]
        for row in catalog_divergences
    )


def test_checked_compatibility_report_is_canonical_and_current() -> None:
    report = build_compatibility_report()
    checked = COMPATIBILITY_REPORT_PATH.read_bytes()
    assert (
        checked
        == (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    )
    assert validate_compatibility_report() == ()


def test_report_generator_check_is_read_only_and_deterministic(tmp_path: Path) -> None:
    script = ROOT / "scripts" / "generate_contract_release_evidence.py"
    before = COMPATIBILITY_REPORT_PATH.read_bytes()
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    for target in (first, second):
        result = subprocess.run(
            [sys.executable, str(script), "--write", "--output", str(target)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr or result.stdout
    checked = subprocess.run(
        [sys.executable, str(script), "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert checked.returncode == 0, checked.stderr or checked.stdout
    assert first.read_bytes() == second.read_bytes() == before
    assert COMPATIBILITY_REPORT_PATH.read_bytes() == before


@pytest.mark.parametrize("escape", ["traversal", "absolute"])
def test_historical_manifest_path_cannot_escape_wire_root(tmp_path: Path, escape: str) -> None:
    repository = tmp_path / "repository"
    outside = repository / "outside" / "release.json"
    manifest_path = "../../../outside/release.json" if escape == "traversal" else str(outside)
    _minimal_wire_root(repository, manifest_path)
    _write_json(outside, _minimal_manifest("artifact.json"))
    _write_json(
        repository / "outside" / "artifact.json",
        {"$id": "urn:beadhive:wire-schema:historical-probe:1"},
    )

    with pytest.raises(ValueError, match="manifest.*traversal"):
        build_compatibility_report(repository)


def test_historical_manifest_path_cannot_follow_symlink(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    wire_root = _minimal_wire_root(repository, "v0.1.0/release.json")
    outside = repository / "outside" / "release.json"
    _write_json(outside, _minimal_manifest("artifact.json"))
    manifest = wire_root / "v0.1.0" / "release.json"
    manifest.parent.mkdir(parents=True)
    manifest.symlink_to(outside)

    with pytest.raises(ValueError, match="manifest.*symlink"):
        build_compatibility_report(repository)


@pytest.mark.parametrize("escape", ["traversal", "absolute"])
def test_historical_artifact_path_cannot_escape_manifest_directory(
    tmp_path: Path, escape: str
) -> None:
    repository = tmp_path / "repository"
    wire_root = _minimal_wire_root(repository, "v0.1.0/release.json")
    outside = repository / "outside" / "artifact.json"
    artifact_path = "../../../../outside/artifact.json" if escape == "traversal" else str(outside)
    _write_json(
        wire_root / "v0.1.0" / "release.json",
        _minimal_manifest(artifact_path),
    )
    _write_json(
        outside,
        {"$id": "urn:beadhive:wire-schema:historical-probe:1"},
    )

    with pytest.raises(ValueError, match="artifact.*traversal"):
        build_compatibility_report(repository)


def test_historical_artifact_path_cannot_follow_symlink(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    wire_root = _minimal_wire_root(repository, "v0.1.0/release.json")
    _write_json(wire_root / "v0.1.0" / "release.json", _minimal_manifest("artifact.json"))
    outside = repository / "outside" / "artifact.json"
    _write_json(outside, {"$id": "urn:beadhive:wire-schema:historical-probe:1"})
    (wire_root / "v0.1.0" / "artifact.json").symlink_to(outside)

    with pytest.raises(ValueError, match="artifact.*symlink"):
        build_compatibility_report(repository)


def test_wheel_contains_the_exact_checksum_verified_release(tmp_path: Path) -> None:
    out = tmp_path / "dist"
    result = subprocess.run(
        ["uv", "build", "--offline", "--wheel", "--out-dir", str(out)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    (wheel,) = out.glob("*.whl")
    inventory = json.loads((release_root() / "inventory.json").read_bytes())

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        prefix = f"beadhive/schemas/contracts/v{RELEASE_VERSION}/"
        assert prefix + "inventory.json" in names
        assert prefix + "conformance.json" in names
        for row in inventory["artifacts"]:
            payload = archive.read(prefix + row["path"])
            assert "sha256:" + hashlib.sha256(payload).hexdigest() == row["sha256"]


def test_installed_release_matches_cli_mcp_and_api_runtime_projections() -> None:
    candidate = build_release()
    installed = load_release(release_root())
    expected_owners = {
        "cli": "beadhive.transport_inventory.document",
        "mcp": "beadhive.transport_inventory.document",
        "openapi": "beadhive.daemon_openapi.generate_openapi_document",
    }

    for family, owner in expected_owners.items():
        candidate_row = next(row for row in candidate["artifacts"] if row["family"] == family)
        installed_row = next(row for row in installed["artifacts"] if row["family"] == family)
        assert candidate_row["source_owner"] == owner
        assert installed_row == candidate_row


def test_release_notes_cover_support_negotiation_safety_and_ownership() -> None:
    notes = RELEASE_NOTES_PATH.read_text()
    required_phrases = (
        "Supported artifacts",
        "Version negotiation",
        "Deprecation",
        "Redaction",
        "Consumer upgrades",
        "Maintainer procedure",
        "Plugin-author procedure",
        "telemetry-disabled",
        "dead-collector",
        "exact release commit",
    )
    assert all(phrase in notes for phrase in required_phrases)
