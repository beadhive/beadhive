"""Pure contract tests for metadata-only plugin discovery."""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.metadata
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from beadhive.kernel.plugins import (
    BuiltInManifestSource,
    CapabilityRef,
    DiagnosticCode,
    DiagnosticSeverity,
    ExternalEntryPoint,
    ExternalEntryPointSource,
    ManifestDocument,
    ManifestProvenance,
    PluginKernelConfig,
    discover_plugins,
    parse_kernel_config,
)


def _manifest(
    plugin_id: str,
    *,
    capabilities: tuple[tuple[str, int], ...] = (("workspace.provision", 1),),
    beadhive_range: tuple[str, str] = ("0.15.0", "1.0.0"),
    kernel_range: tuple[str, str] = ("1.0.0", "2.0.0"),
    executable: tuple[str, bool, str, str] | None = None,
) -> bytes:
    executables = []
    if executable is not None:
        name, required, minimum, maximum = executable
        executables.append(
            {
                "name": name,
                "required": required,
                "version": {
                    "minimum_inclusive": minimum,
                    "maximum_exclusive": maximum,
                },
            }
        )
    value = {
        "manifest_version": 1,
        "plugin_id": plugin_id,
        "plugin_version": "1.2.3",
        "compatibility": {
            "beadhive": {
                "minimum_inclusive": beadhive_range[0],
                "maximum_exclusive": beadhive_range[1],
            },
            "plugin_kernel": {
                "minimum_inclusive": kernel_range[0],
                "maximum_exclusive": kernel_range[1],
            },
        },
        "capabilities": {
            "provides": [
                {"id": capability_id, "api_version": api_version}
                for capability_id, api_version in capabilities
            ]
        },
        "lifecycle": {"subscriptions": []},
        "configuration": {
            "namespace": f"plugins.{plugin_id}",
            "legacy_namespaces": [],
            "schema_artifact": None,
        },
        "presentation": {"cli": []},
        "security": {
            "permissions": [],
            "executables": executables,
            "credentials": [],
        },
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _document(plugin_id: str, **kwargs) -> ManifestDocument:
    return ManifestDocument(
        ManifestProvenance("built-in", f"{plugin_id}.json"),
        _manifest(plugin_id, **kwargs),
    )


def _subscription(subscription_id: str, event: str) -> dict[str, object]:
    return {
        "id": subscription_id,
        "event": event,
        "order": 10,
        "timeout_seconds": 5,
        "criticality": "best-effort",
        "idempotency": "not-applicable",
        "retry": {"max_attempts": 1, "backoff_seconds": 0},
        "compensation": {"mode": "none", "action_id": None},
    }


def _discover(*documents: ManifestDocument, config=None, executables=None):
    return discover_plugins(
        [BuiltInManifestSource(tuple(documents))],
        config=config,
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
        host_executables=executables,
    )


def test_builtin_discovery_is_deterministic_and_does_not_import_integrations():
    before = set(sys.modules)
    result = _discover(_document("zeta"), _document("alpha"))
    assert [plugin.manifest.plugin_id for plugin in result.plugins] == ["alpha", "zeta"]
    assert result.capabilities == ()
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        DiagnosticCode.DUPLICATE_CAPABILITY
    ]
    newly_imported = set(sys.modules) - before
    assert not any(name.endswith("_plugin") for name in newly_imported)
    assert not any(name.startswith("beadhive.integrations") for name in newly_imported)


