"""Corpus fingerprint ``bh.corpus-fingerprint/3`` (ADR section 4; ``/2`` is still read).

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

Two SCOPES (bh-omxyj). Dolt's ``dolt_ignore`` marks tables (bd: ``events``, ``wisps``, ``leases``,
``bh_local_%`` ...) that live only in the working set: never committed, pushed or cloned.

* ``committed`` (the default; relocation: clone-vs-source, P6/P8): reads AS OF the branch head,
  digests only non-ignored tables, and carries the committed root hash (``dolt_hashof_db('main')``)
  in place of the working-set ``dolt_hashof_db()``. Ignored tables are REPORTED beside the digest
  (``Fingerprint.local_only``: names, row counts, optional digests) but never compared.
* ``working`` (native backups: take/verify/restore): a native backup carries the working set, so
  the requested ignored tables are digested too (``components.local_only``) and a drift in them
  fails the comparison. Comparing across scopes is refused.

Ignored tables are discovered from the store's ``dolt_ignore`` patterns, never a hard-coded list.

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

SCHEMA = "bh.corpus-fingerprint/3"
#: ``/2`` is still read (and re-computed on request) so existing backup manifests stay verifiable:
#: it digests every requested table from the working set (the ``working`` scope, no local split).
LEGACY_SCHEMA = "bh.corpus-fingerprint/2"
READABLE_SCHEMAS = (SCHEMA, LEGACY_SCHEMA)
#: Schemas that existed before and can no longer be compared with the current one.
OLD_SCHEMAS = ("bh.corpus-fingerprint/1",)
SCOPE_COMMITTED = "committed"
SCOPE_WORKING = "working"
SCOPES = (SCOPE_COMMITTED, SCOPE_WORKING)
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


class FingerprintScopeMismatch(FingerprintError):
    """Fingerprints of different scopes were compared (refused)."""


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


def configured_local_digests(cfg: Mapping | None) -> bool:
    """``migration.fingerprint.local_digests`` (default off): also digest the local-only tables in
    the committed-scope report. Off by default because it reads every ignored row (``events`` is
    large); the working scope always digests the requested ones."""
    node = ((cfg or {}).get("migration") or {}).get("fingerprint", {})
    return bool(node.get("local_digests", False))


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
    # ``{"db": <dolt_hashof_db>, "root": <dolt_hashof_db('main')>, "tables": {t: <hash>}}``
    native: Mapping | None = None
    # Tables ``dolt_ignore`` marks (existing ones plus any requested one a pattern matches).
    ignored: Sequence[str] = ()
    # Existing ignored table -> row count, for the local-only report.
    local_counts: Mapping[str, int] = field(default_factory=dict)


class CorpusReader(Protocol):
    def read(
        self,
        hive_dir: Path,
        tables: Sequence[str],
        *,
        scope: str = SCOPE_COMMITTED,
        local_digests: bool = False,
    ) -> CorpusFacts: ...


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
    scope: str = SCOPE_COMMITTED
    #: Local-only (dolt_ignore'd) tables, reported beside the digest: name -> {rows[, digest]}.
    #: In the working scope the requested ones are ALSO in ``components["local_only"]``.
    local_only: Mapping | None = None

    def to_dict(self) -> dict:
        out = {
            "schema": self.schema,
            "tier": self.tier,
            "components": dict(self.components),
            "excluded": list(self.excluded),
            "digest": self.digest,
        }
        if self.schema != LEGACY_SCHEMA:
            out["scope"] = self.scope
        if self.native is not None:
            out["native"] = dict(self.native)
        if self.local_only is not None:
            out["local_only"] = dict(self.local_only)
        return out

    @property
    def tables(self) -> tuple[str, ...]:
        """Every table this fingerprint was computed over (committed and digested local-only)."""
        comp = self.components
        return tuple(sorted({*comp.get("tables", {}), *comp.get("local_only", {})}))

    @classmethod
    def from_dict(cls, raw: Mapping) -> Fingerprint:
        schema = raw.get("schema")
        if schema not in READABLE_SCHEMAS:
            raise _version_error(schema)
        # a /2 document predates scopes and digested the working set: it IS the working scope
        scope = SCOPE_WORKING if schema == LEGACY_SCHEMA else raw.get("scope")
        fp = _build(raw["tier"], raw["components"], tuple(raw.get("excluded", ())), schema, scope)
        fp = replace(fp, native=raw.get("native"), local_only=raw.get("local_only"))
        if fp.digest != raw.get("digest"):
            raise FingerprintError("fingerprint digest does not match its components (edited?)")
        return fp


def _version_error(found) -> FingerprintError:
    if found in OLD_SCHEMAS:
        return FingerprintVersionMismatch(
            f"fingerprint schema {found!r} predates {LEGACY_SCHEMA!r}, which canonicalises values "
            "(DATETIME, JSON) before hashing; its digests cannot be compared with current ones. "
            "Take a new fingerprint or snapshot rather than comparing across versions"
        )
    return FingerprintError(f"unsupported fingerprint schema {found!r}")


def _build(
    tier: str,
    components: Mapping,
    excluded: tuple[str, ...],
    schema: str = SCHEMA,
    scope: str = SCOPE_COMMITTED,
) -> Fingerprint:
    if tier not in TIERS:
        raise FingerprintError(f"unknown tier {tier!r}")
    if scope not in SCOPES:
        raise FingerprintError(f"unknown scope {scope!r}")
    doc = {
        "schema": schema,
        "tier": tier,
        "components": dict(components),
        "excluded": list(excluded),
    }
    if schema != LEGACY_SCHEMA:
        doc["scope"] = scope
    return Fingerprint(tier, dict(components), excluded, digest_of(doc), schema, scope=scope)


def compute(
    facts: CorpusFacts,
    tier: str,
    *,
    tables: Sequence[str] = DEFAULT_TABLES,
    scope: str = SCOPE_COMMITTED,
    schema: str = SCHEMA,
) -> Fingerprint:
    """Fingerprint ``facts``. ``scope`` picks what the digest covers (module docstring); ``schema``
    re-computes in an older readable schema so a stored ``/2`` manifest can be re-verified."""
    if tier not in TIERS:
        raise FingerprintError(f"unknown tier {tier!r}")
    if scope not in SCOPES:
        raise FingerprintError(f"unknown scope {scope!r}")
    if schema not in READABLE_SCHEMAS:
        raise _version_error(schema)
    legacy = schema == LEGACY_SCHEMA
    if legacy:
        scope = SCOPE_WORKING
    if facts.schema_version is None:
        raise FingerprintIncomplete(
            "schema_version could not be determined from the store (dolt_health."
            "probe_raw_schema_version); refusing to fingerprint without it"
        )
    ignored = set() if legacy else set(facts.ignored)
    committed = [t for t in sorted(tables) if t not in ignored]
    local = [t for t in sorted(tables) if t in ignored]
    ids = sorted(facts.issue_rows)
    comp: dict = {
        "project_id": facts.project_id,
        "prefix": facts.prefix,
        "schema_version": facts.schema_version,
        "issue_count": len(ids),
        "issue_ids_digest": digest_of(ids),
        "issue_rows_digest": digest_of({i: _row_digest(facts.issue_rows[i]) for i in ids}),
        "tables": {t: _table_component(facts.tables.get(t)) for t in committed},
    }
    if scope == SCOPE_WORKING and not legacy:
        comp["local_only"] = {t: _table_component(facts.tables.get(t)) for t in local}
    if tier == TIER_STRONG:
        if not facts.main_head:
            raise FingerprintIncomplete("strong tier needs main_head, which was not readable")
        comp["main_head"] = facts.main_head
        comp["branches"] = dict(sorted(facts.branches.items()))
    excluded = EXCLUDED if scope == SCOPE_WORKING else (*EXCLUDED, "dolt-ignored-tables")
    fp = _build(tier, comp, excluded, schema, scope)
    if tier == TIER_STRONG and facts.native is not None:
        fp = replace(fp, native=_native_component(facts.native, committed, local, scope, legacy))
    if not legacy:
        fp = replace(fp, local_only=_local_report(facts, local))
    return fp


def compute_like(
    facts: CorpusFacts, want: Fingerprint, *, tables: Sequence[str] | None = None
) -> Fingerprint:
    """Compute ``facts`` in ``want``'s tier, scope and schema, so the two are comparable."""
    return compute(
        facts,
        want.tier,
        tables=tuple(tables or want.tables),
        scope=want.scope,
        schema=want.schema,
    )


