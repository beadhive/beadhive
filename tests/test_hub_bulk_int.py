"""Integration: `hub_bulk`'s cross-database bulk copy (bh-l7sm8) against a REAL, isolated
shared Dolt server — not a mock of bd's SQL surface.

Proves the acceptance bar this bead's own bead text demands directly against real `bd`/Dolt
behavior:

* row-count AND content parity for ``issues``/``dependencies``/``labels``/``comments`` against
  a REAL `bd repo sync`-produced aggregate (not just issues — labels explicitly, the table an
  earlier prototype silently dropped);
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

import json
import socket
import time

import pytest

from beadhive import hub_bulk
from beadhive.run import run
from harness.beads import bd, skip_if_no_bd
from harness.world import free_port, reap_dolt_server

pytestmark = [pytest.mark.integration, skip_if_no_bd]

_TIMEOUT = 60
_STARTUP_TIMEOUT = 30.0


@pytest.fixture
def isolated_shared_server(tmp_path, monkeypatch):
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
    monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(server_dir))
    monkeypatch.setenv("BEADS_DOLT_SERVER_PORT", str(port))
    yield port
    reap_dolt_server(server_dir)


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
    run(
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
        check=True,
        capture=True,
        timeout=_TIMEOUT,
    )
    run(["git", "config", "beads.role", "maintainer"], cwd=str(path), check=True, capture=True)


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


def _list_ids(path):
    res = bd("list", "--json", cwd=path, capture=True)
    return json.loads(res.stdout or "[]")


def test_bulk_copy_matches_a_real_bd_produced_aggregate(tmp_path, isolated_shared_server):
    hive_a = tmp_path / "hive_a"
    _init(hive_a, "hva")
    _wait_until_accepting("127.0.0.1", isolated_shared_server)

    bd("create", "issue one", "--label", "alpha", cwd=hive_a)
    id1 = _list_ids(hive_a)[0]["id"]
    bd("comment", id1, "a comment", cwd=hive_a)
    bd("update", id1, "--status", "in_progress", cwd=hive_a)
    bd("create", "issue two", cwd=hive_a)
    id2 = next(x["id"] for x in _list_ids(hive_a) if x["id"] != id1)
    bd("dep", "add", id2, id1, cwd=hive_a)  # id2 depends on id1

    # A wisp (bd has no direct CLI to mint one outside agent-orchestration flows — inserted
    # directly, matching this table's own schema exactly as `DESCRIBE wisps` reports it).
    _sql(
        hive_a,
        "INSERT INTO wisps (id, title, description, design, acceptance_criteria, notes, "
        "ephemeral) VALUES ('hva-wisp1', 'a wisp', '', '', '', '', 1)",
    )
    # Per-database vocabulary — the collision risk `hub_bulk`'s own docstring names.
    _sql(hive_a, "INSERT INTO custom_statuses (name, category) VALUES ('triage', 'open')")

    # THE BASELINE: a real `bd repo sync`-produced aggregate.
    hub_a = tmp_path / "hub_a"
    _init(hub_a, "hba")
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

    # THE FAST PATH: `hub_bulk`'s cross-database copy, into a SEPARATE fresh target — never
    # sharing an aggregate with the baseline, so a false-positive "match" from shared state is
    # structurally impossible.
    hub_b = tmp_path / "hub_b"
    _init(hub_b, "hbb")
    ok, detail = hub_bulk.copy_hive(hub_b, "hva", {})
    assert ok, detail

    # --- row-count AND content parity against the REAL bd-produced aggregate ---
    for table, cols in (
        ("issues", "id,title,status,priority,created_at,updated_at"),
        ("dependencies", "id,issue_id,type,depends_on_issue_id"),
        ("labels", "issue_id,label"),  # the table an earlier prototype silently dropped
        ("comments", "id,issue_id,author,text"),
    ):
        expected = _rows(hub_a, table, cols)
        actual = _rows(hub_b, table, cols)
        assert actual == expected, f"{table}: bulk copy diverged from the bd-produced aggregate"
        assert actual, f"{table}: fixture produced no rows — the comparison above is vacuous"

    # --- events: bd's own path does NOT replay real history (measured, not assumed) ---
    source_events = _rows(hive_a, "events", "id,issue_id,event_type")
    bulk_events = _rows(hub_b, "events", "id,issue_id,event_type")
    baseline_events = _rows(hub_a, "events", "id,issue_id,event_type")
    assert bulk_events == source_events, "hub_bulk must copy the REAL event log verbatim"
    assert bulk_events != baseline_events, (
        "if this ever matches, bd repo sync started replaying real event history — the "
        "divergence this test pins down no longer holds and hub_bulk's own docstring "
        "rationale for testing events against source-fidelity (not aggregate-parity) needs "
        "re-checking"
    )

    # --- wisps: bd repo sync never touches this family; hub_bulk's copy is a widening ---
    assert _rows(hub_a, "wisps", "id,title") == []
    bulk_wisps = _rows(hub_b, "wisps", "id,title")
    assert bulk_wisps == _rows(hive_a, "wisps", "id,title")
    assert len(bulk_wisps) == 1

    # --- decided-not-to-copy vocabulary tables: zero rows in the target, despite the source ---
    assert _rows(hive_a, "custom_statuses", "name") != []  # fixture sanity: source really has one
    assert _rows(hub_b, "custom_statuses", "name") == []

    # --- identity/bookkeeping untouched: hub_b can still mint ITS OWN prefix afterward ---
    bd("create", "sanity check", cwd=hub_b)
    hub_b_ids = [x["id"] for x in _list_ids(hub_b)]
    assert any(i.startswith("hbb-") for i in hub_b_ids), hub_b_ids
    assert not any(i.startswith("hva-") for i in hub_b_ids if i not in (id1, id2)), hub_b_ids

    # --- set-based ancestor validation runs clean over a real, well-formed graph ---
    assert hub_bulk.validate_ancestors(hub_b) == 0


def test_co_located_database_and_server_databases_against_the_real_server(
    tmp_path, isolated_shared_server
):
    """`server_databases`/`co_located_database` against a REAL `SHOW DATABASES` — proves the
    co-location check (bh-l7sm8 item 3's fallback trigger) sees a genuinely-present database
    and correctly refuses one that was never initialized."""
    hive_a = tmp_path / "hive_a"
    _init(hive_a, "hva")
    _wait_until_accepting("127.0.0.1", isolated_shared_server)
    hub_a = tmp_path / "hub_a"
    _init(hub_a, "hba")

    databases = hub_bulk.server_databases(hub_a)
    assert "hva" in databases
    assert "hba" in databases

    database = hub_bulk.co_located_database(databases, hive_a, "hva")
    assert database == "hva"

    never_initialized = tmp_path / "not-a-real-hive"
    never_initialized.mkdir()
    assert hub_bulk.co_located_database(databases, never_initialized, "ghost") is None
