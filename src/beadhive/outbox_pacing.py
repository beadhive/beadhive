"""The outbox write pacer's runtime seam (``bh-eqtxv``): its clocks, its wait and its ledger.

A deliberately tiny module (standard library only at import), so the test suite can patch
:func:`runtime` for EVERY test without importing the intake composition root, and a lazily
imported ``intake_services`` in a test body still never sleeps for the pace or writes the real
host-level ledger. :func:`beadhive.intake_services.pacing_runtime` reads it on every call.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["PacingRuntime", "runtime"]


@dataclass(frozen=True)
class PacingRuntime:
    """What the write pacer runs on: the monotonic clock its spacing uses, the wall clock its
    ledger entries use, its wait (``wait(seconds, stop)``: up to ``seconds``, cut short only when
    ``stop`` is given and set) and the ledger of a GitHub API host."""

    monotonic: Callable[[], float]
    wall: Callable[[], float]
    wait: Callable[[float, Any], None]
    ledger: Callable[[str], Any]


def _real_wait(seconds: float, stop: Any) -> None:
    """The pacer's real wait, measured as the ``pacer_wait`` phase of a ``--json`` run."""
    from . import phase_timings

    with phase_timings.phase("pacer_wait"):
        if stop is not None:
            stop.wait(seconds)  # a stop (the first SIGINT) ends it at once
        elif seconds > 0:
            time.sleep(seconds)  # mid-operation: never cut short, the operation drains


def _file_ledger(api_host: str) -> Any:
    from .adapters.intake.write_ledger import FileWriteLedger, ledger_directory

    return FileWriteLedger(ledger_directory, api_host)


def runtime() -> PacingRuntime:
    """The real clocks, wait and host-level file ledger (``$XDG_STATE_HOME/beadhive/
    github-writes/<api host>.ledger``)."""
    return PacingRuntime(time.monotonic, time.time, _real_wait, _file_ledger)
