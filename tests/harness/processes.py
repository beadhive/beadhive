"""One process-start policy for the test harness.

``spawn`` is the ordinary policy on every platform.  It never clones pytest-xdist's
multithreaded worker and forces process tests to use importable top-level targets plus
serializable inputs.  A test that truly needs POSIX fork semantics must call
``isolated_fork_context`` and run outside xdist; the normal gate has a separate serial phase for
that reviewed marker.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import threading
from multiprocessing.context import BaseContext


class UnsafeProcessStart(RuntimeError):
    """The requested process start mode is unsafe in the current test runner."""


def process_context() -> BaseContext:
    """Return the cross-platform context for ordinary real-process tests."""
    return multiprocessing.get_context("spawn")


def isolated_fork_context() -> BaseContext:
    """Return POSIX fork only for a reviewed test running outside pytest-xdist."""
    if os.environ.get("PYTEST_XDIST_WORKER"):
        raise UnsafeProcessStart("fork-isolated tests must not run inside a pytest-xdist worker")
    if threading.active_count() != 1:
        raise UnsafeProcessStart("fork-isolated tests require a single-threaded parent process")
    if sys.platform not in {"darwin", "linux"}:
        raise UnsafeProcessStart(f"fork-isolated tests are unsupported on {sys.platform}")
    return multiprocessing.get_context("fork")
