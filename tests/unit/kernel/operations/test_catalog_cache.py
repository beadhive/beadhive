"""The operation catalog is built once per process and never shared mutably (bh-svvnj)."""

from __future__ import annotations

import pytest

from beadhive.kernel import operations as catalog

FIXTURE_VERB = "hive fixture-probe|as_json:boolean:o"


@pytest.fixture
def build_count(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count real catalog builds through the module's own build function."""
    calls: list[int] = []
    build = catalog._build_operations

    def counted():
        calls.append(1)
        return build()

    catalog.clear_catalog_cache()
    monkeypatch.setattr(catalog, "_build_operations", counted)
    yield calls
    catalog.clear_catalog_cache()


def test_catalog_and_mcp_registry_inputs_are_built_once(build_count: list[int]) -> None:
    first = catalog.operations()
    for _ in range(3):
        assert catalog.operations() == first
        catalog.mcp_tool_projections()
        catalog.mcp_resource_projections()
        catalog.mcp_tool_composites()
        catalog.mcp_tool_projection("plan.check")
        catalog.mcp_resource_projection("alerts.show")
        catalog.document()
    assert len(build_count) == 1


def test_clear_catalog_cache_forces_a_fresh_build(build_count: list[int]) -> None:
    catalog.operations()
    catalog.clear_catalog_cache()
    catalog.operations()
    assert len(build_count) == 2


def test_monkeypatched_cli_rows_yield_the_mutated_catalog(
    monkeypatch: pytest.MonkeyPatch, build_count: list[int]
) -> None:
    original = {operation.name for operation in catalog.operations()}
    assert "hive.fixture-probe" not in original

    monkeypatch.setattr(catalog, "_CLI_ROWS", catalog._CLI_ROWS + "\n" + FIXTURE_VERB)
    mutated = {operation.name: operation for operation in catalog.operations()}
    assert set(mutated) == original | {"hive.fixture-probe"}
    assert mutated["hive.fixture-probe"].surfaces["cli"]["path"] == "hive fixture-probe"
    assert "hive.fixture-probe" in {row["name"] for row in catalog.document()["operations"]}

    monkeypatch.undo()
    assert {operation.name for operation in catalog.operations()} == original


def test_in_place_declaration_mutation_rebuilds_the_mcp_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "plan.check" in catalog.mcp_tool_projections()
    tool_name, parameters = catalog._MCP_TOOLS["plan.check"]
    monkeypatch.setitem(catalog._MCP_TOOLS, "plan.check", ("catalog_plan_check", parameters))
    assert catalog.mcp_tool_projections()["plan.check"] == ("catalog_plan_check", parameters)
    monkeypatch.undo()
    assert catalog.mcp_tool_projections()["plan.check"][0] == tool_name


def test_callers_cannot_mutate_the_shared_snapshot() -> None:
    before = catalog.document()
    operations = catalog.operations()
    by_name = {operation.name: operation for operation in operations}
    plan_check = by_name["plan.check"]
    plan_check.surfaces["mcp"]["tool"] = "hijacked"
    plan_check.surfaces["mcp"]["tool_parameters"].append("force")
    plan_check.constraints["interactive"] = True
    plan_check.parameters[0].schema["type"] = "hijacked"
    projections = catalog.mcp_tool_projections()
    projections.clear()
    catalog.mcp_tool_composites().clear()
    catalog.mcp_resource_projections().clear()

    assert catalog.document() == before
    assert catalog.operations() != operations
    assert catalog.mcp_tool_projections()["plan.check"][0] != "hijacked"
    assert catalog.mcp_tool_composites()
    assert catalog.mcp_resource_projections()


def test_catalog_snapshot_shares_no_container_with_the_declarations() -> None:
    declared_maps = {id(value) for value in catalog._CLI_PARAMETER_MAP.values()}
    declared_uris = {id(value) for value in catalog._MCP_NOTIFICATION_URIS.values()}
    for operation in catalog.operations():
        cli = operation.surfaces.get("cli")
        if cli:
            assert id(cli["parameter_map"]) not in declared_maps
        mcp = operation.surfaces.get("mcp")
        if mcp:
            assert id(mcp["progress"]["notification_uris"]) not in declared_uris
