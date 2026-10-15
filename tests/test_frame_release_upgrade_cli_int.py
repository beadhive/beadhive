"""The wheel's CLI process, including native startup and teardown, rotates isolated SQL."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from beadhive.host_lease_contracts import HostLease, now_stamp
from beadhive.hosts import HostManifest
from beadhive.hq_control_plane import SqlControlPlane
from beadhive.hq_seed import apply, plan, prepare
from beadhive.hq_sql_config import SqlFleetConfigRevisionStore
from beadhive.hq_sql_receiver import SqlTrustedReceiver
from test_fleet_membership_e2e_int import (
    Clock,
    _frame,
    _frame_role,
    _key,
    _request_id,
    _runtime_binding,
    _runtime_roles,
    _runtime_schema,
    _signed_beat,
    _signed_initial_runtime,
    _source_git,
)
from test_frame_release_rotation_int import STABLE, _physical_lease, _publish_manifest
from test_hq_sql_config_int import _Broker, _empty_config_server

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def packaged_cli(tmp_path_factory):
    directory = tmp_path_factory.mktemp("release-upgrade-wheel")
    result = subprocess.run(
        ["uv", "build", "--offline", "--wheel", "--out-dir", str(directory / "dist")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    (wheel,) = (directory / "dist").glob("*.whl")
    extracted = directory / "installed"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(extracted)
    return extracted


def _cli(
    wheel,
    directory,
    settings,
    clock,
    action,
    arguments=(),
    *,
    expected_exit=0,
    lost_ack=False,
    stdin=None,
):
    # Only the isolated fixture's secret broker and clock are injected. Command parsing,
    # native filesystem detection, settings, SQL/CAS, printing and shutdown are packaged code.
    script = """
import importlib.metadata, os, sys, time
sys.path.insert(0, sys.argv.pop(1))
time.time = lambda: float(os.environ['FIXTURE_CLOCK'])
import beadhive, beadhive.hq_sql_transport as transport
assert beadhive.__file__.startswith(sys.path[0])
def fixture_secret(self, reference, *, deadline):
    assert reference == {'config_path': '/fixture/fnox.toml', 'profile': 'test', 'key': 'SQL'}
    assert deadline > time.monotonic()
    return 'fixture-secret'
transport.FnoxBroker.get = fixture_secret
from beadhive import config_paths
native_probe = config_paths.statfs_type
def observed_probe(path):
    print('fixture: native filesystem probe ran', file=sys.stderr)
    return native_probe(path)
config_paths.statfs_type = observed_probe
if os.environ.get('FIXTURE_LOST_ACK') == '1':
    import typer
    # Terminate only after the real publication returns, before success JSON is delivered.
    # This models a lost acknowledgment without introducing a product retry/fallback.
    def lost_acknowledgment(*args, **kwargs):
        os._exit(73)
    typer.echo = lost_acknowledgment
