"""Restore floors stay connected across the one-time liveness schema on real Dolt (bh-lso8w).

An HQ whose signed authority predates session rows gets the ``frame_*`` ignore rule and
``hq_liveness_policy`` committed at the same authority sequence, then incarnation stamp
triggers. Readers whose floors were taken before either commit must accept that descendant,
while any other same-sequence change, and every rollback, still refuses with diagnostics
naming the offending commit and the changed tables.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import test_frame_release_upgrade as upgrade_tests
import test_hq_sql_provision_history_int as history
from beadhive.hq_sql_runtime import FLOOR_RECOVERY_DOC, SqlRuntimeError
from beadhive.hq_sql_session import liveness_schema_statements
from beadhive.hq_sql_session_provision import provision_incarnation, provision_liveness_schema
from test_hq_sql_provision_history_int import _commit, _head, _next, _reader
from test_hq_sql_session_int import DB, PASSWORD, _root, _server

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]
fixture = upgrade_tests.fixture


def _initial(cursor, fixture, *, liveness=True):
    """The shared fixture HQ; ``liveness=False`` is one whose authority predates session rows."""
    if liveness:
        return history._initial(cursor, fixture)
    original = history.provision_liveness_schema
    history.provision_liveness_schema = lambda cursor: False
    try:
        return history._initial(cursor, fixture)
    finally:
        history.provision_liveness_schema = original


def _floor(reader):
    return json.loads(Path(reader.settings["runtime_floor_path"]).read_text())


def _liveness_uncommitted(cursor, *, session_ttl_s=300):
    """The liveness schema statements without their own DOLT_ADD/DOLT_COMMIT."""
    for statement in liveness_schema_statements()[:-2]:
        cursor.execute(statement.replace("(1, 300,", f"(1, {session_ttl_s},"))


@pytest.mark.parametrize("shape", ["separate", "combined"])
def test_floors_before_the_liveness_schema_stay_connected_across_provisioning(
    tmp_path, fixture, monkeypatch, shape
):
    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "signed")
    with _server(tmp_path) as port:
        connection = _root(port)
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {DB}")
            cursor.execute(f"USE {DB}")
            initial = _initial(cursor, fixture, liveness=False)
            operator, frame, observer = (
                _reader(tmp_path, fixture, initial, name)
                for name in ("operator", "frame", "observer")
            )
            for reader in (operator, frame, observer):
                reader.verified_state_at(cursor, initial)
            principal, epoch = _next(fixture)
            if shape == "separate":
                assert provision_liveness_schema(cursor) is True
                schema_head = _head(cursor)
                # One reader connects at the intermediate head, the others skip it.
                operator.verified_state_at(cursor, schema_head)
                provision_incarnation(
                    cursor,
                    principal,
                    epoch,
                    frame_address="127.0.0.1",
                    password=PASSWORD,
                    database=DB,
                )
                provisioned = _head(cursor)
            else:
                # A native operator run: ignore rule, policy and stamps in one commit.
                from beadhive.hq_sql_session import evidence_ddl, session_ddl

                _liveness_uncommitted(cursor)
                for ddl in (session_ddl(principal, epoch), evidence_ddl(principal, epoch)):
                    for statement in ddl:
                        cursor.execute(statement)
                provisioned = _commit(cursor)
            assert provisioned != initial
            for reader in (operator, frame, observer):
                state, _crossref, _policies = reader.verified_state_at(cursor, provisioned)
                assert state["revision"] == fixture.state["revision"]
                assert _floor(reader) == {
                    "backend": "runtime-backend",
                    "generation": "runtime-generation",
                    "sequence": fixture.state["revision"],
                    "head": provisioned,
                }
                # Rollback detection is intact: the pre-provisioning head is now refused.
                with pytest.raises(SqlRuntimeError, match="rollback|fork") as refused:
                    reader.verified_state_at(cursor, initial)
                assert FLOOR_RECOVERY_DOC in str(refused.value)
        connection.close()


@pytest.mark.parametrize(
    ("unsafe", "reason"),
    [
        ("extra_ignore", "changes dolt_ignore beyond adding the frame_* rule"),
        ("policy_bounds", "liveness policy outside its TTL bounds"),
        ("policy_only", "outside the one-time liveness schema provisioning"),
        ("mixed", "changes tables outside reviewed session provisioning (unsafe_change"),
        ("registry", "outside reviewed session provisioning (hq_principal_registry)"),
        ("policy_edit", "outside reviewed session provisioning (hq_liveness_policy)"),
        ("reverted_policy", "outside reviewed session provisioning (hq_liveness_policy)"),
    ],
)
def test_unreviewed_liveness_schema_changes_still_refuse_with_diagnostics(
    tmp_path, fixture, monkeypatch, unsafe, reason
):
    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "signed")
    with _server(tmp_path) as port:
        connection = _root(port)
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {DB}")
            cursor.execute(f"USE {DB}")
            initial = _initial(cursor, fixture, liveness=unsafe == "policy_edit")
            reader = _reader(tmp_path, fixture, initial, "operator")
            reader.verified_state_at(cursor, initial)
            if unsafe == "extra_ignore":
                _liveness_uncommitted(cursor)
                # Ignoring the authority table would let it change without a commit.
                cursor.execute("INSERT INTO dolt_ignore VALUES ('hq_authority', TRUE)")
            elif unsafe == "policy_bounds":
                _liveness_uncommitted(cursor, session_ttl_s=0)
            elif unsafe == "policy_only":
                for statement in liveness_schema_statements()[1:-2]:
                    cursor.execute(statement)
            elif unsafe == "mixed":
                _liveness_uncommitted(cursor)
                cursor.execute("CREATE TABLE unsafe_change (id INT PRIMARY KEY)")
            elif unsafe == "registry":
                _liveness_uncommitted(cursor)
                cursor.execute(
                    "INSERT INTO hq_principal_registry VALUES "
                    "('unsafe','frame','host','vm',4,'unsafe','key')"
                )
            elif unsafe == "policy_edit":
                cursor.execute("UPDATE hq_liveness_policy SET session_ttl_s=60")
            elif unsafe == "reverted_policy":
                provision_liveness_schema(cursor)
                cursor.execute("UPDATE hq_liveness_policy SET session_ttl_s=60")
                _commit(cursor)
                cursor.execute("UPDATE hq_liveness_policy SET session_ttl_s=300")
            head = _commit(cursor)
            with pytest.raises(SqlRuntimeError) as refused:
                reader.verified_state_at(cursor, head)
            message = str(refused.value)
            assert message.startswith("HQ runtime rollback or amended authority detected")
            assert reason in message
            assert f"head {head[:12]}" in message and f"floor {initial[:12]}" in message
            assert "tables changed since the floor:" in message
            assert FLOOR_RECOVERY_DOC in message
            assert PASSWORD not in message
            assert _floor(reader)["head"] == initial
        connection.close()
