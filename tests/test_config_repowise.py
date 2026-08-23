"""config.repowise_* accessors resolve the optional integration flag."""

from beadhive import config


def test_enabled_false_by_default():
    assert config.repowise_enabled({}) is False


def test_hive_flag_overrides_global_flag():
    assert config.repowise_enabled(
        {"repowise": {"enabled": False}}, {"repowise": {"enabled": True}}
    )
