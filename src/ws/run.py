"""Thin subprocess helpers — ws delegates all heavy lifting to other binaries.

One OpenTelemetry span wraps the subprocess at this single seam, so every bd/git/dolt call ws
makes is traced when otel is on. The span is gated on ``otel.is_active()``: when telemetry is off
(the default) ``run`` is the original ``subprocess.run`` under a zero-cost ``nullcontext`` — no
span name or attributes are even built. Tests fake subprocesses by patching the per-module ``run``
they import (``ws.work.run`` etc.), which replaces this whole function, so fakes bypass the span
entirely and keep working unchanged.
"""

from __future__ import annotations

import contextlib
import os
import subprocess

from . import otel


def _tool(cmd) -> str:
    """Basename of the invoked binary (argv[0]) — the span's ``ws.subprocess.tool`` attribute."""
    if isinstance(cmd, str):
        toks = cmd.split()
        first = toks[0] if toks else ""
    else:
        first = str(cmd[0]) if cmd else ""
    return os.path.basename(first) if first else "subprocess"


def _safe_op(cmd) -> str:
    """A low-cardinality, non-secret span name: the tool plus its first subcommand (e.g.
    ``git merge``, ``bd gate``), stopping at the first flag. Anything after a flag may be a secret
    (e.g. a dolt ``--password`` value) and positional args (bead ids, paths) are high-cardinality,
    so only the two leading verb tokens land in the name — IDs belong in attributes, not here."""
    toks = cmd.split() if isinstance(cmd, str) else [str(t) for t in (cmd or [])]
    parts: list[str] = []
    for tok in toks:
        if tok.startswith("-"):
            break
        parts.append(os.path.basename(tok) if not parts else tok)
        if len(parts) >= 2:
            break
    return " ".join(parts) if parts else "subprocess"


def _span(cmd):
    """The subprocess span, or a zero-cost ``nullcontext`` when otel is off — gated so the default
    path builds no span name/attributes and stays byte-for-byte the un-instrumented original."""
    if not otel.is_active():
        return contextlib.nullcontext()
    return otel.span(_safe_op(cmd), {"ws.subprocess.tool": _tool(cmd)})


def run(cmd, *, check=True, capture=False, env=None, cwd=None, text_input=None, timeout=None):
    """Run a command. Returns CompletedProcess. capture=True grabs stdout/stderr as text.
    timeout (seconds) raises subprocess.TimeoutExpired so a wedged child can't block forever."""
    with _span(cmd):
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
