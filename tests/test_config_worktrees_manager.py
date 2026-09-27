"""``worktrees.manager`` selection (bh-055ot.1): explicit, native by default, refused otherwise."""

from __future__ import annotations

import pytest

from beadhive import config, config_schema


def test_manager_defaults_to_native():
    assert config.worktrees_manager({}) == "native"
    assert config.worktrees_manager({"worktrees": {}}) == "native"


def test_native_is_accepted_explicitly():
    assert config.worktrees_manager({"worktrees": {"manager": "native"}}) == "native"


@pytest.mark.parametrize("value", ["herdr", "orca", "", "NATIVE"])
def test_any_other_value_is_refused_with_a_clear_message(value):
    with pytest.raises(config.ConfigError) as refused:
        config.worktrees_manager({"worktrees": {"manager": value}})

    message = str(refused.value)
    assert f"worktrees.manager = {value!r} is not supported" in message
    assert "allowed: native" in message
    assert "config set worktrees.manager native" in message


def test_schema_declares_native_as_the_only_choice():
    assert config_schema.literal_choices("worktrees.manager") == ("native",)
    assert config_schema.field_default("worktrees.manager") == "native"


def test_config_set_refuses_a_non_native_manager():
    problems = config._validate(["worktrees", "manager"], "herdr")
    assert any(problem["level"] == "error" for problem in problems)


def test_load_time_nudge_does_not_claim_the_default_is_in_effect(monkeypatch):
    from structlog.testing import capture_logs

    from beadhive import log

    monkeypatch.setattr(config, "load", lambda: {"worktrees": {"manager": "herdr"}})
    log.get_logger("warmup")
    with capture_logs() as captured:
        config.warn_literal_violations_if_needed()

    (warning,) = [e for e in captured if e["event"] == "config_literal_value_invalid"]
    assert warning["key"] == "worktrees.manager"
    assert "refuse until it is fixed" in warning["hint"]
    assert "using default" not in warning["hint"]
