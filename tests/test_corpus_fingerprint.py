"""Corpus fingerprint primitive (bh-ksmt0.2). Fake readers only; no store, no server."""

from __future__ import annotations

import os
import shutil
from types import SimpleNamespace

import pytest

from beadhive import corpus_fingerprint as cf


def facts(**over):
    base = dict(
        project_id="p1",
        prefix="bh",
        schema_version=59,
        main_head="h1",
        branches={"main": "h1", "side": "h2"},
        issue_rows={"bh-1": {"id": "bh-1", "t": "a"}, "bh-2": {"id": "bh-2", "t": "b"}},
        tables={"labels": [{"i": "bh-1", "l": "x"}], "comments": None},
    )
    base.update(over)
    return cf.CorpusFacts(**base)


TABLES = ("labels", "comments")


def fp(tier=cf.TIER_STRONG, scope=cf.SCOPE_COMMITTED, **over):
    return cf.compute(facts(**over), tier, tables=TABLES, scope=scope)


def test_equal_facts_equal_digest_and_roundtrip():
    a, b = fp(), fp()
    assert a.digest == b.digest and cf.compare(a, b).equal
    assert cf.Fingerprint.from_dict(a.to_dict()) == a
    assert a.to_dict()["schema"] == "bh.corpus-fingerprint/3"


def test_row_order_is_irrelevant_and_content_matters():
    swapped = {"bh-2": {"id": "bh-2", "t": "b"}, "bh-1": {"id": "bh-1", "t": "a"}}
    assert fp(issue_rows=swapped).digest == fp().digest
    changed = {"bh-1": {"id": "bh-1", "t": "CHANGED"}, "bh-2": {"id": "bh-2", "t": "b"}}
    diff = cf.compare(fp(), fp(issue_rows=changed))
    assert not diff.equal and diff.differing == ("issue_rows_digest",)


def test_mismatch_names_the_component():
    diff = cf.compare(fp(), fp(main_head="zzz", tables={"labels": [], "comments": None}))
    assert set(diff.differing) == {"main_head", "tables.labels.rows", "tables.labels.digest"}
    assert diff.detail["main_head"] == ("h1", "zzz")


def test_lost_branch_detected_in_strong_but_not_corpus():
    lost = {"main": "h1"}
    assert not cf.compare(fp(), fp(branches=lost)).equal
    assert cf.compare(fp(cf.TIER_CORPUS), fp(cf.TIER_CORPUS, branches=lost, main_head="x")).equal


def test_corpus_tier_omits_commit_hashes():
    comps = fp(cf.TIER_CORPUS).components
    assert "main_head" not in comps and "branches" not in comps


def test_cross_tier_comparison_refused():
    with pytest.raises(cf.FingerprintTierMismatch):
        cf.compare(fp(cf.TIER_STRONG), fp(cf.TIER_CORPUS))


def test_unknown_schema_version_refuses_to_fingerprint():
    with pytest.raises(cf.FingerprintIncomplete, match="schema_version"):
        fp(schema_version=None)


def test_strong_without_head_refused():
    with pytest.raises(cf.FingerprintIncomplete):
        fp(main_head="")


def test_missing_table_is_recorded_not_silent_and_exclusions_named():
    f = fp()
    assert f.components["tables"]["comments"] == {"present": False}
    assert "dolt-transport-cache" in f.excluded


def test_tampered_document_refused():
    doc = fp().to_dict()
    doc["components"]["issue_count"] = 99
    with pytest.raises(cf.FingerprintError, match="digest"):
        cf.Fingerprint.from_dict(doc)


def test_configured_tables_extends_defaults_off_by_default():
    assert cf.configured_tables(None) == cf.DEFAULT_TABLES
    cfg = {"migration": {"fingerprint": {"tables": ["wisps", "labels"]}}}
    assert cf.configured_tables(cfg) == (*cf.DEFAULT_TABLES, "wisps")


