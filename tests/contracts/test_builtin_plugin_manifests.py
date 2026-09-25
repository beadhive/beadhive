"""Isolated conformance and host-catalog drift for optional built-in manifests."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from beadhive import deps
from beadhive.bootstrap.impact import BUILTIN_IMPACT_PROVIDERS
from beadhive.kernel.plugins import (
    BUILD_IMPACT,
    BUILTIN_PLUGIN_IDS,
    BuiltInManifestSource,
    ManifestDocument,
    builtin_manifest_documents,
    discover_plugins,
)
from beadhive.plugin_runtime_catalog import PLUGIN_RUNTIME_CATALOG
from beadhive.testing import PluginSubject, assert_plugin_conforms


@dataclass(frozen=True)
class _DeclaredProvider:
    plugin_id: str
    capability_id: str


def _raw_document(plugin_id: str) -> ManifestDocument:
    return next(
        document
        for document in builtin_manifest_documents()
        if document.provenance.source_name == f"{plugin_id}.json"
    )


def _validate_manifest(manifest: dict[str, object]) -> None:
    raw = dict(manifest)
    raw.pop("provided_capabilities")
    plugin_id = str(raw["plugin_id"])
    result = discover_plugins(
        [BuiltInManifestSource((_raw_document(plugin_id),))],
        config=None,
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
        host_executables={plugin_id: "1.0.0"},
    )
    assert result.errors == ()
    assert result.plugins[0].manifest.plugin_id == plugin_id


def _subject(plugin_id: str) -> PluginSubject:
    raw = json.loads(_raw_document(plugin_id).payload)
    capabilities = tuple(declaration["id"] for declaration in raw["capabilities"]["provides"])
    raw["provided_capabilities"] = list(capabilities)
    return PluginSubject(
        manifest=dict(raw),
        capabilities={
            capability: _DeclaredProvider(plugin_id, capability) for capability in capabilities
        },
    )


@pytest.mark.parametrize("plugin_id", BUILTIN_PLUGIN_IDS)
def test_builtin_plugin_passes_common_conformance_independently(plugin_id):
    assert_plugin_conforms(
        lambda: _subject(plugin_id),
        manifest_validator=_validate_manifest,
        capability_field="provided_capabilities",
        subject=plugin_id,
    )


def test_manifest_inventory_matches_runtime_catalog_without_delivery_coupling():
    documents = builtin_manifest_documents()
    catalog = {entry.plugin_id: entry for entry in PLUGIN_RUNTIME_CATALOG}
    manifests = {
        value["plugin_id"]: value
        for value in (json.loads(document.payload) for document in documents)
    }
    impact = {provider.plugin_id: provider for provider in BUILTIN_IMPACT_PROVIDERS}
    assert tuple(sorted(manifests)) == BUILTIN_PLUGIN_IDS
    assert set(catalog).isdisjoint(impact)
    assert set(manifests) == set(catalog) | set(impact)
    assert tuple(entry.plugin_id for entry in PLUGIN_RUNTIME_CATALOG) == (
        "orca",
        "observaloop",
        "hitch",
        "herdr",
        "repowise",
    )
    for plugin_id, manifest in manifests.items():
        if plugin_id in impact:
            provides = [item["id"] for item in manifest["capabilities"]["provides"]]
            assert BUILD_IMPACT.capability_id in provides
            executable = impact[plugin_id].external_executable
        else:
            entry = catalog[plugin_id]
            assert entry.delivery == "runtime-core"
            assert entry.module.startswith("beadhive.")
            executable = entry.external_executable
        assert [item["name"] for item in manifest["security"]["executables"]] == [executable]
        assert manifest["configuration"]["namespace"] == f"plugins.{plugin_id}"
        artifact = manifest["configuration"]["schema_artifact"]
        # A build-system plugin with no configuration yet declares no schema fragment.
        assert (artifact is None and plugin_id in impact) or artifact.startswith(
            f"urn:beadhive:wire-schema:plugin-config:{plugin_id}:"
        )


def test_git_workspace_remains_the_required_non_plugin_exception():
    assert "git-workspace" not in BUILTIN_PLUGIN_IDS
    assert "git-workspace" not in {entry.plugin_id for entry in PLUGIN_RUNTIME_CATALOG}
    assert deps.by_name("git-workspace").required == deps.ALWAYS
