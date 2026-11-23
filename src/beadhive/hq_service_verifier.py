"""Verify hq-service authority-bearing responses: the signature and the restore floor (bh-2r7qd).

The service is not a new root of trust. ``GET /authority`` (and the ``authority`` envelope that
the config read carries) is checked here exactly as the SQL reader checks a committed
authority row. ``GET /eligibility/{frame}`` carries no envelope (hq-service v0.1.0): the adapter
binds it to a verified ``GET /authority`` read instead (:meth:`HqServiceAdapter.read_eligibility`).

* the operator signature, with :func:`beadhive.hq_sql_signatures.verify_authority` under the
  host-pinned operator key (``signed`` mode; ``trusted`` mode, bh-mk97e, accepts the unsigned
  marker and still applies the floor);
* the host's durable restore floor, in the same file format and refusal rules as
  :meth:`beadhive.hq_sql_runtime.SqlRuntimeAuthority._check_floor`: the sequence never moves
  below the floor, and a head that differs from the floor head must descend from it. Descent and
  same-sequence amendments are judged from the adapter's private history read
  (``GET /history?since=``, :meth:`HqServiceAdapter._read_history`).

Every failure is fail-closed: a definite verdict is :class:`HqRefused` (``authority_signature``,
``restore_floor``), a missing pin or an unusable response is :class:`HqUnavailable`.

The history shape is hq-service's (v0.1.0, ``store/reads.go``): ``{head, floor, revisions}``,
each revision ``{id, message, at, parents, changes[{table, data, schema}]}``, ordered by commit
time. The service issues no ancestry verdict; the check here is the one its design names: walk
``parents`` from the presented head through ``revisions``; the floor is an ancestor iff it is a
parent of a revision so reached. A response that does not fit is refused, never trusted.

Interim catalog check (operator decision, bh-21vpt): a change names a table and whether its data
or schema changed, not whether rows were added, edited or deleted. A same-sequence descendant that
changes ``hq_release_catalog`` is accepted only when every row of the catalog, read through the
v0.1.0 catalog route (``GET /release-catalog/{catalog}``, :meth:`HqServiceAdapter._read_catalog`),
verifies against the service's own verdict for that row. Any other same-sequence change outside
session provisioning is refused.

Open gap (upstream bh-hq-34d, a row-level change kind in ``/history``): the check cannot see a
DELETED row, nor an edit that leaves the surviving rows consistent, because a row that is gone is
not listed. It narrows the SQL reader's row-by-row ``added`` rule; it does not equal it.
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

#: Most commits a same-sequence (provisioning-only) head may sit past the floor (the SQL
#: reader's bound). The whole-history descent walk is bounded by the service response instead.
MAX_HISTORY = 64
#: Tables a same-sequence descendant may touch: reviewed session provisioning only.
#: ``hq_release_catalog`` is judged separately (:meth:`_check_catalog`), not listed here.
_PROVISIONING_TABLES = frozenset({"dolt_ignore", "hq_liveness_policy"})
_CATALOG_TABLE = "hq_release_catalog"

HistoryReader = Callable[..., HistorySinceFloor]
CatalogReader = Callable[..., Mapping[str, Any]]
#: The release catalog a policy names (``desired.release_policy.catalog``; hq-service accepts
#: only this one). Configurable per verifier.
DEFAULT_CATALOG = "beadhive"


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
        catalog: str = DEFAULT_CATALOG,
    ) -> None:
        self._catalog_name = catalog
        self._catalog: CatalogReader | None = None
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

    def bind_catalog(self, reader: CatalogReader) -> None:
        """Feed the same-sequence catalog check from the adapter's private ``_read_catalog``."""
        self._catalog = reader

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
        try:
            history = self._history(since=floor_head, deadline=time.monotonic() + self._timeout)
        except HqRefused as exc:  # unknown_floor, history_too_long: the reader's "not append-only"
            raise _refuse(
                "restore_floor", f"hq-service refused the history since the floor: {exc.code}"
            ) from None
        by_id: dict[str, Mapping[str, Any]] = {}
        for revision in history.revisions:
            identifier = revision.get("id") if isinstance(revision, Mapping) else None
            parents = revision.get("parents") if isinstance(revision, Mapping) else None
            if (
                not isinstance(identifier, str)
                or not isinstance(parents, list)
                or not all(isinstance(parent, str) for parent in parents)
            ):
                raise _refuse("restore_floor", "hq-service history carries a malformed revision")
            by_id[identifier] = revision
        reached: list[Mapping[str, Any]] = []
        pending, seen, found = [head], set(), False
        while pending:
            current = pending.pop()
            if current in seen or current not in by_id:
                continue
            seen.add(current)
            reached.append(by_id[current])
            for parent in by_id[current]["parents"]:
                found = found or parent == floor_head
                pending.append(parent)
        if not found:
            raise _refuse(
                "restore_floor",
                f"HQ history fork detected: head {head[:12]} does not descend from restore "
                f"floor {floor_head[:12]}",
            )
        if same_sequence:
            if len(reached) > MAX_HISTORY:
                raise _refuse(
                    "restore_floor",
                    f"HQ history exceeds verifier bound: {len(reached)} commits between restore "
                    f"floor {floor_head[:12]} and head {head[:12]} (limit {MAX_HISTORY}) for a "
                    "same-sequence head",
                )
            self._check_provisioning(reached, floor_head, head)

    def _check_provisioning(
        self, reached: list[Mapping[str, Any]], floor_head: str, head: str
    ) -> None:
        catalog_changed = False
        for revision in reached:
            changes = revision.get("changes")
            if not isinstance(changes, list):
                raise _refuse("restore_floor", "hq-service history carries a malformed change list")
            for change in changes:
                table = str(change.get("table") or "") if isinstance(change, Mapping) else ""
                if table == _CATALOG_TABLE:
                    catalog_changed = True
                elif not (table in _PROVISIONING_TABLES or table.startswith("frame_")):
                    raise _refuse(
                        "restore_floor",
                        f"head {head[:12]} keeps the authority sequence of restore floor "
                        f"{floor_head[:12]} but changes {table or 'an unknown table'}; only "
                        "reviewed session provisioning may share an authority sequence",
                    )
        if catalog_changed:
            self._check_catalog(floor_head, head)

    def _check_catalog(self, floor_head: str, head: str) -> None:
        """Every catalog row must verify through the catalog route (see the module docstring)."""
        if self._catalog is None:
            raise HqUnavailable("no catalog reader is bound; refusing an unchecked catalog change")
        name = self._catalog_name
        try:
            listing = self._catalog(name, deadline=time.monotonic() + self._timeout)
            rows = listing.get("rows")
            if not isinstance(rows, list) or not all(isinstance(r, Mapping) for r in rows):
                raise _refuse("restore_floor", "hq-service catalog listing carries no rows")
            revoked = {(r.get("version"), r.get("measured_digest")) for r in rows
                       if r.get("status") == "revoked"}  # fmt: skip
            for row in rows:
                key = (row.get("version"), row.get("measured_digest"))
                if row.get("catalog") != name or not all(isinstance(k, str) and k for k in key):
                    raise _refuse("restore_floor", "hq-service catalog row is malformed")
                verdict = self._catalog(
                    name,
                    measured={"id": key[0], "digest": key[1]},
                    deadline=time.monotonic() + self._timeout,
                ).get("release")
                folded = verdict.get("row") if isinstance(verdict, Mapping) else None
                expected = "revoked" if key in revoked else "active"
                ok = (
                    isinstance(folded, Mapping)
                    and (folded.get("version"), folded.get("measured_digest")) == key
                    and folded.get("status") == expected
                    and (expected == "revoked" or verdict.get("verified") is True)
                )
                if row.get("status") not in ("active", "revoked") or not ok:
                    raise _refuse(
                        "restore_floor",
                        f"head {head[:12]} keeps the authority sequence of restore floor "
                        f"{floor_head[:12]} but its release catalog row {key[0]} does not verify",
                    )
        except HqRefused as exc:
            if exc.code == "restore_floor":
                raise
            raise _refuse(
                "restore_floor", f"hq-service refused the catalog read: {exc.code}"
            ) from None