entry = next(e for e in importlib.metadata.distribution('beadhive').entry_points if e.name == 'bh')
sys.argv[0] = 'bh'
entry.load()()
"""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("BH_", "WS_", "OTEL_", "BEADS_", "BD_", "DOLT_", "GIT_"))
    }
    environment.update(
        BH_HOME=str(directory / "home"),
        BH_CONFIG=str(directory / "config.yaml"),
        BH_HQ=str(directory / "no-hq"),
        BH_SKIP_SETUP_CHECK="1",
        PYTHONFAULTHANDLER="1",
        FIXTURE_CLOCK=str(clock()),
        FIXTURE_LOST_ACK="1" if lost_ack else "0",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(wheel),
            "host",
            "release-upgrade",
            action,
            "frame-a",
            "--operator-settings",
            str(settings),
            *arguments,
        ],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        input=stdin,
    )
    assert result.returncode == expected_exit, result.stderr
    assert "fixture: native filesystem probe ran" in result.stderr
    if expected_exit:
        if lost_ack:
            assert result.stdout == ""
        else:
            assert "release-upgrade refused:" in result.stderr
        return result
    return json.loads(result.stdout)


def _authority_state(result):
    # Preparing the canonical manifest can change its public config metadata before
    # rotation. The protected authority and accepted archive must stay unchanged.
    return {
        key: result[key]
        for key in (
            "revision",
            "frame_id",
            "authority",
            "state",
            "release",
            "profile",
            "upgraded",
            "history",
        )
    }


@pytest.mark.parametrize("mode", ["signed", "trusted"])
def test_packaged_cli_plan_apply_check_and_replay_are_safe(tmp_path, packaged_cli, mode):
    clock = Clock()
    git_hq, owner, manifests = _source_git(tmp_path)
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (tmp_path / "config.yaml").write_text("managed_repos: []\n")
    with _empty_config_server(sql_dir) as (port, _parent, config_settings):
        store = SqlFleetConfigRevisionStore(config_settings, broker=_Broker(), clock=clock)

        def fresh_plan():
            return plan(
                hq_dir=git_hq,
                fleet_path=git_hq / "fleet.yaml",
                workspace_sources=(),
                destination=store,
            )

        seed = fresh_plan()
        intent = tmp_path / "seed-intent.json"
        prepare(seed, intent)
        seeded = apply(seed, intent, store, fresh_plan=fresh_plan)
        config_settings["initial_revision"] = seeded.committed_revision
        _runtime_schema(sql_dir, port)
        _runtime_roles(sql_dir, port)
        key = tmp_path / "operator.key"
        public = _key(key)
        head = _signed_initial_runtime(port, store.load_snapshot(), owner, key, public, clock)
        common = {
            **config_settings,
            "enabled": True,
            "runtime_floor_path": str(tmp_path / "runtime-floor.json"),
            "runtime_backend_identity": "fixture-runtime-backend",
            "runtime_generation": "fixture-runtime-generation",
            "runtime_initial_revision": head,
            "runtime_operator_public_key": public,
        }
        settings = {
            **common,
            "runtime": None,
            "authority_writer": _runtime_binding(sql_dir, port, "authority_writer"),
            "observer": _runtime_binding(sql_dir, port, "observer"),
        }
        operator = SqlControlPlane(settings, broker=_Broker(), clock=clock)
        receiver = SqlTrustedReceiver(
            {**common, "observer": settings["observer"]}, broker=_Broker(), clock=clock
        )
        frame = _frame(tmp_path, sql_dir, port, common, head, owner, manifests["a"], 1, clock)
        old_release = frame["manifest"].release.model_dump()
        head = operator.grant(
            frame["authority"],
            frame["public"],
            {
                "declared": True,
                "release": old_release,
                "caps": frame["manifest"].capabilities.model_dump(),
                "profile": "fixture",
            },
            expected=head,
            operator_key=str(key),
        )
        digest = frame["plane"].publish_registration_evidence(
            frame["manifest"], signing_key=str(frame["key"])
        )
        receiver.accept_registration(frame["principal"], _request_id(port, frame["inbox"], digest))
        for sequence in (1, 2, 3):
            digest = _signed_beat(frame, owner, clock, sequence, state="pending")
            receiver.accept_heartbeat(frame["principal"], _request_id(port, frame["inbox"], digest))
        operator.lifecycle(
            "admit",
            "frame-a",
            "apply",
            expected=head,
            expected_host_id="host-a",
            expected_release=old_release["digest"],
            operator_key=str(key),
            confirm=True,
        )
        adopted = HostLease(
            host_id="host-a",
            label="host-a",
            epoch=1,
            adopted_at=now_stamp(clock()),
            expires_at=now_stamp(clock() + 1200),
        )
        proposal, *_ = frame["plane"].propose_hive_lease(
            "bh", adopted, expected="", operation="adopt", signing_key=str(frame["key"])
        )
        receiver.accept_hive_lease(frame["principal"], proposal)
        held = _physical_lease(port)
        settings_file = tmp_path / "operator.json"
        settings_file.write_text(json.dumps({"hq": {"authority_mode": mode, "sql": settings}}))
        before = _cli(packaged_cli, tmp_path, settings_file, clock, "check")
        target = {"id": "fixture-0243", "digest": "sha256:" + "9" * 64}
        manifest = HostManifest.model_validate(
            {**frame["manifest"].model_dump(mode="json"), "release": target, "state": "active"}
        )
        prepared = _publish_manifest(store, manifest)
        arguments = [
            "--expected-revision",
            before["revision"],
            "--expected-host-id",
            "host-a",
            "--expected-epoch",
            "1",
            "--expected-release",
            old_release["digest"],
            "--expected-config-head",
            prepared.commit_revision,
            "--release-id",
            target["id"],
            "--release-digest",
            target["digest"],
            "--profile",
            "fixture",
            "--config-revision",
            "desired-1",
            "--candidate-expires-at",
            str(clock() + 900),
        ]
        reviewed = _cli(packaged_cli, tmp_path, settings_file, clock, "plan", arguments)
        assert reviewed["old_epoch"] == 1 and reviewed["new_epoch"] == 2
        assert _authority_state(
            _cli(packaged_cli, tmp_path, settings_file, clock, "check")
        ) == _authority_state(before)
        apply_arguments = [
            *arguments,
            "--plan-sha256",
            reviewed["plan_sha256"],
            "--confirm",
            *(["--operator-key", str(key)] if mode == "signed" else []),
        ]
        wrong_digest = [*apply_arguments]
        wrong_digest[wrong_digest.index("--plan-sha256") + 1] = "sha256:" + "0" * 64
        _cli(packaged_cli, tmp_path, settings_file, clock, "apply", wrong_digest, expected_exit=1)
        assert _authority_state(
            _cli(packaged_cli, tmp_path, settings_file, clock, "check")
        ) == _authority_state(before)
        _frame_role(sql_dir, port, reviewed["principal"], reviewed["new_epoch"])
        applied = _cli(packaged_cli, tmp_path, settings_file, clock, "apply", apply_arguments)
        after = _cli(packaged_cli, tmp_path, settings_file, clock, "check")
        assert after["revision"] == applied["revision"] != before["revision"]
        assert after["state"] == "active" and after["release"] == target
        assert after["authority"]["epoch"] == 2
        assert after["history"][-1]["plan_sha256"] == reviewed["plan_sha256"]
        for field in (*STABLE, "beadyard_id"):
            assert after["authority"][field] == before["authority"][field]
        # A lost response is diagnosed by this readback. Replaying its old exact CAS refuses
        # and a subsequent public check proves there was no second epoch bump.
        _cli(
            packaged_cli, tmp_path, settings_file, clock, "apply", apply_arguments, expected_exit=1
        )
        assert _cli(packaged_cli, tmp_path, settings_file, clock, "check") == after
        assert _physical_lease(port) == held
        if mode == "trusted":
            # A second rotation deliberately loses its acknowledgment after SQL commit.
            next_release = {"id": "fixture-0243-second", "digest": "sha256:" + "8" * 64}
            prepared = _publish_manifest(
                store,
                HostManifest.model_validate(
                    {**manifest.model_dump(mode="json"), "release": next_release}
                ),
            )
            next_arguments = list(arguments)
            for option, value in (
                ("--expected-revision", after["revision"]),
                ("--expected-epoch", "2"),
                ("--expected-release", target["digest"]),
                ("--expected-config-head", prepared.commit_revision),
                ("--release-id", next_release["id"]),
                ("--release-digest", next_release["digest"]),
            ):
                next_arguments[next_arguments.index(option) + 1] = value
            next_plan = _cli(packaged_cli, tmp_path, settings_file, clock, "plan", next_arguments)
            _frame_role(sql_dir, port, next_plan["principal"], next_plan["new_epoch"])
            next_apply = [
                *next_arguments,
                "--plan-sha256",
                next_plan["plan_sha256"],
                "--confirm",
            ]
            _cli(
                packaged_cli,
                tmp_path,
                settings_file,
                clock,
                "apply",
                next_apply,
                expected_exit=73,
                lost_ack=True,
            )
            recovered = _cli(packaged_cli, tmp_path, settings_file, clock, "check")
            assert recovered["authority"]["epoch"] == 3
            assert recovered["release"] == next_release
            assert recovered["history"][-1]["plan_sha256"] == next_plan["plan_sha256"]
            _cli(packaged_cli, tmp_path, settings_file, clock, "apply", next_apply, expected_exit=1)
            assert _cli(packaged_cli, tmp_path, settings_file, clock, "check") == recovered
            assert _physical_lease(port) == held


def _option(arguments, option, value):
    """``arguments`` with one reviewed option's value replaced."""
    changed = list(arguments)
    changed[changed.index(option) + 1] = value
    return changed


