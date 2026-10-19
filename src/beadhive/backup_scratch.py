"""Isolated restore of a native Dolt backup into a SCRATCH database, and its corpus facts
(bh-ksmt0.4; remote-migration-transaction-adr.md section 2, P3).

A backup is trusted only after it has been restored somewhere that is not the live database and
its fingerprint matches the source's. This module is that "somewhere":

* :class:`ServerScratch` -- server mode (the fleet's mode). ``CALL DOLT_BACKUP('restore', ...)``
  creates a uniquely named database on the same server; it is read with qualified names and
  dropped afterwards. Dolt does not delete a dropped database: it moves it to
  ``<data-dir>/.dolt_dropped_databases/`` so it can be undropped (bh-2hvmd). The copy is REPORTED
  (:class:`DroppedCopy`), never silently left and never purged unless a caller says so.
* :class:`EmbeddedScratch` -- embedded mode. ``dolt backup restore`` into a unique directory
  outside the live store; "dropping" is removing that directory, which leaves no soft-delete
  copy.

Both read facts through ONE function (:func:`read_corpus_facts`) so the source and the restored
copy are derived by the same queries (differing only in qualification), and both refuse a
database name or path that already exists: a scratch is never an overwrite.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import dolt_health, store_locator
from .corpus_fingerprint import (
    CorpusFacts,
    FingerprintError,
    FingerprintIncomplete,
)
from .dolt_remote import redact_url
from .run import run

#: A restore copies the whole database; bound it generously (seconds), never leave it unbounded.
RESTORE_TIMEOUT = 3600.0
QUERY_TIMEOUT = 120.0

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_DROPPED_DIR = ".dolt_dropped_databases"


class ScratchError(RuntimeError):
    """A scratch restore/read/drop failed. The message never contains a credential."""


@dataclass(frozen=True)
class DroppedCopy:
    """What a scratch drop left behind (bh-2hvmd). ``left_copy`` False means nothing remains."""

    database: str
    left_copy: bool
    location: str = ""  # the .dolt_dropped_databases/<db> path when it could be located
    located: bool = False
    size_bytes: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "database": self.database,
            "left_copy": self.left_copy,
            "location": self.location,
            "located": self.located,
            "size_bytes": self.size_bytes,
            "note": self.note,
        }


@dataclass(frozen=True)
class ScratchHandle:
    name: str
    path: str = ""  # embedded: the database directory; server: ""


class ScratchStore(Protocol):
    def restore(self, source_url: str, name: str) -> ScratchHandle: ...
    def query(self, handle: ScratchHandle, sql: str) -> list[dict]: ...
    def qualify(self, handle: ScratchHandle, table: str) -> str: ...
    def drop(self, handle: ScratchHandle) -> DroppedCopy: ...


_URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"]+")


def scrub(text: str) -> str:
    """Free text with every URL in it rendered through :func:`redact_url` (message-safe)."""
    return _URL_IN_TEXT.sub(lambda m: redact_url(m.group(0)), str(text))


def _quote(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def scratch_name(prefix: str, txn: str) -> str:
    """``<prefix>_scr_<txn>`` as a safe database identifier, unique per transaction."""
    base = store_locator.sanitize_database_name(f"{prefix}_scr_{txn}")
    name = base[:64]
    if not _IDENT.match(name):
        raise ScratchError(f"cannot derive a safe scratch database name from {base!r}")
    return name


def _size(path: Path) -> int:
    total = 0
    try:
        for child in path.rglob("*"):
            try:
                if child.is_file() and not child.is_symlink():
                    total += child.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


class ServerScratch:
    """Scratch database on the hive's own Dolt server (never a live hive's database)."""

    def __init__(
        self,
        engine,
        hive_dir: Path,
        *,
        data_dir: Path | None = None,
        restore_timeout: float = RESTORE_TIMEOUT,
    ):
        self._engine = engine
        self._hive_dir = Path(hive_dir)
        self._data_dir = data_dir
        self._timeout = restore_timeout

    def _sql(self, statement: str, *, timeout: float = QUERY_TIMEOUT):
        return self._engine.invoke(
            ["sql", statement], cwd=self._hive_dir, capture=True, timeout=timeout
        )

    def _databases(self) -> set[str]:
        try:
            rows = self._engine.query_rows(
                self._hive_dir, "SELECT schema_name AS n FROM information_schema.schemata"
            )
        except RuntimeError as exc:
            raise ScratchError(f"cannot list databases: {exc}") from exc
        return {str(r.get("n") or r.get("SCHEMA_NAME") or r.get("schema_name") or "") for r in rows}

    def restore(self, source_url: str, name: str) -> ScratchHandle:
        if not _IDENT.match(name):
            raise ScratchError(f"unsafe scratch database name {name!r}")
        if name in self._databases():
            raise ScratchError(f"scratch database {name!r} already exists; refusing to overwrite")
        res = self._sql(
            f"CALL DOLT_BACKUP('restore', {_quote(source_url)}, {_quote(name)})",
            timeout=self._timeout,
        )
        if res.returncode != 0:
            tail = ((res.stderr or res.stdout or "").strip().splitlines() or [""])[-1]
            raise ScratchError(
                f"restore of {redact_url(source_url)} into {name!r} failed: "
                f"{scrub(tail) or 'exit ' + str(res.returncode)}"
            )
        if name not in self._databases():
            raise ScratchError(f"restore reported success but database {name!r} does not exist")
        return ScratchHandle(name)

    def query(self, handle: ScratchHandle, sql: str) -> list[dict]:
        try:
            return self._engine.query_rows(self._hive_dir, sql)
        except RuntimeError as exc:
            raise ScratchError(str(exc)) from exc

    def qualify(self, handle: ScratchHandle, table: str) -> str:
        return f"`{handle.name}`.`{table}`"

    def _locate_data_dir(self) -> Path | None:
        if self._data_dir is not None:
            return self._data_dir
        try:
            rows = self._engine.query_rows(self._hive_dir, "SELECT @@datadir AS d")
        except RuntimeError:
            rows = []
        value = str((rows[0] if rows else {}).get("d") or "").strip()
        if value:
            return Path(value)
        # bd's shared-server data directory (a path fact, no probe).
        return store_locator.server_store_dir()

    def drop(self, handle: ScratchHandle) -> DroppedCopy:
        if not _IDENT.match(handle.name):
            raise ScratchError(f"unsafe scratch database name {handle.name!r}")
        res = self._sql(f"DROP DATABASE `{handle.name}`")
        if res.returncode != 0:
            tail = ((res.stderr or res.stdout or "").strip().splitlines() or [""])[-1]
            raise ScratchError(f"drop of scratch database {handle.name!r} failed: {tail}")
        if handle.name in self._databases():
            raise ScratchError(f"scratch database {handle.name!r} still exists after drop")
        data_dir = self._locate_data_dir()
        if data_dir is not None:
            copy = data_dir / _DROPPED_DIR / handle.name
            if copy.is_dir():
                return DroppedCopy(
                    handle.name,
                    True,
                    str(copy),
                    True,
                    _size(copy),
                    "Dolt keeps a dropped database for undrop; reclaim it deliberately "
                    "(bh-2hvmd). Nothing was purged.",
                )
        return DroppedCopy(
            handle.name,
            True,
            f"<server data dir>/{_DROPPED_DIR}/{handle.name}",
            False,
            0,
            "Dolt moves a dropped database to .dolt_dropped_databases (bh-2hvmd); the server "
            "data directory could not be located from here, so its size is unknown.",
        )


class EmbeddedScratch:
    """Scratch database directory under ``root`` (a unique subdirectory, never the live store)."""

    def __init__(self, root: Path, *, restore_timeout: float = RESTORE_TIMEOUT):
        self._root = Path(root)
        self._timeout = restore_timeout

    def _env(self) -> dict:
        """A private Dolt config home inside the scratch root, so ``dolt backup restore`` has a
        commit author on a host with no global Dolt identity -- and never writes to (or depends
        on) the operator's own Dolt configuration."""
        import os

        home = self._root / ".dolt-home"
        if not (home / ".dolt" / "config_global.json").is_file():
            home.mkdir(parents=True, exist_ok=True)
            env = {**os.environ, "DOLT_ROOT_PATH": str(home)}
            for key, value in (("user.name", "bh-backup-attest"), ("user.email", "bh@localhost")):
                run(
                    ["dolt", "config", "--global", "--add", key, value],
                    check=False,
                    capture=True,
                    env=env,
                    timeout=30,
                )
        return {**os.environ, "DOLT_ROOT_PATH": str(home)}

    def restore(self, source_url: str, name: str) -> ScratchHandle:
        if not _IDENT.match(name):
            raise ScratchError(f"unsafe scratch database name {name!r}")
        self._root.mkdir(parents=True, exist_ok=True)
        target = self._root / name
        if target.exists():
            raise ScratchError(f"scratch path {target} already exists; refusing to overwrite")
        res = run(
            ["dolt", "backup", "restore", source_url, name],
            cwd=str(self._root),
            check=False,
            capture=True,
            timeout=self._timeout,
            env=self._env(),
        )
        if res.returncode != 0:
            tail = ((res.stderr or res.stdout or "").strip().splitlines() or [""])[-1]
            raise ScratchError(
                f"restore of {redact_url(source_url)} into {target} failed: {scrub(tail)}"
            )
        if not (target / ".dolt").is_dir():
            raise ScratchError(f"restore reported success but {target} holds no Dolt database")
        return ScratchHandle(name, str(target))

    def query(self, handle: ScratchHandle, sql: str) -> list[dict]:
        import json

        res = run(
            ["dolt", "sql", "-q", sql, "-r", "json"],
            cwd=handle.path,
            check=False,
            capture=True,
            timeout=QUERY_TIMEOUT,
            env=self._env(),
        )
        if res.returncode != 0:
            tail = ((res.stderr or "").strip().splitlines() or [""])[-1]
            raise ScratchError(f"scratch query failed: {tail}")
        try:
            data = json.loads(res.stdout or "{}")
        except ValueError as exc:
            raise ScratchError("scratch query returned unparseable output") from exc
        rows = data.get("rows", []) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ScratchError("scratch query returned an unexpected shape")
        return [r for r in rows if isinstance(r, dict)]

    def qualify(self, handle: ScratchHandle, table: str) -> str:
        return f"`{table}`"

    def drop(self, handle: ScratchHandle) -> DroppedCopy:
        path = Path(handle.path)
        if path.parent != self._root or path.name != handle.name:
            raise ScratchError(f"refusing to remove {path}: not a scratch directory of this run")
        shutil.rmtree(path)
        if path.exists():
            raise ScratchError(f"scratch directory {path} still exists after removal")
        return DroppedCopy(
            handle.name,
            False,
            note="embedded scratch removed outright; no .dolt_dropped_databases copy exists",
        )


