"""Operator publication on a ``route: api`` host (bh-lejfy): rebind, release-policy and the
signed host-manifest publication (bh-hb75g).

hq-service serves ``POST /authority/rebind/{plan,apply}`` and
``POST /authority/release-policy/{frame}/{plan,apply}``. The service rebuilds the record from
the request (design decision D1); the operator signs its ``signing_input`` client-side, with the
same ``--operator-key`` the SQL route uses, and the signature is skipped when the service says it
needs none (a trusted fleet). Nothing here opens SQL: every call is one HQ port call.

The port's ``state`` argument is the plan REQUEST on this route (``{frame?, duration_s?}`` for
rebind, ``{spec, allow_downgrade?, allow_unknown_readers?}`` for release-policy).

Manifest publication (hq-service v0.1.3, bh-hq-0ci) is ONE operator operation that commits a
host manifest to the fleet config AND rebinds the authority onto the config head that commit
makes, so a frame join sees the manifest with no separate rebind. It is not atomic (config
first, then authority, two databases): a failure between the two is completed by sending the
same apply again, which :func:`publish_manifest` does itself, and a fresh plan also recovers.
"""

from __future__ import annotations

import hashlib
import json

from . import hq_authority_ceiling
from . import hq_authority_enforce as authority_mode
from .hq_port import (
    HqDeadline,
    HqError,
    HqOutcomeUnknown,
    HqRefused,
    HqUnavailable,
    operator_apply_body,
)
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


#: What an operator does about a refusal of a manifest publication or a rebind plan, by code
#: (hq-service ``conformance/operator/v1`` and the shared registries, v0.1.3).
HINTS = {
    "stale_revision": "the authority or config head moved since the plan; run it again to plan "
    "afresh",
    "plan_expired": "the plan outlived its window; run it again to plan afresh",
    "plan_mismatch": "the apply is not what the plan yields (the manifest, duration or echoed "
    "record changed since the plan); run it again to plan afresh",
    "committed_unreadable": "the config committed at a head other than the signed one; run it "
    "again to plan afresh (the new plan sees the committed manifest and only rebinds)",
    "operator_state_invalid": "the authority it would publish is invalid: check this host's and "
    "hq-service's clocks (issued_at must not be in the future) and --duration",
    "operator_signature_required": "this fleet is signed: pass --operator-key",
    "operator_signature_invalid": "the signature does not verify under hq-service's operator "
    "key: check --operator-key",
    "operator_apply_disabled": "hq-service must run with -operator-apply (hq-service v0.1.3 or "
    "later; in a signed fleet also with the config-database grants in its deploy/README.md)",
    "working_set_dirty": "the HQ runtime database has uncommitted changes; an HQ administrator "
    "must clear them before any operator publication",
    "manifest_invalid": "the manifest has an unknown or mistyped field, or a secret value",
    "host_id_mismatch": "the manifest's host_id is not the host named by --frame",
    "beadyard_mismatch": "the manifest's beadyard_id is not this HQ's (hq.beadyard_id)",
    "path_invalid": "the host id is not a manifest file name",
    "config_unreadable": "hq-service cannot read the fleet config; a config carrying bh's "
    "fleet.json needs hq-service v0.1.2 or later",
    "rebind_policy_ambiguous": "managed hives carry different frame_policy.config_revision "
    "values; align them in fleet.yaml and publish the config first",
    "rebind_no_frame_grant": "the frame has no active or candidate grant to rebind",
}


def _hinted(error: HqError) -> str:
    text = str(error)
    code = getattr(error, "code", None)
    if code is None:  # a retryable refusal arrives as unavailable, naming its code
        code = next((name for name in HINTS if name in text), None)
    hint = HINTS.get(code or "")
    if hint is None and isinstance(error, HqUnavailable):
        if " 404" in text:
            hint = "this hq-service does not serve the route: manifest publication needs v0.1.3"
        elif " 401" in text:
            hint = "the hq.api credential is not bound with the lifecycle:operator scope"
    return f"{text} ({hint})" if hint else text


def refusal(error: HqError, *, hinted: bool = False) -> ApiOperatorError:
    """A port refusal as the control-plane error every operator CLI path already reports.
    ``hinted`` appends what the operator does about it (:data:`HINTS`)."""
    if isinstance(error, HqRefused):
        return ApiOperatorError(f"hq-service refused: {_hinted(error) if hinted else error}")
    if hinted and isinstance(error, HqUnavailable):
        coded = any(code in str(error) for code in HINTS)  # a retryable refusal, by its code
        return ApiOperatorError(
            f"hq-service {'refused' if coded else 'unavailable'}: {_hinted(error)}"
        )
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


def _request(duration, ceiling, **fields):
    """A rebind-shaped plan request; ``duration`` is checked against ``ceiling`` before any
    I/O."""
    try:
        hq_authority_ceiling.signed_expiry(0, duration, ceiling)
    except ValueError as exc:
        raise ApiOperatorError(str(exc)) from None
    request = {key: value for key, value in fields.items() if value}
    if duration is not None:
        request["duration_s"] = int(duration)
    return request


