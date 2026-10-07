"""Session/evidence rows against a real Dolt 2.3.5 server (bh-owqdg, P-M9; ADR §5).

One owned, TLS-capable ``dolt sql-server`` with its own ``DOLT_ROOT_PATH``/``HOME`` under
``tmp_path`` (never a live HQ or hive). Proves the acceptance rows that need a server:

* DDL and provisioning match ADR §5; a frame cannot write another frame's tables or a second
  row, and its timestamps are server-stamped;
* the renewal is one UPDATE per tick and a stalled conformance job does not delay it;
* eligibility is one statement with the named predicates and the stamps a claim records;
* the provisioning check refuses a non-pinned or non-TLS account and a co-hosted hive database.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pymysql
import pytest

from beadhive.hq_sql_runtime_schema import (
    COMMITTED_SCHEMA,
    PROTECTED_LIVE_SCHEMA,
    evidence_table,
    session_table,
)
from beadhive.hq_sql_session import (
    RENEW_SQL,
    EvidenceReport,
    LivenessPolicy,
    NotSwitched,
    SessionError,
    SessionRenewer,
    incarnation_switch,
    publish_evidence,
    read_eligibility,
)
from beadhive.hq_sql_session_provision import (
    check_provisioning,
    provision_incarnation,
    provision_liveness_schema,
)
from harness.world import dolt_server_slot, free_port

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]

DB = "beadhive_hq_runtime"
PASSWORD = "fixture-only"
RELEASE = "sha256:" + "1" * 64
REPORT = "sha256:" + "2" * 64


def _openssl(directory, *args):
    subprocess.run(
        ["openssl", *args],
        cwd=directory,
        check=True,
        timeout=20,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _certs(directory: Path) -> None:
    _openssl(
        directory, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key",
        "-out", "ca.crt", "-days", "2", "-subj", "/CN=fixture-ca",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
    )  # fmt: skip
    _openssl(
        directory, "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key",
        "-out", "server.csr", "-subj", "/CN=localhost",
    )  # fmt: skip
    (directory / "server.ext").write_text(
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
    )
    _openssl(
        directory, "x509", "-req", "-in", "server.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
        "-CAcreateserial", "-out", "server.crt", "-days", "2", "-extfile", "server.ext",
    )  # fmt: skip


@contextmanager
def _server(tmp_path: Path):
    dolt = shutil.which("dolt")
    if dolt is None or shutil.which("openssl") is None:
        pytest.skip("dolt or openssl unavailable")
    _certs(tmp_path)
    root, home, data = tmp_path / "dolt-root", tmp_path / "home", tmp_path / "data"
    (root / ".dolt").mkdir(parents=True)
    home.mkdir()
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
        f"  tls_key: {tmp_path / 'server.key'}\n  tls_cert: {tmp_path / 'server.crt'}\n"
        "  require_secure_transport: false\n"
    )
    env = {**os.environ, "DOLT_ROOT_PATH": str(root), "HOME": str(home)}
    with dolt_server_slot(test_id="hq-sql-session"):
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
            yield port
        finally:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=15)


def _root(port, database=None):
    return pymysql.connect(
        host="127.0.0.1", port=port, user="root", password="", database=database,
        autocommit=True, connect_timeout=5, read_timeout=15, write_timeout=15,
    )  # fmt: skip


def _frame(port, tmp_path, principal, *, tls=True, autocommit=False):
    context = None
    if tls:
        context = ssl.create_default_context(cafile=str(tmp_path / "ca.crt"))
    return pymysql.connect(
        host="127.0.0.1", port=port, user=principal, password=PASSWORD, database=DB,
        ssl=context, autocommit=autocommit, connect_timeout=5, read_timeout=15,
        write_timeout=15,
    )  # fmt: skip


def _sql(connection, statement, args=None):
    with connection.cursor() as cursor:
        cursor.execute(statement, args)
        return cursor.fetchall()


def _refused(connection, statement, args=None) -> str:
    try:
        _sql(connection, statement, args)
    except pymysql.MySQLError as exc:
        connection.rollback()
        return str(exc.args[-1])
    raise AssertionError(f"frame principal was allowed: {statement}")


def _head(connection):
    return _sql(connection, "SELECT DOLT_HASHOF('HEAD')")[0][0]


def _provisioned(port, frames=(("frame_a", 1), ("frame_b", 1)), policy=None):
    """HQ runtime schema, the liveness policy, and each incarnation's tables and account."""
    root = _root(port)
    _sql(root, f"CREATE DATABASE {DB}")
    _sql(root, f"USE {DB}")
    with root.cursor() as cursor:
        for statement in (*COMMITTED_SCHEMA, *PROTECTED_LIVE_SCHEMA):
            cursor.execute(statement)
        for index, (principal, epoch) in enumerate(frames):
            cursor.execute(
                "INSERT INTO hq_principal_registry VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    principal,
                    f"frame-{index}",
                    f"host-{index}",
                    f"vm-{index}",
                    epoch,
                    session_table(principal, epoch),
                    f"SHA256:{index}",
                ),
            )
        cursor.execute(
            "INSERT INTO hq_live_hive_leases VALUES ('bh', %s, %s, %s, %s)",
            (
                "a" * 64,
                json.dumps({"authority": {}, "lease": {"host_id": "host-0"}}),
                "0" * 36,
                "b" * 64,
            ),
        )
        cursor.execute("CALL DOLT_ADD('hq_principal_registry')")
        cursor.execute("CALL DOLT_COMMIT('-m','registry')")
        assert provision_liveness_schema(cursor, policy) is True
        assert provision_liveness_schema(cursor, policy) is False  # idempotent
        for principal, epoch in frames:
            assert (
                provision_incarnation(
                    cursor,
                    principal,
                    epoch,
                    frame_address="127.0.0.1",
                    password=PASSWORD,
                    database=DB,
                )
                == []
            )
            # The operator's existing read grants for a frame (not repeated by provisioning).
            for table in ("hq_principal_registry", "hq_live_hive_leases"):
                cursor.execute(f"GRANT SELECT ON `{DB}`.`{table}` TO '{principal}'@'127.0.0.1'")
    return root


