"""Rollout floor for writing ``managed_repos[].local_only`` (bh-sj2bf, local-only-hives.md R3).

Every config section forbids unknown keys, so a frame running a release older than the one that
introduced ``local_only`` fails ``host-config-partition`` on a fleet document carrying it, and the
SQL hive-policy projection rejects the whole catalog. Writing the key too early would fence the
fleet: the failure the marker exists to fix. So every writer of the key (onboard registering a
local-only hive, ``bh hive repair --local-only``) refuses while any frame attests an older
release, with no override. A frame whose release is unknown (no attested release, or an
unparseable one) may be waived with an explicit flag. Legacy (non-frame) hosts cannot be
measured and stay the operator's responsibility. This copies the ``release_policy`` pattern
(:mod:`beadhive.frame_release_policy`) and reuses its old/unknown classification.

Clearing the key (``bh hive replicate``) never needs the floor: it removes the key.
"""

from __future__ import annotations

from collections.abc import Callable

from . import config
from .frame_release_policy import unsafe_readers

#: First release whose config reader accepts ``managed_repos[].local_only``. Bump only if a later
#: reader-visible change to the marker shape ships.
MIN_LOCAL_ONLY_READER = "0.39.0"

#: The explicit waiver flag for frames whose release is unknown (never for an older release).
ALLOW_UNKNOWN_FLAG = "--allow-unknown-readers"


def measured_frames():
    """``(authority_state, evidence)`` for the fleet's frames, or None when no frame authority
    is in force (a legacy fleet: nothing to measure).

    SQL HQ with an authority-writer binding reads attested releases exactly as
    ``release-policy`` does (verified evidence carrier, AS OF the verified head); without that
    binding, or on a Git authority, the authority state's own receipt carriers are read and a
    frame without one counts as unknown. Raises when the authority exists but cannot be read."""
    from .hq_control_plane import SqlControlPlane, control_plane

    binding = config.load_host().get("hq", {}) or {}
    plane = control_plane(config.hq_dir())
    if isinstance(plane, SqlControlPlane):
        if plane.settings.get("runtime") is None:
            return None
        if plane.settings.get("authority_writer") is not None:
            operator = plane._operator()
            budget = plane._operator_deadline()
            head, state, *_ = operator.load(deadline=budget)
            evidence = operator.attested_releases(state, expected_revision=head, deadline=budget)
            return state, evidence
        return plane.eligibility_authority_status()["state"], {}
    if not binding.get("authority_anchor"):
        return None
    return plane.eligibility_authority_status()["state"], {}


def refusal(
    *, allow_unknown_readers: bool = False, reader: Callable[[], object] | None = None
) -> str | None:
    """Why ``local_only`` may NOT be written now, or None when every frame can read it.

    ``reader`` (default :func:`measured_frames`) returns ``(state, evidence)`` or None. An
    authority that cannot be read is unknown for every frame: waivable, never silently passed."""
    read = reader if reader is not None else measured_frames
    try:
        measured = read()
    except Exception:  # noqa: BLE001 — an unreadable authority is an unknown release, below
        if allow_unknown_readers:
            return None
        return (
            "local-only refused: the fleet's frame releases could not be read, so frames may "
            f"not read managed_repos[].local_only (needs {MIN_LOCAL_ONLY_READER} or newer); "
            f"pass {ALLOW_UNKNOWN_FLAG} to accept the risk"
        )
    if measured is None:
        return None
    state, evidence = measured
    blocked = unsafe_readers(state, evidence, floor=MIN_LOCAL_ONLY_READER)
    too_old = [(f, r) for f, r, kind in blocked if kind == "old"]
    unknown = [(f, r) for f, r, kind in blocked if kind == "unknown"]
    if too_old:
        names = ", ".join(f"{f} ({r})" for f, r in too_old)
        return (
            f"local-only refused: these frames attest a release below {MIN_LOCAL_ONLY_READER} "
            f"and cannot read managed_repos[].local_only: {names}; upgrade them first "
            "(no override)"
        )
    if unknown and not allow_unknown_readers:
        names = ", ".join(f"{f} ({r or 'no attested release'})" for f, r in unknown)
        return (
            "local-only refused: the release of these frames is unknown, so they may not read "
            f"managed_repos[].local_only: {names}; pass {ALLOW_UNKNOWN_FLAG} to accept the risk"
        )
    return None
