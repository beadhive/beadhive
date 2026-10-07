"""Explicit reviewed SQL release rotation, preserving identity and accepted evidence.

Two exact shapes exist. A *pending* candidate without an active incarnation
rotates to a new pending epoch and still needs normal admission. A sole
*active* incarnation rotates in place: the operator-signed plan moves the same
admitted identity to a new epoch, release, profile and principal route, keeping
it active (and its cordon bit) while resetting accepted evidence. Eligibility
then needs a fresh authenticated beat at the new epoch carrying the new digest;
the archived active grant lets the hive lease (and so every claim fenced by its
epoch) renew across the rotation. See docs/design/active-frame-release-rotation-adr.md.
"""

from __future__ import annotations

import copy
import hashlib
import math

from ruamel.yaml import YAML

from . import hq_authority_enforce
from . import hq_authority_guard as guard
from .host_manifest_contracts import HostManifest
from .hq_framelease_contracts import ObservationAuthority
from .hq_sql_signatures import canonical, sql_principal

PENDING_DOMAIN = "beadhive/sql-release-upgrade/v1"
ACTIVE_DOMAIN = "beadhive/sql-active-release-rotation/v1"
HISTORY_LIMIT = 16


def candidate(state, frame):
    entry = state.get("frames", {}).get(frame)
    if (
        entry is None
        or entry["active"] is not None
        or entry["candidate"] is None
        or entry["candidate"]["state"] != "pending"
        or "emergency" in entry["candidate"]
    ):
        raise ValueError(
            "release-upgrade supports only a pending candidate without an active incarnation "
            "or a sole active incarnation; quarantined/emergency/coexisting frames are unsupported"
        )
    return entry["candidate"]


def incumbent(state, frame):
    """The sole uncontested active incarnation, without a live or unreviewed emergency."""
    entry = state.get("frames", {}).get(frame)
    record = None if entry is None else entry["active"]
    grant = (record or {}).get("emergency")
    if (
        record is None
        or entry["candidate"] is not None
        or record["state"] != "active"
        or grant is not None
        and (grant.get("revoked_at") is None or grant.get("review_required") is not False)
    ):
        raise ValueError(
            "active release rotation requires a sole active incarnation without a candidate "
            "or live/unreviewed emergency authorization"
        )
    return record


def selected(state, frame):
    """``(slot, record)`` for the one supported rotation shape of this frame."""
    entry = state.get("frames", {}).get(frame) or {}
    if entry.get("active") is None:
        return "candidate", candidate(state, frame)
    return "active", incumbent(state, frame)


def manifest_for(snapshot, frame, record, request, *, state="pending"):
    authority = record["authority"]
    documents = [
        item
        for item in snapshot.documents
        if item.path == f"hosts/{authority['holder_identity']}.yaml"
    ]
    if len(documents) != 1:
        raise ValueError("release-upgrade requires a prepared committed canonical host manifest")
    raw = YAML(typ="safe").load(documents[0].content)
    if isinstance(raw, dict) and "state" not in raw:
        raw = {**raw, "state": "active"}
    manifest = HostManifest.model_validate(raw)
    if (
        snapshot.commit_revision != request["config_head"]
        or not snapshot.beadyard_id
        or manifest.beadyard_id != snapshot.beadyard_id
        or manifest.beadyard_id != authority.get("beadyard_id")
        or manifest.frame_id != frame
        or manifest.host_id != authority["holder_identity"]
        or manifest.instance_ref != authority["instance_ref"]
        or manifest.state != state
        or manifest.release is None
        or manifest.release.model_dump() != request["release"]
        or manifest.capabilities is None
        or manifest.capabilities.model_dump() != record["desired"]["caps"]
    ):
        raise ValueError("prepared manifest/config differs from reviewed release-upgrade identity")
    return manifest


