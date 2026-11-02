"""The XTDB BeadLog store loader: plans with :mod:`._plan`, inspects the target, writes SYNC.

:class:`XtdbStoreLoader` has the ``beadlog`` StoreLoader shape (``name``, ``version``,
``capabilities``, ``export()``, ``load(events, *, dry_run)``), so ``beadlog.testing``'s
loader-conformance kit runs against it unchanged, and the ``beadlog.store`` plugin
(``beadhive-beadlog-xtdb``) can hand it to ``bh backfill``. It never imports ``beadhive``.

One :meth:`~XtdbStoreLoader.load`:

1. **Version gate** (``>=2.2.0,<2.3``), then **target inspection**: the latest transaction in
   ``xt.txs`` and whether any loader table holds a row.
2. **Mode.** ``initial`` needs a fresh database: no row in any loader table and no transaction at
   or after the batch's first ``recorded_at`` (format ADR A2, target check); otherwise it is
   refused before anything is written. ``incremental`` writes wall-clock transactions on top of
   an earlier load (read back from the journal). ``auto`` (the default) picks ``initial`` for a
   fresh target and ``incremental`` otherwise.
3. **Plan** (:func:`~beadlog_xtdb.plan_load`): every guard (strict order, future time, split
   precondition) and every loss is decided here; a dry run stops after this step and returns the
   report.
4. **Write.** Each planned transaction is ``BEGIN READ WRITE [WITH (SYSTEM_TIME = TIMESTAMP
   '...')]``, its statements, and a plain (SYNC) ``COMMIT`` on an autocommit connection, so an
   abort surfaces at ``COMMIT``. On **any** failure the loader rolls back and raises
   :class:`~beadlog_xtdb.LoadAborted` telling the operator to discard the database and reload: it
   never retries in place (a retry cannot recover recorded-time preservation, ``bh-3jp2g`` S2-S5,
   and a non-time abort silently loses that transaction, S12). After the last commit it checks
   that ``xt.txs`` recorded no aborted transaction since the load began; only then is the report
   returned.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from typing import IO, Any

from beadlog import (
    KNOWN_CAPABILITIES,
    ArchiveReader,
    BeadEvent,
    LoaderCapability,
    ReadLimits,
    sort_events,
)

from ._connect import insert_records
from ._plan import (
    DEFAULT_LOAD_BATCH,
    JOURNAL,
    LOADER_TABLES,
    DeletePortion,
    Erase,
    LoadAborted,
    LoadMode,
    LoadPlan,
    TargetNotFreshError,
    Transaction,
    first_recorded_at,
    plan_load,
)
from ._version import require_supported_version

LOADER_NAME = "xtdb"
CAPABILITIES: frozenset[str] = frozenset(
    {
        str(LoaderCapability.VALID_TIME),
        str(LoaderCapability.PER_ELEMENT_VALID_TIME),
        str(LoaderCapability.RECORDED_TIME_MONOTONE_LOAD),
        str(LoaderCapability.ERASE_LOGICAL),
    }
)
LATEST_TX_SQL = "SELECT max(system_time), max(_id) FROM xt.txs"
ABORTED_SINCE_SQL = "SELECT COUNT(*) FROM xt.txs WHERE committed = false AND _id > %s"
ABORTED_ALL_SQL = "SELECT COUNT(*) FROM xt.txs WHERE committed = false"
TABLES_SQL = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
JOURNAL_SQL = f"SELECT event, valid_at FROM {JOURNAL}"
DISCARD_AND_RELOAD = (
    "Discard this database (stop the node and delete its whole data directory, or drop the "
    "attached database) and reload from an empty one. The loader never retries in place, and "
    "no part of this load may be treated as loaded."
)


def _version() -> str:
    try:
        return metadata.version("beadlog-xtdb")
    except metadata.PackageNotFoundError:  # pragma: no cover - always installed in practice
        return "0.0.0"


def system_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class TargetState:
    """What the target holds before a load."""

    latest_system_time: datetime | None
    latest_tx_id: int | None
    tables: frozenset[str]
    nonempty: tuple[str, ...]

    def fresh_for(self, first: datetime | None) -> bool:
        """Whether an initial load whose first system time is ``first`` may run."""
        if self.nonempty:
            return False
        return self.latest_system_time is None or first is None or self.latest_system_time < first


def inspect_target(connection: Any) -> TargetState:
    """Read the latest transaction and which loader tables hold rows (any valid time)."""
    latest, latest_id = connection.execute(LATEST_TX_SQL).fetchone()
    tables = frozenset(str(row[0]) for row in connection.execute(TABLES_SQL).fetchall())
    nonempty = tuple(
        table
        for table in LOADER_TABLES
        if table in tables
        and connection.execute(f"SELECT _id FROM {table} FOR ALL VALID_TIME LIMIT 1").fetchone()
        is not None
    )
    return TargetState(
        latest if isinstance(latest, datetime) else None,
        int(latest_id) if latest_id is not None else None,
        tables,
        nonempty,
    )


def _sql_instant(moment: datetime) -> str:
    text = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        text += "." + f"{moment.microsecond:06d}".rstrip("0")
    return text + "Z"


def begin_statement(tx: Transaction) -> str:
    """``BEGIN READ WRITE`` with the backfill ``SYSTEM_TIME`` (syntax S1a) when it has one."""
    if tx.system_time is None:
        return "BEGIN READ WRITE"
    return f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{_sql_instant(tx.system_time)}')"


def delete_statement(delete: DeletePortion) -> tuple[str, list[Any]]:
    end = "%s" if delete.end is not None else "NULL"
    sql = f"DELETE FROM {delete.table} FOR PORTION OF VALID_TIME FROM %s TO {end} WHERE _id = %s"
    params: list[Any] = [delete.start]
    if delete.end is not None:
        params.append(delete.end)
    params.append(delete.entity)
    return sql, params


def erase_statement(erase: Erase) -> tuple[str, list[Any]]:
    marks = ", ".join(["%s"] * len(erase.values))
    sql = f"ERASE FROM {erase.table} WHERE {erase.column} IN ({marks})"
    params: list[Any] = list(erase.values)
    if erase.keep_tombstones:
        sql += " AND kind <> %s"
        params.append("tombstone")
    return sql, params


class XtdbStoreLoader:
    """BeadLog events into one XTDB database (module doc).

    ``connection`` is a psycopg connection configured by :func:`~beadlog_xtdb.connect`; the
    loader switches it to autocommit, because every transaction is an explicit ``BEGIN`` (the
    backfill ``SYSTEM_TIME`` can only be set there). ``clock`` is the wall clock (injected by
    tests). ``target`` names the database in the report (default: from the connection).
    """

    name = LOADER_NAME
    capabilities = CAPABILITIES

    def __init__(
        self,
        connection: Any,
        *,
        mode: LoadMode | str = LoadMode.AUTO,
        clock: Callable[[], datetime] = system_clock,
        target: str | None = None,
        batch_size: int = DEFAULT_LOAD_BATCH,
        multi_group: bool | None = None,
    ) -> None:
        self.connection = connection
        self.mode = LoadMode(mode)
        self._clock = clock
        self._target = target
        self.batch_size = batch_size
        self.multi_group = multi_group
        self.version = _version()
        self.last_plan: LoadPlan | None = None
        if getattr(connection, "autocommit", True) is False:
            connection.autocommit = True

    @property
    def target(self) -> str:
        if self._target is not None:
            return self._target
        info = getattr(self.connection, "info", None)
        host = getattr(info, "host", None) or "localhost"
        port = getattr(info, "port", None) or 5432
        dbname = getattr(info, "dbname", None) or "xtdb"
        return f"xtdb://{host}:{port}/{dbname}"

    # -- reading back ---------------------------------------------------------------------

    def journal(self) -> list[tuple[BeadEvent, datetime | None]]:
        """Every loaded event with the valid instant its rows were written at."""
        tables = {str(row[0]) for row in self.connection.execute(TABLES_SQL).fetchall()}
        if JOURNAL not in tables:
            return []
        out = []
        for text, valid_at in self.connection.execute(JOURNAL_SQL).fetchall():
            event = BeadEvent.from_json(json.loads(text))
            out.append((event, valid_at if isinstance(valid_at, datetime) else None))
        return out

    def export(self) -> tuple[BeadEvent, ...]:
        """The store's history as events, each with its original id (from the journal)."""
        return sort_events(event for event, _ in self.journal())

    # -- loading --------------------------------------------------------------------------

    def plan(
        self, events: Sequence[BeadEvent], *, order: Sequence[str] | None = None
    ) -> tuple[LoadPlan, TargetState]:
        """Inspect the target, resolve the mode and plan the load; writes nothing."""
        batch = tuple(events)
        require_supported_version(self.connection)
        state = inspect_target(self.connection)
        first = first_recorded_at(batch)
        fresh = state.fresh_for(first)
        mode = self.mode
        if mode is LoadMode.INITIAL and not fresh:
            raise TargetNotFreshError(_not_fresh(state, first))
        if mode is LoadMode.AUTO:
            mode = LoadMode.INITIAL if fresh else LoadMode.INCREMENTAL
        existing: list[tuple[BeadEvent, datetime | None]] = []
        if mode is LoadMode.INCREMENTAL:
            existing = self.journal()
            if state.nonempty and not existing:
                raise TargetNotFreshError(
                    f"{self.target} holds layout (b) rows ({', '.join(state.nonempty)}) but no "
                    f"{JOURNAL} journal: they were not written by this loader, so an incremental "
                    "load cannot know their state; load into a fresh database"
                )
        plan = plan_load(
            batch,
            mode=mode,
            now=self._clock(),
            existing=existing,
            order=order,
            multi_group=self.multi_group,
            batch_size=self.batch_size,
        )
        return plan, state

    def load(
        self,
        events: Sequence[BeadEvent],
        *,
        dry_run: bool,
        order: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Apply (or with ``dry_run`` only plan) ``events``; return the loss report.

        Raises :class:`~beadlog_xtdb.LoadRefused` (nothing written) when a guard fails and
        :class:`~beadlog_xtdb.LoadAborted` when a transaction fails mid-load.
        """
        plan, state = self.plan(events, order=order)
        self.last_plan = plan
        if not dry_run:
            self._write(plan, state)
        return plan.report.to_json(
            name=self.name,
            version=self.version,
            capabilities=self.capabilities,
            target=self.target,
            dry_run=dry_run,
        )

    def _write(self, plan: LoadPlan, state: TargetState) -> None:
        connection = self.connection
        total = len(plan.transactions)
        for number, tx in enumerate(plan.transactions, start=1):
            try:
                connection.execute(begin_statement(tx))
                for table, documents in tx.inserts.items():
                    insert_records(connection, table, documents, batch_size=self.batch_size)
                for delete in tx.deletes:
                    connection.execute(*delete_statement(delete))
                for erase in tx.erases:
                    connection.execute(*erase_statement(erase))
                connection.execute("COMMIT")
            except Exception as exc:
                _rollback(connection)
                raise LoadAborted(_aborted(plan, tx, number, total, exc)) from exc
        try:
            if state.latest_tx_id is None:
                row = connection.execute(ABORTED_ALL_SQL).fetchone()
            else:
                row = connection.execute(ABORTED_SINCE_SQL, [state.latest_tx_id]).fetchone()
        except Exception as exc:
            raise LoadAborted(
                f"could not confirm the load in xt.txs: {exc}. {DISCARD_AND_RELOAD}"
            ) from exc
        if row is not None and int(row[0]):
            raise LoadAborted(
                f"xt.txs records {int(row[0])} aborted transaction(s) during this {plan.mode} "
                f"load to {self.target}. {DISCARD_AND_RELOAD}"
            )


def _rollback(connection: Any) -> None:
    try:
        connection.execute("ROLLBACK")
    except Exception:  # noqa: BLE001 - the abort is what gets reported
        pass


def _aborted(plan: LoadPlan, tx: Transaction, number: int, total: int, exc: BaseException) -> str:
    when = (
        f"SYSTEM_TIME {_sql_instant(tx.system_time)}"
        if tx.system_time is not None
        else "wall clock"
    )
    first = (str(exc).splitlines() or [type(exc).__name__])[0]
    lost = (
        " Recorded-time preservation is lost for this database (bh-3jp2g S2-S5)."
        if plan.mode is LoadMode.INITIAL
        else ""
    )
    return (
        f"XTDB {plan.mode} load aborted in transaction {number} of {total} ({tx.phase}, {when}): "
        f"{first}.{lost} {DISCARD_AND_RELOAD}"
    )


def _not_fresh(state: TargetState, first: datetime | None) -> str:
    if state.nonempty:
        return (
            f"an initial load needs a fresh database, but {', '.join(state.nonempty)} already "
            "hold rows; load into a fresh node or attached database, or use incremental mode "
            "(recorded time is then not preserved)"
        )
    latest = _sql_instant(state.latest_system_time) if state.latest_system_time else "?"
    start = _sql_instant(first) if first is not None else "?"
    return (
        f"an initial load needs a database with no transaction at or after its first "
        f"recorded_at {start}, but the latest transaction is at {latest} (format ADR A2): "
        "every backfill transaction would abort. Load into a fresh database, or use incremental "
        "mode (recorded time is then not preserved)"
    )


def load_archive(
    loader: XtdbStoreLoader,
    source: str | IO[bytes],
    *,
    dry_run: bool = False,
    implemented_capabilities: Iterable[str] = KNOWN_CAPABILITIES,
    limits: ReadLimits | None = None,
) -> dict[str, Any]:
    """Load a ``.bdlg`` replaying its ``index/recorded.jsonl`` order (when it ships one).

    Like ``beadlog.testing.load_archive`` (refuses unknown required capabilities and tampered
    members before anything is written), plus: the index's order drives the replay and is
    checked, and the report's ``archive`` block names the manifest.
    """
    with ArchiveReader(
        source, limits=limits, implemented_capabilities=implemented_capabilities
    ) as reader:
        reader.verify()
        events = tuple(reader.events())
        order = [entry.id for entry in reader.index()] if reader.has_index else None
        report = dict(loader.load(events, dry_run=dry_run, order=order))
        report["archive"] = {
            "format_version": reader.manifest.format_version,
            "manifest_sha256": reader.manifest_sha256,
        }
    return report


__all__ = [
    "ABORTED_ALL_SQL",
    "ABORTED_SINCE_SQL",
    "CAPABILITIES",
    "DISCARD_AND_RELOAD",
    "JOURNAL_SQL",
    "LATEST_TX_SQL",
    "LOADER_NAME",
    "TABLES_SQL",
    "TargetState",
    "XtdbStoreLoader",
    "begin_statement",
    "delete_statement",
    "erase_statement",
    "inspect_target",
    "load_archive",
    "system_clock",
]
