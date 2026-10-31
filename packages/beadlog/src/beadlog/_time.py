"""Canonical BeadLog timestamps (format ADR section 3) and their instant ordering."""

from __future__ import annotations

import re
from datetime import UTC, datetime

_TIMESTAMP = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"
    r"T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]{0,8}[1-9])?Z$"
)

# One instant: (0, "YYYY-MM-DDTHH:MM:SS", nanoseconds), or (1, "", 0) for "unknown" (+infinity).
Instant = tuple[int, str, int]
INFINITY: Instant = (1, "", 0)


class TimestampError(ValueError):
    """A timestamp not in the one canonical RFC 3339 UTC spelling the format allows."""


def check_timestamp(value: object) -> str:
    """Return ``value`` when it is a canonical timestamp; raise :class:`TimestampError` if not.

    Canonical means UTC with a ``Z`` suffix, seconds always present, and a fraction only when
    non-zero, without trailing zeros, at most nine digits (``2026-08-08T14:30:00.25Z``).
    """
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise TimestampError(f"not a canonical BeadLog timestamp: {value!r}")
    return value


def instant(value: str | None) -> Instant:
    """Ordering key of a timestamp as an instant; ``None`` sorts after every instant.

    Text order is wrong for fractions (``...:00.25Z`` sorts before ``...:00Z``), so the
    fraction is compared as an integer number of nanoseconds.
    """
    if value is None:
        return INFINITY
    check_timestamp(value)
    fraction = value[20:-1] if value[19] == "." else ""
    return (0, value[:19], int(fraction.ljust(9, "0")) if fraction else 0)


def format_timestamp(moment: datetime) -> str:
    """Spell an aware ``datetime`` canonically in UTC (microsecond precision)."""
    if moment.tzinfo is None:
        raise TimestampError("a naive datetime has no instant; attach a timezone")
    utc = moment.astimezone(UTC)
    text = utc.strftime("%Y-%m-%dT%H:%M:%S")
    if utc.microsecond:
        text += "." + f"{utc.microsecond:06d}".rstrip("0")
    return text + "Z"
