"""Regeneration and compatibility checks for the catalog-derived Typer groups."""

from __future__ import annotations

import hashlib
import inspect
import json
from copy import deepcopy
from dataclasses import replace
from functools import wraps

import pytest
import typer
from typer.main import get_command
from typer.testing import CliRunner

from beadhive import cli, plan, work
from beadhive.adapters.cli.declarations import (
    PROJECTION_EXCLUSIONS,
    TRANSPORT_MECHANICS,
    command_declarations,
    parent_declarations,
)
from beadhive.adapters.cli.tree import project_cli_tree
from beadhive.cli_projection import (
    HAND_AUTHORED_CLI_GROUPS,
    MIGRATED_CLI_GROUPS,
    CatalogProjectionError,
    catalog_cli_groups,
    project_cli_group,
    validate_catalog_generation_rules,
    validate_migration_inventory,
)
from beadhive.operation_catalog import operations


def _click_inventory(app: typer.Typer) -> list[dict[str, object]]:
    rows = []
    for verb, command in get_command(app).commands.items():
        rows.append(
            {
                "verb": verb,
                "hidden": bool(command.hidden),
                "context": command.context_settings or {},
                "parameters": [
                    {
                        "name": parameter.name,
                        "opts": [
                            *getattr(parameter, "opts", ()),
                            *getattr(parameter, "secondary_opts", ()),
                        ],
                        "required": parameter.required,
                        "type": type(parameter).__name__,
                    }
                    for parameter in command.params
                ],
            }
        )
    return rows


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _handlers(group: str) -> dict[str, object]:
    module = work if group == "work" else plan
    return dict(module.CLI_HANDLERS)


def test_migration_inventory_names_every_catalog_group() -> None:
    validate_migration_inventory()
    assert set(MIGRATED_CLI_GROUPS) == set(catalog_cli_groups())
    assert HAND_AUTHORED_CLI_GROUPS == ()
    assert set(MIGRATED_CLI_GROUPS).isdisjoint(HAND_AUTHORED_CLI_GROUPS)
    assert set(MIGRATED_CLI_GROUPS) | set(HAND_AUTHORED_CLI_GROUPS) == set(catalog_cli_groups())


def test_assembled_tree_is_entirely_catalog_derived_and_idempotent() -> None:
    declarations = command_declarations()
    parents = parent_declarations()
    assert len(declarations) == 214
    assert len(parents) == 38
    assert cli.CLI_PROJECTION.paths == tuple(row.path for row in declarations)
    assert cli.CLI_PROJECTION.operations == tuple(row.operation for row in declarations)
    assert cli.CLI_PROJECTION.parents == tuple(row.path for row in parents)
    assert cli.CLI_PROJECTION.transport_mechanics == (
        "<root>",
        "hive sync",
        "host",
        "host daemon",
    )
    assert tuple(row.path for row in TRANSPORT_MECHANICS) == (
        "<root>",
        "hive sync",
        "host",
        "host daemon",
    )
    assert PROJECTION_EXCLUSIONS == {}
    assert cli.CLI_PROJECTION.exclusions == ()
    assert cli.CLI_PROJECTION.unavailable_optional_plugins == ()

    before = _click_inventory(cli.app)
    before_infos = tuple(cli.app.registered_commands)
    before_callbacks = tuple(info.callback for info in before_infos)
    repeated = project_cli_tree(cli.app)
    assert repeated == cli.CLI_PROJECTION
    assert _click_inventory(cli.app) == before
    assert tuple(cli.app.registered_commands) != before_infos
    assert tuple(info.callback for info in cli.app.registered_commands) == before_callbacks


def test_work_and_plan_are_complete_deterministic_regenerations() -> None:
    expected = {"work": work, "plan": plan}
    for group, module in expected.items():
        scratch = typer.Typer(no_args_is_help=True)
        report = project_cli_group(scratch, group, _handlers(group))
        assert _click_inventory(scratch) == _click_inventory(module.app)
        assert report.operations == module.CLI_PROJECTION.operations
        assert report.paths == module.CLI_PROJECTION.paths
        assert report.digest == module.CLI_PROJECTION.digest


def test_generator_refuses_an_incomplete_handler_map() -> None:
    handlers = _handlers("plan")
    handlers.pop("plan.repair")
    with pytest.raises(CatalogProjectionError, match=r"missing=\['plan\.repair'\]"):
        project_cli_group(typer.Typer(), "plan", handlers)