def make_scratch(engine, hive_dir: Path, set_dir: Path, txn: str) -> ScratchStore:
    """The scratch backend for ``hive_dir``'s engine mode. Embedded scratch lives inside the
    new, unique backup set (``<set>/.scratch-<txn>``); server scratch is a database."""
    if store_locator.is_embedded_mode(Path(hive_dir)):
        return EmbeddedScratch(Path(set_dir) / f".scratch-{txn}")
    return ServerScratch(engine, hive_dir)


def cleanup_embedded_root(set_dir: Path, txn: str) -> None:
    root = Path(set_dir) / f".scratch-{txn}"
    home = root / ".dolt-home"
    if home.is_dir():
        shutil.rmtree(home, ignore_errors=True)
    if root.is_dir() and not any(root.iterdir()):
        root.rmdir()


# ---- corpus facts: one reader for the live source and the restored copy ----------------------

Query = Callable[[str], list[dict]]
Qualify = Callable[[str], str]

_IDENT_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _first(rows: Sequence[dict], key: str, default=None):
    if not rows:
        return default
    row = rows[0]
    for candidate in (key, key.upper(), key.lower()):
        if candidate in row:
            return row[candidate]
    values = list(row.values())
    return values[0] if values else default


def read_corpus_facts(
    query: Query,
    qualify: Qualify,
    *,
    database: str | None,
    tables: Sequence[str],
    schema_version: int | None,
) -> CorpusFacts:
    """Facts of ONE database. ``database`` is the schema name to filter ``information_schema``
    (``None``: the connection's current database). ``schema_version`` is supplied by the caller
    because the live source uses the ADR's real-store probe while a restored copy is read in
    place; ``None`` makes the fingerprint refuse (:class:`FingerprintIncomplete`)."""
    branches = query(f"SELECT name, hash FROM {qualify('dolt_branches')}")
    main = next((b for b in branches if str(b.get("name")) == "main"), None)
    where = f"table_schema = {_quote(database)}" if database else "table_schema = DATABASE()"
    real = {
        str(r.get("table_name") or r.get("TABLE_NAME"))
        for r in query(f"SELECT table_name FROM information_schema.tables WHERE {where}")
    }
    issues = query(f"SELECT * FROM {qualify('issues')} ORDER BY id")
    prefix_rows = query(f"SELECT value FROM {qualify('config')} WHERE `key` = 'issue_prefix'")
    meta = query(f"SELECT value FROM {qualify('metadata')} WHERE `key` = '_project_id'")
    out: dict = {}
    for table in tables:
        if not _IDENT_TABLE.match(table):
            raise FingerprintError(f"invalid table name {table!r}")
        out[table] = query(f"SELECT * FROM {qualify(table)}") if table in real else None
    return CorpusFacts(
        project_id=str(_first(meta, "value", "") or ""),
        prefix=str(_first(prefix_rows, "value", "") or ""),
        schema_version=schema_version,
        main_head=str(main.get("hash") or "") if main else "",
        branches={str(b["name"]): str(b["hash"]) for b in branches},
        issue_rows={str(r["id"]): r for r in issues},
        tables=out,
    )


