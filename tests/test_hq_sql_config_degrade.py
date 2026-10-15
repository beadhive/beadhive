"""A transient HQ config read is retried, then a hive-local read may use the cache (bh-kjxe9).

The SQL read itself is replaced at ``_load_snapshot_once`` (the one connection + verification
pass); the retry loop, the owner-only snapshot cache, the restore-floor match and the
trusted-only degradation run for real. Time is a ``FakeClock``: backoff sleeps and cache age
advance virtual time, so nothing waits.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from tests.harness.clock import FakeClock
from typer.testing import CliRunner

from beadhive import config_store, hq_sql_config
from beadhive import hq_authority_enforce as authority_mode
from beadhive.hq_sql_config import (
    CONFIG_READ_ATTEMPTS,
    SqlConfigError,
    SqlConfigUnavailable,
    SqlFleetConfigRevisionStore,
)
from beadhive.modules.config.domain.ports import FleetConfigDocument, FleetConfigSnapshot

HEAD = "a" * 32
DOCUMENTS = (FleetConfigDocument("fleet.yaml", "hq:\n  mode: dolt-server\n"),)


@pytest.fixture(autouse=True)
def _fresh_process_state():
    hq_sql_config._DEGRADED.clear()
    hq_sql_config._degraded_warned.clear()
    yield
    hq_sql_config._DEGRADED.clear()
    hq_sql_config._degraded_warned.clear()


class _Reader:
    """Scripted ``_load_snapshot_once``: each entry is an exception to raise or ``"ok"``."""

    def __init__(self, store, clock, script):
        self.store, self.clock, self.script, self.calls = store, clock, list(script), 0

    def __call__(self, *, revision=None, deadline=None):
        self.calls += 1
        step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, Exception):
            raise step
        now = self.clock.wall()
        return FleetConfigSnapshot("sql:backend", HEAD, "gen", now, now + 30, DOCUMENTS)


def _store(tmp_path, monkeypatch, script=(), *, ttl=30):
    clock = FakeClock()
    floor = tmp_path / "state" / "hq-config-floor.json"
    floor.parent.mkdir(mode=0o700)
    settings = {
        "reader": {"host": "10.10.10.120", "port": 3308, "user": "secret-user"},
        "floor_path": str(floor),
        "backend_identity": "backend",
        "generation": "gen",
        "cache_ttl": ttl,
    }
    store = SqlFleetConfigRevisionStore(
        settings, broker=object(), clock=clock.wall, sleep=clock.sleep
    )
    reader = _Reader(store, clock, script)
    monkeypatch.setattr(store, "_load_snapshot_once", reader)
    _write_floor(floor, HEAD)
    return store, clock, reader, floor


def _write_floor(floor, head):
    fd = os.open(floor, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"backend": "backend", "generation": "gen", "sequence": 2, "head": head}, handle)


def _outage():
    return SqlConfigUnavailable("verified HQ config connection unavailable")


# ---- bounded retry ---------------------------------------------------------------------------


def test_a_transient_failure_is_retried_with_doubling_backoff(tmp_path, monkeypatch):
    store, clock, reader, _floor = _store(tmp_path, monkeypatch, [_outage(), _outage()])
    assert store.load_snapshot().commit_revision == HEAD
    assert reader.calls == 3
    assert clock.sleeps == [0.25, 0.5]


def test_exhausted_retries_name_the_endpoint_and_outcome_never_the_account(tmp_path, monkeypatch):
    store, clock, reader, _floor = _store(tmp_path, monkeypatch, [_outage()] * 5)
    with pytest.raises(SqlConfigUnavailable) as excinfo:
        store.load_snapshot()
    message = str(excinfo.value)
    assert "verified HQ config connection unavailable" in message
    assert "reader 10.10.10.120:3308" in message
    assert f"failed {CONFIG_READ_ATTEMPTS} of {CONFIG_READ_ATTEMPTS} attempts" in message
    assert "secret-user" not in message
    assert reader.calls == CONFIG_READ_ATTEMPTS
    assert clock.sleeps == [0.25, 0.5]


def test_a_verification_refusal_is_never_retried(tmp_path, monkeypatch):
    refusal = SqlConfigError("HQ configuration rollback or amended head detected")
    store, clock, reader, _floor = _store(tmp_path, monkeypatch, [refusal])
    with pytest.raises(SqlConfigError, match="rollback") as excinfo:
        store.load_snapshot()
    assert not isinstance(excinfo.value, SqlConfigUnavailable)
    assert reader.calls == 1 and clock.sleeps == []


def test_retry_never_sleeps_past_the_callers_deadline(tmp_path, monkeypatch):
    store, clock, reader, _floor = _store(tmp_path, monkeypatch, [_outage()] * 5)
    with pytest.raises(SqlConfigUnavailable, match="failed 1 of"):
        store.load_snapshot(deadline=time.monotonic() - 1)
    assert reader.calls == 1 and clock.sleeps == []


def test_a_verified_read_leaves_an_owner_only_cache(tmp_path, monkeypatch):
    store, _clock, _reader, floor = _store(tmp_path, monkeypatch)
    store.load_snapshot()
    cache = floor.with_name(floor.name + ".snapshot")
    assert cache.stat().st_mode & 0o777 == 0o600
    assert json.loads(cache.read_text())["head"] == HEAD


# ---- bounded-age cached fallback -------------------------------------------------------------


def _warm(tmp_path, monkeypatch, *, trusted=True, ttl=30):
    monkeypatch.setattr(authority_mode, "trusted", lambda: trusted)
    store, clock, reader, floor = _store(tmp_path, monkeypatch, ttl=ttl)
    store.load_snapshot()  # a verified read caches the snapshot
    reader.calls = 0
    return store, clock, reader, floor


def test_trusted_hive_local_read_degrades_to_the_cache_with_one_warning(tmp_path, monkeypatch):
    store, clock, reader, _floor = _warm(tmp_path, monkeypatch)
    clock.advance(10)
    reader.script = [_outage()] * 10
    warnings = []
    snapshot = store.load_snapshot_or_cached(warn=warnings.append)
    assert snapshot.commit_revision == HEAD and snapshot.documents == DOCUMENTS
    assert snapshot.valid_until == snapshot.fetched_at + 30
    assert reader.calls == CONFIG_READ_ATTEMPTS
    (warning,) = warnings
    assert "reader 10.10.10.120:3308" in warning and "10s old" in warning
    assert "hq.sql.cache_ttl=30s" in warning
    # Later reads in the same command reuse it without retrying HQ or warning again.
    assert store.load_snapshot_or_cached(warn=warnings.append) is snapshot
    assert reader.calls == CONFIG_READ_ATTEMPTS and len(warnings) == 1


def test_signed_mode_never_uses_the_cache(tmp_path, monkeypatch):
    store, clock, reader, _floor = _warm(tmp_path, monkeypatch, trusted=False)
    reader.script = [_outage()] * 10
    with pytest.raises(SqlConfigUnavailable, match="no cached fallback in signed authority mode"):
        store.load_snapshot_or_cached(warn=pytest.fail)


def test_a_cache_older_than_cache_ttl_fails_closed(tmp_path, monkeypatch):
    store, clock, reader, _floor = _warm(tmp_path, monkeypatch, ttl=30)
    clock.advance(31)
    reader.script = [_outage()] * 10
    with pytest.raises(SqlConfigUnavailable, match=r"older than hq.sql.cache_ttl \(30s\)"):
        store.load_snapshot_or_cached(warn=pytest.fail)


def test_a_moved_restore_floor_refuses_the_cache(tmp_path, monkeypatch):
    store, _clock, reader, floor = _warm(tmp_path, monkeypatch)
    _write_floor(floor, "b" * 32)  # another reader verified a newer head
    reader.script = [_outage()] * 10
    with pytest.raises(SqlConfigUnavailable, match="restore floor moved past the cached"):
        store.load_snapshot_or_cached(warn=pytest.fail)


def test_a_loosened_cache_file_refuses(tmp_path, monkeypatch):
    store, _clock, reader, floor = _warm(tmp_path, monkeypatch)
    floor.with_name(floor.name + ".snapshot").chmod(0o644)
    reader.script = [_outage()] * 10
    with pytest.raises(SqlConfigUnavailable, match="custody changed"):
        store.load_snapshot_or_cached(warn=pytest.fail)


def test_a_tampered_cache_refuses(tmp_path, monkeypatch):
    store, _clock, reader, floor = _warm(tmp_path, monkeypatch)
    cache = floor.with_name(floor.name + ".snapshot")
    body = json.loads(cache.read_text())
    body["documents"][0][1] += "managed_repos: []\n"
    cache.write_text(json.dumps(body))
    reader.script = [_outage()] * 10
    with pytest.raises(SqlConfigUnavailable, match="digest mismatch"):
        store.load_snapshot_or_cached(warn=pytest.fail)


def test_a_verification_refusal_never_degrades(tmp_path, monkeypatch):
    store, _clock, reader, _floor = _warm(tmp_path, monkeypatch)
    reader.script = [SqlConfigError("HQ configuration history fork detected")]
    with pytest.raises(SqlConfigError, match="fork"):
        store.load_snapshot_or_cached(warn=pytest.fail)


def test_no_cache_at_all_names_why(tmp_path, monkeypatch):
    monkeypatch.setattr(authority_mode, "trusted", lambda: True)
    store, _clock, _reader, _floor = _store(tmp_path, monkeypatch, [_outage()] * 10)
    with pytest.raises(SqlConfigUnavailable, match="no cached verified snapshot"):
        store.load_snapshot_or_cached(warn=pytest.fail)


# ---- only hive-local reads may degrade -------------------------------------------------------


def test_only_an_ordinary_read_inside_the_hive_local_scope_asks_to_degrade(monkeypatch):
    seen = []

    def attachment(api, host=None, *, hive_local=False):
        seen.append(hive_local)
        return None, "snapshot"

    monkeypatch.setattr(config_store, "sql_selected", lambda api, host=None: True)
    monkeypatch.setattr(config_store, "_sql_attachment", attachment)
    monkeypatch.setattr(config_store, "_fleet_document", lambda api, snapshot: snapshot)
    api = object()
    config_store.load_fleet(api)
    with config_store.hive_local_reads():
        config_store.load_fleet(api)
    config_store.load_fleet(api)
    assert seen == [False, True, False]


def test_bh_bd_runs_inside_the_hive_local_scope(monkeypatch):
    from beadhive import bd
    from beadhive.cli import app

    monkeypatch.setenv("BH_BD_PASS_ENABLED", "1")
    scoped = []
    monkeypatch.setattr(
        bd, "passthrough", lambda *a, **k: scoped.append(config_store._hive_local_reads.get())
    )
    result = CliRunner().invoke(app, ["bd", "ready"])
    assert result.exit_code == 0, result.output
    assert scoped == [True]
    assert config_store._hive_local_reads.get() is False
