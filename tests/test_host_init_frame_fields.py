"""`bh host init` on a frame host carries the fields open admission needs (bh-6se58.3).

A host with ``host.frame_id`` publishes frame_id, instance_ref, release and capabilities, each
from an explicit source; a missing input is refused naming its key. A host with no frame_id is
unchanged. Nothing probes the machine and no HQ is contacted.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from beadhive import config, host, host_cli, hosts, release_measurement
from beadhive import fleet_roster as roster
from beadhive.cli import app

runner = CliRunner()
HOST_ID = "11111111-1111-4111-8111-111111111111"
RELEASE = {"id": "0.40.0", "digest": "sha256:" + "a" * 64}
CAPS = {"isolation": "kvm", "trust_zone": "self-hosted", "harnesses": ["claude"], "max_sessions": 2}


def _write_config(document: dict) -> None:
    """The HOST config the verb reads, written for real under the sandboxed BH_HOME."""
    (config.home() / "config.yaml").write_text(json.dumps(document))


def _frame(monkeypatch, *, frame_id="frame-01", instance_ref="vm-1", caps=CAPS, release=RELEASE):
    section = {"frame_id": frame_id, "frame": {"capabilities": dict(caps)}}
    if instance_ref:
        section["instance_ref"] = instance_ref
    _write_config({"host": section})
    monkeypatch.setattr(host, "load", lambda: {"host_id": HOST_ID, "label": "frame"})
    monkeypatch.setattr(host_cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(host_cli.platform, "machine", lambda: "x86_64")
    if isinstance(release, Exception):

        def boom():
            raise release

        monkeypatch.setattr(release_measurement, "installed_release", boom)
    else:
        monkeypatch.setattr(release_measurement, "installed_release", lambda: dict(release))


def _saved():
    return hosts.load(config.hq_dir(), HOST_ID)


def test_every_field_comes_from_its_source(monkeypatch):
    _frame(monkeypatch)
    result = runner.invoke(app, ["host", "init", "--role", "executor"])
    assert result.exit_code == 0, result.output
    manifest = _saved()
    assert manifest.frame_id == "frame-01"
    assert manifest.instance_ref == "vm-1"
    assert manifest.release.model_dump() == RELEASE
    assert manifest.capabilities.model_dump() == {**CAPS, "arch": "x86_64"}
    assert manifest.state == "pending"


def test_the_flag_overrides_the_instance_ref_key(monkeypatch):
    _frame(monkeypatch)
    result = runner.invoke(app, ["host", "init", "--role", "executor", "--instance-ref", "vm-9"])
    assert result.exit_code == 0, result.output
    assert _saved().instance_ref == "vm-9"


def test_the_flag_alone_supplies_the_instance_ref(monkeypatch):
    _frame(monkeypatch, instance_ref="")
    result = runner.invoke(app, ["host", "init", "--role", "executor", "--instance-ref", "vm-9"])
    assert result.exit_code == 0, result.output
    assert _saved().instance_ref == "vm-9"


def test_a_frame_without_an_instance_ref_is_refused_naming_the_key(monkeypatch):
    _frame(monkeypatch, instance_ref="")
    result = runner.invoke(app, ["host", "init", "--role", "executor"])
    assert result.exit_code == 1
    assert "host.instance_ref" in result.output
    assert not roster.exists(config.hq_dir(), HOST_ID)


@pytest.mark.parametrize("key", ["isolation", "trust_zone", "harnesses", "max_sessions"])
def test_a_missing_capability_is_refused_naming_its_key(monkeypatch, key):
    _frame(monkeypatch, caps={k: v for k, v in CAPS.items() if k != key})
    result = runner.invoke(app, ["host", "init", "--role", "executor"])
    assert result.exit_code == 1
    assert f"host.frame.capabilities.{key}" in result.output
    assert not roster.exists(config.hq_dir(), HOST_ID)


def test_an_unmeasurable_release_is_refused(monkeypatch):
    _frame(monkeypatch, release=RuntimeError("editable installs cannot attest"))
    result = runner.invoke(app, ["host", "init", "--role", "executor"])
    assert result.exit_code == 1
    assert "installed release" in result.output
    assert not roster.exists(config.hq_dir(), HOST_ID)


def test_an_invalid_capability_value_is_a_schema_error(monkeypatch):
    _frame(monkeypatch, caps={**CAPS, "isolation": "jail"})
    result = runner.invoke(app, ["host", "init", "--role", "executor"])
    assert result.exit_code != 0
    assert "host.frame.capabilities.isolation" in str(result.exception)


def test_no_clobber_keeps_an_existing_manifest_and_force_refreshes_it(monkeypatch):
    _frame(monkeypatch)
    assert runner.invoke(app, ["host", "init", "--role", "executor"]).exit_code == 0
    _frame(monkeypatch, instance_ref="vm-2")
    kept = runner.invoke(app, ["host", "init", "--role", "executor"])
    assert "exists" in kept.output and _saved().instance_ref == "vm-1"
    assert runner.invoke(app, ["host", "init", "--role", "executor", "--force"]).exit_code == 0
    assert _saved().instance_ref == "vm-2"


def test_a_host_without_a_frame_id_is_unchanged(monkeypatch):
    _write_config({"host": {}})
    monkeypatch.setattr(host, "load", lambda: {"host_id": HOST_ID, "label": "plain"})

    def never():
        raise AssertionError("a non-frame host must not measure the release")

    monkeypatch.setattr(release_measurement, "installed_release", never)
    result = runner.invoke(app, ["host", "init", "--role", "executor"])
    assert result.exit_code == 0, result.output
    manifest = _saved()
    assert (manifest.frame_id, manifest.instance_ref, manifest.release, manifest.capabilities) == (
        None,
        None,
        None,
        None,
    )


def test_an_api_frame_binds_the_hq_instance_and_is_refused_without_it(monkeypatch):
    yard = "12345678-1234-4234-8234-123456789abc"
    _frame(monkeypatch)
    base = config.load_host()
    _write_config({**base, "hq": {"route": "api", "api": {"url": "https://hq.test"}}})
    with pytest.raises(host_cli.FrameManifestError, match="hq.beadyard_id"):
        host_cli.frame_manifest_fields(arch="x86_64")
    _write_config(
        {**base, "hq": {"route": "api", "api": {"url": "https://hq.test"}, "beadyard_id": yard}}
    )
    assert host_cli.frame_manifest_fields(arch="x86_64")["beadyard_id"] == yard
