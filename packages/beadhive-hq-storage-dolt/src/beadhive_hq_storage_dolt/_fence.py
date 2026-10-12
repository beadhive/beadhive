"""The Dolt ``HiveFence``: one node's copy of a hive's fence tables, through a statement engine.

Root injects the engine (hq-storage-port-adr sections 6 and 7): its ``DoltCliEngine`` (embedded
mode) and ``BdServerEngine`` (server mode) satisfy :class:`FenceStatementEngine` structurally,
and :class:`SqlConnectionFenceEngine` reaches a ``dolt sql-server`` database over a DB-API
factory. The engine takes no parameters, so every literal is quoted here; every SQL string the
fence needs lives in this package.

The bump is root's ``writer_adopt`` step 2: the ``bh_writer`` singleton, the ``adopt-<epoch>``
sentinel and the ``ReclaimSet`` revert as ONE local commit (conditions 1, 2 and 7), refused at or
below ``max(history_max, writer)``. ``push`` publishes it; ``lookup_bump`` finds the sentinel on
the remote head and the exact commit that introduced it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Protocol, TypeVar

from beadhive_hq_storage import (
    ABSENT,
    Absent,
    Applied,
    BumpOutcome,
    BumpRequest,
    Confirmed,
    Conflict,
    EpochFloor,
    GuardReport,
    IntegrityViolation,
    InvalidRequest,
    LookupOutcome,
    NotConfirmed,
    NotProvisioned,
    OperationId,
    Outcome,
    ReclaimSet,
    Revision,
    StorageError,
    StorageUnavailable,
    SyncReport,
    Unknown,
    Versioned,
    WriterRow,
    adopt_sentinel,
)

from . import _schema as schema
from ._classify import DeadlinePredicate, error_code, is_deadline, unknown_reason
from ._values import aware, check_key, check_operation, quote

__all__ = [
    "DoltHiveFence",
    "FenceStatementEngine",
    "SqlConnectionFenceEngine",
]

C = TypeVar("C")


class FenceStatementEngine(Protocol):
    """How one node's Dolt data is reached. Root's ``DoltCliEngine`` / ``BdServerEngine`` fit.

    ``query`` returns rows as dicts; ``execute`` runs a statement batch; ``commit`` commits the
    working set (``False``: nothing to commit); ``push`` returns ``False`` on a non-fast-forward
    and raises when the remote could not be reached. Any raise from ``query`` / ``execute`` /
    ``fetch`` / ``reset_to_remote`` means the operation did not happen; a raise from ``commit``
    or ``push`` may follow a landed commit, so the fence reports those as ``Unknown``."""

    def query(self, sql: str) -> list[dict]: ...

    def execute(self, statements: Sequence[str]) -> None: ...

    def commit(self, message: str) -> bool: ...

    def fetch(self) -> None: ...

    def reset_to_remote(self) -> None: ...

    def push(self) -> bool: ...


def _check_reclaim(reclaim: ReclaimSet) -> None:
    """The fake's ``ReclaimSet`` rules, restated (refused before any I/O)."""
    if not isinstance(reclaim, ReclaimSet):
        raise InvalidRequest("reclaim must be a ReclaimSet")
    check_key(reclaim.dead_frame, "dead frame")
    for bead in reclaim.bead_ids:
        check_key(bead, "bead id")
    if not reclaim.resumable <= reclaim.bead_ids:
        raise InvalidRequest("every resumable bead must be in the reclaim set")
    seen: set[str] = set()
    for note in reclaim.notes:
        if note.bead_id not in reclaim.bead_ids:
            raise InvalidRequest("every reclaim note must name a bead in the reclaim set")
        check_key(note.comment_id, "comment id")
        check_key(note.author, "comment author")
        if not isinstance(note.text, str) or not note.text:
            raise InvalidRequest("a reclaim note needs its text")
        if not isinstance(note.created_at, datetime) or note.created_at.tzinfo is None:
            raise InvalidRequest("a reclaim note's created_at must be an aware datetime")
        if note.comment_id in seen:
            raise InvalidRequest("reclaim note comment ids must be unique")
        seen.add(note.comment_id)


def _scalar(rows: list[dict]) -> Any:
    if not rows:
        return None
    return next(iter(rows[0].values()), None)