def record_counts(query: Query, qualify: Qualify, *, database: str | None) -> int:
    """Rows a full ``bd export --all`` emits as issue records: ``issues`` plus ``wisps``."""
    where = f"table_schema = {_quote(database)}" if database else "table_schema = DATABASE()"
    real = {
        str(r.get("table_name") or r.get("TABLE_NAME"))
        for r in query(f"SELECT table_name FROM information_schema.tables WHERE {where}")
    }
    total = 0
    for table in ("issues", "wisps"):
        if table in real:
            total += int(_first(query(f"SELECT COUNT(*) AS c FROM {qualify(table)}"), "c", 0) or 0)
    return total


def scratch_schema_version(query: Query, qualify: Qualify) -> int | None:
    rows = query(f"SELECT MAX(version) AS max_version FROM {qualify('schema_migrations')}")
    value = _first(rows, "max_version")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def source_schema_version(hive_dir: Path) -> int | None:
    """The ADR's real-store probe (never ``bd dolt status --json``)."""
    return dolt_health.probe_raw_schema_version(
        Path(hive_dir), dolt_mode=store_locator.dolt_mode(Path(hive_dir))
    ).version


def source_facts(engine, hive_dir: Path, tables: Sequence[str]) -> CorpusFacts:
    version = source_schema_version(hive_dir)
    if version is None:
        raise FingerprintIncomplete(
            "schema_version could not be determined from the store (dolt_health."
            "probe_raw_schema_version); refusing to fingerprint without it"
        )
    return read_corpus_facts(
        lambda q: engine.query_rows(hive_dir, q),
        lambda t: f"`{t}`",
        database=None,
        tables=tables,
        schema_version=version,
    )


def scratch_facts(
    scratch: ScratchStore, handle: ScratchHandle, tables: Sequence[str]
) -> tuple[CorpusFacts, int]:
    """``(facts, export-record count)`` of a restored scratch database."""
    query = lambda q: scratch.query(handle, q)  # noqa: E731
    qualify = lambda t: scratch.qualify(handle, t)  # noqa: E731
    db = handle.name if not handle.path else None
    version = scratch_schema_version(query, qualify)
    if version is None:
        raise FingerprintIncomplete("restored copy has no readable schema_migrations version")
    facts = read_corpus_facts(query, qualify, database=db, tables=tables, schema_version=version)
    return facts, record_counts(query, qualify, database=db)
