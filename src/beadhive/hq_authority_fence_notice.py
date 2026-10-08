"""Advisory notice when a fleet-config publication unbinds SQL HQ authority.

Operator-signed runtime authority is cross-bound to a fleet-config head.  A publication
that changes anything frames enforce (a ``frame_policy``, a managed hive's identity, a host
manifest, ``allowed_signers``, the beadyard identity) fences every frame: eligibility fails
with "HQ config and authority publications are not bound" until the operator renews.  A
publication that changes nothing frames enforce is tolerated (bh-u67ve) and announces
nothing (bh-3h6al); the same shared check decides both.  The publisher holds no key and
cannot re-sign; this module only *reads* and *tells* (stderr), and never fails the publish.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class AuthorityProbe:
    """Result of one pre-publish authority read."""

    bound_head: str = ""
    authority_head: str = ""
    frames: int = 0
    degraded: bool = False
    #: The latest config head when frames currently accept the authority there (exact or
    #: tolerated), else "". Empty also when the binding could not be read: exact-head
    #: behaviour then applies, so a doubt still announces.
    accepted_head: str = ""
    #: The verified authority-bound snapshot, its signed policies and expiry, used to judge
    #: whether the documents being published are tolerated. None when unread.
    bound_snapshot: object = None
    policies: object = None
    expires_at: float = 0.0


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
        # Resolved by name: a static edge would close hq_sql_config -> hq_sql_runtime cycle.
        runtime = importlib.import_module(f"{__package__}.hq_sql_runtime")
        authority = runtime.SqlRuntimeAuthority(settings, broker=store.broker, clock=store.clock)
        head, state, crossref, policies = authority.load_state()
    except Exception:  # noqa: BLE001 - degrade, never fail the publish
        return AuthorityProbe(degraded=True)
    probe = AuthorityProbe(
        bound_head=crossref[2], authority_head=head, frames=_active_frames(state)
    )
    try:
        bound, latest, snapshot = store.authority_binding(
            crossref, policies, expires_at=state["expires_at"]
        )
    except Exception:  # noqa: BLE001 - binding unread: fall back to exact-head notices
        return probe
    if not bound:
        return probe
    return replace(
        probe,
        accepted_head=latest,
        bound_snapshot=snapshot,
        policies=policies,
        expires_at=state["expires_at"],
    )


def _tolerated(probe: AuthorityProbe, documents) -> bool:
    """Whether publishing `documents` keeps the authority bound (the shared frame check).

    The new head descends from the head being replaced by construction (CAS publish), and
    that head is accepted, so ancestry holds; only the content check remains.
    """
    if probe.bound_snapshot is None or documents is None:
        return False
    try:
        from .hq_hive_policy import config_head_tolerated

        current = replace(probe.bound_snapshot, documents=tuple(documents))
        clock = getattr(probe.bound_snapshot, "fetched_at", 0.0)
        return config_head_tolerated(
            probe.bound_snapshot,
            current,
            probe.policies,
            valid_until=probe.expires_at,
            now=min(clock, probe.expires_at - 1),
        )
    except Exception:  # noqa: BLE001 - doubt announces
        return False


def announce_before(probe: AuthorityProbe | None, expected_head: str, documents=None) -> None:
    """Pre-publish notice; only when authority is currently bound to the head being replaced
    and the `documents` being published change something frames enforce."""
    if probe is None:
        return
    if probe.degraded:
        _say(
            "notice: HQ authority could not be read; this publish may unbind HQ authority "
            "and fence frames until the operator renews (bh hq authority rebind)."
        )
        return
    if expected_head in (probe.bound_head, probe.accepted_head or None) and not _tolerated(
        probe, documents
    ):
        _say(
            f"notice: this publish will unbind HQ authority for {probe.frames} active "
            f"frame(s) (bound to config head {probe.bound_head})."
        )


def announce_after(probe: AuthorityProbe | None, new_head: str, documents=None) -> None:
    """Post-publish notice with the fenced-frame count and the exact off-frame renew command.

    Silent when the published `documents` are tolerated: frames still accept the authority.
    """
    if probe is None or probe.degraded or not new_head or probe.bound_head == new_head:
        return
    if _tolerated(probe, documents):
        return
    _say(
        f"WARNING: fleet-config publish moved the config head; HQ authority is now UNBOUND "
        f"and {probe.frames} active frame(s) are fenced (eligibility fails with "
        f'"HQ config and authority publications are not bound").\n'
        f"  authority bound head: {probe.bound_head}\n"
        f"  new config head:      {new_head}\n"
        "  The publisher holds no operator key and cannot re-sign. Run this OFF-FRAME, on an "
        "operator host (never inside a frame), using the operator-settings binding there:\n"
        f"    bh hq authority rebind --expected-revision {probe.authority_head} "
        "--operator-key <operator-key-path> --confirm"
    )
