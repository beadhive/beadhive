"""Unit tests for `beadhive.hub_bulk` — the cross-database bulk-copy fast path (bh-l7sm8).

Fake-subprocess style, matching `test_hub.py`'s own convention (`hub.run` faked; here
`hub_bulk.run` is faked the same way — `bd sql`/`bd repo remove` never actually spawn).
`tests/test_hub_bulk_int.py` covers the same acceptance bar against a REAL isolated shared
Dolt server; this file is the fast, deterministic half: table-list invariants, SQL-shape
correctness, and `run_bulk_pass`'s orchestration/fallback decisions.
"""

from __future__ import annotations

import json
from collections import namedtuple
from pathlib import Path

# The `bd sql` transport this module's tests fake now lives in `fleet` (shape A) rather than
# in hub_bulk itself (bh-0gvs3) — hub_bulk calls it through `fleet.sql`, so the seam these
# tests patch is `fleet.run`. Same transport, one home; the transport moved to `fleet`.
from beadhive import fleet, hub_bulk

Completed = namedtuple("Completed", "returncode stdout stderr")


def _metadata(
    hive_dir: Path,
    *,
    dolt_mode: str = "server",
    server_database: str | None = None,
    dolt_database: str | None = None,
):
    """`dolt_database` writes the key bd itself resolves — needed to reproduce the CACHE-STORE
    shape measured on the reference host (`dolt_mode: server` + `dolt_database: beads` and NO
    `dolt_server_database`), which is what defeated the original co-location check (bh-4o07n)."""
    hive_dir.mkdir(parents=True, exist_ok=True)
    (hive_dir / ".beads").mkdir(exist_ok=True)
    data = {"dolt_mode": dolt_mode}
    if server_database:
        data["dolt_server_database"] = server_database
    if dolt_database:
        data["dolt_database"] = dolt_database
    (hive_dir / ".beads" / "metadata.json").write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# The curated table list — the whole risk of this bead.
# ---------------------------------------------------------------------------


