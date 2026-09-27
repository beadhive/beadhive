"""Template for an in-repo ``packages/*`` distribution.

Copy ``packages/_template`` to ``packages/<name>``, rename this import package, and run
``uv lock``. This template is a **library package by default**: it carries no ``plugin.json``
and depends on nothing from ``beadhive``, so root and other packages may import its public
``__all__`` surface statically. To make a copy a **plugin package** instead, add a
``plugin.json`` manifest under ``src/<import_name>/``, add its entry to the built-in catalog,
and add ``beadhive`` to the copied ``pyproject.toml``'s dependencies — root then resolves it
lazily, by name, only after manifest selection. See
``docs/design/package-class-library-vs-plugin-adr.md`` for the two classes' full rules.
"""

from __future__ import annotations

DISTRIBUTION = "beadhive-package-template"


def distribution_name() -> str:
    """Return this package's distribution name (a placeholder public surface)."""
    return DISTRIBUTION


__all__ = ["DISTRIBUTION", "distribution_name"]