class DoltHiveFence:
    """``HiveFence`` (plus ``CapabilityHost``) over a :class:`FenceStatementEngine`.

    ``guards`` is the ``bh_*`` trigger set a complete install carries (root passes
    ``fence_schema.trigger_names()``: 42 guard + 2 monotonic triggers)."""

    def __init__(
        self,
        engine: FenceStatementEngine,
        *,
        guards: Sequence[str],
        remote: str = "origin",
        branch: str = "main",
        deadline: DeadlinePredicate | None = None,
    ) -> None:
        if not guards:
            raise InvalidRequest("a fence needs the expected guard-trigger names")
        self._engine = engine
        self._guards = tuple(guards)
        self._remote = check_key(remote, "remote")
        self._branch = check_key(branch, "branch")
        self._deadline = deadline
        self._capabilities: dict[type, object] = {}
        self._builtin: Any = None

    @property
    def engine(self) -> FenceStatementEngine:
        return self._engine

    def register_capability(self, kind: type[C], implementation: C) -> None:
        self._capabilities[kind] = implementation

    def capability(self, kind: type[C]) -> C | None:
        """``DoltHistory`` and ``DoltMerge`` over this fence's engine and its remote and branch,
        plus any registered capability; ``None`` otherwise (``DoltAccounts`` belongs to a
        server login, never to a fence)."""
        if kind not in self._capabilities:
            if self._builtin is None:
                from ._capabilities import fence_capabilities

                self._builtin = fence_capabilities(
                    self._engine, remote=self._remote, branch=self._branch
                )
            found = self._builtin.capability(kind)
            if found is None:
                return None
            self._capabilities[kind] = found
        return self._capabilities[kind]  # type: ignore[return-value]

    # -- engine access, classified ------------------------------------------------------------

    def _query(self, sql: str) -> list[dict]:
        try:
            return self._engine.query(sql)
        except Exception:  # noqa: BLE001 - a failed read applied nothing
            raise StorageUnavailable("fence read unavailable") from None

    def _fetch(self) -> None:
        try:
            self._engine.fetch()
        except Exception:  # noqa: BLE001 - an unreachable remote; nothing was sent
            raise StorageUnavailable(f"fetch from {self._remote} unavailable") from None

    @property
    def _tracking(self) -> str:
        return f"{self._remote}/{self._branch}"

    def _remote_head(self) -> Revision | None:
        found = self._query(
            "SELECT hash FROM dolt_remote_branches WHERE name = "
            + quote(f"remotes/{self._tracking}")
        )
        value = _scalar(found)
        return None if value in (None, "") else Revision(str(value))

    def _head(self) -> Revision:
        return Revision(str(_scalar(self._query("SELECT DOLT_HASHOF('HEAD') AS h"))))

    def _has_writer_table(self) -> bool:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = 'bh_writer'"
        )
        return bool(int(_scalar(rows) or 0))

    def _writer(self, as_of: str | None = None) -> WriterRow | None:
        suffix = "" if as_of is None else f" AS OF {quote(as_of)}"
        try:
            rows = self._engine.query(
                f"SELECT frame, epoch, revision FROM bh_writer{suffix} WHERE id = 1"
            )
        except Exception as exc:  # noqa: BLE001
            if "not found" in str(exc).lower() or "doesn't exist" in str(exc).lower():
                return None
            raise StorageUnavailable("fence writer read unavailable") from None
        if not rows:
            return None
        row = rows[0]
        return WriterRow(str(row["frame"]), int(row["epoch"]), str(row.get("revision") or ""))

    # -- HiveFence -----------------------------------------------------------------------------

    def _counts(self, remote: Revision | None) -> tuple[int, int]:
        if remote is None:
            rows = self._query(f"SELECT COUNT(*) AS n FROM dolt_log({quote(self._branch)})")
            return int(_scalar(rows) or 0), 0
        ahead = self._query(
            f"SELECT COUNT(*) AS n FROM dolt_log({quote(self._branch)}, '--not', "
            f"{quote(self._tracking)})"
        )
        behind = self._query(
            f"SELECT COUNT(*) AS n FROM dolt_log({quote(self._tracking)}, '--not', "
            f"{quote(self._branch)})"
        )
        return int(_scalar(ahead) or 0), int(_scalar(behind) or 0)

    def sync_to_remote(self) -> SyncReport:
        self._fetch()
        ahead, behind = self._counts(self._remote_head())
        try:
            self._engine.reset_to_remote()
        except Exception:  # noqa: BLE001
            raise StorageUnavailable(f"reset to {self._tracking} unavailable") from None
        return SyncReport(self._head(), ahead, behind, ahead > 0 and behind > 0)

    def writer(self) -> WriterRow | None:
        if not self._has_writer_table():
            return None
        return self._writer()

    def remote_writer(self) -> WriterRow | None:
        self._fetch()
        if self._remote_head() is None:
            return None
        return self._writer(self._tracking)

    def _history_max(self) -> int:
        try:
            rows = self._engine.query(
                "SELECT COALESCE(MAX(epoch), 0) AS m FROM dolt_history_bh_writer WHERE id = 1"
            )
        except Exception as exc:  # noqa: BLE001
            if "not found" in str(exc).lower() or "doesn't exist" in str(exc).lower():
                return 0
            raise StorageUnavailable("fence history read unavailable") from None
        return int(_scalar(rows) or 0)

    def epoch_floor(self) -> EpochFloor:
        writer = self.writer()
        return EpochFloor(
            history_max=self._history_max(), writer=None if writer is None else writer.epoch
        )

    def guard_conformance(self) -> GuardReport:
        rows = self._query(
            "SELECT trigger_name AS t FROM information_schema.triggers "
            "WHERE trigger_schema = DATABASE()"
        )
        present = {str(_scalar([row])) for row in rows}
        missing = tuple(name for name in self._guards if name not in present)
        return GuardReport(
            expected=len(self._guards), present=len(self._guards) - len(missing), missing=missing
        )

    def commit_bump(self, bump: BumpRequest) -> BumpOutcome:
        """The bump as ONE local commit. Refusals (``InvalidRequest``, ``NotProvisioned``,
        ``IntegrityViolation``) and a failed statement batch (``StorageUnavailable``) commit
        nothing. Once the engine's commit is called the bump may exist, so a raise from it, or
        from the head read after it, is ``Unknown`` with the pre-bump head as ``expected``
        (bh-xxkf7.6): the caller pushes and resolves it with ``lookup_bump``, never re-issues."""
        if not isinstance(bump, BumpRequest):
            raise InvalidRequest("commit_bump takes a BumpRequest")
        check_key(bump.frame, "frame")
        check_operation(bump.operation)
        if not isinstance(bump.epoch, int) or isinstance(bump.epoch, bool) or bump.epoch < 1:
            raise InvalidRequest("epoch must be a positive integer")
        if not isinstance(bump.revision, str) or len(bump.revision) > 64:
            raise InvalidRequest("revision must be a string of at most 64 chars")
        statements = schema.bump_statements(bump.frame, bump.epoch, bump.revision)
        if bump.reclaim is not None:
            _check_reclaim(bump.reclaim)
            statements += schema.reclaim_statements(bump.reclaim)
        writer = self.writer()
        if writer is None:
            raise NotProvisioned("the hive is not cut over: no bh_writer row")
        floor = max(self._history_max(), writer.epoch)
        if bump.epoch <= floor:
            raise IntegrityViolation(f"epoch {bump.epoch} does not exceed the floor {floor}")
        prior = self._head()  # Unknown's ``expected``: read before anything is sent
        message = f"{schema.ADOPT_COMMIT_PREFIX}{bump.frame}@{bump.epoch}"
        try:
            self._engine.execute(statements)
        except Exception:  # noqa: BLE001 - the working set only: nothing committed
            self._discard(bump.reclaim is not None)
            raise StorageUnavailable(
                f"bump to epoch {bump.epoch} failed; nothing committed"
            ) from None
        try:
            committed = self._engine.commit(message)
        except Exception as exc:  # noqa: BLE001 - the commit point: its fate is unknown
            self._discard(bump.reclaim is not None)  # a no-op when the commit landed
            reason = unknown_reason(exc, deadline=self._deadline)
            return Unknown(bump.operation, prior, reason)
        if not committed:
            raise IntegrityViolation(f"bump to epoch {bump.epoch} changed nothing")
        try:
            head = _scalar(self._engine.query("SELECT DOLT_HASHOF('HEAD') AS h"))
        except Exception as exc:  # noqa: BLE001 - after the commit point (bh-xxkf7.6)
            reason = "deadline" if is_deadline(exc, self._deadline) else "transport"
            return Unknown(bump.operation, prior, reason)
        if head in (None, "") or Revision(str(head)) == prior:
            return Unknown(bump.operation, prior, "transport")
        return Applied(Revision(str(head)), bump.operation)

    def _discard(self, reclaimed: bool) -> None:
        tables = [*schema.FENCE_TABLES, *(("issues", "labels", "comments") if reclaimed else ())]
        try:
            self._engine.execute(
                ["CALL DOLT_CHECKOUT(" + ", ".join(quote(t) for t in tables) + ")"]
            )
        except Exception:  # noqa: BLE001, S110 - best effort; the bump was not committed
            pass

    def push(self) -> Outcome:
        operation = OperationId(str(uuid.uuid4()))
        self._fetch()
        prior = self._remote_head()
        expected: Revision | Absent = ABSENT if prior is None else prior
        local = self._head()
        try:
            pushed = self._engine.push()
        except Exception as exc:  # noqa: BLE001 - classified: refusal, or unknown fate
            return self._push_failure(exc, operation, expected, local)
        if pushed:
            return Applied(local, operation)
        return self._conflict()

    def _conflict(self) -> Outcome:
        self._fetch()
        remote = self._remote_head()
        writer = None if remote is None else self._writer(str(remote))
        body: dict[str, Any] = (
            {} if writer is None else {"frame": writer.frame, "epoch": writer.epoch}
        )
        return Conflict(Versioned("bh_writer", Revision(str(remote or "")), body))

    def _push_failure(
        self,
        exc: BaseException,
        operation: OperationId,
        expected: Revision | Absent,
        local: Revision,
    ) -> Outcome:
        reason = unknown_reason(exc, deadline=self._deadline)
        if is_deadline(exc, self._deadline) or error_code(exc) in (1180, 2006, 2013, 2055):
            return Unknown(operation, expected, reason)
        try:
            self._fetch()
            remote = self._remote_head()
            if remote is not None:
                base = _scalar(
                    self._query(f"SELECT dolt_merge_base({quote(local)}, {quote(remote)}) AS b")
                )
                if str(base) != remote:
                    return self._conflict()
        except StorageError:
            pass
        return Unknown(operation, expected, reason)

    def lookup_bump(self, frame: str, epoch: int, *, operation: OperationId) -> LookupOutcome:
        """``Confirmed`` while the ``adopt-<epoch>`` sentinel is on the remote head AND the
        commit that introduced it named ``frame``; ``NotConfirmed`` otherwise (including once a
        later bump retired it). The operation id is never persisted: the sentinel is the
        witness (settled by bh-9fg5k)."""
        operation = check_operation(operation)
        self._fetch()
        remote = self._remote_head()
        if remote is None:
            return NotConfirmed(operation)
        sentinel = adopt_sentinel(epoch)
        at = quote(str(remote))
        present = self._query(
            f"SELECT COUNT(*) AS n FROM bh_write_mark AS OF {at} WHERE id = {quote(sentinel)}"
        )
        if not int(_scalar(present) or 0):
            return NotConfirmed(operation)
        database = str(_scalar(self._query("SELECT DATABASE() AS d")))
        revision_db = f"`{database}/{remote}`"
        carrying = {
            str(row["c"])
            for row in self._query(
                f"SELECT commit_hash AS c FROM {revision_db}.dolt_history_bh_write_mark "
                f"WHERE id = {quote(sentinel)}"
            )
        }
        log = self._query(
            f"SELECT commit_hash AS c, parents AS p, date AS d FROM dolt_log('--parents', {at})"
        )
        for row in log:
            commit = str(row["c"])
            if commit not in carrying:
                continue
            parents = [p.strip() for p in str(row.get("p") or "").split(",") if p.strip()]
            if any(parent in carrying for parent in parents):
                continue  # the sentinel only rode along from an earlier commit
            writer = self._writer(commit)
            if writer is not None and writer.frame == frame and writer.epoch == int(epoch):
                return Confirmed(operation, Revision(commit), aware(row["d"]))
            break
        return NotConfirmed(operation)


