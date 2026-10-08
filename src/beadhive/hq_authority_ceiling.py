"""Authority expiry policy: the no-expiry sentinel and the opt-in duration ceiling.

Signing-side only: frame verifiers enforce each signed ``expires_at`` and have no maximum.

Since 0.24.0 (bh-y929l) signed authority is standing policy: an operator mutation without an
explicit ``--duration`` signs :data:`AUTHORITY_NO_EXPIRY`, a *finite* far-future sentinel, so
0.23.x verifiers (which need a finite ``expires_at > issued_at`` and canonical JSON, which
forbids ``Infinity``) accept the record unchanged. An explicit ``--duration`` still signs an
expiring authority, capped by the ceiling.

Ceiling resolution order (bh-od8ve): ``--max-duration`` > ``hq.sql.authority_max_duration_s``
(operator settings file) > ``$BH_HQ_AUTHORITY_MAX_DURATION`` > unlimited. The ceiling only
bounds an explicit duration; there is deliberately NO hard upper bound.
"""

from __future__ import annotations

import math
import os
import re
from typing import NamedTuple

#: The no-expiry sentinel: 2100-01-01T00:00:00Z as integer epoch seconds. Finite (canonical JSON
#: and 0.23.x ``validate_state``), exact in a float64/DOUBLE and in BIGINT, representable as a
#: Python ``datetime`` and a SQL ``DATETIME``, and far enough out that ``sentinel - now`` still
#: fits a 32-bit unsigned seconds count. It never lands in a ``TIMESTAMP`` column (2038 cap):
#: authority expiry travels inside the signed ``state_json``/``hive_policies_json`` blobs and
#: Git ``authority.json``/``config.json`` only.
AUTHORITY_NO_EXPIRY = 4102444800

#: Historical 0.23.x default ceiling (7 days). No longer the default -- the default ceiling is
#: unlimited -- but kept as the suggested duration for an operator who opts into expiry.
AUTHORITY_MAX_DURATION_DEFAULT_S = 604800  # 7 days
ENV_VAR = "BH_HQ_AUTHORITY_MAX_DURATION"
SETTINGS_KEY = "hq.sql.authority_max_duration_s"

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
UNIT_PATTERN = r"(\d+(?:\.\d+)?)\s*([smhdw]?)"
_HUMAN = re.compile(rf"^\s*{UNIT_PATTERN}\s*$", re.IGNORECASE)


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
        seconds = float(match.group(1)) * UNITS[(match.group(2) or "s").lower()]
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
    return Ceiling(math.inf, "built-in default (unlimited)")


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


def non_expiring(expires_at) -> bool:
    """Whether a signed ``expires_at`` is the no-expiry sentinel (or later)."""
    return type(expires_at) in (int, float) and expires_at >= AUTHORITY_NO_EXPIRY


def signed_expiry(issued_at, duration=None, ceiling: Ceiling | None = None):
    """``expires_at`` for a fresh operator signature: the sentinel unless ``duration`` is given.

    An explicit duration is checked against the resolved ceiling and is never stretched past
    the sentinel.
    """
    if duration is None:
        return AUTHORITY_NO_EXPIRY
    check_duration(duration, ceiling)
    return min(issued_at + duration, AUTHORITY_NO_EXPIRY)
