"""Laptop-off acceptance (bh-7dokf, epic bh-81nac): one SQL frame stays eligible for 48 h.

A single-operator, single-frame SQL factory on a private Dolt server, on a simulated clock (no
real waits). After one 7 d renew the operator does nothing: an unrelated fleet-config publish
(``work.validation_bypass``) at t+1 h must not fence the frame (bh-u67ve), operator status and
``check --min-remaining 3d`` must agree (bh-3h6al), and an operator-signed cordon/resume at
t+2 h must not shorten the 7 d expiry (bh-oywx8). The negative leg - a ``frame_policy`` edit
at t+3 h - still fences until a renew. The frame then beats on through t+48 h.

Signed liveness keeps the frame's own signed heartbeats as the liveness carrier, so every
read is on the same injectable clock; no receiver process is involved.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from beadhive import hq_authority_expiry
from beadhive import hq_authority_guard as guard
from beadhive.frame_eligibility import EligibilityFacts, eligible
from beadhive.host_heartbeat_core import HeartbeatLease
from beadhive.hosts import HostManifest
from beadhive.hq_control_plane import ControlPlaneError, SqlControlPlane
from beadhive.hq_hive_policy import project_hive_policies
from beadhive.hq_sql_config import SqlFleetConfigRevisionStore, _digest
from beadhive.hq_sql_runtime_schema import COMMITTED_SCHEMA, PROTECTED_LIVE_SCHEMA, inbox_ddl
from beadhive.hq_sql_signatures import canonical, fingerprint, sign_authority
from beadhive.modules.config.domain.ports import FleetConfigDocument, FleetConfigSnapshot
from harness.world import dolt_server_slot, free_port

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]

HOUR = 3600.0
DAY = 24 * HOUR
WEEK = 7 * DAY
RELEASE = {"id": "fixture", "digest": "sha256:" + "1" * 64}
FLEET = (
    "hq:\n  mode: dolt-server\nmanaged_repos:\n"
    "- provider: github\n  org: bee\n  repo: hive\n  prefix: bh\n"
    "  frame_policy:\n    config_revision: desired-1\n"
    "    requires: {max_sessions: 1}\n    evict_after_s: 900\n"
)
#: Frame-irrelevant: the 2026-10-04 per-hive override.
BYPASS_FLEET = FLEET.replace("prefix: bh\n", "prefix: bh\n  work: {validation_bypass: true}\n")
#: Frame-enforced: the hive's eviction policy.
POLICY_FLEET = BYPASS_FLEET.replace("evict_after_s: 900", "evict_after_s: 600")


class Broker:
    def get(self, _reference, *, deadline):
        assert time.monotonic() < deadline
        return "fixture-secret"


class SimClock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now


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


def _binding(port, user, database):
    return {
        "host": "127.0.0.1",
        "port": port,
        "database": database,
        "user": user,
        "tls_mode": "disabled",
        "credential": {"config_path": "/fixture/fnox.toml", "profile": "fixture", "key": "SQL"},
        "connect_timeout": 3,
        "read_timeout": 5,
        "write_timeout": 5,
        "operation_timeout": 20,
    }


def _key(path):
    private = Ed25519PrivateKey.generate()
    path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    )
    return (
        private.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )


def _root(port, database):
    import pymysql

    return pymysql.connect(
        host="127.0.0.1", port=port, user="root", database=database, autocommit=True
    )


def _start_server(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    port = free_port()
    (tmp_path / "server.yaml").write_text(
        f"data_dir: {data}\nlistener:\n  host: 127.0.0.1\n  port: {port}\n"
    )
    with (tmp_path / "server.log").open("w") as log:
        server = subprocess.Popen(
            [shutil.which("dolt"), "sql-server", "--config", str(tmp_path / "server.yaml")],
            cwd=tmp_path,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    end = time.monotonic() + 20
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return server, port
        except OSError:
            if server.poll() is not None or time.monotonic() >= end:
                server.terminate()
                raise AssertionError("owned Dolt server failed to start") from None
            time.sleep(0.1)


CONFIG_SCHEMA = """
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
CALL DOLT_COMMIT('-m','config schema','--author','Fixture <fixture@example.invalid>');
"""

GRANTS = (
    "CREATE USER 'frame_a'@'localhost' IDENTIFIED BY 'fixture-secret'; "
    "GRANT SELECT ON beadhive_hq_runtime.hq_authority TO 'frame_a'@'localhost'; "
    "GRANT SELECT ON beadhive_hq_runtime.hq_principal_registry TO 'frame_a'@'localhost'; "
    "GRANT SELECT,INSERT,UPDATE ON beadhive_hq_runtime.hq_live_inbox_frame_a_1 "
    "TO 'frame_a'@'localhost'; "
    "GRANT SELECT ON beadhive_hq_runtime.hq_live_results TO 'frame_a'@'localhost'; "
    "GRANT SELECT ON beadhive_hq_runtime.hq_live_hive_leases TO 'frame_a'@'localhost'; "
    "GRANT SELECT ON beadhive_hq_runtime.hq_live_public_observations "
    "TO 'frame_a'@'localhost'; "
    "GRANT SELECT ON beadhive_hq_config.hq_config_meta TO 'frame_a'@'localhost'; "
    "GRANT SELECT ON beadhive_hq_config.hq_config_documents TO 'frame_a'@'localhost'; "
    "GRANT SELECT ON beadhive_hq_config.hq_config_publications TO 'frame_a'@'localhost'; "
    "CREATE USER 'config_reader'@'localhost' IDENTIFIED BY 'fixture-secret'; "
    "GRANT SELECT ON beadhive_hq_config.hq_config_meta TO 'config_reader'@'localhost'; "
    "GRANT SELECT ON beadhive_hq_config.hq_config_documents TO 'config_reader'@'localhost'; "
    "GRANT SELECT ON beadhive_hq_config.hq_config_publications "
    "TO 'config_reader'@'localhost'; "
    "CREATE USER 'config_publisher'@'localhost' IDENTIFIED BY 'fixture-secret'; "
    "GRANT SELECT,UPDATE ON beadhive_hq_config.hq_config_meta "
    "TO 'config_publisher'@'localhost'; "
    "GRANT SELECT,INSERT,DELETE ON beadhive_hq_config.hq_config_documents "
    "TO 'config_publisher'@'localhost'; "
    "GRANT SELECT,INSERT ON beadhive_hq_config.hq_config_publications "
    "TO 'config_publisher'@'localhost'; "
    "GRANT SELECT ON beadhive_hq_config.dolt_status TO 'config_publisher'@'localhost'; "
    "GRANT EXECUTE ON PROCEDURE beadhive_hq_config.dolt_add TO 'config_publisher'@'localhost'; "
    "GRANT EXECUTE ON PROCEDURE beadhive_hq_config.dolt_commit "
    "TO 'config_publisher'@'localhost'; "
    "CREATE USER 'authority_writer'@'localhost' IDENTIFIED BY 'fixture-secret'; "
    "GRANT SELECT,UPDATE ON beadhive_hq_runtime.hq_authority TO 'authority_writer'@'localhost'; "
    "GRANT SELECT,INSERT ON beadhive_hq_runtime.hq_principal_registry "
    "TO 'authority_writer'@'localhost'; "
    "GRANT SELECT,INSERT ON beadhive_hq_runtime.hq_live_floors "
    "TO 'authority_writer'@'localhost'; "
    "GRANT SELECT ON beadhive_hq_runtime.hq_live_receipts TO 'authority_writer'@'localhost'; "
    "GRANT SELECT ON beadhive_hq_runtime.hq_live_public_observations "
    "TO 'authority_writer'@'localhost'; "
    "GRANT SELECT ON beadhive_hq_runtime.hq_live_registrations "
    "TO 'authority_writer'@'localhost'; "
    "GRANT SELECT ON beadhive_hq_runtime.dolt_status TO 'authority_writer'@'localhost'; "
    "GRANT EXECUTE ON PROCEDURE beadhive_hq_runtime.dolt_add "
    "TO 'authority_writer'@'localhost'; "
    "GRANT EXECUTE ON PROCEDURE beadhive_hq_runtime.dolt_commit "
    "TO 'authority_writer'@'localhost'; "
    "GRANT SELECT ON beadhive_hq_config.hq_config_meta TO 'authority_writer'@'localhost'; "
    "GRANT SELECT ON beadhive_hq_config.hq_config_documents TO 'authority_writer'@'localhost'; "
    "GRANT SELECT ON beadhive_hq_config.hq_config_publications "
    "TO 'authority_writer'@'localhost';"
)


def _seed_config(port, documents):
    root = _root(port, "beadhive_hq_config")
    try:
        with root.cursor() as cursor:
            cursor.execute("SELECT DOLT_HASHOF('HEAD')")
            schema_head = cursor.fetchone()[0]
            publication = "00000000-0000-0000-0000-000000000001"
            cursor.execute(
                "INSERT INTO hq_config_meta VALUES (1,1,%s,%s,1,%s,%s,%s)",
                (
                    "config-backend",
                    "config-generation",
                    publication,
                    _digest(documents),
                    len(documents),
                ),
            )
            for ordinal, document in enumerate(documents):
                cursor.execute(
                    "INSERT INTO hq_config_documents VALUES (%s,%s,%s,%s,%s)",
                    (
                        document.path,
                        ordinal,
                        "fleet" if document.path == "fleet.yaml" else "host",
                        document.content,
                        hashlib.sha256(document.content.encode()).hexdigest(),
                    ),
                )
            cursor.execute(
                "INSERT INTO hq_config_publications VALUES (%s,1,%s,%s,%s,%s)",
                (publication, "config-generation", schema_head, _digest(documents), len(documents)),
            )
            cursor.execute(
                "CALL DOLT_ADD('hq_config_meta','hq_config_documents','hq_config_publications')"
            )
            cursor.execute(
                "CALL DOLT_COMMIT('-m','seed config','--author',"
                "'Fixture <fixture@example.invalid>')"
            )
            cursor.execute("SELECT DOLT_HASHOF('HEAD')")
            return cursor.fetchone()[0]
    finally:
        root.close()


def _seed_authority(port, *, config_head, documents, manifest, frame_public, operator_key, now):
    expires = now + HOUR
    snapshot = FleetConfigSnapshot(
        "sql:config-backend", config_head, "config-generation", now, now + 30, documents
    )
    policies = project_hive_policies(snapshot, valid_until=expires, now=now)
    record = {
        "authority": {
            "frame_id": "frame-1",
            "holder_identity": "host-1",
            "instance_ref": "vm-1",
            "key_fingerprint": fingerprint(frame_public),
            "epoch": 1,
            "audience": "fixture-fleet",
            "config_revision": "desired-1",
            "candidate_expires_at": None,
        },
        "public_key": frame_public,
        "state": "active",
        "desired": {
            "declared": True,
            "release": RELEASE,
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
        "expires_at": expires,
        "frames": {
            "frame-1": {"active": record, "candidate": None, "retired": [], "epoch_floor": 1}
        },
    }
    guard.validate_state(state)
    signature = sign_authority(
        {
            "backend_identity": "runtime-backend",
            "generation": "runtime-generation",
            "revision": 1,
            "config_backend": "sql:config-backend",
            "config_generation": "config-generation",
            "config_head": config_head,
            "state": state,
            "hive_policies": policies,
        },
        signing_key=str(operator_key),
    )
    root = _root(port, "beadhive_hq_runtime")
    try:
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
                    fingerprint(frame_public),
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
            return initial
    finally:
        root.close()


def test_seven_day_authority_survives_48h_laptop_off(tmp_path):
    started = time.monotonic()
    clock = SimClock(time.time())
    operator_key, frame_key = tmp_path / "operator-key", tmp_path / "frame-key"
    operator_public, frame_public = _key(operator_key), _key(frame_key)
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
            "release": RELEASE,
            "capabilities": {
                "isolation": "container",
                "trust_zone": "self-hosted",
                "arch": "x86_64",
                "harnesses": ["claude"],
                "max_sessions": 1,
            },
        }
    )
    host = FleetConfigDocument(
        "hosts/host-1.yaml", json.dumps(manifest.model_dump(mode="json", exclude_none=True))
    )

    def documents(fleet):
        return (FleetConfigDocument("fleet.yaml", fleet), host)

    with dolt_server_slot(test_id="hq-laptop-off-acceptance"):
        server, port = _start_server(tmp_path)
        try:
            _cli(tmp_path, port, "CREATE DATABASE beadhive_hq_runtime")
            _cli(tmp_path, port, "USE beadhive_hq_runtime; " + "; ".join(COMMITTED_SCHEMA))
            _cli(tmp_path, port, "USE beadhive_hq_runtime; " + "; ".join(PROTECTED_LIVE_SCHEMA))
            _cli(tmp_path, port, "USE beadhive_hq_runtime; " + inbox_ddl("frame_a", 1))
            _cli(tmp_path, port, "CREATE DATABASE beadhive_hq_config")
            _cli(tmp_path, port, CONFIG_SCHEMA)
            config_head = _seed_config(port, documents(FLEET))
            initial = _seed_authority(
                port,
                config_head=config_head,
                documents=documents(FLEET),
                manifest=manifest,
                frame_public=frame_public,
                operator_key=operator_key,
                now=clock(),
            )
            _cli(tmp_path, port, GRANTS)
            settings = {
                "enabled": True,
                "liveness": "signed",
                "reader": _binding(port, "config_reader", "beadhive_hq_config"),
                "runtime": _binding(port, "frame_a", "beadhive_hq_runtime"),
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
                "runtime_operator_public_key": operator_public,
            }
            frame = SqlControlPlane(settings, broker=Broker(), clock=clock)
            operator_settings = {
                **settings,
                "runtime": None,
                "authority_writer": _binding(port, "authority_writer", "beadhive_hq_runtime"),
            }
            operator = SqlControlPlane(operator_settings, broker=Broker(), clock=clock)
            publisher = SqlFleetConfigRevisionStore(
                {
                    **settings,
                    "runtime": None,
                    "publisher": _binding(port, "config_publisher", "beadhive_hq_config"),
                },
                broker=Broker(),
                clock=clock,
            )
            seq = iter(range(1, 10_000))

            def beat():
                lease = HeartbeatLease(
                    audience="fixture-fleet",
                    frame_id="frame-1",
                    holderIdentity="host-1",
                    instance_ref="vm-1",
                    key_id=fingerprint(frame_public),
                    epoch=1,
                    config_revision="desired-1",
                    seq=next(seq),
                    renewTime=datetime.fromtimestamp(clock(), UTC).isoformat(),
                    state_seen="active",
                    release=RELEASE,
                    report_digest="sha256:" + "2" * 64,
                    conformance={"profile": "fixture", "status": "conformant", "checks": []},
                )
                frame.heartbeat(lease, signing_key=str(frame_key))

            def decision():
                beat()
                _head, desired, observation = frame.read_eligibility(manifest)
                _rev, _state, _crossref, policies = frame._runtime_authority().load_state()
                return desired, eligible(
                    manifest, policies["bh"], EligibilityFacts(observation, desired, at=clock())
                )

            def assert_eligible_and_healthy(step):
                desired, verdict = decision()
                assert verdict.allowed, (step, verdict)
                assert not desired["cordoned"], step
                ok, status, message = hq_authority_expiry.check(operator, min_remaining=3 * DAY)
                assert ok, (step, message)
                assert status["config_bound"] is True, step
                assert hq_authority_expiry.authority_status(frame)["config_bound"] is True

            def authority():
                revision, state, _crossref, _policies = operator._operator().load()
                return revision, state

            def advance_to(hours):
                assert clock.now <= t0 + hours * HOUR
                clock.now = t0 + hours * HOUR

            # t0: the operator's one action - a 7 d renew - then the laptop goes off.
            t0 = clock.now
            revision = operator.renew(
                expected=initial, operator_key=str(operator_key), duration=WEEK
            )
            revision, state = authority()
            seven_days = state["expires_at"]
            assert seven_days == pytest.approx(t0 + WEEK)
            assert_eligible_and_healthy("t0 after 7d renew")

            # t+1 h: the key-less publisher changes something no frame enforces.
            advance_to(1)
            bypass = publisher.publish_snapshot(
                documents(BYPASS_FLEET), expected_revision=config_head
            )
            assert bypass.commit_revision != config_head
            assert_eligible_and_healthy("t+1h after validation_bypass publish")
            assert authority() == (revision, state)  # no operator renewal happened

            # t+2 h: an operator-signed lifecycle action keeps the signed 7 d expiry.
            advance_to(2)
            cordoned = operator.lifecycle(
                "cordon",
                "frame-1",
                "apply",
                expected=revision,
                expected_host_id="host-1",
                operator_key=str(operator_key),
                confirm=True,
            )
            assert cordoned["cordoned"] is True
            revision, state = authority()
            assert state["expires_at"] == seven_days
            cordoned_desired, cordoned_verdict = decision()
            assert cordoned_desired["cordoned"] is True and not cordoned_verdict.allowed
            resumed = operator.lifecycle(
                "resume",
                "frame-1",
                "apply",
                expected=revision,
                expected_host_id="host-1",
                operator_key=str(operator_key),
                confirm=True,
            )
            assert resumed["cordoned"] is False and resumed["state"] == "active"
            revision, state = authority()
            assert state["expires_at"] == seven_days
            assert_eligible_and_healthy("t+2h after cordon/resume")

            # t+3 h (negative leg): a frame-enforced edit still fences until a renew.
            advance_to(3)
            publisher.publish_snapshot(
                documents(POLICY_FLEET), expected_revision=bypass.commit_revision
            )
            with pytest.raises(ControlPlaneError):
                decision()
            ok, status, message = hq_authority_expiry.check(operator, min_remaining=3 * DAY)
            assert not ok and status["config_bound"] is False
            assert message == "FAIL: authority is not bound to the latest config head"
            revision = operator.renew(
                expected=revision, operator_key=str(operator_key), duration=WEEK
            )
            revision, state = authority()
            seven_days = state["expires_at"]
            assert seven_days == pytest.approx(clock() + WEEK)
            assert_eligible_and_healthy("t+3h after renew of the policy edit")

            # Laptop off again: the frame beats on, nothing else happens, through t+48 h.
            for hours in (6, 12, 24, 36, 47, 48):
                advance_to(hours)
                assert_eligible_and_healthy(f"t+{hours}h")
            assert authority() == (revision, state)
            assert state["expires_at"] == seven_days
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
    assert time.monotonic() - started < 120, "laptop-off acceptance should stay modest"
