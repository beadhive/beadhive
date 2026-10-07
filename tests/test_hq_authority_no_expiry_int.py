"""Signed authority without expiry (bh-y929l, epic bh-taa04) on a private Dolt server.

One SQL frame, simulated clock. The operator renews once with no ``--duration``: the signed
``expires_at`` and every hive policy ``valid_until`` round-trip through the committed SQL
carrier as :data:`AUTHORITY_NO_EXPIRY`, an operator-signed cordon/resume keeps it, and the
frame stays eligible at t+400 d with no further operator action. An explicit ``--duration``
still signs an expiring authority that fences after expiry, and an explicit ceiling caps it.
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

from beadhive import hq_authority_ceiling as ceiling_mod
from beadhive import hq_authority_guard as guard
from beadhive.frame_eligibility import EligibilityFacts, eligible
from beadhive.host_heartbeat_core import HeartbeatLease
from beadhive.hosts import HostManifest
from beadhive.hq_authority_ceiling import AUTHORITY_NO_EXPIRY
from beadhive.hq_control_plane import ControlPlaneError, SqlControlPlane
from beadhive.hq_sql_runtime_schema import COMMITTED_SCHEMA, PROTECTED_LIVE_SCHEMA, inbox_ddl
from beadhive.hq_sql_signatures import fingerprint
from beadhive.modules.config.domain.ports import FleetConfigDocument
from harness.world import dolt_server_slot

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]

HOUR = 3600.0
DAY = 24 * HOUR


def test_signed_authority_without_duration_never_expires(tmp_path, monkeypatch):
    monkeypatch.delenv(ceiling_mod.ENV_VAR, raising=False)
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
    documents = (
        FleetConfigDocument("fleet.yaml", FLEET),
        FleetConfigDocument(
            "hosts/host-1.yaml", json.dumps(manifest.model_dump(mode="json", exclude_none=True))
        ),
    )

    with dolt_server_slot(test_id="hq-authority-no-expiry"):
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

            def authority():
                revision, state, _crossref, policies = operator._operator().load()
                return revision, state, policies

            def lifecycle(verb, revision):
                return operator.lifecycle(
                    verb,
                    "frame-1",
                    "apply",
                    expected=revision,
                    expected_host_id="host-1",
                    operator_key=str(operator_key),
                    confirm=True,
                )

            # t0: one renew with no --duration signs the sentinel; it round-trips through the
            # committed SQL carrier (state_json, hive_policies_json, signature) unchanged.
            t0 = clock.now
            operator.renew(expected=initial, operator_key=str(operator_key))
            revision, state, policies = authority()
            assert state["expires_at"] == AUTHORITY_NO_EXPIRY
            assert type(state["expires_at"]) is int
            assert policies["bh"]["valid_until"] == AUTHORITY_NO_EXPIRY
            guard.validate_state(state)
            _rev, frame_state, _crossref, frame_policies = frame._runtime_authority().load_state()
            assert frame_state == state and frame_policies == policies
            assert decision()[1].allowed

            # An operator-signed cordon/resume keeps the authority non-expiring.
            clock.now = t0 + HOUR
            revision = lifecycle("cordon", revision)["revision"]
            assert authority()[1]["expires_at"] == AUTHORITY_NO_EXPIRY
            revision = lifecycle("resume", revision)["revision"]
            revision, state, _policies = authority()
            assert state["expires_at"] == AUTHORITY_NO_EXPIRY

            # Laptop off: the frame beats on, with no operator action, through t+400 d.
            for days in (8, 30, 90, 365, 400):
                clock.now = t0 + days * DAY
                desired, verdict = decision()
                assert verdict.allowed, (days, verdict)
                assert not desired["cordoned"]
            assert authority()[:2] == (revision, state)

            # Opt-in expiry: an explicit ceiling caps an explicit --duration ...
            capped = ceiling_mod.resolve_ceiling(cli="1d")
            with pytest.raises(ControlPlaneError, match="exceeds ceiling"):
                operator.renew(
                    expected=revision,
                    operator_key=str(operator_key),
                    duration=int(2 * DAY),
                    ceiling=capped,
                )
            # ... and an expiring authority still fences after it expires.
            operator.renew(
                expected=revision,
                operator_key=str(operator_key),
                duration=int(DAY),
                ceiling=capped,
            )
            revision, state, policies = authority()
            assert state["expires_at"] == pytest.approx(clock() + DAY)
            assert policies["bh"]["valid_until"] == state["expires_at"]
            assert decision()[1].allowed
            clock.now = state["expires_at"] + 1
            with pytest.raises(ControlPlaneError):
                decision()
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
    assert time.monotonic() - started < 120, "no-expiry acceptance should stay modest"
