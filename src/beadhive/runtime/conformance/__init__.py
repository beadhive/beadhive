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

The opt-in loop probe (:mod:`.loop`; ``--loop``, ``run_conformance(..., loop=True)`` or
``loop_probe = True`` on the suite) also drives one turn through BeadHive's real dispatcher loop
over the runtime, in a real scratch bd store. A fault it finds in BeadHive's own loop is classed
``bh-side`` (:data:`BH_SIDE`), never GC-fixable.
"""

from typing import TYPE_CHECKING, Any

from .bdhive import BdHive, BdHiveUnavailable, default_hive
from .checks import (
    BH_SIDE,
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
from .loop import LOOP_CHECKS
from .runner import CheckResult, run_conformance, verdict
from .state import BeadState

if TYPE_CHECKING:
    from .suite import RuntimeConformanceSuite

__all__ = [
    "BH_SIDE",
    "CHECKS",
    "GC_FIXABLE",
    "INVARIANTS",
    "LOOP_CHECKS",
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


def __getattr__(name: str) -> Any:
    """Import the pytest-bound suite on first access (PEP 562), so the kit runs without pytest."""
    if name == "RuntimeConformanceSuite":
        from .suite import RuntimeConformanceSuite

        return RuntimeConformanceSuite
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
