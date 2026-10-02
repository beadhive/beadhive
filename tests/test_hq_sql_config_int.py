"""Actual pinned Dolt/TLS publication boundary for the application SQL store."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from beadhive.hq_sql_config import (
    PublicationUnknown,
    SqlConfigError,
    SqlFleetConfigRevisionStore,
    _digest,
)
from beadhive.hq_sql_transport import SqlTransportError
from beadhive.modules.config.domain.ports import FleetConfigDocument
from harness.world import dolt_server_slot, free_port

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]


class _Broker:
    def get(self, reference, *, deadline):
        assert reference == {"config_path": "/fixture/fnox.toml", "profile": "test", "key": "SQL"}
        assert deadline > time.monotonic()
        return "fixture-secret"


def _openssl(tmp_path, *args):
    subprocess.run(
        ["openssl", *args],
        cwd=tmp_path,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
    )


def _certificates(tmp_path):
    _openssl(
        tmp_path,
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        "ca.key",
        "-out",
        "ca.crt",
        "-days",
        "2",
        "-subj",
        "/CN=fixture-ca",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
    )
    _openssl(
        tmp_path,
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        "server.key",
        "-out",
        "server.csr",
        "-subj",
        "/CN=localhost",
    )
    (tmp_path / "server.ext").write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
    )
    _openssl(
        tmp_path,
        "x509",
        "-req",
        "-in",
        "server.csr",
        "-CA",
        "ca.crt",
        "-CAkey",
        "ca.key",
        "-CAcreateserial",
        "-out",
        "server.crt",
        "-days",
        "2",
        "-extfile",
        "server.ext",
    )


def _negative_server_certificates(tmp_path):
    (tmp_path / "wrong-name.ext").write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:wrong.fixture.invalid\n"
    )
    _openssl(
        tmp_path, "x509", "-req", "-in", "server.csr", "-CA", "ca.crt",
        "-CAkey", "ca.key", "-out", "wrong-name.crt", "-days", "2",
        "-extfile", "wrong-name.ext",
    )
    (tmp_path / "ca-index").write_text("")
    (tmp_path / "ca-serial").write_text("1000\n")
    (tmp_path / "issued").mkdir()
    (tmp_path / "ca.conf").write_text(
        "[ca]\ndefault_ca=fixture\n[fixture]\n"
        f"database={tmp_path / 'ca-index'}\nserial={tmp_path / 'ca-serial'}\n"
        f"new_certs_dir={tmp_path / 'issued'}\ncertificate={tmp_path / 'ca.crt'}\n"
        f"private_key={tmp_path / 'ca.key'}\ndefault_md=sha256\n"
        "policy=policy_any\n[policy_any]\ncommonName=supplied\n"
    )
    (tmp_path / "expired.ext").write_text(
        "[server_cert]\n" + (tmp_path / "server.ext").read_text()
    )
    now = datetime.now(UTC)
    _openssl(
        tmp_path, "ca", "-batch", "-config", "ca.conf", "-in", "server.csr",
        "-out", "expired.crt",
        "-startdate", (now - timedelta(days=2)).strftime("%y%m%d%H%M%SZ"),
        "-enddate", (now - timedelta(days=1)).strftime("%y%m%d%H%M%SZ"),
        "-extensions", "server_cert", "-extfile", "expired.ext",
    )


def _cli(tmp_path, port, query):
    result = subprocess.run(
        [
            shutil.which("dolt"),
            "--host=127.0.0.1",
            f"--port={port}",
            "--no-tls",
            "--user=root",
            "sql",
            "-q",
            query,
        ],
        cwd=tmp_path,
        env={**os.environ, "DOLT_CLI_PASSWORD": ""},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _binding(tmp_path, port, user):
    return {
        "host": "127.0.0.1",
        "port": port,
        "database": "beadhive_hq_config",
        "user": user,
        "server_name": "127.0.0.1",
        "ca_file": str(tmp_path / "ca.crt"),
        "credential": {"config_path": "/fixture/fnox.toml", "profile": "test", "key": "SQL"},
        "connect_timeout": 3,
        "read_timeout": 3,
        "write_timeout": 3,
        "operation_timeout": 20,
    }


def _read_packet(sock):
    def exact(size):
        body = bytearray()
        while len(body) < size:
            chunk = sock.recv(size - len(body))
            if not chunk:
                break
            body.extend(chunk)
        return bytes(body)

    header = exact(4)
    if len(header) != 4:
        return header
    return header + exact(int.from_bytes(header[:3], "little"))


def _missing_client_ssl_relay(server_port):
    """One owned greeting edit; record only whether any client byte escaped."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    port = listener.getsockname()[1]
    evidence = {}

    def serve():
        try:
            with listener.accept()[0] as client:
                with socket.create_connection(("127.0.0.1", server_port), timeout=3) as upstream:
                    client.settimeout(3)
                    upstream.settimeout(3)
                    greeting = bytearray(_read_packet(upstream))
                    payload = greeting[4:]
                    capability_offset = payload.index(0, 1) + 14
                    capabilities = int.from_bytes(
                        payload[capability_offset:capability_offset + 2], "little"
                    )
                    evidence["server_advertised_tls"] = bool(capabilities & 0x800)
                    greeting[4 + capability_offset:4 + capability_offset + 2] = (
                        capabilities & ~0x800
                    ).to_bytes(2, "little")
                    client.sendall(greeting)
                    evidence["client_bytes_after_greeting"] = len(client.recv(1))
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return port, thread, evidence


