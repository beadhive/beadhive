"""The build.impact and build.verify capabilities, and bootstrap backend collection."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

import beadhive.selective_validation as selective_validation_module
from beadhive.bootstrap import impact as bootstrap_impact
from beadhive.bootstrap.impact import (
    BUILD_IMPACT_PORT,
    BUILTIN_IMPACT_PROVIDERS,
    ImpactBackendProvider,
    collect_impact_backends,
    impact_resolver,
)
from beadhive.kernel.plugins import (
    BUILD_IMPACT,
    BUILD_VERIFY,
    BuildVerifier,
    BuiltInManifestSource,
    DiagnosticCode,
    DiagnosticSeverity,
    ManifestDocument,
    ManifestProvenance,
    PluginDiagnostic,
    builtin_manifest_documents,
    builtin_manifest_source,
    discover_plugins,
)
from beadhive.modules.config.contracts import AttestConfig
from beadhive.modules.work.application.impact import (
    FailClosedResolver,
    NativeFullResolver,
    select_resolver,
)
from beadhive.modules.work.contracts.impact import ImpactBackend
from beadhive.modules.work.domain.impact import AttestKey, BackendImpact, ChangedPath
from beadhive_pants.impact import PantsImpactBackend

KEYS = (AttestKey("unit", "just unit", selectors={"pants": "attest:unit"}),)


class Trees:
    def tree_of(self, repo: str, rev: str) -> str:
        return f"{rev}-tree"

    def changed_paths(self, repo: str, base_tree: str, head_tree: str):
        return (ChangedPath("src/a.py"),)


@dataclass
class FakeBackend:
    name: str
    version: str = "1"

    def analyze(self, request) -> BackendImpact:
        return BackendImpact(backend_version=self.version)


def _pants_repo(tmp_path: Path) -> Path:
    (tmp_path / "pants.toml").write_text('[GLOBAL]\npants_version = "2.32.1"\n')
    return tmp_path


def _second_impact_manifest(plugin_id: str = "turbo") -> ManifestDocument:
    """A second plugin providing ``build.impact``, derived from the built-in Pants manifest."""
    pants = next(
        document
        for document in builtin_manifest_documents()
        if document.provenance.source_name == "pants.json"
    )
    raw = json.loads(pants.payload)
    raw["plugin_id"] = plugin_id
    raw["configuration"]["namespace"] = f"plugins.{plugin_id}"
    raw["security"]["executables"] = []
    return ManifestDocument(
        ManifestProvenance("built-in", f"{plugin_id}.json"), json.dumps(raw).encode()
    )


def _sources_with_second_provider():
    return (builtin_manifest_source(), BuiltInManifestSource((_second_impact_manifest(),)))


def _providers(loaded: list[str]):
    def load(plugin_id: str):
        def build(repo: str):
            loaded.append(plugin_id)
            return FakeBackend(plugin_id)

        return build

    return (
        ImpactBackendProvider("pants", "pants", "", "", load("pants")),
        ImpactBackendProvider("turbo", "turbo", "", "", load("turbo")),
    )


def test_build_capabilities_are_versioned_kernel_declarations():
    assert BUILD_IMPACT.render() == "build.impact@1"
    assert BUILD_VERIFY.render() == "build.verify@1"
    assert BUILD_IMPACT_PORT.capability == BUILD_IMPACT
    assert BUILD_IMPACT_PORT.port_type is ImpactBackend

    class Healthy:
        def verify(self, repo: str) -> tuple[PluginDiagnostic, ...]:
            return ()

    assert isinstance(Healthy(), BuildVerifier)
    assert not isinstance(object(), BuildVerifier)


def test_pants_is_the_sole_builtin_build_impact_provider():
    result = discover_plugins(
        [builtin_manifest_source()],
        config=None,
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
    )

    assert result.errors == ()
    assert result.owner_of(BUILD_IMPACT) == "pants"
    assert result.owner_of(BUILD_VERIFY) is None
    assert [provider.plugin_id for provider in BUILTIN_IMPACT_PROVIDERS] == ["pants"]
    assert BUILTIN_IMPACT_PROVIDERS[0].module == "beadhive_pants.impact"
    assert BUILTIN_IMPACT_PROVIDERS[0].object_name == "PantsImpactBackend"


def test_bootstrap_collects_the_pants_backend_from_its_manifest(tmp_path):
    collected = collect_impact_backends(str(_pants_repo(tmp_path)))

    assert list(collected.backends) == ["pants"]
    assert isinstance(collected.backends["pants"], PantsImpactBackend)
    assert collected.backends["pants"].version == "2.32.1"
    assert collected.diagnostics == ()


def test_backend_that_cannot_be_built_for_this_checkout_is_not_available(tmp_path):
    collected = collect_impact_backends(str(tmp_path))  # no pants.toml

    assert dict(collected.backends) == {}
    reason = (
        select_resolver("pants", tree_diff=Trees(), backends=collected.backends)
        .resolve("/r", "b", "h", KEYS)
        .fallback_reason
    )
    assert reason == "pants: backend not available"


def test_two_build_impact_providers_without_owner_selection_fail_closed(tmp_path):
    loaded: list[str] = []
    collected = collect_impact_backends(
        str(_pants_repo(tmp_path)),
        sources=_sources_with_second_provider(),
        providers=_providers(loaded),
    )

    (conflict,) = collected.diagnostics
    assert conflict.code is DiagnosticCode.DUPLICATE_CAPABILITY
    assert conflict.severity is DiagnosticSeverity.ERROR
    assert conflict.capability == BUILD_IMPACT
    assert conflict.render() == (
        "plugin-kernel[duplicate-capability] severity=error plugin=? "
        "capability=build.impact@1: multiple providers ['pants', 'turbo']; "
        "configure plugin_kernel.capability_owners.build.impact@1"
    )
    assert dict(collected.backends) == {}
    assert loaded == []  # neither implementation is loaded when ownership is ambiguous
    receipt = select_resolver("pants", tree_diff=Trees(), backends=collected.backends).resolve(
        "/r", "b", "h", KEYS
    )
    assert receipt.fallback_reason == "pants: backend not available"


def test_capability_owner_selection_resolves_the_conflict(tmp_path):
    loaded: list[str] = []
    collected = collect_impact_backends(
        str(_pants_repo(tmp_path)),
        plugin_kernel={"capability_owners": {"build.impact@1": "turbo"}},
        sources=_sources_with_second_provider(),
        providers=_providers(loaded),
    )

    assert collected.diagnostics == ()
    assert list(collected.backends) == ["turbo"]
    assert loaded == ["turbo"]


def test_disabled_plugin_provides_no_backend(tmp_path):
    loaded: list[str] = []
    collected = collect_impact_backends(
        str(_pants_repo(tmp_path)),
        plugin_kernel={"enabled": {"pants": False}},
        providers=_providers(loaded),
    )

    assert dict(collected.backends) == {}
    assert loaded == []


def test_selected_manifest_without_runtime_binding_provides_no_backend(tmp_path):
    collected = collect_impact_backends(str(_pants_repo(tmp_path)), providers=())

    assert dict(collected.backends) == {}
    assert collected.diagnostics == ()


def test_impact_resolver_binds_the_collected_backend(tmp_path):
    attest = AttestConfig.model_validate({"impact": {"backend": "pants", "timeout_seconds": 7}})
    resolver = impact_resolver(attest, repo=str(_pants_repo(tmp_path)), tree_diff=Trees())

    assert isinstance(resolver, FailClosedResolver)
    assert isinstance(resolver.backend, PantsImpactBackend)


def test_native_full_needs_no_discovery(monkeypatch, tmp_path):
    def refuse(*_args, **_kwargs):
        raise AssertionError("native-full must not collect backends")

    monkeypatch.setattr(bootstrap_impact, "collect_impact_backends", refuse)

    assert isinstance(
        impact_resolver(AttestConfig(), repo=str(tmp_path), tree_diff=Trees()), NativeFullResolver
    )


@pytest.mark.parametrize("explicit", [{}, {"pants": FakeBackend("pants")}])
def test_explicit_backends_are_used_as_given(monkeypatch, tmp_path, explicit):
    monkeypatch.setattr(
        bootstrap_impact,
        "collect_impact_backends",
        lambda *_a, **_k: pytest.fail("explicit backends must not be replaced"),
    )
    attest = AttestConfig.model_validate({"impact": {"backend": "pants"}})

    resolver = impact_resolver(attest, backends=explicit, repo=str(tmp_path), tree_diff=Trees())

    assert isinstance(resolver, FailClosedResolver if explicit else NativeFullResolver)


def test_selective_validation_neither_imports_nor_constructs_a_backend():
    tree = ast.parse(Path(selective_validation_module.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    assert not any("impact_pants" in module for module in imported)
    assert "PantsImpactBackend" not in names | attributes
