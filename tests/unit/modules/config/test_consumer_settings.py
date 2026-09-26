from __future__ import annotations

import pytest

from beadhive import config
from beadhive.config_consumer_ports import daemon_settings, work_settings
from beadhive.modules.config.application.consumer_settings import CapabilitySettings


class _MemorySource:
    def __init__(self) -> None:
        self.values = {"enabled": True}

    def get_value(self, name):
        return self.values[name]

    def set_value(self, name, value):
        self.values[name] = value

    def delete_value(self, name):
        del self.values[name]


def test_capability_settings_exposes_only_its_exact_contract():
    settings = CapabilitySettings("sentinel", {"enabled"}, _MemorySource())

    assert settings.capability == "sentinel"
    assert settings.allowed_names == ("enabled",)
    assert settings.enabled is True
    with pytest.raises(AttributeError, match="does not expose 'secret'"):
        _ = settings.secret


def test_outward_adapter_preserves_dynamic_legacy_patch_points(monkeypatch):
    expected = {"work": {"validation": "loose"}}
    monkeypatch.setattr(config, "load", lambda: expected)

    assert work_settings.load() is expected


def test_worktree_safety_settings_are_exposed_by_the_narrow_work_port():
    assert {
        "precious_globs",
        "junk_globs",
        "precious_min_bytes",
    } <= set(work_settings.allowed_names)


def test_host_daemon_settings_are_exposed_by_the_narrow_daemon_port():
    assert set(daemon_settings.allowed_names) == {
        "BINARY_ALIAS",
        "home",
        "load",
        "otel_flush_timeout",
    }


def test_patching_a_consumer_port_replaces_the_legacy_facade_name(monkeypatch):
    def replacement():
        return {"patched": True}

    monkeypatch.setattr(work_settings, "load", replacement)

    assert config.load is replacement
    assert work_settings.load is replacement