def test_kernel_import_has_no_ambient_or_legacy_plugin_side_effects(monkeypatch):
    forbidden_imports = (
        "beadhive.plugins",
        "beadhive.herdr_plugin",
        "beadhive.hitch_plugin",
        "beadhive.orca",
        "beadhive.observaloop",
        "beadhive.repowise_plugin",
        "typer",
    )
    for name in tuple(sys.modules):
        if name.startswith("beadhive.kernel.plugins") or name.startswith(forbidden_imports):
            monkeypatch.delitem(sys.modules, name, raising=False)

    class RefuseOuterLayer(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(forbidden_imports):
                raise AssertionError(f"plugin kernel imported outer-layer dependency: {fullname}")
            return None

    def forbidden(action):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"plugin kernel attempted {action}")

        return fail

    finder = RefuseOuterLayer()
    sys.meta_path.insert(0, finder)
    monkeypatch.setattr(Path, "read_text", forbidden("filesystem read"))
    monkeypatch.setattr(importlib.metadata, "entry_points", forbidden("entry-point enumeration"))
    monkeypatch.setattr(socket, "create_connection", forbidden("network connection"))
    monkeypatch.setattr(subprocess, "Popen", forbidden("process spawn"))
    monkeypatch.setattr(subprocess, "run", forbidden("process spawn"))
    monkeypatch.setattr(os, "fork", forbidden("process fork"))
    monkeypatch.setattr(threading.Thread, "start", forbidden("runtime thread start"))
    try:
        module = importlib.import_module("beadhive.kernel.plugins")
        assert module.CapabilityRef("workspace.provision", 1).render() == "workspace.provision@1"
    finally:
        sys.meta_path.remove(finder)


@pytest.mark.parametrize(
    ("range_field", "code"),
    [
        ("beadhive_range", DiagnosticCode.INCOMPATIBLE_BEADHIVE),
        ("kernel_range", DiagnosticCode.INCOMPATIBLE_KERNEL),
    ],
)
def test_incompatible_manifest_is_unavailable_with_exact_diagnostic(range_field, code):
    result = _discover(_document("alpha", **{range_field: ("2.0.0", "3.0.0")}))
    assert result.plugins == ()
    assert [diagnostic.code for diagnostic in result.diagnostics] == [code]
    assert (
        result.diagnostics[0]
        .render()
        .startswith(
            f"plugin-kernel[{code.value}] severity=error plugin=alpha source=built-in:alpha.json:"
        )
    )


def test_duplicate_plugin_id_fails_before_enablement():
    first = _document("alpha")
    second = ManifestDocument(
        ManifestProvenance("built-in", "other.json"),
        first.payload,
    )
    result = _discover(first, second, config={"enabled": {"alpha": False}})
    assert result.plugins == ()
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        DiagnosticCode.DUPLICATE_PLUGIN_ID
    ]
    assert "alpha.json" in result.diagnostics[0].detail
    assert "other.json" in result.diagnostics[0].detail


def test_duplicate_capability_requires_explicit_exact_owner():
    capability = CapabilityRef("workspace.provision", 1)
    conflicted = _discover(_document("alpha"), _document("beta"))
    assert conflicted.owner_of(capability) is None
    assert [diagnostic.code for diagnostic in conflicted.diagnostics] == [
        DiagnosticCode.DUPLICATE_CAPABILITY
    ]
    assert "plugin_kernel.capability_owners.workspace.provision@1" in (
        conflicted.diagnostics[0].detail
    )

    selected = _discover(
        _document("alpha"),
        _document("beta"),
        config={"capability_owners": {"workspace.provision@1": "beta"}},
    )
    assert selected.owner_of(capability) == "beta"
    assert selected.errors == ()


def test_disabled_plugin_is_visible_but_unavailable():
    result = _discover(_document("alpha"), config={"enabled": {"alpha": False}})
    assert result.plugins == ()
    assert result.diagnostics == (result.diagnostics[0],)
    assert result.diagnostics[0].code is DiagnosticCode.DISABLED_PLUGIN
    assert result.diagnostics[0].severity is DiagnosticSeverity.INFO


@pytest.mark.parametrize("required", [True, False])
def test_missing_executable_fails_or_degrades_exactly(required):
    result = _discover(_document("alpha", executable=("helper", required, "1.0.0", "2.0.0")))
    expected = (
        DiagnosticCode.MISSING_EXECUTABLE
        if required
        else DiagnosticCode.OPTIONAL_EXECUTABLE_MISSING
    )
    assert [diagnostic.code for diagnostic in result.diagnostics] == [expected]
    assert bool(result.plugins) is (not required)
    assert "required range [1.0.0,2.0.0)" in result.diagnostics[0].detail


def test_declared_executable_version_is_checked_without_path_lookup():
    result = _discover(
        _document("alpha", executable=("helper", True, "1.0.0", "2.0.0")),
        executables={"helper": "1.5.0"},
    )
    assert [plugin.manifest.plugin_id for plugin in result.plugins] == ["alpha"]
    assert result.errors == ()


