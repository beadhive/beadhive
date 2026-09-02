"""Generate an assembled Typer tree from catalog declarations and bound handlers.

Application modules may retain their historical decorators while their compatibility facades are
being removed.  At the composition boundary those decorators are treated only as handler and
presentation bindings: this adapter validates the complete assembled tree, clears the provisional
registrations, and re-registers every leaf and group from catalog-derived declarations.  Handler
signatures keep flags, prompts, rendering, JSON envelopes and exit behavior byte-compatible.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import typer
from typer.main import get_command
from typer.models import CommandInfo, TyperInfo

from ...operation_catalog import OperationSpec
from .declarations import (
    PROJECTION_EXCLUSIONS,
    TRANSPORT_MECHANICS,
    CliCommandDeclaration,
    CliParentDeclaration,
    command_declarations,
    parent_declarations,
)
from .projection import CatalogProjectionError, validate_catalog_generation_rules


@dataclass(frozen=True)
class CliTreeProjectionReport:
    """Deterministic attestation of the generated root Typer projection."""

    operations: tuple[str, ...]
    paths: tuple[str, ...]
    parents: tuple[str, ...]
    transport_mechanics: tuple[str, ...]
    exclusions: tuple[str, ...]
    unavailable_optional_plugins: tuple[str, ...]
    digest: str


@dataclass(frozen=True)
class _CommandBinding:
    app: typer.Typer
    info: CommandInfo
    declaration: CliCommandDeclaration


@dataclass(frozen=True)
class _ParentBinding:
    app: typer.Typer
    info: TyperInfo
    declaration: CliParentDeclaration


def _collect(
    app: typer.Typer,
    commands: dict[str, tuple[typer.Typer, CommandInfo]],
    parents: dict[str, tuple[typer.Typer, TyperInfo]],
    callbacks: set[str],
    path: tuple[str, ...] = (),
) -> None:
    if app.registered_callback is not None:
        callbacks.add(" ".join(path) or "<root>")
    for info in app.registered_commands:
        if not isinstance(info.name, str) or not info.name:
            raise CatalogProjectionError(
                f"CLI compatibility binding at {' '.join(path) or '<root>'} has no explicit name"
            )
        command_path = " ".join((*path, info.name))
        if command_path in commands:
            raise CatalogProjectionError(f"duplicate CLI handler binding for {command_path!r}")
        commands[command_path] = (app, info)
    for info in app.registered_groups:
        if not isinstance(info.name, str) or not info.name:
            raise CatalogProjectionError(
                f"CLI compatibility group at {' '.join(path) or '<root>'} has no explicit name"
            )
        parent_path = " ".join((*path, info.name))
        if parent_path in parents:
            raise CatalogProjectionError(f"duplicate CLI parent binding for {parent_path!r}")
        parents[parent_path] = (app, info)
        _collect(info.typer_instance, commands, parents, callbacks, (*path, info.name))


def _command_context(declaration: CliCommandDeclaration) -> dict[str, Any] | None:
    if declaration.passthrough is None:
        return None
    return {
        "allow_extra_args": declaration.passthrough["allow_extra_args"],
        "ignore_unknown_options": declaration.passthrough["ignore_unknown_options"],
    }


def _rebind_commands(bindings: tuple[_CommandBinding, ...]) -> None:
    by_app: dict[int, tuple[typer.Typer, list[_CommandBinding]]] = {}
    for binding in bindings:
        rows = by_app.setdefault(id(binding.app), (binding.app, []))[1]
        rows.append(binding)
    for app, rows in by_app.values():
        # A Typer app can be mounted twice (``worktree``/``wt``).  Both paths bind the same
        # callbacks; keep one stable registration sequence and require their declaration-owned
        # local metadata to agree.
        unique: dict[int, _CommandBinding] = {}
        for row in rows:
            existing = unique.get(id(row.info))
            if existing is not None:
                left = existing.declaration
                right = row.declaration
                if (
                    left.path.rsplit(" ", 1)[-1],
                    left.declared_hidden,
                    left.passthrough,
                ) != (
                    right.path.rsplit(" ", 1)[-1],
                    right.declared_hidden,
                    right.passthrough,
                ):
                    raise CatalogProjectionError(
                        f"shared Typer binding differs across {left.path!r} and {right.path!r}"
                    )
                continue
            unique[id(row.info)] = row
        app.registered_commands.clear()
        for row in unique.values():
            info = row.info
            declaration = row.declaration
            app.command(
                name=declaration.path.rsplit(" ", 1)[-1],
                cls=info.cls,
                context_settings=_command_context(declaration),
                help=info.help,
                epilog=info.epilog,
                short_help=info.short_help,
                options_metavar=info.options_metavar,
                add_help_option=info.add_help_option,
                no_args_is_help=info.no_args_is_help,
                hidden=declaration.declared_hidden,
                deprecated=info.deprecated,
                rich_help_panel=info.rich_help_panel,
            )(info.callback)


def _rebind_parents(bindings: tuple[_ParentBinding, ...]) -> None:
    by_app: dict[int, tuple[typer.Typer, list[_ParentBinding]]] = {}
    for binding in bindings:
        rows = by_app.setdefault(id(binding.app), (binding.app, []))[1]
        rows.append(binding)
    for app, rows in by_app.values():
        app.registered_groups.clear()
        for row in rows:
            info = row.info
            declaration = row.declaration
            app.add_typer(
                info.typer_instance,
                name=declaration.path.rsplit(" ", 1)[-1],
                cls=info.cls,
                invoke_without_command=info.invoke_without_command,
                no_args_is_help=info.no_args_is_help,
                subcommand_metavar=info.subcommand_metavar,
                chain=info.chain,
                result_callback=info.result_callback,
                context_settings=info.context_settings,
                callback=info.callback,
                help=info.help,
                epilog=info.epilog,
                short_help=info.short_help,
                options_metavar=info.options_metavar,
                add_help_option=info.add_help_option,
                hidden=declaration.declared_hidden,
                deprecated=info.deprecated,
                rich_help_panel=declaration.panel,
            )


def _validate_live_parameters(
    app: typer.Typer, declarations: dict[str, CliCommandDeclaration]
) -> None:
    def walk(command, path: tuple[str, ...] = ()) -> None:
        for name, child in command.commands.items():
            child_path = (*path, name)
            if getattr(child, "commands", None) is not None:
                walk(child, child_path)
                continue
            rendered = " ".join(child_path)
            declaration = declarations[rendered]
            actual = tuple(parameter.name for parameter in child.params)
            if actual != declaration.parameters:
                raise CatalogProjectionError(
                    f"{rendered} handler signature differs from catalog "
                    f"({actual!r} != {declaration.parameters!r})"
                )

    walk(get_command(app))


def project_cli_tree(
    app: typer.Typer,
    *,
    operation_specs: tuple[OperationSpec, ...] | None = None,
    unavailable_optional_plugins: frozenset[str] = frozenset(),
) -> CliTreeProjectionReport:
    """Re-register an assembled Typer tree from canonical catalog declarations.

    The supplied ``app`` is mutated in place so historical module-level app identities and
    monkeypatch seams remain valid during the compatibility window.
    """

    validate_catalog_generation_rules(operation_specs)
    command_rows = command_declarations(operation_specs)
    parent_rows = parent_declarations()
    declared_commands = {row.path: row for row in command_rows}
    declared_parents = {row.path: row for row in parent_rows}

    commands: dict[str, tuple[typer.Typer, CommandInfo]] = {}
    parents: dict[str, tuple[typer.Typer, TyperInfo]] = {}
    callbacks: set[str] = set()
    _collect(app, commands, parents, callbacks)

    def is_unavailable(path: str) -> bool:
        parts = path.split()
        return len(parts) >= 2 and parts[0] == "plugin" and parts[1] in unavailable_optional_plugins

    expected_commands = {
        path
        for path in set(declared_commands) - set(PROJECTION_EXCLUSIONS)
        if not is_unavailable(path)
    }
    if set(commands) != expected_commands:
        raise CatalogProjectionError(
            "CLI handler inventory drift "
            f"(missing={sorted(expected_commands - set(commands))!r}, "
            f"extra={sorted(set(commands) - expected_commands)!r})"
        )
    expected_parents = {path for path in declared_parents if not is_unavailable(path)}
    if set(parents) != expected_parents:
        raise CatalogProjectionError(
            "CLI parent inventory drift "
            f"(missing={sorted(expected_parents - set(parents))!r}, "
            f"extra={sorted(set(parents) - expected_parents)!r})"
        )
    mechanics = {row.path for row in TRANSPORT_MECHANICS}
    if callbacks != mechanics:
        raise CatalogProjectionError(
            "CLI transport mechanic drift "
            f"(missing={sorted(mechanics - callbacks)!r}, extra={sorted(callbacks - mechanics)!r})"
        )

    command_bindings = tuple(
        _CommandBinding(*commands[path], declared_commands[path]) for path in commands
    )
    parent_bindings = tuple(
        _ParentBinding(*parents[path], declared_parents[path]) for path in parents
    )
    _rebind_commands(command_bindings)
    _rebind_parents(parent_bindings)
    _validate_live_parameters(app, declared_commands)

    projected_commands = tuple(row for row in command_rows if row.path in expected_commands)
    projected_parents = tuple(row for row in parent_rows if row.path in expected_parents)
    material = {
        "commands": [
            {
                "operation": row.operation,
                "path": row.path,
                "parameters": row.parameters,
                "hidden": row.declared_hidden,
                "passthrough": dict(row.passthrough) if row.passthrough else None,
                "alias_of": row.alias_of,
            }
            for row in projected_commands
        ],
        "parents": [
            {
                "path": row.path,
                "hidden": row.declared_hidden,
                "panel": row.panel,
                "alias_of": row.alias_of,
            }
            for row in projected_parents
        ],
        "transport_mechanics": [row.path for row in TRANSPORT_MECHANICS],
        "exclusions": dict(PROJECTION_EXCLUSIONS),
        "unavailable_optional_plugins": sorted(unavailable_optional_plugins),
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return CliTreeProjectionReport(
        operations=tuple(row.operation for row in projected_commands),
        paths=tuple(row.path for row in projected_commands),
        parents=tuple(row.path for row in projected_parents),
        transport_mechanics=tuple(row.path for row in TRANSPORT_MECHANICS),
        exclusions=tuple(PROJECTION_EXCLUSIONS),
        unavailable_optional_plugins=tuple(sorted(unavailable_optional_plugins)),
        digest=digest,
    )
