"""The keyless ``authority_signer`` branch of ``SqlRuntimeOperator.publish`` (bh-v3dyp).

Real production writer over a recording SQL transport (the setup of
``test_frame_release_upgrade``). The signer branch must honour the same fence as the legacy
``operator_key`` path: the unsigned marker is written only on a trusted host.
"""

from __future__ import annotations

import time

import pytest

from beadhive import hq_authority_enforce
from beadhive.hq_sql_operator import SqlOperatorError, SqlRuntimeOperator
from beadhive.hq_sql_runtime_schema import inbox_table
from beadhive.hq_sql_signatures import sign_authority
from test_frame_release_upgrade import fixture, prepare, upgrade  # noqa: F401

MODE_ENV = hq_authority_enforce.MODE_ENV


@pytest.fixture(autouse=True)
def _enforced_baseline(monkeypatch):
    for name in (MODE_ENV, hq_authority_enforce.ENFORCE_ENV):
        monkeypatch.delenv(name, raising=False)
    hq_authority_enforce.reset_cache()
    yield
    hq_authority_enforce.reset_cache()


def _writer(fixture, monkeypatch):  # noqa: F811
    state, digest = prepare(fixture)
    route = upgrade.route_for(state, "frame")
    table = inbox_table(route[0], route[4])
    queries = []

    class Cursor:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, parameters=None):
            queries.append((sql, parameters))
            self.sql, self.parameters = sql, parameters

        def fetchone(self):
            if "CURRENT_USER()" in self.sql:
                return "operator@localhost", "runtime", "main", "2.3.5"
            if "DOLT_HASHOF" in self.sql:
                return ("runtime-head",)
            if "DOLT_COMMIT" in self.sql:
                return ("next-runtime-head",)
            raise AssertionError(self.sql)

        def fetchall(self):
            if "information_schema.tables" in self.sql:
                return [(table,)]
            assert "dolt_status" in self.sql
            return []

    class Connection:
        commits = 0
        rollbacks = 0

        def cursor(self):
            return Cursor()

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            pass

    connection = Connection()
    crossref = (
        fixture.snapshot.backend_identity,
        fixture.snapshot.generation,
        fixture.snapshot.commit_revision,
    )
    operator = SqlRuntimeOperator(
        {
            "authority_writer": {"user": "operator", "database": "runtime"},
            "runtime_operator_public_key": fixture.public,
            "runtime_backend_identity": "runtime-backend",
            "runtime_generation": "runtime-generation",
        },
        clock=lambda: 1000,
    )
    monkeypatch.setattr(operator, "_open", lambda **kwargs: (connection, time.monotonic() + 30))
    monkeypatch.setattr(
        operator.authority,
        "verified_state_at",
        lambda *args, **kwargs: (fixture.state, crossref, {}),
    )
    monkeypatch.setattr(
        operator.authority, "load_latest_config_at", lambda *args, **kwargs: fixture.snapshot
    )
    monkeypatch.setattr(operator.authority, "fresh_config_head_fence", lambda *a, **k: None)
    monkeypatch.setattr(operator.authority, "fresh_runtime_head_fence", lambda *a, **k: None)
    monkeypatch.setattr(
        operator, "load", lambda **kwargs: ("next-runtime-head", state, crossref, {})
    )
    kwargs = {
        "expected_revision": "runtime-head",
        "provisioned_route": route,
        "release_upgrade": {"frame": "frame", "request": fixture.request, "plan_sha256": digest},
    }
    return operator, state, kwargs, connection, queries


def _commit_messages(queries):
    return [p for sql, p in queries if "DOLT_COMMIT" in sql]


def test_a_valid_signature_publishes_as_signed(fixture, monkeypatch):  # noqa: F811
    operator, state, kwargs, connection, queries = _writer(fixture, monkeypatch)
    signer = lambda record: sign_authority(record, signing_key=str(fixture.key))  # noqa: E731
    assert operator.publish(state, authority_signer=signer, **kwargs) == "next-runtime-head"
    assert connection.commits == 1
    assert any("UPDATE hq_authority" in sql for sql, _ in queries)
    assert "HQ signed authority" in str(_commit_messages(queries))


def test_the_unsigned_marker_publishes_unsigned_in_trusted_mode(fixture, monkeypatch):  # noqa: F811
    monkeypatch.setenv(MODE_ENV, "trusted")
    hq_authority_enforce.reset_cache()
    operator, state, kwargs, connection, queries = _writer(fixture, monkeypatch)
    signer = lambda record: hq_authority_enforce.UNSIGNED_SIGNATURE  # noqa: E731
    assert operator.publish(state, authority_signer=signer, **kwargs) == "next-runtime-head"
    assert connection.commits == 1
    assert "HQ unsigned (trusted) authority" in str(_commit_messages(queries))


def test_the_unsigned_marker_is_refused_on_an_enforced_host(fixture, monkeypatch):  # noqa: F811
    operator, state, kwargs, connection, queries = _writer(fixture, monkeypatch)
    signer = lambda record: hq_authority_enforce.UNSIGNED_SIGNATURE  # noqa: E731
    with pytest.raises(SqlOperatorError, match="requires separate --operator-key"):
        operator.publish(state, authority_signer=signer, **kwargs)
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert not any("UPDATE hq_authority" in sql for sql, _ in queries)
