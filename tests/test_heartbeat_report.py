from io import StringIO
from types import SimpleNamespace

import pytest
from ruamel.yaml import YAML

from beadhive import heartbeat_report as report
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
        report.importlib.metadata, "distribution", lambda _: SimpleNamespace(files=[])
    )
    with pytest.raises(HeartbeatError, match="editable"):
        report.installed_release()


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
    monkeypatch.setattr(report.importlib.metadata, "distribution", lambda _: distribution)
    digest = hashlib.sha256()
    for name in sorted(files[:2]):
        digest.update(name.encode() + b"\0" + hashlib.sha256(name.encode()).digest())
    assert report.installed_release() == {"id": "0.21.3", "digest": "sha256:" + digest.hexdigest()}


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
