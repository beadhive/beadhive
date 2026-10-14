"""Universal pytest registration with no environment or runtime initialization.

The watchdog is runner infrastructure and is always present.  Stateful fixtures are a
compatibility layer for the native suite: a selective sandbox opts out by excluding the
``stateful_fixtures`` module from its declared inputs.  Decide that topology here, while pytest
is importing its root conftest, so plugin hooks and xdist workers observe the same registration.
"""

from __future__ import annotations

import os
from importlib.util import find_spec

import pytest

_plugins = [
    "harness.watchdog_diagnostics",
    "harness.perf_marker",
    "harness.schedule_telemetry",
    "harness.worksteal_pack",
    "harness.collection_prefilter",
]
if find_spec("stateful_fixtures") is not None:  # pants: no-infer-dep
    _plugins.append("stateful_fixtures")
pytest_plugins = tuple(_plugins)


@pytest.fixture(autouse=True)
def _outer_broker_environment_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Root tests never depend on whether the outer gate ran under slots or the local engine:
    the broker's engine selector, lease and every grant variable are removed from the process
    (a test that needs one sets it itself)."""
    for name in list(os.environ):
        if name.startswith("BH_GRANT_") or name in {
            "BH_VALIDATION_BROKER_ENGINE",
            "BH_VALIDATION_LEASE",
        }:
            monkeypatch.delenv(name, raising=False)


#: The signed-HQ backend fixture (real SSH-signed pre-receive guard, git broker process and frame
#: sandbox). The process boundary is what its tests prove, so they run in the integration lane
#: (bh-100df). Matched by defining module, since ``backend`` is also a parametrize name elsewhere;
#: the module is imported both as ``tests.<name>`` and ``<name>``.
_SIGNED_HQ_BACKEND_MODULE = "test_hq_authority_backend"


def _requests_signed_hq_backend(item: pytest.Item) -> bool:
    info = getattr(item, "_fixtureinfo", None)
    defs = info.name2fixturedefs.get("backend") if info is not None else None
    if not defs:
        return False
    module = getattr(defs[-1].func, "__module__", "")
    return module.rpartition(".")[2] == _SIGNED_HQ_BACKEND_MODULE


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if _requests_signed_hq_backend(item):
            item.add_marker(pytest.mark.integration)
