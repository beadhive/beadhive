"""The pure revision rule of import: explicit tokens, no clock."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from beadhive.modules.intake.application import revision_token
from beadhive.modules.intake.domain import revision_is_newer
from beadhive.modules.tracker.contracts import Revision

JAN_1 = "2026-01-01T00:00:00+00:00"
JAN_2 = "2026-01-02T00:00:00+00:00"


@pytest.mark.parametrize(
    ("stored", "observed", "newer"),
    [
        (JAN_1, JAN_1, False),  # the inclusive boundary repeats: a no-op
        (JAN_2, JAN_1, False),  # a stale read never rolls the snapshot back
        (JAN_1, JAN_2, True),
        (JAN_1, f"{JAN_1}#v2", True),  # same instant, a different version
        (f"{JAN_1}#v2", JAN_1, True),
        (f"{JAN_1}#v2", f"{JAN_1}#v2", False),
        ("garbled", JAN_1, True),  # an unreadable stored token is refreshed, never frozen
        (JAN_1, "garbled", True),
        (JAN_1, "2026-01-02T00:00:00", True),  # aware against naive: not comparable, so refresh
    ],
)
def test_revision_is_newer(stored: str, observed: str, newer: bool) -> None:
    assert revision_is_newer(stored, observed) is newer


def test_the_revision_token_is_the_trackers_timestamp_and_version() -> None:
    moment = datetime(2026, 1, 1, tzinfo=UTC)

    assert revision_token(Revision(moment)) == JAN_1
    assert revision_token(Revision(moment, "v2")) == f"{JAN_1}#v2"
