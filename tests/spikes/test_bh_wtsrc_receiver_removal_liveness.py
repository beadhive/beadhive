"""Executable evidence for the bh-wtsrc spike; not a product contract test.

Question: can frame liveness be a per-frame, server-timed session row written under the frame's
own SQL grant, with conformance as a separately scheduled evidence row and eligibility computed
at read time, without the trusted receiver?  See
``docs/spikes/bh-wtsrc-receiver-removal-liveness.md``.

Every server here is owned by the test and runs with its own ``DOLT_ROOT_PATH`` and ``HOME``.
Some probes deliberately run ``SET GLOBAL`` / ``SET PERSIST`` as an unprivileged frame principal;
the isolated root keeps the persisted value inside ``tmp_path`` and out of the operator's
``~/.dolt/config_global.json``.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import statistics
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pymysql
import pytest

from harness.processes import process_context
from harness.world import free_port

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]

DB = "hq_live"
PASSWORD = "fixture-only"


# --- owned, isolated server ---------------------------------------------------------------------


@contextmanager
def _isolated_server(tmp_path: Path):
    """Start an owned dolt sql-server whose global config lives under *tmp_path* only."""
    dolt = shutil.which("dolt")
    if dolt is None:
        pytest.skip("dolt binary unavailable")
    root = tmp_path / "dolt-root"
    (root / ".dolt").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    (root / ".dolt" / "config_global.json").write_text(
        json.dumps(
            {
                "user.name": "fixture operator",
                "user.email": "operator@fixture.invalid",
                "versioncheck.disabled": "true",
                "metrics.disabled": "true",
            }
        )
    )
    port = free_port()
    (tmp_path / "server.yaml").write_text(
        f"data_dir: {data}\nlistener:\n  host: 127.0.0.1\n  port: {port}\n"
    )
    env = {**os.environ, "DOLT_ROOT_PATH": str(root), "HOME": str(home)}
    with (tmp_path / "server.log").open("w") as log:
        server = subprocess.Popen(
            [dolt, "sql-server", "--config", str(tmp_path / "server.yaml")],
            cwd=tmp_path,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        end = time.monotonic() + 30
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                if server.poll() is not None or time.monotonic() >= end:
                    raise AssertionError("owned Dolt server failed to start") from None
                time.sleep(0.1)
        version = _connect(port).cursor()
        version.execute("SELECT DOLT_VERSION()")
        assert version.fetchone()[0] == "2.3.5", "spike evidence is pinned to Dolt 2.3.5"
        yield port, root
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=15)


def _connect(port, user="root", password="", database=None, bind_address=None):
    return pymysql.connect(
        host="127.0.0.1",
        bind_address=bind_address,
        port=port,
        user=user,
        password=password,
        database=database,
        autocommit=True,
        connect_timeout=5,
        read_timeout=15,
        write_timeout=15,
    )


def _sql(connection, statement, args=None):
    with connection.cursor() as cursor:
        cursor.execute(statement, args)
        return cursor.fetchall()


def _refused(connection, statement) -> str:
    """Run *statement* and return the server's refusal text; fail if it was accepted."""
    try:
        _sql(connection, statement)
    except pymysql.MySQLError as exc:
        return str(exc.args[-1])
    raise AssertionError(f"frame principal was allowed: {statement}")


# --- operator-provisioned schema ---------------------------------------------------------------