def test_content_tables_match_the_bead_curated_list():
    assert hub_bulk.CONTENT_TABLES == (
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


def test_deny_tables_match_the_bead_deny_list():
    assert set(hub_bulk.DENY_TABLES) == {
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
    }


def test_excluded_views_and_undecided_tables():
    assert set(hub_bulk.EXCLUDED_VIEWS) == {"ready_issues", "blocked_issues"}
    assert set(hub_bulk.NOT_COPIED_UNDECIDED) == {
        "custom_statuses",
        "custom_types",
        "routes",
        "interactions",
        "issue_snapshots",
    }


def test_content_tables_never_overlap_anything_denied_excluded_or_undecided():
    forbidden = (
        set(hub_bulk.DENY_TABLES)
        | set(hub_bulk.EXCLUDED_VIEWS)
        | set(hub_bulk.NOT_COPIED_UNDECIDED)
    )
    assert not (set(hub_bulk.CONTENT_TABLES) & forbidden)


def test_content_table_order_is_fk_safe():
    """`issues`/`wisps` (the two FK-referenced parents) must land before anything that
    references them, or a real INSERT would fail on a foreign-key violation."""
    order = list(hub_bulk.CONTENT_TABLES)
    assert order.index("issues") < order.index("dependencies")
    assert order.index("issues") < order.index("labels")
    assert order.index("issues") < order.index("comments")
    assert order.index("issues") < order.index("events")
    assert order.index("wisps") < order.index("wisp_dependencies")
    assert order.index("wisps") < order.index("wisp_labels")
    assert order.index("wisps") < order.index("wisp_comments")
    assert order.index("wisps") < order.index("wisp_events")


# ---------------------------------------------------------------------------
# The upsert SQL shape (bh-z4z52's own measured Dolt 2.2.3 gotchas)
# ---------------------------------------------------------------------------


def test_upsert_query_uses_source_alias_never_values():
    query = hub_bulk._upsert_query("issues", "otherdb", ["id", "title"], ["id"])
    assert "AS s" in query
    assert "s.`title`" in query
    assert "VALUES(" not in query


def test_upsert_query_full_pk_table_updates_key_to_itself():
    """`labels`-shaped tables (composite PK covering every column) have nothing left to
    update — the fallback keeps the SQL valid by updating the key column(s) to themselves."""
    query = hub_bulk._upsert_query(
        "labels", "otherdb", ["issue_id", "label"], ["issue_id", "label"]
    )
    assert "ON DUPLICATE KEY UPDATE `issue_id` = s.`issue_id`, `label` = s.`label`" in query


def test_upsert_query_partial_pk_table_updates_only_non_key_columns():
    query = hub_bulk._upsert_query("issues", "otherdb", ["id", "title", "status"], ["id"])
    assert "UPDATE `title` = s.`title`, `status` = s.`status`" in query
    assert "`id` = s.`id`" not in query


def test_upsert_query_never_uses_insert_ignore_or_replace():
    query = hub_bulk._upsert_query("issues", "otherdb", ["id", "title"], ["id"])
    assert "INSERT IGNORE" not in query
    assert "REPLACE" not in query
    assert query.startswith("INSERT INTO")


# ---------------------------------------------------------------------------
# server_databases / co_located_database
# ---------------------------------------------------------------------------


def test_server_databases_parses_show_databases(tmp_path, monkeypatch):
    def fake_run(cmd, **k):
        assert cmd[-3:] == ["-q", "SHOW DATABASES", "--json"] or cmd[-2] == "SHOW DATABASES"
        return Completed(0, json.dumps([{"Database": "bh"}, {"Database": "hq"}]), "")

    monkeypatch.setattr(fleet, "run", fake_run)
    assert hub_bulk.server_databases(tmp_path) == {"bh", "hq"}


def test_server_databases_empty_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "run", lambda cmd, **k: Completed(1, "", "boom"))
    assert hub_bulk.server_databases(tmp_path) == set()


def test_co_located_database_none_for_embedded_hive(tmp_path):
    hive = tmp_path / "hive"
    _metadata(hive, dolt_mode="embedded")
    assert hub_bulk.co_located_database({"hive"}, hive, "hive") is None


def test_co_located_database_none_when_not_present_on_server(tmp_path):
    hive = tmp_path / "hive"
    _metadata(hive, dolt_mode="server")
    assert hub_bulk.co_located_database({"some-other-db"}, hive, "hive") is None


def test_co_located_database_resolves_the_persisted_server_database_key(tmp_path):
    """A real migration persists `dolt_server_database` explicitly (`storage_migrate
    .ensure_server_database_persisted`) — `bh-infra` -> `bh_infra`, confirmed against a real
    server in this bead's own manual testing. `co_located_database` must resolve through that
    persisted key, not re-derive a name from the prefix itself."""
    hive = tmp_path / "bh-infra"
    _metadata(hive, dolt_mode="server", server_database="bh_infra")
    assert hub_bulk.co_located_database({"bh_infra"}, hive, "bh-infra") == "bh_infra"


def test_co_located_database_prefers_explicit_server_database_key(tmp_path):
    hive = tmp_path / "hive"
    _metadata(hive, dolt_mode="server", server_database="renamed_db")
    assert hub_bulk.co_located_database({"renamed_db"}, hive, "hive") == "renamed_db"


# ---------------------------------------------------------------------------
# copy_table / copy_hive
# ---------------------------------------------------------------------------

_DESCRIBE_ISSUES = [
    {"Field": "id", "Key": "PRI"},
    {"Field": "title", "Key": ""},
]