def _eligibility(connection, principal="frame_a", epoch=1, *, release=RELEASE, prefix="bh"):
    with connection.cursor() as cursor:
        cursor.execute("START TRANSACTION")
        head = _head(connection)
        observation = read_eligibility(
            cursor,
            head=head,
            principal=principal,
            epoch=epoch,
            desired_release_digest=release,
            desired_profile="fixture",
            prefix=prefix,
        )
    connection.rollback()
    return observation


class _Recording:
    """A DB-API connection that records every statement its cursors execute."""

    def __init__(self, connection, log):
        self._connection, self._log = connection, log

    def cursor(self):
        log, inner = self._log, self._connection.cursor()

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                inner.close()
                return False

            def execute(self, sql, params=None):
                log.append(sql)
                return inner.execute(sql, params)

            def __getattr__(self, name):
                return getattr(inner, name)

        return Cursor()

    def __getattr__(self, name):
        return getattr(self._connection, name)


def test_provisioning_isolation_server_stamps_and_eligibility(tmp_path):
    with _server(tmp_path) as port:
        root = _provisioned(port)
        a = _frame(port, tmp_path, "frame_a")
        b_session, b_evidence = session_table("frame_b", 1), evidence_table("frame_b", 1)
        a_session = session_table("frame_a", 1)

        # --- never renewed: provisioning alone does not make a frame live -----------------
        # Read before the frame issues ANY statement against its row (bh-eeyxt): the refused
        # matrix below includes UPDATEs of the row, which fire the stamp trigger.
        before = _eligibility(a)
        assert before.status == "missing" and not before.authenticated_fresh_heartbeat
        assert before.renewed_at == "" and before.age_seconds is None
        assert not before.conformance_pass and not before.release_matches
        assert before.current_hive_lease_holder is True  # placement names host-0

        # --- a frame cannot write another frame's tables, a second row, or the policy -------
        for statement in (
            f"UPDATE {b_session} SET epoch = epoch WHERE id = 1",
            f"UPDATE {b_evidence} SET status = 'conformant' WHERE id = 1",
            f"SELECT * FROM {b_session}",
            f"INSERT INTO {a_session} (id, epoch) VALUES (2, 1)",
            f"DELETE FROM {a_session} WHERE id = 1",
            "UPDATE hq_liveness_policy SET session_ttl_s = 86400",
            "UPDATE hq_principal_registry SET epoch = 9",
            f"DROP TABLE {a_session}",
            f"DROP TRIGGER {a_session}_stamp",
        ):
            assert _refused(a, statement)
        # UPDATE-only on its own single row: moving the row away breaks CHECK (id = 1).
        assert "Check constraint" in _refused(a, f"UPDATE {a_session} SET id = 2 WHERE id = 1")
        assert _sql(root, f"SELECT id FROM {DB}.{a_session}") == ((1,),)

        # --- TLS is required: the same credential without TLS is refused ------------------
        with pytest.raises(pymysql.MySQLError):
            _frame(port, tmp_path, "frame_a", tls=False)

        # --- a frame literal is overwritten by server UTC ---------------------------------
        _sql(a, "SET time_zone = '+14:00'")
        _sql(a, f"UPDATE {a_session} SET renewed_at = '2099-01-01' WHERE id = 1")
        a.commit()
        skew = _sql(
            root,
            f"SELECT ABS(TIMESTAMPDIFF(SECOND, renewed_at, UTC_TIMESTAMP())) FROM {DB}.{a_session}",
        )[0][0]
        assert skew <= 60  # the 2099 literal was overwritten by server UTC
        fresh = _eligibility(a)
        assert fresh.authenticated_fresh_heartbeat and fresh.status == "fresh"
        assert 0 <= fresh.age_seconds < 60 and fresh.session_epoch_matches

        # --- evidence: one UPDATE, server-stamped, compared with the grant ----------------
        route = publish_evidence(
            _frame(port, tmp_path, "frame_a"),
            EvidenceReport(1, "fixture", RELEASE, "fixture", "conformant", REPORT),
            database=DB,
        )
        assert route.evidence == evidence_table("frame_a", 1)
        good = _eligibility(a)
        assert good.conformance_pass and good.release_matches
        assert good.predicates == {
            "authenticated_fresh_heartbeat": True,
            "conformance_pass": True,
            "release_matches": True,
            "current_hive_lease_holder": True,
        }
        stamps = good.stamps()
        assert stamps["session_renewed_at"] and stamps["evidence_measured_at"]
        assert (
            stamps["evidence_digest"].startswith("sha256:") and len(stamps["evidence_digest"]) == 71
        )
        assert not _eligibility(a, release="sha256:" + "9" * 64).release_matches
        assert not _eligibility(a, prefix=None).current_hive_lease_holder
        with pytest.raises(SessionError):
            publish_evidence(
                _frame(port, tmp_path, "frame_a"),
                EvidenceReport(2, "fixture", RELEASE, "fixture", "conformant", REPORT),
                database=DB,
            )

        # --- operator evidence_ttl: expiry is measured_at + TTL, computed by the reader ---
        _sql(root, "UPDATE hq_liveness_policy SET evidence_ttl_s = 1")
        _sql(root, "CALL DOLT_ADD('hq_liveness_policy')")
        _sql(root, "CALL DOLT_COMMIT('-m','shorter evidence ttl')")
        # Wait evidence_ttl_s + 1.1 s, not just past the TTL: Dolt 2.3.5 truncates the
        # fractional seconds of `DATETIME(6) - INTERVAL n SECOND`, so ELIGIBILITY_SQL's cutoff
        # can lag by up to 1 s and keep conformance_pass true a little past the TTL (bh-eeyxt;
        # the product TIMESTAMPDIFF fix is follow-up bead bh-7crof).
        time.sleep(1 + 1.1)
        renewer = SessionRenewer(lambda: _frame(port, tmp_path, "frame_a"), database=DB)
        renewer.tick()
        expired = _eligibility(a)
        assert expired.authenticated_fresh_heartbeat and not expired.conformance_pass
        assert expired.evidence_ttl_s == 1 and expired.evidence_age_seconds >= 1

        # --- the rows leave no history and no dirty working tree --------------------------
        assert _sql(root, "SELECT table_name FROM dolt_status") == ()
        assert check_provisioning(root.cursor(), "frame_a", 1, database=DB) == []

        # --- the switch is table existence: an unprovisioned incarnation is legacy --------
        with root.cursor() as cursor:
            assert incarnation_switch(cursor, "frame_a", 7) is None
            cursor.execute(f"CREATE TABLE {session_table('frame_z', 1)} (id TINYINT PRIMARY KEY)")
            with pytest.raises(SessionError, match="half-provisioned"):
                incarnation_switch(cursor, "frame_z", 1)
        a.close()


