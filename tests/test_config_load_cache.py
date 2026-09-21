"""Focused contracts for the process-local effective-config read cache."""

from __future__ import annotations

import os
from pathlib import Path

from beadhive import config, config_store


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
