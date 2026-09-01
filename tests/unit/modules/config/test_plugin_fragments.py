"""Contract tests for metadata-only plugin configuration composition."""

from __future__ import annotations

import importlib
import importlib.abc
import itertools

import pytest
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel

from beadhive.modules.config.application.plugin_fragments import (
    PluginConfigManifest,
    TrustedManifestProvenance,
    _fragment,
    builtin_plugin_fragments,
    compose_plugin_config,
)
from beadhive.modules.config.application.resolution import SourceLayer
from beadhive.modules.config.application.schema_artifacts import (
    generate_config_json_schema,
    plugin_fragment_artifacts,
)


def _manifest(
    plugin_id: str,
    *,
    artifact: str | None = None,
    namespace: str | None = None,
    version: str = "1.0.0",
    provenance: TrustedManifestProvenance | None = None,
) -> PluginConfigManifest:
    fragment = next(item for item in builtin_plugin_fragments() if item.plugin_id == plugin_id)
    return PluginConfigManifest(
        plugin_id,
        namespace or fragment.namespace,
        artifact or fragment.artifact_id,
        version,
        provenance or TrustedManifestProvenance("built-in", f"{plugin_id}.json"),
    )


def _compose(document, manifests, **kwargs):
    return compose_plugin_config(
        document,
        manifests,
        source_layer=kwargs.pop("source_layer", SourceLayer.HOST),
        **kwargs,
    )


def test_fragment_publication_and_composed_schema_are_deterministic():
    artifacts = plugin_fragment_artifacts()
    assert [fragment.plugin_id for fragment, _payload in artifacts] == [
        "herdr",
        "hitch",
        "observaloop",
        "orca",
        "repowise",
    ]
    assert all(
        payload == payload_again
        for (_fragment, payload), (_again, payload_again) in zip(
            artifacts, plugin_fragment_artifacts(), strict=True
        )
    )
    schema = generate_config_json_schema()
    assert set(schema["properties"]["plugins"]["properties"]) == {
        fragment.plugin_id for fragment, _payload in artifacts
    }


def test_compose_activates_only_available_fragments_and_retains_inactive_unknown_and_legacy():
    result = _compose(
        {
            "plugins": {"orca": {"enabled": True}, "future": {"canary": "secret"}},
            "hitch": {"enabled": True},
        },
        (_manifest("orca"), _manifest("hitch")),
        available_plugin_ids=("orca",),
        disabled_plugin_ids=("hitch",),
    )
    assert result.active["orca"]["enabled"] is True
    assert result.retained["hitch"] == {"enabled": True}
    assert result.retained["future"] == {"canary": "secret"}
    assert {(item.code, item.plugin_id) for item in result.diagnostics} == {
        ("disabled_plugin", "hitch"),
        ("unknown_plugin_config", "future"),
    }


def test_compose_refuses_conflicts_versions_and_invalid_values_without_losing_bytes():
    incompatible = _compose(
        {"plugins": {"orca": {"enabled": True}}},
        (_manifest("orca", artifact="urn:beadhive:wire-schema:plugin-config:orca:2"),),
        available_plugin_ids=("orca",),
    )
    invalid = _compose(
        {"plugins": {"orca": {"enabled": "not-a-bool"}}},
        (_manifest("orca"),),
        available_plugin_ids=("orca",),
    )
    assert incompatible.active == invalid.active == {}
    assert incompatible.retained["orca"] == {"enabled": True}
    assert invalid.retained["orca"] == {"enabled": "not-a-bool"}
    assert [item.code for item in incompatible.diagnostics] == ["incompatible_fragment_version"]
    assert [item.code for item in invalid.diagnostics] == ["invalid_config"]


def test_duplicate_namespace_is_fail_closed():
    result = _compose(
        {"plugins": {"orca": {"enabled": True}}},
        (_manifest("orca"), _manifest("hitch", namespace="plugins.orca")),
        available_plugin_ids=("orca",),
    )
    assert result.active == {}
    assert any(item.code == "duplicate_namespace" for item in result.diagnostics)
    assert any(
        item.code == "unavailable_plugin" and item.plugin_id == "orca"
        for item in result.diagnostics
    )


def test_duplicate_plugin_ids_are_fail_closed_independent_of_manifest_input_order():
    first = _manifest("orca")
    second = _manifest("orca", namespace="plugins.orca-shadow")
    results = [
        _compose(
            {"plugins": {"orca": {"enabled": True}}},
            manifests,
            available_plugin_ids=("orca",),
        )
        for manifests in itertools.permutations((first, second))
    ]
    assert all(result.active == {} for result in results)
    assert all(result.retained["orca"] == {"enabled": True} for result in results)
    assert [result.diagnostics for result in results] == [
        results[0].diagnostics,
        results[0].diagnostics,
    ]
    assert [item.code for item in results[0].diagnostics] == [
        "duplicate_plugin_id",
        "unavailable_plugin",
    ]


