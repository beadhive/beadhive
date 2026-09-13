"""Universal pytest registration with no environment or runtime initialization.

The watchdog is runner infrastructure and is always present.  Stateful fixtures are a
compatibility layer for the native suite: a selective sandbox opts out by excluding the
``stateful_fixtures`` module from its declared inputs.  Decide that topology here, while pytest
is importing its root conftest, so plugin hooks and xdist workers observe the same registration.
"""

from __future__ import annotations

from importlib.util import find_spec

_plugins = ["harness.watchdog_diagnostics"]
if find_spec("stateful_fixtures") is not None:  # pants: no-infer-dep
    _plugins.append("stateful_fixtures")
pytest_plugins = tuple(_plugins)
