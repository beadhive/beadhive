"""config.observaloop_enabled + observaloop_profile_name — accessor unit tests.

Covers:
- observaloop_enabled defaults false
- otel-disabled forces observaloop_enabled to false regardless of flag
- per-rig entry > global > default precedence for the enable flag
- observaloop_profile_name determinism and sanitization
- observaloop_profile_name from entry dict vs rig-id string
- unresolvable rig id returns empty string
"""

from __future__ import annotations

from beadhive import config

# ---- observaloop_enabled ----------------------------------------------------


def test_enabled_false_by_default():
    assert config.observaloop_enabled({}) is False


def test_enabled_false_when_otel_disabled_global_flag_set():
    # otel is off → observaloop must be False regardless of its own flag
    cfg = {"otel": {"enabled": False}, "observaloop": {"enabled": True}}
    assert config.observaloop_enabled(cfg) is False


def test_enabled_false_when_otel_disabled_hive_flag_set():
    cfg = {"otel": {"enabled": False}}
    entry = {"observaloop": {"enabled": True}}
    assert config.observaloop_enabled(cfg, entry) is False


def test_enabled_false_when_otel_enabled_but_flag_absent():
    # otel on, but observaloop flag not set → still False
    cfg = {"otel": {"enabled": True}}
    assert config.observaloop_enabled(cfg) is False


def test_enabled_true_when_otel_and_global_flag_set():
    cfg = {"otel": {"enabled": True}, "observaloop": {"enabled": True}}
    assert config.observaloop_enabled(cfg) is True


def test_enabled_true_when_otel_and_global_flag_set_no_entry():
    cfg = {"otel": {"enabled": True}, "observaloop": {"enabled": True}}
    assert config.observaloop_enabled(cfg, None) is True


def test_enabled_true_when_otel_and_hive_flag_set():
    cfg = {"otel": {"enabled": True}}
    entry = {"observaloop": {"enabled": True}}
    assert config.observaloop_enabled(cfg, entry) is True


def test_hive_entry_overrides_global_false():
    # global flag off, rig flag on → rig wins → True (otel on)
    cfg = {"otel": {"enabled": True}, "observaloop": {"enabled": False}}
    entry = {"observaloop": {"enabled": True}}
    assert config.observaloop_enabled(cfg, entry) is True


def test_hive_entry_overrides_global_true():
    # global flag on, rig flag off → rig wins → False
    cfg = {"otel": {"enabled": True}, "observaloop": {"enabled": True}}
    entry = {"observaloop": {"enabled": False}}
    assert config.observaloop_enabled(cfg, entry) is False


def test_hive_entry_without_observaloop_key_falls_back_to_global():
    cfg = {"otel": {"enabled": True}, "observaloop": {"enabled": True}}
    entry = {}  # no observaloop key → fall back to global → True
    assert config.observaloop_enabled(cfg, entry) is True


def test_hive_entry_with_empty_observaloop_section_falls_back_to_global():
    cfg = {"otel": {"enabled": True}, "observaloop": {"enabled": True}}
    entry = {"observaloop": {}}  # observaloop section present but no 'enabled' key
    assert config.observaloop_enabled(cfg, entry) is True


# ---- _sanitize_profile_name (internal helper) -------------------------------


def test_sanitize_lowercase():
    assert config._sanitize_profile_name("MyRig") == "myrig"


def test_sanitize_underscores_become_hyphens():
    assert config._sanitize_profile_name("my_rig") == "my-rig"


def test_sanitize_dots_become_hyphens():
    assert config._sanitize_profile_name("my.rig") == "my-rig"


def test_sanitize_consecutive_hyphens_collapsed():
    assert config._sanitize_profile_name("my--rig") == "my-rig"


def test_sanitize_special_chars_become_hyphens():
    assert config._sanitize_profile_name("my rig/v2") == "my-rig-v2"


def test_sanitize_strips_leading_trailing_hyphens():
    assert config._sanitize_profile_name("-myrig-") == "myrig"


def test_sanitize_already_valid_unchanged():
    assert config._sanitize_profile_name("my-rig-42") == "my-rig-42"


def test_sanitize_deterministic():
    s = "Acme/Workspace_v1.0"
    assert config._sanitize_profile_name(s) == config._sanitize_profile_name(s)


