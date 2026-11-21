"""Operator publication on a ``route: api`` host (bh-lejfy): rebind and release-policy.

hq-service serves ``POST /authority/rebind/{plan,apply}`` and
``POST /authority/release-policy/{frame}/{plan,apply}``. The service rebuilds the record from
the request (design decision D1); the operator signs its ``signing_input`` client-side, with the
same ``--operator-key`` the SQL route uses, and the signature is skipped when the service says it
needs none (a trusted fleet). Nothing here opens SQL: every call is one HQ port call.

The port's ``state`` argument is the plan REQUEST on this route (``{frame?, duration_s?}`` for
rebind, ``{spec, allow_downgrade?, allow_unknown_readers?}`` for release-policy).
"""

from __future__ import annotations

import hashlib
import json

from . import hq_authority_ceiling
from . import hq_authority_enforce as authority_mode
from .hq_port import HqDeadline, HqError, HqOutcomeUnknown, HqRefused
from .hq_sql_signatures import SqlSignatureError, sign_authority, sign_catalog_record

DOMAIN = "beadhive/sql-release-policy/v1"


class ApiOperatorError(ValueError):
    """An operator refusal on the api route. ``ApiControlPlane`` reports it as the control
    plane's own ``ApiOperatorError``; this module cannot import that module (a cycle)."""


def signer(operator_key, *, catalog=False):
    """The port's ``Signer`` over ``--operator-key``. It is only called when the service
    asks for a signature, so a key-less trusted-mode operator never reaches it."""
    sign = sign_catalog_record if catalog else sign_authority

    def sign_record(record):
        if not operator_key:
            raise ApiOperatorError(
                "the service requires the operator's signature: pass --operator-key"
            )
        try:
            return sign(record, signing_key=str(operator_key))
        except SqlSignatureError as exc:
            raise ApiOperatorError(str(exc)) from None

    return sign_record


def refusal(error: HqError) -> ApiOperatorError:
    """A port refusal as the control-plane error every operator CLI path already reports."""
    if isinstance(error, HqRefused):
        return ApiOperatorError(f"hq-service refused: {error}")
    if isinstance(error, HqOutcomeUnknown):
        return ApiOperatorError(
            f"hq-service outcome unknown ({error}); re-read the authority before retrying"
        )
    if isinstance(error, HqDeadline):
        return ApiOperatorError(f"hq-service deadline exceeded: {error}")
    return ApiOperatorError(str(error))


def rebind(plane, *, expected, operator_key, duration=None, ceiling=None, frame=None):
    """``bh hq authority rebind`` on the api plane. Returns the new authority head."""
    if authority_mode.key_required(operator_key):
        raise ApiOperatorError("mutation requires separate --operator-key")
    try:
        hq_authority_ceiling.signed_expiry(0, duration, ceiling)  # checked before any I/O
    except ValueError as exc:
        raise ApiOperatorError(str(exc)) from None
    if not expected:
        raise ApiOperatorError("rebind requires --expected-revision (the authority head)")
    request = {}
    if frame:
        request["frame"] = frame
    if duration is not None:
        request["duration_s"] = int(duration)
    try:
        return plane._port_call(
            "rebind", request, signer=signer(operator_key), expected_revision=expected
        )
    except (HqRefused, HqOutcomeUnknown, HqDeadline) as exc:
        raise refusal(exc) from None


def _plan_summary(frame, plan):
    """What the operator reviews, and the digest ``--plan-sha256`` binds on apply.

    The digest covers the new policy, the head and the service's own summary; it leaves out the
    plan's ``issued_at`` / ``expires_at``, which differ between two plans of the same change."""
    record = plan["record"]
    policy = None
    slot = "active"
    try:
        entry = record["state"]["frames"][frame]
        slot = "active" if entry.get("active") is not None else "candidate"
        policy = entry[slot]["desired"].get("release_policy")
    except (KeyError, TypeError, AttributeError):
        raise ApiOperatorError("hq-service plan does not describe this frame's grant") from None
    summary = plan.get("summary") if isinstance(plan.get("summary"), dict) else {}
    body = {
        "domain": DOMAIN,
        "head": plan.get("revision"),
        "frame_id": frame,
        "slot": slot,
        "release_policy": policy,
        "summary": summary,
    }
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    return {
        "revision": plan.get("revision"),
        "frame_id": frame,
        "slot": slot,
        "release_policy": policy,
        "stored_spec": None if policy is None else policy.get("spec"),
        "attested_release": summary.get("attested_release"),
        "downgrade": bool(summary.get("downgrade")),
        "epoch_advanced": bool(summary.get("epoch_advanced")),
        "signature_needed": plan.get("signature_needed") is not False,
        "plan_sha256": digest,
    }


def release_policy(
    plane,
    frame,
    action="plan",
    *,
    spec="",
    plan_sha256="",
    operator_key="",
    confirm=False,
    allow_downgrade=False,
    allow_unknown_readers=False,
):
    """``bh hq authority release-policy plan|apply`` on the api plane.

    ``plan`` asks the service for its plan and writes nothing. ``apply`` plans again, refuses
    unless the plan's digest is the reviewed ``plan_sha256``, then signs and applies at the head
    it planned."""
    if action not in {"plan", "apply"}:
        raise ApiOperatorError("release-policy requires plan or apply")
    if not spec:
        raise ApiOperatorError("release-policy requires --spec")
    request = {"spec": spec}
    if allow_downgrade:
        request["allow_downgrade"] = True
    if allow_unknown_readers:
        request["allow_unknown_readers"] = True
    try:
        plan = plane._port_call("plan_release_policy", frame, request)
        result = _plan_summary(frame, plan)
        if action == "plan":
            return result
        if (
            not confirm
            or authority_mode.key_required(operator_key)
            or plan_sha256 != result["plan_sha256"]
        ):
            raise ApiOperatorError(
                "apply requires --confirm, approved operator key and exact reviewed plan digest"
            )
        result["revision"] = plane._port_call(
            "release_policy",
            frame,
            request,
            signer=signer(operator_key),
            expected_revision=plan["revision"],
        )
        return result
    except (HqRefused, HqOutcomeUnknown, HqDeadline) as exc:
        raise refusal(exc) from None
