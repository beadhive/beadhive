"""The discovery decoder consumes the published PluginManifest v1 artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from beadhive.kernel.lifecycle import Criticality, Idempotency
from beadhive.kernel.plugins import (
    BuiltInManifestSource,
    DiagnosticCode,
    ManifestDocument,
    ManifestProvenance,
    discover_plugins,
)

CONFORMANCE = Path(__file__).parents[2] / "docs/schemas/wire/v1.5.0/conformance.json"


def _case(name: str) -> bytes:
    document = json.loads(CONFORMANCE.read_text(encoding="utf-8"))
    value = next(case["input"] for case in document["cases"] if case["name"] == name)
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _discover(payload: bytes):
    source = BuiltInManifestSource(
        (
            ManifestDocument(
                ManifestProvenance("built-in", "published-conformance.json"),
                payload,
            ),
        )
    )
    return discover_plugins(
        [source],
        config=None,
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
        host_executables={"repowise": "1.1.0"},
    )


def test_published_valid_manifest_is_discoverable():
    result = _discover(_case("plugin-manifest-valid"))
    assert [plugin.manifest.plugin_id for plugin in result.plugins] == ["repowise"]
    assert result.errors == ()
    manifest = result.plugins[0].manifest
    assert manifest.configuration_namespace == "plugins.repowise"
    assert manifest.configuration_legacy_namespaces == ("repowise",)
    assert manifest.lifecycle_subscriptions[0].subscription_id == "repowise.index-worktree"
    assert manifest.lifecycle_subscriptions[0].policy.retry.max_attempts == 2
    assert manifest.lifecycle_subscriptions[0].policy.criticality is Criticality.BEST_EFFORT
    assert manifest.lifecycle_subscriptions[0].policy.idempotency is Idempotency.REQUIRED
    assert manifest.cli_projections[0].command == "plugin repowise"
    assert manifest.permissions[0].permission_id == "filesystem.workspace.read"
    assert manifest.credentials[0].sources[0].name == "REPOWISE_API_TOKEN"


def test_published_secret_value_case_is_an_exact_invalid_manifest():
    result = _discover(_case("plugin-manifest-secret-value-invalid"))
    assert result.plugins == ()
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        DiagnosticCode.INVALID_MANIFEST
    ]
    assert "undeclared fields ['secret_value']" in result.diagnostics[0].detail
