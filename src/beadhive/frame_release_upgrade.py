"""Explicit pending-only SQL release rotation, preserving identity and accepted evidence."""

from __future__ import annotations

import copy
import hashlib
import math

from ruamel.yaml import YAML

from . import hq_authority_guard as guard
from .host_manifest_contracts import HostManifest
from .hq_framelease_contracts import ObservationAuthority
from .hq_sql_signatures import canonical, sql_principal


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
            "release-upgrade supports only a pending candidate without an active incarnation; "
            "active/quarantined/emergency frames require a separately reviewed upgrade mechanism"
        )
    return entry["candidate"]


def manifest_for(snapshot, frame, record, request):
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
        or manifest.state != "pending"
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
    old = candidate(original, frame)
    a = old["authority"]
    if (
        original["domain"] != guard.DOMAIN_V2
        or original["expires_at"] <= now
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
        or snapshot.valid_until <= now
        or not isinstance(request["profile"], str)
        or not request["profile"].strip()
        or not isinstance(request["config_revision"], str)
        or not request["config_revision"].strip()
    ):
        raise ValueError("release-upgrade original CAS, authority freshness or target invalid")
    manifest = manifest_for(snapshot, frame, old, request)
    plan_digest = (
        "sha256:"
        + hashlib.sha256(
            canonical(
                {
                    "domain": "beadhive/sql-release-upgrade/v1",
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
    next_record = copy.deepcopy(old)
    next_record["authority"] = {
        **a,
        "epoch": entry["epoch_floor"] + 1,
        "config_revision": request["config_revision"],
        "candidate_expires_at": request["expires_at"],
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
        state="pending", cordoned=False, drain_deadline=None, release_upgrade_history=history
    )
    entry["candidate"] = next_record
    entry["epoch_floor"] = next_record["authority"]["epoch"]
    state.update(revision=original["revision"] + 1, issued_at=now, expires_at=now + 3600)
    guard.validate_state(state)
    return state, plan_digest


def route_for(state, frame):
    authority = state["frames"][frame]["candidate"]["authority"]
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
        or previous["expires_at"] <= now
        or snapshot.valid_until <= now
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
        "authority": state["frames"][frame]["candidate"]["authority"],
        "release": request["release"],
        "profile": request["profile"],
        "state": "pending",
    }
    if action == "plan":
        return result
    if not confirm or not operator_key or plan_sha256 != digest:
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