def _session_ddl(frame: str) -> tuple[str, ...]:
    """One server-stamped session row and one evidence row per frame principal.

    The operator-owned BEFORE triggers overwrite every frame-supplied timestamp with
    UTC_TIMESTAMP(6), so neither a literal, a session/global time_zone nor SET timestamp can move
    a renewal or measurement.  Triggers only assign NEW.* columns: they write no other table.
    """
    return (
        f"CREATE TABLE frame_{frame}_session (id TINYINT PRIMARY KEY CHECK (id = 1), "
        "epoch BIGINT UNSIGNED NOT NULL, renewed_at DATETIME(6) NOT NULL)",
        f"CREATE TRIGGER frame_{frame}_session_ins BEFORE INSERT ON frame_{frame}_session "
        "FOR EACH ROW SET NEW.renewed_at = UTC_TIMESTAMP(6)",
        f"CREATE TRIGGER frame_{frame}_session_upd BEFORE UPDATE ON frame_{frame}_session "
        "FOR EACH ROW SET NEW.renewed_at = UTC_TIMESTAMP(6)",
        f"INSERT INTO frame_{frame}_session (id, epoch, renewed_at) VALUES (1, 1, '1970-01-02')",
        f"CREATE TABLE frame_{frame}_evidence (id TINYINT PRIMARY KEY CHECK (id = 1), "
        "release_digest VARCHAR(80) NOT NULL, profile VARCHAR(64) NOT NULL, "
        "status VARCHAR(32) NOT NULL, measured_at DATETIME(6) NOT NULL)",
        f"CREATE TRIGGER frame_{frame}_evidence_ins BEFORE INSERT ON frame_{frame}_evidence "
        "FOR EACH ROW SET NEW.measured_at = UTC_TIMESTAMP(6)",
        f"CREATE TRIGGER frame_{frame}_evidence_upd BEFORE UPDATE ON frame_{frame}_evidence "
        "FOR EACH ROW SET NEW.measured_at = UTC_TIMESTAMP(6)",
        f"INSERT INTO frame_{frame}_evidence VALUES (1, 'none', 'none', 'unknown', '1970-01-02')",
        f"CREATE USER frame_{frame}@'%' IDENTIFIED BY '{PASSWORD}'",
        # Liveness: renew only (UPDATE).  Evidence: replace own row only.  Authority: read only.
        f"GRANT SELECT, UPDATE ON {DB}.frame_{frame}_session TO frame_{frame}@'%'",
        f"GRANT SELECT, UPDATE ON {DB}.frame_{frame}_evidence TO frame_{frame}@'%'",
        f"GRANT SELECT ON {DB}.hq_grant TO frame_{frame}@'%'",
    )


def _provision(port, frames=("a", "b"), *, session_ttl_ms=300_000, evidence_ttl_ms=900_000):
    root = _connect(port)
    _sql(root, f"CREATE DATABASE {DB}")
    _sql(root, f"USE {DB}")
    # Ignore rules are committed before any live table exists (bh-v0k3i ordering).
    _sql(root, "INSERT INTO dolt_ignore VALUES ('frame_%', TRUE)")
    _sql(
        root,
        "CREATE TABLE hq_grant (frame_id VARCHAR(64) PRIMARY KEY, state VARCHAR(16) NOT NULL, "
        "epoch BIGINT UNSIGNED NOT NULL, release_digest VARCHAR(80) NOT NULL, "
        "profile VARCHAR(64) NOT NULL, session_ttl_ms INT UNSIGNED NOT NULL, "
        "evidence_ttl_ms INT UNSIGNED NOT NULL)",
    )
    for frame in frames:
        _sql(
            root,
            "INSERT INTO hq_grant VALUES (%s,'active',1,%s,'factory',%s,%s)",
            (frame, "sha256:" + "1" * 64, session_ttl_ms, evidence_ttl_ms),
        )
    _sql(root, "CALL DOLT_ADD('hq_grant','dolt_ignore')")
    _sql(root, "CALL DOLT_COMMIT('-m','operator grants and ignore policy')")
    for frame in frames:
        for statement in _session_ddl(frame):
            _sql(root, statement)
    # Dispatcher/reader principal: SELECT only.
    _sql(root, f"CREATE USER reader@'%' IDENTIFIED BY '{PASSWORD}'")
    _sql(root, f"GRANT SELECT ON {DB}.* TO reader@'%'")
    return root


