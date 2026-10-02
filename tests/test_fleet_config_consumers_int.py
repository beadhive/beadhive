"""Actual isolated Dolt/TLS proof through normal config and registry consumers."""

from __future__ import annotations

import hashlib
import shutil
import socket
import subprocess
import time

import pytest
from ruamel.yaml import YAML

from beadhive import config, frame_eligibility, gitworkspace, hq_control_plane, metadata, registry
from beadhive.hq_sql_config import SqlConfigError, SqlFleetConfigRevisionStore, _digest
from beadhive.hq_sql_transport import SqlTransportError
from beadhive.modules.config.domain.ports import FleetConfigDocument
from harness.world import dolt_server_slot, free_port
from test_hq_sql_config_int import _binding, _Broker, _certificates, _cli

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]


def test_normal_consumers_use_committed_revision_cas_and_never_fall_back(tmp_path, monkeypatch):
    _certificates(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    port = free_port()
    server_config = tmp_path / "server.yaml"
    server_config.write_text(
        f"data_dir: {data}\nlistener:\n  host: 127.0.0.1\n  port: {port}\n"
        f"  tls_key: {tmp_path / 'server.key'}\n"
        f"  tls_cert: {tmp_path / 'server.crt'}\n"
        "  require_secure_transport: false\n"
    )
    with dolt_server_slot(test_id="fleet-config-consumers"):
        with (tmp_path / "server.log").open("w") as log:
            server = subprocess.Popen(
                [shutil.which("dolt"), "sql-server", "--config", str(server_config)],
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
                        raise AssertionError("isolated Dolt fixture did not start") from None
                    time.sleep(0.1)

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
                CALL DOLT_COMMIT('-m','schema','--author','Fixture <fixture@example.invalid>');
                """,
            )
            initial_docs = (
                FleetConfigDocument(
                    "fleet.yaml",
                    "schema_version: 1\nhq:\n  mode: dolt-server\n"
                    "work:\n  validate_cmd: focused\nmanaged_repos: []\n",
                ),
            )
            digest = _digest(initial_docs)
            content_hash = hashlib.sha256(initial_docs[0].content.encode()).hexdigest()
            content_sql = (
                initial_docs[0]
                .content.replace("\\", "\\\\")
                .replace("'", "''")
                .replace("\n", "\\n")
            )
            _cli(
                tmp_path,
                port,
                f"""
                USE beadhive_hq_config;
                INSERT INTO hq_config_meta VALUES
                (1,1,'fixture-backend','fixture-generation',1,
                 '00000000-0000-0000-0000-000000000001','{digest}',1);
                INSERT INTO hq_config_documents VALUES
                ('fleet.yaml',0,'fleet',
                 '{content_sql}',
                 '{content_hash}');
                INSERT INTO hq_config_publications VALUES
                ('00000000-0000-0000-0000-000000000001',1,'fixture-generation',
                 DOLT_HASHOF('HEAD'),'{digest}',1);
                CALL DOLT_ADD('hq_config_meta','hq_config_documents','hq_config_publications');
                CALL DOLT_COMMIT('-m','seed','--author','Fixture <fixture@example.invalid>');
                """,
            )
            import pymysql

            root = pymysql.connect(
                host="127.0.0.1", port=port, user="root", database="beadhive_hq_config"
            )
            try:
                with root.cursor() as cursor:
                    cursor.execute("SELECT DOLT_HASHOF('HEAD')")
                    initial_revision = cursor.fetchone()[0]
            finally:
                root.close()
            _cli(
                tmp_path,
                port,
                """
                USE beadhive_hq_config;
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
                """,
            )
            settings = {
                "reader": _binding(tmp_path, port, "reader"),
                "publisher": _binding(tmp_path, port, "publisher"),
                "cache_ttl": 10,
                "backend_identity": "fixture-backend",
                "generation": "fixture-generation",
                "minimum_sequence": 1,
                "initial_revision": initial_revision,
                "floor_path": str(tmp_path / "floor.json"),
            }

            def store():
                return SqlFleetConfigRevisionStore(settings, broker=_Broker())

            first = store().load_snapshot()
            assert first.documents == initial_docs
            workspace = (
                FleetConfigDocument(
                    "workspace.toml",
                    "[[provider]]\nprovider = 'github'\nname = 'first'\n"
                    "path = 'first'\ncustom = 'kept'\n",
                ),
                FleetConfigDocument(
                    "workspace-extra.toml",
                    "[[provider]]\nprovider = 'gitlab'\nname = 'second'\n"
                    "path = 'second'\ncustom = 'kept-too'\n",
                ),
            )
            store().publish_snapshot(
                (*first.documents, *workspace), expected_revision=first.commit_revision
            )

            host_home = tmp_path / "host"
            host_home.mkdir()
            host_data = {
                "hq": {"sql": {"enabled": True, **settings}},
                "work": {"identity": {"name": "source-host", "email": "host@example.invalid"}},
            }
            with (host_home / "config.yaml").open("w") as stream:
                YAML().dump(host_data, stream)
            monkeypatch.setenv("BH_HOME", str(host_home))
            monkeypatch.setattr(
                hq_control_plane.SqlControlPlane,
                "config_store",
                lambda self, **kwargs: store(),
            )
            monkeypatch.setattr(metadata, "invalidate", lambda *a, **kw: None)
            config._config_store.clear_load_cache()

            effective = config.load()
            assert effective["work"]["identity"]["name"] == "source-host"
            assert effective["work"]["validate_cmd"] == "focused"
            assert [group.account for group in gitworkspace.groups(effective)] == [
                "first",
                "second",
            ]
            assert config.key_provenance()["work.identity.name"] == config.PROVENANCE_HOST
            assert config.key_provenance()["work.validate_cmd"] == config.PROVENANCE_FLEET
            assert (
                frame_eligibility.decision_for("fixture-unenrolled", hq_dir=host_home / "hq")
                is None
            )

            before = config.fleet_snapshot()
            assert config.set_value("delimiter", "/", scope=config.SCOPE_FLEET)["ok"]
            after = config.fleet_snapshot()
            assert after.commit_revision != before.commit_revision
            assert after.documents[1:] == workspace
            assert config.load()["delimiter"] == "/"

            registry.register("github", "fixture", "repo", "fx", "prototype")
            registered = config.fleet_snapshot()
            assert registered.commit_revision != after.commit_revision
            assert config.load()["managed_repos"][0]["prefix"] == "fx"
            assert registered.documents[1:] == workspace
            assert not (host_home / "hq").exists()

            with config._write_transaction(config.SCOPE_FLEET):
                stale = config.load_fleet()
                parent = config.fleet_snapshot()
                winner = store().publish_snapshot(
                    parent.documents, expected_revision=parent.commit_revision
                )
                stale["delimiter"] = "stale"
                with pytest.raises(SqlConfigError, match="expected configuration revision changed"):
                    config.save_fleet(stale)
            assert config.fleet_snapshot().commit_revision == winner.commit_revision
            assert config.load()["delimiter"] == "/"

            stale_hq = host_home / "hq"
            stale_hq.mkdir()
            (stale_hq / "fleet.yaml").write_text("delimiter: stale-git\n")
            server.terminate()
            server.wait(timeout=10)
            with pytest.raises((SqlConfigError, SqlTransportError)):
                config.load()
            unavailable = frame_eligibility.decision_for(
                "fixture-unenrolled", hq_dir=host_home / "hq"
            )
            assert dict(unavailable.predicates) == {"authority_available": False}
            assert (stale_hq / "fleet.yaml").read_text() == "delimiter: stale-git\n"
        finally:
            if server.poll() is None:
                server.terminate()
                server.wait(timeout=10)