def test_copy_table_success(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        query = cmd[cmd.index("-q") + 1]
        if query.startswith("DESCRIBE"):
            return Completed(0, json.dumps(_DESCRIBE_ISSUES), "")
        return Completed(0, json.dumps({"rows_affected": 1}), "")

    monkeypatch.setattr(fleet, "run", fake_run)
    ok, detail = hub_bulk.copy_table(tmp_path, "otherdb", "issues", {})
    assert ok
    assert detail == ""
    insert_calls = [c for c in calls if "INSERT INTO" in c[cmd_query_index(c)]]
    assert len(insert_calls) == 1
    assert "`otherdb`.`issues`" in insert_calls[0][cmd_query_index(insert_calls[0])]


def cmd_query_index(cmd):
    return cmd.index("-q") + 1


def test_copy_table_schema_read_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "run", lambda cmd, **k: Completed(1, "", "boom"))
    ok, detail = hub_bulk.copy_table(tmp_path, "otherdb", "issues", {})
    assert not ok
    assert "schema" in detail


def test_copy_table_insert_failure_surfaces_bd_error_line(tmp_path, monkeypatch):
    def fake_run(cmd, **k):
        query = cmd[cmd_query_index(cmd)]
        if query.startswith("DESCRIBE"):
            return Completed(0, json.dumps(_DESCRIBE_ISSUES), "")
        return Completed(1, "", "Error: something broke\n")

    monkeypatch.setattr(fleet, "run", fake_run)
    ok, detail = hub_bulk.copy_table(tmp_path, "otherdb", "issues", {})
    assert not ok
    assert detail == "Error: something broke"


def test_copy_failure_reason_survives_the_multiline_json_bd_really_emits(tmp_path, monkeypatch):
    """bh-f8rdk: the live 2026-08-09 sync reported

        x bh: bulk copy from `bh` failed (issues: {) — leaving it registered ...

    because bd's SQL failure is a multi-line JSON object and the reason was taken as its first
    line. The de-registration half was correct; the operator just had nothing actionable, and
    diagnosing it meant re-running the copy by hand. Feeds that JSON shape and asserts the reason
    arrives intact — through `copy_hive`, which composes the `<table>: <detail>` string the
    operator actually reads."""
    bd_json_error = (
        "{\n  \"error\": \"Duplicate entry 'bh-1' for key 'issues.PRIMARY'\",\n"
        '  "query": "INSERT INTO `issues` ..."\n}\n'
    )

    def fake_run(cmd, **k):
        query = cmd[cmd_query_index(cmd)]
        if query.startswith("DESCRIBE"):
            return Completed(0, json.dumps(_DESCRIBE_ISSUES), "")
        return Completed(1, bd_json_error, "")

    monkeypatch.setattr(fleet, "run", fake_run)
    ok, detail = hub_bulk.copy_hive(tmp_path, "otherdb", {})

    assert not ok
    assert detail == "issues: Duplicate entry 'bh-1' for key 'issues.PRIMARY'"
    assert detail != "issues: {"  # the exact string the report carries


def test_copy_table_caches_schema_across_calls(tmp_path, monkeypatch):
    describe_calls = []

    def fake_run(cmd, **k):
        query = cmd[cmd_query_index(cmd)]
        if query.startswith("DESCRIBE"):
            describe_calls.append(query)
            return Completed(0, json.dumps(_DESCRIBE_ISSUES), "")
        return Completed(0, json.dumps({"rows_affected": 0}), "")

    monkeypatch.setattr(fleet, "run", fake_run)
    cache: dict = {}
    hub_bulk.copy_table(tmp_path, "db1", "issues", cache)
    hub_bulk.copy_table(tmp_path, "db2", "issues", cache)
    assert len(describe_calls) == 1


