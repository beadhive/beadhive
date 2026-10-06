"""Advisory notice when a fleet-config publication unbinds SQL HQ authority.

Operator-signed runtime authority is cross-bound to the exact fleet-config head.  Any
publication moves that head, after which every frame fails eligibility with "HQ config
and authority publications are not bound" until the operator renews.  The publisher
holds no key and cannot re-sign; this module only *reads* and *tells* (stderr), and never
fails the publish.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthorityProbe:
    """Result of one pre-publish authority read."""

    bound_head: str = ""
    authority_head: str = ""
    frames: int = 0
    degraded: bool = False


def _say(message: str) -> None:
    try:
        print(message, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 - an advisory write must never fail the publish
        pass


def _active_frames(state) -> int:
    frames = state.get("frames", {}) if isinstance(state, dict) else {}
    return sum(
        1
        for entry in frames.values()
        if isinstance(entry, dict) and entry.get("active") is not None
    )


def probe_authority(store) -> AuthorityProbe | None:
    """Read the authority's bound config head. None means no HQ authority is configured."""
    settings = getattr(store, "settings", None) or {}
    if not settings.get("runtime"):
        return None
    try:
        from .hq_sql_runtime import SqlRuntimeAuthority

        authority = SqlRuntimeAuthority(settings, broker=store.broker, clock=store.clock)
        head, state, crossref, _policies = authority.load_state()
        return AuthorityProbe(
            bound_head=crossref[2], authority_head=head, frames=_active_frames(state)
        )
    except Exception:  # noqa: BLE001 - degrade, never fail the publish
        return AuthorityProbe(degraded=True)


def announce_before(probe: AuthorityProbe | None, expected_head: str) -> None:
    """Pre-publish notice; only when authority is currently bound to the head being replaced."""
    if probe is None:
        return
    if probe.degraded:
        _say(
            "notice: HQ authority could not be read; this publish may unbind HQ authority "
            "and fence frames until the operator renews (bh hq authority renew)."
        )
        return
    if probe.bound_head == expected_head:
        _say(
            f"notice: this publish will unbind HQ authority for {probe.frames} active "
            f"frame(s) (bound to config head {probe.bound_head})."
        )


def announce_after(probe: AuthorityProbe | None, new_head: str) -> None:
    """Post-publish notice with the fenced-frame count and the exact off-frame renew command."""
    if probe is None or probe.degraded or not new_head or probe.bound_head == new_head:
        return
    _say(
        f"WARNING: fleet-config publish moved the config head; HQ authority is now UNBOUND "
        f"and {probe.frames} active frame(s) are fenced (eligibility fails with "
        f'"HQ config and authority publications are not bound").\n'
        f"  authority bound head: {probe.bound_head}\n"
        f"  new config head:      {new_head}\n"
        "  The publisher holds no operator key and cannot re-sign. Run this OFF-FRAME, on an "
        "operator host (never inside a frame), using the operator-settings binding there:\n"
        f"    bh hq authority renew --expected-revision {probe.authority_head} "
        "--operator-key <operator-key-path> --duration <seconds> --confirm"
    )
