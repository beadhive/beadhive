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

import importlib
import os
import sys
from typing import NamedTuple

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


MODE_ENV = "BH_HQ_AUTHORITY_MODE"
MODES = ("signed", "trusted", "inherit")
DEFAULT_MODE = "inherit"
MODE_DOCS = "docs/CONFIGURATION.md"
TRUSTED_BANNER = (
    "⚠ HQ authority mode is TRUSTED on this frame — authority, heartbeat and config are not "
    f"signature-verified ({MODE_ENV} / hq.authority_mode; see {MODE_DOCS})"
)
DEPRECATION = (
    f"{ENFORCE_ENV} is deprecated; use {MODE_ENV}=trusted or hq.authority_mode: trusted "
    f"({MODE_DOCS})"
)

_deprecation_emitted = False
_invalid_emitted: set[str] = set()


class Resolution(NamedTuple):
    """The resolved authority mode and where it came from."""

    mode: str  # "signed" | "trusted" (never "inherit")
    source: str  # "env" | "enforce-env" | "host" | "default" | "invalid"


def _warn(message: str) -> None:
    print(message, file=sys.stderr)


def resolve(configured: str | None = None) -> Resolution:
    """The ONE resolver: ``$BH_HQ_AUTHORITY_MODE`` > deprecated ``BH_HQ_AUTHORITY_ENFORCE`` >
    *configured* (the frame-local ``hq.authority_mode`` from host config, or the operator
    settings file) > ``inherit``.

    ``inherit`` resolves to ``signed`` here; the fleet-default lookup (bh-taa04.3) extends
    this function. An invalid mode value fails closed to ``signed`` with one error line.
    """
    global _deprecation_emitted
    raw = os.environ.get(MODE_ENV, "").strip()
    source = "env"
    if not raw:
        legacy = os.environ.get(ENFORCE_ENV, "").strip() or "true"
        if legacy not in ENFORCE_VALUES:
            raise AuthorityEnforcementError(
                f"{ENFORCE_ENV} must be 'true' or 'false' (got {legacy[:32]!r})"
            )
        if legacy == "false":
            if not _deprecation_emitted:
                _deprecation_emitted = True
                _warn(f"warning: {DEPRECATION}")
            return Resolution("trusted", "enforce-env")
        raw = (configured or "").strip()
        source = "host"
    if not raw:
        return Resolution("signed", "default")
    if raw not in MODES:
        if raw not in _invalid_emitted:
            _invalid_emitted.add(raw)
            where = MODE_ENV if source == "env" else "hq.authority_mode"
            _warn(
                f"error: {where} must be one of {'|'.join(MODES)} (got {raw[:32]!r}); "
                "failing closed to signed"
            )
        return Resolution("signed", "invalid")
    return Resolution("signed" if raw == "inherit" else raw, source)


def _configured_mode() -> str | None:
    """Frame-local ``hq.authority_mode``: the operator settings file when one is named, else
    this host's config. Unreadable config never changes the default."""
    try:
        if os.environ.get("BH_HQ_OPERATOR_SETTINGS"):
            from pathlib import Path

            from ruamel.yaml import YAML

            raw = YAML(typ="safe").load(Path(os.environ["BH_HQ_OPERATOR_SETTINGS"]).read_text())
            hq = raw.get("hq", raw) if isinstance(raw, dict) else None
            value = hq.get("authority_mode") if isinstance(hq, dict) else None
            return None if value is None else str(value)
        # Resolved late on purpose: config's store already reaches this module, so a static
        # import would close an import cycle.
        config = importlib.import_module("beadhive.config")
        return (config.load_host().get("hq") or {}).get("authority_mode")
    except Exception:  # noqa: BLE001 - selection must stay fail-closed to signed
        return None


def mode() -> str:
    """``"signed"`` or ``"trusted"`` for this process."""
    return resolve(_configured_mode()).mode


def enforced() -> bool:
    """Shim for the pre-modes callers: True unless this process resolves to ``trusted``."""
    return mode() == "signed"


def status() -> str:
    """``"enabled"`` or ``"disabled"`` — the value surfaced by status/doctor JSON."""
    return "enabled" if enforced() else "disabled"


def emit_banner(stream=None) -> bool:
    """Print the trusted banner once per process; return whether enforcement is disabled."""
    global _banner_emitted
    resolved = resolve(_configured_mode())
    if resolved.mode == "signed":
        return False
    if not _banner_emitted:
        _banner_emitted = True
        text = BANNER if resolved.source == "enforce-env" else TRUSTED_BANNER
        print(text, file=stream if stream is not None else sys.stderr)
    return True


def doctor_warnings() -> list[str]:
    """Doctor WARN lines for this process's authority mode (never raises)."""
    try:
        resolved = resolve(_configured_mode())
    except AuthorityEnforcementError as exc:
        return [f"hq: {exc} — HQ authority enforcement setting is invalid"]
    if resolved.mode == "signed":
        return []
    if resolved.source == "enforce-env":
        return [
            f"hq: HQ runtime-authority enforcement is DISABLED on this host ({ENFORCE_ENV}=false)"
            " — UNSUPPORTED, dev/prototype only; frames here are self-asserted (expiry, config "
            "binding, cordon, release pins and caps are not enforced). Unset it before admitting "
            f"new executors ({DOCS})"
        ]
    return [
        f"hq: authority mode is TRUSTED ({resolved.source}) — frames here are self-asserted "
        f"(expiry, config binding, cordon, release pins and caps are not enforced) ({MODE_DOCS})"
    ]