def test_copy_hive_stops_at_first_failing_table(tmp_path, monkeypatch):
    seen_tables = []

    def fake_run(cmd, **k):
        query = cmd[cmd_query_index(cmd)]
        if query.startswith("DESCRIBE"):
            table = query.split("`")[1]
            seen_tables.append(table)
            return Completed(0, json.dumps(_DESCRIBE_ISSUES), "")
        # fail on the second table copied ("dependencies", per CONTENT_TABLES order)
        if "`dependencies`" in query:
            return Completed(1, "", "Error: dependencies exploded\n")
        return Completed(0, json.dumps({"rows_affected": 0}), "")

    monkeypatch.setattr(fleet, "run", fake_run)
    ok, detail = hub_bulk.copy_hive(tmp_path, "otherdb", {})
    assert not ok
    assert "dependencies" in detail
    # never even attempted a table past the failing one
    assert "labels" not in seen_tables


def test_copy_hive_success_touches_every_content_table_and_nothing_else(tmp_path, monkeypatch):
    inserted_into = []

    def fake_run(cmd, **k):
        query = cmd[cmd_query_index(cmd)]
        if query.startswith("DESCRIBE"):
            return Completed(0, json.dumps(_DESCRIBE_ISSUES), "")
        table = query.split("INSERT INTO `")[1].split("`")[0]
        inserted_into.append(table)
        return Completed(0, json.dumps({"rows_affected": 0}), "")

    monkeypatch.setattr(fleet, "run", fake_run)
    ok, detail = hub_bulk.copy_hive(tmp_path, "otherdb", {})
    assert ok, detail
    assert inserted_into == list(hub_bulk.CONTENT_TABLES)
    forbidden = (
        set(hub_bulk.DENY_TABLES)
        | set(hub_bulk.EXCLUDED_VIEWS)
        | set(hub_bulk.NOT_COPIED_UNDECIDED)
    )
    assert not (set(inserted_into) & forbidden)


# ---------------------------------------------------------------------------
# validate_ancestors
# ---------------------------------------------------------------------------


def test_validate_ancestors_parses_violation_count(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fleet, "run", lambda cmd, **k: Completed(0, json.dumps([{"violations": 3}]), "")
    )
    assert hub_bulk.validate_ancestors(tmp_path) == 3


def test_validate_ancestors_none_on_query_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "run", lambda cmd, **k: Completed(1, "", "boom"))
    assert hub_bulk.validate_ancestors(tmp_path) is None


def test_validate_ancestors_none_on_unparseable_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "run", lambda cmd, **k: Completed(0, json.dumps({}), ""))
    assert hub_bulk.validate_ancestors(tmp_path) is None


# ---------------------------------------------------------------------------
# run_bulk_pass — the orchestration: co-location routing, changed/unchanged, fallback safety
# ---------------------------------------------------------------------------


def _fake_bulk_server(*, databases, insert_ok=True, deregister_ok=True, violations=0):
    """A fake `hub_bulk.run` that answers SHOW DATABASES/DESCRIBE/INSERT/repo-remove/ancestor-
    validation queries, recording every SQL statement issued (for the "never touches a denied
    table" assertion) and every path `bd repo remove` was called with."""
    queries: list[str] = []
    deregistered: list[str] = []

    def fake_run(cmd, **k):
        if cmd[3:5] == ["repo", "remove"]:
            deregistered.append(cmd[5])
            return Completed(0 if deregister_ok else 1, "", "")
        query = cmd[cmd_query_index(cmd)]
        queries.append(query)
        if query == "SHOW DATABASES":
            return Completed(0, json.dumps([{"Database": d} for d in databases]), "")
        if query.startswith("DESCRIBE"):
            return Completed(0, json.dumps(_DESCRIBE_ISSUES), "")
        if query.startswith("WITH RECURSIVE"):
            return Completed(0, json.dumps([{"violations": violations}]), "")
        return Completed(0 if insert_ok else 1, json.dumps({"rows_affected": 0}), "insert failed")

    return fake_run, queries, deregistered


def test_run_bulk_pass_empty_entries_makes_no_calls(tmp_path, monkeypatch):
    def fail(cmd, **k):
        raise AssertionError("run() must not be called for an empty entry list")

    monkeypatch.setattr(fleet, "run", fail)
    assert hub_bulk.run_bulk_pass(tmp_path, []) == []