def test_bd_sql_reader_collects_facts(monkeypatch, tmp_path):
    answers = {
        "HASHOF": [{"h": "h1"}],
        "dolt_branches": [{"name": "main", "hash": "h1"}],
        "information_schema": [{"table_name": "labels"}],
        "`issues`": [{"id": "bh-1"}],
        "dolt_ignore": [{"pattern": "events", "ignored": 1}],
        "dolt_status": [],
        "issue_prefix": [{"value": "bh"}],
        "`labels`": [{"l": "x"}],
        "dolt_hashof_db('main')": [{"h": "root"}],
        "dolt_hashof_db()": [{"h": "dbh"}],
        "dolt_hashof_table": [{"h": "th"}],
    }

    def sql(_dir, query):
        return next(v for k, v in answers.items() if k in query)

    monkeypatch.setattr(cf.store_locator, "dolt_mode", lambda d: "server")
    monkeypatch.setattr(cf.store_locator, "project_id", lambda d: "p1")
    probe = lambda d, dolt_mode: SimpleNamespace(version=59)  # noqa: E731
    got = cf.BdSqlCorpusReader(sql, probe=probe).read(tmp_path, ("labels", "comments"))
    assert got.schema_version == 59 and got.prefix == "bh" and got.project_id == "p1"
    assert got.tables == {"labels": [{"l": "x"}], "comments": None}
    assert got.native == {"db": "dbh", "root": "root", "tables": {"issues": "th", "labels": "th"}}
    assert (
        cf.fingerprint(
            cf.BdSqlCorpusReader(sql, probe=probe), tmp_path, "strong", tables=("labels",)
        ).tier
        == "strong"
    )


def test_bd_sql_reader_rejects_bad_table_name(tmp_path):
    reader = cf.BdSqlCorpusReader(
        lambda d, q: [], probe=lambda d, dolt_mode: SimpleNamespace(version=1)
    )
    with pytest.raises(cf.FingerprintError):
        reader.read(tmp_path, ("x; drop table issues",))


# ---- bh-m4ups: canonicalisation and the native cross-check -------------------------------------


def test_datetime_json_bool_render_differences_do_not_change_the_digest():
    server = {"id": "a", "at": "2026-09-23T20:38:43Z", "m": '{"a":1,"b":[1,2.5,"x"]}', "f": 1}
    embedded = {
        "id": "a",
        "at": "2026-09-23 20:38:43",
        "m": {"b": [1, 2.5, "x"], "a": 1},
        "f": True,
    }
    assert fp(issue_rows={"a": server}).digest == fp(issue_rows={"a": embedded}).digest
    assert fp(issue_rows={"a": {**server, "at": "2026-09-23T20:38:44Z"}}).digest != (
        fp(issue_rows={"a": server}).digest
    )


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("2026-09-23 20:38:43", "2026-09-23T20:38:43Z"),
        ("2026-09-23T20:38:43.500000Z", "2026-09-23T20:38:43.5Z"),
        ("2026-09-23 20:38:43.5", "2026-09-23T20:38:43.5Z"),
        ('{"b":1,"a":[2]}', '{"a":[2],"b":1}'),
        ('{"a": 1}', '{"a": 1}'),  # not Dolt's compact form: free text stays verbatim
        ("{not json", "{not json"),
        ("", ""),
        (None, None),
        (True, 1),
        (2.0, 2),
        (2.5, 2.5),
        ("ünï 日本", "ünï 日本"),
    ],
)
def test_canonical_value_rules(raw, want):
    assert cf.canonical_value(raw) == want


def test_null_equals_an_omitted_column_but_not_the_empty_string():
    server = {"id": "a", "x": None, "y": "k"}  # bd sql --json: "x": null
    embedded = {"id": "a", "y": "k"}  # dolt sql -r json: key omitted
    assert fp(issue_rows={"a": server}).digest == fp(issue_rows={"a": embedded}).digest


def test_null_and_empty_string_stay_distinct():
    a = fp(issue_rows={"a": {"id": "a", "x": None}})
    b = fp(issue_rows={"a": {"id": "a", "x": ""}})
    assert a.digest != b.digest


def test_native_hashes_ride_the_strong_tier_only_and_are_compared():
    native = {"db": "d1", "tables": {"labels": "l1", "issues": "i1", "other": "o"}}
    strong = fp(native=native, scope=cf.SCOPE_WORKING)
    assert strong.native == {"db": "d1", "tables": {"issues": "i1", "labels": "l1"}}
    assert fp(cf.TIER_CORPUS, native=native).native is None
    assert cf.Fingerprint.from_dict(strong.to_dict()) == strong
    same = cf.compare(strong, fp(native=native, scope=cf.SCOPE_WORKING))
    assert same.equal
    bad = fp(
        native={"db": "d1", "tables": {"labels": "DIFFERENT", "issues": "i1"}},
        scope=cf.SCOPE_WORKING,
    )
    diff = cf.compare(strong, bad)
    assert not diff.equal and diff.differing == ("dolt_native.tables.labels",)
    # one side unable to read them (a scratch on a shared server) is not a false mismatch
    assert cf.compare(strong, fp(scope=cf.SCOPE_WORKING)).equal


