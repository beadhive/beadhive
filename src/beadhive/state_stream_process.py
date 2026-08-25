"""Lifetime ownership for host-local state-stream backend processes.

The polling adapter is synchronous, but the CLI around it is long-lived.  A backend export can
therefore be interrupted in four ordinary ways: its own timeout, Ctrl-C, SIGTERM, or an output
consumer closing its pipe.  This scope makes those paths converge on one invariant: every active
backend command and every descendant in its process group is sent SIGTERM, then SIGKILL if it
does not leave promptly.  SIGINT is never forwarded to a child (the repository-wide seat signal
policy); it remains the caller's normal cancellation signal.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from types import FrameType
from typing import Any

from . import run as run_mod

DEFAULT_EXPORT_TIMEOUT = 30.0
DEFAULT_TERM_GRACE = 2.0


@dataclass(frozen=True)
class _ActiveProcess:
    proc: subprocess.Popen
    pgid: int


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_trees(active_processes: tuple[_ActiveProcess, ...], grace: float) -> None:
    """Terminate remembered process groups under one shared, count-independent deadline.

    Remembering ``pgid`` at spawn is essential: resolving it from ``proc.pid`` during cleanup
    fails after a short-lived direct child exits while a grandchild still holds an inherited
    output pipe open.
    """

    for active in active_processes:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(active.pgid, signal.SIGTERM)

    deadline = time.monotonic() + max(0.0, grace)
    while (
        any(_group_exists(active.pgid) for active in active_processes)
        and time.monotonic() < deadline
    ):
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

    for active in active_processes:
        if _group_exists(active.pgid):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(active.pgid, signal.SIGKILL)
    reap_deadline = time.monotonic() + max(0.001, grace)
    for active in active_processes:
        remaining = max(0.0, reap_deadline - time.monotonic())
        with contextlib.suppress(subprocess.TimeoutExpired):
            active.proc.wait(timeout=remaining)


def _terminate_tree(active: _ActiveProcess, grace: float) -> None:
    _terminate_trees((active,), grace)


class StreamProcessScope:
    """Context/finalizer owning every backend process used by one stream command.

    Use the scope around frame iteration, not merely around one refresh.  ``__exit__`` then also
    covers ``BrokenPipeError`` and generator cancellation raised by the output layer.  On the
    main thread the scope temporarily handles SIGTERM and SIGINT so an in-flight backend tree is
    reaped before the original signal behavior is resumed.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_EXPORT_TIMEOUT,
        term_grace: float = DEFAULT_TERM_GRACE,
    ) -> None:
        self.timeout = timeout
        self.term_grace = term_grace
        self._lock = threading.RLock()
        self._active: dict[int, _ActiveProcess] = {}
        self._previous_handlers: dict[signal.Signals, Any] = {}
        self._entered = False
        self._closed = False

    def __enter__(self) -> StreamProcessScope:
        if self._entered:
            raise RuntimeError("a StreamProcessScope cannot be entered twice")
        self._entered = True
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                self._previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._handle_signal)
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()
        for sig, previous in self._previous_handlers.items():
            signal.signal(sig, previous)
        self._previous_handlers.clear()
        self._entered = False

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        sig = signal.Signals(signum)
        previous = self._previous_handlers.get(sig, signal.SIG_DFL)
        self.close()

        if previous is signal.SIG_IGN:
            return
        if callable(previous):
            previous(signum, frame)
            return

        # Restore default disposition and re-deliver the signal.  This preserves the native
        # shell-visible SIGTERM/SIGINT status instead of converting cancellation into success.
        signal.signal(sig, signal.SIG_DFL)
        os.kill(os.getpid(), sig)

    def close(self) -> None:
        """Reap every process tree still owned by the stream; safe to call repeatedly."""

        with self._lock:
            self._closed = True
            active = tuple(self._active.values())
            self._active.clear()
        _terminate_trees(active, self.term_grace)

    def run(self, cmd: list[str], *, label: str = "state stream backend"):
        """Run one captured backend command under the scope's timeout and tree ownership."""

        with run_mod._span(cmd):
            with self._lock:
                if self._closed:
                    raise RuntimeError("the state stream process scope is closed")
                try:
                    proc = subprocess.Popen(
                        cmd,
                        text=True,
                        env=run_mod.child_env(),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        preexec_fn=run_mod._die_with_parent,
                    )
                except FileNotFoundError:
                    binary = cmd[0] if cmd else "?"
                    result = subprocess.CompletedProcess(
                        cmd,
                        run_mod.MISSING_BINARY_EXIT,
                        "",
                        f"{binary}: command not found",
                    )
                    result.bh_missing_binary = binary
                    return result

                active = _ActiveProcess(proc=proc, pgid=proc.pid)
                self._active[proc.pid] = active
            try:
                stdout, stderr = proc.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                _terminate_tree(active, self.term_grace)
                with self._lock:
                    self._active.pop(active.pgid, None)
                raise run_mod.ChildTimeout(
                    f"{label} exceeded {self.timeout:g}s and was TERMINATED "
                    f"(pid {proc.pid}; its whole process group was reaped)"
                ) from None
            except BaseException:
                # KeyboardInterrupt, generator cancellation, and caller exceptions all have the
                # same ownership obligation.  Never translate them: their exit semantics belong
                # to the caller.
                _terminate_tree(active, self.term_grace)
                with self._lock:
                    self._active.pop(active.pgid, None)
                raise
            # A successful direct child can still leave a descendant in its process group after
            # closing the captured pipes.  Keep ownership of that group until scope finalization;
            # dropping it here would make an ordinary successful refresh leak the descendant.
            if not _group_exists(active.pgid):
                with self._lock:
                    self._active.pop(active.pgid, None)
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

    def export_jsonl(self, backend, cwd, out_path):
        """Run an engine's explicit stream export command under this scope.

        Failing closed is intentional: a backend used by the host-local stream must explicitly
        expose an argv that this process owner can supervise.  Falling back to an opaque method
        would reintroduce the exact unverified descendant lifetime this bead removes.
        """

        command = getattr(backend, "stream_export_command", None)
        if command is None:
            raise TypeError(
                f"backend {getattr(backend, 'name', type(backend).__name__)!r} does not expose "
                "a supervised stream export command"
            )
        return self.run(
            list(command(cwd, out_path)),
            label=f"{getattr(backend, 'name', 'backend')} stream export against {cwd}",
        )