def test_run_bulk_pass_skips_a_hive_with_no_server_database(tmp_path, monkeypatch):
    hive = tmp_path / "ghost"
    _metadata(hive, dolt_mode="server")
    fake_run, queries, deregistered = _fake_bulk_server(databases={"other"})
    monkeypatch.setattr(fleet, "run", fake_run)

    hydrated = hub_bulk.run_bulk_pass(tmp_path, [("ghost", hive, True, False)])

    assert hydrated == []
    assert deregistered == []  # never touched — bd repo sync's own pass owns it entirely
    assert not any("INSERT INTO" in q for q in queries)


def test_run_bulk_pass_copies_a_changed_co_located_hive_and_keeps_it_registered(
    tmp_path, monkeypatch
):
    hive = tmp_path / "bh"
    _metadata(hive, dolt_mode="server")
    fake_run, queries, deregistered = _fake_bulk_server(databases={"bh"})
    monkeypatch.setattr(fleet, "run", fake_run)

    hydrated = hub_bulk.run_bulk_pass(tmp_path, [("bh", hive, True, False)])

    assert hydrated == ["bh"]
    assert (
        deregistered == []
    )  # bh-4o07n REGRESSION GUARD: a bulk-copied hive must STAY REGISTERED —
    # de-registering made the trailing `bd repo sync` rebuild the aggregate without it and
    # DELETE its rows (HQ 7185 -> 3477 on the first real run).
    assert any("INSERT INTO" in q for q in queries)
    assert any(q.startswith("WITH RECURSIVE") for q in queries)  # ancestor check ran


def test_run_bulk_pass_unchanged_hive_is_hydrated_without_recopying(tmp_path, monkeypatch):
    hive = tmp_path / "bh"
    _metadata(hive, dolt_mode="server")
    fake_run, queries, deregistered = _fake_bulk_server(databases={"bh"})
    monkeypatch.setattr(fleet, "run", fake_run)

    hydrated = hub_bulk.run_bulk_pass(tmp_path, [("bh", hive, False, False)])

    assert hydrated == ["bh"]
    assert (
        deregistered == []
    )  # bh-4o07n REGRESSION GUARD: a bulk-copied hive must STAY REGISTERED —
    # de-registering made the trailing `bd repo sync` rebuild the aggregate without it and
    # DELETE its rows (HQ 7185 -> 3477 on the first real run).
    assert not any("INSERT INTO" in q for q in queries)  # nothing to refresh


def test_run_bulk_pass_leaves_a_hive_registered_when_copy_fails(tmp_path, monkeypatch):
    hive = tmp_path / "bh"
    _metadata(hive, dolt_mode="server")
    fake_run, queries, deregistered = _fake_bulk_server(databases={"bh"}, insert_ok=False)
    monkeypatch.setattr(fleet, "run", fake_run)

    hydrated = hub_bulk.run_bulk_pass(tmp_path, [("bh", hive, True, False)])

    assert hydrated == []  # bd repo sync's own pass is the fallback for this hive this round
    assert deregistered == []
    assert not any(q.startswith("WITH RECURSIVE") for q in queries)  # nothing hydrated to check


def test_run_bulk_pass_reports_but_does_not_raise_on_ancestor_violations(
    tmp_path, monkeypatch, capsys
):
    hive = tmp_path / "bh"
    _metadata(hive, dolt_mode="server")
    fake_run, _queries, _dereg = _fake_bulk_server(databases={"bh"}, violations=2)
    monkeypatch.setattr(fleet, "run", fake_run)

    hydrated = hub_bulk.run_bulk_pass(tmp_path, [("bh", hive, True, False)])

    assert hydrated == ["bh"]  # a violation is reported, never used to un-hydrate a hive
    err = capsys.readouterr().err
    assert "2 ancestor-dependency violation" in err