def test_generated_projection_preserves_help_and_every_migrated_flag() -> None:
    # These hashes pin the full Click parameter inventory: names, long/short flags, arguments,
    # requiredness, hidden state, passthrough context, command ordering, and parameter types.
    assert _digest(_click_inventory(work.app)) == (
        "af87d2877a7f0c8e54cf94e368b25198c702ca87b3fe63cb9ba5c63c044296ef"
    )
    assert _digest(_click_inventory(plan.app)) == (
        "7156c6aeee559e071428f3e3856586da7500742420cbec3286bbf95db4493f19"
    )

    runner = CliRunner()
    invocation_env = {"COLUMNS": "120", "BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1"}
    expected_help = {
        (): "e4df4b937715f1557ae68be69866629bf32a96df6461972aa453834af05ac9a6",
        ("work",): "0e2be538a1848edde4a75489b843e16ece7a988afad5ff87854585fcf0278fc3",
        ("plan",): "25562be42291d9cb0489760bb59061838e88562deb07cfe3b1e1fc20dc34af2b",
    }
    for path, digest in expected_help.items():
        result = runner.invoke(cli.app, [*path, "--help"], env=invocation_env)
        assert result.exit_code == 0
        assert hashlib.sha256(result.stdout.encode()).hexdigest() == digest


def test_naming_adr_is_a_generator_gate_and_generated_traces_are_exact() -> None:
    validate_catalog_generation_rules()
    for group, module in (("work", work), ("plan", plan)):
        assert (
            tuple(
                f"{group}.{command.name or command.callback.__name__.rstrip('_')}"
                for command in module.app.registered_commands
            )
            == module.CLI_PROJECTION.operations
        )
        for command in module.app.registered_commands:
            verb = command.name or command.callback.__name__.rstrip("_")
            assert getattr(command.callback, "__otel_verb__", None) == f"{group}.{verb}"


@pytest.mark.parametrize(
    ("operation_name", "surface", "field", "value", "rule"),
    [
        ("work.brief", "cli", "path", "work briefs", 1),
        ("work.list", "cli", "path", "work inventory", 2),
        ("work.show", "cli", "parameters", ["bead", "view", "json", "hive"], 3),
        ("plan.file", "mcp", "tool", "bd_create", 6),
        ("plan.status", "mcp", "resource", "beadhive://plan/", 7),
        ("work.brief", "cli", "path", "work ws-brief", 8),
    ],
)
def test_generator_rejects_catalog_breakage_for_naming_rules(
    operation_name: str,
    surface: str,
    field: str,
    value: object,
    rule: int,
) -> None:
    specs = list(operations())
    index = next(i for i, operation in enumerate(specs) if operation.name == operation_name)
    surfaces = deepcopy(specs[index].surfaces)
    surfaces[surface][field] = value
    specs[index] = replace(specs[index], surfaces=surfaces)
    with pytest.raises(CatalogProjectionError, match=rf"naming convention {rule}"):
        project_cli_group(
            typer.Typer(),
            operation_name.split(".", 1)[0],
            _handlers(operation_name.split(".", 1)[0]),
            operation_specs=specs,
        )


@pytest.mark.parametrize(
    "passthrough",
    [
        pytest.param(None, id="null"),
        pytest.param(False, id="false"),
        pytest.param("opaque-argv", id="wrong-type-string"),
        pytest.param([], id="wrong-type-list"),
        pytest.param({}, id="empty"),
        pytest.param({"mode": "opaque-argv"}, id="incomplete"),
        pytest.param(
            {
                "mode": "typed-options",
                "allow_extra_args": True,
                "ignore_unknown_options": True,
            },
            id="unsupported-mode",
        ),
        pytest.param(
            {
                "mode": "opaque-argv",
                "allow_extra_args": False,
                "ignore_unknown_options": True,
            },
            id="disabled-extra-args",
        ),
        pytest.param(
            {
                "mode": "opaque-argv",
                "allow_extra_args": True,
                "ignore_unknown_options": "true",
            },
            id="non-boolean-ignore-unknown",
        ),
        pytest.param(
            {
                "mode": "opaque-argv",
                "allow_extra_args": 1,
                "ignore_unknown_options": True,
            },
            id="integer-extra-args",
        ),
    ],
)
def test_generator_rejects_invalid_list_passthrough_modes(passthrough: object) -> None:
    specs = list(operations())
    index = next(i for i, operation in enumerate(specs) if operation.name == "work.list")
    surfaces = deepcopy(specs[index].surfaces)
    surfaces["cli"]["passthrough"] = passthrough
    specs[index] = replace(specs[index], surfaces=surfaces)

    with pytest.raises(CatalogProjectionError, match="naming convention 2"):
        project_cli_group(typer.Typer(), "work", _handlers("work"), operation_specs=specs)