def test_renewal_is_one_update_per_tick_and_ignores_a_stalled_conformance_job(tmp_path):
    with _server(tmp_path) as port:
        root = _provisioned(port, frames=(("frame_a", 1),))
        statements: list[str] = []
        renewer = SessionRenewer(
            lambda: _Recording(_frame(port, tmp_path, "frame_a"), statements), database=DB
        )
        renewer.tick()
        statements.clear()
        for _ in range(3):
            renewer.tick()
        # The route is resolved once; every later tick is exactly one UPDATE.
        assert statements == [RENEW_SQL.format(session=session_table("frame_a", 1))] * 3

        # A conformance job stalls mid-publication: it holds an uncommitted evidence UPDATE
        # open on its own connection and never finishes while the renewal timer keeps going.
        stalled, release = threading.Event(), threading.Event()

        def stalled_conformance():
            connection = _frame(port, tmp_path, "frame_a")
            try:
                _sql(connection, "START TRANSACTION")
                _sql(
                    connection,
                    f"UPDATE {evidence_table('frame_a', 1)} SET status = 'conformant' WHERE id = 1",
                )
                stalled.set()
                release.wait(30)
            finally:
                connection.rollback()
                connection.close()

        job = threading.Thread(target=stalled_conformance, daemon=True)
        job.start()
        assert stalled.wait(15)
        stamps, stop, errors = [], threading.Event(), []
        started = renewer.renewals

        def watch(wait_s):
            stamps.append(
                _sql(root, f"SELECT renewed_at FROM {DB}.{session_table('frame_a', 1)}")[0][0]
            )
            return stop.wait(wait_s) or len(stamps) >= 6

        renewer.run(interval_s=0.2, stop=watch, on_error=errors.append)
        release.set()
        job.join(15)
        assert errors == []
        assert renewer.renewals - started == 6
        assert len(set(stamps)) == 6 and stamps == sorted(stamps)
        # The stalled job published nothing.
        assert _sql(root, f"SELECT status FROM {DB}.{evidence_table('frame_a', 1)}") == (
            ("unmeasured",),
        )


