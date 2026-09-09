"""Compatibility facade for the catalog-derived CLI projection adapter.

New transport composition belongs to :mod:`beadhive.adapters.cli`.  The flat import remains
because ``beadhive.work``, ``beadhive.plan``, and third-party integrations historically import
these names directly; the CLI compatibility-removal ledger owns that temporary surface.
"""

from . import otel
from .adapters.cli.projection import (
    HAND_AUTHORED_CLI_GROUPS,
    MIGRATED_CLI_GROUPS,
    CatalogProjectionError,
    HandlerBinding,
    ProjectionReport,
    bind_handler,
    catalog_cli_groups,
    generated_callbacks,
    validate_catalog_generation_rules,
    validate_migration_inventory,
)
from .adapters.cli.projection import (
    project_cli_group as _project_cli_group,
)


def project_cli_group(*args, **kwargs):
    """Preserve the historical telemetry patch seam around the explicit adapter port."""
    return _project_cli_group(*args, trace_verb=otel.trace_verb, **kwargs)


__all__ = [
    "HAND_AUTHORED_CLI_GROUPS",
    "MIGRATED_CLI_GROUPS",
    "CatalogProjectionError",
    "HandlerBinding",
    "ProjectionReport",
    "bind_handler",
    "catalog_cli_groups",
    "generated_callbacks",
    "project_cli_group",
    "validate_catalog_generation_rules",
    "validate_migration_inventory",
    "otel",
]
