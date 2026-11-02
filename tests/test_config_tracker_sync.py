"""``tracker_sync.commit_after_pull`` - the opt-in commit of a tracker pull (bh-cm4er)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from beadhive.config_partition import HOST, partition_of
from beadhive.config_validate import validate_config
from beadhive.github_projection import commit_after_pull_enabled
from beadhive.modules.config.contracts import BeadhiveConfig


def test_the_key_is_off_when_absent_and_by_default():
    assert BeadhiveConfig.model_validate({}).tracker_sync is None
    assert (
        BeadhiveConfig.model_validate({"tracker_sync": {}}).tracker_sync.commit_after_pull is False
    )
    assert commit_after_pull_enabled({}) is False


def test_the_key_turns_on_only_with_a_real_boolean():
    cfg = BeadhiveConfig.model_validate({"tracker_sync": {"commit_after_pull": True}})
    assert cfg.tracker_sync.commit_after_pull is True
    assert commit_after_pull_enabled(cfg.model_dump()) is True
    assert validate_config(cfg.model_dump()) == []
    for bad in ("true", "false", 1):
        with pytest.raises(ValidationError):
            BeadhiveConfig.model_validate({"tracker_sync": {"commit_after_pull": bad}})


def test_an_unknown_tracker_sync_key_is_refused():
    with pytest.raises(ValidationError):
        BeadhiveConfig.model_validate({"tracker_sync": {"commit": True}})


def test_the_key_is_host_owned_so_a_fleet_push_cannot_enable_it():
    assert partition_of("tracker_sync.commit_after_pull") == HOST
