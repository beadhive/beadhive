"""Server-mode scratch restore transport and failure cleanup (bh-367wc).

bd's SQL client enforces a short fixed read timeout, so a long ``CALL DOLT_BACKUP('restore')``
through ``bd sql`` is cut off with ``invalid connection`` and leaves a partial database. The
restore therefore runs over the Dolt SQL client; any failure drops the partial scratch database.
Real-server tests use a PRIVATE bd shared server (own dir, ephemeral port), never the fleet's.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from beadhive import backup_scratch as bs
from beadhive import engine
from beadhive import native_backup as nb
from beadhive.run import run
from harness.beads import skip_if_no_bd
from harness.world import reap_dolt_server

URL = "file:///backups/set/dolt-native"


class FakeEngine:
    """Records the SQL it is given; ``dbs`` is the server's database list."""

    def __init__(self, dbs=()):
        self.dbs = set(dbs)
        self.sql: list[str] = []

    def query_rows(self, cwd, query):
        if "information_schema.schemata" in query:
            return [{"n": d} for d in sorted(self.dbs)]
        return []

    def invoke(self, args, *, cwd=None, capture=False, timeout=0, **kw):
        self.sql.append(args[1])
        if args[1].startswith("DROP DATABASE"):
            self.dbs.discard(args[1].split("`")[1])
        if args[1].startswith("CALL DOLT_BACKUP"):
            return SimpleNamespace(returncode=1, stdout="", stderr="Error: invalid connection\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _scratch(tmp_path, engine_, **kw):
    return bs.ServerScratch(engine_, tmp_path, data_dir=tmp_path / "data", **kw)


def test_restore_goes_over_the_dolt_client_with_the_configured_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("BEADS_DOLT_SERVER_HOST", "10.1.2.3")
    monkeypatch.setenv("BEADS_DOLT_SERVER_PORT", "4406")
    monkeypatch.setenv("BEADS_DOLT_SERVER_USER", "beads")
    monkeypatch.setenv("BEADS_DOLT_PASSWORD", "s3cret")
    monkeypatch.delenv("BEADS_DOLT_SERVER_TLS", raising=False)
    monkeypatch.setenv(bs.RESTORE_TIMEOUT_ENV, "7200")
    eng = FakeEngine()
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        eng.dbs.add("bh_scr_a1")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bs, "run", fake_run)
    handle = _scratch(tmp_path, eng).restore(URL, "bh_scr_a1")
    assert handle.name == "bh_scr_a1"
    ((argv, kw),) = calls
    assert argv[:7] == ["dolt", "--host", "10.1.2.3", "--port", "4406", "--no-tls", "--user"]
    assert argv[7] == "beads" and "sql" in argv and "s3cret" not in argv
    assert kw["env"]["DOLT_CLI_PASSWORD"] == "s3cret"
    assert kw["timeout"] == 7200.0
    assert any("DOLT_BACKUP('restore'" in a for a in argv)
    assert eng.sql == []  # nothing went through the short-lived bd sql client


def test_explicit_timeout_wins_over_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(bs.RESTORE_TIMEOUT_ENV, "9")
    assert _scratch(tmp_path, FakeEngine(), restore_timeout=11.0)._timeout == 11.0
    assert _scratch(tmp_path, FakeEngine())._timeout == 9.0
    monkeypatch.setenv(bs.RESTORE_TIMEOUT_ENV, "junk")
    assert _scratch(tmp_path, FakeEngine())._timeout == bs.RESTORE_TIMEOUT


def test_a_missing_dolt_binary_falls_back_to_bd_sql(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "run", lambda argv, **kw: SimpleNamespace(returncode=127, stderr=""))
    eng = FakeEngine()
    with pytest.raises(bs.ScratchError, match="failed via bd"):
        _scratch(tmp_path, eng).restore(URL, "bh_scr_a2")
    assert any(s.startswith("CALL DOLT_BACKUP") for s in eng.sql)


def test_a_cut_off_connection_is_explained_and_the_partial_database_is_dropped(
    tmp_path, monkeypatch
):
    eng = FakeEngine()
    dropped = tmp_path / "data" / bs._DROPPED_DIR / "bh_scr_a3"
    dropped.mkdir(parents=True)

    def partial(self, statement):
        eng.dbs.add("bh_scr_a3")  # the server created it before the connection died
        return 1, "Error: exec error: invalid connection\n", "bd"

    monkeypatch.setattr(bs.ServerScratch, "_call_restore", partial)
    with pytest.raises(bs.ScratchError) as caught:
        _scratch(tmp_path, eng).restore(URL, "bh_scr_a3")
    message = str(caught.value)
    assert "read_timeout_millis" in message and "dolt" in message
    assert "partial scratch database 'bh_scr_a3' dropped" in message and str(dropped) in message
    assert "bh_scr_a3" not in eng.dbs
    assert "DROP DATABASE `bh_scr_a3`" in eng.sql


