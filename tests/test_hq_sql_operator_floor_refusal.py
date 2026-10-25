"""The operator authority read names a restore-floor refusal instead of an outage (bh-a0wem).

``SqlRuntimeOperator.load`` is what ``bh hq operator status`` and the native recovery
(``SqlControlPlane.renew``) read through. A floor refusal there used to collapse into
"protected authority writer read unavailable", hiding the actionable recovery pointer. Here a
stand-in connection and a mocked clock prove the refusal text reaches the operator verbatim while
any other failure still reports only the fixed generic text. The floor checks themselves run on a
real private Dolt server in ``test_hq_sql_provision_history_int``.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from beadhive.hq_sql_operator import SqlOperatorError, SqlRuntimeOperator
from beadhive.hq_sql_runtime import (
    AMENDED_REFUSAL,
    FLOOR_RECOVERY_DOC,
    SqlRuntimeAuthority,
    SqlRuntimeError,
    SqlRuntimeFloorRefusal,
)

NOW = 1_800_000_000.0
SECRET = "s3cr3t-row-content"


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if sql.startswith("SELECT CURRENT_USER()"):
            self._result = [("authority_writer@localhost", "rt", "main", "2.3.5")]
        elif "DOLT_HASHOF" in sql:
            self._result = [("h" * 32,)]
        else:
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _Connection:
    def __init__(self, world):
        self.world = world

    def cursor(self):
        return _Cursor()

    def rollback(self):
        pass

    def close(self):
        self.world.closed += 1


def _operator(monkeypatch, failure):
    world = SimpleNamespace(closed=0)
    operator = SqlRuntimeOperator(
        {
            "runtime": None,
            "authority_writer": {
                "user": "authority_writer",
                "database": "rt",
                "operation_timeout": 30,
            },
        },
        broker=object(),
        clock=lambda: NOW,  # mocked clock
    )
    monkeypatch.setattr(
        operator, "_open", lambda deadline=None: (_Connection(world), time.monotonic() + 30)
    )

    def verified(cursor, head, deadline=None, allow_expired=False):
        raise failure

    monkeypatch.setattr(operator.authority, "verified_state_at", verified)
    return operator, world


class _DiffSql:
    """Only what the amended-authority diagnostic reads: the changed table names."""

    @staticmethod
    def diff_summary(_floor, _head):
        return [("", "dolt_schemas", "modified", 1, 0)]


def _amended():
    return SqlRuntimeAuthority._amended(
        _DiffSql(),
        "3ot3cl9b5pps" + "0" * 20,
        "76m6kilekij8" + "0" * 20,
        "commit 76m6kilekij8 adds stamp triggers for an incarnation that is not an "
        "unauthorized next or first (epoch 0) epoch",
    )


@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param(_amended, id="amended"),
        pytest.param(
            lambda: SqlRuntimeFloorRefusal(
                "HQ runtime history fork detected: head 76m6kilekij8 does not descend from "
                f"restore floor 3ot3cl9b5pps; see {FLOOR_RECOVERY_DOC}"
            ),
            id="fork",
        ),
    ],
)
def test_load_surfaces_the_restore_floor_refusal_and_its_recovery_pointer(monkeypatch, refusal):
    error = refusal()
    operator, world = _operator(monkeypatch, error)
    with pytest.raises(SqlOperatorError) as refused:
        operator.load()
    message = str(refused.value)
    assert message == str(error)
    assert FLOOR_RECOVERY_DOC in message
    assert "read unavailable" not in message
    assert world.closed == 1


def test_amended_refusal_names_tables_and_the_reason_not_rows(monkeypatch):
    operator, _world = _operator(monkeypatch, _amended())
    with pytest.raises(SqlOperatorError) as refused:
        operator.load()
    message = str(refused.value)
    assert message.startswith(AMENDED_REFUSAL)
    assert "tables changed since the floor: dolt_schemas" in message
    assert "not an unauthorized next or first (epoch 0) epoch" in message


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(SqlRuntimeError(f"protected HQ runtime metadata {SECRET}"), id="runtime"),
        pytest.param(RuntimeError(f"server said {SECRET}"), id="unexpected"),
    ],
)
def test_load_keeps_other_failures_generic_and_secret_free(monkeypatch, failure):
    operator, world = _operator(monkeypatch, failure)
    with pytest.raises(SqlOperatorError) as refused:
        operator.load()
    assert str(refused.value) == "protected authority writer read unavailable"
    assert SECRET not in str(refused.value)
    assert world.closed == 1
