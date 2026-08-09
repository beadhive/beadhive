"""Cross-database bulk copy — the `hub.bulk_sync` fast path for `hub.sync()` (bh-l7sm8).

`bd repo sync` hydrates the aggregate by round-tripping each hive through JSONL
(`bd export` -> `bd repo sync`'s own JSONL import), and that import validates every
dependency edge with its OWN recursive CTE (`isAncestorInTx`) — measured at 6.4 issues/sec,
655.82s for 4212 issues (bh-z4z52). Since this fleet's storage migration put every hive on
ONE shared Dolt server, a hive's tables are just ANOTHER DATABASE from the aggregate's own
connection — a cross-database ``INSERT ... SELECT`` moves the same content in 1.65s (~398x,
bh-z4z52's own measurement). This module is that fast path, wired in behind
``hub.bulk_sync`` (default OFF — see `config.hub_bulk_sync`) so it stays a REVERSIBLE
stopgap: flip the flag off and `hub.sync()` goes straight back to `bd repo sync`
unconditionally, exactly as it worked before this bead.

## No new runtime dependency

The prototype behind bh-z4z52 used ``pymysql`` (no MySQL client is installed on this host,
and the ``dolt`` CLI's own ``--host`` path fails TLS negotiation against bd's server — see
that bead). This module needs neither: ``bd sql -q "<query>" --json``, run against ANY
hive's own store, executes arbitrary SQL against the SAME shared server every hive's tables
live on — including a query that references another database by qualified name
(``<other_db>.<table>``), confirmed by direct measurement against real scratch databases on
this host. So the aggregate's own `bd sql` is the transport for every query in this module;
no MySQL driver is added to this project's dependencies.

## THE CURATED TABLE LIST — the whole risk of this bead

The hive databases share 29 tables (27 base tables + 2 views), confirmed via ``SHOW FULL
TABLES`` against a real bd-managed database on this host:

* **COPY** (content — :data:`CONTENT_TABLES`, in FK-safe order: ``issues``/``wisps`` before
  anything that references them): ``issues``, ``dependencies``, ``labels``, ``comments``,
  ``events``, ``wisps``, ``wisp_dependencies``, ``wisp_labels``, ``wisp_comments``,
  ``wisp_events``.
* **NEVER COPY** (:data:`DENY_TABLES` — identity/bookkeeping. Each database's OWN identity;
  copying one INTO the aggregate corrupts it, the same failure class as the PROJECT IDENTITY
  MISMATCH hit during this morning's storage migration): ``metadata``, ``local_metadata``,
  ``config``, ``issue_counter``, ``child_counters``, ``wisp_child_counters``,
  ``schema_migrations``, ``ignored_schema_migrations``, ``leases``, ``repo_mtimes``,
  ``federation_peers``, ``compaction_snapshots``.
* **EXCLUDE** (:data:`EXCLUDED_VIEWS` — views, not tables, so ``INSERT ... SELECT`` against
  them is nonsensical regardless): ``ready_issues``, ``blocked_issues``.
* **DECIDED, NOT COPIED** (:data:`NOT_COPIED_UNDECIDED` — the five bh-l7sm8 asked this bead to
  decide on: ``custom_statuses``, ``custom_types``, ``routes``, ``interactions``,
  ``issue_snapshots``). Measured directly against a real ``bd repo sync`` (this bead,
  2026-08-08): NONE of these five are touched by bd's OWN JSONL export/import round trip
  either — ``bd export`` only ever emits per-issue fields (``labels``/``dependencies``/
  ``comments`` arrays nested in each issue record); a fresh aggregate hydrated by
  ``bd repo sync`` has all five at 0 rows regardless of what the source hives contain. So
  excluding them here is not a new gap this fast path introduces relative to the path it
  replaces — it is the status quo, confirmed rather than assumed. Excluding is also the
  right call on independent merits for the two that matter most: ``custom_statuses`` /
  ``custom_types`` are keyed on a bare ``name`` with NO per-hive namespacing (confirmed via
  ``DESCRIBE``) — two hives with a same-named-but-differently-defined status would silently
  overwrite each other under ``ON DUPLICATE KEY UPDATE``, depending on hive iteration order,
  exactly the collision bh-l7sm8 flagged. ``routes`` / ``interactions`` / ``issue_snapshots``
  are hive-local operational bookkeeping (the routing table, an agent tool-call audit log,
  compaction snapshots) with no cross-hive query this aggregate serves.

## A genuine limit this bead's own research surfaced: `events` cannot achieve byte parity

Measured directly (this bead): `bd repo sync`'s own JSONL import does NOT replay a source
hive's real event history into the aggregate. It SYNTHESIZES a fresh, partial event log at
import time (fewer rows, freshly-generated non-deterministic UUIDs, a different
``event_type`` mix — e.g. one ``status_changed`` and one ``dependency_added`` event in a real
source hive surfaced as neither in the bd-produced aggregate). So "content parity against a
bd-produced aggregate" is not a well-defined bar for `events`/`wisp_events` — bd's own path
isn't even deterministic run to run for that table. This module copies the REAL event/
wisp_event rows verbatim (full audit-log fidelity — strictly more correct data for a read
cache to hold than bd's own lossy synthesis), and tests hold it to fidelity against the
SOURCE hive, not identity with a bd-produced aggregate, for exactly this reason. See this
bead's own report for the measurement.

## Ancestor validation

`isAncestorInTx` (the very check whose PER-EDGE cost is this bead's reason to exist) still
matters: it is what stops a `blocks` edge from pointing at its own `parent-child` ancestor.
Skipping it isn't an option, but running it once, SET-BASED, over the whole graph is (0.16s
measured, bh-z4z52) — see :func:`validate_ancestors` / :data:`ANCESTOR_VIOLATION_QUERY`,
run once after every bulk pass that changed anything. Reports violations; never silently
drops or "fixes" them.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import typer

from . import bd, store_locator
from .run import run

SQL_TIMEOUT = 60.0  # seconds per `bd sql` call — a local loopback query, generously bounded

# ---------------------------------------------------------------------------------------------
# THE CURATED TABLE LIST — see module docstring. Order matters: FK-referenced parents
# (`issues`, `wisps`) must land before anything that references them.
# ---------------------------------------------------------------------------------------------

CONTENT_TABLES: tuple[str, ...] = (
    "issues",
    "dependencies",
    "labels",
    "comments",
    "events",
    "wisps",
    "wisp_dependencies",
    "wisp_labels",
    "wisp_comments",
    "wisp_events",
)

DENY_TABLES: tuple[str, ...] = (
    "metadata",
    "local_metadata",
    "config",
    "issue_counter",
    "child_counters",
    "wisp_child_counters",
    "schema_migrations",
    "ignored_schema_migrations",
    "leases",
    "repo_mtimes",
    "federation_peers",
    "compaction_snapshots",
)

EXCLUDED_VIEWS: tuple[str, ...] = ("ready_issues", "blocked_issues")

NOT_COPIED_UNDECIDED: tuple[str, ...] = (
    "custom_statuses",
    "custom_types",
    "routes",
    "interactions",
    "issue_snapshots",
)

assert not (set(CONTENT_TABLES) & set(DENY_TABLES)), "a deny-listed table snuck into the copy list"

# The set-based replacement for bd's per-edge `isAncestorInTx` (internal/storage/issueops/
# dependencies.go:560) — one recursive CTE over the WHOLE graph instead of one per dependency
# edge, verified against bh-z4z52's real HQ graph: 0 violations, 0.16s. Scope, matching that
# bead's own documented boundary: `dependencies` only (not `wisp_dependencies`), and only
# issue-target edges (`depends_on_issue_id`) — wisp-target (`depends_on_wisp_id`) and external
# (`depends_on_external`) edges are NOT covered, the same gap bh-z4z52's own prototype named.
ANCESTOR_VIOLATION_QUERY = """
WITH RECURSIVE anc(node, ancestor) AS (
    SELECT issue_id, depends_on_issue_id FROM dependencies
    WHERE type = 'parent-child' AND depends_on_issue_id IS NOT NULL
    UNION
    SELECT a.node, d.depends_on_issue_id
    FROM anc a JOIN dependencies d
      ON d.issue_id = a.ancestor AND d.type = 'parent-child'
      AND d.depends_on_issue_id IS NOT NULL
)
SELECT COUNT(*) AS violations FROM dependencies d
JOIN anc ON anc.node = d.issue_id AND anc.ancestor = d.depends_on_issue_id
WHERE d.type = 'blocks'
""".strip()


def _run_sql(store: Path, query: str, *, timeout: float = SQL_TIMEOUT):
    """``bd -C <store> sql -q <query> --json`` — the ONE transport this whole module uses (see
    module docstring: no MySQL driver, `bd sql` reaches the shared server directly, including
    cross-database qualified names). Never raises; the caller reads ``returncode``."""
    return run(
        ["bd", "-C", str(store), "sql", "-q", query, "--json"],
        check=False,
        capture=True,
        timeout=timeout,
    )


def _parse_json(res):
    """Parsed JSON body of a `_run_sql` result, or ``None`` on a failed call or unparseable
    output — never raises, matching `bd.json`'s own None-on-failure contract."""
    if res.returncode != 0:
        return None
    try:
        return _json.loads(res.stdout or "null")
    except ValueError:
        return None