def test_old_schema_versions_are_refused_with_a_clear_message():
    old = {**fp().to_dict(), "schema": "bh.corpus-fingerprint/1"}
    with pytest.raises(cf.FingerprintVersionMismatch, match="predates.*cannot be compared"):
        cf.Fingerprint.from_dict(old)
    legacy = cf.Fingerprint(**{**fp().__dict__, "schema": "bh.corpus-fingerprint/1"})
    with pytest.raises(cf.FingerprintVersionMismatch, match="refusing to compare"):
        cf.compare(legacy, fp())


# ---- scopes (bh-omxyj) -------------------------------------------------------------------------

EV_TABLES = ("labels", "events")


def scoped(scope, *, events, ignored=("events",), **over):
    base = dict(
        tables={"labels": [{"i": "bh-1"}], "events": events},
        ignored=ignored,
        local_counts={"events": len(events or ()), "wisps": 4},
    )
    base.update(over)
    return cf.compute(facts(**base), cf.TIER_STRONG, tables=EV_TABLES, scope=scope)


def test_committed_scope_excludes_ignored_tables_but_reports_them():
    a = scoped(cf.SCOPE_COMMITTED, events=[{"n": 1}, {"n": 2}])
    b = scoped(cf.SCOPE_COMMITTED, events=None, local_counts={"wisps": 0})
    assert set(a.components["tables"]) == {"labels"} and "local_only" not in a.components
    assert a.digest == b.digest and cf.compare(a, b).equal
    assert a.local_only["events"]["rows"] == 2 and a.local_only["wisps"] == {"rows": 4}
    assert "dolt-ignored-tables" in a.excluded
    assert cf.local_only_differences(a, b)
    assert cf.Fingerprint.from_dict(a.to_dict()) == a


def test_working_scope_digests_the_requested_local_tables_and_catches_drift():
    a = scoped(cf.SCOPE_WORKING, events=[{"n": 1}])
    same = scoped(cf.SCOPE_WORKING, events=[{"n": 1}])
    drift = scoped(cf.SCOPE_WORKING, events=[{"n": 999}])
    assert cf.compare(a, same).equal
    diff = cf.compare(a, drift)
    assert not diff.equal and "local_only.events.digest" in diff.differing
    assert a.tables == ("events", "labels")  # a re-verify keeps checking the local table
    assert cf.Fingerprint.from_dict(a.to_dict()) == a


def test_committed_native_block_carries_root_not_the_working_set_hash():
    native = {"db": "work", "root": "r1", "tables": {"labels": "l", "events": "e", "issues": "i"}}
    c = scoped(cf.SCOPE_COMMITTED, events=None, native=native)
    assert c.native == {"root": "r1", "tables": {"issues": "i", "labels": "l"}}
    w = scoped(cf.SCOPE_WORKING, events=[], native=native)
    assert w.native["db"] == "work" and w.native["tables"]["events"] == "e"
    # a dirty committed table (no hash on one side) is not a false mismatch
    other = scoped(cf.SCOPE_COMMITTED, events=None, native={**native, "tables": {"issues": "i"}})
    assert cf.compare(c, other).equal


def test_scopes_are_not_comparable():
    with pytest.raises(cf.FingerprintScopeMismatch):
        cf.compare(scoped(cf.SCOPE_COMMITTED, events=None), scoped(cf.SCOPE_WORKING, events=[]))


def test_v2_documents_are_still_read_and_recomputed_for_comparison():
    f = facts(tables={"labels": [{"i": "x"}], "events": [{"n": 1}]}, ignored=("events",))
    old = cf.compute(f, cf.TIER_STRONG, tables=EV_TABLES, schema=cf.LEGACY_SCHEMA)
    assert old.schema == "bh.corpus-fingerprint/2" and "scope" not in old.to_dict()
    assert set(old.components["tables"]) == {"labels", "events"}  # /2 digested every table
    loaded = cf.Fingerprint.from_dict(old.to_dict())
    assert loaded == old and loaded.scope == cf.SCOPE_WORKING
    again = cf.compute_like(f, loaded)
    assert cf.compare(loaded, again).equal and again.schema == loaded.schema
    with pytest.raises(cf.FingerprintVersionMismatch):
        cf.compare(loaded, scoped(cf.SCOPE_WORKING, events=[{"n": 1}]))


def test_ignored_tables_come_from_dolt_ignore_patterns():
    pats = [
        {"pattern": "events", "ignored": 1},
        {"pattern": "wisp_%", "ignored": 1},
        {"pattern": "bh_local_*", "ignored": True},
        {"pattern": "wisp_keep", "ignored": 0},
    ]
    names = ["events", "wisps", "wisp_a", "wisp_keep", "bh_local_x", "labels", "myevents"]
    assert cf.ignored_among(pats, names) == ["events", "wisp_a", "bh_local_x"]


# ---- real Dolt: a clone of a pushed store (file:// remote, no server) ----------------------


