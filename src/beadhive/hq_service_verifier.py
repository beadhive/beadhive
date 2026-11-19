"""Verify hq-service authority-bearing responses: the signature and the restore floor (bh-2r7qd).

The service is not a new root of trust. ``GET /authority`` (and the ``authority`` envelope that
config and eligibility reads carry) is checked here exactly as the SQL reader checks a committed
authority row:

* the operator signature, with :func:`beadhive.hq_sql_signatures.verify_authority` under the
  host-pinned operator key (``signed`` mode; ``trusted`` mode, bh-mk97e, accepts the unsigned
  marker and still applies the floor);
* the host's durable restore floor, in the same file format and refusal rules as
  :meth:`beadhive.hq_sql_runtime.SqlRuntimeAuthority._check_floor`: the sequence never moves
  below the floor, and a head that differs from the floor head must descend from it. Descent and
  same-sequence amendments are judged from the adapter's private history read
  (``GET /history``, :meth:`HqServiceAdapter._read_history`).

Every failure is fail-closed: a definite verdict is :class:`HqRefused` (``authority_signature``,
``restore_floor``), a missing pin or an unusable response is :class:`HqUnavailable`.

PROVISIONAL (bh-thurg): the history shape is the adapter's guess until hq-service serves it.
``commits`` is newest first, each ``{"commit", "parent"}`` (first parent); ``diff_summary`` rows
are ``{"from_table", "to_table", "diff_type"}``. A response that does not fit is refused, never
trusted.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import hq_authority_enforce
from .hq_port import HistorySinceFloor, HqRefused, HqUnavailable
from .hq_sql_deadline import flock_until
from .hq_sql_signatures import SqlSignatureError, verify_authority

#: Most commits walked between the floor and a presented head (the SQL reader's bound).
MAX_HISTORY = 64
#: Tables a same-sequence descendant may touch: reviewed session provisioning only.
_PROVISIONING_TABLES = frozenset({"dolt_ignore", "hq_liveness_policy", "hq_release_catalog"})

HistoryReader = Callable[..., HistorySinceFloor]


def _refuse(code: str, reason: str) -> HqRefused:
    return HqRefused(code, reason)


class ServiceResponseVerifier:
    """``verifier(method, payload)`` for :class:`~beadhive.hq_service_adapter.HqServiceAdapter`."""

    def __init__(
        self,
        *,
        floor_path: str,
        initial_revision: str,
        operator_public_key: str = "",
        backend_identity: str = "",
        generation: str = "",
        history_timeout: float = 30.0,
    ) -> None:
        self._floor_path = floor_path
        self._initial = initial_revision
        self._key = operator_public_key
        self._backend = backend_identity
        self._generation = generation
        self._timeout = history_timeout
        self._history: HistoryReader | None = None

    def bind_history(self, reader: HistoryReader) -> None:
        """Feed the append-only check from the adapter's private ``_read_history``."""
        self._history = reader

    def __call__(self, method: str, payload: Any) -> Any:
        envelope = payload
        if method != "read_authority":
            envelope = payload.get("authority") if isinstance(payload, Mapping) else None
        self._verify_envelope(method, envelope)
        return payload

    # ---- signature ------------------------------------------------------------------------

    def _verify_envelope(self, method: str, envelope: Any) -> None:
        if not isinstance(envelope, Mapping):
            raise HqUnavailable(f"{method}: response carries no authority to verify")
        head, record, signature = (envelope.get(k) for k in ("head", "record", "signature"))
        if not isinstance(head, str) or not head or not isinstance(record, dict):
            raise HqUnavailable(f"{method}: authority response is malformed")
        sequence = record.get("revision")
        if type(sequence) is not int or sequence < 1:
            raise HqUnavailable(f"{method}: authority record carries no usable revision")
        if hq_authority_enforce.enforced():
            self._verify_signature(record, signature)
        elif not (
            hq_authority_enforce.unsigned_signature(signature)
            or (isinstance(signature, str) and signature)
        ):
            raise _refuse("authority_signature", "authority response carries no signature")
        for field, pin in (("backend_identity", self._backend), ("generation", self._generation)):
            if pin and record.get(field) not in (None, pin):
                raise _refuse("restore_floor", "HQ trust pin or authority generation changed")
        self._check_floor(head, sequence)

    def _verify_signature(self, record: dict, signature: Any) -> None:
        if not self._key:
            raise HqUnavailable(
                "hq.api.operator_public_key is not set; refusing unverified authority"
            )
        if not isinstance(signature, str) or hq_authority_enforce.unsigned_signature(signature):
            raise _refuse(
                "authority_signature",
                hq_authority_enforce.unsigned_refusal("hq-service authority"),
            )
        try:
            verify_authority(record, signature, granted_public_key=self._key)
        except SqlSignatureError:
            raise _refuse("authority_signature", "hq-service authority signature invalid") from None

    # ---- restore floor --------------------------------------------------------------------

    def _check_floor(self, head: str, sequence: int) -> None:
        path = Path(self._floor_path)
        if not path.is_absolute() or path.is_symlink():
            raise HqUnavailable("HQ restore floor custody invalid")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with lock_path.open("a+b") as lock:
            try:
                flock_until(lock, time.monotonic() + self._timeout)
            except TimeoutError:
                raise HqUnavailable("HQ restore floor custody wait exceeded deadline") from None
            previous_sequence, previous_head = self._load_floor(path)
            if sequence < previous_sequence:
                raise _refuse(
                    "restore_floor",
                    f"authority sequence {sequence} at head {head[:12]} is below restore floor "
                    f"sequence {previous_sequence} at {previous_head[:12]}",
                )
            if head != previous_head:
                self._check_descent(
                    previous_head, head, same_sequence=sequence == previous_sequence
                )
            self._write_floor(path, sequence, head)

    def _load_floor(self, path: Path) -> tuple[int, str]:
        if not path.exists():
            if not self._initial:
                raise HqUnavailable("hq.api.initial_revision is not set; no restore floor")
            return 1, self._initial
        if path.stat().st_mode & 0o077:
            raise HqUnavailable("HQ restore floor custody changed")
        try:
            prior = json.loads(path.read_text())
            if (self._backend and prior["backend"] != self._backend) or (
                self._generation and prior["generation"] != self._generation
            ):
                raise HqUnavailable("HQ restore floor binding changed")
            return int(prior["sequence"]), str(prior["head"])
        except HqUnavailable:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise HqUnavailable("HQ restore floor unavailable") from None

    def _write_floor(self, path: Path, sequence: int, head: str) -> None:
        body = json.dumps(
            {
                "backend": self._backend,
                "generation": self._generation,
                "sequence": sequence,
                "head": head,
            },
            sort_keys=True,
        )
        temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _check_descent(self, floor_head: str, head: str, *, same_sequence: bool) -> None:
        if self._history is None:
            raise HqUnavailable("no history reader is bound; refusing an unchecked head")
        history = self._history(since=floor_head, deadline=time.monotonic() + self._timeout)
        wanted, count = head, 0
        for commit in history.commits:
            if not isinstance(commit, Mapping) or commit.get("commit") != wanted:
                raise _refuse("restore_floor", f"history fork: head {head[:12]} is not a chain")
            wanted, count = commit.get("parent"), count + 1
            if wanted == floor_head:
                break
            if count >= MAX_HISTORY:
                break
        if wanted != floor_head:
            raise _refuse(
                "restore_floor",
                f"HQ history fork detected: head {head[:12]} does not descend from restore "
                f"floor {floor_head[:12]}",
            )
        if same_sequence:
            self._check_provisioning(history, floor_head, head)

    @staticmethod
    def _check_provisioning(history: HistorySinceFloor, floor_head: str, head: str) -> None:
        for row in history.diff_summary:
            table = ""
            if isinstance(row, Mapping):
                table = str(row.get("to_table") or row.get("from_table") or "")
            ok = table in _PROVISIONING_TABLES or table.startswith("frame_")
            if ok and table == "hq_release_catalog":
                ok = row.get("diff_type") == "added"
            if not ok:
                raise _refuse(
                    "restore_floor",
                    f"head {head[:12]} keeps the authority sequence of restore floor "
                    f"{floor_head[:12]} but changes {table or 'an unknown table'}; only reviewed "
                    "session provisioning may share an authority sequence",
                )