def server_databases(hub: Path) -> set[str]:
    """Every database name that ACTUALLY exists on the shared server right now, per ``SHOW
    DATABASES`` run against the aggregate's own connection — a real, freshly-queried fact
    (every hive on this fleet's shared server is visible from any one connection to it), never
    assumed from a hive's registry entry or persisted metadata alone. Empty on any query
    failure (the caller then treats every hive as not-co-located, which fails toward the safe
    `bd repo sync` fallback, never toward a false positive)."""
    data = _parse_json(_run_sql(hub, "SHOW DATABASES"))
    if not isinstance(data, list):
        return set()
    return {row["Database"] for row in data if isinstance(row, dict) and "Database" in row}


def co_located_database(server_dbs: set[str], hive_dir: Path, prefix: str) -> str | None:
    """The hive's database name on the shared server, iff it is verifiably there — else
    ``None``, which the caller MUST read as "fall back to `bd repo sync` for this hive" (bh-
    l7sm8 item 3's fallback for the hives with no server database at all).

    Two independent signals, both required — never a single guess:

    1. bd's own persisted ``dolt_mode`` for this hive is NOT ``"embedded"``
       (:func:`store_locator.is_embedded_mode`, a pure metadata.json read, no subprocess) — an
       embedded-mode hive has no server database to cross-reference at all. Mirrors
       `dolt_health`'s own documented fleet-wide assumption: a ``dolt_mode == "server"`` hive
       on this fleet means bd's ONE shared server, since no owned/external-mode hive exists
       here yet.
    2. the name :func:`store_locator.server_database` resolves is actually present in
       ``server_dbs`` — a REAL, freshly-queried fact (:func:`server_databases`), not merely
       derived from the hive's prefix. This is what rules out a hydrated-but-never-migrated
       hive even though its registry entry looks identical to a migrated one's.
    """
    if store_locator.is_embedded_mode(hive_dir):
        return None
    name = store_locator.server_database(hive_dir, fallback=prefix)
    return name if name in server_dbs else None


