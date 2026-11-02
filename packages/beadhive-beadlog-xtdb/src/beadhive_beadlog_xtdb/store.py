"""The bound XTDB store: the ``beadlog.store@1`` ``BeadlogStore`` this plugin provides.

Building it connects nothing. The first :meth:`XtdbBeadlogStore.loader` or
:meth:`~XtdbBeadlogStore.snapshot` resolves the password (through the injected source, which root
backs with the host's fnox broker) and opens one pgwire connection to the configured loopback
node; it is reused until :meth:`~XtdbBeadlogStore.close`.

- :meth:`~XtdbBeadlogStore.loader` is the ``beadlog_xtdb.XtdbStoreLoader`` (``auto`` mode: a fresh
  database takes a monotone initial load, a loaded one incremental wall-clock loads) behind a thin
  guard that turns the library's and the driver's failures into
  :class:`~beadhive.modules.beadlog_store.contracts.BeadlogStoreError`, so ``bh backfill`` reports
  them as one clean line. Refusals that are ``ValueError`` (``LoadRefused``) pass through as-is.
- :meth:`~XtdbBeadlogStore.snapshot` is the as-of reader (``beadlog_xtdb.read_state``) at the
  current belief, for one bead group, with the read's notes counted.

A non-loopback DSN is refused before any password is resolved (bh-cntr0: loopback only). No
message here repeats the password: driver text is cut to its first line and scrubbed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from beadhive.modules.beadlog_store.contracts import BeadlogStoreError, StoreSnapshot

from . import STORE_NAME
from .settings import XtdbSettings, is_loopback

#: ``connect(dsn, password) -> connection``; the default imports ``beadlog_xtdb`` lazily.
Connector = Callable[[str, "str | None"], Any]

CONNECT_TIMEOUT_SECONDS = 10


def default_connect(dsn: str, password: str | None) -> Any:
    """A configured autocommit connection (transit reads, typed dumpers) via ``beadlog_xtdb``."""
    from beadlog_xtdb import connect  # lazy: psycopg is imported only here

    params: dict[str, Any] = {"connect_timeout": CONNECT_TIMEOUT_SECONDS}
    if password:
        params["password"] = password
    return connect(dsn, autocommit=True, **params)


def scrub(text: str, secret: str | None) -> str:
    """The first line of ``text`` with ``secret`` (when set) masked."""
    line = (str(text).splitlines() or [""])[0]
    if secret:
        line = line.replace(secret, "***")
    return line


#: XTDB creates a table on its first insert, so a store that never held an edge, a label or a
#: comment has no such table and a ``SELECT`` from it fails ("Table not found"). The snapshot
#: asks which layout (b) tables exist and reads an absent one as empty.
TABLES_SQL = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
_PERIOD_COLUMNS = ("_id", "_valid_from", "_valid_to", "_system_from", "_system_to")


class _EmptyCursor:
    description = tuple(type("Column", (), {"name": name})() for name in _PERIOD_COLUMNS)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class _PresentTablesOnly:
    """A connection view that answers a ``SELECT ... FROM <absent table>`` with no rows."""

    def __init__(self, connection: Any, missing: frozenset[str]) -> None:
        self._connection = connection
        self._missing = missing

    def execute(self, sql: str, params: Any = None) -> Any:
        tail = sql.split(" FROM ", 1)
        if len(tail) == 2 and tail[1].split()[0] in self._missing:
            return _EmptyCursor()
        return self._connection.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class _GuardedLoader:
    """``XtdbStoreLoader`` with its non-``ValueError`` failures as ``BeadlogStoreError``."""

    def __init__(self, inner: Any, target: str, secret: Callable[[], str | None]) -> None:
        self._inner = inner
        self._target = target
        self._secret = secret

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def version(self) -> str:
        return self._inner.version

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(self._inner.capabilities)

    @property
    def target(self) -> str:
        return self._target

    def _translate(self, exc: Exception) -> BeadlogStoreError:
        return BeadlogStoreError(
            f"XTDB store {self._target}: {scrub(str(exc) or type(exc).__name__, self._secret())}"
        )

    def export(self) -> Iterable[Any]:
        try:
            return tuple(self._inner.export())
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - LoadAborted, version gate, driver errors
            raise self._translate(exc) from None

    def load(self, events: Sequence[Any], *, dry_run: bool) -> Mapping[str, Any]:
        try:
            return self._inner.load(events, dry_run=dry_run)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - LoadAborted, version gate, driver errors
            raise self._translate(exc) from None


class XtdbBeadlogStore:
    """The XTDB BeadLog store (module doc). ``connect`` is injected by tests."""

    def __init__(
        self,
        settings: XtdbSettings,
        password_source: Callable[[], str | None],
        *,
        connect: Connector | None = None,
    ) -> None:
        if not isinstance(settings, XtdbSettings):
            raise TypeError("settings must be XtdbSettings")
        if not callable(password_source):
            raise TypeError("password_source must be callable")
        self.settings = settings
        self._password_source = password_source
        self._connector = connect or default_connect
        self._endpoint = settings.endpoint()
        self._connection: Any = None
        self._password: str | None = None
        self._loader: _GuardedLoader | None = None

    @property
    def store(self) -> str:
        return STORE_NAME

    @property
    def target(self) -> str:
        return self._endpoint.label

    def _connect(self) -> Any:
        if self._connection is not None:
            return self._connection
        if not is_loopback(self._endpoint.host):
            raise BeadlogStoreError(
                f"plugins.beadlog-xtdb.dsn points at a non-loopback host ({self._endpoint.host}); "
                "bh reaches only a loopback XTDB node — publish it with -p 127.0.0.1:<port>:5432 "
                "and point the dsn at 127.0.0.1 (see `bh plugin beadlog-xtdb doctor`)"
            )
        self._password = self._password_source()
        try:
            self._connection = self._connector(self.settings.dsn, self._password)
        except Exception as exc:  # noqa: BLE001 - any driver failure is one clean refusal
            raise BeadlogStoreError(
                f"cannot connect to the XTDB node at {self.target} "
                f"({type(exc).__name__}: {scrub(str(exc), self._password)}) — is it running? "
                "check it with `bh plugin beadlog-xtdb doctor`"
            ) from None
        return self._connection

    def loader(self) -> _GuardedLoader:
        if self._loader is None:
            from beadlog_xtdb import XtdbStoreLoader  # lazy: psycopg is imported only here

            inner = XtdbStoreLoader(self._connect(), target=self.target)
            self._loader = _GuardedLoader(inner, self.target, lambda: self._password)
        return self._loader

    def snapshot(self, *, group: str, extracted_at: str) -> StoreSnapshot:
        from beadlog_xtdb import TABLES, read_state  # lazy: psycopg is imported only here

        connection = self._connect()
        try:
            present = {str(row[0]) for row in connection.execute(TABLES_SQL).fetchall()}
            if TABLES[0] not in present:  # nothing was ever loaded: no bead table yet
                return StoreSnapshot(self.target, (), {})
            missing = frozenset(TABLES) - present
            reading = _PresentTablesOnly(connection, missing) if missing else connection
            result = read_state(reading, extracted_at=extracted_at, group=group, groups=[group])
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - driver errors become one clean refusal
            raise BeadlogStoreError(
                f"XTDB store {self.target}: {scrub(str(exc) or type(exc).__name__, self._password)}"
            ) from None
        return StoreSnapshot(self.target, tuple(result.all_events), dict(result.note_codes()))

    def close(self) -> None:
        connection, self._connection, self._loader = self._connection, None, None
        self._password = None
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass


__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "TABLES_SQL",
    "Connector",
    "XtdbBeadlogStore",
    "default_connect",
    "scrub",
]
