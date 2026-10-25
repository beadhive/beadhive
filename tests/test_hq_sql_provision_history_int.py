"""Persisted restore floors accept only verified next-incarnation provisioning on Dolt."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

import test_frame_release_upgrade as upgrade_tests
from beadhive.hq_framelease_contracts import ObservationAuthority
from beadhive.hq_sql_runtime import SqlRuntimeAuthority, SqlRuntimeError
from beadhive.hq_sql_runtime_schema import (
    COMMITTED_SCHEMA,
    PROTECTED_LIVE_SCHEMA,
    evidence_table,
    session_table,
)
from beadhive.hq_sql_session import evidence_ddl, session_ddl
from beadhive.hq_sql_session_provision import (
    provision_incarnation,
    provision_liveness_schema,
    resume_incarnation,
)
from beadhive.hq_sql_signatures import canonical, sign_authority, sql_principal
from test_hq_sql_session_int import DB, PASSWORD, _root, _server

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]
fixture = upgrade_tests.fixture


def _head(cursor):
    cursor.execute("SELECT DOLT_HASHOF('HEAD')")
    return cursor.fetchone()[0]


def _commit(cursor):
    cursor.execute("CALL DOLT_ADD('-A')")
    cursor.execute("CALL DOLT_COMMIT('-m','fixture history')")
    return _head(cursor)


def _initial(cursor, fixture, *, snapshot=None, session_route=False):
    for statement in COMMITTED_SCHEMA:
        cursor.execute(statement)
    for statement in PROTECTED_LIVE_SCHEMA:
        cursor.execute(statement)
    provision_liveness_schema(cursor)
    if session_route:
        authority = ObservationAuthority(**fixture.record["authority"])
        principal = sql_principal(authority)
        provision_incarnation(
            cursor,
            principal,
            authority.epoch,
            frame_address="127.0.0.1",
            password=PASSWORD,
            database=DB,
        )
        cursor.execute(
            "INSERT INTO hq_principal_registry VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                principal,
                authority.frame_id,
                authority.holder_identity,
                authority.instance_ref,
                authority.epoch,
                session_table(principal, authority.epoch),
                authority.key_fingerprint,
            ),
        )
    signed = {
        "backend_identity": "runtime-backend",
        "generation": "runtime-generation",
        "revision": fixture.state["revision"],
        "config_backend": snapshot.backend_identity if snapshot else "config-backend",
        "config_generation": snapshot.generation if snapshot else "config-generation",
        "config_head": snapshot.commit_revision if snapshot else "c" * 32,
        "state": fixture.state,
        "hive_policies": {},
    }
    body, policies = canonical(fixture.state), canonical({})
    cursor.execute(
        "INSERT INTO hq_authority VALUES (1,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            signed["backend_identity"],
            signed["generation"],
            signed["revision"],
            signed["config_backend"],
            signed["config_generation"],
            signed["config_head"],
            body,
            hashlib.sha256(body).hexdigest(),
            policies,
            hashlib.sha256(policies).hexdigest(),
            sign_authority(signed, signing_key=str(fixture.key)),
        ),
    )
    return _commit(cursor)


def _reader(tmp_path, fixture, head, name):
    return SqlRuntimeAuthority(
        {
            "runtime_backend_identity": "runtime-backend",
            "runtime_generation": "runtime-generation",
            "runtime_initial_revision": head,
            "runtime_operator_public_key": fixture.public,
            "runtime_floor_path": str(tmp_path / f"{name}.json"),
        },
        clock=lambda: 1000,
    )


def _next(fixture):
    authority = ObservationAuthority(**{**fixture.record["authority"], "epoch": 4})
    return sql_principal(authority), authority.epoch


def test_operator_frame_observer_floors_accept_only_provisioning_and_refuse_rollback(
    tmp_path, fixture, monkeypatch
):
    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "signed")
    with _server(tmp_path) as port:
        connection = _root(port)
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {DB}")
            cursor.execute(f"USE {DB}")
            initial = _initial(cursor, fixture)
            readers = [
                _reader(tmp_path, fixture, initial, name)
                for name in ("operator", "frame", "observer")
            ]
            for reader in readers:
                reader.verified_state_at(cursor, initial)
            principal, epoch = _next(fixture)
            provision_incarnation(
                cursor,
                principal,
                epoch,
                frame_address="127.0.0.1",
                password=PASSWORD,
                database=DB,
            )
            provisioned = _head(cursor)
            for reader in readers:
                reader.verified_state_at(cursor, provisioned)
                floor = json.loads(Path(reader.settings["runtime_floor_path"]).read_text())
                assert floor["head"] == provisioned and floor["sequence"] == 4
                with pytest.raises(SqlRuntimeError, match="rollback|fork"):
                    reader.verified_state_at(cursor, initial)
                for pin, value in (
                    ("runtime_backend_identity", "wrong"),
                    ("runtime_generation", "wrong"),
                ):
                    mismatch = SqlRuntimeAuthority(
                        {**reader.settings, pin: value}, clock=lambda: 1000
                    )
                    with pytest.raises(SqlRuntimeError, match="trust pin"):
                        mismatch.verified_state_at(cursor, provisioned)
            # A sibling provisioning commit is still a fork of the persisted floor.
            cursor.execute("CALL DOLT_CHECKOUT('-b','fork',%s)", (initial,))
            principal = "frame_" + "b" * 20
            for ddl in (session_ddl(principal, epoch), evidence_ddl(principal, epoch)):
                cursor.execute(ddl[0])
                cursor.execute(ddl[1])
                cursor.execute(ddl[2])
            fork = _commit(cursor)
            with pytest.raises(SqlRuntimeError, match="fork"):
                readers[0].verified_state_at(cursor, fork)
        connection.close()


@pytest.mark.parametrize("reserved_source", ["registered", "registry_only", "authority", "archive"])
def test_registered_cross_frame_missing_stamps_cannot_advance_floor(
    tmp_path, fixture, monkeypatch, reserved_source
):
    """Another frame's next epoch cannot repair an already-authorized route."""
    import copy

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from beadhive.hq_sql_signatures import fingerprint

    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "signed")
    other = copy.deepcopy(fixture.record)
    other["public_key"] = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    other["authority"].update(
        frame_id="other-frame",
        holder_identity=str(uuid4()),
        epoch=4,
        key_fingerprint=fingerprint(other["public_key"]),
    )
    authority = ObservationAuthority(**other["authority"])
    principal, epoch = sql_principal(authority), authority.epoch
    if reserved_source == "archive":
        archived = copy.deepcopy(other)
        other["authority"]["epoch"] = 5
        other["release_upgrade_history"] = [
            {
                "authority_revision": "reviewed-prior-head",
                "config_head": "c" * 32,
                "plan_sha256": "sha256:" + "a" * 64,
                "record": archived,
            }
        ]
    fixture.state["frames"]["other-frame"] = {
        "active": None,
        "candidate": other,
        "retired": [],
        "epoch_floor": other["authority"]["epoch"],
    }
    if reserved_source == "registry_only":
        # An older registered route may outlive the bounded signed archive.
        del fixture.state["frames"]["other-frame"]
    with _server(tmp_path) as port:
        connection = _root(port)
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {DB}")
            cursor.execute(f"USE {DB}")
            _initial(cursor, fixture)
            if reserved_source in {"registered", "registry_only"}:
                cursor.execute(
                    "INSERT INTO hq_principal_registry VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        principal,
                        authority.frame_id,
                        authority.holder_identity,
                        authority.instance_ref,
                        epoch,
                        session_table(principal, epoch),
                        authority.key_fingerprint,
                    ),
                )
            # Existing ignored route tables have no stamps at the persisted floor.
            for ddl in (session_ddl(principal, epoch), evidence_ddl(principal, epoch)):
                cursor.execute(ddl[0])
                cursor.execute(ddl[2])
            initial = (
                _commit(cursor)
                if reserved_source in {"registered", "registry_only"}
                else _head(cursor)
            )
            reader = _reader(tmp_path, fixture, initial, "operator")
            reader.verified_state_at(cursor, initial)
            for ddl in (session_ddl(principal, epoch), evidence_ddl(principal, epoch)):
                cursor.execute(ddl[1])
            head = _commit(cursor)
            with pytest.raises(SqlRuntimeError, match="rollback|amended"):
                reader.verified_state_at(cursor, head)
            assert (
                json.loads(Path(reader.settings["runtime_floor_path"]).read_text())["head"]
                == initial
            )
        connection.close()


