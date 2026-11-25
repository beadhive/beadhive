"""A :class:`DisposableHive` over a scratch **real beads store** (``bd init --server``).

This is the hive that makes the ``no-lifecycle-writes`` check meaningful: its :meth:`snapshot`
reads status, assignee, labels, metadata and gates out of ``bd`` itself, so a runtime that runs
``bd update --claim``, ``bd label add`` or ``bd gate resolve`` against the store is seen, where
:class:`~.hive.FileHive` (a JSON file the runtime never touches) cannot see it. Standard library
plus the ``bd`` binary; the store lives under *root* and is reaped by :meth:`close`.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .hive import FileHive
from .seat import SEAT_SCRIPT_NAME, scrubbed_env, write_outcome_bd
from .state import BeadState

__all__ = ["BdHive", "BdHiveUnavailable", "default_hive"]

ACTOR = "dev/conformance"
PREFIX = "cf"
_TIMEOUT = 120


class BdHiveUnavailable(RuntimeError):
    """``bd`` is missing or could not create a scratch store."""


class BdHive:
    """A scratch beads store plus the turn-gate and start-log directories the stand-in seat
    uses. ``store`` is the directory to pass to ``bd -C``; a test double that misbehaves by
    writing through ``bd`` directly needs it."""

    kind = "bd"

    def __init__(self, root: str | os.PathLike[str]):
        """Create the scratch store under *root* (``bd init --server``).

        bd-seam-justified: the kit is a standalone contract kit that must drive a real bd store
        with the standard library only, not through beadhive's own bd routes under test."""
        self.root = Path(root)
        self.store = self.root / "store"
        for sub in ("release", "starts"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.store.mkdir(parents=True, exist_ok=True)
        self._gates: dict[str, str] = {}
        self._closed = False
        try:
            subprocess.run(
                ["bd", "init", "--server", "--prefix", PREFIX, "--non-interactive"],
                cwd=str(self.store),
                check=True,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
                env=scrubbed_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.close()
            raise BdHiveUnavailable(f"could not create a scratch bd store: {exc}") from exc

    # ---- bd --------------------------------------------------------------------------------

    def bd(self, *args: str) -> str:
        """Run ``bd -C <store> --actor dev/conformance <args>``; the stdout, or raise.

        bd-seam-justified: scratch-store driver of a standalone kit (see ``__init__``)."""
        done = subprocess.run(
            ["bd", "-C", str(self.store), "--actor", ACTOR, *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            env=scrubbed_env(),
        )
        if done.returncode != 0:
            raise RuntimeError(f"bd {' '.join(args)} -> {done.returncode}: {done.stderr.strip()}")
        return done.stdout

    def _show(self, bead_id: str) -> dict:
        data = json.loads(self.bd("show", bead_id, "--json"))
        return data[0] if isinstance(data, list) else data

    # ---- DisposableHive --------------------------------------------------------------------

    def create_bead(self) -> str:
        bead_id = self.bd("q", "conformance bead").strip().splitlines()[-1].strip()
        self.bd("update", bead_id, "--claim", "--add-label", "conformance")
        self.bd("update", bead_id, "--set-metadata", "note=pre-existing metadata")
        created = json.loads(
            self.bd("gate", "create", "--blocks", bead_id, "--type", "human", "--json")
        )
        self._gates[bead_id] = created["id"]
        return bead_id

    def snapshot(self, bead_id: str) -> BeadState:
        issue = self._show(bead_id)
        gates = {}
        gate_id = self._gates.get(bead_id)
        if gate_id:
            rows = json.loads(self.bd("gate", "list", "--all", "--json") or "[]") or []
            gates = {r["id"]: r.get("status", "") for r in rows if r["id"] == gate_id}
        return BeadState(
            status=issue.get("status", ""),
            assignee=issue.get("assignee", ""),
            labels=frozenset(issue.get("labels") or ()),
            metadata=dict(issue.get("metadata") or {}),
            gates=gates,
        )

    def seat_command(self) -> str:
        script = Path(__file__).with_name(SEAT_SCRIPT_NAME)
        return (
            f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} "
            f"--hive {shlex.quote(str(self.root))} --bd-store {shlex.quote(str(self.store))}"
        )

    def release_turn(self, bead_id: str, session_id: str) -> None:
        (self.root / "release" / session_id).write_text("go\n", encoding="utf-8")

    def seat_starts(self, bead_id: str) -> list[str]:
        log = self.root / "starts" / f"{bead_id}.log"
        return log.read_text(encoding="utf-8").split() if log.exists() else []

    def record_outcome(self, bead_id: str, session_id: str, seat_stdout: str) -> bool:
        from ... import outcome

        document = outcome.record_document(seat_stdout, session_id, exit_code=None)
        return write_outcome_bd(self.store, bead_id, session_id, json.dumps(document))

    def add_label(self, bead_id: str, label: str) -> None:
        self.bd("update", bead_id, "--add-label", label)

    def set_status(self, bead_id: str, status: str) -> None:
        self.bd("update", bead_id, "--status", status)

    def set_metadata(self, bead_id: str, key: str, value: object) -> None:
        self.bd("update", bead_id, "--metadata", json.dumps({key: value}))

    def resolve_gate(self, bead_id: str) -> None:
        """Resolve the bead's gate (what a misbehaving runtime would do)."""
        self.bd("gate", "resolve", self._gates[bead_id], "--reason", "conformance")

    # ---- teardown --------------------------------------------------------------------------

    def close(self) -> None:
        """Stop the store's sql-server, by the pidfile bd wrote under this store only."""
        if self._closed:
            return
        self._closed = True
        pid_file = self.store / ".beads" / "dolt-server.pid"
        if not pid_file.is_file():
            return
        with contextlib.suppress(OSError, ValueError):
            pid = int(pid_file.read_text().strip())
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
                for _ in range(50):
                    time.sleep(0.1)
                    os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)


def default_hive(root: str | os.PathLike[str]):
    """The hive a run uses when none is given: a real scratch beads store (:class:`BdHive`)
    when the ``bd`` binary is available and a store can be made, else a :class:`FileHive`."""
    if shutil.which("bd") is not None:
        try:
            return BdHive(Path(root) / "hive")
        except BdHiveUnavailable:
            pass
    return FileHive(Path(root) / "hive")