def _probe_installed_application(executable, binding, *, expected, tmp_path):
    """Run the installed application closure, not this test process's imports."""
    probe = (
        "import json,sys,time\n"
        "from beadhive.hq_sql_transport import connect,SqlTransportError\n"
        "class Broker:\n"
        " def get(self,reference,*,deadline): return 'fixture-secret'\n"
        "try:\n"
        " c=connect(json.loads(sys.argv[1]),Broker(),deadline=time.monotonic()+5)\n"
        " with c.cursor() as cur:\n"
        "  cur.execute('SELECT DOLT_VERSION()')\n"
        "  version=cur.fetchone()[0]\n"
        " c.close()\n"
        " print('valid:'+version)\n"
        "except SqlTransportError:\n"
        " print('denied')\n"
    )
    result = subprocess.run(
        [executable, "-c", probe, json.dumps(binding)],
        cwd=tmp_path,
        env={**os.environ, "PEX_INTERPRETER": "1", "PEX_ROOT": str(tmp_path / "pex-root")},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_committed_sql_config_publication_and_floor(tmp_path, monkeypatch):
    _certificates(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    port = free_port()
    (tmp_path / "server.yaml").write_text(
        f"data_dir: {data}\nlistener:\n  host: 127.0.0.1\n  port: {port}\n"
        f"  tls_key: {tmp_path / 'server.key'}\n"
        f"  tls_cert: {tmp_path / 'server.crt'}\n"
        "  require_secure_transport: false\n"
    )
    with dolt_server_slot(test_id="sql-config-app-fixture"):
        with (tmp_path / "server.log").open("w") as log:
            server = subprocess.Popen(
                [shutil.which("dolt"), "sql-server", "--config", str(tmp_path / "server.yaml")],
                cwd=tmp_path,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    if server.poll() is not None or time.monotonic() >= deadline:
                        raise AssertionError("owned Dolt fixture did not start") from None
                    time.sleep(0.1)
            _cli(tmp_path, port, "CREATE DATABASE beadhive_hq_config")
            setup = """
                USE beadhive_hq_config;
                CREATE TABLE hq_config_meta (
                  singleton_id TINYINT PRIMARY KEY, schema_version INT NOT NULL,
                  backend_identity VARCHAR(128) NOT NULL, generation VARCHAR(128) NOT NULL,
                  publication_sequence BIGINT UNSIGNED NOT NULL, publication_id CHAR(36) NOT NULL,
                  documents_sha256 CHAR(64) NOT NULL, document_count INT UNSIGNED NOT NULL);
                CREATE TABLE hq_config_documents (
                  path VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY,
                  ordinal INT UNSIGNED NOT NULL UNIQUE, kind VARCHAR(32) NOT NULL,
                  content LONGBLOB NOT NULL, content_sha256 CHAR(64) NOT NULL);
                CREATE TABLE hq_config_publications (
                  publication_id CHAR(36) PRIMARY KEY,
                  publication_sequence BIGINT UNSIGNED NOT NULL UNIQUE,
                  generation VARCHAR(128) NOT NULL, expected_parent_revision VARCHAR(128) NOT NULL,
                  documents_sha256 CHAR(64) NOT NULL, document_count INT UNSIGNED NOT NULL);
                CALL DOLT_ADD('hq_config_meta','hq_config_documents','hq_config_publications');
                CALL DOLT_COMMIT('-m','schema','--author','Fixture <fixture@example.invalid>');
            """
            _cli(tmp_path, port, setup)
            original = (FleetConfigDocument("fleet.yaml", "hq:\n  mode: dolt-server\n"),)
            digest = _digest(original)
            content_hash = hashlib.sha256(original[0].content.encode()).hexdigest()
            seed = f"""
                USE beadhive_hq_config;
                INSERT INTO hq_config_meta VALUES
                (1,1,'fixture-backend','fixture-generation',1,
                 '00000000-0000-0000-0000-000000000001','{digest}',1);
                INSERT INTO hq_config_documents VALUES
                ('fleet.yaml',0,'fleet','hq:\\n  mode: dolt-server\\n','{content_hash}');
                INSERT INTO hq_config_publications VALUES
                ('00000000-0000-0000-0000-000000000001',1,'fixture-generation',
                 DOLT_HASHOF('HEAD'),'{digest}',1);
                CALL DOLT_ADD('hq_config_meta','hq_config_documents','hq_config_publications');
                CALL DOLT_COMMIT('-m','seed','--author','Fixture <fixture@example.invalid>');
                CREATE USER 'reader'@'localhost' IDENTIFIED BY 'fixture-secret';
                CREATE USER 'publisher'@'localhost' IDENTIFIED BY 'fixture-secret';
                GRANT SELECT ON beadhive_hq_config.hq_config_meta TO 'reader'@'localhost';
                GRANT SELECT ON beadhive_hq_config.hq_config_documents TO 'reader'@'localhost';
                GRANT SELECT ON beadhive_hq_config.hq_config_publications TO 'reader'@'localhost';
                GRANT SELECT,INSERT,UPDATE ON beadhive_hq_config.hq_config_meta
                  TO 'publisher'@'localhost';
                GRANT SELECT,INSERT,UPDATE,DELETE ON beadhive_hq_config.hq_config_documents
                  TO 'publisher'@'localhost';
                GRANT SELECT,INSERT ON beadhive_hq_config.hq_config_publications
                  TO 'publisher'@'localhost';
                GRANT SELECT ON beadhive_hq_config.dolt_status TO 'publisher'@'localhost';
                GRANT EXECUTE ON PROCEDURE beadhive_hq_config.dolt_add TO 'publisher'@'localhost';
                GRANT EXECUTE ON PROCEDURE beadhive_hq_config.dolt_commit
                  TO 'publisher'@'localhost';
            """
            _cli(tmp_path, port, seed)
            import pymysql

            root = pymysql.connect(
                host="127.0.0.1", port=port, user="root", database="beadhive_hq_config"
            )
            with root.cursor() as cursor:
                cursor.execute("SELECT DOLT_HASHOF('HEAD')")
                initial = cursor.fetchone()[0]
            root.close()
            settings = {
                "reader": _binding(tmp_path, port, "reader"),
                "publisher": _binding(tmp_path, port, "publisher"),
                "cache_ttl": 10,
                "backend_identity": "fixture-backend",
                "generation": "fixture-generation",
                "minimum_sequence": 1,
                "initial_revision": initial,
                "floor_path": str(tmp_path / "floor.json"),
            }
            from beadhive.hq_sql_transport import connect

            hostname_binding = {
                **settings["reader"], "host": "localhost", "server_name": "localhost"
            }
            hostname_connection = connect(
                hostname_binding, _Broker(), deadline=time.monotonic() + 5
            )
            try:
                with hostname_connection.cursor() as cursor:
                    cursor.execute("SELECT CURRENT_USER(),DATABASE(),DOLT_VERSION()")
                    assert cursor.fetchone()[1:] == ("beadhive_hq_config", "2.3.5")
            finally:
                hostname_connection.close()
            artifacts = tuple(
                artifact
                for name in ("BEADHIVE_SQL_PROBE_PEX", "BEADHIVE_SQL_PROBE_NIX_PYTHON")
                if (artifact := os.environ.get(name))
            )
            for artifact in artifacts:
                _probe_installed_application(
                    artifact, hostname_binding, expected="valid:2.3.5", tmp_path=tmp_path
                )
            relay_port, relay_thread, relay_evidence = _missing_client_ssl_relay(port)
            try:
                without_tls = {**settings["reader"], "port": relay_port}
                with pytest.raises(SqlTransportError, match="before authentication"):
                    connect(without_tls, _Broker(), deadline=time.monotonic() + 4)
            finally:
                relay_thread.join(timeout=5)
            assert not relay_thread.is_alive()
            assert relay_evidence == {
                "server_advertised_tls": True,
                "client_bytes_after_greeting": 0,
            }
            for artifact in artifacts:
                relay_port, relay_thread, relay_evidence = _missing_client_ssl_relay(port)
                try:
                    _probe_installed_application(
                        artifact, {**settings["reader"], "port": relay_port},
                        expected="denied", tmp_path=tmp_path,
                    )
                finally:
                    relay_thread.join(timeout=5)
                assert not relay_thread.is_alive()
                assert relay_evidence["client_bytes_after_greeting"] == 0
            _openssl(
                tmp_path, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", "wrong-ca.key", "-out", "wrong-ca.crt", "-days", "2",
                "-subj", "/CN=untrusted-fixture-ca",
            )
            with pytest.raises(SqlTransportError, match="verified SQL connection unavailable"):
                connect(
                    {**settings["reader"], "ca_file": str(tmp_path / "wrong-ca.crt")},
                    _Broker(), deadline=time.monotonic() + 4,
                )
            for artifact in artifacts:
                _probe_installed_application(
                    artifact,
                    {**settings["reader"], "ca_file": str(tmp_path / "wrong-ca.crt")},
                    expected="denied", tmp_path=tmp_path,
                )
            store = SqlFleetConfigRevisionStore(settings, broker=_Broker())
            loaded = store.load_snapshot()
            assert loaded.commit_revision == initial
            assert loaded.documents == original
            updated = (
                FleetConfigDocument(
                    "fleet.yaml", "hq:\n  mode: dolt-server\n  admission_policy: manual\n"
                ),
                FleetConfigDocument(
                    "workspace.toml",
                    "[[provider]]\ntype = 'github'\nname = 'first'\n"
                    "[provider.extension]\nkey = 'one'\n",
                ),
                FleetConfigDocument(
                    "workspace-extra.toml",
                    "[[provider]]\ntype = 'gitlab'\nname = 'second'\n"
                    "[provider.extension]\nkey = 'two'\n",
                ),
            )
            published = store.publish_snapshot(updated, expected_revision=initial)
            assert published.documents == updated
            assert published.commit_revision != initial
            restarted = SqlFleetConfigRevisionStore(settings, broker=_Broker())
            assert restarted.load_snapshot().commit_revision == published.commit_revision
            with pytest.raises(SqlConfigError, match="expected configuration revision changed"):
                restarted.publish_snapshot(original, expected_revision=initial)
            with pytest.raises(SqlConfigError, match="no longer current"):
                restarted.load_snapshot(revision=initial)
            with monkeypatch.context() as patch:

                def lose_readback(*, revision=None, deadline=None):
                    raise SqlConfigError("simulated lost post-COMMIT acknowledgment")

                patch.setattr(restarted, "load_snapshot", lose_readback)
                with pytest.raises(PublicationUnknown) as uncertain:
                    restarted.publish_snapshot(
                        original, expected_revision=published.commit_revision
                    )
            unknown = uncertain.value
            assert unknown.expected_revision == published.commit_revision
            after_unknown = SqlFleetConfigRevisionStore(settings, broker=_Broker())
            recovered = after_unknown.recover_publication(
                unknown.publication_id, expected_revision=published.commit_revision
            )
            assert recovered.publication_id == unknown.publication_id
            assert recovered.expected_revision == published.commit_revision
            assert recovered.publication_sequence == 3
            assert recovered.documents_sha256 == _digest(original)
            assert recovered.observed_head == after_unknown.load_snapshot().commit_revision
            with pytest.raises(SqlConfigError, match="identity or parent mismatch"):
                after_unknown.recover_publication(unknown.publication_id, expected_revision=initial)
            assert (
                after_unknown.recover_publication(
                    "00000000-0000-0000-0000-000000000099",
                    expected_revision=published.commit_revision,
                )
                is None
            )
            race_parent = recovered.observed_head
            barrier = threading.Barrier(2)
            thread_state = threading.local()
            original_head = SqlFleetConfigRevisionStore._head

            def shared_original_head(cursor):
                head = original_head(cursor)
                if not getattr(thread_state, "captured", False):
                    thread_state.captured = True
                    barrier.wait(timeout=10)
                return head

            def competing_publish(index):
                race_settings = {
                    **settings,
                    "floor_path": str(tmp_path / f"race-floor-{index}.json"),
                }
                candidate = (
                    FleetConfigDocument(
                        "fleet.yaml", f"hq:\n  mode: dolt-server\n  race_winner: {index}\n"
                    ),
                )
                publisher = SqlFleetConfigRevisionStore(race_settings, broker=_Broker())
                try:
                    return publisher.publish_snapshot(candidate, expected_revision=race_parent)
                except SqlConfigError as exc:
                    return exc

            with monkeypatch.context() as patch:
                patch.setattr(
                    SqlFleetConfigRevisionStore, "_head", staticmethod(shared_original_head)
                )
                with ThreadPoolExecutor(max_workers=2) as pool:
                    outcomes = list(pool.map(competing_publish, (1, 2)))
            winners = [item for item in outcomes if not isinstance(item, SqlConfigError)]
            losers = [item for item in outcomes if isinstance(item, SqlConfigError)]
            assert len(winners) == len(losers) == 1
            assert after_unknown.load_snapshot().documents == winners[0].documents
            locked_settings = {
                **settings,
                "reader": {**settings["reader"], "operation_timeout": 1},
            }
            with (tmp_path / "floor.json.lock").open("a+b") as floor_lock:
                fcntl.flock(floor_lock, fcntl.LOCK_EX)
                began = time.monotonic()
                with pytest.raises(SqlConfigError, match="floor custody wait exceeded deadline"):
                    SqlFleetConfigRevisionStore(
                        locked_settings, broker=_Broker()
                    ).load_snapshot()
                assert time.monotonic() - began < 1.5
            _cli(
                tmp_path,
                port,
                "USE beadhive_hq_config; UPDATE hq_config_documents "
                "SET content='unpublished dirty bytes' WHERE path='fleet.yaml';",
            )
            assert restarted.load_snapshot().documents == winners[0].documents
            with pytest.raises(SqlConfigError, match="working tree is dirty"):
                restarted.publish_snapshot(updated, expected_revision=winners[0].commit_revision)
            # Schema allowlisting is checked against the actual committed tree,
            # not just the rows returned by the three known tables.
            _cli(tmp_path, port, "USE beadhive_hq_config; CALL DOLT_RESET('--hard')")
            _cli(
                tmp_path,
                port,
                "USE beadhive_hq_config; CREATE TABLE forbidden_extra (id INT PRIMARY KEY); "
                "CALL DOLT_ADD('forbidden_extra'); "
                "CALL DOLT_COMMIT('-m','fixture extra table','--author',"
                "'Fixture <fixture@example.invalid>')",
            )
            root = pymysql.connect(
                host="127.0.0.1", port=port, user="root", database="beadhive_hq_config"
            )
            with root.cursor() as cursor:
                cursor.execute("SELECT DOLT_HASHOF('HEAD')")
                extra_head = cursor.fetchone()[0]
            root.close()
            extra_settings = {
                **settings,
                "floor_path": str(tmp_path / "fresh-extra-floor.json"),
                "initial_revision": extra_head,
                "minimum_sequence": 4,
            }
            extra_store = SqlFleetConfigRevisionStore(extra_settings, broker=_Broker())
            with pytest.raises(SqlConfigError, match="schema table allowlist changed"):
                extra_store.publish_snapshot(updated, expected_revision=extra_head)

            # A newly constructed store must honor the durable HOST floor after
            # an operator rewinds this isolated test branch to an older commit.
            _cli(
                tmp_path,
                port,
                f"USE beadhive_hq_config; CALL DOLT_RESET('--hard','{initial}')",
            )
            with pytest.raises(SqlConfigError, match="rollback"):
                SqlFleetConfigRevisionStore(settings, broker=_Broker()).load_snapshot()
            _negative_server_certificates(tmp_path)
            for certificate in ("wrong-name", "expired"):
                server.terminate()
                server.wait(timeout=10)
                with socket.socket() as check:
                    check.settimeout(0.2)
                    assert check.connect_ex(("127.0.0.1", port)) != 0
                (tmp_path / "server.yaml").write_text(
                    f"data_dir: {data}\nlistener:\n  host: 127.0.0.1\n  port: {port}\n"
                    f"  tls_key: {tmp_path / 'server.key'}\n"
                    f"  tls_cert: {tmp_path / (certificate + '.crt')}\n"
                    "  require_secure_transport: false\n"
                )
                with (tmp_path / f"server-{certificate}.log").open("w") as log:
                    server = subprocess.Popen(
                        [shutil.which("dolt"), "sql-server", "--config",
                         str(tmp_path / "server.yaml")],
                        cwd=tmp_path, stdout=log, stderr=subprocess.STDOUT,
                    )
                started_by = time.monotonic() + 10
                while True:
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        if server.poll() is not None or time.monotonic() >= started_by:
                            raise AssertionError("negative TLS fixture server failed") from None
                        time.sleep(0.05)
                with pytest.raises(SqlTransportError, match="verified SQL connection unavailable"):
                    connect(settings["reader"], _Broker(), deadline=time.monotonic() + 4)
                for artifact in artifacts:
                    _probe_installed_application(
                        artifact, settings["reader"], expected="denied", tmp_path=tmp_path
                    )
        finally:
            server.terminate()
            server.wait(timeout=10)
            with socket.socket() as check:
                check.settimeout(0.2)
                assert check.connect_ex(("127.0.0.1", port)) != 0