def test_disabled_config_is_validated_but_remains_inactive():
    valid = _compose(
        {"plugins": {"orca": {"enabled": True}}},
        (_manifest("orca"),),
        available_plugin_ids=(),
        disabled_plugin_ids=("orca",),
    )
    invalid = _compose(
        {"plugins": {"orca": {"enabled": "not-a-bool"}}},
        (_manifest("orca"),),
        available_plugin_ids=(),
        disabled_plugin_ids=("orca",),
    )
    assert valid.active == invalid.active == {}
    assert valid.retained["orca"] == {"enabled": True}
    assert invalid.retained["orca"] == {"enabled": "not-a-bool"}
    assert [item.code for item in valid.diagnostics] == ["disabled_plugin"]
    assert [item.code for item in invalid.diagnostics] == ["invalid_config"]


def test_equivalent_legacy_and_canonical_config_records_value_free_per_field_provenance():
    canary = "do-not-render-this-secret"
    result = _compose(
        {
            "plugins": {"orca": {"enabled": True, "data_path": canary}},
            "orca": {"enabled": True, "data_path": canary},
        },
        (
            _manifest(
                "orca",
                version="9.8.7",
                provenance=TrustedManifestProvenance("built-in", "orca.json", "beadhive-core"),
            ),
        ),
        available_plugin_ids=("orca",),
        source_layer=SourceLayer.HIVE,
    )
    enabled = result.provenance["plugins.orca.enabled"]
    data_path = result.provenance["plugins.orca.data_path"]
    assert enabled.source_paths == ("orca.enabled", "plugins.orca.enabled")
    assert data_path.source_paths == ("orca.data_path", "plugins.orca.data_path")
    assert enabled.plugin_id == "orca"
    assert enabled.plugin_version == "9.8.7"
    assert enabled.manifest_provenance == TrustedManifestProvenance(
        "built-in", "orca.json", "beadhive-core"
    )
    assert enabled.schema_artifact == "urn:beadhive:wire-schema:plugin-config:orca:1"
    assert enabled.fragment_major == 1 and enabled.schema_digest
    assert enabled.source_layer is SourceLayer.HIVE
    assert canary not in repr(result.provenance)


@pytest.mark.parametrize(
    ("document", "enabled_path"),
    [
        ({"plugins": {"orca": {"enabled": True}}}, "plugins.orca.enabled"),
        ({"orca": {"enabled": True}}, "orca.enabled"),
    ],
)
def test_provenance_marks_schema_defaults_without_claiming_a_persisted_source(
    document, enabled_path
):
    result = _compose(
        document,
        (_manifest("orca"),),
        available_plugin_ids=("orca",),
        source_layer=SourceLayer.HOST,
    )
    enabled = result.provenance["plugins.orca.enabled"]
    data_path = result.provenance["plugins.orca.data_path"]
    worktrees = result.provenance["plugins.orca.worktrees"]
    assert enabled.source_layer is SourceLayer.HOST
    assert enabled.source_paths == (enabled_path,)
    assert data_path.source_layer is worktrees.source_layer is SourceLayer.DEFAULT
    assert data_path.source_paths == worktrees.source_paths == ()


def test_composed_snapshot_is_recursively_immutable():
    result = _compose(
        {
            "plugins": {
                "orca": {"worktrees": {"enabled": True, "fallback": False}},
                "future": {"nested": [{"value": "keep"}], "tags": {"stable"}},
            },
        },
        (_manifest("orca"),),
        available_plugin_ids=("orca",),
    )
    with pytest.raises(TypeError):
        result.active["orca"]["worktrees"]["enabled"] = False
    with pytest.raises(TypeError):
        result.retained["future"]["nested"][0]["value"] = "mutated"
    with pytest.raises(AttributeError):
        result.retained["future"]["nested"].append("mutated")
    with pytest.raises(AttributeError):
        result.retained["future"]["tags"].add("mutated")
    with pytest.raises(TypeError):
        result.provenance["plugins.orca.worktrees.enabled"].source_paths[0] = "mutated"


def test_fragment_defaults_are_checked_before_publication():
    class BrokenDefaults(BaseModel):
        enabled: bool = "wrong"  # type: ignore[assignment]

    with pytest.raises(JsonSchemaValidationError):
        _fragment("broken", BrokenDefaults)


def test_fragment_publication_never_imports_plugin_runtime(monkeypatch):
    forbidden = (
        "beadhive.orca",
        "beadhive.hitch_plugin",
        "beadhive.herdr_plugin",
        "beadhive.repowise_plugin",
    )

    class RefuseRuntime(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(forbidden):
                raise AssertionError(f"fragment publisher imported runtime: {fullname}")
            return None

    finder = RefuseRuntime()
    import sys

    sys.meta_path.insert(0, finder)
    try:
        assert importlib.reload(
            importlib.import_module("beadhive.modules.config.application.plugin_fragments")
        ).builtin_plugin_fragments()
    finally:
        sys.meta_path.remove(finder)
