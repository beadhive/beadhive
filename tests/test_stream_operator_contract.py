"""Durable checks for the stream operator-entity v1 wire contract."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA_PATH = _ROOT / "docs" / "schemas" / "beadhive-stream-operator-entities-v1.schema.json"
_CONTRACT_PATH = _ROOT / "docs" / "design" / "beadhive-stream-operator-entities-v1-contract.md"


def _schema() -> dict[str, object]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(_schema())


def _entity_id(prefix: str, key: list[str]) -> str:
    encoded = json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"{prefix}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def _snapshot() -> dict[str, object]:
    return {
        "schema_version": 1,
        "frame": "snapshot",
        "scope": "hive",
        "revision": "opaque-revision-1",
        "as_of": "2026-08-24T12:00:00Z",
        "partial": False,
        "partial_reason": None,
        "reason": "initial",
        "issues": [],
        "work_dependencies": [
            {
                "id": _entity_id(
                    "work-dependency", ["beadhive", "bh-child", "bh-parent", "blocks"]
                ),
                "hive": "beadhive",
                "issue_id": "bh-child",
                "depends_on_id": "bh-parent",
                "type": "blocks",
                "created_at": None,
                "created_by": None,
            }
        ],
        "gate_requests": [
            {
                "id": _entity_id("gate-request", ["beadhive", "bh-gate"]),
                "hive": "beadhive",
                "gate_id": "bh-gate",
                "blocks": ["bh-child"],
                "gate_type": "human",
                "gate_kind": "review",
                "status": "open",
                "reason": "bh:review abc1234",
                "opened_at": "2026-08-24T11:00:00Z",
                "resolved_at": None,
            }
        ],
        "epic_schedules": [
            {
                "id": _entity_id("epic-schedule", ["beadhive", "bh-epic"]),
                "hive": "beadhive",
                "epic_id": "bh-epic",
                "groups": [
                    {
                        "kind": "chain",
                        "batch": None,
                        "issue_ids": ["bh-parent", "bh-child"],
                    }
                ],
                "singletons": ["bh-docs"],
                "coordinators": ["bh-child-epic"],
            }
        ],
        "assignments": [
            {
                "id": _entity_id("assignment", ["beadhive", "bh-child"]),
                "hive": "beadhive",
                "issue_id": "bh-child",
                "seat": "dev/operator-entity-contract",
            }
        ],
    }


def _delta() -> dict[str, object]:
    snapshot = _snapshot()
    return {
        "schema_version": 1,
        "frame": "delta",
        "scope": "hive",
        "revision": "opaque-revision-2",
        "as_of": "2026-08-24T12:01:00Z",
        "partial": False,
        "partial_reason": None,
        "since_revision": "opaque-revision-1",
        "changed": [],
        "removed": [],
        "work_dependencies_changed": snapshot["work_dependencies"],
        "work_dependencies_removed": [],
        "gate_requests_changed": snapshot["gate_requests"],
        "gate_requests_removed": [],
        "epic_schedules_changed": snapshot["epic_schedules"],
        "epic_schedules_removed": [],
        "assignments_changed": snapshot["assignments"],
        "assignments_removed": [],
    }


def test_operator_frame_schema_is_valid_and_accepts_all_frame_kinds() -> None:
    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)

    validator.validate(_snapshot())
    validator.validate(_delta())
    validator.validate(
        {
            "schema_version": 1,
            "frame": "resync",
            "scope": "hive",
            "as_of": "2026-08-24T12:02:00Z",
            "partial": False,
            "partial_reason": None,
            "reason": "adapter_error",
        }
    )


@pytest.mark.parametrize(
    "slot",
    ["work_dependencies", "gate_requests", "epic_schedules", "assignments"],
)
def test_snapshot_requires_every_operator_collection(slot: str) -> None:
    frame = _snapshot()
    del frame[slot]
    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(frame)


@pytest.mark.parametrize(
    "slot",
    [
        "work_dependencies_changed",
        "work_dependencies_removed",
        "gate_requests_changed",
        "gate_requests_removed",
        "epic_schedules_changed",
        "epic_schedules_removed",
        "assignments_changed",
        "assignments_removed",
    ],
)
def test_delta_requires_every_changed_and_removed_collection(slot: str) -> None:
    frame = _delta()
    del frame[slot]
    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(frame)


def test_records_have_exact_fields_and_no_private_revision_clock() -> None:
    frame = _snapshot()
    dependency = frame["work_dependencies"][0]  # type: ignore[index]
    dependency["revision"] = "wrong-private-clock"

    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(frame)


def test_entity_hive_is_registry_repo_slug_not_owner_repo_path() -> None:
    frame = _snapshot()
    frame["assignments"][0]["hive"] = "github/beadhive/beadhive"  # type: ignore[index]

    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(frame)


def test_aggregate_orphan_gate_omission_is_explicit_partial_state() -> None:
    frame = _snapshot()
    frame["scope"] = "factory"
    frame["gate_requests"] = []
    frame["partial"] = True
    frame["partial_reason"] = "gate_hive_identity_unavailable"

    _validator().validate(frame)


@pytest.mark.parametrize("kind", ["fanout", "collapsed"])
def test_schedule_rejects_runtime_only_group_vocabulary(kind: str) -> None:
    frame = copy.deepcopy(_snapshot())
    frame["epic_schedules"][0]["groups"][0]["kind"] = kind  # type: ignore[index]

    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(frame)


def test_stable_id_vectors_pin_prefixes_and_canonical_json() -> None:
    assert _entity_id("work-dependency", ["beadhive", "bh-child", "bh-parent", "blocks"]) == (
        "work-dependency:sha256:d62ed91f1152068ea9f2c76d9b2a48c3c15c9c50b1121dca57ca2c2280601f1c"
    )
    assert _entity_id("assignment", ["beadhive", "bh-child"]) == (
        "assignment:sha256:a482290006d50a982ea5f98ef14ef65cb6ca97ab1695ea8379b33f027251e55b"
    )
    assert _entity_id("gate-request", ["beadhive", "bh-gate"]) == (
        "gate-request:sha256:a7a8b3022a982fb0a6822246c0d48282bd4d1166e5dd94019d7c968697572f43"
    )
    assert _entity_id("epic-schedule", ["beadhive", "bh-epic"]) == (
        "epic-schedule:sha256:4910375c4452ee0a9e0f99c5dbf407238eebb29d8aa84e6206705f6d6de9bb3c"
    )


def test_non_schema_semantics_are_normative_and_auditable() -> None:
    text = " ".join(_CONTRACT_PATH.read_text(encoding="utf-8").split())

    assert "`as_of - 24 hours <= resolved_at <= as_of`" in text
    assert "`release-hold` maps explicitly to `other`" in text
    assert "current marker is exactly `bh:review <sha>`" in text
    assert "including a closed epic" in text
    assert "status is not `closed`" in text
    assert "There is no `fanout` group kind" in text
    assert "There is no `collapsed` group kind" in text
    assert "one enclosing frame `revision` and `as_of`" in text
    assert "including `--since` sessions" in text
    assert "one coalesced scope refresh" in text
    assert "produce explicit partial state" in text
    assert "registry-owned repository slug" in text
    assert '`partial_reason: "gate_hive_identity_unavailable"`' in text
    assert "never infers a hive from a gate ID or description" in text
    assert "`blocks: []` is valid only when hive identity is independently known" in text
    assert "one scope-level gate listing" in text
    assert "shared `work_logic._gate_kind` classifier" in text
    assert "`max_size=max(1, len(non_closed_direct_leaves))`" in text
    assert "does not call `work.schedule_payload`" in text
    assert "inspect git merged-group state" in text
    assert "real `bh stream --format ndjson` command" in text
    assert "no descendant backend process survives" in text
