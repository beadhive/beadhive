"""Pinned Dolt fixture for signed committed runtime authority and role custody."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from beadhive import hq_authority_guard as guard
from beadhive.frame_eligibility import EligibilityFacts, eligible
from beadhive.host_heartbeat_core import HeartbeatLease
from beadhive.host_lease_contracts import HostLease, now_stamp
from beadhive.hosts import HostManifest
from beadhive.hq_control_plane import ControlPlaneError, SqlControlPlane
from beadhive.hq_hive_policy import project_hive_policies
from beadhive.hq_sql_config import _digest
from beadhive.hq_sql_runtime_schema import COMMITTED_SCHEMA, PROTECTED_LIVE_SCHEMA, inbox_ddl
from beadhive.hq_sql_signatures import (
    canonical,
    fingerprint,
    sign_authority,
    sign_heartbeat,
    verify_heartbeat,
)
from beadhive.modules.config.domain.ports import FleetConfigDocument, FleetConfigSnapshot
from harness.world import dolt_server_slot, free_port

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]


class Broker:
    def get(self, _reference, *, deadline):
        assert time.monotonic() < deadline
        return "fixture-secret"


def _runtime_commit_hashes(port):
    """Trusted fixture view of versioned history, separate from live SQL rows."""
    import pymysql

    root = pymysql.connect(
        host="127.0.0.1",
        port=port,
        user="root",
        database="beadhive_hq_runtime",
        autocommit=True,
    )
    try:
        with root.cursor() as cursor:
            cursor.execute("SELECT commit_hash FROM dolt_log")
            return tuple(sorted(row[0] for row in cursor.fetchall()))
    finally:
        root.close()


def _openssl(directory, *args):
    subprocess.run(
        ["openssl", *args],
        cwd=directory,
        check=True,
        timeout=20,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _certs(directory):
    _openssl(
        directory,
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
        directory,
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
    (directory / "server.ext").write_text(
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
    )
    _openssl(
        directory,
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


def _cli(directory, port, sql):
    result = subprocess.run(
        [
            shutil.which("dolt"),
            "--host=127.0.0.1",
            f"--port={port}",
            "--no-tls",
            "--user=root",
            "sql",
            "-q",
            sql,
        ],
        cwd=directory,
        env={**os.environ, "DOLT_CLI_PASSWORD": ""},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _binding(directory, port, user, database):
    return {
        "host": "127.0.0.1",
        "port": port,
        "database": database,
        "user": user,
        "server_name": "127.0.0.1",
        "ca_file": str(directory / "ca.crt"),
        "credential": {"config_path": "/fixture/fnox.toml", "profile": "fixture", "key": "SQL"},
        "connect_timeout": 3,
        "read_timeout": 3,
        "write_timeout": 3,
        "operation_timeout": 15,
    }


def test_committed_signed_runtime_authority_and_separate_frame_grants(tmp_path, monkeypatch):
    _certs(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    port = free_port()
    (tmp_path / "server.yaml").write_text(
        f"data_dir: {data}\nlistener:\n  host: 127.0.0.1\n  port: {port}\n"
        f"  tls_key: {tmp_path / 'server.key'}\n"
        f"  tls_cert: {tmp_path / 'server.crt'}\n"
        "  require_secure_transport: false\n"
    )
    with dolt_server_slot(test_id="sql-runtime-authority-fixture"):
        with (tmp_path / "server.log").open("w") as log:
            server = subprocess.Popen(
                [shutil.which("dolt"), "sql-server", "--config", str(tmp_path / "server.yaml")],
                cwd=tmp_path,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        try:
            end = time.monotonic() + 20
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    if server.poll() is not None or time.monotonic() >= end:
                        raise AssertionError("owned runtime Dolt server failed to start") from None
                    time.sleep(0.1)
            _cli(tmp_path, port, "CREATE DATABASE beadhive_hq_runtime")
            _cli(tmp_path, port, "USE beadhive_hq_runtime; " + "; ".join(COMMITTED_SCHEMA))
            _cli(tmp_path, port, "USE beadhive_hq_runtime; " + "; ".join(PROTECTED_LIVE_SCHEMA))
            _cli(tmp_path, port, "USE beadhive_hq_runtime; " + inbox_ddl("frame_a", 1))
            _cli(tmp_path, port, "CREATE DATABASE beadhive_hq_config")
            _cli(
                tmp_path,
                port,
                """
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
                CALL DOLT_COMMIT('-m','config schema','--author',
                  'Fixture <fixture@example.invalid>');
            """,
            )
            fleet_document = FleetConfigDocument(
                "fleet.yaml",
                "hq:\n  mode: dolt-server\nmanaged_repos:\n"
                "- provider: github\n  org: bee\n  repo: hive\n  prefix: bh\n"
                "  frame_policy:\n    config_revision: desired-1\n"
                "    requires: {max_sessions: 1}\n    evict_after_s: 900\n"
                # A second existing hive lets the bound-incarnation proof
                # exercise a fresh ADOPT without evicting the live bh lease.
                "- provider: github\n  org: bee\n  repo: identity\n  prefix: bi\n"
                "  frame_policy:\n    config_revision: desired-1\n"
                "    requires: {max_sessions: 1}\n    evict_after_s: 900\n",
            )
            manifest = HostManifest.model_validate(
                {
                    "frame_id": "frame-1",
                    "host_id": "host-1",
                    "instance_ref": "vm-1",
                    "state": "active",
                    "label": "fixture",
                    "os": "linux",
                    "arch": "x86_64",
                    "role": "executor",
                    "identity": {"kind": "none", "value": ""},
                    "release": {"id": "fixture", "digest": "sha256:" + "1" * 64},
                    "capabilities": {
                        "isolation": "container",
                        "trust_zone": "self-hosted",
                        "arch": "x86_64",
                        "harnesses": ["claude"],
                        "max_sessions": 1,
                    },
                }
            )
            host_document = FleetConfigDocument(
                "hosts/host-1.yaml", json.dumps(manifest.model_dump(mode="json", exclude_none=True))
            )
            manifest2 = manifest.model_copy(
                update={
                    "frame_id": "frame-2",
                    "host_id": "host-2",
                    "instance_ref": "vm-2",
                    "state": "pending",
                    "label": "candidate-two",
                }
            )
            host2_document = FleetConfigDocument(
                "hosts/host-2.yaml",
                json.dumps(manifest2.model_dump(mode="json", exclude_none=True)),
            )
            documents = (fleet_document, host_document, host2_document)
            import pymysql

            config_root = pymysql.connect(
                host="127.0.0.1",
                port=port,
                user="root",
                database="beadhive_hq_config",
                autocommit=True,
            )
            with config_root.cursor() as cursor:
                cursor.execute("SELECT DOLT_HASHOF('HEAD')")
                schema_head = cursor.fetchone()[0]
                cursor.execute(
                    "INSERT INTO hq_config_meta VALUES (1,1,%s,%s,1,%s,%s,3)",
                    (
                        "config-backend",
                        "config-generation",
                        "00000000-0000-0000-0000-000000000001",
                        _digest(documents),
                    ),
                )
                cursor.execute(
                    "INSERT INTO hq_config_documents VALUES (%s,0,'fleet',%s,%s)",
                    (
                        fleet_document.path,
                        fleet_document.content,
                        hashlib.sha256(fleet_document.content.encode()).hexdigest(),
                    ),
                )
                cursor.execute(
                    "INSERT INTO hq_config_documents VALUES (%s,1,'host',%s,%s)",
                    (
                        host_document.path,
                        host_document.content,
                        hashlib.sha256(host_document.content.encode()).hexdigest(),
                    ),
                )
                cursor.execute(
                    "INSERT INTO hq_config_documents VALUES (%s,2,'host',%s,%s)",
                    (
                        host2_document.path,
                        host2_document.content,
                        hashlib.sha256(host2_document.content.encode()).hexdigest(),
                    ),
                )
                cursor.execute(
                    "INSERT INTO hq_config_publications VALUES (%s,1,%s,%s,%s,3)",
                    (
                        "00000000-0000-0000-0000-000000000001",
                        "config-generation",
                        schema_head,
                        _digest(documents),
                    ),
                )
                cursor.execute(
                    "CALL DOLT_ADD('hq_config_meta','hq_config_documents','hq_config_publications')"
                )
                cursor.execute(
                    "CALL DOLT_COMMIT('-m','seed config','--author',"
                    "'Fixture <fixture@example.invalid>')"
                )
                cursor.execute("SELECT DOLT_HASHOF('HEAD')")
                config_head = cursor.fetchone()[0]
            config_root.close()
            now = time.time()
            policy_expiry = now + 3600
            config_snapshot = FleetConfigSnapshot(
                "sql:config-backend",
                config_head,
                "config-generation",
                time.time(),
                time.time() + 30,
                documents,
            )
            policies = project_hive_policies(config_snapshot, valid_until=policy_expiry)

            private = Ed25519PrivateKey.generate()
            key = tmp_path / "operator-key"
            key.write_bytes(
                private.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.OpenSSH,
                    serialization.NoEncryption(),
                )
            )
            public = (
                private.public_key()
                .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
                .decode()
            )
            frame_private = Ed25519PrivateKey.generate()
            frame_key = tmp_path / "frame-key"
            frame_key.write_bytes(
                frame_private.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.OpenSSH,
                    serialization.NoEncryption(),
                )
            )
            frame_public = (
                frame_private.public_key()
                .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
                .decode()
            )
            frame_fingerprint = fingerprint(frame_public)
            authority = {
                "frame_id": "frame-1",
                "holder_identity": "host-1",
                "instance_ref": "vm-1",
                "key_fingerprint": frame_fingerprint,
                "epoch": 1,
                "audience": "fixture-fleet",
                "config_revision": "desired-1",
                "candidate_expires_at": now + 1800,
            }
            candidate = {
                "authority": authority,
                "public_key": frame_public,
                "state": "active",
                "desired": {
                    "declared": True,
                    "release": {"id": "fixture", "digest": "sha256:" + "1" * 64},
                    "caps": manifest.capabilities.model_dump(),
                    "profile": "fixture",
                },
                "receipt": {
                    "sequence": 0,
                    "sha": "",
                    "first_seen": None,
                    "consecutive": 0,
                    "lease": None,
                    "registration": None,
                },
                "cordoned": False,
                "drain_deadline": None,
            }
            state = {
                "domain": guard.DOMAIN,
                "generation": "runtime-generation",
                "revision": 1,
                "issued_at": now - 1,
                "expires_at": policy_expiry,
                "frames": {
                    "frame-1": {
                        "active": candidate,
                        "candidate": None,
                        "retired": [],
                        "epoch_floor": 1,
                    }
                },
            }
            guard.validate_state(state)
            signed = {
                "backend_identity": "runtime-backend",
                "generation": "runtime-generation",
                "revision": 1,
                "config_backend": "sql:config-backend",
                "config_generation": "config-generation",
                "config_head": config_head,
                "state": state,
                "hive_policies": policies,
            }
            signature = sign_authority(signed, signing_key=str(key))
            import pymysql

            root = pymysql.connect(
                host="127.0.0.1",
                port=port,
                user="root",
                database="beadhive_hq_runtime",
                autocommit=True,
            )
            with root.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO hq_authority VALUES (1,1,%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        "runtime-backend",
                        "runtime-generation",
                        "sql:config-backend",
                        "config-generation",
                        config_head,
                        canonical(state),
                        hashlib.sha256(canonical(state)).hexdigest(),
                        canonical(policies),
                        hashlib.sha256(canonical(policies)).hexdigest(),
                        signature,
                    ),
                )
                cursor.execute(
                    "INSERT INTO hq_principal_registry VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        "frame_a",
                        "frame-1",
                        "host-1",
                        "vm-1",
                        1,
                        "hq_live_inbox_frame_a_1",
                        frame_fingerprint,
                    ),
                )
                cursor.execute("CALL DOLT_ADD('hq_authority','hq_principal_registry')")
                cursor.execute(
                    "CALL DOLT_COMMIT('-m','seed authority','--author',"
                    "'Fixture <fixture@example.invalid>')"
                )
                cursor.execute("SELECT DOLT_HASHOF('HEAD')")
                initial = cursor.fetchone()[0]
                cursor.execute(
                    "INSERT INTO hq_live_floors VALUES (%s,%s,%s,%s,%s,%s)",
                    ("frame-1", "host-1", 1, 0, "", None),
                )
            root.close()
            _cli(
                tmp_path,
                port,
                "CREATE USER 'frame_a'@'localhost' IDENTIFIED BY 'fixture-secret'; "
                "GRANT SELECT ON beadhive_hq_runtime.hq_authority TO 'frame_a'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_runtime.hq_principal_registry "
                "TO 'frame_a'@'localhost'; "
                "GRANT SELECT,INSERT,UPDATE ON beadhive_hq_runtime.hq_live_inbox_frame_a_1 "
                "TO 'frame_a'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_runtime.hq_live_results "
                "TO 'frame_a'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_runtime.hq_live_hive_leases "
                "TO 'frame_a'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_runtime.hq_live_public_observations "
                "TO 'frame_a'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_config.hq_config_meta TO 'frame_a'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_config.hq_config_documents "
                "TO 'frame_a'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_config.hq_config_publications "
                "TO 'frame_a'@'localhost'; "
                "CREATE USER 'observer'@'localhost' IDENTIFIED BY 'fixture-secret'; "
                "GRANT SELECT ON beadhive_hq_runtime.hq_authority TO 'observer'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_runtime.hq_principal_registry "
                "TO 'observer'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_runtime.hq_live_inbox_frame_a_1 "
                "TO 'observer'@'localhost'; "
                "GRANT SELECT,INSERT ON beadhive_hq_runtime.hq_live_receipts "
                "TO 'observer'@'localhost'; "
                "GRANT SELECT,UPDATE ON beadhive_hq_runtime.hq_live_floors "
                "TO 'observer'@'localhost'; "
                "GRANT SELECT,INSERT ON beadhive_hq_runtime.hq_live_results "
                "TO 'observer'@'localhost'; "
                "GRANT SELECT,INSERT,UPDATE ON beadhive_hq_runtime.hq_live_hive_leases "
                "TO 'observer'@'localhost'; "
                "GRANT SELECT,INSERT,UPDATE ON beadhive_hq_runtime.hq_live_public_observations "
                "TO 'observer'@'localhost'; "
                "GRANT SELECT,INSERT,UPDATE ON beadhive_hq_runtime.hq_live_registrations "
                "TO 'observer'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_config.hq_config_meta TO 'observer'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_config.hq_config_documents "
                "TO 'observer'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_config.hq_config_publications "
                "TO 'observer'@'localhost'; "
                "CREATE USER 'config_reader'@'localhost' IDENTIFIED BY 'fixture-secret'; "
                "GRANT SELECT ON beadhive_hq_config.hq_config_meta "
                "TO 'config_reader'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_config.hq_config_documents "
                "TO 'config_reader'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_config.hq_config_publications "
                "TO 'config_reader'@'localhost'; "
                "CREATE USER 'config_publisher'@'localhost' IDENTIFIED BY 'fixture-secret'; "
                "GRANT SELECT,UPDATE ON beadhive_hq_config.hq_config_meta "
                "TO 'config_publisher'@'localhost'; "
                "GRANT SELECT,INSERT,DELETE ON beadhive_hq_config.hq_config_documents "
                "TO 'config_publisher'@'localhost'; "
                "GRANT SELECT,INSERT ON beadhive_hq_config.hq_config_publications "
                "TO 'config_publisher'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_config.dolt_status "
                "TO 'config_publisher'@'localhost'; "
                "GRANT EXECUTE ON PROCEDURE beadhive_hq_config.dolt_add "
                "TO 'config_publisher'@'localhost'; "
                "GRANT EXECUTE ON PROCEDURE beadhive_hq_config.dolt_commit "
                "TO 'config_publisher'@'localhost'; "
                "CREATE USER 'authority_writer'@'localhost' IDENTIFIED BY 'fixture-secret'; "
                "GRANT SELECT,UPDATE ON beadhive_hq_runtime.hq_authority "
                "TO 'authority_writer'@'localhost'; "
                "GRANT SELECT,INSERT ON beadhive_hq_runtime.hq_principal_registry "
                "TO 'authority_writer'@'localhost'; "
                "GRANT SELECT,INSERT ON beadhive_hq_runtime.hq_live_floors "
                "TO 'authority_writer'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_runtime.hq_live_receipts "
                "TO 'authority_writer'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_runtime.hq_live_public_observations "
                "TO 'authority_writer'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_runtime.hq_live_registrations "
                "TO 'authority_writer'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_runtime.dolt_status "
                "TO 'authority_writer'@'localhost'; "
                "GRANT EXECUTE ON PROCEDURE beadhive_hq_runtime.dolt_add "
                "TO 'authority_writer'@'localhost'; "
                "GRANT EXECUTE ON PROCEDURE beadhive_hq_runtime.dolt_commit "
                "TO 'authority_writer'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_config.hq_config_meta "
                "TO 'authority_writer'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_config.hq_config_documents "
                "TO 'authority_writer'@'localhost'; "
                "GRANT SELECT ON beadhive_hq_config.hq_config_publications "
                "TO 'authority_writer'@'localhost';",
            )
            settings = {
                "enabled": True,
                "reader": _binding(tmp_path, port, "config_reader", "beadhive_hq_config"),
                "runtime": _binding(tmp_path, port, "frame_a", "beadhive_hq_runtime"),
                "floor_path": str(tmp_path / "config-floor.json"),
                "backend_identity": "config-backend",
                "generation": "config-generation",
                "initial_revision": config_head,
                "minimum_sequence": 1,
                "cache_ttl": 30,
                "runtime_floor_path": str(tmp_path / "runtime-floor.json"),
                "runtime_backend_identity": "runtime-backend",
                "runtime_generation": "runtime-generation",
                "runtime_initial_revision": initial,
                "runtime_operator_public_key": public,
            }
            plane = SqlControlPlane(settings, broker=Broker())
            status = plane.authority_status()
            assert status["revision"] == initial
            assert status["state"] == state
            assert status["authority_ready"] is True
            assert SqlControlPlane(settings, broker=Broker()).authority_status() == status
            locked_settings = {
                **settings,
                "runtime": {**settings["runtime"], "operation_timeout": 1},
            }
            with (tmp_path / "runtime-floor.json.lock").open("a+b") as floor_lock:
                fcntl.flock(floor_lock, fcntl.LOCK_EX)
                began = time.monotonic()
                with pytest.raises(ValueError, match="floor custody wait exceeded deadline"):
                    SqlControlPlane(locked_settings, broker=Broker()).authority_status()
                assert time.monotonic() - began < 1.5
            from beadhive.hq_sql_transport import connect

            frame_snapshot_connection = connect(
                settings["runtime"], Broker(), deadline=time.monotonic() + 10
            )
            try:
                with frame_snapshot_connection.cursor() as cursor:
                    cursor.execute("START TRANSACTION")
                    cursor.execute("SELECT DOLT_HASHOF('HEAD')")
                    runtime_head = cursor.fetchone()[0]
                    verified_state, crossref, verified_policies = (
                        plane._runtime_authority().verified_state_at(cursor, runtime_head)
                    )
                    assert verified_state == state
                    assert verified_policies == policies
                    snapshot = plane._runtime_authority().load_config_at(cursor, crossref)
                    assert snapshot.commit_revision == config_head
                    fresh_projection = project_hive_policies(snapshot, valid_until=policy_expiry)
                    assert {
                        key: value
                        for key, value in fresh_projection["bh"].items()
                        if key != "valid_until"
                    } == {
                        key: value for key, value in policies["bh"].items() if key != "valid_until"
                    }
                    assert policies["bh"]["valid_until"] > snapshot.valid_until
            finally:
                frame_snapshot_connection.rollback()
                frame_snapshot_connection.close()

            runtime_commits_before_live_sql = _runtime_commit_hashes(port)
            assert initial in runtime_commits_before_live_sql
            lease = HeartbeatLease(
                audience="fixture-fleet",
                frame_id="frame-1",
                holderIdentity="host-1",
                instance_ref="vm-1",
                key_id=frame_fingerprint,
                epoch=1,
                config_revision="desired-1",
                seq=1,
                renewTime=datetime.fromtimestamp(now, UTC).isoformat(),
                state_seen="active",
                release={"id": "fixture", "digest": "sha256:" + "1" * 64},
                report_digest="sha256:" + "2" * 64,
                conformance={"profile": "fixture", "status": "conformant", "checks": []},
            )
            published_digest = plane.heartbeat(lease, signing_key=str(frame_key))
            assert published_digest.startswith("sha256:")

            root = pymysql.connect(
                host="127.0.0.1",
                port=port,
                user="root",
                database="beadhive_hq_runtime",
                autocommit=True,
            )
            with root.cursor() as cursor:
                cursor.execute(
                    "SELECT request_id FROM hq_live_inbox_frame_a_1 WHERE payload_sha256=%s",
                    (published_digest.removeprefix("sha256:"),),
                )
                request_id = cursor.fetchone()[0]
            root.close()
            receiver_settings = {
                **settings,
                "runtime": None,
                "observer": _binding(tmp_path, port, "observer", "beadhive_hq_runtime"),
            }
            assert plane.settings["observer"] is None
            receiver_settings_path = tmp_path / "observer-settings.json"
            receiver_settings_path.write_text(json.dumps(receiver_settings))
            receiver_settings_path.chmod(0o600)
            worker = Path(__file__).parent / "harness/sql_receiver_worker.py"
            registration_digest = plane.publish_registration_evidence(
                manifest, signing_key=str(frame_key)
            )
            registration_root = pymysql.connect(
                host="127.0.0.1",
                port=port,
                user="root",
                database="beadhive_hq_runtime",
                autocommit=True,
            )
            with registration_root.cursor() as cursor:
                cursor.execute(
                    "SELECT request_id FROM hq_live_inbox_frame_a_1 "
                    "WHERE kind='registration' AND payload_sha256=%s",
                    (registration_digest.removeprefix("sha256:"),),
                )
                registration_id = cursor.fetchone()[0]
            registration_root.close()
            registered = subprocess.run(
                [
                    sys.executable,
                    str(worker),
                    str(receiver_settings_path),
                    "frame_a",
                    registration_id,
                    "--registration",
                ],
                cwd=Path(__file__).parent.parent,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert registered.returncode == 0, registered.stderr
            assert registered.stdout.strip() == registration_digest
            # The frame owns this inbox row, so its bytes are treated only as
            # a proposal. A bad signature cannot create a protected receipt,
            # result or observer floor; restoring the original signed bytes
            # allows exactly that original request to be accepted below.
            tamper = pymysql.connect(
                host="127.0.0.1",
                port=port,
                user="root",
                database="beadhive_hq_runtime",
                autocommit=True,
            )
            try:
                with tamper.cursor() as cursor:
                    cursor.execute(
                        "SELECT payload,payload_sha256 FROM hq_live_inbox_frame_a_1 "
                        "WHERE request_id=%s",
                        (request_id,),
                    )
                    original_payload, original_payload_sha = cursor.fetchone()
                    forged_envelope = json.loads(original_payload)
                    forged_envelope["spec"]["signature"]["value"] = "AAAA"
                    forged_payload = canonical(forged_envelope)
                    cursor.execute(
                        "UPDATE hq_live_inbox_frame_a_1 SET payload=%s,payload_sha256=%s "
                        "WHERE request_id=%s",
                        (forged_payload, hashlib.sha256(forged_payload).hexdigest(), request_id),
                    )
                    forged = subprocess.run(
                        [
                            sys.executable,
                            str(worker),
                            str(receiver_settings_path),
                            "frame_a",
                            request_id,
                        ],
                        cwd=Path(__file__).parent.parent,
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                    assert forged.returncode != 0
                    assert "authentication failed" in forged.stderr
                    cursor.execute("SELECT sequence FROM hq_live_floors WHERE frame_id='frame-1'")
                    assert cursor.fetchone() == (0,)
                    cursor.execute(
                        "SELECT request_id FROM hq_live_results WHERE request_id=%s",
                        (request_id,),
                    )
                    assert cursor.fetchone() is None
                    assert not plane.read_eligibility(manifest)[2].verified
                    cursor.execute(
                        "UPDATE hq_live_inbox_frame_a_1 SET payload=%s,payload_sha256=%s "
                        "WHERE request_id=%s",
                        (original_payload, original_payload_sha, request_id),
                    )
            finally:
                tamper.close()
            killed_before_commit = subprocess.run(
                [
                    sys.executable,
                    str(worker),
                    str(receiver_settings_path),
                    "frame_a",
                    request_id,
                    "--abort-before-commit",
                ],
                cwd=Path(__file__).parent.parent,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert killed_before_commit.returncode == 77
            crash_check = pymysql.connect(
                host="127.0.0.1",
                port=port,
                user="root",
                database="beadhive_hq_runtime",
                autocommit=True,
            )
            try:
                with crash_check.cursor() as cursor:
                    cursor.execute("SELECT sequence FROM hq_live_floors WHERE frame_id='frame-1'")
                    assert cursor.fetchone() == (0,)
                    cursor.execute("SELECT COUNT(*) FROM hq_live_receipts WHERE frame_id='frame-1'")
                    assert cursor.fetchone() == (0,)
                    cursor.execute(
                        "SELECT COUNT(*) FROM hq_live_results WHERE request_id=%s",
                        (request_id,),
                    )
                    assert cursor.fetchone() == (0,)
            finally:
                crash_check.close()
            killed_after_commit = subprocess.run(
                [
                    sys.executable,
                    str(worker),
                    str(receiver_settings_path),
                    "frame_a",
                    request_id,
                    "--abort-after-commit",
                ],
                cwd=Path(__file__).parent.parent,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert killed_after_commit.returncode == 78
            crash_recovery = {
                "request_sha256": published_digest.removeprefix("sha256:"),
                "principal": "frame_a",
                "frame_id": "frame-1",
                "holder_identity": "host-1",
                "instance_ref": "vm-1",
                "epoch": 1,
                "audience": "fixture-fleet",
                "signer_fingerprint": frame_fingerprint,
                "expected_revision": initial,
            }
            crash_recovery_path = tmp_path / "lost-ack-request.json"
            crash_recovery_path.write_text(json.dumps(crash_recovery))
            recovered_after_crash = subprocess.run(
                [
                    sys.executable,
                    str(worker),
                    str(receiver_settings_path),
                    "frame_a",
                    request_id,
                    str(crash_recovery_path),
                ],
                cwd=Path(__file__).parent.parent,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert recovered_after_crash.returncode == 0, recovered_after_crash.stderr
            assert recovered_after_crash.stdout.strip() == published_digest
            for _ in range(2):
                accepted = subprocess.run(
                    [
                        sys.executable,
                        str(worker),
                        str(receiver_settings_path),
                        "frame_a",
                        request_id,
                    ],
                    cwd=Path(__file__).parent.parent,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                assert accepted.returncode == 0, accepted.stderr
                assert accepted.stdout.strip() == published_digest
            # Frame proposals and trusted receiver commits are live SQL only:
            # neither may create a versioned authority commit in dolt_log.
            assert _runtime_commit_hashes(port) == runtime_commits_before_live_sql
            expired_lease = lease.model_copy(
                update={
                    "seq": 99,
                    "renewTime": datetime.fromtimestamp(now - 1000, UTC).isoformat(),
                }
            )
            expired_digest = plane.heartbeat(expired_lease, signing_key=str(frame_key))
            expired_root = pymysql.connect(
                host="127.0.0.1",
                port=port,
                user="root",
                database="beadhive_hq_runtime",
                autocommit=True,
            )
            try:
                with expired_root.cursor() as cursor:
                    cursor.execute(
                        "SELECT request_id FROM hq_live_inbox_frame_a_1 WHERE payload_sha256=%s",
                        (expired_digest.removeprefix("sha256:"),),
                    )
                    expired_id = cursor.fetchone()[0]
            finally:
                expired_root.close()
            late = subprocess.run(
                [sys.executable, str(worker), str(receiver_settings_path), "frame_a", expired_id],
                cwd=Path(__file__).parent.parent,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert late.returncode != 0
            assert "expired or future-skewed" in late.stderr
            authenticated_route = plane._runtime_authority().load_frame_binding(
                expected_head=initial
            )
            assert plane._runtime_authority().read_public_result(
                request_id,
                request_sha256=published_digest.removeprefix("sha256:"),
                principal=authenticated_route,
                audience="fixture-fleet",
                expected_revision=initial,
            ) == ("accepted", published_digest)

            hive_lease = HostLease(
                host_id="host-1",
                label="fixture",
                epoch=1,
                adopted_at=now_stamp(now),
                expires_at=now_stamp(now + 1200),
            )
            proposal_id, proposal_sha, route, proposal_audience = plane.propose_hive_lease(
                "bh", hive_lease, expected="", operation="adopt", signing_key=str(frame_key)
            )
            hive_accept = subprocess.run(
                [
                    sys.executable,
                    str(worker),
                    str(receiver_settings_path),
                    "frame_a",
                    proposal_id,
                    "--hive",
                ],
                cwd=Path(__file__).parent.parent,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert hive_accept.returncode == 0, hive_accept.stderr
            hive_revision = hive_accept.stdout.strip()
            assert len(hive_revision) == 64
            assert plane._runtime_authority().read_public_result(
                proposal_id,
                request_sha256=proposal_sha,
                principal=route,
                audience=proposal_audience,
                expected_revision="",
            ) == ("accepted", hive_revision)
            repeated = subprocess.run(
                [
                    sys.executable,
                    str(worker),
                    str(receiver_settings_path),
                    "frame_a",
                    proposal_id,
                    "--hive",
                ],
                cwd=Path(__file__).parent.parent,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert repeated.returncode == 0, repeated.stderr
            assert repeated.stdout.strip() == hive_revision
            renewals = [
                HostLease(
                    host_id="host-1",
                    label="fixture",
                    epoch=1,
                    adopted_at=hive_lease.adopted_at,
                    expires_at=now_stamp(now + duration),
                )
                for duration in (1300, 1400)
            ]
            competing = [
                plane.propose_hive_lease(
                    "bh",
                    candidate,
                    expected=hive_revision,
                    operation="renew",
                    signing_key=str(frame_key),
                )
                for candidate in renewals
            ]
            workers = [
                subprocess.Popen(
                    [
                        sys.executable,
                        str(worker),
                        str(receiver_settings_path),
                        "frame_a",
                        proposal[0],
                        "--hive",
                    ],
                    cwd=Path(__file__).parent.parent,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for proposal in competing
            ]
            try:
                results = [process.communicate(timeout=20) for process in workers]
            finally:
                for process in workers:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
            accepted_indexes = [
                index for index, process in enumerate(workers) if process.returncode == 0
            ]
            assert len(accepted_indexes) == 1, results
            winner = accepted_indexes[0]
            hive_revision = results[winner][0].strip()
            hive_lease = renewals[winner]
            assert plane.read_hive_lease_record("bh", holder_identity="host-1") == (
                hive_revision,
                hive_lease,
            )
            manifest_identity = SimpleNamespace(
                frame_id="frame-1", host_id="host-1", instance_ref="vm-1"
            )
            from harness.hq_membership_conformance import assert_common_membership_reads

            assert_common_membership_reads(plane, manifest_identity, published_digest)
            # The authenticated read cache can expire without expiring the
            # separately signed durable policy. A new read must requalify HEAD.
            short_cache_plane = SqlControlPlane({**settings, "cache_ttl": 1}, broker=Broker())
            short_cache_snapshot = short_cache_plane.config_store().load_snapshot()
            time.sleep(1.1)
            assert time.time() > short_cache_snapshot.valid_until
            assert short_cache_plane.read_eligibility(manifest_identity)[2].sha == published_digest
            eligible_head, desired, observation = plane.read_eligibility(manifest_identity)
            assert eligible_head == initial
            assert desired["state"] == "active"
            assert observation.verified and observation.fresh
            assert observation.sha == published_digest
            assert plane.observe(manifest_identity).sha == published_digest
            assert plane.fetch_config("frame-1", holder_identity="host-1") == desired
            assert plane.watch_state("frame-1").sha == published_digest
            joined = plane.load_config_authority_snapshot("frame-1", revision=config_head)
            assert joined.config.commit_revision == config_head
            assert joined.authority.sha == published_digest
            assert joined.token.revision == initial
            assert plane.eligibility_authority_status()["authority_ready"] is True
            assert plane.heartbeat_reference(lease) == "sql:inbox/frame_a/1"
            from beadhive.hq_sql_operator import SqlRuntimeOperator

            operator_settings = {
                **settings,
                "runtime": None,
                "authority_writer": _binding(
                    tmp_path, port, "authority_writer", "beadhive_hq_runtime"
                ),
            }
            operator = SqlRuntimeOperator(operator_settings, broker=Broker())
            protected_record, protected_registration, protected_receipts = operator.evidence(
                "frame-1", "host-1", expected_revision=initial
            )
            assert protected_record["state"] == "active"
            assert protected_registration == manifest
            assert protected_receipts[0][1] == published_digest
            assert plane.read_hive_lease_record("bh", holder_identity="host-1") == (
                hive_revision,
                hive_lease,
            )

            # Both public eligibility and private operator evidence verify the
            # complete current-signer envelope on read, even after ingestion.
            root = pymysql.connect(
                host="127.0.0.1",
                port=port,
                user="root",
                database="beadhive_hq_runtime",
                autocommit=True,
            )
            try:
                with root.cursor() as cursor:
                    cursor.execute(
                        "SELECT lease_json,envelope_json,digest,signer_fingerprint "
                        "FROM hq_live_public_observations WHERE frame_id='frame-1'"
                    )
                    public_original = cursor.fetchone()
                    cursor.execute(
                        "SELECT envelope_json FROM hq_live_receipts "
                        "WHERE frame_id='frame-1' AND sequence=1"
                    )
                    private_envelope = cursor.fetchone()[0]
                    signed = json.loads(public_original[1])
                    bad_signature = json.loads(public_original[1])
                    bad_signature["spec"]["signature"]["value"] = "AAAA"
                    signed_body = json.loads(public_original[0])
                    corruptions = [
                        ("envelope_json", canonical(bad_signature)),
                        ("digest", "sha256:" + "0" * 64),
                        ("signer_fingerprint", "SHA256:wrong"),
                    ]
                    for name, value in (
                        (
                            "conformance",
                            {"profile": "fixture", "status": "non-conformant", "checks": []},
                        ),
                        ("release", {"id": "other", "digest": "sha256:" + "3" * 64}),
                        ("capabilities", {"max_sessions": 999}),
                    ):
                        altered = {**signed_body, name: value}
                        corruptions.append(("lease_json", canonical(altered)))
                    assert signed["spec"]["signature"]["value"] != "AAAA"
                    for column, value in corruptions:
                        try:
                            cursor.execute(
                                f"UPDATE hq_live_public_observations SET {column}=%s "
                                "WHERE frame_id='frame-1'",
                                (value,),
                            )
                            with pytest.raises(ControlPlaneError):
                                plane.read_eligibility(manifest_identity)
                        finally:
                            cursor.execute(
                                f"UPDATE hq_live_public_observations SET {column}=%s "
                                "WHERE frame_id='frame-1'",
                                (
                                    public_original[
                                        {
                                            "lease_json": 0,
                                            "envelope_json": 1,
                                            "digest": 2,
                                            "signer_fingerprint": 3,
                                        }[column]
                                    ],
                                ),
                            )
                    try:
                        cursor.execute(
                            "UPDATE hq_live_receipts SET envelope_json=%s "
                            "WHERE frame_id='frame-1' AND sequence=1",
                            (canonical(bad_signature),),
                        )
                        with pytest.raises(ValueError):
                            operator.evidence("frame-1", "host-1", expected_revision=initial)
                    finally:
                        cursor.execute(
                            "UPDATE hq_live_receipts SET envelope_json=%s "
                            "WHERE frame_id='frame-1' AND sequence=1",
                            (private_envelope,),
                        )
            finally:
                root.close()

            deadline = time.monotonic() + 10
            frame = connect(settings["runtime"], Broker(), deadline=deadline)
            try:
                with frame.cursor() as cursor:
                    for sql in (
                        "UPDATE hq_authority SET revision=999",
                        "SELECT * FROM hq_live_receipts",
                        "UPDATE hq_live_floors SET sequence=999",
                        "UPDATE hq_live_public_observations SET sequence=999",
                        "INSERT INTO hq_live_results (request_id) VALUES ('forged')",
                        "CREATE TABLE forbidden (id INT)",
                        "CALL DOLT_COMMIT('-am','forbidden')",
                    ):
                        with pytest.raises(pymysql.MySQLError):
                            cursor.execute(sql)
                    cursor.execute(
                        "INSERT INTO hq_live_inbox_frame_a_1 VALUES "
                        "('00000000-0000-0000-0000-000000000001','heartbeat','{}',%s,0)",
                        (hashlib.sha256(b"{}").hexdigest(),),
                    )
                    cursor.execute(
                        "SELECT payload FROM hq_live_inbox_frame_a_1 WHERE payload_sha256=%s",
                        (published_digest.removeprefix("sha256:"),),
                    )
                    signed_lease, verified_digest = verify_heartbeat(
                        json.loads(cursor.fetchone()[0]), granted_public_key=frame_public
                    )
                    assert signed_lease == lease
                    assert verified_digest == published_digest
                    frame.commit()
            finally:
                frame.close()

            root = pymysql.connect(
                host="127.0.0.1",
                port=port,
                user="root",
                database="beadhive_hq_runtime",
                autocommit=True,
            )
            with root.cursor() as cursor:
                cursor.execute(
                    "SELECT sequence,digest,first_seen FROM hq_live_floors "
                    "WHERE frame_id='frame-1' AND holder_identity='host-1' AND epoch=1"
                )
                floor = cursor.fetchone()
                assert floor[0:2] == (1, published_digest)
                cursor.execute(
                    "SELECT first_seen FROM hq_live_receipts "
                    "WHERE frame_id='frame-1' AND holder_identity='host-1' AND epoch=1 "
                    "AND sequence=1"
                )
                assert cursor.fetchone()[0] == floor[2]
                cursor.execute(
                    "SELECT request_sha256,principal,status,result_revision "
                    "FROM hq_live_results WHERE request_id=%s",
                    (request_id,),
                )
                assert cursor.fetchone() == (
                    published_digest.removeprefix("sha256:"),
                    "frame_a",
                    "accepted",
                    published_digest,
                )
            root.close()

            # A later accepted nonconformant heartbeat changes no committed HEAD.
            # Inject it after the pinned composite read, before the outside-snapshot
            # public observation reread; the old receipt must be rejected.
            from beadhive.hq_sql_runtime import SqlRuntimeAuthority, SqlRuntimeError

            original_fence = SqlRuntimeAuthority.fresh_public_observation_fence
            changed_once = False

            def accept_later_nonconformant(self, route, expected, *, deadline):
                nonlocal changed_once
                if not changed_once:
                    changed_once = True
                    later = HeartbeatLease.model_validate(
                        {
                            **lease.model_dump(),
                            "seq": 2,
                            "conformance": {
                                "profile": "fixture",
                                "status": "non-conformant",
                                "checks": [],
                            },
                        }
                    )
                    later_digest = plane.heartbeat(later, signing_key=str(frame_key))
                    owner = pymysql.connect(
                        host="127.0.0.1",
                        port=port,
                        user="root",
                        database="beadhive_hq_runtime",
                        autocommit=True,
                    )
                    try:
                        with owner.cursor() as cursor:
                            cursor.execute(
                                "SELECT request_id FROM hq_live_inbox_frame_a_1 "
                                "WHERE payload_sha256=%s",
                                (later_digest.removeprefix("sha256:"),),
                            )
                            later_id = cursor.fetchone()[0]
                    finally:
                        owner.close()
                    accepted_later = subprocess.run(
                        [
                            sys.executable,
                            str(worker),
                            str(receiver_settings_path),
                            "frame_a",
                            later_id,
                        ],
                        cwd=Path(__file__).parent.parent,
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                    assert accepted_later.returncode == 0, accepted_later.stderr
                return original_fence(self, route, expected, deadline=deadline)

            with monkeypatch.context() as patch:
                patch.setattr(
                    SqlRuntimeAuthority,
                    "fresh_public_observation_fence",
                    accept_later_nonconformant,
                )
                with pytest.raises(SqlRuntimeError, match="accepted observer receipt changed"):
                    plane._runtime_authority().read_frame_composite()
            assert changed_once
            assert plane.observe(manifest_identity).lease.conformance.status == "non-conformant"

            # Frame-owned ON DUP/UPDATE can rewrite inbox evidence, but never the
            # protected result's accepted meaning or the immutable first_seen.
            changed = sign_heartbeat(
                lease.model_copy(update={"seq": 2}), signing_key=str(frame_key)
            )
            changed_body = canonical(changed)
            frame = connect(settings["runtime"], Broker(), deadline=time.monotonic() + 10)
            try:
                with frame.cursor() as cursor:
                    cursor.execute(
                        "UPDATE hq_live_inbox_frame_a_1 SET payload=%s,payload_sha256=%s "
                        "WHERE request_id=%s",
                        (changed_body, hashlib.sha256(changed_body).hexdigest(), request_id),
                    )
                    frame.commit()
            finally:
                frame.close()
            altered = subprocess.run(
                [sys.executable, str(worker), str(receiver_settings_path), "frame_a", request_id],
                cwd=Path(__file__).parent.parent,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert altered.returncode != 0
            assert "request ID was reused" in altered.stderr

            recovery = {
                "request_sha256": published_digest.removeprefix("sha256:"),
                "principal": "frame_a",
                "frame_id": "frame-1",
                "holder_identity": "host-1",
                "instance_ref": "vm-1",
                "epoch": 1,
                "audience": "fixture-fleet",
                "signer_fingerprint": frame_fingerprint,
                "expected_revision": initial,
            }
            recovery_path = tmp_path / "original-request.json"
            recovery_path.write_text(json.dumps(recovery))

            # Live protected receipts and the hive lease survive a server
            # restart. The new client process must still authenticate their
            # signer, floor, and committed authority instead of trusting an
            # in-memory observation cache.
            pre_restart_observation = plane.observe(manifest_identity)
            pre_restart_receipts = operator.evidence(
                "frame-1", "host-1", expected_revision=initial
            )[2]
            server.terminate()
            server.wait(timeout=10)
            with pytest.raises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=0.2)
            with (tmp_path / "server.log").open("a") as log:
                server = subprocess.Popen(
                    [shutil.which("dolt"), "sql-server", "--config", str(tmp_path / "server.yaml")],
                    cwd=tmp_path,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            restart_deadline = time.monotonic() + 20
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    if server.poll() is not None or time.monotonic() >= restart_deadline:
                        raise AssertionError("owned runtime Dolt restart failed") from None
                    time.sleep(0.1)
            assert plane.observe(manifest_identity).sha == pre_restart_observation.sha
            assert (
                operator.evidence("frame-1", "host-1", expected_revision=initial)[2][0][1:3]
                == pre_restart_receipts[0][1:3]
            )
            assert plane.read_hive_lease_record("bh", holder_identity="host-1") == (
                hive_revision,
                hive_lease,
            )

            operator_plane = SqlControlPlane(operator_settings, broker=Broker())
            # A second, separately provisioned principal moves from pending
            # grant to admission only after three signed, accepted beats.
            from beadhive.host_heartbeat_core import ObservationAuthority

            second_private = Ed25519PrivateKey.generate()
            second_key = tmp_path / "frame-two-key"
            second_key.write_bytes(
                second_private.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.OpenSSH,
                    serialization.NoEncryption(),
                )
            )
            second_public = (
                second_private.public_key()
                .public_bytes(
                    serialization.Encoding.OpenSSH,
                    serialization.PublicFormat.OpenSSH,
                )
                .decode()
            )
            second_authority = ObservationAuthority(
                "frame-2",
                "host-2",
                "vm-2",
                fingerprint(second_public),
                1,
                "fixture-fleet",
                "desired-1",
                time.time() + 1800,
            )
            second_principal = SqlRuntimeOperator.principal_for(second_authority)
            second_inbox = f"hq_live_inbox_{second_principal}_1"
            _cli(tmp_path, port, "USE beadhive_hq_runtime; " + inbox_ddl(second_principal, 1))
            _cli(
                tmp_path,
                port,
                f"CREATE USER '{second_principal}'@'localhost' IDENTIFIED BY 'fixture-secret'; "
                f"GRANT SELECT ON beadhive_hq_runtime.hq_authority "
                f"TO '{second_principal}'@'localhost'; "
                f"GRANT SELECT ON beadhive_hq_runtime.hq_principal_registry "
                f"TO '{second_principal}'@'localhost'; "
                f"GRANT SELECT,INSERT,UPDATE ON beadhive_hq_runtime.{second_inbox} "
                f"TO '{second_principal}'@'localhost'; "
                f"GRANT SELECT ON beadhive_hq_runtime.hq_live_results "
                f"TO '{second_principal}'@'localhost'; "
                f"GRANT SELECT ON beadhive_hq_runtime.hq_live_hive_leases "
                f"TO '{second_principal}'@'localhost'; "
                f"GRANT SELECT ON beadhive_hq_runtime.hq_live_public_observations "
                f"TO '{second_principal}'@'localhost'; "
                f"GRANT SELECT ON beadhive_hq_config.hq_config_meta "
                f"TO '{second_principal}'@'localhost'; "
                f"GRANT SELECT ON beadhive_hq_config.hq_config_documents "
                f"TO '{second_principal}'@'localhost'; "
                f"GRANT SELECT ON beadhive_hq_config.hq_config_publications "
                f"TO '{second_principal}'@'localhost'; "
                f"GRANT SELECT ON beadhive_hq_runtime.{second_inbox} "
                "TO 'observer'@'localhost'",
            )
            desired2 = {
                "declared": True,
                "release": manifest2.release.model_dump(),
                "caps": manifest2.capabilities.model_dump(),
                "profile": "fixture",
            }
            grant_head = operator_plane.grant(
                second_authority,
                second_public,
                desired2,
                expected=initial,
                operator_key=str(key),
            )
            second_settings = {
                **settings,
                "runtime": _binding(tmp_path, port, second_principal, "beadhive_hq_runtime"),
                "runtime_floor_path": str(tmp_path / "runtime-floor-two.json"),
                "runtime_initial_revision": grant_head,
            }
            second_plane = SqlControlPlane(second_settings, broker=Broker())
            second_identity = SimpleNamespace(
                frame_id="frame-2", host_id="host-2", instance_ref="vm-2"
            )
            second_registration = second_plane.publish_registration_evidence(
                manifest2, signing_key=str(second_key)
            )
            root = pymysql.connect(
                host="127.0.0.1",
                port=port,
                user="root",
                database="beadhive_hq_runtime",
                autocommit=True,
            )
            try:
                with root.cursor() as cursor:
                    cursor.execute(
                        f"SELECT request_id FROM {second_inbox} "
                        "WHERE kind='registration' AND payload_sha256=%s",
                        (second_registration.removeprefix("sha256:"),),
                    )
                    registration_id = cursor.fetchone()[0]
            finally:
                root.close()
            registered = subprocess.run(
                [
                    sys.executable,
                    str(worker),
                    str(receiver_settings_path),
                    second_principal,
                    registration_id,
                    "--registration",
                ],
                cwd=Path(__file__).parent.parent,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert registered.returncode == 0, registered.stderr
            before_admit = operator_plane.lifecycle(
                "admit", "frame-2", "check", expected_host_id="host-2"
            )
            assert before_admit["state"] == "pending"
            assert before_admit["consecutive_verified_beats"] == 0
            _pending_head, pending_desired, pending_observation = second_plane.read_eligibility(
                second_identity
            )
            git_equivalent_policy = {**policies["bh"], "config_head": "f" * 40}
            guard.validate_hive_policies({"bh": git_equivalent_policy})
            pending_facts = EligibilityFacts(pending_observation, pending_desired)
            pending_sql = eligible(manifest2, policies["bh"], pending_facts)
            assert not pending_sql.allowed
            assert (
                pending_sql.predicates
                == eligible(manifest2, git_equivalent_policy, pending_facts).predicates
            )
            for sequence in (1, 2, 3):
                second_lease = HeartbeatLease(
                    audience="fixture-fleet",
                    frame_id="frame-2",
                    holderIdentity="host-2",
                    instance_ref="vm-2",
                    key_id=fingerprint(second_public),
                    epoch=1,
                    config_revision="desired-1",
                    seq=sequence,
                    renewTime=datetime.now(UTC).isoformat(),
                    state_seen="pending",
                    release=desired2["release"],
                    report_digest="sha256:" + "2" * 64,
                    conformance={
                        "profile": "fixture",
                        "status": "conformant",
                        "checks": [{"id": "required", "status": "pass"}],
                    },
                )
                digest = second_plane.heartbeat(second_lease, signing_key=str(second_key))
                root = pymysql.connect(
                    host="127.0.0.1",
                    port=port,
                    user="root",
                    database="beadhive_hq_runtime",
                    autocommit=True,
                )
                try:
                    with root.cursor() as cursor:
                        cursor.execute(
                            f"SELECT request_id FROM {second_inbox} "
                            "WHERE kind='heartbeat' AND payload_sha256=%s",
                            (digest.removeprefix("sha256:"),),
                        )
                        beat_id = cursor.fetchone()[0]
                finally:
                    root.close()
                accepted = subprocess.run(
                    [
                        sys.executable,
                        str(worker),
                        str(receiver_settings_path),
                        second_principal,
                        beat_id,
                    ],
                    cwd=Path(__file__).parent.parent,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                assert accepted.returncode == 0, accepted.stderr
            admit_plan = operator_plane.lifecycle(
                "admit", "frame-2", "plan", expected_host_id="host-2"
            )
            assert admit_plan["consecutive_verified_beats"] == 3
            admitted = operator_plane.lifecycle(
                "admit",
                "frame-2",
                "apply",
                expected=grant_head,
                expected_host_id="host-2",
                expected_release=manifest2.release.digest,
                operator_key=str(key),
                confirm=True,
            )
            assert admitted["state"] == "active"
            _second_head, second_desired, second_observation = second_plane.read_eligibility(
                second_identity
            )
            assert second_desired["state"] == "active"
            assert second_observation.verified and second_observation.fresh
            active_facts = EligibilityFacts(second_observation, second_desired)
            active_sql = eligible(manifest2, policies["bh"], active_facts)
            assert active_sql.allowed
            assert (
                active_sql.predicates
                == eligible(manifest2, git_equivalent_policy, active_facts).predicates
            )
            stale_observation = second_plane.read_eligibility(
                second_identity, now=time.time() + second_lease.leaseDurationSeconds + 1
            )[2]
            assert not eligible(
                manifest2,
                policies["bh"],
                EligibilityFacts(stale_observation, second_desired),
            ).allowed
            with pytest.raises(ControlPlaneError):
                second_plane.read_eligibility(
                    SimpleNamespace(frame_id="frame-2", host_id="wrong-host", instance_ref="vm-2")
                )
            wrong_release = manifest2.model_copy(
                update={
                    "release": manifest2.release.model_copy(update={"digest": "sha256:" + "3" * 64})
                }
            )
            assert not eligible(wrong_release, policies["bh"], active_facts).allowed
            changed_caps = manifest2.model_copy(
                update={
                    "capabilities": manifest2.capabilities.model_copy(update={"max_sessions": 2})
                }
            )
            assert not eligible(changed_caps, policies["bh"], active_facts).allowed
            # A different active holder cannot take over while the incumbent
            # still has a live protected receipt and an unexpired hive lease.
            takeover = HostLease(
                host_id="host-2",
                label="candidate-two",
                epoch=2,
                adopted_at=now_stamp(now),
                expires_at=now_stamp(now + 1200),
            )
            takeover_id, takeover_sha, takeover_route, takeover_audience = (
                second_plane.propose_hive_lease(
                    "bh",
                    takeover,
                    expected=hive_revision,
                    operation="adopt",
                    signing_key=str(second_key),
                )
            )
            denied_takeover = subprocess.run(
                [
                    sys.executable,
                    str(worker),
                    str(receiver_settings_path),
                    second_principal,
                    takeover_id,
                    "--hive",
                ],
                cwd=Path(__file__).parent.parent,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert denied_takeover.returncode != 0
            assert "live incumbent is not evictable" in denied_takeover.stderr
            assert (
                second_plane._runtime_authority().read_public_result(
                    takeover_id,
                    request_sha256=takeover_sha,
                    principal=takeover_route,
                    audience=takeover_audience,
                    expected_revision=hive_revision,
                )
                is None
            )
            assert plane.read_hive_lease_record("bh", holder_identity="host-1") == (
                hive_revision,
                hive_lease,
            )
            # Publishing even identical canonical documents changes the
            # committed config HEAD. The old signed runtime projection cannot
            # authorize either frame until a separate operator publication
            # binds the exact new HEAD.
            from beadhive.hq_sql_config import SqlFleetConfigRevisionStore

            config_writer_settings = {
                **settings,
                "runtime": None,
                "publisher": _binding(tmp_path, port, "config_publisher", "beadhive_hq_config"),
            }
            config_writer = SqlFleetConfigRevisionStore(config_writer_settings, broker=Broker())
            republished = config_writer.publish_snapshot(documents, expected_revision=config_head)
            assert republished.commit_revision != config_head
            with pytest.raises(ValueError):
                second_plane.eligibility_authority_status()
            with pytest.raises(ValueError):
                second_plane.read_eligibility(second_identity)
            projection_head = operator_plane.renew(
                expected=admitted["revision"], operator_key=str(key)
            )
            assert projection_head != admitted["revision"]
            assert second_plane.read_eligibility(second_identity)[2].verified

            # Bind the same committed HQ to its existing SQL incarnations.
            # The old signed beat and registration remain recorded, but cannot
            # authorize a new lease until matching bound evidence is accepted.
            # A prior negative fixture left frame-1's latest floor on an
            # accepted nonconformant beat; publish a fresh conformant legacy
            # beat without changing that historical receipt.
            legacy_refresh = lease.model_copy(
                update={"seq": 3, "renewTime": datetime.now(UTC).isoformat()}
            )
            legacy_refresh_digest = plane.heartbeat(
                legacy_refresh, signing_key=str(frame_key)
            )
            refresh_reader = pymysql.connect(
                host="127.0.0.1", port=port, user="root",
                database="beadhive_hq_runtime", autocommit=True,
            )
            try:
                with refresh_reader.cursor() as cursor:
                    cursor.execute(
                        "SELECT request_id FROM hq_live_inbox_frame_a_1 "
                        "WHERE payload_sha256=%s",
                        (legacy_refresh_digest.removeprefix("sha256:"),),
                    )
                    legacy_refresh_id = cursor.fetchone()[0]
            finally:
                refresh_reader.close()
            refresh_result = subprocess.run(
                [sys.executable, str(worker), str(receiver_settings_path),
                 "frame_a", legacy_refresh_id],
                cwd=Path(__file__).parent.parent, capture_output=True, text=True,
                timeout=20,
            )
            assert refresh_result.returncode == 0, refresh_result.stderr
            from beadhive import config as config_facade
            from beadhive.beadyard_identity import DOCUMENT_PATH, new_document, parse_document

            identity_doc = FleetConfigDocument(DOCUMENT_PATH, new_document())
            owner = parse_document(identity_doc.content)
            bound_manifest = manifest.model_copy(update={"beadyard_id": owner})
            bound_manifest2 = manifest2.model_copy(update={"beadyard_id": owner})
            bound_documents = (
                fleet_document,
                FleetConfigDocument(
                    host_document.path,
                    json.dumps(bound_manifest.model_dump(mode="json", exclude_none=True)),
                ),
                FleetConfigDocument(
                    host2_document.path,
                    json.dumps(bound_manifest2.model_dump(mode="json", exclude_none=True)),
                ),
                identity_doc,
            )
            bound_config = config_writer.publish_snapshot(
                bound_documents, expected_revision=republished.commit_revision,
                explicit_adoption=True,
            )
            assert bound_config.beadyard_id == owner
            monkeypatch.setattr(
                config_facade, "load_host", lambda: {"hq": {"beadyard_id": owner}}
            )
            prior_frames = json.loads(json.dumps(operator.load()[1]["frames"]))
            projection_head = operator_plane.bind_beadyard(
                expected=projection_head, operator_key=str(key)
            )
            bound_frames = operator.load()[1]["frames"]
            for frame_id in ("frame-1", "frame-2"):
                prior = prior_frames[frame_id]["active"]
                current = bound_frames[frame_id]["active"]
                assert current["authority"] == {**prior["authority"], "beadyard_id": owner}
                assert {k: v for k, v in current.items() if k != "authority"} == {
                    k: v for k, v in prior.items() if k != "authority"
                }

            def identity_request_id(digest, *, inbox="hq_live_inbox_frame_a_1"):
                reader = pymysql.connect(
                    host="127.0.0.1", port=port, user="root",
                    database="beadhive_hq_runtime", autocommit=True,
                )
                try:
                    with reader.cursor() as cursor:
                        cursor.execute(
                            f"SELECT request_id FROM {inbox} WHERE payload_sha256=%s",
                            (digest.removeprefix("sha256:"),),
                        )
                        return cursor.fetchone()[0]
                finally:
                    reader.close()

            def receive_identity(request_id, *, principal="frame_a", kind="--hive"):
                arguments = [sys.executable, str(worker), str(receiver_settings_path),
                             principal, request_id]
                if kind is not None:
                    arguments.append(kind)
                return subprocess.run(
                    arguments,
                    cwd=Path(__file__).parent.parent, capture_output=True, text=True,
                    timeout=20,
                )

            new_hive_lease = HostLease(
                host_id="host-1", label="fixture", epoch=1,
                adopted_at=now_stamp(), expires_at=now_stamp(time.time() + 900),
            )
            new_hive_id, *_ = plane.propose_hive_lease(
                "bi", new_hive_lease, expected="", operation="adopt",
                signing_key=str(frame_key),
            )
            denied_old_beat = receive_identity(new_hive_id)
            assert denied_old_beat.returncode != 0
            assert "fresh conformant receipt" in denied_old_beat.stderr

            bridge_id, *_ = plane.propose_hive_lease(
                "bh", hive_lease, expected=hive_revision, operation="renew",
                signing_key=str(frame_key),
            )
            bridge = receive_identity(bridge_id)
            assert bridge.returncode == 0, bridge.stderr
            hive_revision = bridge.stdout.strip()
            assert plane.read_hive_lease_record("bh", holder_identity="host-1") == (
                hive_revision, hive_lease,
            )
            repeated_bridge_id, *_ = plane.propose_hive_lease(
                "bh", hive_lease, expected=hive_revision, operation="renew",
                signing_key=str(frame_key),
            )
            repeated_bridge = receive_identity(repeated_bridge_id)
            assert repeated_bridge.returncode != 0
            assert "fresh conformant receipt" in repeated_bridge.stderr

            bound_beat = lease.model_copy(
                update={"seq": 4, "domain": "beadhive/frame-heartbeat/v2", "beadyard_id": owner,
                        "renewTime": datetime.now(UTC).isoformat()}
            )
            bound_beat_digest = plane.heartbeat(bound_beat, signing_key=str(frame_key))
            bound_beat_result = receive_identity(
                identity_request_id(bound_beat_digest), kind=None
            )
            assert bound_beat_result.returncode == 0, bound_beat_result.stderr
            from uuid import uuid4

            foreign_beat = bound_beat.model_copy(
                update={"seq": 5, "beadyard_id": str(uuid4())}
            )
            frame_runtime = plane._runtime_authority()
            frame_head, _state, _crossref, _policies = frame_runtime.load_state()
            frame_binding = frame_runtime.load_frame_binding(expected_head=frame_head)
            foreign_id, _foreign_digest = frame_runtime.publish_inbox(
                "heartbeat", sign_heartbeat(foreign_beat, signing_key=str(frame_key)),
                binding=frame_binding, expected_head=frame_head, request_id=str(uuid4()),
            )
            denied_foreign_beat = receive_identity(foreign_id, kind=None)
            assert denied_foreign_beat.returncode != 0
            assert "differs from protected grant" in denied_foreign_beat.stderr
            denied_old_registration = receive_identity(new_hive_id)
            assert denied_old_registration.returncode != 0
            assert "current signed registration" in denied_old_registration.stderr
            bound_registration = plane.publish_registration_evidence(
                bound_manifest, signing_key=str(frame_key)
            )
            registered_bound = receive_identity(
                identity_request_id(bound_registration), kind="--registration"
            )
            assert registered_bound.returncode == 0, registered_bound.stderr
            registration_reader = pymysql.connect(
                host="127.0.0.1", port=port, user="root",
                database="beadhive_hq_runtime", autocommit=True,
            )
            try:
                with registration_reader.cursor() as cursor:
                    cursor.execute(
                        "SELECT request_id,manifest_json FROM hq_live_registrations "
                        "WHERE frame_id='frame-1' AND holder_identity='host-1' "
                        "AND instance_ref='vm-1' AND epoch=1"
                    )
                    bound_row = cursor.fetchone()
                    assert bound_row[0] == identity_request_id(bound_registration)
                    assert json.loads(bound_row[1]) == bound_manifest.model_dump(
                        mode="json", exclude_none=True
                    )
                    cursor.execute(
                        "SELECT status FROM hq_live_results WHERE request_id=%s",
                        (registration_id,),
                    )
                    assert cursor.fetchone() == ("accepted",)
            finally:
                registration_reader.close()
            adopted_bound = receive_identity(new_hive_id)
            assert adopted_bound.returncode == 0, adopted_bound.stderr
            assert plane.read_hive_lease_record("bi", holder_identity="host-1")[1] == (
                new_hive_lease
            )
            ordinary_renew_id, *_ = plane.propose_hive_lease(
                "bh", hive_lease, expected=hive_revision, operation="renew",
                signing_key=str(frame_key),
            )
            ordinary_renew = receive_identity(ordinary_renew_id)
            assert ordinary_renew.returncode == 0, ordinary_renew.stderr
            hive_revision = ordinary_renew.stdout.strip()

            second_registration_bound = second_plane.publish_registration_evidence(
                bound_manifest2, signing_key=str(second_key)
            )
            second_registered = receive_identity(
                identity_request_id(second_registration_bound, inbox=second_inbox),
                principal=second_principal, kind="--registration",
            )
            assert second_registered.returncode == 0, second_registered.stderr
            second_lease = second_lease.model_copy(
                update={"seq": 4, "domain": "beadhive/frame-heartbeat/v2", "beadyard_id": owner,
                        "renewTime": datetime.now(UTC).isoformat()}
            )
            second_bound_digest = second_plane.heartbeat(
                second_lease, signing_key=str(second_key)
            )
            second_bound_result = receive_identity(
                identity_request_id(second_bound_digest, inbox=second_inbox),
                principal=second_principal, kind=None,
            )
            assert second_bound_result.returncode == 0, second_bound_result.stderr
            projection_head = operator_plane.accept_observation(
                "frame-2", expected=projection_head, operator_key=str(key),
                holder_identity="host-2",
            )
            manifest2 = bound_manifest2
            manifest_identity.beadyard_id = owner
            second_identity.beadyard_id = owner
            assert second_plane.read_eligibility(second_identity)[2].verified
            assert operator_plane.lifecycle("cordon", "frame-1")["state"] == "active"
            cordoned = operator_plane.lifecycle(
                "cordon",
                "frame-1",
                "apply",
                expected=projection_head,
                expected_host_id="host-1",
                expected_release=lease.release.digest,
                operator_key=str(key),
                confirm=True,
            )
            assert cordoned["cordoned"] is True
            resumed = operator_plane.lifecycle(
                "resume",
                "frame-1",
                "apply",
                expected=cordoned["revision"],
                expected_host_id="host-1",
                expected_release=lease.release.digest,
                operator_key=str(key),
                confirm=True,
            )
            assert resumed["cordoned"] is False
            retired = operator_plane.lifecycle(
                "retire",
                "frame-1",
                "apply",
                expected=resumed["revision"],
                expected_host_id="host-1",
                expected_release=lease.release.digest,
                operator_key=str(key),
                confirm=True,
            )
            assert retired["state"] == "retired"
            revoked_head = retired["revision"]
            assert revoked_head != initial
            denied_after_revoke = subprocess.run(
                [sys.executable, str(worker), str(receiver_settings_path), "frame_a", request_id],
                cwd=Path(__file__).parent.parent,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert denied_after_revoke.returncode != 0
            assert "no current operator-granted incarnation" in denied_after_revoke.stderr
            with pytest.raises(ControlPlaneError):
                plane.read_eligibility(manifest_identity)

            renewed = operator_plane.renew(expected=revoked_head, operator_key=str(key), duration=1)
            assert renewed != revoked_head
            time.sleep(1.1)
            recovered = subprocess.run(
                [
                    sys.executable,
                    str(worker),
                    str(receiver_settings_path),
                    "frame_a",
                    request_id,
                    str(recovery_path),
                ],
                cwd=Path(__file__).parent.parent,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert recovered.returncode == 0, recovered.stderr
            assert recovered.stdout.strip() == published_digest
            with pytest.raises(ValueError):
                plane.authority_status()
            restored = operator_plane.renew(expected=renewed, operator_key=str(key), duration=3600)
            assert restored != renewed
            restored_head, restored_state, _ref, _policy = operator.load()
            assert restored_head == restored
            assert restored_state["expires_at"] > time.time()

            drain = operator_plane.lifecycle(
                "drain",
                "frame-2",
                "apply",
                expected=restored,
                expected_host_id="host-2",
                expected_release=manifest2.release.digest,
                deadline=time.time() + 300,
                operator_key=str(key),
                confirm=True,
            )
            assert drain["state"] == "draining" and drain["cordoned"]
            draining_head, draining_desired, draining_observation = second_plane.read_eligibility(
                second_identity
            )
            assert draining_head == drain["revision"]
            assert not eligible(
                manifest2,
                policies["bh"],
                EligibilityFacts(draining_observation, draining_desired),
            ).allowed
            drained_lease = second_lease.model_copy(
                update={
                    "seq": 5,
                    "state_seen": "drained",
                    "renewTime": datetime.now(UTC).isoformat(),
                }
            )
            drained_digest = second_plane.heartbeat(drained_lease, signing_key=str(second_key))
            root = pymysql.connect(
                host="127.0.0.1",
                port=port,
                user="root",
                database="beadhive_hq_runtime",
                autocommit=True,
            )
            try:
                with root.cursor() as cursor:
                    cursor.execute(
                        f"SELECT request_id FROM {second_inbox} "
                        "WHERE kind='heartbeat' AND payload_sha256=%s",
                        (drained_digest.removeprefix("sha256:"),),
                    )
                    drained_id = cursor.fetchone()[0]
            finally:
                root.close()
            drained_accept = subprocess.run(
                [
                    sys.executable,
                    str(worker),
                    str(receiver_settings_path),
                    second_principal,
                    drained_id,
                ],
                cwd=Path(__file__).parent.parent,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert drained_accept.returncode == 0, drained_accept.stderr
            drained_head = operator_plane.accept_observation(
                "frame-2",
                expected=drain["revision"],
                operator_key=str(key),
                holder_identity="host-2",
            )
            parked = operator_plane.lifecycle(
                "park",
                "frame-2",
                "apply",
                expected=drained_head,
                expected_host_id="host-2",
                expected_release=manifest2.release.digest,
                operator_key=str(key),
                confirm=True,
            )
            assert parked["state"] == "parked"
            resumed_two = operator_plane.lifecycle(
                "resume",
                "frame-2",
                "apply",
                expected=parked["revision"],
                expected_host_id="host-2",
                expected_release=manifest2.release.digest,
                operator_key=str(key),
                confirm=True,
            )
            assert resumed_two["state"] == "active" and not resumed_two["cordoned"]
            quarantined = operator_plane.lifecycle(
                "quarantine",
                "frame-2",
                "apply",
                expected=resumed_two["revision"],
                expected_host_id="host-2",
                expected_release=manifest2.release.digest,
                operator_key=str(key),
                confirm=True,
            )
            assert quarantined["state"] == "quarantined" and quarantined["cordoned"]
            with pytest.raises(ControlPlaneError, match="admit requires pending candidate"):
                operator_plane.lifecycle(
                    "admit",
                    "frame-2",
                    "apply",
                    expected=quarantined["revision"],
                    expected_host_id="host-2",
                    expected_release=manifest2.release.digest,
                    operator_key=str(key),
                    confirm=True,
                )
            retired_two = operator_plane.lifecycle(
                "retire",
                "frame-2",
                "apply",
                expected=quarantined["revision"],
                expected_host_id="host-2",
                expected_release=manifest2.release.digest,
                operator_key=str(key),
                confirm=True,
            )
            assert retired_two["state"] == "retired"
            with pytest.raises(ControlPlaneError, match="retired incarnation"):
                operator_plane.lifecycle(
                    "resume",
                    "frame-2",
                    "apply",
                    expected=retired_two["revision"],
                    expected_host_id="host-2",
                    expected_release="",
                    operator_key=str(key),
                    confirm=True,
                )
            with pytest.raises(ControlPlaneError):
                second_plane.heartbeat(
                    drained_lease.model_copy(update={"seq": 6}),
                    signing_key=str(second_key),
                )
            # A fixture-only branch restoration below the trusted authority
            # floor must fail both for this continuing host and for a newly
            # provisioned host pinned to the latest known signed HEAD.
            trusted_head = retired_two["revision"]
            _cli(
                tmp_path,
                port,
                f"USE beadhive_hq_runtime; CALL DOLT_RESET('--hard','{initial}')",
            )
            with pytest.raises(ValueError, match="rollback|amended|history fork"):
                SqlControlPlane(settings, broker=Broker()).authority_status()
            fresh_host_settings = {
                **settings,
                "runtime_floor_path": str(tmp_path / "fresh-runtime-floor.json"),
                "runtime_initial_revision": trusted_head,
            }
            with pytest.raises(ValueError, match="rollback|amended|history fork"):
                SqlControlPlane(fresh_host_settings, broker=Broker()).authority_status()
        finally:
            server.terminate()
            server.wait(timeout=10)
            with pytest.raises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=0.2)
