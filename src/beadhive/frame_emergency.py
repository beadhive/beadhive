"""Operator-authorized bounded emergency admission and visible structured audit."""

from __future__ import annotations

import copy
import math

from . import hq_authority_guard as guard


def audit(event, record, *, prefix=None, revision=None):
    import structlog

    grant = record["emergency"]
    structlog.get_logger(__name__).warning(
        "frame_emergency_admission",
        action=event,
        frame_id=record["authority"]["frame_id"],
        holder_identity=record["authority"]["holder_identity"],
        prefix=prefix or grant["prefix"],
        reason=grant["reason"],
        expires_at=grant["expires_at"],
        original_revision=grant["original_revision"],
        revision=revision,
    )


def authorize(
    record, *, prefix, reason, duration, revision, now, registration, beat, execution_digest
):
    """Caller verified signed registration/receipt and original revision/host/release CAS."""
    if (
        type(duration) not in (int, float)
        or not math.isfinite(duration)
        or not 0 < duration <= 1800
        or record["state"] not in {"pending", "active"}
        or record["cordoned"]
        or not record["desired"]["declared"]
        or registration is None
        or beat is None
    ):
        raise ValueError("emergency admission requires declared uncordoned live grant and evidence")
    authority, desired = record["authority"], record["desired"]
    if authority.get("beadyard_id") is None:
        raise ValueError("emergency admission requires canonical HQ binding")
    expiry = authority["candidate_expires_at"]
    if expiry is not None and now >= expiry:
        raise ValueError("emergency admission cannot revive expired candidate authority")
    if (
        registration.frame_id != authority["frame_id"]
        or registration.host_id != authority["holder_identity"]
        or registration.instance_ref != authority["instance_ref"]
        or registration.beadyard_id != authority["beadyard_id"]
        or registration.release is None
        or registration.capabilities is None
        or registration.release.model_dump() != desired["release"]
        or registration.capabilities.model_dump() != desired["caps"]
        or beat.frame_id != authority["frame_id"]
        or beat.holderIdentity != authority["holder_identity"]
        or beat.instance_ref != authority["instance_ref"]
        or beat.beadyard_id != authority["beadyard_id"]
        or beat.key_id != authority["key_fingerprint"]
        or beat.epoch != authority["epoch"]
        or beat.audience != authority["audience"]
        or beat.config_revision != authority["config_revision"]
        or beat.release.model_dump() != desired["release"]
    ):
        raise ValueError("emergency admission identity/release/capabilities mismatch")
    record["state"] = "active"
    record["authority"]["candidate_expires_at"] = None
    record["emergency"] = {
        "domain": "beadhive/emergency-admission/v1",
        "prefix": prefix,
        "reason": reason,
        "issued_at": now,
        "expires_at": now + duration,
        "revoked_at": None,
        "authority": copy.deepcopy(authority),
        "release": copy.deepcopy(desired["release"]),
        "original_revision": revision,
        # Expiry/revocation never turns an emergency admission into normal admission.
        "review_required": True,
        "execution_digest": execution_digest,
    }
    guard.validate_emergency(record, now)


def revoke(record, now):
    if "emergency" not in record or record["state"] not in {"pending", "active"}:
        raise ValueError("no revocable emergency admission")
    record["emergency"]["revoked_at"] = now
    # Admission remains review-required; ordinary admit performs the full evidence checks.


def status(record, now):
    grant = record.get("emergency")
    if grant is None:
        return None
    return {
        **grant,
        "status": "revoked"
        if grant["revoked_at"] is not None
        else "expired"
        if now >= grant["expires_at"]
        else "active",
    }


def cap_lease(record, prefix, lease, now):
    """Client convenience only; the trusted receiver independently enforces the cap."""
    from dataclasses import replace

    from .host_lease_contracts import _parse_stamp, now_stamp

    if guard.emergency_review_required(record):
        if not locally_active(record, prefix, now):
            raise ValueError("emergency authorization expired/revoked or out of scope")
        return replace(
            lease,
            expires_at=now_stamp(
                min(_parse_stamp(lease.expires_at), record["emergency"]["expires_at"])
            ),
        )
    return lease


def locally_active(record, prefix, now):
    """All local emergency use requires freshly measured executing package bytes."""
    if not guard.emergency_active(record, prefix, now):
        return False
    try:
        from .heartbeat_report import installed_release

        matched = installed_release()["digest"] == record["emergency"]["execution_digest"]
        if matched:
            audit("local-use-attempt", record, prefix=prefix)
        return matched
    except (ImportError, OSError, ValueError, RuntimeError, KeyError):
        return False