ELIGIBILITY = """
SELECT
  g.state = 'active' AND s.epoch = g.epoch                                   AS granted,
  s.renewed_at > UTC_TIMESTAMP(6) - INTERVAL g.session_ttl_ms * 1000 MICROSECOND
                                                                             AS session_fresh,
  e.status = 'conformant' AND e.profile = g.profile                          AS evidence_pass,
  e.measured_at > UTC_TIMESTAMP(6) - INTERVAL g.evidence_ttl_ms * 1000 MICROSECOND
                                                                             AS evidence_unexpired,
  e.release_digest = g.release_digest                                        AS release_matches
FROM hq_grant AS g
JOIN frame_{frame}_session AS s ON s.id = 1
JOIN frame_{frame}_evidence AS e ON e.id = 1
WHERE g.frame_id = %s
"""

PREDICATES = ("granted", "session_fresh", "evidence_pass", "evidence_unexpired", "release_matches")


def _decide(reader, frame) -> dict[str, bool]:
    """Read-time eligibility from one statement, timed by the server clock alone."""
    rows = _sql(reader, ELIGIBILITY.format(frame=frame), (frame,))
    if len(rows) != 1:
        return {"authority_available": False}
    return {name: bool(value) for name, value in zip(PREDICATES, rows[0], strict=True)}


def _blocked_by(decision: dict[str, bool]) -> set[str]:
    return {name for name, value in decision.items() if not value}


# --- process targets (module scope, spawn-safe) ------------------------------------------------


def _renewer(port, frame, interval_s, stop, errors):
    """The frame's liveness loop: a no-op UPDATE; the trigger stamps server time."""
    connection = _connect(port, f"frame_{frame}", PASSWORD, DB)
    try:
        while not stop.is_set():
            try:
                _sql(connection, f"UPDATE frame_{frame}_session SET epoch = epoch WHERE id = 1")
            except pymysql.MySQLError as exc:
                errors.put(repr(exc))
            stop.wait(interval_s)
    finally:
        connection.close()


def _conformance_probe(port, frame, duration_s, started, finished):
    """A slow measurement that publishes its evidence only when it completes."""
    connection = _connect(port, f"frame_{frame}", PASSWORD, DB)
    try:
        started.set()
        time.sleep(duration_s)
        _sql(
            connection,
            f"UPDATE frame_{frame}_evidence SET release_digest=%s, profile='factory', "
            "status='conformant' WHERE id = 1",
            ("sha256:" + "1" * 64,),
        )
        finished.set()
    finally:
        connection.close()


def _coupled_heartbeat(port, frame, duration_s, started, finished):
    """Today's shape: the renewal carries conformance, so it waits for the measurement."""
    connection = _connect(port, f"frame_{frame}", PASSWORD, DB)
    try:
        started.set()
        time.sleep(duration_s)
        _sql(connection, f"UPDATE frame_{frame}_session SET epoch = epoch WHERE id = 1")
        finished.set()
    finally:
        connection.close()


# --- evidence ----------------------------------------------------------------------------------


