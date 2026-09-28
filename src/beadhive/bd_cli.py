"""The one place root resolves ``beadhive-bd-cli`` — lazily, by name (bh-o3xuf).

``beadhive_bd_cli`` is the library package holding every ``bd`` argv route the API-first cutover
left in this shell: the migrated cohorts' CLI-compatibility routes (``CliIssues``,
``CliMoleculeFiler``, the ``show`` / ``children`` / ``child_rows`` / ``ready`` reads) and the
operations Beads 1.3 has no API for (``CliLeases``, ``CliStateReads``, ``CliStateOperations``,
``CliGateOperations``, ``CliPlanningGates``, and the gate / merge-slot / heartbeat / reclaim
wrappers :mod:`beadhive.coordination` re-exports).

Root never imports it statically (``scripts/check_package_imports.py``): :func:`package` resolves
it with ``importlib`` exactly as :mod:`beadhive.beads_routing` resolves ``beadhive_core``. The
package only shapes argv and parses output; the process is always root's own ``bd`` invocation
seam — :func:`transport` hands the package the :mod:`beadhive.bd` module itself, whose ``run`` /
``json`` route through the configured ``Engine`` (``-C <hive>`` scoping, ``--actor``, strict-read
narration). The module is looked up at call time, so a test patching ``bd.run`` / ``bd.json`` /
``bd._run`` still intercepts every package route.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from . import bd

PACKAGE = "beadhive_bd_cli"


def package() -> Any:
    """The ``beadhive_bd_cli`` package, imported by name on first use."""
    return importlib.import_module(PACKAGE)


def transport() -> Any:
    """Root's ``bd`` invocation seam as the package's ``BdTransport``: :mod:`beadhive.bd`."""
    return bd


# ---- the ports, bound to root's transport ------------------------------------------------------


def issues(main: Path) -> Any:
    """``work.issue.get`` / ``work.issue.update`` over ``bd`` (``CliIssues``)."""
    return package().CliIssues(transport(), main)


def leases(main: Path) -> Any:
    """``work.lease.acquire`` / ``release`` over ``bd`` (``CliLeases``)."""
    return package().CliLeases(transport(), main)


def state_reads(main: Path) -> Any:
    """``work.state.get`` over ``bd`` (``CliStateReads``)."""
    return package().CliStateReads(transport(), main)


def state_operations(main: Path) -> Any:
    """``work.state.update`` over ``bd`` (``CliStateOperations``)."""
    return package().CliStateOperations(transport(), main)


def gate_operations(main: Path) -> Any:
    """``work.gate.lookup`` / ``work.gate.resolve`` over ``bd`` (``CliGateOperations``)."""
    return package().CliGateOperations(transport(), main)


def molecule_filer(main: Path) -> Any:
    """``plan.batch-apply.atomic``'s CLI-compatibility route (``CliMoleculeFiler``)."""
    return package().CliMoleculeFiler(transport(), main)


def planning_gates(main: Path) -> Any:
    """``plan.gate.create`` / ``plan.kickoff.update`` over ``bd`` (``CliPlanningGates``)."""
    return package().CliPlanningGates(transport(), main)


# ---- the ready reads ---------------------------------------------------------------------------


def ready(cwd: Any, args: Any = ()) -> Any:
    """`bd ready <args>`, captured — `bh work ready`'s byte-for-byte forward."""
    return package().reads.ready(transport(), cwd, args)


def ready_rows(cwd: Any, args: Any = ()) -> list | None:
    """`bd ready <args> --json`'s rows, or None on failure — the pick/claim loop's and the local
    loop poll's CLI-compatibility read."""
    return package().reads.ready_rows(transport(), cwd, args)