class SqlConnectionFenceEngine:
    """A :class:`FenceStatementEngine` over one database of a ``dolt sql-server``, reached by a
    DB-API factory (format paramstyle, autocommit irrelevant: every statement commits its own
    transaction here). The commit stages exactly the working set (``-A``), as root's engines do."""

    def __init__(
        self,
        connect: Callable[[], Any],
        *,
        remote: str = "origin",
        branch: str = "main",
        author: str = "beadhive fence <fence@localhost>",
    ) -> None:
        self._connect = connect
        self.remote, self.branch, self.author = remote, branch, author

    def _run(self, statements: Sequence[str], *, rows: bool = False) -> list[dict]:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            out: list[dict] = []
            for statement in statements:
                cursor.execute(statement)
                if rows and cursor.description:
                    names = [d[0] for d in cursor.description]
                    out = [dict(zip(names, r, strict=True)) for r in cursor.fetchall()]
            connection.commit()
            return out
        except Exception:
            try:
                connection.rollback()
            finally:
                raise
        finally:
            connection.close()

    def query(self, sql: str) -> list[dict]:
        return self._run([sql], rows=True)

    def execute(self, statements: Sequence[str]) -> None:
        self._run(list(statements))

    def commit(self, message: str) -> bool:
        if not self.query("SELECT table_name FROM dolt_status"):
            return False
        self._run([f"CALL DOLT_COMMIT('-Am', {quote(message)}, '--author', {quote(self.author)})"])
        return True

    def fetch(self) -> None:
        self._run([f"CALL DOLT_FETCH({quote(self.remote)})"])

    def reset_to_remote(self) -> None:
        self.fetch()
        self._run([f"CALL DOLT_RESET('--hard', {quote(f'{self.remote}/{self.branch}')})"])

    def push(self) -> bool:
        try:
            self._run([f"CALL DOLT_PUSH({quote(self.remote)}, {quote(self.branch)})"])
        except Exception as exc:
            text = str(exc).lower()
            if "non-fast-forward" in text or "rejected" in text:
                return False
            raise
        return True
