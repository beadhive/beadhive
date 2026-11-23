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

On an ``hq.route: api`` host (bh-obhfx) the join goes only through the HQ port, with no SQL
login, grant or escrowed secret and no ``authority_writer`` binding: the incarnation is
registered with hq-service (``POST /incarnations``, operator-bound at the Gateway) and the frame
open-admits itself (``POST /admissions/{frame}``, frame-bound); hq-service builds and signs the
grant from the committed manifest. Each step a caller is not bound for is left to the seat that
is (:func:`_join_api`).
"""

from __future__ import annotations

from . import hq_authority_enforce
from . import hq_authority_guard as guard
from .hq_control_plane import ControlPlaneError, HqPlaneRefused, open_admission_guard

#: Conformance profile an open-admitted frame echoes when the fleet has no other frame to copy.
DEFAULT_PROFILE = "open-admission"
#: Heartbeat audience when the fleet has no other frame to copy and no beadyard identity.
DEFAULT_AUDIENCE = "beadhive"


def _api(plane) -> bool:
    return getattr(plane, "config_backend", None) == "api"


def _operator_state(plane) -> tuple[str, dict]:
    """``(authority head, state)`` read with operator custody, on either plane (the verified
    ``GET /authority`` on an api plane)."""
    if _api(plane):
        head, state, _crossref, _policies = plane.authority_state()
        return head, state or {}
    if hasattr(plane, "_operator_read"):
        head, state, _policy = plane._operator_read()
        return head, state or {}
    head, state, _crossref, _policies = plane._operator().load(deadline=plane._operator_deadline())
    return head, state


def publish_command(host_id: str) -> str:
    """The operator command that publishes ``host_id``'s manifest AND rebinds the authority
    onto it on ``hq.route: api`` (hq-service v0.1.3, bh-hb75g)."""
    return (
        f"bh hq authority publish-manifest --frame {host_id} --manifest <hosts/{host_id}.yaml> "
        "--plan, then the same with --confirm (and --operator-key in a signed fleet)"
    )


def committed_manifest(plane, host_id: str):
    """``host_id``'s committed manifest, as the join needs it.

    On ``hq.route: api`` a manifest hq-service does not serve (never published, or published
    but the authority still pins an older config head, bh-e4aou) is refused naming the
    operator step that fixes it: the signed publication that also rebinds. The exception keeps
    its type (:class:`~beadhive.hq_control_plane.CommittedManifestAbsent`)."""
    from .hq_control_plane import CommittedManifestAbsent

    try:
        return plane.load_host_manifest(host_id)
    except CommittedManifestAbsent as exc:
        if not _api(plane):
            raise
        raise CommittedManifestAbsent(
            f"{exc}: hq-service does not serve it (not published, or published while the "
            "authority still pins an older config head). An operator publishes it and rebinds "
            f"in one step: `{publish_command(host_id)}`; then join again"
        ) from None


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
    manifest = committed_manifest(plane, host_id)
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
    if _api(plane):
        return _join_api(plane, host_id, authority, key, desired, expected)
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


def _join_api(plane, host_id, authority, key, desired, expected) -> dict:
    """Register the incarnation, then open-admit it, through the HQ port only.

    ``POST /incarnations`` is operator-bound and ``POST /admissions/{frame}`` frame-bound
    (hq-service v0.1.0, ``caller_not_bound``), so one credential may hold only one of them:
    a frame skips the registration it is not bound for (the operator registered it), and an
    operator skips the admission (the frame admits itself). Both principal and epoch are
    derived as hq-service derives them, so the two seats name the same incarnation."""
    from .hq_sql_signatures import sql_principal

    principal = sql_principal(authority)
    result = {
        "host_id": host_id,
        "frame_id": authority.frame_id,
        "principal": principal,
        "epoch": authority.epoch,
        "route": "api",
    }
    try:
        plane.register_incarnation(authority, expected=expected)
        result["registered"] = True
    except HqPlaneRefused as exc:
        if exc.code != "caller_not_bound":
            raise
        result["registered"] = False  # not this caller's: the operator registers it
    try:
        revision = plane.admit_open(authority, key, desired, expected=expected)
    except HqPlaneRefused as exc:
        if exc.code == "caller_not_bound" and result["registered"]:
            # An operator registered the incarnation; only the frame can admit itself.
            return {**result, "admitted": False, "changed": True}
        if exc.code == "incarnation_unprovisioned":
            raise ControlPlaneError(
                f"open admission of {host_id}: incarnation {principal} epoch {authority.epoch} "
                "is not registered with hq-service; an operator-bound caller registers it "
                f"(`bh hq authority join --frame {host_id} --public-key <key.pub> --confirm`), "
                "then the frame joins again"
            ) from None
        raise
    return {
        **result,
        "admitted": True,
        "changed": True,
        "revision": revision,
        "mode": hq_authority_enforce.mode(),
    }
