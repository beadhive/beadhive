"""The ``dolt`` contract adapter: an owned ``dolt sql-server``, the harnesses and fault injection.

``beadhive_hq_storage.contract`` defines the scenarios and the differential state machine once;
this module supplies what the ``dolt`` parameter needs to run them against real Dolt:

* :class:`DoltSqlServer` -- one private ``dolt sql-server`` (its own ``DOLT_ROOT_PATH``, ``HOME``,
  data dir and port), holding one run-wide Dolt slot (the ``BH_DOLT_SLOTS`` flock throttle root's
  test harness uses, same lock directory) for its lifetime. Readiness is a handshake on the
  server's own "Server ready" line, never a sleep. Each case gets fresh databases on it instead
  of a fresh server.
* :class:`DoltHqHarness` / :class:`DoltFenceHarness` -- the contract's ``HqHarness`` /
  ``FenceHarness`` over :class:`~beadhive_hq_storage_dolt.DoltHqStorage` /
  :class:`~beadhive_hq_storage_dolt.DoltHiveFence`, with a ``file://`` remote, a peer clone that
  plays the competing remote writer, and fault injection at the adapter's commit points.
* :func:`dolt_adapter` -- the :class:`~beadhive_hq_storage.contract.ContractAdapter`.

**Fault injection** is real: the harness wraps the connections (HQ) or the engine (fence) the
adapter is given. An armed ``Unknown`` fires at the first commit point of the named adapter
method (``COMMIT``, ``CALL DOLT_COMMIT``, ``CALL DOLT_PUSH``, an engine push): with
``applied=True`` the real commit runs first, otherwise it never reaches the server, and then the
adapter sees the error the reason names (``TimeoutError`` for ``deadline``, ``2013`` for
``transport``, ``1180`` for ``commit``). ``StorageUnavailable`` is a refused connection
(``2003``) or a failing statement batch, before anything is sent. The store clock is the
server's, so ``clock`` is ``None`` and clock-controlled scenarios skip.

The package never imports a SQL driver: the caller passes one (``pymysql.connect``) as
``driver``. Like ``beadhive_hq_storage.contract``, nothing here is imported by the adapter.
"""

from __future__ import annotations

import fcntl
import json
import os
import selectors
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from beadhive_hq_storage import (
    BumpRequest,
    ChangeSet,
    FenceSnapshot,
    InMemoryEpochRef,
    RecordKind,
    Revision,
    UnknownReason,
    WriterRow,
)
from beadhive_hq_storage.contract import BEADS_SEED, LABELS_SEED, ContractAdapter

from ._capabilities import SqlDoltAccounts, connection_runner
from ._fence import DoltHiveFence, SqlConnectionFenceEngine
from ._hq import DoltHqStorage, Role
from ._schema import COMMENT_STAMP_FORMAT
from ._values import aware, quote

__all__ = [
    "DEFAULT_SLOTS",
    "FENCE_GUARDS",
    "SLOTS_ENV",
    "DoltFenceHarness",
    "DoltHqHarness",
    "DoltSqlServer",
    "Driver",
    "FaultPlan",
    "InjectedFault",
    "dolt_adapter",
    "dolt_available",
]

SLOTS_ENV = "BH_DOLT_SLOTS"
DEFAULT_SLOTS = 8
"""Root ``tests/harness/world.MAX_CONCURRENT_DOLT_SERVER_TESTS``'s default; never raised here."""
FENCE_GUARDS: tuple[str, ...] = ("bh_writer_guard", "bh_epoch_live_guard", "bh_write_mark_guard")
"""The guard set the contract fence carries: the in-memory fake's default, so the state machine
compares equal conformance reports. Root composes the real 44-trigger set."""

_READY = "Server ready"
_START_BUDGET_S = 60.0
_OPERATOR = "HQ operator <hq-operator@localhost>"

Driver = Callable[..., Any]
"""A DB-API ``connect(host=, port=, user=, password=, database=, autocommit=)`` (PyMySQL's)."""


def dolt_available() -> bool:
    return shutil.which("dolt") is not None


# --- the run-wide Dolt slot (root's flock throttle, same lock files) ----------------------------