def _table_schema(hub: Path, table: str, cache: dict[str, tuple[list[str], list[str]]]):
    """``(columns, primary-key columns)`` for ``table``, read once per bulk pass via
    ``DESCRIBE`` against the aggregate's OWN copy of the table (every database on this shared
    server is bd-managed at the same schema version — the same precondition `co_located_
    database` already establishes before any copy is attempted) and cached in ``cache`` for the
    rest of the pass. ``([], [])`` on a failed/unparseable ``DESCRIBE`` — the caller treats an
    empty column list as "can't safely build this INSERT", never guesses a shape."""
    if table in cache:
        return cache[table]
    data = _parse_json(_run_sql(hub, f"DESCRIBE `{table}`"))
    if not isinstance(data, list):
        cache[table] = ([], [])
        return cache[table]
    cols = [row["Field"] for row in data if isinstance(row, dict) and "Field" in row]
    pk = [row["Field"] for row in data if isinstance(row, dict) and row.get("Key") == "PRI"]
    cache[table] = (cols, pk)
    return cache[table]


def _upsert_query(table: str, database: str, cols: list[str], pk: list[str]) -> str:
    """The exact cross-database upsert shape bh-z4z52 measured working on Dolt 2.2.3:
    ``INSERT ... SELECT ... FROM <db>.<table> AS s ON DUPLICATE KEY UPDATE col = s.col`` — the
    SOURCE-ALIAS form, never ``VALUES(col)`` (Dolt 2.2.3 rejects that form for INSERT..SELECT:
    "expected ON DUPLICATE KEY ... VALUES() to reference a column, found: __new_ins.<col>").
    Preserves bd's own documented upsert-ONLY semantics exactly (no DELETE/TRUNCATE/REPLACE —
    the source-alias UPDATE is the only write besides the INSERT itself).

    A table whose primary key covers EVERY column (``labels``/``wisp_labels`` — composite PK
    ``(issue_id, label)``, nothing beyond it) has no non-key column left to update; the fallback
    updates the key column(s) to themselves — a harmless no-op that keeps the SQL valid without
    ever touching a row's actual identity."""
    update_cols = [c for c in cols if c not in pk] or pk
    col_list = ", ".join(f"`{c}`" for c in cols)
    update_clause = ", ".join(f"`{c}` = s.`{c}`" for c in update_cols)
    return (
        f"INSERT INTO `{table}` ({col_list}) "
        f"SELECT {col_list} FROM `{database}`.`{table}` AS s "
        f"ON DUPLICATE KEY UPDATE {update_clause}"
    )


