"""Integration: `hub_bulk`'s cross-database bulk copy (bh-l7sm8) against a REAL, isolated
shared Dolt server — not a mock of bd's SQL surface.

Proves the acceptance bar this bead's own bead text demands directly against real `bd`/Dolt
behavior:

* row-count AND content parity for ``issues``/``dependencies``/``labels``/``comments`` against
  a REAL `bd repo sync`-produced aggregate (not just issues — labels explicitly, the table an
  earlier prototype silently dropped). The aggregate snapshot is captured, then every curated
  content table is proven empty before the same database becomes the copy target;
* NO identity/bookkeeping table (:data:`beadhive.hub_bulk.DENY_TABLES`) is written into the
  target — proven by showing the target's OWN prefix identity survives the copy, not merely
  asserting it in a docstring;
* the ``events`` table CANNOT be held to identity with a bd-produced aggregate — measured here,
  not assumed: `bd repo sync`'s own JSONL import SYNTHESIZES a fresh, partial, non-deterministic
  event log rather than replaying history, so this test asserts `hub_bulk`'s copy against
  SOURCE fidelity instead, and separately proves the bd-produced aggregate's own event log
  really does diverge (different ids, different `event_type` mix) — the finding that justifies
  testing events differently from every other content table;
* the ``wisps`` family (untouched by `bd repo sync` entirely) is carried over with full
  source fidelity — an intentional widening past what `bd repo sync` does today, not a parity
  regression;
* the five DECIDED-NOT-TO-COPY tables (`custom_statuses` chief among them, for the vocabulary-
  collision reason `hub_bulk`'s own docstring gives) land at zero rows in the target even
  though the source hive has one;
* the set-based ancestor-validation query (:func:`beadhive.hub_bulk.validate_ancestors`) runs
  clean (0 violations) over a real, well-formed graph.

Runs against an ISOLATED shared-server instance (its own `BEADS_SHARED_SERVER_DIR` + a free TCP
port) — never the operator's real `~/.beads/shared-server/`, matching every other real-bd test
in this suite (bh-u562.1/bh-00cq). Marked `integration` (slower — spins up a real Dolt
sql-server) + self-skips without a `bd` binary on PATH.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from beadhive import hub, hub_bulk, store_locator
from beadhive.run import run as _run
from harness.beads import bd as _bd
from harness.beads import skip_if_no_bd
from harness.world import free_port, reap_dolt_server

# `dolt_server`: every test here stands up a REAL sql-server, so each holds one of the run-wide
# slots `conftest._bound_concurrent_dolt_servers` hands out (bh-wa3ch).
pytestmark = [pytest.mark.integration, pytest.mark.dolt_server, skip_if_no_bd]

_TIMEOUT = 60
_STARTUP_TIMEOUT = 30.0


def run(args, *pargs, **kwargs):
    """Retry ordinary bd work while another worker briefly holds bd init's exclusive gate."""
    check = kwargs.pop("check", False)
    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while True:
        lock = (
            _shared_server_command_lock()
            if args[0] == "bd" and "init" not in args
            else contextlib.nullcontext()
        )
        with lock:
            result = _run(args, *pargs, check=False, **kwargs)
        detail = f"{result.stdout or ''}{result.stderr or ''}"
        if result.returncode == 0 or args[0] != "bd" or "workspace gate busy" not in detail:
            if check and result.returncode:
                raise subprocess.CalledProcessError(
                    result.returncode, result.args, output=result.stdout, stderr=result.stderr
                )
            return result
        if time.monotonic() >= deadline:
            if check:
                raise subprocess.CalledProcessError(
                    result.returncode, result.args, output=result.stdout, stderr=result.stderr
                )
            return result
        time.sleep(0.2)


def bd(*args, **kwargs):
    deadline = time.monotonic() + _STARTUP_TIMEOUT
    # Capture gate diagnostics even for callers that do not otherwise consume output.
    kwargs["capture"] = True
    while True:
        try:
            with _shared_server_command_lock():
                return _bd(*args, **kwargs)
        except AssertionError as exc:
            if "workspace gate busy" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


