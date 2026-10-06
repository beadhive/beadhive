"""The product fence + guard DDL (bh-uz46l): shape, order, idempotence, guarded table set.

Pure: no engine runs here. ``tests/test_fence_data_int.py`` runs the same statements on the pinned
Dolt/bd; the F3 canary (``tests/test_fence_trigger_canary_int.py``) pins the engine behaviour the
shape depends on, and its static checker is reused here against the PRODUCT script.
"""

from __future__ import annotations

import re

import pytest

from beadhive import fence_schema as fs
from beadhive import writer_adopt
from harness import composed_fence as cf
from harness import trigger_canary as tc
from harness import write_guard as wg

SEED = ("a", 7, "rev-7")


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().lower())


def _created(statements):
    out = []
    for s in statements:
        m = re.match(r"create (trigger|procedure) (\w+)", s.strip(), re.I)
        if m:
            out.append((m.group(1).lower(), m.group(2)))
    return out


def test_exactly_44_triggers_and_every_count_agrees():
    assert fs.TRIGGER_COUNT == 44
    assert len(set(fs.trigger_names())) == 44
    assert writer_adopt.FENCE_TRIGGER_COUNT == fs.TRIGGER_COUNT
    assert tc.COMPOSED_TRIGGER_COUNT == fs.TRIGGER_COUNT
    script = fs.render_script(fs.install_statements(SEED))
    assert len(re.findall(r"^create trigger ", script, re.M | re.I)) == 44


def test_guarded_table_set_is_pinned_to_the_bh_sieai_set():
    assert fs.GUARDED_TABLES == wg.GUARDED_TABLES
    assert len(fs.GUARDED_TABLES) == 14
    for unguarded in (*fs.UNGUARDED_BD_TABLES, "child_counters", "metadata"):
        assert unguarded not in fs.GUARDED_TABLES
    ignored = ("events", "leases", "local_metadata", "repo_mtimes", "wisps", "schema_migrations")
    for table in ignored:
        assert table not in fs.GUARDED_TABLES
    assert not any(t.startswith(("bd_events", "wisp_", "bh_")) for t in fs.GUARDED_TABLES)
    script = fs.render_script(fs.install_statements(SEED)).lower()
    for unguarded in fs.UNGUARDED_BD_TABLES:
        assert f"on `{unguarded}`" not in script


def test_install_runs_in_the_composed_order():
    """bh-jbb6r: fence tables (+ seed), then the guard, then the monotonic triggers LAST."""
    statements = fs.install_statements(SEED)
    lowered = [s.lower() for s in statements]

    def first(prefix: str) -> int:
        return next(i for i, s in enumerate(lowered) if s.startswith(prefix))

    def last(prefix: str) -> int:
        return max(i for i, s in enumerate(lowered) if s.startswith(prefix))

    tables = last("create table if not exists")
    seed = first("insert into bh_writer")
    procedure = first("create procedure bh_guard_check")
    guard_last = max(i for i, s in enumerate(lowered) if s.startswith("create trigger bh_guard_"))
    monotonic = first("drop trigger if exists bh_writer_monotonic")
    assert tables < seed < procedure < guard_last < monotonic
    assert [name for kind, name in _created(statements)][-2:] == list(fs.MONOTONIC_TRIGGERS)
    assert "insert into bh_writer" not in " ".join(lowered[: first("create table")])


def test_install_is_idempotent_by_construction():
    """Tables only IF NOT EXISTS and never dropped; procedure + every trigger drop-then-create;
    a re-install (no seed) writes no fence row."""
    statements = fs.install_statements(SEED)
    lowered = [_norm(s) for s in statements]
    assert not any(s.startswith("drop table") for s in lowered)
    assert all("if not exists" in s for s in lowered if s.startswith("create table"))
    for i, (kind, name) in enumerate(_created(statements)):
        index = next(j for j, s in enumerate(lowered) if s.startswith(f"create {kind} {name}"))
        assert lowered[index - 1] == f"drop {kind} if exists {name}", (i, name)
    again = [_norm(s) for s in fs.install_statements(None)]
    assert not any(s.startswith(("insert into bh_", "update bh_", "delete")) for s in again)
    assert any("dolt_ignore" in s and "'bh_local_%'" in s for s in again)


def test_product_guard_matches_the_composed_prototype_statement_for_statement():
    """The product module owns the DDL now; it is the bh-jbb6r composed fence, unchanged."""
    proto = cf.fence_ddl("a", 7)
    blocks = re.findall(r"create (?:trigger|procedure) .*?\nend//", proto, re.S | re.I)
    proto_objects = {re.match(r"create \w+ (\w+)", b, re.I).group(1): _norm(b[:-2]) for b in blocks}
    product = {name: s for s in fs.install_statements(SEED) for _, name in _created([s])}
    assert set(product) == set(proto_objects)
    for name, body in product.items():
        assert _norm(body) == proto_objects[name], name


def test_product_guard_has_the_canary_shape():
    """F3 canary's static checker on the product script: inline mark BEFORE the CALL, the CALL
    last, no @user variable in any trigger body."""
    assert tc.guard_shape_violations(fs.render_script(fs.guard_statements())) == []


def test_fence_reads_use_locals_never_subqueries_or_user_variables():
    """bh-vje85 E3 / bh-sieai R5: a subquery or an @variable reading the fence misbehaves under
    the fence triggers on Dolt 2.3.5."""
    for statement in fs.install_statements(SEED) + fs.ident_statements("a"):
        lowered = statement.lower()
        assert "(select" not in lowered, statement
        assert "@" not in lowered, statement


def test_seed_names_the_writer_and_inserts_the_adopt_sentinel():
    seed = [_norm(s) for s in fs.seed_statements("a", 7, "rev")]
    assert seed == [
        "insert into bh_writer values (1, 'a', 7, 'rev')",
        "insert into bh_epoch_live values (1, 7)",
        "insert into bh_write_mark (id, epoch, tbl) values ('adopt-7', 7, 'bh_writer')",
    ]
    with pytest.raises(ValueError):
        fs.seed_statements("a", 0, "rev")


def test_ident_statements_quote_and_validate():
    stmts = fs.ident_statements("o'brien", "branch")
    assert "'o''brien', 'branch'" in stmts[-1]
    with pytest.raises(ValueError):
        fs.ident_statements("a", "writer")
    with pytest.raises(ValueError):
        fs.ident_statements("", "replica")
    assert fs.quote("a\\b'c") == "'a\\\\b''c'"


def test_render_script_terminates_every_statement_for_the_cli():
    script = fs.render_script(["select 1", "create trigger x before insert on t for each row\nend"])
    assert script.startswith("delimiter //\n") and script.endswith("delimiter ;\n")
    assert "select 1//\n" in script and "\nend//\n" in script
