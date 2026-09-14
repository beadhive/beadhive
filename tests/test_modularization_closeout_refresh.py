from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts import refresh_modularization_closeout as refresher


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    first = root / "first.txt"
    second = root / "second.txt"
    first.write_text("first\n")
    second.write_text("second\n")
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
    return root, report


def test_check_names_every_stale_current_candidate_artifact(tmp_path: Path) -> None:
    root, report = _fixture(tmp_path)

    code, messages = refresher.refresh(root, report, write=False)

    assert code == 1
    assert messages == [
        "first.txt: stale artifact digest",
        "second.txt: stale artifact digest",
    ]


def test_write_is_idempotent_and_changes_only_generated_digest_fields(tmp_path: Path) -> None:
    root, report = _fixture(tmp_path)
    before = json.loads(report.read_text())

    assert refresher.refresh(root, report, write=True)[0] == 0
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
    assert refresher.refresh(root, report, write=True) == (0, [])
    assert report.read_bytes() == written


def test_missing_artifact_refuses_without_rewriting_report(tmp_path: Path) -> None:
    root, report = _fixture(tmp_path)
    (root / "second.txt").unlink()
    before = report.read_bytes()

    code, messages = refresher.refresh(root, report, write=True)

    assert code == 1
    assert messages == [
        "second.txt: artifact is missing",
        "first.txt: stale artifact digest",
    ]
    assert report.read_bytes() == before


def test_check_aggregates_missing_invalid_and_computable_stale_rows(tmp_path: Path) -> None:
    root, report = _fixture(tmp_path)
    third = root / "third.txt"
    third.write_text("third\n")
    payload = json.loads(report.read_text())
    payload["evidence_inventory"]["current_candidate"]["artifacts"].append(
        {"path": "third.txt", "sha256": "invalid"}
    )
    report.write_text(json.dumps(payload, indent=2) + "\n")
    (root / "second.txt").unlink()

    code, messages = refresher.refresh(root, report, write=False)

    assert code == 1
    assert messages == [
        "second.txt: artifact is missing",
        "third.txt: recorded sha256 is not a lowercase 64-character digest",
        "first.txt: stale artifact digest",
    ]