def test_frame_grant_confines_writes_to_its_own_session_and_evidence(tmp_path):
    with _isolated_server(tmp_path) as (port, _root):
        root = _provision(port)
        frame_a = _connect(port, "frame_a", PASSWORD, DB)

        _sql(frame_a, "UPDATE frame_a_session SET epoch = epoch WHERE id = 1")
        _sql(frame_a, "UPDATE frame_a_evidence SET status = 'conformant' WHERE id = 1")

        refusals = {
            statement: _refused(frame_a, statement)
            for statement in (
                # cross-frame rows
                "UPDATE frame_b_session SET epoch = 9 WHERE id = 1",
                "UPDATE frame_b_evidence SET status = 'non-conformant' WHERE id = 1",
                "SELECT * FROM frame_b_session",
                # authority
                "UPDATE hq_grant SET state = 'active'",
                "INSERT INTO hq_grant VALUES ('c','active',1,'x','factory',1,1)",
                "DELETE FROM hq_grant",
                # own rows: renew-only, no fresh rows, no deletion
                "INSERT INTO frame_a_session VALUES (1, 1, NOW())",
                "DELETE FROM frame_a_session",
                # schema and triggers
                "ALTER TABLE frame_a_session ADD COLUMN x INT",
                "DROP TABLE frame_b_session",
                "CREATE TABLE frame_c_session (id INT PRIMARY KEY)",
                "DROP TRIGGER frame_a_session_upd",
                "CREATE TRIGGER t BEFORE UPDATE ON frame_a_session FOR EACH ROW SET NEW.epoch = 9",
                # ignore policy, version control, privileges
                "INSERT INTO dolt_ignore VALUES ('hq_grant', TRUE)",
                "DELETE FROM dolt_ignore",
                "CALL DOLT_ADD('-A')",
                "CALL DOLT_COMMIT('-Am', 'frame commit')",
                "CALL DOLT_BRANCH('frame-branch')",
                "CALL DOLT_CHECKOUT('-b', 'frame-branch-2')",
                "CALL DOLT_RESET('--hard')",
                f"GRANT SELECT ON {DB}.frame_b_session TO frame_a@'%'",
                "CREATE USER intruder@'%'",
            )
        }
        assert all(refusals.values()), refusals
        print("GRANT_REFUSALS " + json.dumps(refusals))

        assert _sql(root, "SELECT epoch FROM frame_b_session") == ((1,),)
        assert _sql(root, "SELECT status FROM frame_b_evidence") == (("unknown",),)
        assert _sql(root, "SELECT COUNT(*) FROM hq_grant") == ((2,),)
        triggers = {row[0] for row in _sql(root, "SELECT name FROM dolt_schemas")}
        assert "frame_a_session_upd" in triggers


def test_operator_trigger_stamps_server_utc_over_frame_literals_and_time_zone(tmp_path):
    with _isolated_server(tmp_path) as (port, _root):
        root = _provision(port)
        # Negative control: the same table shape without the trigger, stamped by the frame.
        _sql(
            root,
            "CREATE TABLE frame_a_naive (id TINYINT PRIMARY KEY, renewed_at DATETIME(6))",
        )
        _sql(root, "INSERT INTO frame_a_naive VALUES (1, UTC_TIMESTAMP(6))")
        _sql(root, f"GRANT SELECT, UPDATE ON {DB}.frame_a_naive TO frame_a@'%'")
        frame_a = _connect(port, "frame_a", PASSWORD, DB)

        def skew_s(table, column="renewed_at"):
            utc_now, stamped = _sql(root, f"SELECT UTC_TIMESTAMP(6), {column} FROM {table}")[0]
            return (stamped - utc_now).total_seconds()

        # 1. A literal from the frame is overwritten.
        _sql(frame_a, "UPDATE frame_a_session SET renewed_at = '2099-01-01' WHERE id = 1")
        assert abs(skew_s("frame_a_session")) < 5
        _sql(frame_a, "UPDATE frame_a_naive SET renewed_at = '2099-01-01' WHERE id = 1")
        assert skew_s("frame_a_naive") > 365 * 86400  # hole without the trigger

        # 2. Session time_zone: NOW() shifts, the trigger's UTC_TIMESTAMP does not.
        _sql(frame_a, "SET time_zone = '+14:00'")
        _sql(frame_a, "UPDATE frame_a_session SET epoch = epoch WHERE id = 1")
        assert abs(skew_s("frame_a_session")) < 5
        _sql(frame_a, "UPDATE frame_a_naive SET renewed_at = NOW(6) WHERE id = 1")
        assert skew_s("frame_a_naive") > 13 * 3600  # NOW() follows the frame's zone

        # 3. SET timestamp does not move the server clock either way.
        _sql(frame_a, "SET timestamp = 2000000000")
        _sql(frame_a, "UPDATE frame_a_session SET epoch = epoch WHERE id = 1")
        assert abs(skew_s("frame_a_session")) < 5
        _sql(frame_a, "SET timestamp = DEFAULT")

        # 4. A no-op renewal still fires the BEFORE UPDATE trigger and advances renewed_at.
        before = _sql(root, "SELECT renewed_at FROM frame_a_session")[0][0]
        time.sleep(0.05)
        _sql(frame_a, "UPDATE frame_a_session SET epoch = epoch WHERE id = 1")
        assert _sql(root, "SELECT renewed_at FROM frame_a_session")[0][0] > before

        # 5. Even a frame-set GLOBAL time_zone does not move trigger-stamped UTC.
        _sql(frame_a, "SET GLOBAL time_zone = '+14:00'")
        fresh = _connect(port, "frame_a", PASSWORD, DB)
        _sql(fresh, "UPDATE frame_a_session SET epoch = epoch WHERE id = 1")
        assert abs(skew_s("frame_a_session")) < 5
        _sql(root, "SET GLOBAL time_zone = 'SYSTEM'")

        # 6. Evidence measurement time is server-stamped the same way.
        _sql(frame_a, "UPDATE frame_a_evidence SET measured_at = '2099-01-01' WHERE id = 1")
        assert abs(skew_s("frame_a_evidence", "measured_at")) < 5