def copy_table(
    hub: Path, database: str, table: str, schema_cache: dict[str, tuple[list[str], list[str]]]
) -> tuple[bool, str]:
    """Upsert-copy one :data:`CONTENT_TABLES` member from ``database`` into ``hub``. Returns
    ``(True, "")`` on success, else ``(False, <bd's own error line>)`` — never raises; the
    caller decides whether a failed table aborts the whole hive's copy."""
    cols, pk = _table_schema(hub, table, schema_cache)
    if not cols:
        return False, f"could not read `{table}`'s schema from the aggregate"
    res = _run_sql(hub, _upsert_query(table, database, cols, pk))
    if res.returncode != 0:
        return False, bd.err_line(res)
    return True, ""


def copy_hive(
    hub: Path, database: str, schema_cache: dict[str, tuple[list[str], list[str]]]
) -> tuple[bool, str]:
    """Upsert-copy every :data:`CONTENT_TABLES` member for one hive's ``database`` into
    ``hub``, in the fixed FK-safe order. Stops at the first failing table — a partially
    bulk-copied hive is left exactly as `run_bulk_pass` needs it (still registered with the
    hub, so the caller's own `bd repo sync` fallback still fills in the rest this same round;
    see that function)."""
    for table in CONTENT_TABLES:
        ok, detail = copy_table(hub, database, table, schema_cache)
        if not ok:
            return False, f"{table}: {detail}"
    return True, ""


def validate_ancestors(hub: Path) -> int | None:
    """Run :data:`ANCESTOR_VIOLATION_QUERY` against the aggregate once, after a bulk pass.
    Returns the violation count (``0`` == clean), or ``None`` if the check itself could not be
    run — the two are never conflated: a caller that can't tell "checked, clean" from "couldn't
    check" must report the latter as its own finding (see `run_bulk_pass`)."""
    data = _parse_json(_run_sql(hub, ANCESTOR_VIOLATION_QUERY))
    if not isinstance(data, list) or not data:
        return None
    row = data[0]
    value = row.get("violations") if isinstance(row, dict) else None
    return value if isinstance(value, int) else None


