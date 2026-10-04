import subprocess
from io import StringIO
from types import SimpleNamespace

import pytest
from ruamel.yaml import YAML

from beadhive import heartbeat_report as report
from beadhive import release_measurement
from beadhive.hq_framelease_contracts import HeartbeatError


@pytest.fixture
def plane(monkeypatch):
    release = {"id": "beadhive-factory-v0.21.3", "digest": "sha256:" + "a" * 64}
    yard = "12345678-1234-4234-8234-123456789abc"
    manifest = {
        "host_id": "host-a",
        "label": "a",
        "os": "linux",
        "arch": "x86_64",
        "role": "executor",
        "identity": {"kind": "none"},
        "frame_id": "factory",
        "beadyard_id": yard,
        "instance_ref": "instance-a",
        "release": release,
    }
    stream = StringIO()
    YAML().dump(manifest, stream)
    route = SimpleNamespace(
        holder_identity="host-a",
        frame_id="factory",
        instance_ref="instance-a",
        signer_fingerprint="SHA256:fingerprint",
        epoch=2,
    )
    record = {
        "authority": {"audience": yard, "beadyard_id": yard, "config_revision": "rev"},
        "desired": {"release": release, "profile": "factory-v1"},
        "state": "pending",
    }
    snapshot = SimpleNamespace(
        documents=[SimpleNamespace(path="hosts/host-a.yaml", content=stream.getvalue())]
    )
    composite = ("head", {}, route, "candidate", record, snapshot, {}, (8,), None)
    plane = SimpleNamespace(
        _runtime_authority=lambda: SimpleNamespace(read_frame_composite=lambda: composite)
    )
    monkeypatch.setattr(report.host, "host_id", lambda: "host-a")
    monkeypatch.setattr(
        report, "installed_release", lambda: {"id": "0.21.3", "digest": release["digest"]}
    )
    monkeypatch.setattr(report, "config_valid", lambda: True)
    monkeypatch.setattr(
        report.config, "load", lambda: {"host": {"frame_id": "factory"}, "managed_repos": [{}]}
    )
    monkeypatch.setattr(report, "hive_ready", lambda _: True)
    return plane


def test_truthful_report_and_monotonic_sequence(plane):
    lease = report.generate(plane)
    assert lease.seq == 9 and lease.epoch == 2
    assert lease.release.id == "beadhive-factory-v0.21.3"
    assert lease.conformance.status == "conformant"
    assert lease.free_sessions == 0


@pytest.mark.parametrize("check", ["config_valid", "hive_ready"])
def test_failed_checks_remain_nonconformant(plane, monkeypatch, check):
    monkeypatch.setattr(report, check, lambda *args: False)
    lease = report.generate(plane)
    assert lease.conformance.status == "non-conformant"
    assert any(check.status == "fail" for check in lease.conformance.checks)


def test_probe_errors_are_redacted(plane, monkeypatch):
    def fail(*args):
        raise RuntimeError("password=SECRET")

    monkeypatch.setattr(report, "hive_ready", fail)
    lease = report.generate(plane)
    assert lease.conformance.status == "non-conformant"
    assert "SECRET" not in lease.model_dump_json()


def test_install_drift_refuses_generation_and_send(plane, monkeypatch):
    monkeypatch.setattr(
        report, "installed_release", lambda: {"id": "0.21.2", "digest": "sha256:" + "b" * 64}
    )
    plane.heartbeat = lambda *args, **kwargs: pytest.fail("must never publish drift")
    with pytest.raises(HeartbeatError, match="operator upgrade required"):
        report.send(plane)


def test_no_hives_is_not_conformant(plane, monkeypatch):
    monkeypatch.setattr(report.config, "load", lambda: {"host": {"frame_id": "factory"}})
    assert report.generate(plane).conformance.status == "non-conformant"


def test_capacity_requires_committed_capacity(plane):
    with pytest.raises(HeartbeatError, match="capacity"):
        report.generate(plane, free_sessions=1)


def test_empty_installed_distribution_fails(monkeypatch):
    monkeypatch.setattr(
        release_measurement.importlib.metadata, "distribution", lambda _: SimpleNamespace(files=[])
    )
    with pytest.raises(HeartbeatError, match="editable"):
        release_measurement.installed_release()


def test_send_remeasures_and_only_publishes(plane, monkeypatch):
    published = []
    monkeypatch.setattr(report.host, "signing_key", lambda: "key-reference")
    plane.heartbeat = lambda lease, **kwargs: published.append((lease, kwargs)) or "accepted"
    assert report.send(plane) == "accepted"
    assert len(published) == 1
    assert published[0][0].seq == 9
    assert published[0][1] == {"signing_key": "key-reference"}


def test_config_read_failure_is_nonconformant(plane, monkeypatch):
    def fail():
        raise RuntimeError("secret-password")

    monkeypatch.setattr(report.config, "load", fail)
    lease = report.generate(plane)
    assert lease.conformance.status == "non-conformant"
    assert "secret-password" not in lease.model_dump_json()