def test_trigger_body_writes_run_with_the_invoking_frame_privileges(tmp_path):
    """Dolt 2.3.5 checks a trigger body's DML against the *invoker*, not the trigger creator.

    So an operator trigger cannot be a write path for a frame, NEW.*-keyed cross-table writes
    reach only tables the frame could already write, and revoking (or dropping) the creator after
    the trigger exists changes nothing.  The only safe trigger body is ``SET NEW.<col> = ...``.
    """
    with _isolated_server(tmp_path) as (port, _root):
        root = _provision(port)
        _sql(root, "CREATE TABLE status_board (frame_id VARCHAR(16) PRIMARY KEY, n INT)")
        _sql(root, "INSERT INTO status_board VALUES ('a', 0), ('b', 0)")
        _sql(root, "CREATE TABLE frame_a_keyed (id TINYINT PRIMARY KEY, frame_id VARCHAR(16))")
        _sql(root, "INSERT INTO frame_a_keyed VALUES (1, 'a')")
        _sql(root, f"CREATE USER op@'%' IDENTIFIED BY '{PASSWORD}'")
        _sql(root, f"GRANT SELECT, INSERT, UPDATE, TRIGGER ON {DB}.* TO op@'%'")
        _sql(root, f"GRANT SELECT, UPDATE ON {DB}.frame_a_keyed TO frame_a@'%'")
        operator = _connect(port, "op", PASSWORD, DB)
        _sql(
            operator,
            "CREATE TRIGGER frame_a_keyed_board AFTER UPDATE ON frame_a_keyed FOR EACH ROW "
            "UPDATE status_board SET n = n + 1 WHERE frame_id = NEW.frame_id",
        )
        frame_a = _connect(port, "frame_a", PASSWORD, DB)

        # The frame lacks UPDATE on status_board, so its own-row UPDATE is refused outright.
        assert "status_board" in _refused(
            frame_a, "UPDATE frame_a_keyed SET frame_id = 'b' WHERE id = 1"
        )
        _sql(root, "REVOKE INSERT, UPDATE ON hq_live.* FROM op@'%'")
        assert "status_board" in _refused(frame_a, "UPDATE frame_a_keyed SET id = 1 WHERE id = 1")
        _sql(root, "DROP USER op@'%'")
        assert "status_board" in _refused(frame_a, "UPDATE frame_a_keyed SET id = 1 WHERE id = 1")
        assert _sql(root, "SELECT n FROM status_board ORDER BY frame_id") == ((0,), (0,))

        # Only once the frame itself may write the shared table does NEW.* retargeting reach
        # frame b's row: no reach beyond the frame's own grant.
        _sql(root, f"GRANT SELECT, UPDATE ON {DB}.status_board TO frame_a@'%'")
        retarget = _connect(port, "frame_a", PASSWORD, DB)
        _sql(retarget, "UPDATE frame_a_keyed SET frame_id = 'b' WHERE id = 1")
        assert _sql(root, "SELECT frame_id, n FROM status_board ORDER BY frame_id") == (
            ("a", 0),
            ("b", 1),
        )
        # Triggers are versioned schema (dolt_schemas), unlike the ignored session rows.
        assert ("dolt_schemas",) in _sql(root, "SELECT table_name FROM dolt_status")