# ---- observaloop_profile_name -----------------------------------------------


def test_profile_name_from_entry_simple_prefix():
    entry = {"prefix": "ws"}
    assert config.observaloop_profile_name({}, entry) == "ws"


def test_profile_name_from_entry_sanitizes_prefix():
    entry = {"prefix": "My_Rig.v2"}
    assert config.observaloop_profile_name({}, entry) == "my-rig-v2"


def test_profile_name_deterministic_same_entry():
    entry = {"prefix": "acme-api"}
    assert config.observaloop_profile_name({}, entry) == config.observaloop_profile_name({}, entry)


def test_profile_name_from_string_hive_id():
    cfg = {
        "managed_repos": [
            {"provider": "github", "org": "acme", "repo": "api", "prefix": "ac-api"}
        ]
    }
    assert config.observaloop_profile_name(cfg, "ac-api") == "ac-api"


def test_profile_name_from_string_hive_id_sanitized():
    cfg = {
        "managed_repos": [
            {"provider": "github", "org": "acme", "repo": "api", "prefix": "Ac_API"}
        ]
    }
    assert config.observaloop_profile_name(cfg, "Ac_API") == "ac-api"


def test_profile_name_from_string_hive_id_not_found_returns_empty():
    cfg = {"managed_repos": []}
    assert config.observaloop_profile_name(cfg, "nonexistent") == ""


def test_profile_name_from_entry_missing_prefix_returns_empty():
    entry = {}  # no prefix key
    assert config.observaloop_profile_name({}, entry) == ""


def test_profile_name_from_entry_none_prefix_returns_empty():
    entry = {"prefix": None}
    assert config.observaloop_profile_name({}, entry) == ""


def test_profile_name_multiple_hives_resolves_correct_one():
    cfg = {
        "managed_repos": [
            {"provider": "github", "org": "acme", "repo": "api", "prefix": "ac-api"},
            {"provider": "github", "org": "acme", "repo": "ui", "prefix": "ac-ui"},
        ]
    }
    assert config.observaloop_profile_name(cfg, "ac-ui") == "ac-ui"
    assert config.observaloop_profile_name(cfg, "ac-api") == "ac-api"


# ---- CLI-metrics collector preset asset -------------------------------------


def _load_preset():
    """Parse the shipped preset YAML the way the rig will (ruamel, like every other ws asset)."""
    from ruamel.yaml import YAML

    path = config.observaloop_metrics_preset_asset()
    assert path.exists(), f"preset asset not bundled: {path}"
    return YAML(typ="safe").load(path.read_text())


def test_metrics_preset_asset_parses():
    preset = _load_preset()
    assert isinstance(preset, dict)
    assert set(preset) == {"processors", "metrics_pipeline_processors"}


def test_metrics_preset_has_three_processors():
    procs = _load_preset()["processors"]
    assert set(procs) == {
        "resource/strip_instance",
        "transform/promote_bh_attrs",
        "deltatocumulative",
    }


def test_metrics_preset_strip_instance_deletes_service_instance_id():
    attrs = _load_preset()["processors"]["resource/strip_instance"]["attributes"]
    assert {"key": "service.instance.id", "action": "delete"} in attrs


def test_metrics_preset_promote_bh_attrs_ottl_statements():
    """transform/promote_bh_attrs is OTTL, context=datapoint, with four guarded set() copies."""
    transform = _load_preset()["processors"]["transform/promote_bh_attrs"]
    block = transform["metric_statements"][0]
    assert block["context"] == "datapoint"
    statements = block["statements"]
    for attr in ("bh.rig", "bh.worktree", "bh.role", "observaloop.profile"):
        expected = (
            f'set(attributes["{attr}"], resource.attributes["{attr}"]) '
            f'where resource.attributes["{attr}"] != nil'
        )
        assert expected in statements


def test_metrics_preset_has_deltatocumulative():
    procs = _load_preset()["processors"]
    assert "deltatocumulative" in procs


def test_metrics_preset_pipeline_order():
    """metrics pipeline runs strip → promote → accumulate after resource/profile, before batch."""
    order = _load_preset()["metrics_pipeline_processors"]
    assert order == [
        "resource/profile",
        "resource/strip_instance",
        "transform/promote_bh_attrs",
        "deltatocumulative",
        "batch",
    ]