def test_installed_measurement_matches_original_algorithm(monkeypatch, tmp_path):
    import hashlib
    from pathlib import Path

    files = ["beadhive/b.py", "beadhive/a.py", "beadhive/__pycache__/a.pyc", "other.py"]
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    distribution = SimpleNamespace(
        files=[Path(item) for item in files],
        version="0.21.3",
        locate_file=lambda item: tmp_path / item,
    )
    monkeypatch.setattr(
        release_measurement.importlib.metadata, "distribution", lambda _: distribution
    )
    digest = hashlib.sha256()
    for name in sorted(files[:2]):
        digest.update(name.encode() + b"\0" + hashlib.sha256(name.encode()).digest())
    assert release_measurement.installed_release() == {
        "id": "0.21.3",
        "digest": "sha256:" + digest.hexdigest(),
    }


def test_host_validation_uses_effective_fleet_schema_version(monkeypatch):
    checked = []
    monkeypatch.setattr(report.config, "load_host", lambda: checked.append("raw-host") or {})
    monkeypatch.setattr(report.config, "load", lambda: {"schema_version": 99})
    monkeypatch.setattr(
        report,
        "validate_config",
        lambda cfg: [] if cfg.get("schema_version") else [{"level": "error"}],
    )
    assert report.config_valid()
    assert checked == ["raw-host"]


def _stub_hive(monkeypatch, tmp_path, *, ready=True, ping=None, calls=None):
    """Point ``hive_ready`` at an existing hive dir; capture every bd invocation via ``bd.run``."""
    from beadhive import bd, hive_ready

    monkeypatch.setattr(report.registry, "hive_dir", lambda _entry: tmp_path)
    monkeypatch.setattr(hive_ready, "probe_readiness", lambda **_kw: SimpleNamespace(ready=ready))

    def run(args, cwd, actor="", capture=False, text_input=None, **kwargs):
        if calls is not None:
            calls.append((list(args), cwd, capture, kwargs.get("timeout")))
        if isinstance(ping, BaseException):
            raise ping
        return ping

    monkeypatch.setattr(bd, "run", run)


def _ping(returncode=0, stdout='{"status": "ok"}'):
    return subprocess.CompletedProcess(["bd"], returncode, stdout, "")


def test_hive_ready_pings_database_through_the_bd_package_route(monkeypatch, tmp_path):
    calls = []
    _stub_hive(monkeypatch, tmp_path, ping=_ping(), calls=calls)
    assert report.hive_ready({}) is True
    assert calls == [(["ping", "--json"], tmp_path, True, 20)]


@pytest.mark.parametrize(
    "ping",
    [_ping(1, ""), _ping(124, ""), _ping(0, '{"status": "error"}')],
    ids=["unreachable", "timed-out", "not-ok"],
)
def test_hive_ready_is_false_when_the_database_is_unreachable(monkeypatch, tmp_path, ping):
    _stub_hive(monkeypatch, tmp_path, ping=ping)
    assert report.hive_ready({}) is False


def test_hive_ready_skips_the_ping_when_not_ready(monkeypatch, tmp_path):
    calls = []
    _stub_hive(monkeypatch, tmp_path, ready=False, ping=_ping(), calls=calls)
    assert report.hive_ready({}) is False
    assert calls == []


def test_signed_liveness_seq_advances_from_newest_verified_inbox_row(plane, tmp_path):
    """hq.sql.liveness: signed — the composite row is the newest *verified* beat in the
    frame's own inbox, so seq follows what was actually sent even with no receiver."""
    from datetime import UTC, datetime

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from beadhive.hq_framelease_contracts import DOMAIN_V2, HeartbeatLease
    from beadhive.hq_sql_runtime import PrincipalBinding, newest_signed_heartbeat
    from beadhive.hq_sql_signatures import canonical, fingerprint, sign_heartbeat

    _head, _state, route, slot, record, snapshot, policies, _row, lease_row = (
        plane._runtime_authority().read_frame_composite()
    )
    key = tmp_path / "frame.key"
    private = Ed25519PrivateKey.generate()
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
    route = PrincipalBinding(
        "frame_a",
        "factory",
        "host-a",
        "instance-a",
        2,
        "hq_live_inbox_frame_a_2",
        fingerprint(public),
    )
    record = {
        **record,
        "public_key": public,
        "authority": {**record["authority"], "key_fingerprint": route.signer_fingerprint},
    }
    now = datetime.now(UTC).timestamp()

    def beat(seq, age):
        lease = HeartbeatLease(
            domain=DOMAIN_V2,
            beadyard_id=record["authority"]["beadyard_id"],
            audience=record["authority"]["audience"],
            frame_id="factory",
            holderIdentity="host-a",
            instance_ref="instance-a",
            key_id=route.signer_fingerprint,
            epoch=2,
            config_revision="rev",
            seq=seq,
            renewTime=datetime.fromtimestamp(now - age, UTC).isoformat(),
            state_seen="active",
            release=record["desired"]["release"],
            report_digest="sha256:" + "2" * 64,
            conformance={"profile": "factory-v1", "status": "conformant", "checks": []},
        )
        return (canonical(sign_heartbeat(lease, signing_key=str(key))),)

    row = newest_signed_heartbeat(
        [beat(40, 900), beat(41, 600), beat(42, 30), (b"junk",)], route, record, now=now
    )
    assert row[0] == 42
    composite = ("head", {}, route, slot, record, snapshot, policies, row, lease_row)
    plane._runtime_authority = lambda: SimpleNamespace(read_frame_composite=lambda: composite)
    assert report.generate(plane).seq == 43
