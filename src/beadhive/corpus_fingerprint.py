"""Corpus fingerprint ``bh.corpus-fingerprint/2`` (ADR section 4).

A canonical, key-sorted document reduced to a sha256, with every component also kept so a
mismatch reports WHICH component differed. Two tiers:

* ``strong``: includes ``main_head`` and ``branches``; required when the target is hydrated from
  the same history (relocation).
* ``corpus``: omits the commit hashes; the only meaningful tier for a JSONL-derived or
  re-initialised store (engine-mode migration, JSONL restore).

The tier is recorded beside the digest and comparing across tiers is REFUSED. A store whose real
schema version cannot be determined cannot be fingerprinted (a gate that cannot fail is a defect).

Every value is CANONICALISED before hashing (:func:`canonical_value`) so one database fingerprints
identically whether it is read through ``bd sql --json`` (server engine mode) or ``dolt sql -r
json`` (embedded): the two render DATETIME, JSON columns and the like differently (bh-m4ups). The
strong tier also carries Dolt-native ``dolt_hashof_db`` / ``dolt_hashof_table`` hashes, which are
independent of any rendering, as a cross-check (``Fingerprint.native``). They sit beside, not in,
the digest because a scratch database on a shared server cannot be asked for them.

Collection is behind the :class:`CorpusReader` port. :class:`BdSqlCorpusReader` is the minimal
implementation over ``bd sql``; bh-ksmt0.3's below-bd inspection can supply another.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from . import dolt_health, fleet, store_locator
from .migration_ledger import canonical, digest_of

SCHEMA = "bh.corpus-fingerprint/2"
#: Schemas that existed before and can no longer be compared with the current one.
OLD_SCHEMAS = ("bh.corpus-fingerprint/1",)
TIER_STRONG = "strong"
TIER_CORPUS = "corpus"
TIERS = (TIER_STRONG, TIER_CORPUS)

#: ADR section 9 ``migration.fingerprint.tables`` default.
DEFAULT_TABLES = ("dependencies", "labels", "comments", "events", "config", "metadata")
#: Named exclusions, never silent ones.
EXCLUDED = (
    "dolt-transport-cache",
    "local-only-files",
    "volatile-caches",
    "workspace-config-yaml",
)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class FingerprintError(Exception):
    pass


class FingerprintTierMismatch(FingerprintError):
    """Fingerprints of different tiers were compared (refused)."""


class FingerprintVersionMismatch(FingerprintError):
    """Fingerprints of different schema versions were compared or loaded (refused)."""


class FingerprintIncomplete(FingerprintError):
    """A component could not be derived; no fingerprint is better than a hollow one."""


def configured_tables(cfg: Mapping | None) -> tuple[str, ...]:
    """``migration.fingerprint.tables`` (extra tables), added to the defaults, order-stable."""
    extra = ((cfg or {}).get("migration") or {}).get("fingerprint", {}).get("tables") or ()
    out = list(DEFAULT_TABLES)
    out += [t for t in extra if t not in out]
    return tuple(out)


@dataclass(frozen=True)
class CorpusFacts:
    """Raw facts a reader returns; digests are computed here, not by the reader."""

    project_id: str
    prefix: str
    schema_version: int | None
    main_head: str
    branches: Mapping[str, str]
    issue_rows: Mapping[str, Mapping]  # id -> row
    # table -> rows, or None when the table does not exist in the real schema
    tables: Mapping[str, Sequence[Mapping] | None] = field(default_factory=dict)
    # ``{"db": <dolt_hashof_db>, "tables": {table: <dolt_hashof_table>}}``, or None when the
    # reader cannot ask Dolt (a scratch database on a shared server).
    native: Mapping | None = None


class CorpusReader(Protocol):
    def read(self, hive_dir: Path, tables: Sequence[str]) -> CorpusFacts: ...


_DATETIME = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(?:Z|\+00:?00)?$")


def canonical_value(value):
    """One engine-independent form of a single cell value.

    * DATETIME/TIMESTAMP strings (``2026-09-23 20:38:43`` embedded, ``2026-09-23T20:38:43Z``
      server) become ``YYYY-MM-DDTHH:MM:SS[.ffffff]Z``; Dolt stores them zone-less in UTC.
    * JSON columns (a parsed object/array embedded; Dolt's compact text on the server) become
      key-sorted compact JSON text. Text that merely looks like JSON but is not byte-for-byte
      Dolt's compact rendering is left verbatim, so a text column's whitespace stays significant.
    * bools become 0/1 (tinyint(1) is 0/1 in both modes already); integral floats become ints.
    * ``None`` stays distinct from the empty string (but see :func:`canonical_row`).
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, (dict, list)):
        return json.dumps(
            _canonical_json(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    if isinstance(value, str):
        m = _DATETIME.match(value)
        if m:
            day, clock, frac = m.groups()
            frac = (frac or "").rstrip("0")
            return f"{day}T{clock}{'.' + frac if frac else ''}Z"
        if value[:1] in ("{", "["):
            try:
                parsed = json.loads(value)
            except ValueError:
                return value
            if json.dumps(parsed, separators=(",", ":"), ensure_ascii=False) == value:
                return canonical_value(parsed)
    return value


def _canonical_json(value):
    if isinstance(value, dict):
        return {k: _canonical_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_canonical_json(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def canonical_row(row: Mapping) -> dict:
    """A row with every value canonicalised and NULL columns dropped: ``bd sql --json`` renders a
    NULL as ``"col": null`` while ``dolt sql -r json`` omits the key. A NULL therefore equals an
    absent column but stays distinct from the empty string, which both modes render as ``""``."""
    return {k: canonical_value(v) for k, v in row.items() if v is not None}


def _row_digest(row: Mapping) -> str:
    return digest_of(canonical_row(row))


def _table_component(rows: Sequence[Mapping] | None) -> dict:
    if rows is None:
        return {"present": False}
    return {
        "present": True,
        "rows": len(rows),
        "digest": digest_of(sorted(_row_digest(r) for r in rows)),
    }


@dataclass(frozen=True)
class Fingerprint:
    tier: str
    components: Mapping
    excluded: tuple[str, ...]
    digest: str
    schema: str = SCHEMA
    native: Mapping | None = None

    def to_dict(self) -> dict:
        out = {
            "schema": self.schema,
            "tier": self.tier,
            "components": dict(self.components),
            "excluded": list(self.excluded),
            "digest": self.digest,
        }
        if self.native is not None:
            out["native"] = dict(self.native)
        return out

    @classmethod
    def from_dict(cls, raw: Mapping) -> Fingerprint:
        if raw.get("schema") != SCHEMA:
            raise _version_error(raw.get("schema"))
        fp = _build(raw["tier"], raw["components"], tuple(raw.get("excluded", ())))
        fp = replace(fp, native=raw.get("native"))
        if fp.digest != raw.get("digest"):
            raise FingerprintError("fingerprint digest does not match its components (edited?)")
        return fp


def _version_error(found) -> FingerprintError:
    if found in OLD_SCHEMAS:
        return FingerprintVersionMismatch(
            f"fingerprint schema {found!r} predates {SCHEMA!r}, which canonicalises values "
            "(DATETIME, JSON) before hashing; its digests cannot be compared with current ones. "
            "Take a new fingerprint or snapshot rather than comparing across versions"
        )
    return FingerprintError(f"unsupported fingerprint schema {found!r}")


def _build(tier: str, components: Mapping, excluded: tuple[str, ...]) -> Fingerprint:
    if tier not in TIERS:
        raise FingerprintError(f"unknown tier {tier!r}")
    doc = {
        "schema": SCHEMA,
        "tier": tier,
        "components": dict(components),
        "excluded": list(excluded),
    }
    return Fingerprint(tier, dict(components), excluded, digest_of(doc))


def compute(
    facts: CorpusFacts, tier: str, *, tables: Sequence[str] = DEFAULT_TABLES
) -> Fingerprint:
    if tier not in TIERS:
        raise FingerprintError(f"unknown tier {tier!r}")
    if facts.schema_version is None:
        raise FingerprintIncomplete(
            "schema_version could not be determined from the store (dolt_health."
            "probe_raw_schema_version); refusing to fingerprint without it"
        )
    ids = sorted(facts.issue_rows)
    comp: dict = {
        "project_id": facts.project_id,
        "prefix": facts.prefix,
        "schema_version": facts.schema_version,
        "issue_count": len(ids),
        "issue_ids_digest": digest_of(ids),
        "issue_rows_digest": digest_of({i: _row_digest(facts.issue_rows[i]) for i in ids}),
        "tables": {t: _table_component(facts.tables.get(t)) for t in sorted(tables)},
    }
    if tier == TIER_STRONG:
        if not facts.main_head:
            raise FingerprintIncomplete("strong tier needs main_head, which was not readable")
        comp["main_head"] = facts.main_head
        comp["branches"] = dict(sorted(facts.branches.items()))
    fp = _build(tier, comp, EXCLUDED)
    if tier == TIER_STRONG and facts.native is not None:
        fp = replace(fp, native=_native_component(facts.native, tables))
    return fp


def _native_component(native: Mapping, tables: Sequence[str]) -> dict:
    """Only the hashes of the fingerprinted tables (plus ``issues``) and the database."""
    wanted = {"issues", *tables}
    per_table = native.get("tables") or {}
    return {
        "db": native.get("db"),
        "tables": {t: per_table[t] for t in sorted(wanted) if t in per_table},
    }


def fingerprint(
    reader: CorpusReader,
    hive_dir: Path,
    tier: str,
    *,
    tables: Sequence[str] = DEFAULT_TABLES,
) -> Fingerprint:
    return compute(reader.read(Path(hive_dir), tuple(tables)), tier, tables=tables)


@dataclass(frozen=True)
class FingerprintDiff:
    equal: bool
    tier: str
    differing: tuple[str, ...]  # component paths, e.g. "main_head", "tables.labels"
    detail: Mapping[str, tuple]  # path -> (left, right)


def compare(left: Fingerprint, right: Fingerprint) -> FingerprintDiff:
    if left.schema != right.schema:
        raise FingerprintVersionMismatch(
            f"refusing to compare fingerprint schema {left.schema!r} with {right.schema!r}; "
            "their digests are computed over differently canonicalised values. Re-take the "
            "older fingerprint with the current tool"
        )
    if left.tier != right.tier:
        raise FingerprintTierMismatch(
            f"refusing to compare a {left.tier!r} fingerprint with a {right.tier!r} one; take "
            "both at the tier the transaction requires (strong: same history; corpus: re-init)"
        )
    detail: dict[str, tuple] = {}

    def walk(path: str, a, b) -> None:
        if isinstance(a, Mapping) and isinstance(b, Mapping):
            for key in sorted(set(a) | set(b)):
                walk(f"{path}.{key}" if path else key, a.get(key), b.get(key))
        elif a != b:
            detail[path] = (a, b)

    walk("", left.components, right.components)
    equal = left.digest == right.digest
    # Dolt-native hashes are compared only when BOTH sides could produce them; one-sided
    # absence is not a difference (it is a weaker check, never a false mismatch).
    if left.native is not None and right.native is not None:
        walk("dolt_native", left.native, right.native)
        equal = equal and not any(k.startswith("dolt_native") for k in detail)
    return FingerprintDiff(equal, left.tier, tuple(detail), detail)


# ---- minimal reader over `bd sql` ----------------------------------------------------------

SqlFn = Callable[[Path, str], list[dict]]


class SqlError(FingerprintIncomplete):
    pass


def bd_sql(hive_dir: Path, query: str, *, timeout: float = 60.0) -> list[dict]:
    """Read-only ``bd sql --json`` query returning row dicts, via the shared ``fleet.sql`` seam."""
    res = fleet.sql(hive_dir, query, timeout=timeout)
    if res.returncode != 0:
        tail = (res.stderr or "").strip().splitlines()
        raise SqlError(f"bd sql failed ({tail[-1] if tail else res.returncode}): {query[:60]}")
    data = fleet.sql_rows(res)
    if isinstance(data, dict):
        data = data.get("rows")
    if not isinstance(data, list):
        raise SqlError(f"unexpected bd sql shape for: {query[:60]}")
    return [r for r in data if isinstance(r, dict)]


def native_hashes(query: Callable[[str], list[dict]], tables: Sequence[str]) -> dict:
    """Dolt-native, rendering-independent content hashes of the session's CURRENT database:
    ``dolt_hashof_db()`` and ``dolt_hashof_table(t)`` per table. ``query`` returns row dicts."""

    def one(sql: str) -> str:
        rows = query(sql)
        if not rows or not rows[0]:
            raise FingerprintIncomplete(f"no result for {sql[:60]}")
        return str(next(iter(rows[0].values())) or "")

    per_table: dict = {}
    for table in dict.fromkeys(tables):
        if not _IDENT.match(table):
            raise FingerprintError(f"invalid table name {table!r}")
        per_table[table] = one(f"SELECT dolt_hashof_table('{table}') AS h")
    return {"db": one("SELECT dolt_hashof_db() AS h"), "tables": per_table}


class BdSqlCorpusReader:
    """Minimal :class:`CorpusReader` over a running server. ``sql`` is injectable for tests."""

    def __init__(self, sql: SqlFn = bd_sql, *, probe=dolt_health.probe_raw_schema_version):
        self._sql = sql
        self._probe = probe

    def read(self, hive_dir: Path, tables: Sequence[str]) -> CorpusFacts:
        sql = self._sql
        schema = self._probe(hive_dir, dolt_mode=store_locator.dolt_mode(hive_dir)).version
        head = sql(hive_dir, "SELECT HASHOF('main') AS h")
        branches = sql(hive_dir, "SELECT name, hash FROM dolt_branches")
        real = {
            str(r.get("table_name") or r.get("TABLE_NAME"))
            for r in sql(
                hive_dir,
                "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()",
            )
        }
        issues = sql(hive_dir, "SELECT * FROM issues ORDER BY id")
        prefix_rows = sql(hive_dir, "SELECT value FROM config WHERE `key` = 'issue_prefix'")
        out: dict = {}
        for table in tables:
            if not _IDENT.match(table):
                raise FingerprintError(f"invalid table name {table!r}")
            out[table] = sql(hive_dir, f"SELECT * FROM `{table}`") if table in real else None
        return CorpusFacts(
            project_id=store_locator.project_id(hive_dir),
            prefix=str(prefix_rows[0].get("value", "")) if prefix_rows else "",
            schema_version=schema,
            main_head=str(head[0].get("h", "")) if head else "",
            branches={str(b["name"]): str(b["hash"]) for b in branches},
            issue_rows={str(r["id"]): r for r in issues},
            tables=out,
            native=native_hashes(
                lambda q: sql(hive_dir, q),
                [
                    t
                    for t in dict.fromkeys(("issues", *tables))
                    if t == "issues" or out.get(t) is not None
                ],
            ),
        )


__all__ = [
    "SCHEMA",
    "CorpusFacts",
    "CorpusReader",
    "BdSqlCorpusReader",
    "Fingerprint",
    "FingerprintDiff",
    "FingerprintVersionMismatch",
    "canonical_row",
    "canonical_value",
    "native_hashes",
    "canonical",
    "compare",
    "compute",
    "fingerprint",
    "configured_tables",
]
