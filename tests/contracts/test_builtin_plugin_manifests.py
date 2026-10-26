"""Isolated conformance and host-catalog drift for optional built-in manifests."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass

import pytest

from beadhive import deps
from beadhive.bootstrap.impact import BUILTIN_IMPACT_PROVIDERS
from beadhive.bootstrap.tracker import BUILTIN_TRACKER_PROVIDERS, TRACKER_ADAPTER
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


def _manifests() -> dict[str, dict[str, object]]:
    return {
        value["plugin_id"]: value
        for value in (json.loads(document.payload) for document in builtin_manifest_documents())
    }


def _inventory_problems(manifests: dict[str, dict[str, object]]) -> list[str]:
    """Every way the built-in manifests disagree with the three provider sets.

    A built-in manifest belongs to exactly one set: the runtime catalog (legacy integrations,
    one required-by-the-catalog executable), the build-impact providers (no config fragment yet)
    or the tracker-adapter providers (a package plugin: only OPTIONAL host executables, which
    need not match any catalog, and always a published config fragment). Returns an empty list
    when they agree, so a negative control can show each rule actually fires.
    """

    catalog = {entry.plugin_id: entry for entry in PLUGIN_RUNTIME_CATALOG}
    impact = {provider.plugin_id: provider for provider in BUILTIN_IMPACT_PROVIDERS}
    tracker = {provider.plugin_id: provider for provider in BUILTIN_TRACKER_PROVIDERS}
    problems: list[str] = []
    overlaps = (set(catalog) & set(impact)) | (set(tracker) & (set(catalog) | set(impact)))
    if overlaps:
        problems.append(f"provider sets overlap: {sorted(overlaps)}")
    declared = set(catalog) | set(impact) | set(tracker)
    if set(manifests) != declared:
        problems.append(f"manifests {sorted(manifests)} != provider sets {sorted(declared)}")
    for plugin_id, manifest in manifests.items():
        provides = [item["id"] for item in manifest["capabilities"]["provides"]]
        executables = manifest["security"]["executables"]
        artifact = manifest["configuration"]["schema_artifact"]
        if manifest["configuration"]["namespace"] != f"plugins.{plugin_id}":
            problems.append(f"{plugin_id}: namespace is not plugins.{plugin_id}")
        urn = f"urn:beadhive:wire-schema:plugin-config:{plugin_id}:"
        if plugin_id in impact:
            if BUILD_IMPACT.capability_id not in provides:
                problems.append(f"{plugin_id}: does not provide {BUILD_IMPACT.capability_id}")
            if [item["name"] for item in executables] != [impact[plugin_id].external_executable]:
                problems.append(f"{plugin_id}: executable differs from the impact provider")
            # A build-system plugin with no configuration yet declares no schema fragment.
            if not (artifact is None or str(artifact).startswith(urn)):
                problems.append(f"{plugin_id}: schema_artifact is neither null nor its URN")
        elif plugin_id in tracker:
            if TRACKER_ADAPTER.capability_id not in provides:
                problems.append(f"{plugin_id}: does not provide {TRACKER_ADAPTER.capability_id}")
            if any(item["required"] for item in executables):
                problems.append(f"{plugin_id}: declares a required host executable")
            # A config-bearing provider always publishes its fragment.
            if not (isinstance(artifact, str) and artifact.startswith(urn)):
                problems.append(f"{plugin_id}: schema_artifact is not its plugin-config URN")
        elif plugin_id in catalog:
            entry = catalog[plugin_id]
            if entry.delivery != "runtime-core" or not entry.module.startswith("beadhive."):
                problems.append(f"{plugin_id}: catalog entry is not a runtime-core module")
            if [item["name"] for item in executables] != [entry.external_executable]:
                problems.append(f"{plugin_id}: executable differs from the catalog")
            if not (isinstance(artifact, str) and artifact.startswith(urn)):
                problems.append(f"{plugin_id}: schema_artifact is not its plugin-config URN")
    return problems


def test_manifest_inventory_matches_runtime_catalog_without_delivery_coupling():
    manifests = _manifests()
    assert tuple(sorted(manifests)) == BUILTIN_PLUGIN_IDS
    assert tuple(entry.plugin_id for entry in PLUGIN_RUNTIME_CATALOG) == (
        "orca",
        "observaloop",
        "hitch",
        "herdr",
        "repowise",
    )
    assert _inventory_problems(manifests) == []


def test_tracker_provider_set_is_exactly_the_manifests_providing_tracker_adapter():
    providers = {provider.plugin_id for provider in BUILTIN_TRACKER_PROVIDERS}
    providing = {
        plugin_id
        for plugin_id, manifest in _manifests().items()
        if TRACKER_ADAPTER.capability_id
        in [item["id"] for item in manifest["capabilities"]["provides"]]
    }
    assert providers == providing == {"tracker-github"}
    # The third set is real: the tracker plugin declares an OPTIONAL `gh` that no catalog lists.
    executables = _manifests()["tracker-github"]["security"]["executables"]
    assert [(item["name"], item["required"]) for item in executables] == [("gh", False)]


def _mutated(mutate) -> dict[str, dict[str, object]]:
    manifests = copy.deepcopy(_manifests())
    mutate(manifests)
    return manifests


@pytest.mark.parametrize(
    ("label", "mutate", "expected"),
    [
        (
            "an undeclared extra manifest",
            lambda m: m.update({"rogue": {**m["tracker-github"], "plugin_id": "rogue"}}),
            "!= provider sets",
        ),
        (
            "a tracker provider without its manifest",
            lambda m: m.pop("tracker-github"),
            "!= provider sets",
        ),
        (
            "a tracker manifest that stops providing tracker.adapter",
            lambda m: m["tracker-github"]["capabilities"]["provides"].clear(),
            "does not provide tracker.adapter",
        ),
        (
            "a tracker manifest that requires a host executable",
            lambda m: m["tracker-github"]["security"]["executables"][0].update(required=True),
            "required host executable",
        ),
        (
            "a tracker manifest with no config fragment",
            lambda m: m["tracker-github"]["configuration"].update(schema_artifact=None),
            "not its plugin-config URN",
        ),
        (
            "a tracker manifest with another plugin's fragment",
            lambda m: m["tracker-github"]["configuration"].update(
                schema_artifact="urn:beadhive:wire-schema:plugin-config:hitch:1"
            ),
            "not its plugin-config URN",
        ),
        (
            "a tracker manifest in the wrong namespace",
            lambda m: m["tracker-github"]["configuration"].update(namespace="plugins.github"),
            "namespace is not plugins.tracker-github",
        ),
        (
            "a catalog executable drift (the pre-existing rule still fires)",
            lambda m: m["orca"]["security"]["executables"][0].update(name="not-orca"),
            "executable differs from the catalog",
        ),
        (
            "an impact manifest with a config fragment of someone else",
            lambda m: m["pants"]["configuration"].update(schema_artifact="urn:x:y"),
            "neither null nor its URN",
        ),
    ],
)
def test_negative_control_each_inventory_rule_fires(label, mutate, expected):
    """P2 negative control: the generalised check is not vacuous, every rule detects its drift."""
    problems = _inventory_problems(_mutated(mutate))
    assert any(expected in problem for problem in problems), (label, problems)


def test_git_workspace_remains_the_required_non_plugin_exception():
    assert "git-workspace" not in BUILTIN_PLUGIN_IDS
    assert "git-workspace" not in {entry.plugin_id for entry in PLUGIN_RUNTIME_CATALOG}
    assert deps.by_name("git-workspace").required == deps.ALWAYS
