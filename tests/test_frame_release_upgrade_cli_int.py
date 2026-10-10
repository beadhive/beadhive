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
    wheel, directory, settings, clock, action, arguments=(), *, expected_exit=0, lost_ack=False
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
        assert _cli(packaged_cli, tmp_path, settings_file, clock, "check") == before
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
        assert _cli(packaged_cli, tmp_path, settings_file, clock, "check") == before
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