def prepare(original, frame, head, snapshot, request, *, now):
    """Recompute the exact reviewed transition from current protected inputs."""
    if (
        not isinstance(request, dict)
        or set(request)
        != {
            "expected_revision",
            "host_id",
            "epoch",
            "old_release",
            "config_head",
            "release",
            "profile",
            "config_revision",
            "expires_at",
        }
        or not isinstance(request["release"], dict)
        or set(request["release"]) != {"id", "digest"}
    ):
        raise ValueError("invalid release-upgrade request fields")
    guard.validate_state(original)
    slot, old = selected(original, frame)
    a = old["authority"]
    # Trusted mode (bh-mk97e): authority/config expiry never blocks the reviewed transition.
    expiring = hq_authority_enforce.enforced()
    if (
        original["domain"] != guard.DOMAIN_V2
        or expiring
        and original["expires_at"] <= now
        or now < original["issued_at"]
        or not old["desired"]["declared"]
        or request["expected_revision"] != head
        or request["host_id"] != a["holder_identity"]
        or type(request["epoch"]) is not int
        or request["epoch"] != a["epoch"]
        or request["old_release"] != old["desired"]["release"]["digest"]
        or request["release"] == old["desired"]["release"]
        or request["release"]["digest"] == old["desired"]["release"]["digest"]
        or any(not isinstance(request["release"][key], str) for key in ("id", "digest"))
        or type(request["expires_at"]) not in (int, float)
        or not math.isfinite(request["expires_at"])
        or not now < request["expires_at"] <= now + 86400
        or expiring
        and snapshot.valid_until <= now
        or not isinstance(request["profile"], str)
        or not request["profile"].strip()
        or not isinstance(request["config_revision"], str)
        or not request["config_revision"].strip()
    ):
        raise ValueError("release-upgrade original CAS, authority freshness or target invalid")
    active = slot == "active"
    manifest = manifest_for(snapshot, frame, old, request, state="active" if active else "pending")
    plan_digest = (
        "sha256:"
        + hashlib.sha256(
            canonical(
                {
                    "domain": ACTIVE_DOMAIN if active else PENDING_DOMAIN,
                    "frame": frame,
                    "request": request,
                    "original": original,
                    "config_backend": snapshot.backend_identity,
                    "config_generation": snapshot.generation,
                    "manifest": manifest.model_dump(mode="json", exclude_none=True),
                },
                limit=4 * 1024 * 1024,
            )
        ).hexdigest()
    )
    state = copy.deepcopy(original)
    entry = state["frames"][frame]
    archived = copy.deepcopy(old)
    history = archived.pop("release_upgrade_history", [])
    history.append(
        {
            "authority_revision": head,
            "config_head": snapshot.commit_revision,
            "plan_sha256": plan_digest,
            "record": archived,
        }
    )
    if active:
        # Active frames rotate every release; keep the newest bounded archive. Older
        # evidence stays immutable in SQL under its own epoch; epochs never repeat.
        history = history[-HISTORY_LIMIT:]
    next_record = copy.deepcopy(old)
    next_record.pop("emergency", None)  # Bound to the archived authority; kept there.
    next_record["authority"] = {
        **a,
        "epoch": entry["epoch_floor"] + 1,
        "config_revision": request["config_revision"],
        # An active rotation stays admitted; the request expiry bounds only the plan.
        "candidate_expires_at": None if active else request["expires_at"],
    }
    next_record["desired"] = {
        **old["desired"],
        "release": request["release"],
        "profile": request["profile"],
    }
    next_record["receipt"] = {
        "sequence": 0,
        "sha": "",
        "first_seen": None,
        "consecutive": 0,
        "lease": None,
        "registration": None,
    }
    next_record.update(
        state="active" if active else "pending",
        cordoned=old["cordoned"] if active else False,
        drain_deadline=None,
        release_upgrade_history=history,
    )
    entry[slot] = next_record
    entry["epoch_floor"] = next_record["authority"]["epoch"]
    state.update(
        revision=original["revision"] + 1,
        issued_at=now,
        expires_at=guard.operator_signed_expiry(original, now),
    )
    guard.validate_state(state)
    return state, plan_digest


