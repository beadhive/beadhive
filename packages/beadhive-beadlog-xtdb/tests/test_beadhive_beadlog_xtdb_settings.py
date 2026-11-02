"""The DSN endpoint parser and the loopback rule (doctor's ``bind`` check and the store guard)."""

from __future__ import annotations

import pytest

from beadhive_beadlog_xtdb.settings import (
    DEFAULT_DSN,
    VALIDATED_IMAGE,
    Endpoint,
    XtdbSettings,
    is_loopback,
    parse_dsn,
)


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        (DEFAULT_DSN, Endpoint("127.0.0.1", 5432, "xtdb", "xtdb")),
        ("host=localhost port=15470 dbname=bdlg_1", Endpoint("localhost", 15470, "bdlg_1", "xtdb")),
        ("host='127.0.0.1' user='xtdb'", Endpoint("127.0.0.1", 5432, "xtdb", "xtdb")),
        ("", Endpoint("127.0.0.1", 5432, "xtdb", "xtdb")),
        ("postgresql://xtdb@127.0.0.1:15470/db1", Endpoint("127.0.0.1", 15470, "db1", "xtdb")),
        ("postgres://[::1]:5433/x", Endpoint("::1", 5433, "x", "xtdb")),
        ("postgresql:///xtdb?host=10.0.0.5&port=6000", Endpoint("10.0.0.5", 6000, "xtdb", "xtdb")),
    ],
)
def test_parse_dsn_finds_the_endpoint(dsn, expected) -> None:
    assert parse_dsn(dsn) == expected


@pytest.mark.parametrize("dsn", ["host=127.0.0.1 port=abc", "justaword", "host='unterminated"])
def test_parse_dsn_refuses_without_repeating_the_input(dsn) -> None:
    with pytest.raises(ValueError) as caught:
        parse_dsn(dsn)
    assert dsn not in str(caught.value)


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("127.8.9.10", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("/var/run/postgresql", True),
        ("0.0.0.0", False),
        ("10.0.0.5", False),
        ("xtdb.example.org", False),
    ],
)
def test_is_loopback(host, loopback) -> None:
    assert is_loopback(host) is loopback


def test_settings_default_to_the_validated_loopback_node() -> None:
    settings = XtdbSettings.from_mapping({})
    assert settings == XtdbSettings()
    assert settings.enabled is False
    assert settings.image == VALIDATED_IMAGE
    assert settings.endpoint().label == "xtdb://xtdb@127.0.0.1:5432/xtdb"
    assert XtdbSettings.from_mapping({"enabled": "true"}).enabled is False  # exact True only
