"""UNSUPPORTED, dev-only per-host switch for HQ runtime-authority enforcement (bh-6pqul).

``BH_HQ_AUTHORITY_ENFORCE=false`` makes this process skip HQ runtime-authority enforcement:
authority expiry, the config-revision binding, and the authority-derived frame eligibility
predicates (admission state, cordon, emergency review, desired release/caps/profile). Identity
checks, the heartbeat (which follows ``BH_FRAME_HEARTBEAT``), hive-lease ownership and
validation are unchanged. The frame becomes self-asserted — see docs/HQ.md for the trust delta.

The setting is an environment variable on purpose, following ``BH_FRAME_HEARTBEAT`` and
``BH_HQ_SQL_LIVENESS``: the strict host schema rejects unknown keys, and pre-0.22.x readers
(the 0.21.3 heartbeat sender) share the HOST file, so a ``host.yaml`` key would break them. A
fleet key would itself need an authority-bound publish. Unset, empty or ``true`` keeps
enforcement on; any other value is an error, never a fallback.
"""

from __future__ import annotations

import os
import sys

ENFORCE_ENV = "BH_HQ_AUTHORITY_ENFORCE"
ENFORCE_VALUES = ("true", "false")
DOCS = "docs/HQ.md#unsupported-disabling-hq-authority-enforcement"
BANNER = (
    "⚠ HQ authority enforcement DISABLED on this host — unsupported, dev/prototype only "
    f"({ENFORCE_ENV}=false; re-enable before admitting new executors, see {DOCS})"
)

_banner_emitted = False


class AuthorityEnforcementError(ValueError):
    """``BH_HQ_AUTHORITY_ENFORCE`` holds a value other than ``true`` / ``false``."""


def enforced() -> bool:
    """True unless this process explicitly runs with ``BH_HQ_AUTHORITY_ENFORCE=false``."""
    raw = os.environ.get(ENFORCE_ENV, "").strip() or "true"
    if raw not in ENFORCE_VALUES:
        raise AuthorityEnforcementError(
            f"{ENFORCE_ENV} must be 'true' or 'false' (got {raw[:32]!r})"
        )
    return raw == "true"


def status() -> str:
    """``"enabled"`` or ``"disabled"`` — the value surfaced by status/doctor JSON."""
    return "enabled" if enforced() else "disabled"


def emit_banner(stream=None) -> bool:
    """Print the disabled banner once per process; return whether enforcement is disabled."""
    global _banner_emitted
    if enforced():
        return False
    if not _banner_emitted:
        _banner_emitted = True
        print(BANNER, file=stream if stream is not None else sys.stderr)
    return True


def doctor_warnings() -> list[str]:
    """Doctor WARN lines for this process's enforcement setting (never raises)."""
    try:
        if enforced():
            return []
    except AuthorityEnforcementError as exc:
        return [f"hq: {exc} — HQ authority enforcement setting is invalid"]
    return [
        f"hq: HQ runtime-authority enforcement is DISABLED on this host ({ENFORCE_ENV}=false) — "
        "UNSUPPORTED, dev/prototype only; frames here are self-asserted (expiry, config "
        "binding, cordon, release pins and caps are not enforced). Unset it before admitting "
        f"new executors ({DOCS})"
    ]