@pytest.mark.parametrize("historical_registry_rows", [0, 4097])
def test_unregistered_canonical_future_stamps_are_only_inert_structure(
    tmp_path, fixture, monkeypatch, historical_registry_rows
):
    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "signed")
    with _server(tmp_path) as port:
        connection = _root(port)
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {DB}")
            cursor.execute(f"USE {DB}")
            initial = _initial(cursor, fixture)
            if historical_registry_rows:
                cursor.executemany(
                    "INSERT INTO hq_principal_registry VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    [
                        (
                            old_principal,
                            "retired-frame",
                            f"old-host-{index}",
                            "old-instance",
                            2,
                            session_table(old_principal, 2),
                            fixture.record["authority"]["key_fingerprint"],
                        )
                        for index in range(historical_registry_rows)
                        for old_principal in (f"frame_{index:020x}",)
                    ],
                )
                initial = _commit(cursor)
            cursor.execute("SELECT COUNT(*) FROM hq_principal_registry")
            assert cursor.fetchone()[0] == historical_registry_rows
            reader = _reader(tmp_path, fixture, initial, "operator")
            reader.verified_state_at(cursor, initial)
            principal, epoch = "frame_" + "e" * 20, 4
            assert principal != _next(fixture)[0]
            for ddl in (session_ddl(principal, epoch), evidence_ddl(principal, epoch)):
                for statement in ddl:
                    cursor.execute(statement)
            head = _commit(cursor)
            reader.verified_state_at(cursor, head)
            cursor.execute(
                "SELECT principal FROM hq_principal_registry WHERE principal=%s", (principal,)
            )
            assert not cursor.fetchall()
            cursor.execute("SELECT state_json FROM hq_authority")
            assert json.loads(cursor.fetchone()[0]) == fixture.state
        connection.close()


