"""``bh hq release-catalog provision``: the one-time, server-local catalog table (bh-vfrem.8).

The table creation itself (and the restore-floor acceptance of that commit) is covered on a real
private Dolt server by ``test_hq_release_catalog_int``. Here the operator wrapper and the verb
are proved with a stand-in connection and a mocked clock: which binding it uses, that both heads
verify, how it reports a repeat run, and how it refuses.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from beadhive import hq_release_catalog_cli as cli
from beadhive import hq_sql_session_provision as provisioning
from beadhive.hq_sql_operator import SqlOperatorError, SqlRuntimeOperator

NOW = 1_800_000_000.0


class _Cursor:
    def __init__(self, world):
        self.world = world

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if sql.startswith("SELECT CURRENT_USER()"):
            self._result = [(self.world.user, "rt", "main", "2.3.5")]
        elif "DOLT_HASHOF" in sql:
            self._result = [(self.world.heads[-1],)]
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
        return _Cursor(self.world)

    def close(self):
        self.world.closed += 1


def _operator(monkeypatch, *, user="authority_writer@localhost", created=True, fail=None):
    world = SimpleNamespace(user=user, heads=["head-1"], closed=0, verified=[])
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
        assert allow_expired is True
        world.verified.append(head)
        return {}, None, None

    monkeypatch.setattr(operator.authority, "verified_state_at", verified)

    def provision(cursor):
        if fail is not None:
            raise fail
        if created:
            world.heads.append("head-2")
        return created

    monkeypatch.setattr(provisioning, "provision_release_catalog", provision)
    return operator, world


def test_provision_verifies_the_head_before_and_after_and_reports_the_new_head(monkeypatch):
    operator, world = _operator(monkeypatch)
    assert operator.provision_release_catalog() == {"created": True, "head": "head-2"}
    assert world.verified == ["head-1", "head-2"] and world.closed == 1


def test_provision_is_idempotent_and_changes_no_head(monkeypatch):
    operator, world = _operator(monkeypatch, created=False)
    assert operator.provision_release_catalog() == {"created": False, "head": "head-1"}
    assert world.verified == ["head-1", "head-1"]


def test_provision_refuses_a_connection_that_is_not_the_bound_principal(monkeypatch):
    operator, world = _operator(monkeypatch, user="someone_else@localhost")
    with pytest.raises(SqlOperatorError, match="principal or endpoint mismatch"):
        operator.provision_release_catalog()
    assert world.closed == 1


def test_provision_names_the_missing_privilege_without_the_server_message(monkeypatch):
    import pymysql

    denied = pymysql.err.OperationalError(1142, "CREATE command denied to user 'x' s3cr3t")
    operator, world = _operator(monkeypatch, fail=denied)
    with pytest.raises(SqlOperatorError, match="provisioning denied") as caught:
        operator.provision_release_catalog()
    assert "s3cr3t" not in str(caught.value) and world.closed == 1


def test_provision_wraps_an_unexpected_failure(monkeypatch):
    operator, _world = _operator(monkeypatch, fail=RuntimeError("boom s3cr3t"))
    with pytest.raises(SqlOperatorError, match="unavailable") as caught:
        operator.provision_release_catalog()
    assert "s3cr3t" not in str(caught.value)


# ---- the verb --------------------------------------------------------------------------------


def _app():
    app = typer.Typer()
    app.command("rc")(cli.release_catalog_cmd)
    app.command("noop")(lambda: None)
    return app


def test_the_verb_runs_the_operator_and_prints_json(monkeypatch):
    seen = []

    class Operator:
        def provision_release_catalog(self):
            return {"created": True, "head": "h"}

    monkeypatch.setattr(cli, "_operator", lambda settings: seen.append(settings) or Operator())
    result = CliRunner().invoke(_app(), ["rc", "provision", "--operator-settings", "ops.yaml"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"created": True, "head": "h"}
    assert seen == ["ops.yaml"]


@pytest.mark.parametrize(
    "extra",
    [["0.28.5"], ["--all"], ["--reason", "x"], ["--bundle", "b"], ["--wheel", "w"]],
)
def test_the_verb_takes_no_other_arguments(monkeypatch, extra):
    monkeypatch.setattr(cli, "_operator", lambda settings: pytest.fail("must not connect"))
    result = CliRunner().invoke(_app(), ["rc", "provision", *extra])
    assert result.exit_code == 1 and "takes no other arguments" in result.output


def test_the_verb_needs_server_local_operator_settings(monkeypatch):
    monkeypatch.delenv("BH_HQ_OPERATOR_SETTINGS", raising=False)
    result = CliRunner().invoke(_app(), ["rc", "provision"])
    assert result.exit_code == 1 and "BH_HQ_OPERATOR_SETTINGS" in result.output


def test_the_verb_reports_an_operator_refusal_and_exits_nonzero(monkeypatch):
    class Operator:
        def provision_release_catalog(self):
            raise SqlOperatorError("release catalog provisioning denied")

    monkeypatch.setattr(cli, "_operator", lambda settings: Operator())
    result = CliRunner().invoke(_app(), ["rc", "provision"])
    assert result.exit_code == 1 and "provisioning denied" in result.output
