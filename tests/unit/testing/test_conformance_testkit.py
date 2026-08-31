"""Pure tests for the supported conformance-test surface."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import pytest

from beadhive.testing import (
    ConformanceFailure,
    LifecycleCase,
    PluginCase,
    PluginSubject,
    PortCase,
    SchemaArtifact,
    assert_lifecycle_conforms,
    assert_plugin_conforms,
    assert_port_conforms,
    assert_schema_artifact_conforms,
    canonical_json_bytes,
)


@runtime_checkable
class EventSink(Protocol):
    def publish(self, event: str) -> None: ...


@dataclass
class RecordingSink:
    """In-memory outbound-port implementation used by the public kit example."""

    name: str = "recording-sink"
    events: list[str] = field(default_factory=list)

    def publish(self, event: str) -> None:
        self.events.append(event)


def _publishes_in_order(sink: RecordingSink) -> None:
    sink.publish("prepared")
    sink.publish("committed")
    assert sink.events == ["prepared", "committed"], "events were not retained in call order"


def test_in_memory_outbound_port_example_uses_a_fresh_subject_per_semantic_case():
    created: list[RecordingSink] = []

    def factory() -> RecordingSink:
        sink = RecordingSink()
        created.append(sink)
        return sink

    assert_port_conforms(
        factory,
        [PortCase("event-sink.publishes-in-order", _publishes_in_order)],
        port=EventSink,
    )

    assert len(created) == 2  # structural membership plus one fresh semantic-case subject
    assert created[0].events == []
    assert created[1].events == ["prepared", "committed"]


def test_port_failure_names_the_violated_semantic_case():
    def rejects_event(_sink: RecordingSink) -> None:
        raise AssertionError("rejected event was accepted")

    with pytest.raises(ConformanceFailure) as caught:
        assert_port_conforms(
            RecordingSink,
            [PortCase("event-sink.rejects-secret-payload", rejects_event)],
        )

    assert caught.value.case_id == "event-sink.rejects-secret-payload"
    assert "semantic-case=event-sink.rejects-secret-payload" in str(caught.value)
    assert "rejected event was accepted" in str(caught.value)


def test_outbound_port_suite_rejects_an_empty_semantic_case_list_before_construction():
    constructed = False

    def factory():
        nonlocal constructed
        constructed = True
        return RecordingSink()

    with pytest.raises(ValueError, match="requires at least one semantic case"):
        assert_port_conforms(factory, [])

    assert constructed is False


@dataclass
class Participant:
    calls: list[str] = field(default_factory=list)

    def prepare(self) -> None:
        self.calls.append("prepare")

    def abort(self) -> None:
        self.calls.append("abort")


def _abort_follows_prepare(participant: Participant) -> None:
    participant.prepare()
    participant.abort()
    assert participant.calls == ["prepare", "abort"]


def test_lifecycle_participant_cases_exercise_phase_semantics():
    assert_lifecycle_conforms(
        Participant,
        [LifecycleCase("launch.abort-follows-prepare", _abort_follows_prepare)],
    )


def test_lifecycle_suite_rejects_an_empty_semantic_case_list_before_construction():
    constructed = False

    def factory():
        nonlocal constructed
        constructed = True
        return Participant()

    with pytest.raises(ValueError, match="requires at least one semantic case"):
        assert_lifecycle_conforms(factory, [])

    assert constructed is False


def _validate_manifest(manifest) -> None:
    assert manifest.get("schemaVersion") == 1, "unsupported manifest schema"
    assert manifest.get("id"), "plugin id is required"


@dataclass
class LaunchCapability:
    calls: list[str] = field(default_factory=list)

    def __call__(self) -> None:
        self.calls.append("launch")


def _capability_is_callable(plugin: PluginSubject) -> None:
    assert callable(plugin.capabilities["launch"]), "launch capability must be callable"


def test_plugin_manifest_capabilities_and_custom_semantics_conform():
    assert_plugin_conforms(
        lambda: PluginSubject(
            manifest={"schemaVersion": 1, "id": "memory", "capabilities": ["launch"]},
            capabilities={"launch": LaunchCapability()},
        ),
        manifest_validator=_validate_manifest,
        cases=[PluginCase("plugin.launch-capability-callable", _capability_is_callable)],
        subject="memory",
    )


def test_plugin_capability_drift_names_the_common_semantic_case():
    with pytest.raises(
        ConformanceFailure, match="semantic-case=plugin.capabilities-match-manifest"
    ):
        assert_plugin_conforms(
            lambda: PluginSubject(
                manifest={
                    "schemaVersion": 1,
                    "id": "memory",
                    "capabilities": ["launch", "observe"],
                },
                capabilities={"launch": LaunchCapability()},
            ),
            manifest_validator=_validate_manifest,
            subject="memory",
        )


def test_plugin_cases_receive_fresh_subjects_and_provider_objects():
    providers: list[LaunchCapability] = []

    def factory() -> PluginSubject:
        provider = LaunchCapability()
        providers.append(provider)
        return PluginSubject(
            manifest={"schemaVersion": 1, "id": "memory", "capabilities": ["launch"]},
            capabilities={"launch": provider},
        )

    def mutate(subject: PluginSubject) -> None:
        subject.capabilities["launch"].calls.append("mutated")

    def remains_pristine(subject: PluginSubject) -> None:
        assert subject.capabilities["launch"].calls == []

    assert_plugin_conforms(
        factory,
        manifest_validator=_validate_manifest,
        cases=[
            PluginCase("plugin.provider-mutation", mutate),
            PluginCase("plugin.next-provider-pristine", remains_pristine),
        ],
        subject="memory",
    )

    assert len(providers) >= 2
    assert len({id(provider) for provider in providers}) == len(providers)


def test_plugin_suite_rejects_a_factory_that_reuses_a_provider_object():
    shared = LaunchCapability()

    with pytest.raises(ConformanceFailure, match="semantic-case=plugin.subject-freshness"):
        assert_plugin_conforms(
            lambda: PluginSubject(
                manifest={"schemaVersion": 1, "id": "memory", "capabilities": ["launch"]},
                capabilities={"launch": shared},
            ),
            manifest_validator=_validate_manifest,
            subject="memory",
        )


def test_plugin_missing_provider_retains_its_semantic_diagnostic():
    with pytest.raises(ConformanceFailure, match="semantic-case=plugin.capabilities-provided"):
        assert_plugin_conforms(
            lambda: PluginSubject(
                manifest={"schemaVersion": 1, "id": "memory", "capabilities": ["launch"]},
                capabilities={"launch": None},
            ),
            manifest_validator=_validate_manifest,
            subject="memory",
        )


def test_schema_artifact_checks_determinism_identity_version_and_checked_in_bytes(tmp_path):
    document = {
        "$id": "urn:beadhive:test:message",
        "properties": {"message": {"type": "string"}},
        "type": "object",
        "version": 1,
    }
    released = tmp_path / "message-v1.schema.json"
    released.write_bytes(canonical_json_bytes(document))

    assert_schema_artifact_conforms(
        SchemaArtifact(
            artifact_id="urn:beadhive:test:message",
            version=1,
            generate=lambda: document,
            checked_in=released,
        )
    )


def test_schema_drift_names_the_semantic_case():
    generated = {"$id": "urn:beadhive:test:message", "type": "object", "version": 1}

    with pytest.raises(ConformanceFailure, match="semantic-case=schema.checked-in-drift"):
        assert_schema_artifact_conforms(
            SchemaArtifact(
                artifact_id="urn:beadhive:test:message",
                version=1,
                generate=lambda: generated,
                checked_in=b"{}\n",
            )
        )


def test_schema_generator_failure_names_the_semantic_case():
    def fail_generation():
        raise RuntimeError("generator read ambient runtime state")

    with pytest.raises(ConformanceFailure, match="semantic-case=schema.generation"):
        assert_schema_artifact_conforms(
            SchemaArtifact(
                artifact_id="urn:beadhive:test:message",
                version=1,
                generate=fail_generation,
                checked_in=b"{}\n",
            )
        )


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_canonical_json_rejects_nonstandard_nonfinite_numbers(value):
    with pytest.raises(ValueError, match="JSON compliant"):
        canonical_json_bytes({"value": value})


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_schema_artifact_rejects_nonstandard_json_constants(constant):
    generated = f'{{"$id":"urn:beadhive:test:message","value":{constant},"version":1}}\n'.encode()

    with pytest.raises(ConformanceFailure, match="semantic-case=schema.valid-json"):
        assert_schema_artifact_conforms(
            SchemaArtifact(
                artifact_id="urn:beadhive:test:message",
                version=1,
                generate=lambda: generated,
                checked_in=generated,
            )
        )


@pytest.mark.parametrize(
    ("artifact_id", "version"),
    [
        (7, 1),
        ("urn:beadhive:test:message", True),
    ],
)
def test_schema_artifact_requires_strict_identity_and_version_types(artifact_id, version):
    with pytest.raises(TypeError):
        SchemaArtifact(
            artifact_id=artifact_id,
            version=version,
            generate=lambda: {},
            checked_in=b"{}\n",
        )


def test_schema_document_version_does_not_treat_bool_as_integer():
    generated = {
        "$id": "urn:beadhive:test:message",
        "type": "object",
        "version": True,
    }

    with pytest.raises(ConformanceFailure, match="semantic-case=schema.explicit-version"):
        assert_schema_artifact_conforms(
            SchemaArtifact(
                artifact_id="urn:beadhive:test:message",
                version=1,
                generate=lambda: generated,
                checked_in=canonical_json_bytes(generated),
            )
        )


def test_case_ids_are_stable_diagnostic_identifiers():
    with pytest.raises(ValueError, match="stable lowercase semantic identifier"):
        PortCase("Not a semantic ID", lambda _subject: None)
