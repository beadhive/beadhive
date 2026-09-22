from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from beadhive import beads_schema

PIN_VERSION = "1.3.0"
PIN_COMMIT = "f45b249ce6b40ba62aecc03949e6371e8f7c79d8"


def _flake(*, version: str = PIN_VERSION, commit: str = PIN_COMMIT) -> str:
    return f'''{{
      outputs = {{ self }}: let
        beadsReleaseCommit = "{commit}";
        beadsRelease = pkgs:
          pkgs.stdenvNoCC.mkDerivation {{
            pname = "beads";
            version = "{version}";
          }};
      in {{ }};
    }}
    '''


def _schema(**updates) -> bytes:
    document = {
        "schema_version": 1,
        "types": {
            "dependency": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"issue_id": {"type": "string"}},
                "additionalProperties": False,
            },
            "issue": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    }
    document.update(updates)
    return (json.dumps(document, indent=3) + "\n").encode()


class FakeBd:
    def __init__(self, schema: bytes | None = None, *, version=PIN_VERSION, commit=PIN_COMMIT):
        self.schema = schema or _schema()
        self.version = version
        self.commit = commit
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((list(command), cwd))
        if list(command[1:]) == ["version", "--json"]:
            payload = json.dumps({"version": self.version, "commit": self.commit}).encode()
            return subprocess.CompletedProcess(command, 0, payload, b"")
        if list(command[1:]) == ["schema"]:
            return subprocess.CompletedProcess(command, 0, self.schema, b"")
        raise AssertionError(command)


@pytest.fixture
def bare_checkout(tmp_path: Path) -> Path:
    (tmp_path / "flake.nix").write_text(_flake())
    return tmp_path


def test_real_flake_pin_is_the_certified_release():
    pin = beads_schema.read_beads_pin(Path(__file__).parents[1] / "flake.nix")
    assert pin == beads_schema.BeadsPin(PIN_VERSION, PIN_COMMIT)


def test_capture_on_bare_checkout_preserves_raw_bytes_and_records_provenance(bare_checkout):
    raw = _schema()
    bd = FakeBd(raw)
    captured = beads_schema.capture_schema(
        bare_checkout,
        runner=bd,
        captured_at=datetime(2026, 9, 21, 6, 0, tzinfo=UTC),
    )

    assert not (bare_checkout / ".beads").exists()
    assert bd.calls == [
        (["bd", "version", "--json"], bare_checkout),
        (["bd", "schema"], bare_checkout),
    ]
    assert captured.artifact == (bare_checkout / "src/beadhive/schemas/beads/v1.3.0/schema.json")
    assert captured.artifact.read_bytes() == raw
    provenance = json.loads(captured.provenance.read_text())
    assert provenance == {
        "version": PIN_VERSION,
        "commit": PIN_COMMIT,
        "schema_version": 1,
        "captured_at": "2026-09-21T06:00:00Z",
        "document_sha256": hashlib.sha256(raw).hexdigest(),
    }


@pytest.mark.parametrize(
    ("version", "commit"),
    [("1.3.1", PIN_COMMIT), (PIN_VERSION, "0" * 40)],
)
def test_binary_pin_mismatch_aborts_before_schema_or_write(bare_checkout, version, commit):
    bd = FakeBd(version=version, commit=commit)
    with pytest.raises(beads_schema.CaptureError) as raised:
        beads_schema.capture_schema(bare_checkout, runner=bd)

    message = str(raised.value)
    assert f"expected version={PIN_VERSION} commit={PIN_COMMIT}" in message
    assert f"observed version={version} commit={commit}" in message
    assert len(bd.calls) == 1
    assert not (bare_checkout / "src").exists()


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda document: document.pop("schema_version"), "schema_version"),
        (
            lambda document: document["types"]["issue"].update({"type": "not-a-type"}),
            "types.issue failed Draft202012Validator.check_schema",
        ),
        (
            lambda document: document["types"]["issue"].update({"additionalProperties": True}),
            "types.issue additionalProperties must be false",
        ),
        (
            lambda document: document["types"]["dependency"].pop("additionalProperties"),
            "types.dependency additionalProperties must be false",
        ),
    ],
)
def test_invalid_schema_aborts_without_writing(bare_checkout, mutate, reason):
    document = json.loads(_schema())
    mutate(document)
    bd = FakeBd(json.dumps(document).encode())

    with pytest.raises(beads_schema.CaptureError, match=reason):
        beads_schema.capture_schema(bare_checkout, runner=bd)

    assert not (bare_checkout / "src").exists()


