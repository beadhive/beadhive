"""Opt-in worksteal chunk packing of the selected tests (bh-xqtp8); see ``duration_order.pack``.

``BH_TEST_WORKSTEAL_PACK=1`` and an xdist ``--dist worksteal`` worker: re-lay the collection as
worksteal's initial per-worker chunks, each a balanced longest-processing-time bin, Dolt-slot
aware against ``BH_DOLT_SLOTS``. Anything else leaves the order exactly as the other hooks made it.

``trylast`` so it orders the SELECTED items: ``-m`` / ``-k`` deselection is itself a
``pytest_collection_modifyitems`` hook, and packing must size its chunks for what xdist will
actually schedule. It runs after ``stateful_fixtures``' longest-first sort and Dolt interleave and
replaces that order wholesale when it applies.
"""

from __future__ import annotations

import os

import pytest

from harness import duration_order

#: The controller's ``--dist`` mode, exported for its workers. A worker cannot read it from its own
#: options: xdist's ``remote.setup_config`` resets ``config.option.dist`` to ``"no"`` on every
#: worker (measured: the first packed run was silently a plain longest-first run because of it).
DIST_ENV = "BH_TEST_XDIST_DIST"


def worksteal_workers(config, env=None) -> int:
    """The worker count when this process is an xdist worker of a ``worksteal`` run, else 0."""
    workerinput = getattr(config, "workerinput", None)
    if not workerinput:
        return 0
    if (os.environ if env is None else env).get(DIST_ENV) != "worksteal":
        return 0
    return int(workerinput.get("workercount", 0))


def pytest_configure(config) -> None:
    """Controller only: publish ``--dist`` to the workers, which inherit the environment."""
    if hasattr(config, "workerinput") or not duration_order.pack_enabled():
        return
    os.environ[DIST_ENV] = str(config.getoption("dist", "no"))


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items) -> None:
    if not (duration_order.pack_enabled() and duration_order.enabled()):
        return
    workers = worksteal_workers(config)
    if workers:
        slots = int(os.environ.get("BH_DOLT_SLOTS", "8"))
        items[:] = duration_order.apply_pack(items, workers, slots)
