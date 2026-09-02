"""Catalog-derived Typer projection adapter."""

from .projection import (
    CatalogProjectionError,
    HandlerBinding,
    ProjectionReport,
    bind_handler,
    generated_callbacks,
    project_cli_group,
)

__all__ = [
    "CatalogProjectionError",
    "HandlerBinding",
    "ProjectionReport",
    "bind_handler",
    "generated_callbacks",
    "project_cli_group",
]