@pytest.mark.parametrize(
    "config",
    [
        {"unknown": True},
        {"enabled": {"alpha": "yes"}},
        {"capability_owners": {"workspace.provision": "alpha"}},
        {"allow_external_entry_points": "yes"},
    ],
)
def test_invalid_core_config_fails_before_reading_sources(config):
    reads: list[str] = []

    class Source:
        def read(self, *, allow_external: bool):
            reads.append(str(allow_external))
            raise AssertionError("source must not be read")

    result = discover_plugins(
        [Source()],
        config=config,
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
    )
    assert reads == []
    assert [diagnostic.code for diagnostic in result.diagnostics] == [DiagnosticCode.INVALID_CONFIG]


@pytest.mark.parametrize("external", ["yes", "false"])
def test_direct_config_non_bool_external_policy_cannot_invoke_loader(external):
    calls: list[str] = []

    def load(entry: ExternalEntryPoint) -> bytes:
        calls.append(entry.name)
        return _manifest("alpha")

    result = discover_plugins(
        [ExternalEntryPointSource((ExternalEntryPoint("alpha", "dist-alpha"),), load)],
        config=PluginKernelConfig(allow_external_entry_points=external),  # type: ignore[arg-type]
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
    )
    assert calls == []
    assert [diagnostic.code for diagnostic in result.diagnostics] == [DiagnosticCode.INVALID_CONFIG]


@pytest.mark.parametrize(
    "config",
    [
        PluginKernelConfig(enabled={"alpha": "yes"}),  # type: ignore[dict-item]
        PluginKernelConfig(enabled={1: True}),  # type: ignore[dict-item]
        PluginKernelConfig(capability_owners={"workspace.provision@1": 7}),  # type: ignore[dict-item]
        PluginKernelConfig(capability_owners={1: "alpha"}),  # type: ignore[dict-item]
    ],
)
def test_forged_direct_config_mappings_fail_before_source_access(config):
    reads: list[bool] = []

    class Source:
        def read(self, *, allow_external: bool):
            reads.append(allow_external)
            raise AssertionError("source must not be read")

    result = discover_plugins(
        [Source()],
        config=config,
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
    )
    assert reads == []
    assert [diagnostic.code for diagnostic in result.diagnostics] == [DiagnosticCode.INVALID_CONFIG]


def test_direct_and_mapping_config_have_identical_valid_semantics():
    mapping = {
        "enabled": {"alpha": True},
        "capability_owners": {"workspace.provision@1": "alpha"},
        "allow_external_entry_points": False,
    }
    direct = PluginKernelConfig(
        enabled={"alpha": True},
        capability_owners={"workspace.provision@1": "alpha"},
        allow_external_entry_points=False,
    )
    documents = (_document("alpha"), _document("beta"))
    assert _discover(*documents, config=direct) == _discover(*documents, config=mapping)


def test_direct_config_is_snapshotted_before_any_source_can_mutate_it():
    enabled: dict[str, bool] = {"alpha": True, "beta": True}
    owners = {"workspace.provision@1": "alpha"}

    class MutatingSource:
        def read(self, *, allow_external: bool):
            assert allow_external is False
            enabled["alpha"] = False
            owners["workspace.provision@1"] = "beta"
            return BuiltInManifestSource((_document("alpha"), _document("beta"))).read(
                allow_external=allow_external
            )

    direct = PluginKernelConfig(enabled=enabled, capability_owners=owners)
    result = discover_plugins(
        [MutatingSource()],
        config=direct,
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
    )
    assert [plugin.manifest.plugin_id for plugin in result.plugins] == ["alpha", "beta"]
    assert result.owner_of(CapabilityRef("workspace.provision", 1)) == "alpha"
    assert result.errors == ()


def test_parsed_config_is_an_immutable_snapshot_of_direct_mappings():
    enabled = {"alpha": True}
    owners = {"workspace.provision@1": "alpha"}
    parsed = parse_kernel_config(PluginKernelConfig(enabled=enabled, capability_owners=owners))
    enabled["alpha"] = False
    owners["workspace.provision@1"] = "other"
    assert dict(parsed.enabled) == {"alpha": True}
    assert dict(parsed.capability_owners) == {"workspace.provision@1": "alpha"}
    with pytest.raises(TypeError):
        parsed.enabled["alpha"] = False  # type: ignore[index]