@contextlib.contextmanager
def _shared_server_init_lock():
    """Serialize bd init's exclusive workspace gate while allowing later test work to overlap."""
    server_dir = Path(os.environ["BEADS_SHARED_SERVER_DIR"])
    lock_path = server_dir.with_name(f"{server_dir.name}.bd-init.lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


@contextlib.contextmanager
def _shared_server_command_lock():
    """Keep ordinary commands active while excluding bd init's maintenance gate."""
    server_dir = Path(os.environ["BEADS_SHARED_SERVER_DIR"])
    lock_path = server_dir.with_name(f"{server_dir.name}.bd-init.lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        yield


@pytest.fixture
def fresh_shared_server(tmp_path, monkeypatch):
    """This test's OWN shared-server instance, at its own data dir and a free port — never the
    operator's real fleet server. Mirrors `test_storage_migrate_int.py`'s fixture of the same
    name (same reasoning: `bd dolt stop` cannot reliably tear this down, see
    `harness.world.reap_dolt_server`'s own docstring), except this one also YIELDS the port —
    every test here mints a SECOND store on the same server right after the first, and (measured
    under this suite's own `-n auto` parallel load, not assumed) the first `bd init
    --shared-server` can return before the server it just spawned is actually accepting
    connections yet; a second `bd init --shared-server` landing in that window races it for the
    port instead of finding it already up. `_wait_until_accepting` below is what closes that gap
    (the same fix `test_dolt_health_real_server_int.py`'s own fixture applies)."""
    server_dir = tmp_path / "shared-server"
    port = free_port()
    # Pinned bd 50763fc uses non-wide `ps`; xdist's COLUMNS=80 truncates
    # `dolt sql-server`, falsely treating the live server as dead during port reclaim.
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(server_dir))
    monkeypatch.setenv("BEADS_DOLT_SERVER_PORT", str(port))
    yield port
    reap_dolt_server(server_dir)


@pytest.fixture
def isolated_shared_server(reusable_dolt_server):
    """Run-owned shared process with unique, per-test databases and filesystem state."""
    yield reusable_dolt_server


def _wait_until_accepting(host: str, port: int, *, timeout: float = _STARTUP_TIMEOUT) -> None:
    """Poll with a raw TCP connect until *host*:*port* accepts one, or raise — never
    `dolt_health.probe_endpoint` (that is the module under test elsewhere), just startup-
    readiness plumbing, matching `test_dolt_health_real_server_int.py`'s own helper."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as exc:
            last_err = exc
            time.sleep(0.2)
    raise TimeoutError(f"{host}:{port} never started accepting connections: {last_err}")


def _init(path, prefix):
    """A fresh bd store, on the isolated shared server, with no interactive prompts."""
    path.mkdir(parents=True, exist_ok=True)
    with _shared_server_init_lock():
        initialized = run(
            [
                "bd",
                "init",
                "--prefix",
                prefix,
                "--shared-server",
                "--skip-agents",
                "--skip-hooks",
                "--non-interactive",
            ],
            cwd=str(path),
            check=False,
            capture=True,
            timeout=_TIMEOUT,
        )
    assert initialized.returncode == 0, initialized.stderr
    run(["git", "config", "beads.role", "maintainer"], cwd=str(path), check=True, capture=True)


def _snapshot_empty_server_database(hive, snapshot_dir):
    """Copy an empty, initialized Beads database into a clean local DOLT_CLONE source.

    The source store is initialized by real ``bd init`` on this test's isolated server. Copying
    it before any issues are created keeps the schema template empty; ``dolt gc`` on the private
    copy removes the live server's chunk journal without stopping or mutating that server.
    """
    database = store_locator.server_database(hive)
    counts = _content_row_counts(hive, database)
    assert set(counts) == set(hub_bulk.CONTENT_TABLES), counts
    assert not any(counts.values()), counts
    snapshot_dir = Path(snapshot_dir)
    shutil.copytree(store_locator.database_dir(hive), snapshot_dir)
    run(["dolt", "gc"], cwd=str(snapshot_dir), check=True, capture=True, timeout=_TIMEOUT)
    return (snapshot_dir / ".dolt" / "noms").as_uri()


def _init_from_template(path, prefix, *, template_url, control_hive):
    """Clone the clean template into the already-running isolated server, then attach real bd.

    ``control_hive`` supplies bd's configured server endpoint and credentials for DOLT_CLONE.
    ``bd init --database`` writes this project's own server metadata; ordinary hive prefixes are
    then set through bd's supported rename-prefix command. The reserved hub prefix is already the
    template prefix and is left intact.
    """
    database = prefix
    source = template_url.replace("'", "''")
    target = database.replace("'", "''")
    bd(
        "sql",
        "-q",
        f"CALL DOLT_CLONE('{source}', '{target}')",
        cwd=control_hive,
        capture=True,
        timeout=_TIMEOUT,
    )
    path.mkdir(parents=True, exist_ok=True)
    with _shared_server_init_lock():
        initialized = run(
            [
                "bd",
                "init",
                "--prefix",
                prefix,
                "--database",
                database,
                "--shared-server",
                "--external",
                "--skip-agents",
                "--skip-hooks",
                "--non-interactive",
            ],
            cwd=str(path),
            check=False,
            capture=True,
            timeout=_TIMEOUT,
        )
    assert initialized.returncode == 0, initialized.stderr
    if prefix != hub.HUB_PREFIX:
        run(
            ["bd", "-C", str(path), "rename-prefix", f"{prefix}-"],
            check=True,
            capture=True,
            timeout=_TIMEOUT,
        )
    run(["git", "config", "beads.role", "maintainer"], cwd=str(path), check=True, capture=True)


def _batch_create(path, titles):
    """Seed this row-count-only fixture in one real bd transaction."""
    batch = path.parent / f"{path.name}-seed.batch"
    batch.write_text("".join(f'create task 2 "{title}"\n' for title in titles))
    bd("batch", "-f", str(batch), cwd=path, capture=True)


def _sql(path, query):
    return run(
        ["bd", "-C", str(path), "sql", "-q", query, "--json"],
        check=True,
        capture=True,
        timeout=_TIMEOUT,
    )


def _rows(path, table, cols):
    res = _sql(path, f"SELECT {cols} FROM `{table}` ORDER BY 1")
    return json.loads(res.stdout or "[]")


def _bulk_snapshot(hub_dir, *, source_prefix, target_prefix):
    """Read the real-server parity surfaces in one SQL round trip.

    These databases all live on the same isolated Dolt server, so the proof can query their
    fully-qualified tables together. Keeping the table/column inventory here explicit makes the
    parity contract reviewable while avoiding a separate bd startup for every asserted table.
    """
    columns = {
        "issues": ("id", "title", "status", "priority", "created_at", "updated_at"),
        "dependencies": ("id", "issue_id", "type", "depends_on_issue_id"),
        "labels": ("issue_id", "label"),
        "comments": ("id", "issue_id", "author", "text"),
        "events": ("id", "issue_id", "event_type"),
        "wisps": ("id", "title"),
        "custom_statuses": ("name",),
    }
    databases = {
        source_prefix: set(columns),
        target_prefix: set(columns),
    }
    selects = []
    for database, tables in databases.items():
        for table in sorted(tables):
            fields = ", ".join(f"'{column}', `{column}`" for column in columns[table])
            selects.append(
                f"SELECT '{database}' AS database_name, '{table}' AS table_name, "
                f"JSON_OBJECT({fields}) AS row_json FROM `{database}`.`{table}`"
            )
    query = "\nUNION ALL\n".join(selects)
    result = run(
        ["bd", "-C", str(hub_dir), "sql", "-q", query, "--json"],
        check=True,
        capture=True,
        timeout=_TIMEOUT,
    )
    snapshot = {(database, table): [] for database, tables in databases.items() for table in tables}
    for row in json.loads(result.stdout or "[]"):
        value = row["row_json"]
        if isinstance(value, str):
            value = json.loads(value)
        snapshot[(row["database_name"], row["table_name"])].append(value)
    for rows in snapshot.values():
        rows.sort(key=lambda value: json.dumps(value, sort_keys=True))
    return snapshot


def _content_row_counts(hub_dir, database):
    """Count every curated copy table after removing the imported baseline repo."""
    selects = [
        f"SELECT '{table}' AS table_name, COUNT(*) AS row_count FROM `{database}`.`{table}`"
        for table in hub_bulk.CONTENT_TABLES
    ]
    result = run(
        ["bd", "-C", str(hub_dir), "sql", "-q", "\nUNION ALL\n".join(selects), "--json"],
        check=True,
        capture=True,
        timeout=_TIMEOUT,
    )
    return {row["table_name"]: int(row["row_count"]) for row in json.loads(result.stdout or "[]")}


def test_bulk_copy_matches_a_real_bd_produced_aggregate(tmp_path, isolated_shared_server):
    hva = isolated_shared_server.database("hva")
    hba = isolated_shared_server.database("hba")
    hive_a = tmp_path / "hive_a"
    _init(hive_a, hva)
    _wait_until_accepting("127.0.0.1", isolated_shared_server.port)
    template_url = _snapshot_empty_server_database(hive_a, tmp_path / "schema-template")

    id1 = bd(
        "create", "issue one", "--label", "alpha", "--silent", cwd=hive_a, capture=True
    ).stdout.strip()
    bd("comment", id1, "a comment", cwd=hive_a)
    bd("update", id1, "--status", "in_progress", cwd=hive_a)
    id2 = bd("create", "issue two", "--silent", cwd=hive_a, capture=True).stdout.strip()
    bd("dep", "add", id2, id1, cwd=hive_a)  # id2 depends on id1

    # A wisp (bd has no direct CLI to mint one outside agent-orchestration flows — inserted
    # directly, matching this table's own schema exactly as `DESCRIBE wisps` reports it).
    _sql(
        hive_a,
        "INSERT INTO wisps (id, title, description, design, acceptance_criteria, notes, "
        f"ephemeral) VALUES ('{hva}-wisp1', 'a wisp', '', '', '', '', 1)",
    )
    # Per-database vocabulary — the collision risk `hub_bulk`'s own docstring names.
    _sql(hive_a, "INSERT INTO custom_statuses (name, category) VALUES ('triage', 'open')")

    # THE BASELINE: a real `bd repo sync`-produced aggregate.
    hub_a = tmp_path / "hub_a"
    _init_from_template(hub_a, hba, template_url=template_url, control_hive=hive_a)
    run(
        ["bd", "-C", str(hive_a), "export", "-o", str(hive_a / ".beads" / "issues.jsonl")],
        check=True,
        capture=True,
        timeout=_TIMEOUT,
    )
    run(
        ["bd", "-C", str(hub_a), "repo", "add", str(hive_a)],
        check=True,
        capture=True,
        timeout=_TIMEOUT,
    )
    run(["bd", "-C", str(hub_a), "repo", "sync"], check=True, capture=True, timeout=_TIMEOUT)

    # Save the real aggregate before resetting the database that will become the copy target.
    baseline = _bulk_snapshot(hub_a, source_prefix=hva, target_prefix=hba)

    # The baseline is now held in memory. Remove its imported repo rows and verify every curated
    # table is empty, then use this isolated empty state as the copy target. This preserves the
    # independent-baseline comparison while avoiding a third slow shared-server bd init.
    run(
        ["bd", "-C", str(hub_a), "repo", "remove", str(hive_a)],
        check=True,
        capture=True,
        timeout=_TIMEOUT,
    )
    empty_counts = _content_row_counts(hub_a, hba)
    assert set(empty_counts) == set(hub_bulk.CONTENT_TABLES), empty_counts
    assert not any(empty_counts.values()), empty_counts

    # --- the empty hba target now receives the real cross-database copy ---
    ok, detail = hub_bulk.copy_hive(hub_a, hva, {})
    assert ok, detail
    snapshot = _bulk_snapshot(hub_a, source_prefix=hva, target_prefix=hba)

    # --- row-count AND content parity against the REAL bd-produced aggregate ---
    for table in ("issues", "dependencies", "labels", "comments"):
        expected = baseline[(hba, table)]
        actual = snapshot[(hba, table)]
        assert actual == expected, f"{table}: bulk copy diverged from the bd-produced aggregate"
        assert actual, f"{table}: fixture produced no rows — the comparison above is vacuous"

    # --- events: bd's own path does NOT replay real history (measured, not assumed) ---
    source_events = snapshot[(hva, "events")]
    bulk_events = snapshot[(hba, "events")]
    baseline_events = baseline[(hba, "events")]
    assert bulk_events == source_events, "hub_bulk must copy the REAL event log verbatim"
    assert bulk_events != baseline_events, (
        "if this ever matches, bd repo sync started replaying real event history — the "
        "divergence this test pins down no longer holds and hub_bulk's own docstring "
        "rationale for testing events against source-fidelity (not aggregate-parity) needs "
        "re-checking"
    )

    # --- wisps: bd repo sync never touches this family; hub_bulk's copy is a widening ---
    assert baseline[(hba, "wisps")] == []
    bulk_wisps = snapshot[(hba, "wisps")]
    assert bulk_wisps == snapshot[(hva, "wisps")]
    assert len(bulk_wisps) == 1

    # --- decided-not-to-copy vocabulary tables: zero rows in the target, despite the source ---
    assert snapshot[(hva, "custom_statuses")] != []  # source really has one
    assert snapshot[(hba, "custom_statuses")] == []

    # --- identity/bookkeeping untouched: hba can still mint ITS OWN prefix afterward ---
    source_ids = {row["id"] for row in snapshot[(hba, "issues")]}
    assert source_ids == {id1, id2}, source_ids
    sanity_id = bd("create", "sanity check", "--silent", cwd=hub_a, capture=True).stdout.strip()
    assert sanity_id.startswith(f"{hba}-"), sanity_id

    # --- set-based ancestor validation runs clean over a real, well-formed graph ---
    assert hub_bulk.validate_ancestors(hub_a) == 0


def _prefix_counts(hub_dir) -> dict[str, int]:
    """``{prefix: row count}`` over every issue in `hub_dir`'s ``issues`` table, bucketed by
    the id's leading `<prefix>-` segment — the per-prefix view bh-4o07n's own incident was
    diagnosed from (HQ's per-prefix counts, not just the total: a total can hold steady while
    one prefix is wiped and another grows)."""
    rows = _rows(hub_dir, "issues", "id")
    counts: dict[str, int] = {}
    for row in rows:
        prefix = row["id"].rsplit("-", 1)[0]
        counts[prefix] = counts.get(prefix, 0) + 1
    return counts


def test_hub_sync_row_counts_are_non_decreasing_per_prefix_across_a_sync(
    tmp_path, monkeypatch, fresh_shared_server
):
    """bh-eu2pp / bh-4o07n regression guard: drives the REAL `hub.sync()` (real bd, real
    shared Dolt server, real bulk pass — not a mock of any of it) over a multi-hive fixture,
    three times in a row, and asserts every prefix's row count never decreases across a sync
    — the invariant bh-4o07n's incident was diagnosed with (HQ's per-prefix counts fell
    7185 -> 3477) but that nothing in CI checked before this bead, so the same shape could
    slip through again undetected.

    Manually confirmed (2026-08-22, `bd` 1.1.0) that reinstating bh-4o07n's exact defect —
    `hub_bulk.run_bulk_pass` calling `bd repo remove` on a hive right after bulk-copying it —
    no longer reproduces a wipe against today's `bd`: a bulk copy overwrites `source_repo` to
    the source hive's own (blank) value before the remove runs, and `bd repo remove`'s
    deletion is keyed on `source_repo` matching the removed path, so it now finds nothing to
    delete. The INVARIANT this test asserts is still the right one to hold — it is what
    would have caught bh-4o07n's actual incident (measured against a real aggregate, not a
    hypothesis) and it makes no assumption about *how* a future `hub.sync()` regression might
    shrink a prefix. Verified this test's own assertions have teeth by manually deleting a row
    between two sync() calls in this test and confirming it fails (reverted before commit)."""
    hives: dict[str, Path] = {prefix: tmp_path / "hives" / prefix for prefix in ("hva", "hvb")}

    managed_repos = [
        {"provider": "gh", "org": "x", "repo": prefix, "prefix": prefix} for prefix in hives
    ]
    monkeypatch.setattr(hub.config, "load", lambda: {"managed_repos": managed_repos})
    monkeypatch.setattr(hub.registry, "hive_dir", lambda e: hives[e["prefix"]])

    hub_dir, _ = hub.hub_target()
    _init(hub_dir, hub.HUB_PREFIX)
    _wait_until_accepting("127.0.0.1", fresh_shared_server)
    template_url = _snapshot_empty_server_database(hub_dir, tmp_path / "schema-template")
    for prefix, titles in (("hva", ["one", "two"]), ("hvb", ["three"])):
        path = hives[prefix]
        _init_from_template(path, prefix, template_url=template_url, control_hive=hub_dir)
        _batch_create(path, titles)

    failed = hub.sync()
    assert not failed, failed
    before = _prefix_counts(hub_dir)
    assert set(before) == set(hives), before
    assert all(n > 0 for n in before.values()), before

    # A second sync with nothing changed — the steady state a hub.sync() regression must not
    # disturb.
    failed = hub.sync()
    assert not failed, failed
    after = _prefix_counts(hub_dir)
    for prefix, n in before.items():
        assert after.get(prefix, 0) >= n, (
            f"{prefix} shrank across a sync: {n} -> {after.get(prefix, 0)}"
        )

    # Growing one hive must show up in ITS OWN prefix without any sibling prefix shrinking —
    # the "total holds steady while one prefix is wiped and another grows" shape the per-prefix
    # assertion (not just a total) exists to catch.
    bd("create", "a new one", cwd=hives["hva"])
    failed = hub.sync()
    assert not failed, failed
    grown = _prefix_counts(hub_dir)
    assert grown["hva"] == after["hva"] + 1, grown
    for prefix, n in after.items():
        assert grown.get(prefix, 0) >= n, (
            f"{prefix} shrank across a sync: {n} -> {grown.get(prefix, 0)}"
        )


def test_co_located_database_and_server_databases_against_the_real_server(
    tmp_path, isolated_shared_server
):
    """`server_databases`/`co_located_database` against a REAL `SHOW DATABASES` — proves the
    co-location check (bh-l7sm8 item 3's fallback trigger) sees a genuinely-present database
    and correctly refuses one that was never initialized."""
    hva = isolated_shared_server.database("hva")
    hba = isolated_shared_server.database("hba")
    hive_a = tmp_path / "hive_a"
    _init(hive_a, hva)
    _wait_until_accepting("127.0.0.1", isolated_shared_server.port)
    hub_a = tmp_path / "hub_a"
    _init(hub_a, hba)

    databases = hub_bulk.server_databases(hub_a)
    assert hva in databases
    assert hba in databases

    database = hub_bulk.co_located_database(databases, hive_a, hva)
    assert database == hva

    never_initialized = tmp_path / "not-a-real-hive"
    never_initialized.mkdir()
    assert hub_bulk.co_located_database(databases, never_initialized, "ghost") is None
