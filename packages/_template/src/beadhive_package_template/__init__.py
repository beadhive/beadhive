"""Template for an in-repo ``packages/*`` distribution.

Copy ``packages/_template`` to ``packages/<name>``, rename this import package, and run
``uv lock``. Nothing in ``src/beadhive`` imports a package statically; first-party plugins are
named as data in the built-in catalog and resolved lazily at bootstrap.
"""

from __future__ import annotations

DISTRIBUTION = "beadhive-package-template"


def distribution_name() -> str:
    """Return this package's distribution name (a placeholder public surface)."""
    return DISTRIBUTION


__all__ = ["DISTRIBUTION", "distribution_name"]
