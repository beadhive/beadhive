"""Compatibility and concrete-adapter contracts for the extracted hives module."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from beadhive import cli, hive_identity, hive_services, mcp
from beadhive.modules.hives import HiveIdentity, OnboardHiveResult

ROOT = Path(__file__).parents[2]


def test_legacy_identity_facade_keeps_imports_but_module_owns_policy() -> None:
    assert hive_identity.identity_record.__module__ == "beadhive.modules.hives.domain.identity"
    assert hive_identity.affiliation_for_kind.__module__ == (
        "beadhive.modules.hives.domain.identity"
    )
    assert hive_identity.list_payload.__module__ == "beadhive.hive_identity"
    assert "registry.hives" in inspect.getsource(hive_identity.list_payload)


def test_each_outer_transport_owns_its_hive_list_projection() -> None:
    cli_source = inspect.getsource(cli.hive_list)
    mcp_source = inspect.getsource(mcp._register_hive_tools)
    legacy_source = inspect.getsource(hive_identity._page_payload)

    assert 'jsonout.envelope(\n                "hive list"' in cli_source
    assert "list(page.hives)" in cli_source
    assert "list(result.discovery.candidates)" in mcp_source
    assert "list(result.discovery.registered)" in mcp_source
    assert "as_payload" not in mcp_source
    assert '"schema_version": SCHEMA_VERSION' in legacy_source
    assert '"command": "hive list"' in legacy_source
    assert "list(page.hives)" in legacy_source


def test_workspace_adapter_consumes_the_accepted_cgcg_root_contract() -> None:
    adapter = hive_services.AcceptedWorkspaceRealizer(lambda: "/accepted/root")

    assert adapter.target_for(HiveIdentity.parse("github/acme/api")) == (
        "/accepted/root/github/acme/api"
    )


def test_cli_and_mcp_hive_transports_enter_the_same_service_boundary() -> None:
    cli_sources = "\n".join(
        inspect.getsource(function)
        for function in (
            cli.hive_add,
            cli.hive_onboard,
            cli.hive_list,
            cli.hive_status,
            cli.hive_retire,
            cli.hive_reclaim,
        )
    )
    mcp_source = inspect.getsource(mcp._register_hive_tools)

    assert cli_sources.count("hive_lifecycle_service") == 6
    assert mcp_source.count("hive_lifecycle_service") >= 6


def test_concrete_lifecycle_adapter_keeps_kernel_owned_plugin_delivery() -> None:
    onboard_source = (ROOT / "src/beadhive/onboard.py").read_text()
    retire_source = (ROOT / "src/beadhive/retire.py").read_text()
    plugin_source = (ROOT / "src/beadhive/plugins.py").read_text()

    assert "action_composition(" in onboard_source
    assert "onboard_participants(" in onboard_source
    assert "retire_observers(" in retire_source
    assert "LifecycleDispatcher" in plugin_source
    assert "declaration.policy" in plugin_source
    adapter_source = inspect.getsource(hive_services.KernelHiveLifecycle)
    assert "execute_onboard" in adapter_source
    assert "execute_retire_hive" in adapter_source
    assert "execute_reclaim_hive" in adapter_source
    for presentation_name in ("as_json", "machine", "jsonout", "typer", "render"):
        assert presentation_name not in adapter_source


def test_mcp_projects_the_same_silent_onboard_result_without_transport_side_effects(
    monkeypatch, tmp_path
) -> None:
    functions = {}

    def register(name):
        def decorator(function):
            functions[name] = function
            return function

        return decorator

    class SilentService:
        def onboard(self, request):
            return OnboardHiveResult(
                request.identity,
                str(tmp_path / request.identity.canonical_id),
                cloned=False,
                registered=True,
                prefix="acme-api",
                synced=True,
                kind="org-native",
                warnings=("semantic warning",),
            )

    async def no_notification(*_args, **_kwargs):
        return None

    target = tmp_path / "github" / "acme" / "api"
    target.mkdir(parents=True)
    monkeypatch.setattr(mcp, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        mcp.hive_services, "hive_lifecycle_service", lambda **_kwargs: SilentService()
    )
    monkeypatch.setattr(mcp, "_notify_updated", no_notification)
    monkeypatch.setattr(mcp, "_notify_alerts_if_changed", no_notification)
    mcp._register_hive_tools(None, register, register)

    payload = asyncio.run(functions["hive.onboard"]("github", "acme", "api"))

    assert payload == {
        "cloned": False,
        "registered": True,
        "prefix": "acme-api",
        "synced": True,
        "warnings": ["semantic warning"],
    }
