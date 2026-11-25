"""The disposable hive a conformance run drives, and the bead-state snapshot it diffs.

The kit needs only a handful of operations from a hive, so it asks for them through the
:class:`DisposableHive` protocol rather than a BeadHive fixture. :class:`FileHive` is the
reference implementation: a directory of JSON files, nothing but the standard library, safe to
share with a seat subprocess. A package that wants the kit to run against a real beads store
implements the protocol over it.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Protocol, runtime_checkable

from .seat import OUTCOME_PREFIX, SEAT_SCRIPT_NAME
from .state import BeadState

__all__ = ["BeadState", "DisposableHive", "FileHive", "OUTCOME_PREFIX"]

SEAT_SCRIPT = Path(__file__).with_name(SEAT_SCRIPT_NAME)


@runtime_checkable
class DisposableHive(Protocol):
    """What the kit needs from a throwaway hive. Implement it over a scratch beads store to
    run the kit against the real thing; :class:`FileHive` is the dependency-free reference.

    ``kind`` names what the hive is: ``"bd"`` for a real beads store (a runtime's writes
    through ``bd`` are visible to :meth:`snapshot`), ``"file"`` for a JSON stand-in a runtime
    can only write through the kit's own back-channel."""

    kind: str

    def create_bead(self) -> str:
        """A fresh claimed bead (status ``in_progress``, an assignee, one label, some
        non-outcome metadata) for one turn to run against. Returns its id."""
        ...

    def snapshot(self, bead_id: str) -> BeadState:
        """The bead's claim, status, labels and metadata right now."""
        ...

    def seat_command(self) -> str:
        """A ``work.dispatch.seat_command`` style template (``{role}`` allowed) whose command
        plays one seat turn for the settled ``bh-<role>`` argv, finishes only after
        :meth:`release_turn`, and then records its SeatRun under ``bh.outcome.<session_id>``
        on the bead exactly as ``bh role --outcome-sink bead --session-id`` does."""
        ...

    def release_turn(self, bead_id: str, session_id: str) -> None:
        """Let the seat turn for *session_id* on *bead_id* finish (turns are gated per
        session, so a second attempt on the same bead has its own ``running`` window)."""
        ...

    def seat_starts(self, bead_id: str) -> list[str]:
        """Session ids of every seat process started for *bead_id*, in order."""
        ...

    def record_outcome(self, bead_id: str, session_id: str, seat_stdout: str) -> bool:
        """Store a finished seat's stdout through the outcome sink for *session_id*. False when
        the session already has a record (the sink is append-only per session)."""
        ...

    # The three below are what a misbehaving runtime would reach for; the kit never calls them
    # itself, they exist so a test double can misbehave through the same hive the kit watches.
    def add_label(self, bead_id: str, label: str) -> None: ...

    def set_status(self, bead_id: str, status: str) -> None: ...

    def set_metadata(self, bead_id: str, key: str, value: object) -> None: ...


class FileHive:
    """A :class:`DisposableHive` kept as JSON files under *root*. Hermetic, but it cannot see a
    runtime that writes through ``bd``: a GO obtained on it is caveated (``kind == "file"``).

    ``beads/<id>.json`` holds a bead, ``release/<session_id>`` is the turn gate and
    ``starts/<id>.log`` lists the sessions the stand-in seat started."""

    kind = "file"

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)
        for sub in ("beads", "release", "starts"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self._next = 0

    # ---- DisposableHive ---------------------------------------------------------------------

    def create_bead(self) -> str:
        self._next += 1
        bead_id = f"conf-{self._next}"
        self._save(
            bead_id,
            {
                "status": "in_progress",
                "assignee": "dev/conformance",
                "labels": ["conformance"],
                "metadata": {"note": "pre-existing metadata"},
            },
        )
        return bead_id

    def snapshot(self, bead_id: str) -> BeadState:
        raw = self._load(bead_id)
        return BeadState(
            status=raw["status"],
            assignee=raw["assignee"],
            labels=frozenset(raw["labels"]),
            metadata=dict(raw["metadata"]),
        )

    def seat_command(self) -> str:
        return (
            f"{shlex.quote(sys.executable)} {shlex.quote(str(SEAT_SCRIPT))} "
            f"--hive {shlex.quote(str(self.root))}"
        )

    def release_turn(self, bead_id: str, session_id: str) -> None:
        (self.root / "release" / session_id).write_text("go\n", encoding="utf-8")

    def seat_starts(self, bead_id: str) -> list[str]:
        log = self.root / "starts" / f"{bead_id}.log"
        return log.read_text(encoding="utf-8").split() if log.exists() else []

    def record_outcome(self, bead_id: str, session_id: str, seat_stdout: str) -> bool:
        import json as _json

        from ... import outcome
        from .seat import write_outcome

        document = outcome.record_document(seat_stdout, session_id, exit_code=None)
        return write_outcome(self.root, bead_id, session_id, _json.dumps(document))

    def add_label(self, bead_id: str, label: str) -> None:
        raw = self._load(bead_id)
        raw["labels"] = sorted({*raw["labels"], label})
        self._save(bead_id, raw)

    def set_status(self, bead_id: str, status: str) -> None:
        raw = self._load(bead_id)
        raw["status"] = status
        self._save(bead_id, raw)

    def set_metadata(self, bead_id: str, key: str, value: object) -> None:
        raw = self._load(bead_id)
        raw["metadata"][key] = value
        self._save(bead_id, raw)

    # ---- storage ---------------------------------------------------------------------------

    def _path(self, bead_id: str) -> Path:
        return self.root / "beads" / f"{bead_id}.json"

    def _load(self, bead_id: str) -> dict:
        return json.loads(self._path(bead_id).read_text(encoding="utf-8"))

    def _save(self, bead_id: str, raw: dict) -> None:
        path = self._path(bead_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw), encoding="utf-8")
        os.replace(tmp, path)