def test_generator_rejects_non_boolean_as_json_through_projection() -> None:
    specs = list(operations())
    index = next(i for i, operation in enumerate(specs) if operation.name == "work.show")
    parameters = tuple(
        replace(parameter, schema={"type": "string"}) if parameter.name == "as_json" else parameter
        for parameter in specs[index].parameters
    )
    specs[index] = replace(specs[index], parameters=parameters)
    with pytest.raises(CatalogProjectionError, match="naming convention 3"):
        project_cli_group(typer.Typer(), "work", _handlers("work"), operation_specs=specs)


def test_generator_rejects_undeclared_format_through_projection() -> None:
    def bad_brief(
        bead: str = typer.Argument(...),
        hive: str = typer.Option("", "--hive"),
        format_: str = typer.Option("", "--format"),
    ) -> None:
        pass

    handlers = _handlers("work")
    handlers["work.brief"] = bad_brief
    with pytest.raises(CatalogProjectionError, match="naming convention 3"):
        project_cli_group(typer.Typer(), "work", handlers)


def test_generator_rejects_short_hive_flags() -> None:
    def bad_brief(
        bead: str = typer.Argument(...),
        hive: str = typer.Option("", "--hive", "-r"),
    ) -> None:
        pass

    handlers = _handlers("work")
    handlers["work.brief"] = bad_brief
    with pytest.raises(CatalogProjectionError, match="naming convention 4"):
        project_cli_group(typer.Typer(), "work", handlers)


def test_generator_rejects_missing_hive_scope() -> None:
    def bad_brief(bead: str = typer.Argument(...)) -> None:
        pass

    handlers = _handlers("work")
    handlers["work.brief"] = bad_brief
    with pytest.raises(CatalogProjectionError, match="naming convention 4"):
        project_cli_group(typer.Typer(), "work", handlers)


def test_generator_refuses_a_dropped_trace_wrapper(monkeypatch) -> None:
    monkeypatch.setattr("beadhive.cli_projection.otel.trace_verb", lambda _name: lambda fn: fn)
    with pytest.raises(CatalogProjectionError, match="naming convention 5"):
        project_cli_group(typer.Typer(), "plan", _handlers("plan"))


def test_generator_rejects_handler_docstring_residue_through_projection() -> None:
    handler = work.CLI_HANDLERS["work.brief"]

    @wraps(handler)
    def legacy_handler(*args, **kwargs):
        return handler(*args, **kwargs)

    legacy_handler.__doc__ = "Legacy ws handler exposed on the generated surface."
    handlers = _handlers("work")
    handlers["work.brief"] = legacy_handler
    with pytest.raises(CatalogProjectionError, match="naming convention 8"):
        project_cli_group(typer.Typer(), "work", handlers)


def test_canonical_json_and_hive_bindings_are_generated() -> None:
    for group in (work.app, plan.app):
        for command in get_command(group).commands.values():
            for parameter in command.params:
                opts = [
                    *getattr(parameter, "opts", ()),
                    *getattr(parameter, "secondary_opts", ()),
                ]
                if "--json" in opts:
                    assert parameter.name == "as_json"
                    assert opts == ["--json"]
                if "--hive" in opts:
                    assert parameter.name == "hive"
                    assert opts == ["--hive"]


def test_compatibility_parameter_adapters_leave_core_handlers_callable() -> None:
    # Catalog generation canonicalizes CLI parameter names without changing the historical
    # Python behavior-source signatures used by internal callers.
    assert "issue_type" in str(inspect.signature(work.CLI_HANDLERS["work.accept"].handler))
    assert "json_out" in str(inspect.signature(work.CLI_HANDLERS["work.show"].handler))
    generated = get_command(work.app).commands
    assert "type_" in {parameter.name for parameter in generated["accept"].params}
    assert "as_json" in {parameter.name for parameter in generated["show"].params}
