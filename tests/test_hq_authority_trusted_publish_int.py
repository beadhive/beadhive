"""Key-less trusted-mode publication (bh-l4q0s, epic bh-taa04) on a private Dolt server.

A trusted operator rebinds, cordons, resumes and grants with no operator key: every record
carries the unsigned marker, a trusted frame accepts it and honours its content, a signed frame
refuses it fail-closed with the actionable message. A key-less mutation outside trusted mode
still refuses, and a signed rebind (the operator supplies the key) restores signed-frame
eligibility.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime

import pytest
from tests.test_hq_laptop_off_acceptance_int import (
    CONFIG_SCHEMA,
    FLEET,
    GRANTS,
    RELEASE,
    Broker,
    SimClock,
    _binding,
    _cli,
    _key,
    _seed_authority,
    _seed_config,
    _start_server,
)

from beadhive import hq_authority_enforce
from beadhive.frame_eligibility import EligibilityFacts, eligible
from beadhive.host_heartbeat_core import HeartbeatLease, ObservationAuthority
from beadhive.hosts import HostManifest
from beadhive.hq_control_plane import ControlPlaneError, SqlControlPlane
from beadhive.hq_sql_operator import SqlRuntimeOperator
from beadhive.hq_sql_runtime_schema import COMMITTED_SCHEMA, PROTECTED_LIVE_SCHEMA, inbox_ddl
from beadhive.hq_sql_signatures import fingerprint
from beadhive.modules.config.domain.ports import FleetConfigDocument
from harness.world import dolt_server_slot

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]

MODE_ENV = hq_authority_enforce.MODE_ENV
HOUR = 3600.0


@pytest.fixture(autouse=True)
def _signed_baseline(monkeypatch):
    for name in (MODE_ENV, hq_authority_enforce.ENFORCE_ENV, "BH_HQ_OPERATOR_SETTINGS"):
        monkeypatch.delenv(name, raising=False)
    hq_authority_enforce.reset_cache()
    yield
    hq_authority_enforce.reset_cache()


def _manifest():
    return HostManifest.model_validate(
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


def test_keyless_trusted_publication_and_signed_rebind_on_sql(tmp_path, monkeypatch):
    started = time.monotonic()
    clock = SimClock(time.time())
    operator_key, frame_key = tmp_path / "operator-key", tmp_path / "frame-key"
    operator_public, frame_public = _key(operator_key), _key(frame_key)
    manifest = _manifest()
    documents = (
        FleetConfigDocument("fleet.yaml", FLEET),
        FleetConfigDocument(
            "hosts/host-1.yaml", json.dumps(manifest.model_dump(mode="json", exclude_none=True))
        ),
    )

    with dolt_server_slot(test_id="hq-authority-trusted-publish"):
        server, port = _start_server(tmp_path)
        try:
            _cli(tmp_path, port, "CREATE DATABASE beadhive_hq_runtime")
            _cli(tmp_path, port, "USE beadhive_hq_runtime; " + "; ".join(COMMITTED_SCHEMA))
            _cli(tmp_path, port, "USE beadhive_hq_runtime; " + "; ".join(PROTECTED_LIVE_SCHEMA))
            _cli(tmp_path, port, "USE beadhive_hq_runtime; " + inbox_ddl("frame_a", 1))
            _cli(tmp_path, port, "CREATE DATABASE beadhive_hq_config")
            _cli(tmp_path, port, CONFIG_SCHEMA)
            config_head = _seed_config(port, documents)
            initial = _seed_authority(
                port,
                config_head=config_head,
                documents=documents,
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
            operator = SqlControlPlane(
                {
                    **settings,
                    "runtime": None,
                    "authority_writer": _binding(port, "authority_writer", "beadhive_hq_runtime"),
                },
                broker=Broker(),
                clock=clock,
            )
            seq = iter(range(1, 10_000))

            def decision():
                frame.heartbeat(
                    HeartbeatLease(
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
                    ),
                    signing_key=str(frame_key),
                )
                _head, desired, observation = frame.read_eligibility(manifest)
                _rev, _state, _crossref, policies = frame._runtime_authority().load_state()
                return desired, eligible(
                    manifest, policies["bh"], EligibilityFacts(observation, desired, at=clock())
                )

            def lifecycle(verb, revision, key=""):
                return operator.lifecycle(
                    verb,
                    "frame-1",
                    "apply",
                    expected=revision,
                    expected_host_id="host-1",
                    operator_key=key,
                    confirm=True,
                )["revision"]

            def stored_signature():
                return _cli(
                    tmp_path,
                    port,
                    "SELECT CAST(operator_signature AS CHAR) FROM beadhive_hq_runtime.hq_authority",
                )

            def signed_frame_refuses():
                assert hq_authority_enforce.UNSIGNED_SIGNATURE in stored_signature()
                monkeypatch.delenv(MODE_ENV)
                with pytest.raises(ValueError, match="UNSIGNED.*hq.authority_mode: trusted"):
                    frame._runtime_authority().load_state()
                with pytest.raises(ControlPlaneError):
                    decision()
                monkeypatch.setenv(MODE_ENV, "trusted")

            # Outside trusted mode a key-less mutation refuses exactly as before.
            with pytest.raises(ControlPlaneError, match="requires separate --operator-key"):
                operator.renew(expected=initial, operator_key="")
            assert decision()[1].allowed

            # Trusted operator, no key: rebind writes the unsigned marker.
            monkeypatch.setenv(MODE_ENV, "trusted")
            head = operator.renew(expected=initial, operator_key="")
            assert decision()[1].allowed
            signed_frame_refuses()

            # Lifecycle key-less: a trusted frame honours the unsigned cordon and resume.
            clock.now += HOUR
            head = lifecycle("cordon", head)
            desired, verdict = decision()
            assert desired["cordoned"] and not verdict.allowed
            signed_frame_refuses()
            head = lifecycle("resume", head)
            desired, verdict = decision()
            assert not desired["cordoned"] and verdict.allowed

            # Grant key-less: a second incarnation is recorded as a pending candidate.
            second_key = tmp_path / "second-key"
            second_public = _key(second_key)
            second = ObservationAuthority(
                "frame-2",
                "host-2",
                "vm-2",
                fingerprint(second_public),
                1,
                "fixture-fleet",
                "desired-1",
                clock() + 1800,
            )
            principal = SqlRuntimeOperator.principal_for(second)
            _cli(tmp_path, port, "USE beadhive_hq_runtime; " + inbox_ddl(principal, 1))
            head = operator.grant(
                second,
                second_public,
                {
                    "declared": True,
                    "release": RELEASE,
                    "caps": manifest.capabilities.model_dump(),
                    "profile": "fixture",
                },
                expected=head,
                operator_key="",
            )
            revision, state, _crossref, _policies = operator._operator().load()
            assert revision == head
            assert state["frames"]["frame-2"]["candidate"]["state"] == "pending"
            assert decision()[1].allowed
            signed_frame_refuses()

            # Switching back: the trusted operator rebinds WITH the key; signed frames are
            # eligible again on the signed record.
            operator.renew(expected=head, operator_key=str(operator_key))
            assert hq_authority_enforce.UNSIGNED_SIGNATURE not in stored_signature()
            monkeypatch.delenv(MODE_ENV)
            hq_authority_enforce.reset_cache()
            desired, verdict = decision()
            assert verdict.allowed and not desired["cordoned"]
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
    assert time.monotonic() - started < 120, "trusted publication acceptance should stay modest"
