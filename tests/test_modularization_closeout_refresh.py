from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from scripts import refresh_modularization_closeout as refresher


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    first = root / "first.txt"
    second = root / "second.txt"
    first.write_text("first\n")
    second.write_text("second\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Closeout Test")
    _git(root, "config", "user.email", "closeout@example.com")
    _git(root, "add", "first.txt", "second.txt")
    _git(root, "commit", "-qm", "test: freeze candidate artifacts")
    frozen_revision = _git(root, "rev-parse", "HEAD")
    report = root / "closeout.json"
    report.write_text(
        json.dumps(
            {
                "evidence_inventory": {
                    "current_candidate": {
                        "artifacts": [
                            {"path": "first.txt", "sha256": "0" * 64, "kind": "proof"},
                            {"path": "second.txt", "sha256": "1" * 64},
                        ]
                    },
                    "immutable_historical": [{"path": "first.txt", "sha256": "2" * 64}],
                },
                "preserved": {"value": 7},
            },
            indent=2,
        )
        + "\n"
    )
    return root, report, frozen_revision


def test_check_names_every_stale_current_candidate_artifact(tmp_path: Path) -> None:
    root, report, frozen_revision = _fixture(tmp_path)

    code, messages = refresher.refresh(root, report, write=False, frozen_revision=frozen_revision)

    assert code == 1
    assert messages == [
        "first.txt: stale artifact digest",
        "second.txt: stale artifact digest",
    ]


def test_write_is_idempotent_and_changes_only_generated_digest_fields(tmp_path: Path) -> None:
    root, report, frozen_revision = _fixture(tmp_path)
    before = json.loads(report.read_text())

    assert refresher.refresh(root, report, write=True, frozen_revision=frozen_revision)[0] == 0
    written = report.read_bytes()
    after = json.loads(written)

    assert after["preserved"] == before["preserved"]
    assert (
        after["evidence_inventory"]["immutable_historical"]
        == before["evidence_inventory"]["immutable_historical"]
    )
    rows = after["evidence_inventory"]["current_candidate"]["artifacts"]
    assert [row["sha256"] for row in rows] == [
        _digest(root / "first.txt"),
        _digest(root / "second.txt"),
    ]
    assert refresher.refresh(root, report, write=True, frozen_revision=frozen_revision) == (0, [])
    assert report.read_bytes() == written


def test_missing_artifact_refuses_without_rewriting_report(tmp_path: Path) -> None:
    root, report, frozen_revision = _fixture(tmp_path)
    payload = json.loads(report.read_text())
    payload["evidence_inventory"]["current_candidate"]["artifacts"][1]["path"] = "missing.txt"
    report.write_text(json.dumps(payload, indent=2) + "\n")
    before = report.read_bytes()

    code, messages = refresher.refresh(root, report, write=True, frozen_revision=frozen_revision)

    assert code == 1
    assert messages == [
        "missing.txt: artifact is missing at frozen closeout revision",
        "first.txt: stale artifact digest",
    ]
    assert report.read_bytes() == before


def test_check_aggregates_missing_invalid_and_computable_stale_rows(tmp_path: Path) -> None:
    root, report, frozen_revision = _fixture(tmp_path)
    third = root / "third.txt"
    third.write_text("third\n")
    _git(root, "add", "third.txt")
    _git(root, "commit", "-qm", "test: add invalid-digest artifact")
    frozen_revision = _git(root, "rev-parse", "HEAD")
    payload = json.loads(report.read_text())
    payload["evidence_inventory"]["current_candidate"]["artifacts"].append(
        {"path": "third.txt", "sha256": "invalid"}
    )
    report.write_text(json.dumps(payload, indent=2) + "\n")
    payload["evidence_inventory"]["current_candidate"]["artifacts"][1]["path"] = "missing.txt"
    report.write_text(json.dumps(payload, indent=2) + "\n")

    code, messages = refresher.refresh(root, report, write=False, frozen_revision=frozen_revision)

    assert code == 1
    assert messages == [
        "missing.txt: artifact is missing at frozen closeout revision",
        "third.txt: recorded sha256 is not a lowercase 64-character digest",
        "first.txt: stale artifact digest",
    ]


def test_live_artifact_can_evolve_without_rewriting_historical_proof(tmp_path: Path) -> None:
    root, report, frozen_revision = _fixture(tmp_path)
    assert refresher.refresh(root, report, write=True, frozen_revision=frozen_revision)[0] == 0
    frozen_report = report.read_bytes()

    (root / "first.txt").write_text("live successor state\n")

    assert refresher.refresh(root, report, write=False, frozen_revision=frozen_revision) == (0, [])
    assert report.read_bytes() == frozen_report
    recorded = json.loads(report.read_text())["evidence_inventory"]["current_candidate"][
        "artifacts"
    ][0]["sha256"]
    assert recorded != _digest(root / "first.txt")