def _local_report(facts: CorpusFacts, requested: Sequence[str]) -> dict:
    """Local-only tables (names, row counts, a digest wherever rows were read): reported, and in
    the working scope also digested via ``components.local_only``."""
    out: dict = {}
    for name in sorted({*facts.local_counts, *requested}):
        rows = facts.tables.get(name)
        entry: dict = {}
        if rows is not None:
            entry = {"rows": len(rows), "digest": _table_component(rows)["digest"]}
        elif name in facts.local_counts:
            entry = {"rows": int(facts.local_counts[name])}
        else:
            continue
        out[name] = entry
    return out


def _native_component(
    native: Mapping, committed: Sequence[str], local: Sequence[str], scope: str, legacy: bool
) -> dict:
    """Only the hashes of the fingerprinted tables (plus ``issues``). The committed scope keeps
    the committed root and drops the local-only tables and the working-set database hash."""
    per_table = native.get("tables") or {}
    if scope == SCOPE_COMMITTED:
        wanted = {"issues", *committed}
        return {
            "root": native.get("root"),
            "tables": {t: per_table[t] for t in sorted(wanted) if t in per_table},
        }
    wanted = {"issues", *committed, *(local if not legacy else ())}
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
    scope: str = SCOPE_COMMITTED,
    local_digests: bool = False,
) -> Fingerprint:
    facts = reader.read(Path(hive_dir), tuple(tables), scope=scope, local_digests=local_digests)
    return compute(facts, tier, tables=tables, scope=scope)


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
            "older fingerprint with the current tool (compute_like)"
        )
    if left.scope != right.scope:
        raise FingerprintScopeMismatch(
            f"refusing to compare a {left.scope!r}-scope fingerprint with a {right.scope!r}-scope "
            "one; relocation compares the committed scope, a native backup the working scope"
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
    # absence is not a difference (it is a weaker check, never a false mismatch). A table whose
    # hash one side could not give (uncommitted edits to a committed table) is skipped the same way.
    if left.native is not None and right.native is not None:
        ln, rn = dict(left.native), dict(right.native)
        lt, rt = ln.pop("tables", None) or {}, rn.pop("tables", None) or {}
        shared = set(lt) & set(rt)
        walk("dolt_native", ln, rn)
        walk("dolt_native.tables", {t: lt[t] for t in shared}, {t: rt[t] for t in shared})
        equal = equal and not any(k.startswith("dolt_native") for k in detail)
    return FingerprintDiff(equal, left.tier, tuple(detail), detail)


def local_only_differences(left: Fingerprint, right: Fingerprint) -> dict[str, tuple]:
    """Informational: how the reported local-only tables differ (name -> (left, right)). Never part
    of the committed-scope verdict; the working scope compares them through the digest."""
    a, b = left.local_only or {}, right.local_only or {}
    return {n: (a.get(n), b.get(n)) for n in sorted({*a, *b}) if a.get(n) != b.get(n)}


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


def native_hashes(
    query: Callable[[str], list[dict]],
    tables: Sequence[str],
    *,
    skip: Sequence[str] = (),
    branch: str = "main",
) -> dict:
    """Dolt-native, rendering-independent content hashes of the session's CURRENT database:
    ``db`` (``dolt_hashof_db()``, the WORKING set), ``root`` (``dolt_hashof_db('<branch>')``, the
    COMMITTED root, what a clone reproduces) and ``dolt_hashof_table(t)`` per table other than
    ``skip``. ``query`` returns row dicts."""

    def one(sql: str) -> str:
        rows = query(sql)
        if not rows or not rows[0]:
            raise FingerprintIncomplete(f"no result for {sql[:60]}")
        return str(next(iter(rows[0].values())) or "")

    if not _IDENT.match(branch) and not re.match(r"^[A-Za-z0-9_./-]+$", branch):
        raise FingerprintError(f"invalid branch name {branch!r}")
    per_table: dict = {}
    for table in dict.fromkeys(tables):
        if not _IDENT.match(table):
            raise FingerprintError(f"invalid table name {table!r}")
        if table not in skip:
            per_table[table] = one(f"SELECT dolt_hashof_table('{table}') AS h")
    return {
        "db": one("SELECT dolt_hashof_db() AS h"),
        "root": one(f"SELECT dolt_hashof_db('{branch}') AS h"),
        "tables": per_table,
    }


def _ignore_regex(pattern: str) -> re.Pattern:
    """A ``dolt_ignore`` pattern as a regex: ``%`` (bd's spelling) and ``*`` match any run."""
    return re.compile(
        "^" + "".join(".*" if c in "%*" else re.escape(c) for c in pattern) + "$", re.IGNORECASE
    )


def ignored_among(patterns: Sequence[Mapping], names: Sequence[str]) -> list[str]:
    """The ``names`` that ``dolt_ignore`` ``patterns`` (rows of ``pattern, ignored``) ignore. Where
    an ignore and a keep pattern both match, the longer (more specific) one wins, as in Dolt."""
    rules = [
        (str(r.get("pattern") or ""), bool(r.get("ignored"))) for r in patterns if r.get("pattern")
    ]
    out = []
    for name in dict.fromkeys(names):
        hits = [(len(p), ign) for p, ign in rules if _ignore_regex(p).match(name)]
        if hits and max(hits)[1]:
            out.append(name)
    return out


@dataclass(frozen=True)
class ScopedRead:
    """What a reader gathers for a scope: the issue rows, the table rows, the ignored inventory."""

    issues: list
    tables: dict
    ignored: tuple[str, ...]
    local_counts: dict
    dirty: tuple[str, ...]


def read_scoped(
    query: Callable[[str], list[dict]],
    qualify: Callable[[str], str],
    *,
    real: set[str],
    tables: Sequence[str],
    scope: str,
    local_digests: bool = False,
    branch: str = "main",
) -> ScopedRead:
    """The scope-dependent reads, shared by every reader (so they cannot drift apart).

    ``committed`` reads ``AS OF`` the branch head and never reads an ignored table's rows (only
    counts them) unless ``local_digests``; ``working`` reads the working set, ignored tables too.
    Ignored tables are discovered from ``dolt_ignore``: every existing table a pattern matches, plus
    any REQUESTED table a pattern matches even if absent here (a clone does not have it)."""
    if scope not in SCOPES:
        raise FingerprintError(f"unknown scope {scope!r}")
    for table in tables:
        if not _IDENT.match(table):
            raise FingerprintError(f"invalid table name {table!r}")
    patterns = query(f"SELECT pattern, ignored FROM {qualify('dolt_ignore')}")
    ignored = tuple(sorted(ignored_among(patterns, [*sorted(real), *tables])))
    dirty = tuple(
        sorted(
            str(r.get("table_name"))
            for r in query(f"SELECT table_name FROM {qualify('dolt_status')}")
        )
    )
    as_of = f" AS OF '{branch}'" if scope == SCOPE_COMMITTED else ""
    issues = query(f"SELECT * FROM {qualify('issues')}{as_of} ORDER BY id")
    out: dict = {}
    for table in tables:
        local = table in ignored
        if table not in real or (local and scope == SCOPE_COMMITTED and not local_digests):
            out[table] = None
        else:
            out[table] = query(f"SELECT * FROM {qualify(table)}{'' if local else as_of}")
    counts: dict = {}
    for name in ignored:
        if name not in real:
            continue
        if out.get(name) is not None:
            counts[name] = len(out[name])
        else:
            rows = query(f"SELECT COUNT(*) AS c FROM {qualify(name)}")
            counts[name] = int(next(iter(rows[0].values()), 0) or 0) if rows else 0
    return ScopedRead(issues, out, ignored, counts, dirty)


def native_for(
    query: Callable[[str], list[dict]], read: ScopedRead, scope: str, *, branch: str = "main"
) -> dict:
    """:func:`native_hashes` for the tables a scope fingerprints: the committed scope leaves out
    ignored tables and any committed table with uncommitted edits (its working hash is not the
    committed one)."""
    present = [
        t
        for t in dict.fromkeys(("issues", *read.tables))
        if t == "issues" or read.tables[t] is not None
    ]
    if scope == SCOPE_COMMITTED:
        skip = {*read.ignored, *read.dirty}
        return native_hashes(query, [t for t in present if t not in skip], branch=branch)
    return native_hashes(query, present, branch=branch)


class BdSqlCorpusReader:
    """Minimal :class:`CorpusReader` over a running server. ``sql`` is injectable for tests."""

    def __init__(self, sql: SqlFn = bd_sql, *, probe=dolt_health.probe_raw_schema_version):
        self._sql = sql
        self._probe = probe

    def read(
        self,
        hive_dir: Path,
        tables: Sequence[str],
        *,
        scope: str = SCOPE_COMMITTED,
        local_digests: bool = False,
    ) -> CorpusFacts:
        sql = self._sql
        query = lambda q: sql(hive_dir, q)  # noqa: E731
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
        read = read_scoped(
            query,
            lambda t: f"`{t}`",
            real=real,
            tables=tables,
            scope=scope,
            local_digests=local_digests,
        )
        as_of = " AS OF 'main'" if scope == SCOPE_COMMITTED else ""
        prefix_rows = sql(hive_dir, f"SELECT value FROM config{as_of} WHERE `key` = 'issue_prefix'")
        return CorpusFacts(
            project_id=store_locator.project_id(hive_dir),
            prefix=str(prefix_rows[0].get("value", "")) if prefix_rows else "",
            schema_version=schema,
            main_head=str(head[0].get("h", "")) if head else "",
            branches={str(b["name"]): str(b["hash"]) for b in branches},
            issue_rows={str(r["id"]): r for r in read.issues},
            tables=read.tables,
            native=native_for(query, read, scope),
            ignored=read.ignored,
            local_counts=read.local_counts,
        )


__all__ = [
    "SCHEMA",
    "CorpusFacts",
    "CorpusReader",
    "BdSqlCorpusReader",
    "Fingerprint",
    "FingerprintDiff",
    "FingerprintScopeMismatch",
    "FingerprintVersionMismatch",
    "LEGACY_SCHEMA",
    "SCOPE_COMMITTED",
    "SCOPE_WORKING",
    "compute_like",
    "configured_local_digests",
    "local_only_differences",
    "read_scoped",
    "native_for",
    "canonical_row",
    "canonical_value",
    "native_hashes",
    "canonical",
    "compare",
    "compute",
    "fingerprint",
    "configured_tables",
]
