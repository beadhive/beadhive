"""Focused contracts for the process-local effective-config read cache."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from ruamel.yaml.error import YAMLError

from beadhive import config, config_store, hq_control_plane
from beadhive.modules.config.domain.ports import FleetConfigDocument


def _write_layers(home: Path) -> tuple[Path, Path]:
    host = home / "config.yaml"
    fleet = home / "hq" / "fleet.yaml"
    fleet.parent.mkdir(parents=True)
    host.write_text("otel:\n  enabled: false\n")
    fleet.write_text("work:\n  validate_cmd: focused\n")
    return host, fleet


def test_repeated_load_parses_each_unchanged_layer_once(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    _write_layers(tmp_path)
    config_store.clear_load_cache()
    original = config_store.load_path
    calls: list[Path] = []

    def counted(api, path, *, missing_ok=False):
        calls.append(path)
        return original(api, path, missing_ok=missing_ok)

    monkeypatch.setattr(config_store, "load_path", counted)
    assert config.load() == config.load()
    assert calls == [config.fleet_path(), config.config_path()]


def test_concurrent_first_legacy_load_parses_each_layer_once(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    _write_layers(tmp_path)
    config_store.clear_load_cache()
    original = config_store.load_path
    calls: list[Path] = []
    ready = threading.Barrier(3)
    values = []

    def counted(api, path, *, missing_ok=False):
        calls.append(path)
        return original(api, path, missing_ok=missing_ok)

    def load_together():
        ready.wait()
        values.append(config.load())

    monkeypatch.setattr(config_store, "load_path", counted)
    workers = [threading.Thread(target=load_together) for _ in range(2)]
    for worker in workers:
        worker.start()
    ready.wait()
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()
    assert values[0] == values[1]
    assert calls == [config.fleet_path(), config.config_path()]


def test_caller_mutation_cannot_corrupt_the_cached_view(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    _write_layers(tmp_path)
    config_store.clear_load_cache()

    first = config.load()
    first["otel"]["enabled"] = True

    assert config.load()["otel"]["enabled"] is False


def test_external_host_and_fleet_edits_are_observed(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    host, fleet = _write_layers(tmp_path)
    config_store.clear_load_cache()
    assert config.load()["work"]["validate_cmd"] == "focused"

    host.write_text("otel:\n  enabled: true\n")
    fleet.write_text("work:\n  validate_cmd: changed\n")
    # Defend the contract even on filesystems whose timestamp granularity is coarse.
    os.utime(host, ns=(host.stat().st_atime_ns, host.stat().st_mtime_ns + 1))
    os.utime(fleet, ns=(fleet.stat().st_atime_ns, fleet.stat().st_mtime_ns + 1))

    loaded = config.load()
    assert loaded["otel"]["enabled"] is True
    assert loaded["work"]["validate_cmd"] == "changed"


def test_set_then_load_observes_the_in_process_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    _write_layers(tmp_path)
    config_store.clear_load_cache()
    assert config.load()["otel"]["enabled"] is False

    result = config.set_value("otel.enabled", "true")

    assert result["ok"] is True
    assert config.load()["otel"]["enabled"] is True


def test_explicit_sql_host_switch_ignores_local_fleet_and_never_memoizes_outage(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    host = config.config_path()
    fleet = config.fleet_path()
    fleet.parent.mkdir(parents=True)
    binding = {
        "enabled": True,
        "reader": {
            "host": "sql.example.test", "database": "beadhive_hq_config",
            "user": "reader", "server_name": "sql.example.test",
            "ca_file": str(tmp_path / "ca.pem"),
            "credential": {
                "config_path": str(tmp_path / "fnox.toml"),
                "profile": "test", "key": "SQL_READER",
            },
        },
        "floor_path": str(tmp_path / "floor.json"),
        "backend_identity": "fixture", "generation": "fixture-generation",
        "initial_revision": "a" * 32,
    }

    def set_host(enabled):
        host.write_text(json.dumps({"hq": {"sql": {**binding, "enabled": enabled}}}))

    config_store.clear_load_cache()
    set_host(True)
    calls = []
    unavailable = False

    def sql_snapshot(*, bootstrap):
        assert bootstrap["sql"]["enabled"] is True
        calls.append("read")
        if unavailable:
            raise hq_control_plane.ControlPlaneError("committed SQL config unavailable")
        return None, SimpleNamespace(
            documents=(FleetConfigDocument(
                "fleet.yaml", "hq:\n  mode: dolt-server\nwork:\n  validate_cmd: central\n"
            ),)
        )

    monkeypatch.setattr(hq_control_plane, "attach_fleet_config", sql_snapshot)
    assert config.load()["work"]["validate_cmd"] == "central"  # absent local checkout
    fleet.write_text("[malformed YAML")
    assert config.load()["work"]["validate_cmd"] == "central"
    assert len(calls) == 2  # authenticated SQL is read again, never filesystem-memoized
    unavailable = True
    with pytest.raises(hq_control_plane.ControlPlaneError, match="SQL config unavailable"):
        config.load()
    unavailable = False
    with monkeypatch.context() as local:
        load_path = config_store.load_path

        def unreadable_fleet(api, path, *, missing_ok=False):
            if path == fleet:
                raise PermissionError("optional local fleet unreadable")
            return load_path(api, path, missing_ok=missing_ok)

        local.setattr(config_store, "load_path", unreadable_fleet)
        signature = config_store._file_signature

        def unreadable_signature(path):
            if path == fleet:
                raise PermissionError("optional local fleet stat denied")
            return signature(path)

        local.setattr(
            config_store, "_file_signature",
            unreadable_signature,
        )
        assert config.load()["work"]["validate_cmd"] == "central"  # unreadable local fleet

    set_host(False)
    with pytest.raises(YAMLError):
        config.load()  # disabled selector does not ignore malformed local fleet
    fleet.write_text("work:\n  validate_cmd: local\n")
    assert config.load()["work"]["validate_cmd"] == "local"
    before = len(calls)
    assert config.load()["work"]["validate_cmd"] == "local"
    assert len(calls) == before
    set_host(True)
    assert config.load()["work"]["validate_cmd"] == "central"
    set_host("false")
    with pytest.raises(ValueError, match="invalid SQL HOST bootstrap"):
        config.load()