def _capture(bare_checkout: Path, schema: bytes | None = None) -> None:
    beads_schema.capture_schema(
        bare_checkout,
        runner=FakeBd(schema),
        captured_at=datetime(2026, 9, 21, 6, 0, tzinfo=UTC),
    )


def test_drift_check_is_a_clean_noop_for_the_current_pin_without_calling_bd(bare_checkout):
    _capture(bare_checkout)
    checked: list[Path] = []

    def never_run(*_args):
        raise AssertionError("unchanged pin must not invoke bd")

    def models(repo: Path, _captured: beads_schema.CapturedContract) -> None:
        checked.append(repo)

    beads_schema.check_schema_drift(bare_checkout, runner=never_run, model_checker=models)

    assert checked == [bare_checkout.resolve()]


def test_drift_check_rejects_a_hand_edited_captured_artifact_without_calling_bd(bare_checkout):
    _capture(bare_checkout)
    artifact = bare_checkout / "src/beadhive/schemas/beads/v1.3.0/schema.json"
    artifact.write_bytes(artifact.read_bytes() + b"\n")

    with pytest.raises(beads_schema.SchemaDriftError, match="schema bytes drift from provenance"):
        beads_schema.check_schema_drift(
            bare_checkout,
            runner=lambda *_args: (_ for _ in ()).throw(AssertionError("no bd expected")),
            model_checker=lambda _repo, _captured: None,
        )


def test_drift_check_names_pin_capture_command_and_exact_contract_delta(bare_checkout):
    previous = json.loads(_schema())
    previous["types"]["issue"]["properties"]["status"] = {"enum": ["open", "closed"]}
    _capture(bare_checkout, json.dumps(previous).encode())
    bare_checkout.joinpath("flake.nix").write_text(_flake(version="1.3.1"))
    current = json.loads(json.dumps(previous))
    current["types"]["issue"]["properties"].pop("id")
    current["types"]["issue"]["properties"]["title"] = {"type": "string"}
    current["types"]["issue"]["properties"]["status"] = {"enum": ["open", "review"]}

    with pytest.raises(beads_schema.SchemaDriftError) as raised:
        beads_schema.check_schema_drift(
            bare_checkout,
            runner=FakeBd(json.dumps(current).encode(), version="1.3.1"),
            model_checker=lambda _repo, _captured: None,
        )

    message = str(raised.value)
    assert "pinned version=1.3.1" in message
    assert f"captured version={PIN_VERSION}" in message
    assert "uv run bh beads schema capture" in message
    assert "types.issue.properties: added title" in message
    assert "types.issue.properties: removed id" in message
    assert "types.issue.properties: changed status" in message
    assert 'added enum members "review"' in message
    assert 'removed enum members "closed"' in message


def test_drift_check_rejects_hand_edited_models_without_hive_or_database(tmp_path: Path):
    root = Path(__file__).parents[1]
    schema_dir = tmp_path / "src/beadhive/schemas/beads/v1.3.0"
    schema_dir.mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    shutil.copy(root / "flake.nix", tmp_path / "flake.nix")
    shutil.copy(
        root / "scripts/generate_beads_models.py", tmp_path / "scripts/generate_beads_models.py"
    )
    shutil.copy(root / "src/beadhive/beads_models.py", tmp_path / "src/beadhive/beads_models.py")
    shutil.copy(root / "src/beadhive/schemas/beads/v1.3.0/schema.json", schema_dir / "schema.json")
    shutil.copy(
        root / "src/beadhive/schemas/beads/v1.3.0/provenance.json", schema_dir / "provenance.json"
    )
    models = tmp_path / "src/beadhive/beads_models.py"
    models.write_text(models.read_text() + "# hand edit\n")

    with pytest.raises(beads_schema.SchemaDriftError, match="generated Beads models drift"):
        beads_schema.check_schema_drift(
            tmp_path,
            runner=lambda *_args: (_ for _ in ()).throw(AssertionError("no bd expected")),
        )