@pytest.mark.parametrize(
    "unsafe",
    [
        "mixed",
        "registry",
        "authority",
        "trigger",
        "metadata",
        "reverted",
        "partial",
        "wrong_epoch",
        "unreviewed",
        "merge",
        "view",
        "removed",
    ],
)
def test_same_sequence_unsafe_history_remains_refused(tmp_path, fixture, monkeypatch, unsafe):
    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "signed")
    with _server(tmp_path) as port:
        connection = _root(port)
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {DB}")
            cursor.execute(f"USE {DB}")
            initial = _initial(cursor, fixture)
            reader = _reader(tmp_path, fixture, initial, "operator")
            reader.verified_state_at(cursor, initial)
            if unsafe == "merge":
                cursor.execute("CALL DOLT_CHECKOUT('-b','side',%s)", (initial,))
                cursor.execute("CREATE TABLE unsafe_change (id INT PRIMARY KEY)")
                _commit(cursor)
                cursor.execute("DROP TABLE unsafe_change")
                _commit(cursor)
                cursor.execute("CALL DOLT_CHECKOUT('main')")
            principal, epoch = _next(fixture)
            if unsafe == "wrong_epoch":
                epoch += 1
            elif unsafe == "unreviewed":
                principal = "arbitrary_principal"
            for ddl in (session_ddl(principal, epoch), evidence_ddl(principal, epoch)):
                cursor.execute(ddl[0])
                if unsafe != "partial" or ddl[0].split()[2].endswith("_session"):
                    cursor.execute(
                        ddl[1].replace("UTC_TIMESTAMP(6)", "UTC_TIMESTAMP(3)")
                        if unsafe == "trigger"
                        else ddl[1]
                    )
                cursor.execute(ddl[2])
            if unsafe in ("mixed", "reverted"):
                cursor.execute("CREATE TABLE unsafe_change (id INT PRIMARY KEY)")
            elif unsafe == "registry":
                cursor.execute(
                    "INSERT INTO hq_principal_registry VALUES "
                    "('unsafe','frame','host','vm',4,'unsafe','key')"
                )
            elif unsafe == "authority":
                cursor.execute("UPDATE hq_authority SET config_head=%s", ("d" * 32,))
            elif unsafe == "metadata":
                initial = _commit(cursor)
                reader.verified_state_at(cursor, initial)
                cursor.execute("SET @@SESSION.sql_mode='STRICT_TRANS_TABLES'")
                cursor.execute(f"DROP TRIGGER {session_table(principal, epoch)}_stamp")
                cursor.execute(session_ddl(principal, epoch)[1])
            elif unsafe == "removed":
                initial = _commit(cursor)
                reader.verified_state_at(cursor, initial)
                cursor.execute(f"DROP TRIGGER {session_table(principal, epoch)}_stamp")
            elif unsafe == "view":
                cursor.execute("CREATE VIEW unrelated_view AS SELECT 1 AS value")
            head = _commit(cursor)
            if unsafe == "merge":
                cursor.execute("CALL DOLT_MERGE('--no-ff','side','-m','fixture merge')")
                head = _head(cursor)
            if unsafe == "reverted":
                cursor.execute("DROP TABLE unsafe_change")
                head = _commit(cursor)
            with pytest.raises(ValueError):
                reader.verified_state_at(cursor, head)
            assert (
                json.loads(Path(reader.settings["runtime_floor_path"]).read_text())["head"]
                == initial
            )
        connection.close()


def test_interrupted_provisioning_resumes_without_rewriting_evidence(tmp_path):
    with _server(tmp_path) as port:
        connection = _root(port)
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {DB}")
            cursor.execute(f"USE {DB}")
            for statement in COMMITTED_SCHEMA:
                cursor.execute(statement)
            provision_liveness_schema(cursor)
            principal, epoch = "frame_" + "a" * 20, 4
            # Simulate an interruption after the session table's CREATE/trigger/seed.
            for statement in session_ddl(principal, epoch):
                cursor.execute(statement)
            assert (
                resume_incarnation(
                    cursor,
                    principal,
                    epoch,
                    frame_address="127.0.0.1",
                    password=PASSWORD,
                    database=DB,
                )
                == []
            )
            head = _head(cursor)
            cursor.execute(
                f"UPDATE {evidence_table(principal, epoch)} "
                "SET status='conformant',report_digest='preserve'"
            )
            cursor.execute(f"UPDATE {session_table(principal, epoch)} SET epoch=4")
            cursor.execute(f"SELECT * FROM {evidence_table(principal, epoch)}")
            evidence = cursor.fetchall()
            cursor.execute(f"SELECT * FROM {session_table(principal, epoch)}")
            session = cursor.fetchall()
            assert (
                resume_incarnation(
                    cursor,
                    principal,
                    epoch,
                    frame_address="127.0.0.1",
                    password="different retry password",
                    database=DB,
                )
                == []
            )
            assert _head(cursor) == head
            cursor.execute(f"SELECT * FROM {evidence_table(principal, epoch)}")
            assert cursor.fetchall() == evidence
            cursor.execute(f"SELECT * FROM {session_table(principal, epoch)}")
            assert cursor.fetchall() == session
        connection.close()


@pytest.mark.parametrize("unsafe", ["default", "check", "index", "trigger", "dirty"])
def test_resume_refuses_lookalike_schema_and_unrelated_working_changes(tmp_path, unsafe):
    from beadhive.hq_sql_session import SessionError

    with _server(tmp_path) as port:
        connection = _root(port)
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {DB}")
            cursor.execute(f"USE {DB}")
            for statement in COMMITTED_SCHEMA:
                cursor.execute(statement)
            provision_liveness_schema(cursor)
            principal, epoch = "frame_" + "a" * 20, 4
            ddl = session_ddl(principal, epoch)
            table = session_table(principal, epoch)
            cursor.execute(ddl[0])
            if unsafe == "default":
                cursor.execute(f"ALTER TABLE {table} ALTER COLUMN epoch SET DEFAULT 4")
            elif unsafe == "check":
                cursor.execute(f"ALTER TABLE {table} ADD CHECK (epoch=4)")
            elif unsafe == "index":
                cursor.execute(f"CREATE INDEX extra_index ON {table} (epoch)")
            elif unsafe == "trigger":
                cursor.execute(ddl[1].replace("UTC_TIMESTAMP(6)", "NULL"))
            else:
                cursor.execute("CREATE TABLE unrelated_change (id int PRIMARY KEY)")
            head = _head(cursor)
            with pytest.raises(SessionError):
                resume_incarnation(
                    cursor,
                    principal,
                    epoch,
                    frame_address="127.0.0.1",
                    password=PASSWORD,
                    database=DB,
                )
            assert _head(cursor) == head
            cursor.execute("SELECT user FROM mysql.user WHERE user=%s", (principal,))
            assert not cursor.fetchall()
        connection.close()