def _dolt(cwd, *args, env=None):
    import subprocess

    done = subprocess.run(
        ["dolt", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr or done.stdout
    return done.stdout


def _store_facts(path, env, scope, tables=EV_TABLES):
    import json

    from beadhive.backup_scratch import read_corpus_facts

    def query(q):
        out = _dolt(path, "sql", "-r", "json", "-q", q, env=env).strip()
        return json.loads(out).get("rows", []) if out else []

    return read_corpus_facts(
        query,
        lambda t: f"`{t}`",
        database=None,
        tables=tables,
        schema_version=59,
        native=True,
        scope=scope,
    )


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("dolt") is None, reason="dolt is not installed")
def test_clone_of_a_pushed_store_matches_its_source_in_the_committed_scope(tmp_path):
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "DOLT_ROOT_PATH": str(tmp_path / "home"),
    }
    (tmp_path / "home").mkdir()
    _dolt(tmp_path, "config", "--global", "--add", "user.name", "t", env=env)
    _dolt(tmp_path, "config", "--global", "--add", "user.email", "t@example.com", env=env)
    src = tmp_path / "src"
    src.mkdir()
    _dolt(src, "init", env=env)
    _dolt(
        src,
        "sql",
        "-q",
        "INSERT INTO dolt_ignore VALUES ('events', true), ('wisp_%', true);"
        "CREATE TABLE issues (id VARCHAR(20) PRIMARY KEY, title TEXT);"
        "CREATE TABLE config (`key` VARCHAR(40) PRIMARY KEY, value TEXT);"
        "CREATE TABLE metadata (`key` VARCHAR(40) PRIMARY KEY, value TEXT);"
        "CREATE TABLE labels (issue_id VARCHAR(20), label VARCHAR(40));"
        "INSERT INTO issues VALUES ('bh-1', 'one'), ('bh-2', 'two');"
        "INSERT INTO config VALUES ('issue_prefix', 'bh');"
        "INSERT INTO metadata VALUES ('_project_id', 'p1');"
        "INSERT INTO labels VALUES ('bh-1', 'x');",
        env=env,
    )
    _dolt(src, "add", ".", env=env)
    _dolt(src, "commit", "-m", "committed state", env=env)
    # ignored tables are created AFTER the commit: they are working-set only
    _dolt(
        src,
        "sql",
        "-q",
        "CREATE TABLE events (id INT PRIMARY KEY, body TEXT);"
        "INSERT INTO events VALUES (1, 'a'), (2, 'b'), (3, 'c');"
        "CREATE TABLE wisp_x (id INT PRIMARY KEY); INSERT INTO wisp_x VALUES (7);",
        env=env,
    )
    remote = f"file://{tmp_path / 'remote'}"
    _dolt(src, "remote", "add", "origin", remote, env=env)
    _dolt(src, "push", "origin", "main", env=env)
    clone = tmp_path / "clone"
    _dolt(tmp_path, "clone", remote, str(clone), env=env)

    def fps(path, scope):
        return cf.compute(
            _store_facts(path, env, scope), cf.TIER_STRONG, tables=EV_TABLES, scope=scope
        )

    s_c, c_c = fps(src, cf.SCOPE_COMMITTED), fps(clone, cf.SCOPE_COMMITTED)
    diff = cf.compare(s_c, c_c)
    assert diff.equal, diff.detail
    assert set(s_c.components["tables"]) == {"labels"}
    assert set(s_c.native) == {"root", "tables"} and s_c.native == c_c.native
    # the local-only tables are reported, not compared: the source has them, the clone does not
    assert s_c.local_only["events"]["rows"] == 3 and s_c.local_only["wisp_x"]["rows"] == 1
    assert "events" not in (c_c.local_only or {})
    assert cf.local_only_differences(s_c, c_c)

    # a native backup carries the working set, so its (working) scope still sees local-only drift
    s_w, c_w = fps(src, cf.SCOPE_WORKING), fps(clone, cf.SCOPE_WORKING)
    drift = cf.compare(s_w, c_w)
    assert not drift.equal and "local_only.events.rows" in drift.differing
    assert "dolt_native.db" in drift.differing  # the old failure, now confined to this scope

    # and a committed change DOES break the committed comparison
    _dolt(clone, "sql", "-q", "INSERT INTO labels VALUES ('bh-2', 'y');", env=env)
    _dolt(clone, "add", ".", env=env)
    _dolt(clone, "commit", "-m", "diverge", env=env)
    moved = cf.compare(s_c, fps(clone, cf.SCOPE_COMMITTED))
    assert (
        not moved.equal
        and "main_head" in moved.differing
        and "tables.labels.rows" in moved.differing
    )
