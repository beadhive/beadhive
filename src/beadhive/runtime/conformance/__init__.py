"""``beadhive.runtime.conformance``: the contract kit for third-party runtimes
(gas-city-runtime-seam-adr.md Decisions 4 and 6).

Passing the kit is the GO condition for documenting a runtime as supported; a failure names the
ADR invariant it broke and whether it is GC-fixable (fix and re-run) or structural (Mode 2).

Invocation, from any package that depends on ``beadhive`` (no BeadHive fixtures needed):

pytest::

    from beadhive.runtime.conformance import RuntimeConformanceSuite, RuntimeEnv

    class TestMyRuntime(RuntimeConformanceSuite):
        def make_runtime(self, env: RuntimeEnv):
            return MyRuntime(seat_command=env.seat_command)

command line::

    python -m beadhive.runtime.conformance my_pkg.runtime:make_runtime

where the factory takes a :class:`RuntimeEnv` and returns a ``Runtime``. ``env.seat_command`` is
a ``work.dispatch.seat_command`` template for a stand-in seat: the runtime must launch it for
``schedule()`` with the settled ``bh-<role>`` argv. ``env.hive`` is a throwaway
:class:`DisposableHive`: a :class:`BdHive` (a real scratch beads store) by default when ``bd``
is on PATH, so a runtime that writes lifecycle state through ``bd`` is caught by
``no-lifecycle-writes``. A :class:`FileHive` cannot see such writes; a verdict obtained on one
prints ``GO (FileHive: bd-level lifecycle writes NOT exercised)``. See docs/WORK.md for the
checks and what each invariant means.
"""

from .bdhive import BdHive, BdHiveUnavailable, default_hive
from .checks import (
    CHECKS,
    GC_FIXABLE,
    INVARIANTS,
    STRUCTURAL,
    ConformanceFailure,
    Invariant,
    RuntimeEnv,
    RuntimeFactory,
)
from .hive import DisposableHive, FileHive
from .runner import CheckResult, run_conformance, verdict
from .state import BeadState
from .suite import RuntimeConformanceSuite

__all__ = [
    "CHECKS",
    "GC_FIXABLE",
    "INVARIANTS",
    "STRUCTURAL",
    "BdHive",
    "BdHiveUnavailable",
    "BeadState",
    "CheckResult",
    "ConformanceFailure",
    "DisposableHive",
    "FileHive",
    "Invariant",
    "RuntimeConformanceSuite",
    "RuntimeEnv",
    "RuntimeFactory",
    "default_hive",
    "run_conformance",
    "verdict",
]