def test_timeout_and_unexpected_errors_also_drop_the_partial_database(tmp_path, monkeypatch):
    def hang(argv, **kw):
        eng.dbs.add("bh_scr_a4")
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    eng = FakeEngine()
    monkeypatch.setattr(bs, "run", hang)
    with pytest.raises(bs.ScratchError, match=r"timed out after 5s.*dropped"):
        _scratch(tmp_path, eng, restore_timeout=5.0).restore(URL, "bh_scr_a4")
    assert "bh_scr_a4" not in eng.dbs

    def explode(self, statement):
        eng.dbs.add("bh_scr_a5")
        raise OSError("disk gone")

    monkeypatch.setattr(bs.ServerScratch, "_call_restore", explode)
    with pytest.raises(bs.ScratchError, match=r"disk gone.*dropped"):
        _scratch(tmp_path, eng).restore(URL, "bh_scr_a5")
    assert "bh_scr_a5" not in eng.dbs


def test_only_scratch_named_databases_are_ever_dropped(tmp_path):
    eng = FakeEngine({"bh"})
    assert "not a scratch name" in _scratch(tmp_path, eng)._discard_partial("bh")
    assert eng.sql == [] and eng.dbs == {"bh"}


def test_a_failure_with_nothing_left_reports_it_and_never_drops(tmp_path, monkeypatch):
    eng = FakeEngine()
    monkeypatch.setattr(bs.ServerScratch, "_call_restore", lambda self, s: (1, "boom", "dolt"))
    with pytest.raises(bs.ScratchError, match="boom; no partial scratch database remained"):
        _scratch(tmp_path, eng).restore(URL, "bh_scr_a6")
    assert not any(s.startswith("DROP") for s in eng.sql)


# ---- a real, PRIVATE server --------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def private_server(tmp_path, monkeypatch):
    shared = tmp_path / "shared-server"
    monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(shared))
    monkeypatch.setenv("BEADS_DOLT_SERVER_PORT", str(_free_port()))
    monkeypatch.setenv("BH_HOME", str(tmp_path / "bhhome"))
    monkeypatch.setenv("BD_NON_INTERACTIVE", "1")
    yield shared
    reap_dolt_server(shared)


def _init_hive(tmp_path) -> Path:
    hive = tmp_path / "hive"
    hive.mkdir()
    run(["git", "init", "-q", "-b", "main"], cwd=str(hive), check=True, capture=True)
    run(
        ["bd", "init", "--shared-server", "--prefix", "ns", "--non-interactive", "--quiet"],
        cwd=str(hive),
        check=True,
        capture=True,
        timeout=120,
    )
    run(["bd", "-C", str(hive), "create", "x", "-p", "2"], check=True, capture=True, timeout=120)
    return hive


@pytest.mark.integration
@pytest.mark.dolt_server
@skip_if_no_bd
def test_real_server_restore_uses_the_dolt_client_and_survives_past_bds_read_timeout(
    tmp_path, private_server, monkeypatch
):
    hive = _init_hive(tmp_path)
    eng = engine.BdEngine()
    res = nb.take_snapshot({}, hive, slug="s", prefix="ns", engine=eng)
    assert res.ok, res.errors
    seen = []
    real_run = bs.run

    def spy(argv, **kw):
        seen.append(argv)
        return real_run(argv, **kw)

    monkeypatch.setattr(bs, "run", spy)
    scratch = bs.ServerScratch(eng, hive)
    handle = scratch.restore(f"file://{Path(res.set_dir) / nb.NATIVE_NAME}", "ns_scr_real1")
    assert seen and seen[0][0] == "dolt" and "--host" in seen[0]
    assert handle.name in scratch._databases()
    rows = scratch.query(handle, "SELECT COUNT(*) AS c FROM `ns_scr_real1`.`issues`")
    assert int(rows[0]["c"]) >= 1

    # the exact failure bd's client hits: a call longer than its fixed read timeout (bd sql
    # gives up at ~10s); the Dolt client the restore uses completes it.
    long_call = ["dolt", *seen[0][1:-1], "SELECT SLEEP(12)"]
    done = real_run(long_call, check=False, capture=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert scratch.drop(handle).left_copy


@pytest.mark.integration
@pytest.mark.dolt_server
@skip_if_no_bd
def test_real_server_failed_restore_leaves_no_scratch_database(tmp_path, private_server):
    hive = _init_hive(tmp_path)
    eng = engine.BdEngine()
    scratch = bs.ServerScratch(eng, hive)
    with pytest.raises(bs.ScratchError, match="failed via dolt"):
        scratch.restore(f"file://{tmp_path / 'no-such-backup'}", "ns_scr_real2")
    assert "ns_scr_real2" not in scratch._databases()

    # a partial database the server really created is dropped, and the copy is reported
    def partial(self, statement):
        eng.invoke(["sql", "CREATE DATABASE `ns_scr_real3`"], cwd=hive, capture=True)
        return 1, "Error: invalid connection", "dolt"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bs.ServerScratch, "_call_restore", partial)
        with pytest.raises(
            bs.ScratchError, match="partial scratch database 'ns_scr_real3' dropped"
        ):
            scratch.restore(f"file://{tmp_path / 'x'}", "ns_scr_real3")
    assert "ns_scr_real3" not in scratch._databases()
    assert (Path(scratch._locate_data_dir()) / bs._DROPPED_DIR / "ns_scr_real3").is_dir()
