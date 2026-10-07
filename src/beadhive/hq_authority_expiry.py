"""Authority expiry visibility: status fields, pre-lapse warnings and the preflight check.

The authority carrier silently fences the whole fleet when ``expires_at`` passes. This module
makes the remaining time visible without any new fleet or host config key (either would move the
HQ head or break older readers of a shared HOST file): the lead time is the
``BH_HQ_AUTHORITY_WARN_WITHIN`` environment variable (duration, default 24h). Everything here
is read-only.
"""

from __future__ import annotations

import importlib
import os
import re
import sys

from .hq_authority_ceiling import (
    AUTHORITY_MAX_DURATION_DEFAULT_S,
    UNIT_PATTERN,
    UNITS,
    non_expiring,
    resolve_ceiling,
)

WARN_ENV = "BH_HQ_AUTHORITY_WARN_WITHIN"
MIN_REMAINING_ENV = "BH_HQ_AUTHORITY_MIN_REMAINING"
DEFAULT_WARN_WITHIN = 24 * 3600.0
_WARNED: set[str] = set()


def parse_duration(value, *, name="duration") -> float:
    """``90``, ``90s``, ``30m``, ``6h``, ``2d`` or ``1w`` to seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
    else:
        match = re.fullmatch(rf"\s*{UNIT_PATTERN}\s*", str(value), re.IGNORECASE)
        if match is None:
            raise ValueError(f"{name} must look like 90s, 30m, 6h, 2d or 1w")
        seconds = float(match.group(1)) * UNITS[(match.group(2) or "s").lower()]
    if seconds < 0:
        raise ValueError(f"{name} must not be negative")
    return seconds


def warn_within(env=None) -> float:
    raw = (os.environ if env is None else env).get(WARN_ENV, "")
    if not raw:
        return DEFAULT_WARN_WITHIN
    try:
        return parse_duration(raw, name=WARN_ENV)
    except ValueError:
        return DEFAULT_WARN_WITHIN


def humanize(seconds: float) -> str:
    total = int(abs(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = [f"{n}{u}" for n, u in ((days, "d"), (hours, "h"), (minutes, "m")) if n]
    text = " ".join(parts[:2]) or f"{total}s"
    return f"expired {text} ago" if seconds < 0 else text


def min_remaining(env=None) -> float:
    """Floor from BH_HQ_AUTHORITY_MIN_REMAINING (default 0: only expiry or unbound fail)."""
    raw = (os.environ if env is None else env).get(MIN_REMAINING_ENV, "")
    return parse_duration(raw, name=MIN_REMAINING_ENV) if raw else 0.0


def _renew_ceiling_s(plane=None) -> int:
    """Whole seconds of the resolved authority ceiling; the shipped default if unresolvable."""
    try:
        ceiling = resolve_ceiling(settings=getattr(plane, "authority_max_duration_s", None))
        return max(int(ceiling.seconds), 1)
    except Exception:  # noqa: BLE001 - a hint must never fail its caller
        return AUTHORITY_MAX_DURATION_DEFAULT_S


def _mode() -> str:
    """This process's authority mode; ``signed`` if it cannot be resolved (never raises)."""
    try:
        from . import hq_authority_enforce

        return hq_authority_enforce.mode()
    except Exception:  # noqa: BLE001 - reporting must never fail its caller
        return "signed"


NOT_ENFORCED = "not enforced (trusted)"


def renew_command(revision="<revision>", settings_file=None, plane=None) -> str:
    """The renew command for the resolved ceiling (``--max-duration`` only above the default)."""
    path = settings_file or "<file>"
    seconds = _renew_ceiling_s(plane)
    raise_ceiling = (
        f" --max-duration {seconds}" if seconds > AUTHORITY_MAX_DURATION_DEFAULT_S else ""
    )
    return (
        f"BH_HQ_OPERATOR_SETTINGS={path} bh hq authority rebind --expected-revision {revision} "
        f"--operator-key <key> --duration {seconds}{raise_ceiling} --confirm"
    )


def _config_bound(plane, crossref, policies, state) -> bool:
    """Whether frames would accept this authority at the latest committed config head.

    Exact cross-reference or a tolerated descendant head (bh-u67ve, bh-3h6al): the same
    shared check the frame verifier runs
    (:meth:`beadhive.hq_sql_config.SqlFleetConfigRevisionStore.authority_binding`), so
    status, check and doctor agree with the frames.
    """
    try:
        bound, _head, _snapshot = plane.config_store().authority_binding(
            crossref, policies, expires_at=state["expires_at"]
        )
        return bool(bound)
    except Exception:  # noqa: BLE001 - an unreadable config head means "not provably bound"
        return False


def read_state(plane):
    """(revision, state, config_bound) for any backend, reading even an expired authority."""
    if hasattr(plane, "_read") and not hasattr(plane, "_runtime_authority"):
        sha, state, _policy = plane._read(allow_expired=True)
        return sha, state, True
    settings = plane.settings
    if settings.get("runtime") is not None:
        head, state, crossref, policies = plane._runtime_authority().load_state(allow_expired=True)
    elif settings.get("authority_writer") is not None:
        head, state, crossref, policies = plane._operator().load()
    else:
        return "", None, False
    return head, state, _config_bound(plane, crossref, policies, state)


