"""Per-frame remote gate for the writer-fencing fixture (bh-eybn7). Stdlib only.

Every frame in :mod:`harness.writer_fencing` gets a ``bin/git`` shim first on its ``PATH``. The
shim execs local Git plumbing straight through and hands only the remote-touching subcommands
(``fetch``/``push``/``ls-remote``/...) to this script, which runs as::

    python -I -S writer_fencing_gate.py <ctl-dir> <real-git> <git args...>

Both Dolt CLI and the Dolt that bd links in-process reach a ``git+file://`` remote by spawning
``git`` from ``PATH`` (measured: the shim's log sees every call), and so does a bd-managed
``dolt sql-server``, which inherits the env of the bd call that started it. That makes this the
one choke point every driver shares, so the fault controls below behave the same for all three.

Controls, all plain files under ``<ctl-dir>`` so the controller needs no IPC:

* ``partitioned`` exists -> the call fails like an unreachable host (exit 128). Checked AFTER any
  hold, so partitioning a frame that is parked at a barrier makes its released call fail.
* ``holds/<point>`` exists -> the first call classified as ``<point>`` claims the hold (atomic
  rename to ``held/<point>``), writes its pid to ``arrived/<point>`` and parks until the
  controller deletes ``held/<point>``. The file's integer content is a skip count: ``1`` lets the
  first matching call through and parks the second.
* every gated call is appended to ``git.jsonl`` (pid, point, argv) for the recorder and asserts.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time

#: A ``push --force-with-lease=refs/dolt/data:<old>``: Dolt's compare-and-swap of the remote
#: manifest. Parking here means the frame has read the remote head and uploaded nothing visible.
CAS = "cas"
#: A ``fetch ... refs/dolt/data``: the frame reading the remote head.
FETCH = "fetch"
#: The cosmetic ``refs/heads/__dolt_remote_info__`` force-push Dolt does after a data push.
INFO = "info"

_OPTS_WITH_VALUE = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--exec-path",
    "--super-prefix",
    "--config-env",
}
_PARK_LIMIT_SECONDS = 600.0


def subcommand(args: list[str]) -> tuple[str, list[str]]:
    """The Git subcommand and its own arguments, skipping global options and their values."""
    skip = False
    for i, arg in enumerate(args):
        if skip:
            skip = False
            continue
        if arg in _OPTS_WITH_VALUE:
            skip = True
            continue
        if arg.startswith("-"):
            continue
        return arg, args[i + 1 :]
    return "", []


def classify(args: list[str]) -> str:
    sub, rest = subcommand(args)
    if sub == "push":
        if any(a.startswith("--force-with-lease=refs/dolt/data") for a in rest):
            return CAS
        if any("__dolt_remote_info__" in a for a in rest):
            return INFO
        return "push"
    if sub == "fetch" and any("refs/dolt/data" in a for a in rest):
        return FETCH
    return sub


def _log(ctl: str, event: dict) -> None:
    line = (json.dumps(event, sort_keys=True) + "\n").encode()
    fd = os.open(os.path.join(ctl, "git.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def _claim_hold(ctl: str, point: str) -> bool:
    """Atomically take the armed hold for ``point`` (or burn one unit of its skip count)."""
    armed = os.path.join(ctl, "holds", point)
    if not os.path.exists(armed):
        return False
    with open(os.path.join(ctl, "gate.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(armed) as fh:
                skip = int(fh.read().strip() or "0")
        except FileNotFoundError:
            return False
        if skip > 0:
            with open(armed, "w") as fh:
                fh.write(str(skip - 1))
            return False
        os.replace(armed, os.path.join(ctl, "held", point))
        return True


def _park(ctl: str, point: str) -> None:
    held = os.path.join(ctl, "held", point)
    arrived = os.path.join(ctl, "arrived", point)
    tmp = arrived + f".{os.getpid()}"
    with open(tmp, "w") as fh:
        fh.write(str(os.getpid()))
    os.replace(tmp, arrived)
    deadline = time.monotonic() + _PARK_LIMIT_SECONDS
    while os.path.exists(held):
        if time.monotonic() > deadline:  # never wedge a suite on a forgotten release
            break
        time.sleep(0.01)


def main(argv: list[str]) -> int:
    ctl, real_git, *args = argv
    point = classify(args)
    _log(ctl, {"t": time.time(), "pid": os.getpid(), "point": point, "argv": args[-4:]})
    if _claim_hold(ctl, point):
        _park(ctl, point)
        _log(ctl, {"t": time.time(), "pid": os.getpid(), "point": point, "released": True})
    if os.path.exists(os.path.join(ctl, "partitioned")):
        _log(ctl, {"t": time.time(), "pid": os.getpid(), "point": point, "partitioned": True})
        sys.stderr.write(
            "fatal: unable to access remote: Could not resolve host "
            "(writer-fencing fixture partition)\n"
        )
        return 128
    os.execv(real_git, [real_git, *args])
    return 127  # pragma: no cover - execv does not return


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