def test_renewer_on_a_legacy_incarnation_is_not_switched(tmp_path):
    with _server(tmp_path) as port:
        root = _provisioned(port, frames=(("frame_a", 1),))
        _sql(root, "UPDATE hq_principal_registry SET epoch = 2 WHERE principal = 'frame_a'")
        renewer = SessionRenewer(lambda: _frame(port, tmp_path, "frame_a"), database=DB)
        with pytest.raises(NotSwitched):
            renewer.tick()


def test_provisioning_check_refuses_unpinned_plaintext_accounts_and_cohosted_hives(tmp_path):
    with _server(tmp_path) as port:
        root = _provisioned(port, frames=(("frame_a", 1),))
        with root.cursor() as cursor:
            assert check_provisioning(cursor, "frame_a", 1, database=DB) == []
            # A wildcard-host account for the same principal: a leaked password works anywhere.
            cursor.execute(f"CREATE USER 'frame_a'@'%' IDENTIFIED BY '{PASSWORD}' REQUIRE SSL")
            problems = check_provisioning(cursor, "frame_a", 1, database=DB)
            assert any("not pinned" in problem for problem in problems), problems
            cursor.execute("DROP USER 'frame_a'@'%'")
            # A pinned account that does not require TLS.
            cursor.execute(f"CREATE USER 'frame_a'@'10.0.0.9' IDENTIFIED BY '{PASSWORD}'")
            problems = check_provisioning(cursor, "frame_a", 1, database=DB)
            assert any("does not require TLS" in problem for problem in problems), problems
            cursor.execute("DROP USER 'frame_a'@'10.0.0.9'")
            assert check_provisioning(cursor, "frame_a", 1, database=DB) == []
            # A hive database co-hosted on the HQ server.
            cursor.execute("CREATE DATABASE hive_bh")
            cursor.execute("CREATE TABLE hive_bh.issues (id VARCHAR(64) PRIMARY KEY)")
            problems = check_provisioning(cursor, "frame_a", 1, database=DB)
            assert problems == ["hive database 'hive_bh' is co-hosted on the HQ server"]
            cursor.execute("DROP DATABASE hive_bh")
            # Provisioning itself refuses an unpinned address and a second incarnation write.
            with pytest.raises(SessionError, match="pinned"):
                provision_incarnation(
                    cursor, "frame_c", 1, frame_address="%", password=PASSWORD, database=DB
                )
            with pytest.raises(SessionError, match="fresh-only"):
                provision_incarnation(
                    cursor, "frame_a", 1, frame_address="127.0.0.1", password=PASSWORD,
                    database=DB,
                )  # fmt: skip


def test_incarnation_tables_require_the_committed_ignore_rule_first(tmp_path):
    with _server(tmp_path) as port:
        root = _root(port)
        _sql(root, f"CREATE DATABASE {DB}")
        _sql(root, f"USE {DB}")
        with root.cursor() as cursor:
            with pytest.raises(SessionError, match="ignore rule"):
                provision_incarnation(
                    cursor, "frame_a", 1, frame_address="127.0.0.1", password=PASSWORD,
                    database=DB,
                )  # fmt: skip
            with pytest.raises(SessionError):
                LivenessPolicy(session_ttl_s=0)
