"""Configurable authority-duration ceiling (bh-od8ve).

Signing-side only: frame verifiers enforce each signed ``expires_at`` and have no maximum.
There is deliberately NO hard upper bound -- the operator owns the ceiling.

Resolution order: ``--max-duration`` > ``hq.sql.authority_max_duration_s`` (operator
settings file) > ``$BH_HQ_AUTHORITY_MAX_DURATION`` > ``AUTHORITY_MAX_DURATION_DEFAULT_S``.
"""

from __future__ import annotations

import math
import os
import re
from typing import NamedTuple

AUTHORITY_MAX_DURATION_DEFAULT_S = 604800  # 7 days
ENV_VAR = "BH_HQ_AUTHORITY_MAX_DURATION"
SETTINGS_KEY = "hq.sql.authority_max_duration_s"

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_HUMAN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$", re.IGNORECASE)


class Ceiling(NamedTuple):
    seconds: float
    source: str


def parse_duration(value) -> float:
    """Parse seconds or a human value (``7d``, ``36h``, ``90m``); refuse non-positive/non-finite."""
    if isinstance(value, bool):
        raise ValueError("duration must be a positive number of seconds or e.g. 7d / 36h")
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        match = _HUMAN.match(str(value))
        if not match:
            raise ValueError(f"invalid duration {value!r}: use seconds or e.g. 7d / 36h")
        seconds = float(match.group(1)) * _UNITS[(match.group(2) or "s").lower()]
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"invalid duration {value!r}: must be positive and finite")
    return int(seconds) if seconds == int(seconds) else seconds


def resolve_ceiling(cli=None, settings=None, env=None) -> Ceiling:
    """Resolve the ceiling and name its source. ``settings`` is the operator-settings value."""
    environ = os.environ if env is None else env
    for raw, source in (
        (cli, "--max-duration"),
        (settings, f"operator settings {SETTINGS_KEY}"),
        (environ.get(ENV_VAR), f"${ENV_VAR}"),
    ):
        if raw is None or raw == "":
            continue
        try:
            return Ceiling(parse_duration(raw), source)
        except ValueError as exc:
            raise ValueError(f"authority ceiling from {source}: {exc}") from None
    return Ceiling(AUTHORITY_MAX_DURATION_DEFAULT_S, "built-in default (7d)")


def check_duration(duration, ceiling: Ceiling | None = None) -> None:
    """Refuse a non-positive/non-finite duration or one above the resolved ceiling."""
    ceiling = ceiling or resolve_ceiling()
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("bounded authority renewal duration required")
    if duration > ceiling.seconds:
        raise ValueError(
            f"authority duration {duration:g}s exceeds ceiling {ceiling.seconds:g}s "
            f"(from {ceiling.source}); raise it with --max-duration, {SETTINGS_KEY} "
            f"or ${ENV_VAR}"
        )