def _plan_facts(plan):
    """What every operator plan shows: the heads it binds, its window and what is signed."""
    record = plan.get("record") if isinstance(plan.get("record"), dict) else {}
    summary = plan.get("summary") if isinstance(plan.get("summary"), dict) else {}
    return {
        "plan": True,
        "revision": plan.get("revision"),
        "next_revision": record.get("revision"),
        "config_head": plan.get("config_head"),
        "config_moved": summary.get("config_moved"),
        "issued_at": plan.get("issued_at"),
        "expires_at": plan.get("expires_at"),
        "plan_expires_at": plan.get("plan_expires_at"),
        "signature_needed": plan.get("signature_needed") is not False,
        "signing_sha256": plan.get("signing_sha256"),
        "summary": summary,
    }


def rebind_plan(plane, *, expected="", duration=None, ceiling=None, frame=None):
    """``bh hq authority rebind --plan`` on the api plane: the service's rebind plan, nothing
    written, no key and no ``--confirm`` (bh-hb75g). ``summary`` carries ``config_head_before``
    / ``config_head_after`` / ``config_moved`` and, with ``--frame``, the grant's
    ``config_revision_before`` / ``config_revision_after``. With ``expected`` the plan must have
    been made at that authority head."""
    request = _request(duration, ceiling, frame=frame)
    try:
        plan = plane._port_call("plan_rebind", request)
    except (HqRefused, HqUnavailable, HqOutcomeUnknown, HqDeadline) as exc:
        raise refusal(exc, hinted=True) from None
    if expected and plan.get("revision") != expected:
        raise ApiOperatorError(
            f"hq-service refused: stale_revision: the plan was made at {plan.get('revision')}, "
            f"not the expected {expected} ({HINTS['stale_revision']})"
        )
    return {**_plan_facts(plan), **({"frame": frame} if frame else {})}


#: How many times an apply that may have half-completed is sent again, byte for byte, before
#: the operator is told to run the command again.
MANIFEST_RESENDS = 2


def publish_manifest(
    plane,
    manifest,
    *,
    plan_only=False,
    confirm=False,
    operator_key="",
    expected="",
    duration=None,
    ceiling=None,
):
    """``bh hq authority publish-manifest`` on the api plane (hq-service v0.1.3, bh-hb75g).

    ``manifest`` is the host manifest as ``bh host init`` publishes it (a JSON mapping). The
    service's plan shows the document it will write (``manifest_sha256``), the config head the
    publication makes (``config_head_after``) and the rebound authority that binds it; with
    ``plan_only`` nothing more happens. Otherwise (``confirm``, and an operator key unless the
    fleet is trusted) the plan's ``signing_input`` is signed with the rebind signer and applied.

    The apply commits the config, then the authority. An unknown outcome or a transient
    failure re-sends the SAME apply (:data:`MANIFEST_RESENDS` times): it completes a publication
    that stopped between the two commits, or answers ``replayed``. If it still fails, the
    operator runs the command again: the fresh plan sees the committed manifest and only
    rebinds."""
    host_id = manifest.get("host_id") if isinstance(manifest, dict) else None
    if not host_id:
        raise ApiOperatorError("publish-manifest: the manifest carries no host_id")
    request = _request(duration, ceiling, manifest=manifest)
    if not plan_only:
        if not confirm:
            raise ApiOperatorError(
                "operator mutation requires --confirm (preview it first with --plan)"
            )
        if authority_mode.key_required(operator_key):
            raise ApiOperatorError("mutation requires separate --operator-key")
    try:
        plan = plane._port_call("plan_manifest_publish", host_id, request)
    except (HqRefused, HqUnavailable, HqOutcomeUnknown, HqDeadline) as exc:
        raise refusal(exc, hinted=True) from None
    if expected and plan.get("revision") != expected:
        raise ApiOperatorError(
            f"hq-service refused: stale_revision: the plan was made at {plan.get('revision')}, "
            f"not the expected {expected} ({HINTS['stale_revision']})"
        )
    document = plan["manifest"]
    publication = plan.get("config_publication") or {}
    result = {
        **_plan_facts(plan),
        "host_id": host_id,
        "path": document.get("path"),
        "manifest_sha256": document.get("sha256"),
        "config_head_after": plan.get("config_head_after"),
        "config_published": bool(publication.get("publish")),
        "config_publication": publication,
    }
    if plan_only:
        return result
    body = operator_apply_body(request, plan, signer=signer(operator_key))
    sent = 0
    while True:
        sent += 1
        try:
            answer = plane._port_call("apply_manifest_publish", host_id, body)
            break
        except (HqOutcomeUnknown, HqUnavailable, HqDeadline) as exc:
            if "stale_revision" in str(exc):
                # A head moved under the apply: the same bytes can only be refused again.
                raise refusal(exc, hinted=True) from None
            if sent <= MANIFEST_RESENDS:
                continue  # the same apply, byte for byte: it completes or replays
            raise ApiOperatorError(
                f"manifest publication of {host_id} did not complete after {sent} sends of the "
                f"same apply ({_hinted(exc)}). The config may hold the manifest while the "
                "authority still pins the older head (frames fence until it moves). Run this "
                "command again: the new plan sees the committed manifest and only rebinds"
            ) from None
        except HqRefused as exc:
            raise refusal(exc, hinted=True) from None
    result.update(
        plan=False,
        outcome=answer.get("outcome"),
        revision=answer.get("revision"),
        config_head=answer.get("config_head", plan.get("config_head_after")),
        sends=sent,
    )
    if "config_published" in answer:
        result["config_published"] = bool(answer["config_published"])
    return result


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
