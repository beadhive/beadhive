"""Corpus fingerprint ``bh.corpus-fingerprint/1`` (ADR section 4).

A canonical, key-sorted document reduced to a sha256, with every component also kept so a
mismatch reports WHICH component differed. Two tiers:

* ``strong``: includes ``main_head`` and ``branches``; required when the target is hydrated from
  the same history (relocation).
* ``corpus``: omits the commit hashes; the only meaningful tier for a JSONL-derived or
  re-initialised store (engine-mode migration, JSONL restore).

The tier is recorded beside the digest and comparing across tiers is REFUSED. A store whose real
schema version cannot be determined cannot be fingerprinted (a gate that cannot fail is a defect).

Collection is behind the :class:`CorpusReader` port. :class:`BdSqlCorpusReader` is the minimal
implementation over ``bd sql``; bh-ksmt0.3's below-bd inspection can supply another.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import dolt_health, fleet, store_locator
from .migration_ledger import canonical, digest_of

SCHEMA = "bh.corpus-fingerprint/1"
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


class CorpusReader(Protocol):
    def read(self, hive_dir: Path, tables: Sequence[str]) -> CorpusFacts: ...


def _row_digest(row: Mapping) -> str:
    return digest_of(dict(row))


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

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "tier": self.tier,
            "components": dict(self.components),
            "excluded": list(self.excluded),
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, raw: Mapping) -> Fingerprint:
        if raw.get("schema") != SCHEMA:
            raise FingerprintError(f"unsupported fingerprint schema {raw.get('schema')!r}")
        fp = _build(raw["tier"], raw["components"], tuple(raw.get("excluded", ())))
        if fp.digest != raw.get("digest"):
            raise FingerprintError("fingerprint digest does not match its components (edited?)")
        return fp


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
    return _build(tier, comp, EXCLUDED)


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
    return FingerprintDiff(left.digest == right.digest, left.tier, tuple(detail), detail)


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
        )


__all__ = [
    "SCHEMA",
    "CorpusFacts",
    "CorpusReader",
    "BdSqlCorpusReader",
    "Fingerprint",
    "FingerprintDiff",
    "canonical",
    "compare",
    "compute",
    "fingerprint",
    "configured_tables",
]