@pytest.mark.parametrize("preparation_failure", [None, "lost_ack", "concurrent"])
def test_public_cli_provisions_and_rotates_session_route_with_persisted_floors(
    tmp_path, fixture, monkeypatch, preparation_failure
):
    from typer.testing import CliRunner

    from beadhive import host_cli, hq_operator_settings
    from beadhive.host_lease_contracts import EpochFence, HostLease, now_stamp
    from beadhive.hosts import HostManifest
    from beadhive.hq_control_plane import SqlControlPlane
    from beadhive.hq_sql_config import SqlFleetConfigRevisionStore
    from beadhive.hq_sql_placement import lease_body, placement_witness
    from beadhive.hq_sql_session import EvidenceReport, SessionRenewer, publish_evidence
    from beadhive.hq_sql_transport import connect
    from beadhive.modules.config.domain.ports import FleetConfigDocument
    from test_hq_sql_config_int import _binding, _Broker, _empty_config_server

    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "signed")
    fixture.record = upgrade_tests.make_active(fixture)
    with _empty_config_server(tmp_path) as (port, parent, config_settings):
        store = SqlFleetConfigRevisionStore(config_settings, broker=_Broker(), clock=lambda: 1000)
        documents = tuple(
            FleetConfigDocument(
                "fleet.yaml",
                "schema_version: 1\nhq: {mode: dolt-server}\nmanaged_repos:\n"
                "  - provider: github\n    org: fixture\n    repo: hive-bh\n"
                "    prefix: bh\n    kind: org-native\n"
                "    frame_policy:\n      config_revision: policy-v2\n"
                "      requires: {max_sessions: 1}\n      evict_after_s: 301\n",
            )
            if row.path == "fleet.yaml"
            else FleetConfigDocument(
                row.path,
                json.dumps({**fixture.manifest, "release": fixture.record["desired"]["release"]}),
            )
            if row.path.startswith("hosts/")
            else row
            for row in fixture.snapshot.documents
        )
        receipt = store.initialize_snapshot(
            documents,
            expected_schema_parent=parent,
            publication_id=str(uuid4()),
        )
        config_settings["initial_revision"] = receipt.committed_revision
        snapshot = store.load_snapshot()
        connection = _root(port)
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {DB}")
            cursor.execute(f"USE {DB}")
            initial = _initial(cursor, fixture, snapshot=snapshot, session_route=True)
            lease = HostLease(
                host_id=fixture.request["host_id"],
                label="fixture",
                epoch=12,
                adopted_at=now_stamp(1000),
                expires_at=now_stamp(1800),
            )
            body = lease_body(fixture.record["authority"], lease)
            lease_revision = "a" * 64
            cursor.execute(
                "INSERT INTO hq_live_hive_leases VALUES (%s,%s,%s,%s,%s)",
                (
                    "bh",
                    lease_revision,
                    body,
                    str(uuid4()),
                    placement_witness("bh", lease_revision, body),
                ),
            )
            cursor.execute("CREATE USER 'provisioner'@'localhost' IDENTIFIED BY 'fixture-secret'")
            # Exactly the documented server-local binding (docs/HQ.md#runtime-sql-privileges),
            # never ALL: a broad fixture grant hid the missing UPDATE delegation (bh-t759y).
            # UPDATE ON <runtime>.* WITH GRANT OPTION is granted after the gap is shown.
            config_db = config_settings["reader"]["database"]
            for grant in (
                f"GRANT SELECT ON `{DB}`.* TO {{u}} WITH GRANT OPTION",
                f"GRANT UPDATE ON `{DB}`.`hq_authority` TO {{u}}",
                f"GRANT INSERT ON `{DB}`.`hq_principal_registry` TO {{u}}",
                f"GRANT INSERT ON `{DB}`.`hq_live_floors` TO {{u}}",
                f"GRANT EXECUTE ON PROCEDURE `{DB}`.`dolt_add` TO {{u}}",
                f"GRANT EXECUTE ON PROCEDURE `{DB}`.`dolt_commit` TO {{u}}",
                f"GRANT CREATE, TRIGGER, INSERT ON `{DB}`.* TO {{u}}",
                f"GRANT SELECT ON `{config_db}`.* TO {{u}} WITH GRANT OPTION",
                "GRANT CREATE USER ON *.* TO {u}",
                "GRANT SELECT ON `mysql`.* TO {u}",
            ):
                cursor.execute(grant.format(u="'provisioner'@'localhost'"))
            settings = {
                **config_settings,
                "enabled": True,
                "runtime": None,
                "authority_writer": {**_binding(tmp_path, port, "provisioner"), "database": DB},
                "runtime_backend_identity": "runtime-backend",
                "runtime_generation": "runtime-generation",
                "runtime_initial_revision": initial,
                "runtime_operator_public_key": fixture.public,
                "runtime_floor_path": str(tmp_path / "operator-floor.json"),
            }
            plane = SqlControlPlane(settings, broker=_Broker(), clock=lambda: 1000)
            readers = [
                SqlRuntimeAuthority(
                    {**settings, "runtime_floor_path": str(tmp_path / f"{name}-floor.json")},
                    clock=lambda: 1000,
                )
                for name in ("frame", "observer")
            ]
            for reader in readers:
                reader.verified_state_at(cursor, initial)
            monkeypatch.setattr(hq_operator_settings, "select_plane", lambda _path: plane)
            runner = CliRunner()
            arguments = [
                "frame",
                "--expected-revision",
                initial,
                "--expected-host-id",
                fixture.request["host_id"],
                "--expected-epoch",
                "3",
                "--expected-release",
                fixture.request["old_release"],
                "--expected-config-head",
                snapshot.commit_revision,
                "--release-id",
                fixture.request["release"]["id"],
                "--release-digest",
                fixture.request["release"]["digest"],
                "--profile",
                fixture.request["profile"],
                "--config-revision",
                fixture.request["config_revision"],
                "--candidate-expires-at",
                "1600",
            ]

            def invoke(action, extra=(), *, stdin=None):
                output = runner.invoke(
                    host_cli.app, ["release-upgrade", action, *arguments, *extra], input=stdin
                )
                assert output.exit_code == 0, output.output
                return json.loads(output.stdout)

            preparation = invoke("prepare-config")
            assert not preparation["applied"] and not preparation["already_prepared"]
            wrong = runner.invoke(
                host_cli.app,
                [
                    "release-upgrade",
                    "prepare-config",
                    *arguments,
                    "--confirm",
                    "--plan-sha256",
                    "wrong",
                ],
            )
            assert wrong.exit_code == 1
            assert store.load_snapshot().commit_revision == snapshot.commit_revision
            stale = list(arguments)
            stale[2] = "stale-runtime-head"
            refused = runner.invoke(host_cli.app, ["release-upgrade", "prepare-config", *stale])
            assert refused.exit_code == 1
            assert store.load_snapshot().commit_revision == snapshot.commit_revision
            if preparation_failure:
                original_publish = SqlFleetConfigRevisionStore.publish_snapshot

                def disrupted_publish(store, documents, **kwargs):
                    published = original_publish(store, documents, **kwargs)
                    if preparation_failure == "lost_ack":
                        from beadhive.hq_sql_config import PublicationUnknown

                        raise PublicationUnknown(str(uuid4()), kwargs["expected_revision"])
                    state = {**fixture.state, "revision": 5}
                    encoded = canonical(state)
                    signed = {
                        "backend_identity": "runtime-backend",
                        "generation": "runtime-generation",
                        "revision": 5,
                        "config_backend": snapshot.backend_identity,
                        "config_generation": snapshot.generation,
                        "config_head": snapshot.commit_revision,
                        "state": state,
                        "hive_policies": {},
                    }
                    cursor.execute(
                        "UPDATE hq_authority SET revision=5,state_json=%s,state_sha256=%s,"
                        "operator_signature=%s",
                        (
                            encoded,
                            hashlib.sha256(encoded).hexdigest(),
                            sign_authority(signed, signing_key=str(fixture.key)),
                        ),
                    )
                    _commit(cursor)  # A separate, authenticated fixture writer races publication.
                    return published

                with monkeypatch.context() as disruption:
                    disruption.setattr(
                        SqlFleetConfigRevisionStore, "publish_snapshot", disrupted_publish
                    )
                    failed = runner.invoke(
                        host_cli.app,
                        [
                            "release-upgrade",
                            "prepare-config",
                            *arguments,
                            "--confirm",
                            "--plan-sha256",
                            preparation["plan_sha256"],
                        ],
                    )
                    assert failed.exit_code == 1
                current = invoke("check")
                assert current["canonical_release"] == fixture.request["release"]
                assert current["canonical_config_head"] != snapshot.commit_revision
                assert current["canonical_config_head"] in failed.output
                assert "run release-upgrade check and review prepare-config again" in failed.output
                if preparation_failure == "concurrent":
                    arguments[2] = current["revision"]
                arguments[arguments.index("--expected-config-head") + 1] = current[
                    "canonical_config_head"
                ]
                preparation = invoke("prepare-config")
                assert preparation["already_prepared"]
            prepared = invoke(
                "prepare-config", ("--confirm", "--plan-sha256", preparation["plan_sha256"])
            )
            assert prepared["applied"]
            arguments[arguments.index("--expected-config-head") + 1] = prepared["config_head"]
            prepared_snapshot = store.load_snapshot()
            assert [row.path for row in prepared_snapshot.documents] == [
                row.path for row in snapshot.documents
            ]
            for before, after in zip(snapshot.documents, prepared_snapshot.documents, strict=True):
                if before.path.startswith("hosts/"):
                    prior, updated = json.loads(before.content), json.loads(after.content)
                    assert updated == {**prior, "release": fixture.request["release"]}
                else:
                    assert before == after
            for reader in readers:
                reader.verified_state_at(cursor, _head(cursor))
            reviewed = invoke("plan")
            assert reviewed["route"] == "session" and not reviewed["provisioned"]
            # Without the UPDATE delegation the preflight names it and provision refuses
            # before writing anything (bh-t759y; 0.25.3 reported ready, then failed midway).
            update_grant = f"UPDATE ON `{DB}`.* WITH GRANT OPTION"
            assert not reviewed["preflight"]["ready"]["provision"]
            assert any(update_grant in item for item in reviewed["preflight"]["provision"])
            assert reviewed["preflight"]["ready"]["apply"]
            provision_flags = [
                "--confirm",
                "--plan-sha256",
                reviewed["plan_sha256"],
                "--frame-address",
                "127.0.0.1",
                "--password-stdin",
            ]
            before = _head(cursor)
            gap = runner.invoke(
                host_cli.app,
                ["release-upgrade", "provision", *arguments, *provision_flags],
                input="fixture-secret\n",
            )
            assert gap.exit_code == 1 and update_grant in gap.output
            assert "provisioner" not in gap.output and "fixture-secret" not in gap.output
            assert _head(cursor) == before
            assert not invoke("plan")["provisioning_incomplete"]
            if preparation_failure == "lost_ack":
                # Reproduce the 0.25.3 partial state: tables, triggers and an account with no
                # table grants, uncommitted schemas, runtime HEAD unmoved. Supported resume
                # after the grant completes it without manual SQL.
                import pymysql

                from beadhive.hq_sql_capabilities import denied

                restricted = pymysql.connect(
                    host="127.0.0.1", port=port, user="provisioner",
                    password="fixture-secret", database=DB, autocommit=True,
                )  # fmt: skip
                with restricted.cursor() as partial, pytest.raises(pymysql.MySQLError) as caught:
                    resume_incarnation(
                        partial,
                        reviewed["principal"],
                        reviewed["new_epoch"],
                        frame_address="127.0.0.1",
                        password="fixture-secret",
                        database=DB,
                    )
                assert denied(caught.value)
                restricted.close()
                assert _head(cursor) == before
                partial_plan = invoke("plan")
                assert partial_plan["plan_sha256"] == reviewed["plan_sha256"]
            cursor.execute(
                f"GRANT UPDATE ON `{DB}`.* TO 'provisioner'@'localhost' WITH GRANT OPTION"
            )
            reviewed = invoke("plan")
            assert reviewed["preflight"]["ready"]["provision"], reviewed["preflight"]
            if preparation_failure is None:
                from beadhive import hq_sql_session_provision
                from beadhive.hq_sql_session import SessionError

                def interrupted_provision(cursor, principal, epoch, **kwargs):
                    for statement in session_ddl(principal, epoch):
                        cursor.execute(statement)
                    raise SessionError("fixture interruption after session trigger/seed")

                with monkeypatch.context() as interruption:
                    interruption.setattr(
                        hq_sql_session_provision, "resume_incarnation", interrupted_provision
                    )
                    interrupted = runner.invoke(
                        host_cli.app,
                        [
                            "release-upgrade",
                            "provision",
                            *arguments,
                            "--confirm",
                            "--plan-sha256",
                            reviewed["plan_sha256"],
                            "--frame-address",
                            "127.0.0.1",
                            "--password-stdin",
                        ],
                        input="fixture-secret\n",
                    )
                    assert interrupted.exit_code == 1
                resumed_plan = invoke("plan")
                assert resumed_plan["provisioning_incomplete"]
                assert resumed_plan["revision"] == reviewed["revision"]
                assert resumed_plan["plan_sha256"] == reviewed["plan_sha256"]
            provisioned = invoke(
                "provision",
                (
                    "--confirm",
                    "--plan-sha256",
                    reviewed["plan_sha256"],
                    "--frame-address",
                    "127.0.0.1",
                    "--password-stdin",
                ),
                stdin="fixture-secret\n",
            )
            assert provisioned["replan_required"] and provisioned["route"] == "session"
            for reader in readers:
                reader.verified_state_at(cursor, provisioned["revision"])
            arguments[2] = provisioned["revision"]
            reviewed = invoke("plan")
            resumed = invoke(
                "provision",
                (
                    "--confirm",
                    "--plan-sha256",
                    reviewed["plan_sha256"],
                    "--frame-address",
                    "127.0.0.1",
                    "--password-stdin",
                ),
                stdin="retry does not reset password\n",
            )
            assert not resumed["replan_required"]
            if preparation_failure is None:
                target_session = session_table(reviewed["principal"], reviewed["new_epoch"])
                account = f"'{reviewed['principal']}'@'127.0.0.1'"
                for drift in ("schema", "grant"):
                    if drift == "schema":
                        cursor.execute(
                            f"ALTER TABLE {target_session} ALTER COLUMN epoch SET DEFAULT 4"
                        )
                    else:
                        cursor.execute(f"GRANT UPDATE ON `{DB}`.`hq_authority` TO {account}")
                    before = _head(cursor)
                    floors = [
                        Path(reader.settings["runtime_floor_path"]).read_text()
                        for reader in readers
                    ]
                    refused = runner.invoke(
                        host_cli.app,
                        [
                            "release-upgrade",
                            "apply",
                            *arguments,
                            "--confirm",
                            "--plan-sha256",
                            reviewed["plan_sha256"],
                            "--operator-key",
                            str(fixture.key),
                        ],
                    )
                    assert refused.exit_code == 1, (drift, refused.output)
                    assert _head(cursor) == before
                    cursor.execute("SELECT revision FROM hq_authority")
                    assert cursor.fetchone()[0] == fixture.state["revision"]
                    cursor.execute(
                        "SELECT revision,lease_json FROM hq_live_hive_leases WHERE prefix='bh'"
                    )
                    assert cursor.fetchone() == (lease_revision, body)
                    assert [
                        Path(reader.settings["runtime_floor_path"]).read_text()
                        for reader in readers
                    ] == floors
                    # Restore isolated fixture drift before testing the successful cutover.
                    if drift == "schema":
                        cursor.execute(
                            f"ALTER TABLE {target_session} ALTER COLUMN epoch DROP DEFAULT"
                        )
                    else:
                        cursor.execute(f"REVOKE UPDATE ON `{DB}`.`hq_authority` FROM {account}")
            rotated = invoke(
                "apply",
                (
                    "--confirm",
                    "--plan-sha256",
                    reviewed["plan_sha256"],
                    "--operator-key",
                    str(fixture.key),
                ),
            )
            checked = invoke("check")
            assert checked["authority"]["epoch"] == 4 and checked["route"] == "session"
            assert checked["inbox_table"] == rotated["session_table"]
            assert checked["release"] == fixture.request["release"]
            assert checked["authority"]["holder_identity"] == fixture.request["host_id"]
            for reader in readers:
                reader.verified_state_at(cursor, checked["revision"])
            frame_settings = {
                **settings,
                "authority_writer": None,
                "publisher": None,
                "reader": {
                    **_binding(tmp_path, port, rotated["principal"]),
                    "database": settings["reader"]["database"],
                },
                "runtime": {**_binding(tmp_path, port, rotated["principal"]), "database": DB},
                "runtime_floor_path": str(tmp_path / "frame-floor.json"),
                "liveness": "signed",
            }
            if preparation_failure is None:
                from beadhive import heartbeat_sender, hq_control_plane, hq_sql_transport

                fixture_home = tmp_path / "activation-home"
                fixture_home.mkdir()
                old_settings = {
                    **frame_settings,
                    "reader": settings["reader"],
                    "runtime": {
                        **frame_settings["runtime"],
                        "user": sql_principal(ObservationAuthority(**fixture.record["authority"])),
                    },
                }
                local_host = {
                    "hq": {"sql": old_settings},
                    "host": {"frame_id": "frame"},
                }
                (fixture_home / "config.yaml").write_text(json.dumps(local_host))
                with monkeypatch.context() as activation:
                    activation.setenv("BH_HOME", str(fixture_home))
                    for name in ("BH_CONFIG", "WS_CONFIG", "BH_HQ", "WS_HQ"):
                        activation.delenv(name, raising=False)

                    def secret(_self, reference, *, deadline):
                        assert reference["key"] in {"SQL", "NEW_SQL_0243"}
                        return "fixture-secret"

                    activation.setattr(hq_sql_transport.FnoxBroker, "get", secret)

                    def attach(*, bootstrap, **kwargs):
                        attached = SqlFleetConfigRevisionStore(bootstrap["sql"], clock=lambda: 1000)
                        return attached, attached.load_snapshot()

                    activation.setattr(hq_control_plane, "attach_fleet_config", attach)
                    from beadhive.cli import config_app

                    before = runner.invoke(config_app, ["get", "hq.sql", "--scope", "host"])
                    assert before.exit_code == 0, before.output
                    actual_before = json.loads(before.stdout)
                    proposed = json.loads(before.stdout)
                    for name in ("runtime", "reader"):
                        proposed[name]["user"] = rotated["principal"]
                        proposed[name]["credential"]["key"] = "NEW_SQL_0243"
                    saved = runner.invoke(
                        config_app,
                        ["set", "hq.sql", json.dumps(proposed), "--json", "--scope", "host"],
                    )
                    assert saved.exit_code == 0, saved.output
                    after = runner.invoke(config_app, ["get", "hq.sql", "--scope", "host"])
                    assert after.exit_code == 0, after.output
                    assert json.loads(after.stdout) == proposed
                    from ruamel.yaml import YAML

                    local_after = YAML(typ="safe").load((fixture_home / "config.yaml").read_text())
                    assert local_after == {
                        **local_host,
                        "hq": {**local_host["hq"], "sql": proposed},
                    }
                    for name in ("runtime", "reader"):
                        restored = {**proposed[name], "user": actual_before[name]["user"]}
                        restored["credential"] = actual_before[name]["credential"]
                        assert restored == actual_before[name]
                    frame_settings = proposed
                    activated = SqlControlPlane(proposed, clock=lambda: 1000)
                    activation.setattr(hq_control_plane, "control_plane", lambda *args: activated)
                    assert heartbeat_sender.main(["renew"]) == 0
                    from beadhive.config_store import clear_load_cache

                    clear_load_cache()

            class FrameBroker(_Broker):
                def get(self, reference, *, deadline):
                    assert reference["key"] in {"SQL", "NEW_SQL_0243"}
                    return super().get({**reference, "key": "SQL"}, deadline=deadline)

            host_identity = fixture.request["host_id"]

            def hive_fence(prefix):
                # The fixture owns no external hive Git remote. Its original placement
                # epoch is supplied at that provider boundary; all SQL proofs stay real.
                assert prefix == "bh"
                return EpochFence(epoch=12, host_id=host_identity)

            frame = SqlControlPlane(
                frame_settings,
                broker=FrameBroker(),
                clock=lambda: 1000,
                fence_reader=hive_fence,
            )

            def frame_connection():
                import time

                return connect(
                    frame.settings["runtime"], frame.broker, deadline=time.monotonic() + 20
                )

            SessionRenewer(frame_connection, database=DB).tick()
            publication = frame_connection()
            publish_evidence(
                publication,
                EvidenceReport(
                    4,
                    fixture.request["release"]["id"],
                    fixture.request["release"]["digest"],
                    fixture.request["profile"],
                    "conformant",
                    "sha256:" + "a" * 64,
                ),
                database=DB,
            )
            publication.close()
            manifest = HostManifest.model_validate(fixture.manifest)
            _head_revision, desired, observation = frame.read_eligibility(manifest, prefix="bh")
            assert observation.predicates["current_hive_lease_holder"]
            assert observation.predicates["release_matches"]
            assert observation.predicates["authenticated_fresh_heartbeat"]
            # Rotation archives the old grant without replacing the protected hive
            # placement. New intake and finishing claims must agree on its epoch.
            for qualifier in ("holder_identity", "incumbent_identity"):
                revision, current_lease = frame.read_hive_lease_record(
                    "bh", **{qualifier: host_identity}
                )
                assert revision == lease_revision
                assert current_lease is not None
                assert current_lease.host_id == host_identity
                assert current_lease.epoch == lease.epoch == 12
                assert current_lease.advisory_expiry
            assert frame.read_hive_lease("bh", holder_identity="foreign") is None

            from beadhive import config, guard, host, host_lease, hq_control_plane, registry
            from beadhive import frame_eligibility as policy
            from beadhive.claim_authority import ClaimRecord

            with monkeypatch.context() as local:
                local.setattr(hq_control_plane, "control_plane", lambda *args: frame)
                local.setattr(host_lease, "_frame_plane", lambda *args: frame)
                local.setattr(config, "load", lambda: {})
                local.setattr(config, "hq_dir", lambda: tmp_path)
                local.setattr(config, "fleet_sql_selected", lambda: True)
                local.setattr(host, "host_id", lambda: host_identity)
                entry = {"prefix": "bh"}
                local.setattr(registry, "resolve_hive", lambda *args: entry)
                local.setattr(registry, "entry_for_dir", lambda *args: entry)
                local.setattr(registry, "hive_dir_for", lambda *args: tmp_path)

                def decision(identity, hive=None, **kwargs):
                    assert identity == host_identity
                    _revision, actual_desired, actual_observation = frame.read_eligibility(
                        manifest, prefix="bh"
                    )
                    return policy.eligible(
                        manifest,
                        hive or entry,
                        policy.EligibilityFacts(actual_observation, actual_desired),
                    )

                local.setattr(policy, "decision_for", decision)
                # This isolated fixture has no local hive writer state or supervisor.
                # Ownership, intake, claim epochs and all SQL reads remain unmodified.
                local.setattr(guard, "writer_state", lambda *args, **kwargs: None)
                local.setattr(guard, "_renew_liveness_hint", lambda *args: None)
                assert policy.local_intake_decision("bh").allowed
                policy.require_intake("bh")
                guard.guard_primary("bh", verb="execute")
                claim = ClaimRecord(
                    "fixture",
                    "dev/test",
                    str(tmp_path),
                    now_stamp(),
                    host_id=host_identity,
                    epoch=12,
                )
                guard.guard_claim_epoch(claim, "bh", verb="submit")
                guard.guard_claim_epoch(claim, "bh", verb="publish")
            cursor.execute("SELECT revision,lease_json FROM hq_live_hive_leases WHERE prefix='bh'")
            assert cursor.fetchone() == (lease_revision, body)
        connection.close()


