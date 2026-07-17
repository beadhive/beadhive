"""validate_config() (config_validate.py) — the schema validator over a loaded config dict.

Covers the three acceptance cases for bh-5cgm.2 (renamed ws-era key → actionable message
naming the new key; wrong-type value → error; clean current config → no problems) plus the
schema_version staleness + old-home-path warnings and the ws→bh rename table.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

from beadhive import config
from beadhive.config_schema import SCHEMA_VERSION
from beadhive.config_validate import (
    RENAMED_KEYS,
    renamed_key_table,
    validate_config,
)


def _load_example() -> dict:
    y = YAML()
    return dict(y.load(Path(config.template("config.example.yaml")).read_text()))


def _errors(problems) -> list[dict]:
    return [p for p in problems if p["level"] == "error"]


def _messages(problems) -> str:
    return "\n".join(p["message"] for p in problems)


# ---- clean current config ----------------------------------------------------


def test_clean_current_config_yields_no_problems():
    assert validate_config(_load_example()) == []


# ---- renamed ws-era keys -----------------------------------------------------


def test_renamed_keys_report_actionable_message_naming_the_new_key():
    stale = {
        "schema_version": SCHEMA_VERSION,
        "otel": {"rig": "my-hive"},
        "git_workspace": {"rig_match": "prefix"},
    }
    problems = validate_config(stale)
    msg = _messages(problems)
    # each renamed key is called out with its current name
    assert "otel.rig" in msg and "otel.hive" in msg
    assert "git_workspace.rig_match" in msg and "git_workspace.hive_match" in msg
    # a renamed key is an actionable error, not opaque pydantic "extra_forbidden"
    assert "extra_forbidden" not in msg
    assert _errors(problems), "renamed keys must gate (error-level)"


def test_renamed_key_map_matches_authoritative_rig_to_hive_renames():
    assert RENAMED_KEYS == {
        "otel.rig": "otel.hive",
        "git_workspace.rig_match": "git_workspace.hive_match",
    }


# ---- wrong-type value --------------------------------------------------------


def test_wrong_type_value_yields_error():
    # a non-boolean string for a bool field (pydantic coerces boolish "yes"/"no" but rejects
    # a genuine non-bool like "maybe" — see bh-5cgm.2 notes on lax settings coercion).
    problems = validate_config(
        {"schema_version": SCHEMA_VERSION, "worktrees": {"ephemeral": "maybe"}}
    )
    errs = _errors(problems)
    assert errs, "a wrong-type value must produce an error"
    assert any("worktrees.ephemeral" in e["message"] for e in errs)


def test_unknown_key_is_an_error():
    problems = validate_config({"schema_version": SCHEMA_VERSION, "totally_bogus": 1})
    assert any("totally_bogus" in e["message"] for e in _errors(problems))


def test_unknown_key_close_to_a_real_key_gets_a_did_you_mean():
    # A typo of a known key (`providers`) is still an error, but now carries a did-you-mean
    # suggestion routed through config_schema.suggest_key — the same helper `.4` wired into
    # config.py's get/set/unset error paths.
    problems = validate_config({"schema_version": SCHEMA_VERSION, "providrs": ["github"]})
    errs = _errors(problems)
    assert any("providrs" in e["message"] for e in errs)
    assert any("did you mean `providers`" in e["message"] for e in errs)


def test_hopelessly_wrong_unknown_key_gets_no_suggestion():
    # A key with no close match stays a bare unknown-key error — no false-positive suggestion.
    problems = validate_config({"schema_version": SCHEMA_VERSION, "totally_bogus": 1})
    assert not any("did you mean" in e["message"] for e in _errors(problems))


# ---- schema_version staleness ------------------------------------------------


def test_missing_schema_version_warns():
    problems = validate_config({"providers": ["github"]})
    assert any(p["level"] == "warning" and "schema_version" in p["message"] for p in problems)


def test_newer_schema_version_is_an_error():
    problems = validate_config({"schema_version": SCHEMA_VERSION + 1})
    assert any("newer" in e["message"] for e in _errors(problems))


# ---- old-home path values ----------------------------------------------------


def test_old_ws_home_path_value_warns():
    problems = validate_config(
        {"schema_version": SCHEMA_VERSION, "worktrees": {"path": "~/.ws/wt"}}
    )
    assert any(
        p["level"] == "warning"
        and "worktrees.path" in p["message"]
        and "~/.beadhive" in p["message"]
        for p in problems
    )


# ---- rename table ------------------------------------------------------------


def test_renamed_key_table_lists_every_rename():
    table = "\n".join(renamed_key_table())
    for token in (
        "otel.rig",
        "otel.hive",
        "rig_match",
        "hive_match",
        "~/.ws",
        "~/.beadhive",
        "WS_*",
        "BH_*",
    ):
        assert token in table
