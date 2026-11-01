"""The injected command seam shared by the history readers.

A :data:`Runner` takes an argv and returns the command's stdout; tests pass a fake, production
uses :func:`subprocess_runner`. A reader makes a fixed, small number of runner calls for a whole
hive, so a counting fake asserts the store cost without any timing.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence

Runner = Callable[[Sequence[str]], str]

# Generous default for the one bulk read (the real ``events`` query takes ~13 s for ~36k rows).
DEFAULT_TIMEOUT_SECONDS = 300.0


def subprocess_runner(
    timeout: float | None, error: type[Exception], label: str = "command"
) -> Runner:
    """A :data:`Runner` that spawns ``argv`` with a timeout, raising ``error`` on any failure.

    ``timeout`` is seconds (``None`` disables it); a non-zero exit, a missing binary and a timeout
    each raise ``error`` with a one-line reason, never a traceback of the subprocess internals.
    """

    def run(argv: Sequence[str]) -> str:
        try:
            result = subprocess.run(
                list(argv), capture_output=True, text=True, check=False, timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise error(f"{label} timed out after {timeout:g}s") from exc
        except OSError as exc:
            raise error(f"cannot run {argv[0]}: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise error(f"{label} failed ({result.returncode}): {detail[-1] if detail else ''}")
        return result.stdout

    return run


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "Runner", "subprocess_runner"]
