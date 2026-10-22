"""The release catalog on a real, private Dolt server (bh-vfrem.4).

One owned ``dolt sql-server`` under ``tmp_path`` (never a live HQ). Proves, with locally built
entries only (no Sigstore, PyPI or GitHub), that:

* the INSERT-only catalog binding appends and revokes through the real store, and can do nothing
  else (no UPDATE, DELETE, DDL or ``mysql`` read);
* a same-sequence commit that only appends catalog rows advances every restore floor, while an
  edit, a delete, a mixed change and a pre-filled creation still refuse;
* frames read the catalog ``AS OF`` the verified runtime head with no new authority publication
  (the resolution of the ADR's open question 1).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import test_frame_release_upgrade as upgrade_tests
from beadhive.hq_release_catalog import (
    ALGORITHM,
    CatalogConflict,
    CatalogEntry,
    ReleaseCatalogStore,
)
from beadhive.hq_sql_placement import check_grants
from beadhive.hq_sql_runtime import SqlRuntimeError
from beadhive.hq_sql_runtime_schema import RELEASE_CATALOG_SCHEMA
from beadhive.hq_sql_session_provision import (
    check_provisioning,
    provision_incarnation,
    provision_release_catalog,
)
from test_hq_sql_provision_history_int import _commit, _head, _initial, _reader
from test_hq_sql_session_int import DB, PASSWORD, _frame, _refused, _root, _server, _sql

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]
fixture = upgrade_tests.fixture

WRITER = "catalog_writer"
NOW = 1_800_000_000.0


def _entry(version="0.28.5", digest="1", **extra):
    return CatalogEntry(
        version,
        "sha256:" + digest * 64,
        ALGORITHM,
        "a" * 64,
        "c" * 40,
        b'{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json"}',
        **extra,
    )


def _store(port, tmp_path):
    def connect(_binding, _broker, *, deadline):
        return _frame(port, tmp_path, WRITER)

    return ReleaseCatalogStore(
        {"user": WRITER, "database": DB, "operation_timeout": 15},
        clock=lambda: NOW,  # mocked clock
        connect=connect,
    )


def _world(tmp_path, fixture, port, monkeypatch):
    """Signed authority committed, the catalog provisioned, one reader floor taken at each step."""
    from beadhive.hq_sql_signatures import canonical  # noqa: F401  (fixture import side effect)

    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "signed")
    root = _root(port)
    cursor = root.cursor()
    cursor.execute(f"CREATE DATABASE {DB}")
    cursor.execute(f"USE {DB}")
    initial = _initial(cursor, fixture)
    reader = _reader(tmp_path, fixture, initial, "operator")
    reader.verified_state_at(cursor, initial)
    assert provision_release_catalog(cursor) is True
    assert provision_release_catalog(cursor) is False  # idempotent
    created = _head(cursor)
    reader.verified_state_at(cursor, created)  # the one-time creation is a reviewed commit
    from beadhive_hq_storage_dolt import catalog_account_statements

    for statement in catalog_account_statements(f"'{WRITER}'@'127.0.0.1'", DB):
        cursor.execute(statement, (PASSWORD,) if "%s" in statement else None)
    return root, cursor, reader, created


def _floor(reader):
    return json.loads(Path(reader.settings["runtime_floor_path"]).read_text())["head"]


def test_catalog_appends_through_the_insert_only_binding_and_every_floor_follows(
    tmp_path, fixture, monkeypatch
):
    with _server(tmp_path) as port:
        root, cursor, reader, created = _world(tmp_path, fixture, port, monkeypatch)
        store = _store(port, tmp_path)
        assert store.add(_entry()) == "inserted"
        head = _head(cursor)
        assert head != created
        # Same authority sequence, new head: the floor accepts a commit that only adds rows.
        reader.verified_state_at(cursor, head)
        assert _floor(reader) == head
        # Open question 1: a frame-style reader sees the row AS OF the verified runtime head,
        # with no authority publication in between.
        seen = _sql(root, "SELECT version, status FROM hq_release_catalog AS OF %s", (head,))
        assert [tuple(row) for row in seen] == [("0.28.5", "active")]
        assert store.add(_entry()) == "unchanged"  # idempotent: no commit
        assert _head(cursor) == head
        with pytest.raises(CatalogConflict, match="different active digest"):
            store.add(_entry(digest="2"))
        assert _head(cursor) == head
        # Revocation is a tombstone row; the floor follows that too.
        assert store.revoke("0.28.5", "compromised build host") == ["sha256:" + "1" * 64]
        revoked = _head(cursor)
        reader.verified_state_at(cursor, revoked)
        statuses = [(r.status, r.reason, r.recorded_at) for r in store.rows("0.28.5")]
        assert sorted(statuses) == [
            ("active", "", NOW),
            ("revoked", "compromised build host", NOW),
        ]
        with pytest.raises(CatalogConflict, match="revoked"):
            store.add(_entry())
        assert store.add(_entry(digest="2")) == "inserted"  # a new digest after the revoke
        reader.verified_state_at(cursor, _head(cursor))
        root.close()


def test_catalog_binding_can_insert_and_nothing_else(tmp_path, fixture, monkeypatch):
    with _server(tmp_path) as port:
        root, cursor, _reader_, _created = _world(tmp_path, fixture, port, monkeypatch)
        writer = _frame(port, tmp_path, WRITER)
        row = (
            "beadhive", "0.1.0", "sha256:" + "5" * 64, "active", ALGORITHM, "a" * 64,
            "c" * 40, b"{}", "", NOW,
        )  # fmt: skip
        _sql(writer, "INSERT INTO hq_release_catalog VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", row)
        writer.rollback()
        for statement in (
            "UPDATE hq_release_catalog SET reason='x'",
            "DELETE FROM hq_release_catalog",
            "CREATE TABLE sneaky (id INT)",
            "DROP TABLE hq_release_catalog",
            "UPDATE hq_authority SET revision = revision",
            "INSERT INTO hq_live_hive_leases VALUES ('x','x','{}','x','x')",
            "SELECT * FROM mysql.user",
        ):
            _refused(writer, statement)
        lines = [r[0] for r in _sql(root, f"SHOW GRANTS FOR '{WRITER}'@'127.0.0.1'")]
        assert check_grants(lines, role="catalog", database=DB) == []
        # The shape check rejects a binding that gained any other write right.
        widened = [*lines, f"GRANT UPDATE ON `{DB}`.`hq_release_catalog` TO `{WRITER}`@`127.0.0.1`"]
        assert any("only INSERT" in p for p in check_grants(widened, role="catalog", database=DB))
        no_insert = [line for line in lines if "INSERT" not in line]
        assert any(
            "lacks INSERT" in p for p in check_grants(no_insert, role="catalog", database=DB)
        )
        trigger = [*lines, f"GRANT TRIGGER ON `{DB}`.* TO `{WRITER}`@`127.0.0.1`"]
        assert any("TRIGGER" in p for p in check_grants(trigger, role="catalog", database=DB))
        writer.close()
        root.close()


@pytest.mark.parametrize("change", ["edit", "delete", "mixed", "primary_key"])
def test_the_floor_still_refuses_edits_and_deletes_of_the_catalog(
    tmp_path, fixture, monkeypatch, change
):
    with _server(tmp_path) as port:
        root, cursor, reader, _created = _world(tmp_path, fixture, port, monkeypatch)
        store = _store(port, tmp_path)
        store.add(_entry())
        safe = _head(cursor)
        reader.verified_state_at(cursor, safe)
        if change == "edit":
            cursor.execute("UPDATE hq_release_catalog SET reason='tampered'")
        elif change == "delete":
            cursor.execute("DELETE FROM hq_release_catalog")
        elif change == "primary_key":
            cursor.execute("UPDATE hq_release_catalog SET version='0.28.6'")
        else:
            cursor.execute(
                "INSERT INTO hq_release_catalog VALUES "
                "('beadhive','0.28.6',%s,'active',%s,%s,%s,'{}','',1)",
                ("sha256:" + "3" * 64, ALGORITHM, "b" * 64, "d" * 40),
            )
            cursor.execute("CREATE TABLE unreviewed (id INT PRIMARY KEY)")
        head = _commit(cursor)
        with pytest.raises(SqlRuntimeError, match="amended authority"):
            reader.verified_state_at(cursor, head)
        assert _floor(reader) == safe  # the floor did not move
        root.close()


def test_a_prefilled_catalog_creation_is_refused(tmp_path, fixture, monkeypatch):
    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "signed")
    with _server(tmp_path) as port:
        root = _root(port)
        cursor = root.cursor()
        cursor.execute(f"CREATE DATABASE {DB}")
        cursor.execute(f"USE {DB}")
        initial = _initial(cursor, fixture)
        reader = _reader(tmp_path, fixture, initial, "operator")
        reader.verified_state_at(cursor, initial)
        for statement in RELEASE_CATALOG_SCHEMA:
            cursor.execute(statement)
        cursor.execute(
            "INSERT INTO hq_release_catalog VALUES "
            "('beadhive','0.28.6',%s,'active',%s,%s,%s,'{}','',1)",
            ("sha256:" + "3" * 64, ALGORITHM, "b" * 64, "d" * 40),
        )
        head = _commit(cursor)
        with pytest.raises(SqlRuntimeError, match="amended authority"):
            reader.verified_state_at(cursor, head)
        assert _floor(reader) == initial
        root.close()


def test_store_refuses_a_dirty_working_tree_and_a_wrong_principal(tmp_path, fixture, monkeypatch):
    with _server(tmp_path) as port:
        root, cursor, _reader_, _created = _world(tmp_path, fixture, port, monkeypatch)
        cursor.execute("CREATE TABLE stray (id INT PRIMARY KEY)")  # uncommitted residue
        store = _store(port, tmp_path)
        with pytest.raises(Exception, match="working tree is not clean"):
            store.add(_entry())
        assert _sql(root, "SELECT COUNT(*) FROM hq_release_catalog")[0][0] == 0
        cursor.execute("DROP TABLE stray")
        wrong = ReleaseCatalogStore(
            {"user": "someone_else", "database": DB, "operation_timeout": 15},
            connect=lambda *_a, **_k: _frame(port, tmp_path, WRITER),
        )
        with pytest.raises(Exception, match="principal or endpoint mismatch"):
            wrong.rows()
        root.close()


def test_frames_get_select_on_the_catalog_and_the_provisioning_check_covers_it(
    tmp_path, fixture, monkeypatch
):
    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "signed")
    with _server(tmp_path) as port:
        root = _root(port)
        cursor = root.cursor()
        cursor.execute(f"CREATE DATABASE {DB}")
        cursor.execute(f"USE {DB}")
        _initial(cursor, fixture)
        # Frame A is provisioned BEFORE the catalog exists: it holds table-level grants only.
        provision_incarnation(
            cursor, "frame_a", 1, frame_address="127.0.0.1", password=PASSWORD, database=DB
        )
        assert provision_release_catalog(cursor) is True
        assert any(
            "lacks SELECT on hq_release_catalog" in p
            for p in check_provisioning(cursor, "frame_a", 1, database=DB)
        )
        # The reviewed remedy is the database-wide read the restore floor needs anyway.
        cursor.execute(f"GRANT SELECT ON `{DB}`.* TO 'frame_a'@'127.0.0.1'")
        assert check_provisioning(cursor, "frame_a", 1, database=DB) == []
        # Frame B is provisioned AFTER the catalog exists: the provisioning plan grants it SELECT.
        provision_incarnation(
            cursor, "frame_b", 1, frame_address="127.0.0.1", password=PASSWORD, database=DB
        )
        lines = [r[0] for r in _sql(root, "SHOW GRANTS FOR 'frame_b'@'127.0.0.1'")]
        assert any("hq_release_catalog" in line and "SELECT" in line for line in lines)
        assert check_provisioning(cursor, "frame_b", 1, database=DB) == []
        # A frame reads the catalog and can never write it.
        frame = _frame(port, tmp_path, "frame_b")
        assert _sql(frame, "SELECT COUNT(*) FROM hq_release_catalog")[0][0] == 0
        _refused(
            frame,
            "INSERT INTO hq_release_catalog VALUES "
            "('beadhive','9','sha256:x','active','a','b','c','{}','',1)",
        )
        # And a catalog write right on a frame is a conformance violation.
        bad = [*lines, f"GRANT INSERT ON `{DB}`.`hq_release_catalog` TO `frame_b`@`127.0.0.1`"]
        assert any("frame holds" in p for p in check_grants(bad, role="frame", database=DB))
        frame.close()
        root.close()


def test_the_catalog_diff_probe_is_row_level_on_this_dolt(tmp_path, fixture, monkeypatch):
    """The restore floor depends on ``DOLT_DIFF``: added rows are ``added``, an edit is
    ``modified`` and a delete is ``removed`` (not distinguishable by ``DOLT_DIFF_SUMMARY``)."""
    from beadhive_hq_storage_dolt import HqRuntimeStatements

    with _server(tmp_path) as port:
        root, cursor, _reader_, created = _world(tmp_path, fixture, port, monkeypatch)
        store = _store(port, tmp_path)
        store.add(_entry())
        added = _head(cursor)
        cursor.execute("UPDATE hq_release_catalog SET reason='x'")
        edited = _commit(cursor)
        cursor.execute("DELETE FROM hq_release_catalog")
        deleted = _commit(cursor)
        sql = HqRuntimeStatements.of(cursor)
        assert {r[0] for r in sql.catalog_diff_types(created, added)} == {"added"}
        assert {r[0] for r in sql.catalog_diff_types(added, edited)} == {"modified"}
        assert {r[0] for r in sql.catalog_diff_types(edited, deleted)} == {"removed"}
        root.close()
