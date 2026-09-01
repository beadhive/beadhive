"""Legacy facade contracts for resolution, migrations, and future-version edits."""

from __future__ import annotations

import pytest

from beadhive import config


@pytest.mark.parametrize(
    ("raw_version", "code"),
    [
        ("2", "future_schema_version"),
        ('"2"', "invalid_schema_version"),
        ("2.0", "invalid_schema_version"),
        ("true", "invalid_schema_version"),
        ("null", "invalid_schema_version"),
        ("[]", "invalid_schema_version"),
        ("{}", "invalid_schema_version"),
        ('"raw-version-secret-canary"', "invalid_schema_version"),
    ],
)
def test_future_or_invalid_raw_host_version_is_opaque_to_set_and_unset(raw_version, code):
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    canary = "future-secret-canary"
    original = f"schema_version: {raw_version}\notel:\n  token: {canary}\n".encode()
    path.write_bytes(original)

    set_result = config.set_value("otel.enabled", "true")
    unset_result = config.unset_value("otel.token")

    assert set_result["ok"] is False
    assert unset_result["ok"] is False
    assert set_result["problems"][0]["message"].startswith(f"{code}:")
    assert unset_result["problems"][0]["message"].startswith(f"{code}:")
    assert "layer=host" in set_result["problems"][0]["message"]
    assert "layer=host" in unset_result["problems"][0]["message"]
    assert canary not in repr(set_result)
    assert canary not in repr(unset_result)
    assert "raw-version-secret-canary" not in repr(set_result)
    assert "raw-version-secret-canary" not in repr(unset_result)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    ("raw_version", "code"),
    [
        ("2", "future_schema_version"),
        ('"2"', "invalid_schema_version"),
        ("2.0", "invalid_schema_version"),
        ("true", "invalid_schema_version"),
        ("null", "invalid_schema_version"),
        ("[]", "invalid_schema_version"),
        ("{}", "invalid_schema_version"),
        ('"raw-version-secret-canary"', "invalid_schema_version"),
    ],
)
def test_future_or_invalid_raw_fleet_version_refuses_edits_as_fleet(monkeypatch, raw_version, code):
    path = config.fleet_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    canary = "fleet-secret-canary"
    original = f"schema_version: {raw_version}\notel:\n  token: {canary}\n".encode()
    path.write_bytes(original)
    saved = []
    monkeypatch.setattr(config, "save_fleet", saved.append)

    set_result = config.set_value("otel.enabled", "true", scope=config.SCOPE_FLEET)
    unset_result = config.unset_value("otel.token", scope=config.SCOPE_FLEET)

    for result in (set_result, unset_result):
        assert result["ok"] is False
        message = result["problems"][0]["message"]
        assert message.startswith(f"{code}:")
        assert "layer=fleet" in message
        assert canary not in repr(result)
        assert "raw-version-secret-canary" not in repr(result)
    assert saved == []
    assert path.read_bytes() == original


def test_honest_current_integer_version_remains_mutable():
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("schema_version: 1\notel:\n  enabled: false\n")

    result = config.set_value("otel.enabled", "true")

    assert result["ok"] is True
    assert config.load_host()["schema_version"] == 1
    assert config.load_host()["otel"]["enabled"] is True


def test_legacy_migration_patch_points_still_drive_load_save_and_diagnostics(monkeypatch):
    source = {"otel": {"rig": "legacy"}}
    saved = []
    warnings = []
    monkeypatch.setattr(config, "load_host", lambda: source)
    monkeypatch.setattr(config, "save", saved.append)
    monkeypatch.setattr(
        config,
        "_warning",
        lambda event, **fields: warnings.append((event, fields)),
    )

    config.migrate_hive_keys_if_needed()

    assert saved == [{"otel": {"hive": "legacy"}}]
    assert warnings[0][0] == "hive_config_keys_migrated"
    assert warnings[0][1]["migrated"] == ["otel.rig -> otel.hive"]
    assert source == {"otel": {"rig": "legacy"}}


@pytest.mark.parametrize("raw_version", ["2", 2.0, True, None, [], {}])
def test_legacy_migration_guard_refuses_raw_invalid_versions_before_save(monkeypatch, raw_version):
    source = {
        "schema_version": raw_version,
        "otel": {"rig": "migration-secret-canary"},
    }
    saved = []
    monkeypatch.setattr(config, "load_host", lambda: source)
    monkeypatch.setattr(config, "save", saved.append)

    with pytest.raises(config.ConfigError) as captured:
        config.migrate_hive_keys_if_needed()

    assert saved == []
    assert "migration-secret-canary" not in str(captured.value)
