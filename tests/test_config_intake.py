"""``intake.external``: the host-owned, default-off configuration of external issue intake."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from beadhive.modules.config.application import partition
from beadhive.modules.config.contracts import (
    BeadhiveConfig,
    IntakeCommentsConfig,
    IntakeConfig,
    IntakeDiscoveryConfig,
    IntakeExternalConfig,
    IntakeLabelsConfig,
    IntakeNeedsInfoConfig,
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
    assert external.comments.enabled is False  # comment ingestion is opt-in (bh-o9rep)
    assert external.needs_info == IntakeNeedsInfoConfig()  # waiting for the reporter (bh-61xkc)
    assert (external.needs_info.enabled, external.needs_info.sweep) == (False, False)
    assert external.milestones.catch_up_on_link is False


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
        {"external": {"milestones": {"draft_on_progress": True}}},  # draft_on_start is bh-dev42
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
        "comments",
        "deletion_confirmations",
        "purge_deleted_bodies",
        "max_body_chars",
        "catch_up_on_link",
        "needs_info",
        "sweep",
        "draft_on_start",
        "draft_on_blocked",
        "draft_on_deprioritised",
        "labels",  # the label projection section (bh-la587)
        "prefix",
        "project",
        "aliases",
        "colors",
        "overrides",
        "name",
        "description",
        "color",
        "on_transition",
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


# -- intake.external.comments (bh-o9rep): comment ingestion, off by default --


def test_comments_are_off_by_default_with_the_adr_defaults() -> None:
    comments = IntakeConfig().external.comments

    assert comments == IntakeCommentsConfig()
    assert (comments.enabled, comments.deletion_confirmations) == (False, 2)
    assert (comments.purge_deleted_bodies, comments.max_body_chars) == (True, None)


def test_a_comments_section_validates() -> None:
    config = BeadhiveConfig.model_validate(
        {
            "intake": {
                "external": {
                    "comments": {
                        "enabled": True,
                        "deletion_confirmations": 3,
                        "purge_deleted_bodies": False,
                        "max_body_chars": 4000,
                    }
                }
            }
        }
    )

    assert config.intake is not None
    comments = config.intake.external.comments
    assert (comments.enabled, comments.deletion_confirmations) == (True, 3)
    assert (comments.purge_deleted_bodies, comments.max_body_chars) == (False, 4000)


@pytest.mark.parametrize(
    "comments",
    [
        {"enabled": "true"},
        {"deletion_confirmations": 0},
        {"max_body_chars": -1},
        {"purge_deleted_bodies": 1},
        {"reconcile_every": 3},  # no timer cadence ships with the verbs
        {"tracker": "github"},
    ],
)
def test_bad_comments_values_and_unknown_keys_fail_closed(comments) -> None:
    with pytest.raises(ValidationError):
        BeadhiveConfig.model_validate({"intake": {"external": {"comments": comments}}})


# -- intake.external.needs_info and milestones.catch_up_on_link (bh-61xkc), off by default --


def test_a_needs_info_section_and_the_catch_up_switch_validate_and_are_host_owned() -> None:
    config = BeadhiveConfig.model_validate(
        {
            "intake": {
                "external": {
                    "needs_info": {"enabled": True, "sweep": True},
                    "milestones": {"catch_up_on_link": True},
                }
            }
        }
    )

    assert config.intake is not None
    external = config.intake.external
    assert (external.needs_info.enabled, external.needs_info.sweep) == (True, True)
    assert external.milestones.catch_up_on_link is True
    for path in (
        "intake.external.needs_info.enabled",
        "intake.external.needs_info.sweep",
        "intake.external.milestones.catch_up_on_link",
    ):
        assert partition.partition_of(path) == partition.HOST


@pytest.mark.parametrize(
    "external",
    [
        {"needs_info": {"enabled": "true"}},
        {"needs_info": {"sweep": 1}},
        {"needs_info": {"interval_seconds": 60}},  # no timer ships with the sweep
        {"milestones": {"catch_up_on_link": "yes"}},
    ],
)
def test_bad_needs_info_values_and_unknown_keys_fail_closed(external) -> None:
    with pytest.raises(ValidationError):
        BeadhiveConfig.model_validate({"intake": {"external": external}})


# -- the progress milestones (bh-dev42): three more draft switches, off by default --


def test_the_progress_switches_are_off_validate_and_are_host_owned() -> None:
    assert not IntakeExternalConfig().milestones.draft_on_start
    config = BeadhiveConfig.model_validate(
        {
            "intake": {
                "external": {
                    "milestones": {
                        "draft_on_start": True,
                        "draft_on_blocked": True,
                        "draft_on_deprioritised": True,
                    }
                }
            }
        }
    )

    assert config.intake is not None
    milestones = config.intake.external.milestones
    assert milestones.draft_on_start and milestones.draft_on_blocked
    assert milestones.draft_on_deprioritised
    for key in ("draft_on_start", "draft_on_blocked", "draft_on_deprioritised"):
        assert partition.partition_of(f"intake.external.milestones.{key}") == partition.HOST


@pytest.mark.parametrize(
    "milestones",
    [
        {"draft_on_start": "true"},
        {"draft_on_blocked": 1},
        {"draft_on_deprioritized": True},  # the spelling is `deprioritised`, as the event
    ],
)
def test_bad_progress_values_and_unknown_keys_fail_closed(milestones) -> None:
    with pytest.raises(ValidationError):
        BeadhiveConfig.model_validate({"intake": {"external": {"milestones": milestones}}})


# -- intake.external.labels (bh-la587): the registry-derived label projection, off by default --

LABEL_DIMENSIONS = ["intake", "delivery", "progress", "type", "priority", "component"]


def _labels(section=None, github=None) -> dict:
    document: dict = {"intake": {"external": {"labels": section or {}}}}
    if github is not None:
        document["plugins"] = {"tracker-github": github}
    return document


def test_the_label_projection_is_off_by_default_with_the_documented_defaults() -> None:
    labels = IntakeConfig().external.labels

    assert labels == IntakeLabelsConfig()
    assert (labels.enabled, labels.prefix, labels.on_transition) == (False, "bh:", False)
    assert labels.project == LABEL_DIMENSIONS
    assert (labels.aliases, labels.colors, labels.overrides) == ({}, {}, {})


def test_a_full_label_projection_section_validates() -> None:
    config = BeadhiveConfig.model_validate(
        _labels(
            {
                "enabled": True,
                "prefix": "bh:",
                "project": ["intake", "delivery"],
                "aliases": {"intake": "triage"},
                "colors": {"intake": "0E8A16", "delivery": "5319e7"},
                "overrides": {
                    "intake/accepted": {"name": "bh:triage/ok", "color": "00ff00"},
                    "delivery/merged": {"description": "Merged to main"},
                },
                "on_transition": True,
            }
        )
    )

    assert config.intake is not None
    labels = config.intake.external.labels
    assert (labels.enabled, labels.on_transition, labels.project) == (
        True,
        True,
        ["intake", "delivery"],
    )
    assert labels.overrides["intake/accepted"].color == "00ff00"
    assert labels.overrides["delivery/merged"].name is None


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ({"colors": {"intake": "#0E8A16"}}, "intake.external.labels.colors"),
        ({"colors": {"intake": "0E8A1"}}, "intake.external.labels.colors"),
        ({"colors": {"intake": "gggggg"}}, "intake.external.labels.colors"),
        ({"colors": {"nonsense": "0E8A16"}}, "intake.external.labels.colors"),
        ({"project": ["intake", "nonsense"]}, "intake.external.labels.project"),
        ({"project": ["intake", "intake"]}, "intake.external.labels.project"),
        ({"aliases": {"nonsense": "x"}}, "intake.external.labels.aliases"),
        ({"aliases": {"intake": " "}}, "intake.external.labels.aliases"),
        ({"overrides": {"intake": {}}}, "intake.external.labels.overrides"),
        ({"overrides": {"nonsense/x": {}}}, "intake.external.labels.overrides"),
        ({"overrides": {"intake/x": {"color": "red"}}}, "color"),
        ({"overrides": {"intake/x": {"name": " "}}}, "name"),
        ({"prefix": ""}, "intake.external.labels.prefix"),
        ({"prefix": " bh:"}, "intake.external.labels.prefix"),
        ({"enabled": "true"}, "enabled"),
        ({"on_transition": 1}, "on_transition"),
        ({"token": "x"}, "token"),  # unknown keys fail closed
    ],
)
def test_a_bad_label_projection_is_refused_naming_the_key(section, key) -> None:
    with pytest.raises(ValidationError) as error:
        BeadhiveConfig.model_validate(_labels(section))

    assert key in str(error.value)


def test_a_label_projection_error_never_repeats_the_input() -> None:
    planted = "ghp_" + "a" * 36
    with pytest.raises(ValidationError) as error:
        BeadhiveConfig.model_validate(_labels({"colors": {"intake": planted}}))

    assert planted not in str(error.value)


@pytest.mark.parametrize(
    "write",
    [
        {"labels": {"enabled": True, "owned_prefixes": ["other:"]}},
        {"label_definitions": {"enabled": True}},
    ],
)
def test_the_projection_prefix_must_be_owned_when_labels_may_be_written(write) -> None:
    document = _labels({"enabled": True}, {"enabled": True, "write": write})

    with pytest.raises(ValidationError) as error:
        BeadhiveConfig.model_validate(document)

    text = str(error.value)
    assert "intake.external.labels.prefix" in text
    assert "plugins.tracker-github.write.labels.owned_prefixes" in text


@pytest.mark.parametrize(
    ("section", "github"),
    [
        # owned
        (
            {"enabled": True},
            {
                "write": {
                    "label_definitions": {"enabled": True},
                    "labels": {"owned_prefixes": ["bh:"]},
                }
            },
        ),
        (
            {"enabled": True, "prefix": "bh:x/"},
            {"write": {"labels": {"enabled": True, "owned_prefixes": ["bh:"]}}},
        ),
        # nothing may write labels: ownership is not consulted
        ({"enabled": True}, {"write": {"labels": {"owned_prefixes": ["other:"]}}}),
        ({"enabled": True}, None),
        # the projection is off
        ({}, {"write": {"label_definitions": {"enabled": True}}}),
    ],
)
def test_the_ownership_check_only_applies_when_both_sides_are_on(section, github) -> None:
    BeadhiveConfig.model_validate(_labels(section, github and {"enabled": True, **github}))


def test_the_tracker_github_label_flags_are_strict_and_off_by_default() -> None:
    from beadhive.modules.config.contracts import TrackerGithubConfig

    config = TrackerGithubConfig()
    assert (config.read.labels.enabled, config.write.label_definitions.enabled) == (False, False)
    for fragment in (
        {"read": {"labels": {"enabled": "true"}}},
        {"write": {"label_definitions": {"enabled": 1}}},
    ):
        with pytest.raises(ValidationError):
            TrackerGithubConfig.model_validate(fragment)
    ok = TrackerGithubConfig.model_validate(
        {"read": {"labels": {"enabled": True}}, "write": {"label_definitions": {"enabled": True}}}
    )
    assert (ok.read.labels.enabled, ok.write.label_definitions.enabled) == (True, True)


def test_the_label_keys_are_host_owned_so_a_fleet_push_cannot_set_them() -> None:
    paths = [
        f.path
        for f in iter_schema_fields()
        if f.path.startswith(
            (
                "intake.external.labels",
                "plugins.tracker-github.read.labels",
                "plugins.tracker-github.write.label_definitions",
            )
        )
    ]

    assert "intake.external.labels.prefix" in paths
    assert "plugins.tracker-github.read.labels.enabled" in paths
    assert "plugins.tracker-github.write.label_definitions.enabled" in paths
    assert {partition.partition_of(path) for path in paths} == {partition.HOST}