def test_host_pinned_principal_refuses_a_leaked_password_from_another_address(tmp_path):
    """Mitigation for the one new exposure: a SQL password leaked without the frame host.

    With the receiver, a stolen SQL password alone cannot forge liveness (the envelope needs the
    signing key).  Without it, the password is the liveness credential, so pin the account to
    the frame's address.  Loopback aliases stand in for two hosts.
    """
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.2", 0))
    except OSError:
        pytest.skip("no 127.0.0.2 loopback alias on this platform")
    with _isolated_server(tmp_path) as (port, _root):
        root = _provision(port)
        _sql(root, f"CREATE USER frame_p@'127.0.0.2' IDENTIFIED BY '{PASSWORD}'")
        _sql(root, f"GRANT SELECT, UPDATE ON {DB}.frame_a_session TO frame_p@'127.0.0.2'")
        with pytest.raises(pymysql.MySQLError) as refused:
            _connect(port, "frame_p", PASSWORD, DB, bind_address="127.0.0.1")
        assert refused.value.args[0] == 1045  # access denied
        pinned = _connect(port, "frame_p", PASSWORD, DB, bind_address="127.0.0.2")
        _sql(pinned, "UPDATE frame_a_session SET epoch = epoch WHERE id = 1")


def test_any_frame_principal_can_set_server_globals_and_persist_config(tmp_path):
    """Availability threat shared by the receiver design: globals are not privilege-checked."""
    with _isolated_server(tmp_path) as (port, dolt_root):
        root = _provision(port)
        frame_a = _connect(port, "frame_a", PASSWORD, DB)
        accepted = []
        for statement in (
            "SET GLOBAL max_connections = 1",
            "SET GLOBAL read_only = 1",
            "SET GLOBAL dolt_force_transaction_commit = 1",
            "SET GLOBAL dolt_transaction_commit = 1",
            "SET GLOBAL time_zone = '+14:00'",
            "SET GLOBAL sql_mode = ''",
        ):
            _sql(frame_a, statement)
            accepted.append(statement)
        observed = _sql(
            root,
            "SELECT @@global.max_connections, @@global.read_only, "
            "@@global.dolt_force_transaction_commit, @@global.dolt_transaction_commit, "
            "@@global.time_zone",
        )[0]
        assert observed == (1, 1, 1, 1, "+14:00")

        # SET PERSIST writes the server user's global Dolt config: here, the isolated root only.
        config = dolt_root / ".dolt" / "config_global.json"
        _sql(frame_a, "SET PERSIST max_connections = 777")
        assert json.loads(config.read_text())["sqlserver.global.max_connections"] == "777"

        for statement in (
            "SET GLOBAL max_connections = 1000",
            "SET GLOBAL read_only = 0",
            "SET GLOBAL dolt_force_transaction_commit = 0",
            "SET GLOBAL dolt_transaction_commit = 0",
            "SET GLOBAL time_zone = 'SYSTEM'",
        ):
            _sql(root, statement)


