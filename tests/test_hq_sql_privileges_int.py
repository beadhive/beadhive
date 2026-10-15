"""Documented HQ runtime grants verify restore floors on real Dolt 2.3.5 (bh-aprgo, bh-t759y).

Accounts are provisioned from the grants documented in docs/HQ.md (#runtime-sql-privileges) on
a runtime database that also holds unrelated tables, then a next-epoch session provisioning
commit lands. The documented reader verifies the floor across it; a legacy per-table reader
is refused with the named capability instead of an opaque outage. Neither account can write.
"""

from __future__ import annotations

import pymysql
import pytest

import test_frame_release_upgrade as upgrade_tests
from beadhive import hq_sql_capabilities as capabilities
from beadhive.hq_sql_runtime import SqlRuntimeCapabilityMissing
from beadhive.hq_sql_session_provision import provision_incarnation
from beadhive.hq_storage_composition import runtime_statements
from test_hq_sql_provision_history_int import _initial, _next, _reader
from test_hq_sql_session_int import DB, PASSWORD, _root, _server

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]
fixture = upgrade_tests.fixture

#: The legacy shape: SELECT on each protected table the reader names, nothing database-wide.
LEGACY_TABLES = ("hq_authority", "hq_principal_registry", "dolt_schemas", "dolt_commit_ancestors")


def _account(port, user):
    return pymysql.connect(
        host="127.0.0.1", port=port, user=user, password=PASSWORD, database=DB,
        autocommit=True, connect_timeout=5, read_timeout=15, write_timeout=15,
    )  # fmt: skip


def _create(cursor, user, grants):
    cursor.execute(f"CREATE USER '{user}'@'localhost' IDENTIFIED BY %s", (PASSWORD,))
    for grant in grants:
        cursor.execute(grant.format(user=f"'{user}'@'localhost'"))


def test_documented_grants_verify_floor_across_provisioning_with_unrelated_tables(
    tmp_path, fixture, monkeypatch
):
    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "signed")
    with _server(tmp_path) as port:
        root = _root(port)
        with root.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {DB}")
            cursor.execute(f"USE {DB}")
            # Unrelated committed tables no reader was ever granted individually.
            cursor.execute("CREATE TABLE zz_operator_notes (id INT PRIMARY KEY, note TEXT)")
            cursor.execute("INSERT INTO zz_operator_notes VALUES (1, 'unrelated')")
            initial = _initial(cursor, fixture)
            _create(cursor, "documented", ["GRANT SELECT ON `" + DB + "`.* TO {user}"])
            _create(
                cursor,
                "legacy",
                [f"GRANT SELECT ON `{DB}`.`{table}` TO {{user}}" for table in LEGACY_TABLES],
            )
            _create(
                cursor,
                "hq_admin",
                ["GRANT ALL PRIVILEGES ON *.* TO {user} WITH GRANT OPTION"],
            )
        documented, legacy, admin = (
            _account(port, user) for user in ("documented", "legacy", "hq_admin")
        )
        readers = {
            "documented": _reader(tmp_path, fixture, initial, "documented"),
            "legacy": _reader(tmp_path, fixture, initial, "legacy"),
        }
        for name, connection in (("documented", documented), ("legacy", legacy)):
            with connection.cursor() as cursor:
                readers[name].verified_state_at(cursor, initial)
        # Preflight names the gap before any provisioning commit exists.
        with legacy.cursor() as cursor:
            missing = [str(item) for item in capabilities.runtime_read(cursor, DB, initial)]
        assert any(
            "DOLT_DIFF_SUMMARY" in item and f"SELECT ON `{DB}`.*" in item for item in missing
        )
        assert any("dolt_ignore" in item for item in missing)
        assert not any("legacy" in item or "localhost" in item for item in missing)
        with documented.cursor() as cursor:
            assert capabilities.runtime_read(cursor, DB, initial) == []
            # Read-only: the documented reader still cannot write anything.
            with pytest.raises(pymysql.MySQLError):
                cursor.execute("INSERT INTO zz_operator_notes VALUES (2, 'write')")
            inspect = [str(item) for item in capabilities.session_inspection(cursor, DB)]
            assert any("TRIGGER" in item for item in inspect)
            assert any("`mysql`" in item for item in inspect)
            assert capabilities.provisioning(cursor, DB, None)
        with admin.cursor() as cursor:
            assert capabilities.session_inspection(cursor, DB) == []
            assert capabilities.provisioning(cursor, DB, DB) == []

        # The server-local operator provisions the next epoch's session/evidence pair.
        principal, epoch = _next(fixture)
        with root.cursor() as cursor:
            cursor.execute(f"USE {DB}")
            provision_incarnation(
                cursor,
                principal,
                epoch,
                frame_address="127.0.0.1",
                password=PASSWORD,
                database=DB,
            )
            cursor.execute("SELECT DOLT_HASHOF('HEAD')")
            provisioned = cursor.fetchone()[0]
            # The frame account a session provisioning creates gets database-wide read too.
            runtime_statements(cursor).grant_select_database(DB, f"'{principal}'@'127.0.0.1'")
        assert provisioned != initial
        with documented.cursor() as cursor:
            state, _crossref, _policies = readers["documented"].verified_state_at(
                cursor, provisioned
            )
        assert state["revision"] == fixture.state["revision"]
        with legacy.cursor() as cursor, pytest.raises(SqlRuntimeCapabilityMissing) as refused:
            readers["legacy"].verified_state_at(cursor, provisioned)
        assert f"SELECT ON `{DB}`.*" in str(refused.value)
        assert "not evidence of rollback" in str(refused.value)
        assert "legacy" not in str(refused.value)
        # The documented reader's floor advanced; rollback protection is intact for it.
        with documented.cursor() as cursor, pytest.raises(Exception, match="rollback|fork"):
            readers["documented"].verified_state_at(cursor, initial)
        for connection in (documented, legacy, admin, root):
            connection.close()