def test_run_bulk_pass_never_queries_a_denied_or_undecided_table(tmp_path, monkeypatch):
    hive = tmp_path / "bh"
    _metadata(hive, dolt_mode="server")
    fake_run, queries, _dereg = _fake_bulk_server(databases={"bh"})
    monkeypatch.setattr(fleet, "run", fake_run)

    hub_bulk.run_bulk_pass(tmp_path, [("bh", hive, True, False)])

    forbidden = (
        set(hub_bulk.DENY_TABLES)
        | set(hub_bulk.EXCLUDED_VIEWS)
        | set(hub_bulk.NOT_COPIED_UNDECIDED)
    )
    joined = "\n".join(queries)
    for table in forbidden:
        assert f"`{table}`" not in joined, table


def test_run_bulk_pass_mixed_co_located_and_not(tmp_path, monkeypatch):
    """A realistic mixed round: one co-located hive, one not — only the co-located one is
    bulk-hydrated; the other is left entirely alone for `bd repo sync`."""
    bh_hive = tmp_path / "bh"
    _metadata(bh_hive, dolt_mode="server")
    ghost_hive = tmp_path / "bc-workspace"
    _metadata(ghost_hive, dolt_mode="embedded")
    fake_run, _queries, deregistered = _fake_bulk_server(databases={"bh"})
    monkeypatch.setattr(fleet, "run", fake_run)

    hydrated = hub_bulk.run_bulk_pass(
        tmp_path, [("bh", bh_hive, True, False), ("bc-workspace", ghost_hive, True, False)]
    )

    assert hydrated == ["bh"]
    assert deregistered == []  # bh-4o07n: neither hive is ever de-registered


# ---------------------------------------------------------------------------
# bh-4o07n — the two defects the first real `bh sync` exposed. Both of these
# reproduce the exact on-disk shapes measured on the reference host.
# ---------------------------------------------------------------------------


def test_a_cache_store_is_never_co_located(tmp_path):
    """THE defect: every `_fetch_cache` hydration artifact on the reference host carries
    `dolt_mode: server` + bd's generic `dolt_database: beads` (bootstrapped with
    BEADS_DOLT_SHARED_SERVER=1, bh-hpeye). Both of the original signals therefore passed, and
    FOUR unrelated hives — homelab, workspace, dell-x-nvidia-hackathon, agentic-git-flow — were
    each 'bulk-copied from `beads`', a database belonging to none of them and holding 14
    ag-hp issues. They are indistinguishable from one another by identity too: all five share
    one project_id, and it is the same one `beads` itself carries."""
    cache = tmp_path / "homelab"
    _metadata(cache, dolt_mode="server", dolt_database="beads")

    assert hub_bulk.co_located_database({"beads"}, cache, "hl", is_cache=True) is None
    # ...and the same store would still be refused on the generic name alone.
    assert hub_bulk.co_located_database({"beads"}, cache, "hl", is_cache=False) is None


def test_generic_database_names_are_never_co_located(tmp_path):
    """bd's own defaults cannot be one hive's private database on a SHARED server — that is
    exactly what bh-g5ujg's migration preflight refuses. Resolving to one means the hive's
    metadata was never repointed, not that it is co-located."""
    hive = tmp_path / "hl"
    for generic in ("beads", "beads_global"):
        _metadata(hive, dolt_mode="server", dolt_database=generic)
        assert hub_bulk.co_located_database({generic}, hive, "hl", is_cache=False) is None


def test_a_real_co_located_hive_is_still_accepted(tmp_path):
    """The guards must not disable the fast path for genuine hives — five of the eight migrated
    hives on the reference host carry NO `dolt_server_database` key, so requiring one was
    measured and rejected as a fix."""
    hive = tmp_path / "bh-infra"
    _metadata(hive, dolt_mode="server", dolt_database="bh_infra")

    assert (
        hub_bulk.co_located_database({"bh_infra"}, hive, "bh-infra", is_cache=False) == "bh_infra"
    )
