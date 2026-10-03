"""Pinned guard evaluator preserves the complete existing HostManifest schema."""

from __future__ import annotations

import copy

import jsonschema
import pytest
from ruamel.yaml.error import YAMLError

from beadhive.hosts import HostManifest
from beadhive.hq_manifest_guard import parse_manifest, validate


def test_pinned_evaluator_matches_existing_schema_for_every_field():
    schema = HostManifest.model_json_schema()
    valid = HostManifest(
        host_id="host-one",
        label="one",
        os="linux",
        arch="x86_64",
        role="executor",
        identity={"kind": "none"},
        frame_id="frame-one",
        instance_ref="vm-one",
        release={"id": "release", "digest": "sha256:" + "1" * 64},
        capabilities={
            "isolation": "kvm",
            "trust_zone": "self-hosted",
            "arch": "x86_64",
            "harnesses": ["codex"],
            "max_sessions": 2,
        },
    ).model_dump(mode="json")
    cases = [valid, {**valid, "unknown": True}]
    for key in valid:
        without = copy.deepcopy(valid)
        without.pop(key)
        cases.append(without)
        for value in (None, True, 1, -1, "illegal", [], {}, {"unknown": True}):
            cases.append({**valid, key: value})
    for field in ("identity", "release", "capabilities"):
        for key in valid[field]:
            for value in (None, True, -1, "illegal", [], {}, {"unknown": True}):
                case = copy.deepcopy(valid)
                case[field][key] = value
                cases.append(case)
    for case in cases:
        accepted = jsonschema.Draft202012Validator(schema).is_valid(case)
        try:
            validate(case, schema)
        except ValueError:
            assert not accepted, case
        else:
            assert accepted, case


@pytest.mark.parametrize(
    "text", ["host_id: one\nhost_id: two\n", "!!python/object/apply:os.system ['false']"]
)
def test_safe_yaml_rejects_duplicates_and_custom_tags(text):
    with pytest.raises((ValueError, YAMLError)):
        parse_manifest(text, HostManifest.model_json_schema())


def test_unknown_schema_assertion_fails_closed():
    with pytest.raises(ValueError, match="unsupported"):
        validate("one", {"type": "string", "inventedAssertion": True})