def _first_incarnation(fixture, frame_id="new-frame"):
    """A frame with no authority entry yet: admission grants it epoch 0 (bh-a0wem)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from beadhive.hq_sql_signatures import fingerprint

    public_key = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    authority = {
        **fixture.record["authority"],
        "frame_id": frame_id,
        "holder_identity": str(uuid4()),
        "instance_ref": "vm-new",
        "key_fingerprint": fingerprint(public_key),
        "epoch": 0,
    }
    return sql_principal(ObservationAuthority(**authority)), authority, public_key


def test_new_frame_first_incarnation_provisioning_advances_every_floor(
    tmp_path, fixture, monkeypatch
):
    """Epoch-0 stamps for a frame with no authority entry are reviewed provisioning."""
    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "signed")
    assert "new-frame" not in fixture.state["frames"]
    with _server(tmp_path) as port:
        connection = _root(port)
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {DB}")
            cursor.execute(f"USE {DB}")
            initial = _initial(cursor, fixture)
            readers = [
                _reader(tmp_path, fixture, initial, name)
                for name in ("operator", "frame", "observer")
            ]
            for reader in readers:
                reader.verified_state_at(cursor, initial)
            principal, _authority, _key = _first_incarnation(fixture)
            provision_incarnation(
                cursor,
                principal,
                0,
                frame_address="127.0.0.1",
                password=PASSWORD,
                database=DB,
            )
            provisioned = _head(cursor)
            assert provisioned != initial
            for reader in readers:
                state, _crossref, _policies = reader.verified_state_at(cursor, provisioned)
                assert state == fixture.state
                floor = json.loads(Path(reader.settings["runtime_floor_path"]).read_text())
                assert floor["head"] == provisioned and floor["sequence"] == 4
            cursor.execute(
                "SELECT principal FROM hq_principal_registry WHERE principal=%s", (principal,)
            )
            assert not cursor.fetchall()
        connection.close()


@pytest.mark.parametrize(
    "unsafe", ["registered", "reserved", "archived", "wrong_epoch", "partial", "noncanonical"]
)
def test_first_incarnation_provisioning_refuses_everything_else(
    tmp_path, fixture, monkeypatch, unsafe
):
    """Epoch 0 is only for a principal no record or registry row names (bh-a0wem)."""
    import copy

    monkeypatch.setenv("BH_HQ_AUTHORITY_MODE", "signed")
    principal, authority, public_key = _first_incarnation(fixture)
    epoch = 0
    if unsafe in ("reserved", "archived"):
        record = copy.deepcopy(fixture.record)
        record.update(public_key=public_key, authority=dict(authority))
        if unsafe == "archived":
            # The epoch-0 record survives only in the signed release-upgrade archive.
            current = copy.deepcopy(record)
            current["authority"]["epoch"] = 1
            current["release_upgrade_history"] = [
                {
                    "authority_revision": "reviewed-prior-head",
                    "config_head": "c" * 32,
                    "plan_sha256": "sha256:" + "a" * 64,
                    "record": record,
                }
            ]
            record = current
        fixture.state["frames"]["new-frame"] = {
            "active": None,
            "candidate": record,
            "retired": [],
            "epoch_floor": record["authority"]["epoch"],
        }
    elif unsafe == "wrong_epoch":
        # Neither a first incarnation nor any existing frame's next epoch (4).
        epoch = 1
        principal = sql_principal(ObservationAuthority(**{**authority, "epoch": epoch}))
    with _server(tmp_path) as port:
        connection = _root(port)
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {DB}")
            cursor.execute(f"USE {DB}")
            initial = _initial(cursor, fixture)
            if unsafe == "registered":
                cursor.execute(
                    "INSERT INTO hq_principal_registry VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        principal,
                        authority["frame_id"],
                        authority["holder_identity"],
                        authority["instance_ref"],
                        epoch,
                        session_table(principal, epoch),
                        authority["key_fingerprint"],
                    ),
                )
                initial = _commit(cursor)
            reader = _reader(tmp_path, fixture, initial, "operator")
            reader.verified_state_at(cursor, initial)
            for ddl in (session_ddl(principal, epoch), evidence_ddl(principal, epoch)):
                cursor.execute(ddl[0])
                if unsafe != "partial" or ddl[0].split()[2].endswith("_session"):
                    cursor.execute(
                        ddl[1].replace("UTC_TIMESTAMP(6)", "UTC_TIMESTAMP(3)")
                        if unsafe == "noncanonical"
                        else ddl[1]
                    )
                cursor.execute(ddl[2])
            head = _commit(cursor)
            reason = {
                "registered": "already registered incarnation",
                "reserved": "not an unauthorized next or first",
                "archived": "not an unauthorized next or first",
                "wrong_epoch": "not an unauthorized next or first",
                "partial": "incomplete session/evidence trigger pair",
                "noncanonical": "not a canonical session/evidence stamp-trigger pair",
            }[unsafe]
            with pytest.raises(SqlRuntimeError, match="amended authority") as refused:
                reader.verified_state_at(cursor, head)
            assert reason in str(refused.value)
            assert "docs/HQ.md#restore-floor-recovery" in str(refused.value)
            assert (
                json.loads(Path(reader.settings["runtime_floor_path"]).read_text())["head"]
                == initial
            )
        connection.close()