def authority_status(plane, *, now=None, lead=None) -> dict:
    """Top-level expiry fields; still reports when already expired or unbound."""
    now = plane.clock() if now is None else now
    lead = warn_within() if lead is None else lead
    revision, state, bound = read_state(plane)
    if state is None:
        return {
            "revision": "",
            "state": "AUTHORITY_NOT_READY",
            "authority_ready": False,
            "expires_at": None,
            "expires_in_s": None,
            "expires_in": "unknown",
            "config_bound": False,
            "expiring_soon": False,
            "warn_within_s": lead,
            "expires_never": False,
            "mode": _mode(),
        }
    remaining = state["expires_at"] - now
    never = non_expiring(state["expires_at"])
    return {
        "revision": revision,
        "authority_revision": state.get("revision"),
        "state": state,
        "authority_ready": remaining > 0,
        "expires_at": state["expires_at"],
        "expires_in_s": int(remaining),
        "expires_in": "never" if never else humanize(remaining),
        "config_bound": bool(bound),
        "expiring_soon": False if never else remaining <= lead,
        "warn_within_s": lead,
        "expires_never": never,
        "mode": _mode(),
    }


def warning_line(status, settings_file=None, plane=None) -> str:
    if status["expires_in_s"] <= 0:
        lead = f"HQ authority {status['expires_in']}; the fleet is fenced"
    else:
        lead = f"HQ authority expires in {status['expires_in']}"
    return f"WARN: {lead}. Renew: {renew_command(status['revision'], settings_file, plane)}"


def check(plane, *, min_remaining=0.0, now=None) -> tuple[bool, dict, str]:
    """Healthy means bound to the latest config head with at least ``min_remaining`` left."""
    status = authority_status(plane, now=now)
    if status["expires_at"] is None:
        return False, status, "FAIL: no authority binding available to read expiry"
    if status.get("mode") == "trusted":
        # A trusted frame ignores authority expiry and the config-head binding (bh-mk97e), so
        # the check agrees with it instead of failing an expired opt-in authority (bh-taa04.3).
        return True, status, f"OK: HQ authority expiry and config binding {NOT_ENFORCED}"
    if not status["config_bound"]:
        return False, status, "FAIL: authority is not bound to the latest config head"
    if status.get("expires_never"):
        return True, status, "OK: HQ authority does not expire"
    if status["expires_in_s"] <= 0:
        return False, status, warning_line(status, plane=plane).replace("WARN", "FAIL", 1)
    if status["expires_in_s"] < min_remaining:
        return False, status, warning_line(status, plane=plane).replace("WARN", "FAIL", 1)
    return True, status, f"OK: HQ authority expires in {status['expires_in']}"


def _frame_plane():
    """The running host's plane when it is a protected-authority reader, else None."""
    # Resolved by name, not imported: the control plane reaches the lifecycle callers of this
    # advisory module, and a static edge would add an import cycle to the reviewed snapshot.
    hq_control_plane = importlib.import_module(f"{__package__}.hq_control_plane")
    SqlControlPlane, control_plane = (
        hq_control_plane.SqlControlPlane,
        hq_control_plane.control_plane,
    )

    hq = hq_control_plane.config.load_host().get("hq", {})
    if not (hq.get("authority_anchor") or (hq.get("sql") or {}).get("runtime")):
        return None
    plane = control_plane()
    if isinstance(plane, SqlControlPlane) and plane.settings.get("runtime") is None:
        return None
    return plane


def warn_if_expiring(plane=None, *, now=None, stream=None, settings_file=None) -> str | None:
    """Print at most one WARN line per process when expiry is inside the lead time.

    Never raises and never fails the caller: an unreadable authority is simply not warned about
    here (eligibility checks and `bh doctor` already fail closed on that).
    """
    try:
        if plane is None:
            plane = _frame_plane()
            if plane is None:
                return None
        status = authority_status(plane, now=now)
        if status["expires_in_s"] is None or not status["expiring_soon"]:
            return None
        if status.get("expires_never") or status.get("mode") == "trusted":
            return None
        line = warning_line(status, settings_file, plane)
        key = f"{status['revision']}"
        if key in _WARNED:
            return None
        _WARNED.add(key)
        print(line, file=stream or sys.stderr)
        return line
    except Exception:  # noqa: BLE001 - advisory only
        return None


def _reset_for_tests() -> None:
    _WARNED.clear()


__all__ = [
    "authority_status",
    "check",
    "parse_duration",
    "renew_command",
    "warn_if_expiring",
    "warn_within",
]


def doctor_data(*, now=None) -> dict:
    """Doctor section: level is ok | warn | fail | skip (not a protected-authority host)."""
    try:
        plane = _frame_plane()
    except Exception:  # noqa: BLE001
        return {"level": "skip", "detail": "no HQ authority binding on this host"}
    if plane is None:
        return {"level": "skip", "detail": "no HQ authority binding on this host"}
    try:
        status = authority_status(plane, now=now)
    except Exception as exc:  # noqa: BLE001
        return {"level": "fail", "detail": f"authority unreadable: {exc}"}
    if not status["config_bound"]:
        return {"level": "fail", "detail": "authority is not bound to the latest config head"}
    mode = status.get("mode", "signed")
    if status.get("expires_never"):
        return {"level": "ok", "detail": "HQ authority expires: never", "mode": mode}
    if mode == "trusted":
        return {"level": "ok", "detail": f"HQ authority expiry: {NOT_ENFORCED}", "mode": mode}
    if status["expires_in_s"] <= 0:
        return {"level": "fail", "detail": f"HQ authority {status['expires_in']}; fleet is fenced"}
    if status["expiring_soon"]:
        return {
            "level": "warn",
            "detail": warning_line(status, plane=plane).removeprefix("WARN: "),
            "mode": mode,
        }
    return {
        "level": "ok",
        "detail": f"HQ authority expires in {status['expires_in']}",
        "mode": mode,
    }
