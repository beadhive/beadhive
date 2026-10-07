"""Open admission in a trusted fleet (bh-taa04.3).

When the fleet default authority mode is ``trusted`` (``bh hq authority mode-trusted``), a
registering frame joins with no operator grant record, no enrollment/observation gate and no
operator key: :func:`join` derives the incarnation entirely from the frame's own committed host
manifest and runtime public key, and writes it ACTIVE in one key-less authority publication
(:meth:`~beadhive.hq_control_plane.GitControlPlane.admit_open` /
:meth:`~beadhive.hq_control_plane.SqlControlPlane.admit_open`). Cordon, drain and retire act on
the record like on any other.

Who writes: the authority publication needs HQ authority-write access (the Git operator anchor
or the SQL ``authority_writer`` binding, e.g. via ``$BH_HQ_OPERATOR_SETTINGS``) — in a trusted
fleet HQ write access is admin by definition. SQL HQ still needs the frame's server-local
principal provisioning (account, inbox and session tables) that is not an authority step.

Trust delta: in a trusted fleet anything that can reach HQ with write access and register can
join. A signed fleet refuses (fail closed) exactly as before.
"""

from __future__ import annotations

from . import hq_authority_enforce
from . import hq_authority_guard as guard
from .hq_control_plane import ControlPlaneError, open_admission_guard

#: Conformance profile an open-admitted frame echoes when the fleet has no other frame to copy.
DEFAULT_PROFILE = "open-admission"
#: Heartbeat audience when the fleet has no other frame to copy and no beadyard identity.
DEFAULT_AUDIENCE = "beadhive"


def _operator_state(plane) -> tuple[str, dict]:
    """``(authority head, state)`` read with operator custody, on either plane."""
    if hasattr(plane, "_operator_read"):
        head, state, _policy = plane._operator_read()
        return head, state or {}
    head, state, _crossref, _policies = plane._operator().load(deadline=plane._operator_deadline())
    return head, state


def _fingerprint(plane, public_key: str) -> str:
    if hasattr(plane, "_operator_read"):
        from .hq_control_plane import fingerprint

        return fingerprint(public_key)
    from .hq_sql_signatures import fingerprint

    return fingerprint(public_key)


def request(plane, host_id: str, public_key: str) -> tuple:
    """``(authority, public_key, desired, expected)`` for `host_id`'s open admission.

    Identity (frame, holder, instance, beadyard), release and capabilities come from the
    frame's committed host manifest; audience and conformance profile are copied from the
    fleet's existing incarnations (so heartbeats compare like every other frame's), else
    :data:`DEFAULT_AUDIENCE` / :data:`DEFAULT_PROFILE`; the epoch advances the frame's floor.
    """
    from .host_heartbeat_core import ObservationAuthority

    open_admission_guard({})
    manifest = plane.load_host_manifest(host_id)
    if (
        not manifest.frame_id
        or not manifest.instance_ref
        or manifest.release is None
        or manifest.capabilities is None
    ):
        raise ControlPlaneError(
            "open admission needs a committed frame manifest with frame_id, instance_ref, "
            "release and capabilities (bh host init / bh hq push)"
        )
    if not public_key or not public_key.strip():
        raise ControlPlaneError("open admission needs the frame's runtime public key")
    head, state = _operator_state(plane)
    records = [record for _, record in guard.records(state or {"frames": {}})]
    audience = next(
        (record["authority"]["audience"] for record in records),
        manifest.beadyard_id or DEFAULT_AUDIENCE,
    )
    profile = next((record["desired"]["profile"] for record in records), DEFAULT_PROFILE)
    entry = (state.get("frames") or {}).get(manifest.frame_id) or {"epoch_floor": -1}
    try:
        config_revision = plane.config_store().load_snapshot().commit_revision
    except (ValueError, OSError):  # Git HQ without a published config carrier
        config_revision = next(
            (record["authority"]["config_revision"] for record in records), head or "unpublished"
        )
    authority = ObservationAuthority(
        frame_id=manifest.frame_id,
        holder_identity=manifest.host_id,
        instance_ref=manifest.instance_ref,
        key_fingerprint=_fingerprint(plane, public_key),
        epoch=entry["epoch_floor"] + 1,
        audience=audience,
        config_revision=config_revision,
        candidate_expires_at=None,
        beadyard_id=manifest.beadyard_id,
    )
    desired = {
        "declared": True,
        "release": manifest.release.model_dump(),
        "caps": manifest.capabilities.model_dump(),
        "profile": profile,
    }
    return authority, public_key.strip(), desired, head


def admitted(plane, host_id: str) -> bool:
    """Whether `host_id` already holds a live (non-retired) incarnation."""
    _head, state = _operator_state(plane)
    for _, record in guard.records(state or {"frames": {}}):
        if record["authority"]["holder_identity"] == host_id and record["state"] != "retired":
            return True
    return False


def join(plane, host_id: str, public_key: str, *, operator_key: str = "") -> dict:
    """Open-admit `host_id` (idempotent: an already-admitted holder is reported, not rewritten).

    Refused unless this process has learned a trusted fleet default."""
    open_admission_guard({})
    if admitted(plane, host_id):
        return {"host_id": host_id, "admitted": True, "changed": False}
    authority, key, desired, expected = request(plane, host_id, public_key)
    revision = plane.admit_open(
        authority, key, desired, expected=expected, operator_key=operator_key
    )
    return {
        "host_id": host_id,
        "frame_id": authority.frame_id,
        "admitted": True,
        "changed": True,
        "revision": revision,
        "mode": hq_authority_enforce.mode(),
    }