def _deregister(hub: Path, src: Path) -> bool:
    """Drop ``src``'s registration from the hub's own ``bd repo list`` — so the caller's
    subsequent unconditional `bd repo sync` call never reprocesses a hive this pass already
    bulk-copied (which would silently re-pay the exact per-edge CTE cost this bead exists to
    avoid, for precisely the hives it succeeded on). Self-healing regardless: `hub.sync()`'s own
    per-hive loop re-runs `bd repo add` unconditionally on EVERY call (see `_sync_hive`), so a
    de-registered-here hive is re-registered at the top of the very next `sync()` call — this
    is never a permanent removal, only a "skip it THIS pass, bulk already handled it" signal."""
    res = run(["bd", "-C", str(hub), "repo", "remove", str(src)], check=False, capture=True)
    return res.returncode == 0


def run_bulk_pass(hub: Path, entries: list[tuple[str, Path, bool]]) -> list[str]:
    """The whole bulk fast path for one `hub.sync()` call, gated by the caller on
    `config.hub_bulk_sync`. ``entries`` is ``(prefix, src, changed)`` for every hive
    `hub.sync()`'s own per-hive loop already exported + registered this round (`changed` is
    that same loop's own per-hive watermark comparison — bh-d5jhc.2 — reused here rather than
    re-derived, so an unchanged hive is skipped the same way it already is for `bd export`).

    For each entry:

    1. :func:`co_located_database` decides co-location, verified against a live `SHOW
       DATABASES` snapshot (:func:`server_databases`, queried once for the whole pass) — not
       co-located means "leave it registered, `bd repo sync` handles it" (bh-l7sm8 item 3's
       fallback), silently, since that's the expected steady state for every non-migrated hive.
    2. Co-located AND changed: :func:`copy_hive`. A failure is reported and the hive is left
       REGISTERED — the caller's own subsequent `bd repo sync` call is the safety net that
       still lands this hive correctly THIS round, just via the slow path (never a silent
       drop).
    3. Co-located, changed, and copied (or co-located and unchanged — nothing to refresh):
       :func:`_deregister` so `bd repo sync` skips it. A de-register failure is treated the
       same as a copy failure (left registered, reported) — "bulk-copied but might also be
       redundantly reprocessed" is unacceptable to CLAIM as hydrated without the caller's own
       fallback able to confirm it independently.

    Returns the prefixes this pass copied (or found already current) AND successfully
    de-registered — the caller (`hub.sync()`) treats these as unconditionally hydrated,
    bypassing `bd repo sync`'s own report-text parsing entirely for them (they were
    deliberately hidden from that call). Runs :func:`validate_ancestors` once at the end,
    only if anything was actually hydrated this pass — reported, never auto-fixed, never
    silently swallowed (see that function)."""
    if not entries:
        return []
    databases = server_databases(hub)
    schema_cache: dict[str, tuple[list[str], list[str]]] = {}
    hydrated: list[str] = []
    for prefix, src, changed in entries:
        database = co_located_database(databases, src, prefix)
        if database is None:
            continue
        if changed:
            ok, detail = copy_hive(hub, database, schema_cache)
            if not ok:
                typer.echo(
                    f"  ✗ {prefix}: bulk copy from `{database}` failed ({detail}) — "
                    "leaving it registered for bd repo sync to handle this round",
                    err=True,
                )
                continue
            typer.echo(f"  ✓ {prefix}: bulk-copied from `{database}` (cross-database)", err=True)
        else:
            typer.echo(f"  ✓ {prefix}: unchanged — bulk aggregate already current", err=True)
        if _deregister(hub, src):
            hydrated.append(prefix)
        else:
            typer.echo(
                f"  ⚠ {prefix}: bulk-copied but could not de-register from the hub — leaving "
                "it registered so bd repo sync can confirm it independently this round",
                err=True,
            )
    if hydrated:
        violations = validate_ancestors(hub)
        if violations is None:
            typer.echo(
                "  ⚠ bulk sync: could not run the post-copy ancestor validation query", err=True
            )
        elif violations:
            typer.echo(
                f"  ⚠ bulk sync: {violations} ancestor-dependency violation(s) found in the "
                "aggregate after bulk copy (a 'blocks' edge targets a 'parent-child' ancestor) "
                "— reported, not auto-fixed; see hub_bulk.ANCESTOR_VIOLATION_QUERY",
                err=True,
            )
    return hydrated
