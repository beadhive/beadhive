"""``intake.external``: the host-owned, default-off configuration of external issue intake."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from beadhive.modules.config.application import partition
from beadhive.modules.config.contracts import (
    BeadhiveConfig,
    IntakeConfig,
    IntakeExternalConfig,
    ManagedRepoEntry,
    iter_schema_fields,
)


def test_intake_is_absent_by_default_and_every_switch_is_off() -> None:
    assert BeadhiveConfig().intake is None
    external = IntakeConfig().external
    assert external == IntakeExternalConfig()
    assert external.enabled is False and external.default_type == "bug"


def test_a_full_documented_section_validates() -> None:
    config = BeadhiveConfig.model_validate(
        {"intake": {"external": {"enabled": True, "default_type": "feature"}}}
    )

    assert config.intake is not None
    assert (config.intake.external.enabled, config.intake.external.default_type) == (
        True,
        "feature",
    )


@pytest.mark.parametrize("value", ["true", "false", 1, 0])
def test_enabled_is_a_strict_boolean(value) -> None:
    with pytest.raises(ValidationError):
        IntakeExternalConfig.model_validate({"enabled": value})


@pytest.mark.parametrize(
    "section",
    [
        {"external": {"default_type": "epic"}},
        {"external": {"token": "x"}},  # unknown keys fail closed (extra=forbid)
        {"external": {"outbox": {"enabled": True}}},  # a later bead's key is not accepted early
        {"other": {}},
    ],
)
def test_unknown_values_and_keys_fail_closed(section) -> None:
    with pytest.raises(ValidationError):
        BeadhiveConfig.model_validate({"intake": section})


def test_a_validation_error_never_repeats_the_input() -> None:
    planted = "ghp_" + "a" * 36
    with pytest.raises(ValidationError) as error:
        BeadhiveConfig.model_validate({"intake": {"external": {"default_type": planted}}})
    assert planted not in str(error.value)


def test_intake_is_host_owned_so_a_fleet_push_cannot_enable_it() -> None:
    paths = [f.path for f in iter_schema_fields() if f.path.startswith("intake")]

    assert "intake.external.enabled" in paths and "intake.external.default_type" in paths
    assert {partition.partition_of(path) for path in paths} == {partition.HOST}


def test_there_is_no_per_hive_override_a_fleet_document_could_carry() -> None:
    assert "intake" not in ManagedRepoEntry.model_fields


def test_no_intake_key_holds_a_secret_a_grant_or_an_approval() -> None:
    names = {f.path.rsplit(".", 1)[-1] for f in iter_schema_fields() if f.path.startswith("intake")}

    assert names == {"intake", "external", "enabled", "default_type"}
    assert not {"token", "grant", "approve", "write", "credential", "tracker"} & names