@contextmanager
def _dolt_slot() -> Iterator[int]:
    """Hold one of ``BH_DOLT_SLOTS`` run-wide permits (``0`` disables the bound), on the same
    lock files as root's ``tests/harness/world.dolt_server_slot``. Try every slot without
    blocking; when all are held, block in the kernel on one of them (no polling). The kernel
    drops a lock when its holder dies, so a crashed worker never wedges a slot."""
    slots = int(os.environ.get(SLOTS_ENV, str(DEFAULT_SLOTS)))
    if slots <= 0:
        yield -1
        return
    lock_dir = Path(tempfile.gettempdir()) / "bh-dolt-server-slots"
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = None
    index = -1
    for candidate in range(slots):
        opened = (lock_dir / f"slot-{candidate}").open("a+")
        try:
            fcntl.flock(opened.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            opened.close()
            continue
        handle, index = opened, candidate
        break
    if handle is None:
        index = os.getpid() % slots
        handle = (lock_dir / f"slot-{index}").open("a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        yield index
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


# --- the owned server ---------------------------------------------------------------------------


class DoltSqlServer:
    """One private ``dolt sql-server`` under ``base``. Use as a context manager.

    ``hold_slot=False`` when the caller already holds a Dolt slot (a root test marked
    ``dolt_server`` gets one from root's autouse fixture): a second, nested slot from a different
    lock implementation is how slot holders deadlock each other."""

    def __init__(self, base: Path, driver: Driver, *, hold_slot: bool = True) -> None:
        self.base = Path(base)
        self.driver = driver
        self.hold_slot = hold_slot
        self.port = 0
        self._process: subprocess.Popen[bytes] | None = None
        self._slot: Any = None
        self._names = 0
        self._lock = threading.Lock()

    def __enter__(self) -> DoltSqlServer:
        dolt = shutil.which("dolt")
        if dolt is None:
            raise RuntimeError("dolt binary unavailable")
        self._slot = _dolt_slot() if self.hold_slot else None
        if self._slot is not None:
            self._slot.__enter__()
        try:
            self._start(dolt)
        except BaseException:
            if self._slot is not None:
                self._slot.__exit__(None, None, None)
            raise
        return self

    def _start(self, dolt: str) -> None:
        root = self.base / "dolt-root"
        (root / ".dolt").mkdir(parents=True, exist_ok=True)
        (self.base / "home").mkdir(exist_ok=True)
        (self.base / "data").mkdir(exist_ok=True)
        (self.base / "remotes").mkdir(exist_ok=True)
        (root / ".dolt" / "config_global.json").write_text(
            json.dumps(
                {
                    "user.name": "fixture operator",
                    "user.email": "operator@fixture.invalid",
                    "versioncheck.disabled": "true",
                    "metrics.disabled": "true",
                }
            )
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        config = self.base / "server.yaml"
        config.write_text(
            f"data_dir: {self.base / 'data'}\nlistener:\n  host: 127.0.0.1\n  port: {self.port}\n"
            "  max_connections: 256\n"
        )
        env = {**os.environ, "DOLT_ROOT_PATH": str(root), "HOME": str(self.base / "home")}
        self._process = subprocess.Popen(
            [dolt, "sql-server", "--config", str(config)],
            cwd=self.base,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self._await_ready()

    def _await_ready(self) -> None:
        """Read the server's output until its "Server ready" line (a handshake, no polling),
        then hand the pipe to a drain thread writing ``server.log``."""
        process = self._process
        assert process is not None and process.stdout is not None
        log = (self.base / "server.log").open("ab")
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        give_up = time.monotonic() + _START_BUDGET_S
        buffered = b""
        try:
            while _READY.encode() not in buffered:
                remaining = give_up - time.monotonic()
                if remaining <= 0 or not selector.select(timeout=remaining):
                    raise RuntimeError(f"dolt sql-server not ready within {_START_BUDGET_S}s")
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("dolt sql-server exited before it was ready")
                log.write(chunk)
                buffered = (buffered + chunk)[-4096:]
        finally:
            selector.close()

        def drain() -> None:
            with log:
                for chunk in iter(lambda: os.read(process.stdout.fileno(), 65536), b""):
                    log.write(chunk)

        threading.Thread(target=drain, name="dolt-server-drain", daemon=True).start()

    def __exit__(self, *exc: object) -> None:
        try:
            if self._process is not None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=30)
        finally:
            if self._slot is not None:
                self._slot.__exit__(None, None, None)

    # -- connections and names -----------------------------------------------------------------

    def connect(self, database: str | None = None, *, autocommit: bool = True) -> Any:
        return self.driver(
            host="127.0.0.1",
            port=self.port,
            user="root",
            password="",
            database=database,
            autocommit=autocommit,
            connect_timeout=10,
            read_timeout=120,
            write_timeout=120,
        )

    def fresh_name(self, prefix: str) -> str:
        with self._lock:
            self._names += 1
            return f"{prefix}{self._names}_{uuid.uuid4().hex[:6]}"

    def sql(self, database: str | None, *statements: str, params: Sequence[Any] = ()) -> list:
        """Run ``statements`` as root on one autocommit connection; the last one's rows."""
        connection = self.connect(database)
        try:
            cursor = connection.cursor()
            rows: list = []
            for statement in statements:
                cursor.execute(statement, tuple(params) or None)
                rows = list(cursor.fetchall()) if cursor.description else []
            return rows
        finally:
            connection.close()

    def remote_url(self, name: str) -> tuple[str, Path]:
        path = self.base / "remotes" / name
        path.mkdir(parents=True, exist_ok=True)
        return f"file://{path}", path


# --- fault injection ------------------------------------------------------------------------------


class InjectedFault(Exception):
    """A driver-shaped error carrying a MySQL error number in ``args[0]``."""


@dataclass
class _Armed:
    kind: Literal["unknown", "unavailable"]
    on: str
    reason: UnknownReason = "commit"
    applied: bool = False


@dataclass
class FaultPlan:
    """Faults armed for the NEXT matching adapter method (FIFO), like the fake's ``FaultPlan``.

    ``active`` is the adapter method in progress, set by the harness's tracking proxies."""

    armed: list[_Armed] = field(default_factory=list)
    active: str | None = None

    def arm_unknown(self, method: str, reason: UnknownReason, *, applied: bool) -> None:
        self.armed.append(_Armed("unknown", method, reason, applied))

    def arm_unavailable(self, method: str) -> None:
        self.armed.append(_Armed("unavailable", method))

    def take(self, kind: str) -> _Armed | None:
        for index, fault in enumerate(self.armed):
            if fault.on == self.active and fault.kind == kind:
                return self.armed.pop(index)
        return None


def _error_for(reason: UnknownReason) -> BaseException:
    if reason == "deadline":
        return TimeoutError("injected: operation deadline exceeded")
    if reason == "transport":
        return InjectedFault(2013, "injected: lost connection awaiting the result")
    return InjectedFault(1180, "injected: error during COMMIT")


def _fire(fault: _Armed, real: Callable[[], Any], skip: Callable[[], Any]) -> None:
    if fault.applied:
        try:
            real()
        except Exception:  # noqa: BLE001, S110 - the fault decides what the adapter sees
            pass
    else:
        skip()
    raise _error_for(fault.reason)


_COMMIT_CALLS = ("CALL DOLT_COMMIT", "CALL DOLT_PUSH")


class _FaultingCursor:
    def __init__(self, cursor: Any, plan: FaultPlan) -> None:
        self._cursor = cursor
        self._plan = plan

    def execute(self, operation: str, parameters: Any = None) -> Any:
        if operation.lstrip().upper().startswith(_COMMIT_CALLS):
            fault = self._plan.take("unknown")
            if fault is not None:
                _fire(fault, lambda: self._cursor.execute(operation, parameters), lambda: None)
        return self._cursor.execute(operation, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _FaultingConnection:
    def __init__(self, connection: Any, plan: FaultPlan) -> None:
        self._connection = connection
        self._plan = plan

    def cursor(self) -> _FaultingCursor:
        return _FaultingCursor(self._connection.cursor(), self._plan)

    def commit(self) -> Any:
        fault = self._plan.take("unknown")
        if fault is not None:
            _fire(fault, self._connection.commit, self._connection.rollback)
        return self._connection.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class _FaultingEngine:
    """Wrap a fence engine: ``commit`` and ``push`` are the commit points; an armed
    ``unavailable`` fails the first statement batch or push, an armed ``unknown`` the commit or
    push (after or instead of the real one, as the fault says)."""

    def __init__(self, engine: SqlConnectionFenceEngine, plan: FaultPlan) -> None:
        self._engine = engine
        self._plan = plan

    def execute(self, statements: Sequence[str]) -> None:
        if self._plan.take("unavailable") is not None:
            raise InjectedFault(2003, "injected: engine unavailable")
        self._engine.execute(statements)

    def commit(self, message: str) -> bool:
        fault = self._plan.take("unknown")
        if fault is not None:
            _fire(fault, lambda: self._engine.commit(message), lambda: None)
        return self._engine.commit(message)

    def push(self) -> bool:
        if self._plan.take("unavailable") is not None:
            raise InjectedFault(2003, "injected: engine unavailable")
        fault = self._plan.take("unknown")
        if fault is not None:
            _fire(fault, self._engine.push, lambda: None)
        return self._engine.push()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._engine, name)


class _Tracked:
    """Proxy an adapter object so the fault plan knows which port method is running."""

    def __init__(self, target: Any, plan: FaultPlan) -> None:
        self._target = target
        self._plan = plan

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._target, name)
        if not callable(value) or name.startswith("_"):
            return value
        plan = self._plan

        def call(*args: Any, **kwargs: Any) -> Any:
            outer, plan.active = plan.active, name
            try:
                return value(*args, **kwargs)
            finally:
                plan.active = outer

        return call


def _swap_reachable(path: Path, reachable: bool) -> None:
    """An unreachable ``file://`` remote: the directory moved aside and a plain file in its
    place (Dolt fails fast with "operation not valid on a file"; it would create a missing one)."""
    away = path.with_name(path.name + ".away")
    if not reachable and not away.exists():
        path.rename(away)
        path.write_bytes(b"")
    elif reachable and away.exists():
        path.unlink()
        away.rename(path)


# --- the HQ harness ---------------------------------------------------------------------------


class DoltHqHarness:
    """``HqHarness`` over a fresh HQ database on ``server`` with its own ``file://`` remote."""

    clock = None

    def __init__(self, server: DoltSqlServer) -> None:
        self.server = server
        self.database = server.fresh_name("hq")
        self.faults = FaultPlan()
        server.sql(None, f"CREATE DATABASE {self.database}")
        self.remote, self._remote_path = server.remote_url(self.database)
        accounts = SqlDoltAccounts(connection_runner(self._raw_factory, Role.OPERATOR))
        accounts.provision_hq_schema(generation=str(uuid.uuid4()), author=_OPERATOR)
        server.sql(
            self.database,
            f"CALL DOLT_REMOTE('add', 'origin', {quote(self.remote)})",
            "CALL DOLT_PUSH('origin', 'main')",
        )
        self.store = DoltHqStorage(self._factory)
        tracked = _Tracked(self.store, self.faults)
        self.records = self.log = self.liveness = self.history = self.host = tracked
        self.placements = _Tracked(self.store.placements(), self.faults)
        self._peer: DoltHqStorage | None = None

    def _raw_factory(self, _role: Role) -> Any:
        return self.server.connect(self.database, autocommit=False)

    def _factory(self, _role: Role) -> Any:
        if self.faults.take("unavailable") is not None:
            raise InjectedFault(2003, "injected: cannot connect")
        return _FaultingConnection(self._raw_factory(_role), self.faults)

    def provision(self, kind: RecordKind, key: str, body: Mapping[str, Any]) -> Revision:
        return self.store.provision(kind, key, body)

    def inject_unknown(self, method: str, reason: UnknownReason, *, applied: bool) -> None:
        self.faults.arm_unknown(method, reason, applied=applied)

    def inject_unavailable(self, method: str) -> None:
        self.faults.arm_unavailable(method)

    def remote_publish(self, documents: Sequence[tuple[str, bytes]]) -> Revision:
        """A competing writer: a peer clone at the remote head publishes and pushes."""
        if self._peer is None:
            peer_db = self.database + "_peer"
            self.server.sql(None, f"CALL DOLT_CLONE({quote(self.remote)}, {quote(peer_db)})")
            self._peer = DoltHqStorage(lambda _role: self.server.connect(peer_db, autocommit=False))
            self._peer_db = peer_db
        self.server.sql(
            self._peer_db,
            "CALL DOLT_FETCH('origin')",
            "CALL DOLT_RESET('--hard', 'origin/main')",
        )
        tables = frozenset(name.partition("/")[0] for name, _ in documents)
        out = self._peer.publish(
            ChangeSet(tuple(documents), tables),
            expected_head=self._peer.head(),
            operation=_operation(),
            author="remote",
            message="competing",
        )
        pushed = self._peer.push()
        if out.__class__.__name__ != "Applied" or pushed.__class__.__name__ != "Applied":
            raise RuntimeError(f"competing publish failed: {out!r} / {pushed!r}")
        return out.revision  # type: ignore[union-attr]

    def set_remote_reachable(self, reachable: bool) -> None:
        _swap_reachable(self._remote_path, reachable)


# --- the fence harness ------------------------------------------------------------------------


def _fence_seed(cut_over: bool) -> list[str]:
    beads = ", ".join(
        f"({quote(bead)}, {quote(status)}, {quote(assignee)}, NULL)"
        for bead, (status, assignee) in BEADS_SEED.items()
    )
    labels = ", ".join(f"({quote(bead)}, {quote(label)})" for bead, label in sorted(LABELS_SEED))
    statements = [
        "CREATE TABLE issues (id VARCHAR(64) PRIMARY KEY, status VARCHAR(32) NOT NULL, "
        "assignee VARCHAR(255) NOT NULL DEFAULT '', started_at DATETIME NULL)",
        "CREATE TABLE labels (issue_id VARCHAR(64) NOT NULL, label VARCHAR(255) NOT NULL, "
        "PRIMARY KEY (issue_id, label))",
        "CREATE TABLE comments (id VARCHAR(64) PRIMARY KEY, issue_id VARCHAR(64) NOT NULL, "
        "author VARCHAR(255) NOT NULL, text TEXT NOT NULL, created_at DATETIME NOT NULL)",
        f"INSERT INTO issues VALUES {beads}",
        f"INSERT INTO labels VALUES {labels}",
    ]
    if cut_over:
        statements += [
            "CREATE TABLE bh_writer (id INT PRIMARY KEY, frame VARCHAR(64) NOT NULL, "
            "epoch BIGINT NOT NULL, revision VARCHAR(64) NOT NULL)",
            "CREATE TABLE bh_epoch_live (id INT PRIMARY KEY, epoch BIGINT NOT NULL, "
            "UNIQUE KEY bh_epoch_live_epoch (epoch))",
            "CREATE TABLE bh_write_mark (id VARCHAR(64) PRIMARY KEY, epoch BIGINT NOT NULL, "
            "tbl VARCHAR(64) NOT NULL DEFAULT '', CONSTRAINT bh_write_mark_epoch "
            "FOREIGN KEY (epoch) REFERENCES bh_epoch_live (epoch))",
            "INSERT INTO bh_writer VALUES (1, '', 0, '')",
            "INSERT INTO bh_epoch_live VALUES (1, 0)",
            # The contract's guard set: two monotonic triggers and a mark-table guard.
            "CREATE TRIGGER bh_writer_guard BEFORE UPDATE ON bh_writer FOR EACH ROW "
            "SET NEW.epoch = IF(NEW.epoch > OLD.epoch, NEW.epoch, NULL)",
            "CREATE TRIGGER bh_epoch_live_guard BEFORE UPDATE ON bh_epoch_live FOR EACH ROW "
            "SET NEW.epoch = IF(NEW.epoch > OLD.epoch, NEW.epoch, NULL)",
            "CREATE TRIGGER bh_write_mark_guard BEFORE INSERT ON bh_write_mark FOR EACH ROW "
            "SET NEW.tbl = COALESCE(NEW.tbl, '')",
        ]
    return [
        *statements,
        "CALL DOLT_COMMIT('-Am', 'seed hive', '--author', " + quote(_OPERATOR) + ")",
    ]


class DoltFenceHarness:
    """``FenceHarness`` over a fresh hive database (cut over or not) with a ``file://`` remote."""

    def __init__(self, server: DoltSqlServer, cut_over: bool = True) -> None:
        self.server = server
        self.database = server.fresh_name("hive")
        self.faults = FaultPlan()
        server.sql(None, f"CREATE DATABASE {self.database}")
        server.sql(self.database, *_fence_seed(cut_over))
        self.remote, self._remote_path = server.remote_url(self.database)
        server.sql(
            self.database,
            f"CALL DOLT_REMOTE('add', 'origin', {quote(self.remote)})",
            "CALL DOLT_PUSH('origin', 'main')",
        )
        engine = SqlConnectionFenceEngine(lambda: server.connect(self.database))
        self.fence_adapter = DoltHiveFence(
            _FaultingEngine(engine, self.faults), guards=FENCE_GUARDS
        )
        self.fence = _Tracked(self.fence_adapter, self.faults)
        self.epoch_ref = InMemoryEpochRef(("frame-dead", 1))  # a git ref: root's, not Dolt's
        self._peer: DoltHiveFence | None = None

    def snapshot_at(self, revision: Revision) -> FenceSnapshot:
        at = quote(str(revision))
        rows = self.server.sql(
            self.database, f"SELECT frame, epoch, revision FROM bh_writer AS OF {at}"
        )
        writer = WriterRow(str(rows[0][0]), int(rows[0][1]), str(rows[0][2])) if rows else None
        live = self.server.sql(self.database, f"SELECT epoch FROM bh_epoch_live AS OF {at}")
        marks = self.server.sql(self.database, f"SELECT id, epoch FROM bh_write_mark AS OF {at}")
        beads = self.server.sql(
            self.database, f"SELECT id, status, assignee FROM issues AS OF {at}"
        )
        labels = self.server.sql(self.database, f"SELECT issue_id, label FROM labels AS OF {at}")
        comments = self.server.sql(
            self.database,
            f"SELECT id, issue_id, author, text, created_at FROM comments AS OF {at}",
        )
        return FenceSnapshot(
            writer=writer,
            live_epoch=int(live[0][0]) if live else 0,
            marks=frozenset((str(m), int(e)) for m, e in marks),
            beads={str(b): (str(s), str(a)) for b, s, a in beads},
            labels=frozenset((str(b), str(label)) for b, label in labels),
            comments=frozenset(
                (str(c), str(b), str(a), str(t), aware(when).strftime(COMMENT_STAMP_FORMAT))
                for c, b, a, t, when in comments
            ),
        )

    def parent_of(self, revision: Revision) -> Revision | None:
        rows = self.server.sql(
            self.database,
            "SELECT parent_hash FROM dolt_commit_ancestors WHERE commit_hash = %s "
            "AND parent_index = 0",
            params=(str(revision),),
        )
        return Revision(str(rows[0][0])) if rows else None

    def remote_bump(self, bump: BumpRequest) -> Revision:
        """A competing node: a peer clone synced to the remote head bumps and pushes."""
        if self._peer is None:
            peer_db = self.database + "_peer"
            self.server.sql(None, f"CALL DOLT_CLONE({quote(self.remote)}, {quote(peer_db)})")
            engine = SqlConnectionFenceEngine(lambda: self.server.connect(peer_db))
            self._peer = DoltHiveFence(engine, guards=FENCE_GUARDS)
        self._peer.sync_to_remote()
        committed = self._peer.commit_bump(bump)
        if committed.__class__.__name__ != "Applied":
            raise RuntimeError(f"competing bump commit failed: {committed!r}")
        pushed = self._peer.push()
        if pushed.__class__.__name__ != "Applied":
            raise RuntimeError(f"competing bump push failed: {pushed!r}")
        return committed.revision  # type: ignore[union-attr]

    def inject_unknown(self, method: str, reason: UnknownReason, *, applied: bool) -> None:
        self.faults.arm_unknown(method, reason, applied=applied)

    def inject_unavailable(self, method: str) -> None:
        self.faults.arm_unavailable(method)

    def set_remote_reachable(self, reachable: bool) -> None:
        _swap_reachable(self._remote_path, reachable)


def _operation() -> Any:
    from beadhive_hq_storage import new_operation_id

    return new_operation_id()


def dolt_adapter(server: DoltSqlServer) -> ContractAdapter:
    """The ``dolt`` :class:`ContractAdapter` over ``server`` (fresh databases per harness)."""
    return ContractAdapter(
        id="dolt",
        make_hq=lambda: DoltHqHarness(server),
        make_fence=lambda cut_over: DoltFenceHarness(server, cut_over),
        clock_controlled=False,
    )
