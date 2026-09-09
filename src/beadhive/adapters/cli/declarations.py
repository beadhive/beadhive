"""Immutable CLI declarations derived from the canonical operation catalog.

The operation catalog owns operation identity and transport eligibility.  This module turns its
CLI projection data into small typed records; it does not import application handlers or Typer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ...operation_catalog import OperationSpec, cli_parents, operations


@dataclass(frozen=True)
class CliCommandDeclaration:
    """One catalog-owned CLI path, canonical or compatibility alias."""

    operation: str
    path: str
    parameters: tuple[str, ...]
    parameter_map: Mapping[str, str]
    declared_hidden: bool
    passthrough: Mapping[str, Any] | None
    alias_of: str | None
    parameter_defaults: Mapping[str, Any]
    interactivity: Mapping[str, Any]


@dataclass(frozen=True)
class CliParentDeclaration:
    """One catalog-owned Typer group mount."""

    path: str
    declared_hidden: bool
    panel: str | None
    alias_of: str | None


@dataclass(frozen=True)
class CliTransportMechanic:
    """A callable CLI surface that deliberately is not an application operation."""

    path: str
    reason: str


# These callbacks govern process/transport behavior rather than an application use case.  Keeping
# the list exact prevents a group callback from silently escaping catalog coverage.
TRANSPORT_MECHANICS: tuple[CliTransportMechanic, ...] = (
    CliTransportMechanic(
        "<root>",
        "global routing, setup gating, migration nudges, telemetry lifecycle, version and "
        "shell-completion options",
    ),
    CliTransportMechanic(
        "hive sync",
        "backward-compatible default for a Typer group; catalog operations are the typed peers "
        "and remotes leaves",
    ),
)

# All 208 public leaves are eligible today.  The explicit empty set is still policy: adding an
# exclusion requires a named path and rationale in the same review as its catalog change.
PROJECTION_EXCLUSIONS: Mapping[str, str] = MappingProxyType({})


def command_declarations(
    operation_specs: Sequence[OperationSpec] | None = None,
) -> tuple[CliCommandDeclaration, ...]:
    """Return every catalog-derived CLI leaf and alias in stable path order."""

    specs = operations() if operation_specs is None else operation_specs
    result: list[CliCommandDeclaration] = []
    for operation in specs:
        projection = operation.surfaces.get("cli")
        if projection is None:
            continue
        result.append(_command(operation.name, projection, alias_of=None))
        result.extend(
            _command(operation.name, alias, alias_of=projection["path"])
            for alias in projection["aliases"]
        )
    return tuple(sorted(result, key=lambda row: row.path))


def parent_declarations() -> tuple[CliParentDeclaration, ...]:
    """Return every catalog-derived group mount in stable path order."""

    return tuple(
        CliParentDeclaration(
            path=row["path"],
            declared_hidden=bool(row["declared_hidden"]),
            panel=row["panel"],
            alias_of=row["alias_of"],
        )
        for row in cli_parents()
    )


def _command(
    operation: str,
    projection: Mapping[str, Any],
    *,
    alias_of: str | None,
) -> CliCommandDeclaration:
    passthrough = projection.get("passthrough")
    return CliCommandDeclaration(
        operation=operation,
        path=str(projection["path"]),
        parameters=tuple(projection["parameters"]),
        parameter_map=MappingProxyType(dict(projection.get("parameter_map", {}))),
        declared_hidden=bool(projection["declared_hidden"]),
        passthrough=(MappingProxyType(dict(passthrough)) if passthrough is not None else None),
        alias_of=alias_of,
        parameter_defaults=MappingProxyType(dict(projection.get("parameter_defaults", {}))),
        interactivity=MappingProxyType(dict(projection["interactivity"])),
    )
