"""Thin subprocess helpers — ws delegates all heavy lifting to other binaries."""

from __future__ import annotations

import subprocess


def run(cmd, *, check=True, capture=False, env=None, cwd=None, text_input=None, timeout=None):
    """Run a command. Returns CompletedProcess. capture=True grabs stdout/stderr as text.
    timeout (seconds) raises subprocess.TimeoutExpired so a wedged child can't block forever."""
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        env=env,
        cwd=cwd,
        input=text_input,
        timeout=timeout,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def out(cmd, **kw):
    """Run and return stdout. Raises on non-zero unless check=False is passed."""
    return run(cmd, capture=True, **kw).stdout


def ok(cmd, **kw):
    """True iff the command exits 0 (output suppressed)."""
    return run(cmd, check=False, capture=True, **kw).returncode == 0
