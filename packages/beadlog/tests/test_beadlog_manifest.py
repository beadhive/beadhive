"""Manifest parsing and capability refusal (format ADR sections 1, 7 and 11)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import beadlog

V01 = Path(__file__).resolve().parents[3] / "docs" / "schemas" / "beadlog" / "v0.1"


def _example_manifest() -> dict:
    return json.loads((V01 / "examples" / "archive" / "MANIFEST.json").read_text(encoding="utf-8"))


def test_example_manifest_round_trips() -> None:
    raw = _example_manifest()
    manifest = beadlog.Manifest.from_json(raw)
    assert manifest.to_json() == raw
    assert manifest.group("urn:uuid:5f0c9a52-8a43-4f0e-9a5e-1c7d2b3e4f60").dir == "bh"
    assert manifest.group("nope") is None
    manifest.capabilities.require()


def test_valid_example_manifests_parse() -> None:
    for name in ("manifest-minimal.json", "manifest-plan.json"):
        raw = json.loads((V01 / "examples" / "valid" / name).read_text(encoding="utf-8"))
        assert beadlog.Manifest.from_json(raw).to_json() == raw


def test_capability_vocabulary_matches_the_schema() -> None:
    schema = json.loads((V01 / "manifest.schema.json").read_text(encoding="utf-8"))
    assert set(schema["$defs"]["known_capability"]["enum"]) == beadlog.KNOWN_CAPABILITIES
    assert schema["properties"]["time_semantics"]["const"] == json.loads(
        json.dumps(dict(beadlog.TIME_SEMANTICS), default=list)
    )


def test_unknown_required_capability_is_refused() -> None:
    capabilities = beadlog.Capabilities(required=("tombstones", "kind:alias"), optional=("x-y",))
    assert capabilities.unsupported() == ("kind:alias",)
    with pytest.raises(beadlog.UnsupportedCapabilityError) as excinfo:
        capabilities.require()
    assert excinfo.value.missing == ("kind:alias",)
    # A reader implementing fewer capabilities refuses what it lacks; optional ones never refuse.
    with pytest.raises(beadlog.UnsupportedCapabilityError):
        beadlog.Capabilities(required=("tombstones",)).require(implemented=("checkpoints",))
    beadlog.Capabilities(optional=("anything-new",)).require(implemented=())


@pytest.mark.parametrize("version", ["0.2.0", "1.0.0", "0.1", "0.1.01", None])
def test_unknown_format_versions_are_refused(version: object) -> None:
    raw = dict(_example_manifest(), format_version=version)
    with pytest.raises(beadlog.ManifestError):
        beadlog.Manifest.from_json(raw)


def test_patch_versions_of_the_known_minor_are_accepted() -> None:
    assert beadlog.check_format_version("0.1.7") == "0.1.7"


def test_time_semantics_must_be_the_v01_constant() -> None:
    raw = _example_manifest()
    raw["time_semantics"] = dict(raw["time_semantics"], timezone="Europe/Paris")
    with pytest.raises(beadlog.ManifestError):
        beadlog.Manifest.from_json(raw)


def test_constants() -> None:
    assert beadlog.FORMAT_VERSION == "0.1.0"
    assert beadlog.MEDIA_TYPE == "application/vnd.beadhive.beadlog+zip"
    assert beadlog.FILE_EXTENSION == ".bdlg"
    assert {str(item) for item in beadlog.LoaderCapability} >= {"valid-time", "erase"}
