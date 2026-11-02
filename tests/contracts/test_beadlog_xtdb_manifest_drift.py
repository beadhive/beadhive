"""The packaged ``beadlog-xtdb`` manifest cannot drift from what root relies on.

``packages/beadhive-beadlog-xtdb`` ships ``plugin.json`` and root ships a byte copy under
``kernel/plugins/manifests/`` (copied, never generated). The two must be byte-identical, provide
exactly the ``beadlog.store`` capability root composes, own the namespace root validates and name
the plugin-config schema root publishes. Each check has a negative control.
"""

from __future__ import annotations

import copy
import json
from importlib import resources

import pytest

from beadhive.bootstrap.beadlog_store import BEADLOG_STORE, STORE_FRAGMENT_MODELS
from beadhive.kernel.plugins import builtin_manifest_documents
from beadhive.modules.config.application.plugin_fragments import builtin_plugin_fragments

PLUGIN_ID = "beadlog-xtdb"


def _packaged_bytes() -> bytes:
    return (resources.files("beadhive_beadlog_xtdb") / "plugin.json").read_bytes()


def _builtin_bytes() -> bytes:
    return next(
        doc.payload
        for doc in builtin_manifest_documents()
        if doc.provenance.source_name == f"{PLUGIN_ID}.json"
    )


def _drift(manifest: dict) -> list[str]:
    problems: list[str] = []
    provides = [(c["id"], c["api_version"]) for c in manifest["capabilities"]["provides"]]
    if provides != [(BEADLOG_STORE.capability_id, BEADLOG_STORE.api_version)]:
        problems.append(f"provides {provides}")
    configuration = manifest["configuration"]
    if configuration["namespace"] != f"plugins.{PLUGIN_ID}":
        problems.append("namespace")
    published = {fragment.artifact_id for fragment in builtin_plugin_fragments()}
    if configuration["schema_artifact"] not in published:
        problems.append("schema_artifact is not a published plugin-config fragment")
    if PLUGIN_ID not in STORE_FRAGMENT_MODELS:
        problems.append("root has no fragment model")
    if manifest["security"]["executables"]:
        problems.append("declares an executable (bh never runs the node)")
    if any(item["required"] for item in manifest["security"]["credentials"]):
        problems.append("a credential is required")
    return problems


def test_the_packaged_manifest_is_byte_identical_to_the_builtin_copy() -> None:
    assert _packaged_bytes() == _builtin_bytes()


def test_the_manifest_matches_what_root_composes_and_publishes() -> None:
    assert _drift(json.loads(_builtin_bytes())) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m["capabilities"]["provides"].clear(),
        lambda m: m["capabilities"]["provides"][0].update(api_version=2),
        lambda m: m["capabilities"]["provides"][0].update(id="beadlog.loader"),
        lambda m: m["configuration"].update(namespace="plugins.xtdb"),
        lambda m: m["configuration"].update(schema_artifact="urn:x"),
        lambda m: m["security"]["executables"].append({"name": "docker", "required": False}),
        lambda m: m["security"]["credentials"][0].update(required=True),
    ],
)
def test_negative_control_drift_is_detected(mutate) -> None:
    manifest = copy.deepcopy(json.loads(_builtin_bytes()))
    mutate(manifest)
    assert _drift(manifest) != []
