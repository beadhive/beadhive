"""``intake.external``: the host-owned, default-off configuration of external issue intake."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from beadhive.modules.config.application import partition
from beadhive.modules.config.contracts import (
    BeadhiveConfig,
    IntakeConfig,
    IntakeDiscoveryConfig,
    IntakeExternalConfig,
    ManagedRepoEntry,
    iter_schema_fields,
)


def test_intake_is_absent_by_default_and_every_switch_is_off() -> None:
    assert BeadhiveConfig().intake is None
    external = IntakeConfig().external
    assert external == IntakeExternalConfig()
    assert external.enabled is False and external.default_type == "bug"
    assert external.completion_policy == "close-on-merge"
    assert external.outbox.enabled is False and external.outbox.mode == "supervised"
    assert external.outbox.labels_enabled is False  # the labels.change gate has its own switch
    assert not any(vars(external.milestones).values())  # no milestone drafts unless asked


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
        {"external": {"outbox": {"mode": "auto"}}},  # supervised is the only mode (D2)
        {"external": {"outbox": {"labels": {"on_delivery": "x"}}}},  # a later bead's key
        {"external": {"completion_policy": "close-never"}},
        {"external": {"milestones": {"draft_on_start": True}}},
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

    assert names == {
        "intake",
        "external",
        "enabled",
        "default_type",
        "discovery",
        "interval_seconds",
        "full_reconcile_every",
        "completion_policy",
        "milestones",
        "draft_on_merge",
        "draft_on_release",
        "append_release_to_closed",
        "outbox",
        "labels_enabled",
        "mode",
    }
    assert not {"token", "grant", "approve", "write", "credential", "tracker"} & names


# -- intake.external.discovery (bh-df64b): the opt-in daemon timer, off by default --


def test_discovery_is_off_by_default_and_has_no_default_cadence() -> None:
    discovery = IntakeConfig().external.discovery

    assert discovery == IntakeDiscoveryConfig()
    assert (discovery.enabled, discovery.interval_seconds) == (False, None)
    assert discovery.full_reconcile_every == 24


def test_a_discovery_section_validates_when_a_cadence_is_chosen() -> None:
    config = BeadhiveConfig.model_validate(
        {
            "intake": {
                "external": {
                    "enabled": True,
                    "discovery": {
                        "enabled": True,
                        "interval_seconds": 900,
                        "full_reconcile_every": 6,
                    },
                }
            }
        }
    )

    assert config.intake is not None
    discovery = config.intake.external.discovery
    assert (discovery.enabled, discovery.interval_seconds, discovery.full_reconcile_every) == (
        True,
        900.0,
        6,
    )


@pytest.mark.parametrize(
    "section",
    [
        {"enabled": True},  # enabling the timer without choosing an interval
        {"enabled": "true", "interval_seconds": 60},
        {"enabled": True, "interval_seconds": 0},
        {"enabled": True, "interval_seconds": -5},
        {"interval_seconds": 60, "full_reconcile_every": 0},
        {"token": "x"},  # unknown keys fail closed on every host
    ],
)
def test_a_bad_discovery_section_fails_closed(section) -> None:
    with pytest.raises(ValidationError):
        BeadhiveConfig.model_validate({"intake": {"external": {"discovery": section}}})


def test_discovery_is_host_owned_so_a_fleet_push_cannot_start_the_timer() -> None:
    paths = [f.path for f in iter_schema_fields() if f.path.startswith("intake.external.discovery")]

    assert {
        "intake.external.discovery.enabled",
        "intake.external.discovery.interval_seconds",
        "intake.external.discovery.full_reconcile_every",
    } <= set(paths)
    assert {partition.partition_of(path) for path in paths} == {partition.HOST}


def test_the_milestone_and_outbox_switches_validate_strictly_and_stay_off_by_default() -> None:
    config = BeadhiveConfig.model_validate(
        {
            "intake": {
                "external": {
                    "enabled": True,
                    "completion_policy": "close-on-release",
                    "milestones": {"draft_on_merge": True, "draft_on_release": True},
                    "outbox": {"enabled": True, "mode": "supervised"},
                }
            }
        }
    )

    assert config.intake is not None
    external = config.intake.external
    assert external.completion_policy == "close-on-release"
    assert (external.milestones.draft_on_merge, external.milestones.draft_on_release) == (
        True,
        True,
    )
    assert external.milestones.append_release_to_closed is False
    assert external.outbox.enabled is True
    assert external.outbox.labels_enabled is False  # enabling the outbox never enables labels
    for section in (
        {"milestones": {"draft_on_merge": "yes"}},
        {"outbox": {"enabled": 1}},
        {"outbox": {"labels_enabled": 1}},
        {"outbox": {"labels_enabled": "true"}},
    ):
        with pytest.raises(ValidationError):
            IntakeExternalConfig.model_validate(section)


def test_the_labels_gate_is_its_own_host_owned_strict_switch() -> None:
    labels = {"intake": {"external": {"outbox": {"enabled": True, "labels_enabled": True}}}}
    config = BeadhiveConfig.model_validate(labels)

    assert config.intake is not None
    assert config.intake.external.outbox.labels_enabled is True
    assert partition.partition_of("intake.external.outbox.labels_enabled") == partition.HOST