def test_stale_explicit_owner_is_an_exact_config_error():
    result = _discover(
        _document("alpha"),
        config={"capability_owners": {"workspace.provision@1": "missing"}},
    )
    assert result.owner_of(CapabilityRef("workspace.provision", 1)) is None
    assert [diagnostic.code for diagnostic in result.diagnostics] == [DiagnosticCode.INVALID_CONFIG]
    assert "configured owner 'missing' is not an available provider" in result.diagnostics[0].detail


def test_invalid_manifest_rejects_undeclared_secret_with_source_diagnostic():
    value = json.loads(_manifest("alpha"))
    value["security"]["credentials"] = [
        {
            "id": "token",
            "required": True,
            "purpose": "authenticate",
            "sources": [{"kind": "environment-variable", "name": "TOKEN"}],
            "secret": "do-not-accept",
        }
    ]
    document = ManifestDocument(
        ManifestProvenance("built-in", "alpha.json"),
        json.dumps(value).encode(),
    )
    result = _discover(document)
    assert result.plugins == ()
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        DiagnosticCode.INVALID_MANIFEST
    ]
    assert "undeclared fields ['secret']" in result.diagnostics[0].detail


@pytest.mark.parametrize(
    ("subscription_id", "event", "expected"),
    [
        ("other.observe", "worktree.created", "qualified by plugin ID 'alpha'"),
        ("alpha.observe", "invented.event", "unknown kernel lifecycle event 'invented.event'"),
    ],
)
def test_manifest_subscriptions_use_plugin_qualified_ids_and_kernel_events(
    subscription_id, event, expected
):
    value = json.loads(_manifest("alpha"))
    value["lifecycle"]["subscriptions"] = [_subscription(subscription_id, event)]
    result = _discover(
        ManifestDocument(
            ManifestProvenance("built-in", "alpha.json"),
            json.dumps(value).encode(),
        )
    )
    assert result.plugins == ()
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        DiagnosticCode.INVALID_MANIFEST
    ]
    assert expected in result.diagnostics[0].detail


def test_external_entry_point_metadata_is_policy_gated_and_sorted():
    calls: list[tuple[str, str]] = []
    payloads = {"a": _manifest("alpha"), "z": _manifest("zeta", capabilities=())}

    def load(entry: ExternalEntryPoint) -> bytes:
        calls.append((entry.name, entry.distribution))
        return payloads[entry.name]

    source = ExternalEntryPointSource(
        (
            ExternalEntryPoint("z", "dist-z"),
            ExternalEntryPoint("a", "dist-a"),
        ),
        load,
    )
    disabled = discover_plugins(
        [source],
        config=None,
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
    )
    assert calls == []
    assert [diagnostic.code for diagnostic in disabled.diagnostics] == [
        DiagnosticCode.EXTERNAL_LOADING_DISABLED,
        DiagnosticCode.EXTERNAL_LOADING_DISABLED,
    ]

    enabled = discover_plugins(
        [source],
        config={"allow_external_entry_points": True},
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
    )
    assert calls == [("a", "dist-a"), ("z", "dist-z")]
    assert [plugin.manifest.plugin_id for plugin in enabled.plugins] == ["alpha", "zeta"]


def test_only_declared_metadata_source_is_invoked():
    calls: list[bool] = []

    class DeclaredSource:
        def read(self, *, allow_external: bool):
            calls.append(allow_external)
            return BuiltInManifestSource((_document("alpha"),)).read(allow_external=allow_external)

    result = discover_plugins(
        [DeclaredSource()],
        config=None,
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
    )
    assert calls == [False]
    assert [plugin.manifest.plugin_id for plugin in result.plugins] == ["alpha"]


def test_source_enumeration_order_cannot_change_result_or_diagnostics():
    alpha = BuiltInManifestSource((_document("alpha"),))
    beta = BuiltInManifestSource((_document("beta"),))
    forward = discover_plugins(
        [alpha, beta],
        config=None,
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
    )
    reverse = discover_plugins(
        [beta, alpha],
        config=None,
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
    )
    assert reverse == forward