def test_packaged_cli_migrates_legacy_inbox_to_session_rows_with_documented_grants(
    tmp_path, packaged_cli
):
    """bh-ct91o, bh-t759y, bh-aprgo, bh-odevt through the packaged CLI on real Dolt.

    The remote operator binding holds only the documented least-privilege grants (writes on
    its protected tables plus database-wide SELECT); the server-local binding provisions and
    applies. Restricted capabilities are named before preparation, never masked."""
    import pymysql

    from beadhive.hq_sql_runtime_schema import session_table
    from beadhive.hq_sql_session_provision import provision_liveness_schema

    clock = Clock()
    git_hq, owner, manifests = _source_git(tmp_path)
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (tmp_path / "config.yaml").write_text("managed_repos: []\n")
    with _empty_config_server(sql_dir) as (port, _parent, config_settings):
        store = SqlFleetConfigRevisionStore(config_settings, broker=_Broker(), clock=clock)

        def fresh_plan():
            return plan(
                hq_dir=git_hq,
                fleet_path=git_hq / "fleet.yaml",
                workspace_sources=(),
                destination=store,
            )

        seed = fresh_plan()
        intent = tmp_path / "seed-intent.json"
        prepare(seed, intent)
        seeded = apply(seed, intent, store, fresh_plan=fresh_plan)
        config_settings["initial_revision"] = seeded.committed_revision
        _runtime_schema(sql_dir, port)
        root = pymysql.connect(
            host="127.0.0.1", port=port, user="root", database="beadhive_hq_runtime",
            autocommit=True,
        )  # fmt: skip
        with root.cursor() as cursor:
            # Session routes are enabled once per runtime database, before any authority.
            provision_liveness_schema(cursor)
            # An unrelated committed table no account was ever granted individually.
            cursor.execute("CREATE TABLE zz_operator_notes (id INT PRIMARY KEY)")
            cursor.execute("CALL DOLT_ADD('zz_operator_notes')")
            cursor.execute("CALL DOLT_COMMIT('-m','unrelated operator table')")
            _runtime_roles(sql_dir, port)
            # docs/HQ.md#runtime-sql-privileges: database-wide, read-only.
            for user in ("authority_writer", "observer"):
                cursor.execute(f"GRANT SELECT ON beadhive_hq_runtime.* TO '{user}'@'localhost'")
            # The server-local administrative binding (provision and session apply).
            cursor.execute("CREATE USER 'hq_admin'@'localhost' IDENTIFIED BY 'fixture-secret'")
            cursor.execute(
                "GRANT ALL PRIVILEGES ON *.* TO 'hq_admin'@'localhost' WITH GRANT OPTION"
            )
        root.close()
        key = tmp_path / "operator.key"
        public = _key(key)
        head = _signed_initial_runtime(port, store.load_snapshot(), owner, key, public, clock)
        common = {
            **config_settings,
            "enabled": True,
            "runtime_floor_path": str(tmp_path / "runtime-floor.json"),
            "runtime_backend_identity": "fixture-runtime-backend",
            "runtime_generation": "fixture-runtime-generation",
            "runtime_initial_revision": head,
            "runtime_operator_public_key": public,
        }
        settings = {
            **common,
            "runtime": None,
            "authority_writer": _runtime_binding(sql_dir, port, "authority_writer"),
            "observer": _runtime_binding(sql_dir, port, "observer"),
        }
        operator = SqlControlPlane(settings, broker=_Broker(), clock=clock)
        receiver = SqlTrustedReceiver(
            {**common, "observer": settings["observer"]}, broker=_Broker(), clock=clock
        )
        frame = _frame(tmp_path, sql_dir, port, common, head, owner, manifests["a"], 1, clock)
        old_release = frame["manifest"].release.model_dump()
        head = operator.grant(
            frame["authority"],
            frame["public"],
            {
                "declared": True,
                "release": old_release,
                "caps": frame["manifest"].capabilities.model_dump(),
                "profile": "fixture",
            },
            expected=head,
            operator_key=str(key),
        )
        digest = frame["plane"].publish_registration_evidence(
            frame["manifest"], signing_key=str(frame["key"])
        )
        receiver.accept_registration(frame["principal"], _request_id(port, frame["inbox"], digest))
        for sequence in (1, 2, 3):
            digest = _signed_beat(frame, owner, clock, sequence, state="pending")
            receiver.accept_heartbeat(frame["principal"], _request_id(port, frame["inbox"], digest))
        operator.lifecycle(
            "admit",
            "frame-a",
            "apply",
            expected=head,
            expected_host_id="host-a",
            expected_release=old_release["digest"],
            operator_key=str(key),
            confirm=True,
        )
        adopted = HostLease(
            host_id="host-a",
            label="host-a",
            epoch=1,
            adopted_at=now_stamp(clock()),
            expires_at=now_stamp(clock() + 1200),
        )
        proposal, *_ = frame["plane"].propose_hive_lease(
            "bh", adopted, expected="", operation="adopt", signing_key=str(frame["key"])
        )
        receiver.accept_hive_lease(frame["principal"], proposal)
        held = _physical_lease(port)
        remote = tmp_path / "operator.json"
        remote.write_text(json.dumps({"hq": {"authority_mode": "signed", "sql": settings}}))
        admin = tmp_path / "server-local.json"
        admin.write_text(
            json.dumps(
                {
                    "hq": {
                        "authority_mode": "signed",
                        "sql": {
                            **settings,
                            "authority_writer": _runtime_binding(sql_dir, port, "hq_admin"),
                            "runtime_floor_path": str(tmp_path / "server-local-floor.json"),
                        },
                    }
                }
            )
        )

        def cli(settings_file, action, arguments=(), **kwargs):
            return _cli(packaged_cli, tmp_path, settings_file, clock, action, arguments, **kwargs)

        # The admitted frame's canonical manifest records it active at the admitted release.
        _publish_manifest(
            store,
            HostManifest.model_validate(
                {**frame["manifest"].model_dump(mode="json"), "state": "active"}
            ),
        )
        before = cli(remote, "check")
        assert before["route"] == "inbox"
        target = {"id": "fixture-0244", "digest": "sha256:" + "7" * 64}
        arguments = [
            "--expected-revision", before["revision"],
            "--expected-host-id", "host-a",
            "--expected-epoch", "1",
            "--expected-release", old_release["digest"],
            "--expected-config-head", before["canonical_config_head"],
            "--release-id", target["id"],
            "--release-digest", target["digest"],
            "--profile", "fixture",
            "--config-revision", "desired-1",
            "--candidate-expires-at", str(clock() + 900),
            "--target-route", "session",
        ]  # fmt: skip

        # Preparation exposes the explicit transition and the remote binding's capabilities
        # before the canonical release changes: reads are complete, session apply is not.
        review = cli(remote, "prepare-config", arguments)
        assert review["route"] == "session" and review["route_transition"] == "inbox-to-session"
        assert review["preflight"]["runtime_read"] == []
        assert review["preflight"]["ready"] == {"apply": False, "provision": False}
        assert any("TRIGGER" in item for item in review["preflight"]["apply"])
        assert not any("authority_writer" in item for item in review["preflight"]["apply"])
        confirm = ["--plan-sha256", review["plan_sha256"], "--confirm"]
        prepared = cli(remote, "prepare-config", [*arguments, *confirm])
        assert prepared["applied"] and not prepared["rollback"]

        # bh-odevt: cancel the unapplied preparation through the public CLI, idempotently.
        rollback = _option(arguments[:-2], "--expected-config-head", prepared["config_head"])
        rollback = _option(rollback, "--release-id", old_release["id"])
        rollback = _option(rollback, "--release-digest", old_release["digest"])
        cancel = cli(remote, "prepare-config", rollback)
        assert cancel["rollback"] and cancel["canonical_release_before"] == target
        cancelled = cli(
            remote,
            "prepare-config",
            [*rollback, "--plan-sha256", cancel["plan_sha256"], "--confirm"],
        )
        restored = cli(remote, "check")
        assert restored["canonical_release"] == old_release
        assert restored["canonical_config_head"] == cancelled["config_head"]
        assert _authority_state(restored) == _authority_state(before)
        rollback = _option(rollback, "--expected-config-head", cancelled["config_head"])
        assert cli(remote, "prepare-config", rollback)["already_prepared"]

        # Prepare again, then plan the explicit transition.
        arguments = _option(arguments, "--expected-config-head", cancelled["config_head"])
        review = cli(remote, "prepare-config", arguments)
        prepared = cli(
            remote,
            "prepare-config",
            [*arguments, "--plan-sha256", review["plan_sha256"], "--confirm"],
        )
        arguments = _option(arguments, "--expected-config-head", prepared["config_head"])
        reviewed = cli(remote, "plan", arguments)
        assert reviewed["route"] == "session" and not reviewed["provisioned"]
        assert reviewed["inbox_table"] == session_table(reviewed["principal"], 2)
        provision = [
            *arguments,
            "--plan-sha256", reviewed["plan_sha256"], "--confirm",
            "--frame-address", "127.0.0.1", "--password-stdin",
        ]  # fmt: skip
        refused = cli(remote, "provision", provision, stdin="frame-secret\n", expected_exit=1)
        assert "provision preflight" in refused.stderr and "CREATE USER" in refused.stderr
        assert _authority_state(cli(remote, "check")) == _authority_state(before)

        provisioned = cli(admin, "provision", provision, stdin="frame-secret\n")
        assert provisioned["provisioned"] and provisioned["replan_required"]
        # bh-aprgo: the remote documented reader verifies its floor across the provisioning
        # commit (unrelated tables included) and replans at the new revision.
        arguments = _option(arguments, "--expected-revision", provisioned["revision"])
        reviewed = cli(remote, "plan", arguments)
        assert reviewed["provisioned"] and reviewed["preflight"]["runtime_read"] == []
        apply_arguments = [
            *arguments,
            "--plan-sha256", reviewed["plan_sha256"], "--confirm",
            "--operator-key", str(key),
        ]  # fmt: skip
        # bh-t759y: the remote apply names the missing inspection capability up front.
        refused = cli(remote, "apply", apply_arguments, expected_exit=1)
        assert "apply preflight" in refused.stderr and "TRIGGER" in refused.stderr
        assert "publication unavailable" not in refused.stderr
        # Only the provisioning commit moved HEAD; the signed authority is unchanged.
        unchanged = _authority_state(cli(remote, "check"))
        assert unchanged["revision"] == provisioned["revision"]
        assert {**unchanged, "revision": before["revision"]} == _authority_state(before)

        # The server-local apply commits but loses its acknowledgment; readback decides.
        cli(admin, "apply", apply_arguments, expected_exit=73, lost_ack=True)
        after = cli(remote, "check")
        assert after["authority"]["epoch"] == 2 and after["release"] == target
        assert after["state"] == "active" and after["route"] == "session"
        assert after["inbox_table"] == session_table(after["principal"], 2)
        assert after["history"][-1]["plan_sha256"] == reviewed["plan_sha256"]
        for field in (*STABLE, "beadyard_id"):
            assert after["authority"][field] == before["authority"][field]
        # Replaying the committed plan refuses and manufactures no further epoch.
        cli(admin, "apply", apply_arguments, expected_exit=1)
        assert cli(remote, "check") == after
        assert _physical_lease(port) == held

        # An incumbent session route rotates again, inheriting its family (no target route).
        following = {"id": "fixture-0245", "digest": "sha256:" + "6" * 64}
        arguments = arguments[:-2]
        for option, value in (
            ("--expected-revision", after["revision"]),
            ("--expected-epoch", "2"),
            ("--expected-release", target["digest"]),
            ("--expected-config-head", after["canonical_config_head"]),
            ("--release-id", following["id"]),
            ("--release-digest", following["digest"]),
        ):
            arguments = _option(arguments, option, value)
        review = cli(remote, "prepare-config", arguments)
        assert review["route"] == "session" and review["route_transition"] == "none"
        prepared = cli(
            remote,
            "prepare-config",
            [*arguments, "--plan-sha256", review["plan_sha256"], "--confirm"],
        )
        arguments = _option(arguments, "--expected-config-head", prepared["config_head"])
        reviewed = cli(remote, "plan", arguments)
        assert reviewed["route"] == "session" and reviewed["new_epoch"] == 3
        provisioned = cli(
            admin,
            "provision",
            [
                *arguments,
                "--plan-sha256", reviewed["plan_sha256"], "--confirm",
                "--frame-address", "127.0.0.1", "--password-stdin",
            ],
            stdin="frame-secret-3\n",
        )  # fmt: skip
        arguments = _option(arguments, "--expected-revision", provisioned["revision"])
        reviewed = cli(remote, "plan", arguments)
        apply_arguments = [
            *arguments,
            "--plan-sha256", reviewed["plan_sha256"], "--confirm",
            "--operator-key", str(key),
        ]  # fmt: skip
        applied = cli(admin, "apply", apply_arguments)
        again = cli(remote, "check")
        assert again["revision"] == applied["revision"]
        assert again["authority"]["epoch"] == 3 and again["release"] == following
        assert again["inbox_table"] == session_table(again["principal"], 3)
        cli(admin, "apply", apply_arguments, expected_exit=1)
        assert cli(remote, "check") == again
        assert _physical_lease(port) == held