def route_for(state, frame):
    entry = state["frames"][frame]
    authority = (entry["candidate"] or entry["active"])["authority"]
    principal = sql_principal(ObservationAuthority(**authority))
    return (
        principal,
        frame,
        authority["holder_identity"],
        authority["instance_ref"],
        authority["epoch"],
        authority["key_fingerprint"],
    )


def preserve_history(previous, state):
    """Ordinary operator publications cannot add, drop or rewrite upgrade archives."""
    before = {
        guard.binding(r): r.get("release_upgrade_history") for _, r in guard.records(previous)
    }
    after = {guard.binding(r): r.get("release_upgrade_history") for _, r in guard.records(state)}
    if {key: value for key, value in before.items() if value is not None} != {
        key: value for key, value in after.items() if value is not None
    }:
        raise ValueError("release upgrade history requires the explicit reviewed transition")


def validate_publication(previous, state, head, snapshot, upgrade, route, *, now):
    expected, digest = prepare(
        previous, upgrade["frame"], head, snapshot, upgrade["request"], now=state["issued_at"]
    )
    if (
        digest != upgrade["plan_sha256"]
        or expected != state
        or route != route_for(expected, upgrade["frame"])
        or upgrade["request"]["expires_at"] <= now
        or hq_authority_enforce.enforced()
        and (previous["expires_at"] <= now or snapshot.valid_until <= now)
    ):
        raise ValueError("release-upgrade publication differs from the reviewed plan")


def release_upgrade(
    plane, frame, action="plan", *, request=None, plan_sha256="", operator_key="", confirm=False
):
    operator = plane._operator()
    budget = plane._operator_deadline()
    head, original, _crossref, _policies = operator.load(deadline=budget)
    if action == "check":
        record = (original.get("frames", {}).get(frame) or {}).get("candidate") or (
            original.get("frames", {}).get(frame) or {}
        ).get("active")
        if record is None:
            raise ValueError("no current incarnation to check")
        authority = record["authority"]
        return {
            "revision": head,
            "frame_id": frame,
            "authority": authority,
            "state": record["state"],
            "release": record["desired"]["release"],
            "profile": record["desired"]["profile"],
            "upgraded": bool(record.get("release_upgrade_history")),
            "history": record.get("release_upgrade_history", []),
        }
    if action not in {"plan", "apply"} or not isinstance(request, dict):
        raise ValueError("release-upgrade requires plan, apply or check and reviewed inputs")
    snapshot = plane.config_store().load_snapshot()
    state, digest = prepare(original, frame, head, snapshot, request, now=plane.clock())
    route = route_for(state, frame)
    entry = state["frames"][frame]
    rotated = entry["candidate"] or entry["active"]
    from .hq_sql_runtime_schema import inbox_table

    result = {
        "revision": head,
        "config_head": snapshot.commit_revision,
        "plan_sha256": digest,
        "frame_id": frame,
        "old_epoch": request["epoch"],
        "new_epoch": route[4],
        "principal": route[0],
        "inbox_table": inbox_table(route[0], route[4]),
        "authority": rotated["authority"],
        "release": request["release"],
        "profile": request["profile"],
        "state": rotated["state"],
        "rotation": "active" if state["frames"][frame]["active"] is rotated else "pending",
    }
    if action == "plan":
        return result
    if not confirm or hq_authority_enforce.key_required(operator_key) or plan_sha256 != digest:
        raise ValueError(
            "apply requires --confirm, approved operator key and exact reviewed plan digest"
        )
    result["revision"] = operator.publish(
        state,
        expected_revision=head,
        operator_key=operator_key,
        provisioned_route=route,
        deadline=budget,
        release_upgrade={"frame": frame, "request": request, "plan_sha256": digest},
    )
    return result