def test_session_rows_leave_no_history_and_renewal_latency_is_small(tmp_path):
    with _isolated_server(tmp_path) as (port, _root):
        root = _provision(port)
        frame_a = _connect(port, "frame_a", PASSWORD, DB)
        head = _sql(root, "SELECT DOLT_HASHOF('HEAD')")[0][0]

        persistent = []
        for _ in range(200):
            started = time.perf_counter()
            _sql(frame_a, "UPDATE frame_a_session SET epoch = epoch WHERE id = 1")
            persistent.append(time.perf_counter() - started)
        reconnect = []
        for _ in range(50):
            started = time.perf_counter()
            connection = _connect(port, "frame_a", PASSWORD, DB)
            _sql(connection, "UPDATE frame_a_session SET epoch = epoch WHERE id = 1")
            connection.close()
            reconnect.append(time.perf_counter() - started)

        def summary(samples):
            ordered = sorted(samples)
            return {
                "n": len(ordered),
                "p50_ms": round(statistics.median(ordered) * 1000, 2),
                "p95_ms": round(ordered[int(len(ordered) * 0.95) - 1] * 1000, 2),
                "max_ms": round(ordered[-1] * 1000, 2),
            }

        print(
            "RENEWAL_LATENCY "
            + json.dumps({"persistent": summary(persistent), "reconnect": summary(reconnect)})
        )
        # Loose bound: a renewal is a single-row autocommit UPDATE, far below any 300 s TTL.
        assert statistics.median(persistent) < 0.5
        assert statistics.median(reconnect) < 1.0

        # 250 renewals: no Dolt commit, no staged or unstaged change, and an operator's
        # `-Am` commit cannot sweep the ignored rows into history.
        assert _sql(root, "SELECT DOLT_HASHOF('HEAD')")[0][0] == head
        pending = _sql(root, "SELECT table_name FROM dolt_status WHERE table_name LIKE 'frame_%'")
        assert pending == ()
        _sql(root, "UPDATE hq_grant SET evidence_ttl_ms = evidence_ttl_ms WHERE frame_id = 'a'")
        _sql(root, "INSERT INTO hq_grant VALUES ('c','pending',1,'x','factory',1,1)")
        _sql(root, "CALL DOLT_COMMIT('-Am', 'operator grant change')")
        changed = {
            row[0]
            for row in _sql(
                root, "SELECT table_name FROM dolt_diff WHERE commit_hash = DOLT_HASHOF('HEAD')"
            )
        }
        assert changed and not any(name.startswith("frame_") for name in changed), changed


def test_scenario_10_slow_probe_keeps_session_fresh_and_expired_evidence_alone_blocks(tmp_path):
    """Scenario 10: a conformance probe slower than the session TTL.

    Scaled-down clock: session TTL 3 s, renewal every 0.3 s, evidence TTL 1.5 s, probe 6 s.
    Decoupled: the session stays fresh throughout; once evidence expires, eligibility fails on
    ``evidence_unexpired`` alone, and recovers when the probe publishes.  Coupled counterfactual
    (renewal waits for the measurement, as a HeartbeatLease with embedded conformance does):
    the same probe lets the session go stale.
    """
    session_ttl_ms, evidence_ttl_ms, renew_s, probe_s = 3000, 1500, 0.3, 6.0
    context = process_context()
    with _isolated_server(tmp_path) as (port, _root):
        root = _provision(port, session_ttl_ms=session_ttl_ms, evidence_ttl_ms=evidence_ttl_ms)
        reader = _connect(port, "reader", PASSWORD, DB)
        frame_a = _connect(port, "frame_a", PASSWORD, DB)
        _sql(
            frame_a,
            "UPDATE frame_a_evidence SET release_digest=%s, profile='factory', "
            "status='conformant' WHERE id = 1",
            ("sha256:" + "1" * 64,),
        )

        stop, errors = context.Event(), context.Queue()
        renewers = [
            context.Process(target=_renewer, args=(port, frame, renew_s, stop, errors))
            for frame in ("a", "b")
        ]
        for process in renewers:
            process.start()

        # Concurrent operator config commits must not disturb renewals or reads.
        operator_stop = threading.Event()
        commit_errors = []

        def operator_commits():
            connection = _connect(port, database=DB)
            n = 0
            while not operator_stop.is_set():
                n += 1
                try:
                    _sql(
                        connection,
                        "UPDATE hq_grant SET profile = 'factory' WHERE frame_id = 'b'",
                    )
                    _sql(
                        connection,
                        "INSERT INTO hq_grant VALUES (%s,'pending',1,'x','p',1,1)",
                        (f"op-{n}",),
                    )
                    _sql(connection, "CALL DOLT_COMMIT('-am', 'operator change')")
                except pymysql.MySQLError as exc:
                    commit_errors.append(repr(exc))
                operator_stop.wait(0.2)
            connection.close()

        committer = threading.Thread(target=operator_commits)
        committer.start()
        try:
            # Wait for both spawned renewers to have renewed at least once after this point.
            mark = _sql(root, "SELECT UTC_TIMESTAMP(6)")[0][0]
            deadline = time.monotonic() + 60
            while not all(
                _sql(root, f"SELECT renewed_at > %s FROM frame_{frame}_session", (mark,))[0][0]
                for frame in ("a", "b")
            ):
                assert time.monotonic() < deadline, "renewers never renewed"
                time.sleep(0.1)
            _sql(
                frame_a,
                "UPDATE frame_a_evidence SET status='conformant' WHERE id = 1",
            )
            assert _blocked_by(_decide(reader, "a")) == set()
            # Frame b renews but never measured: live, yet blocked by its evidence alone.
            blocked_b = _blocked_by(_decide(reader, "b"))
            assert {"evidence_pass", "release_matches"} <= blocked_b
            assert blocked_b <= {"evidence_pass", "release_matches", "evidence_unexpired"}

            started, finished = context.Event(), context.Event()
            probe = context.Process(
                target=_conformance_probe, args=(port, "a", probe_s, started, finished)
            )
            probe.start()
            assert started.wait(30)
            samples = []
            while not finished.is_set():
                samples.append(_decide(reader, "a"))
                time.sleep(0.1)
            probe.join(30)
            assert probe.exitcode == 0
            after = _decide(reader, "a")

            assert len(samples) > 20
            assert all(sample["session_fresh"] for sample in samples), samples
            blocked = [_blocked_by(sample) for sample in samples]
            assert {"evidence_unexpired"} in blocked  # expired evidence alone blocks
            assert all(reasons <= {"evidence_unexpired"} for reasons in blocked), blocked
            assert _blocked_by(after) == set()  # republished evidence restores eligibility
            print(
                "SCENARIO_10_DECOUPLED "
                + json.dumps(
                    {
                        "samples": len(samples),
                        "session_fresh": sum(s["session_fresh"] for s in samples),
                        "blocked_by_evidence_only": blocked.count({"evidence_unexpired"}),
                        "eligible": blocked.count(set()),
                    }
                )
            )
        finally:
            operator_stop.set()
            committer.join(30)
            stop.set()
            for process in renewers:
                process.join(30)
        renewal_errors = []
        while True:
            try:
                renewal_errors.append(errors.get(timeout=0.2))
            except queue.Empty:
                break
        assert renewal_errors == []
        assert commit_errors == []
        assert all(process.exitcode == 0 for process in renewers)

        # Coupled counterfactual: no independent renewer, renewal waits on the measurement.
        _sql(frame_a, "UPDATE frame_a_session SET epoch = epoch WHERE id = 1")
        _sql(root, "UPDATE hq_grant SET evidence_ttl_ms = 60000 WHERE frame_id = 'a'")
        started, finished = context.Event(), context.Event()
        coupled = context.Process(
            target=_coupled_heartbeat, args=(port, "a", probe_s, started, finished)
        )
        coupled.start()
        assert started.wait(30)
        stale = []
        while not finished.is_set():
            stale.append(_blocked_by(_decide(reader, "a")))
            time.sleep(0.1)
        coupled.join(30)
        assert coupled.exitcode == 0
        assert {"session_fresh"} in stale  # a healthy frame expired by its own probe
        print(
            "SCENARIO_10_COUPLED "
            + json.dumps(
                {
                    "samples": len(stale),
                    "blocked_by_session_only": stale.count({"session_fresh"}),
                    "renewer_errors": len(renewal_errors),
                    "operator_commit_errors": len(commit_errors),
                }
            )
        )
