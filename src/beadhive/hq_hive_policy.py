"""Project operator-signed hive requirements from one committed fleet catalog.

The projection is derived only from the canonical fleet document in a verified
snapshot.  Its head and validity are provenance supplied by the config adapter,
never values a frame or a second policy file can nominate.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import replace

from ruamel.yaml import YAML

from . import hq_authority_guard as guard
from .modules.config.contracts import ManagedRepoEntry
from .modules.config.domain.ports import FleetConfigSnapshot


class HivePolicyError(ValueError):
    """The committed catalog cannot authorize a hive policy projection."""


def validate_sql_hive_policies(
    policies, *, config_head: str, now: float, require_fresh: bool = True
) -> None:
    """Validate protected SQL projection without loosening Git's SHA-1 validator."""
    if (
        not isinstance(config_head, str)
        or not re.fullmatch(r"[0-9a-v]{32}", config_head)
        or type(now) not in (int, float)
        or not math.isfinite(now)
        or not isinstance(policies, dict)
    ):
        raise HivePolicyError("invalid SQL hive policy provenance")
    for prefix, item in policies.items():
        if (
            not isinstance(prefix, str)
            or not re.fullmatch(r"[a-z][a-z0-9-]*", prefix)
            or not isinstance(item, dict)
            or set(item)
            != {"config_revision", "config_head", "valid_until", "requires", "evict_after_s"}
            or not isinstance(item["config_revision"], str)
            or not item["config_revision"]
            or item["config_head"] != config_head
            or type(item["valid_until"]) not in (int, float)
            or not math.isfinite(item["valid_until"])
            or require_fresh
            and now >= item["valid_until"]
            or type(item["evict_after_s"]) not in (int, float)
            or not math.isfinite(item["evict_after_s"])
            or item["evict_after_s"] <= 0
            or not isinstance(item["requires"], dict)
        ):
            raise HivePolicyError("invalid SQL hive policy projection")
        try:
            guard.requirements(item["requires"], None)
        except ValueError:
            raise HivePolicyError("invalid SQL hive capability requirements") from None


def project_hive_policies(
    snapshot: FleetConfigSnapshot, *, valid_until: float, now: float | None = None
) -> dict:
    """Build explicit per-hive policy from the verified raw fleet document.

    Entries without ``frame_policy`` confer no frame authority.  The caller signs
    this exact projection with the runtime authority and config cross-reference.
    """
    if not isinstance(snapshot, FleetConfigSnapshot):
        raise HivePolicyError("committed catalog snapshot required")
    at = time.time() if now is None else now
    if type(at) not in (int, float) or not math.isfinite(at) or at >= snapshot.valid_until:
        raise HivePolicyError("committed catalog snapshot expired")
    if type(valid_until) not in (int, float) or not math.isfinite(valid_until) or valid_until <= at:
        raise HivePolicyError("finite future operator policy expiry required")
    if not re.fullmatch(r"[0-9a-v]{32}", snapshot.commit_revision):
        raise HivePolicyError("committed Dolt config head invalid")
    fleet = [document for document in snapshot.documents if document.path == "fleet.yaml"]
    if len(fleet) != 1:
        raise HivePolicyError("one canonical fleet catalog required")
    try:
        parsed = YAML(typ="safe").load(fleet[0].content)
    except Exception:
        raise HivePolicyError("canonical fleet catalog syntax invalid") from None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("managed_repos", []), list):
        raise HivePolicyError("canonical managed hive catalog invalid")
    projected = {}
    seen = set()
    for raw in parsed.get("managed_repos", []):
        try:
            entry = ManagedRepoEntry.model_validate(raw)
        except Exception:
            raise HivePolicyError("canonical managed hive entry invalid") from None
        if not re.fullmatch(r"[a-z][a-z0-9-]*", entry.prefix) or entry.prefix in seen:
            raise HivePolicyError("canonical hive prefix missing, invalid or duplicated")
        seen.add(entry.prefix)
        if entry.kind == "hq":
            if entry.frame_policy is not None:
                raise HivePolicyError("HQ singleton cannot carry frame policy")
            continue
        if entry.frame_policy is None:
            continue
        policy = entry.frame_policy
        try:
            guard.requirements(policy.requires, None)
        except ValueError:
            raise HivePolicyError("canonical hive requirements invalid") from None
        projected[entry.prefix] = {
            "config_revision": policy.config_revision,
            "config_head": snapshot.commit_revision,
            "valid_until": valid_until,
            "requires": policy.requires,
            "evict_after_s": policy.evict_after_s,
        }
    validate_sql_hive_policies(projected, config_head=snapshot.commit_revision, now=at)
    return projected


#: The one config document whose frame-irrelevant edits may move the head without fencing.
FLEET_DOCUMENT = "fleet.yaml"


def _catalog_identity(snapshot: FleetConfigSnapshot) -> tuple:
    """Ordered identity of every managed hive (frame policy or not): who each prefix is."""
    fleet = [document for document in snapshot.documents if document.path == FLEET_DOCUMENT]
    if len(fleet) != 1:
        raise HivePolicyError("one canonical fleet catalog required")
    parsed = YAML(typ="safe").load(fleet[0].content)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("managed_repos", []), list):
        raise HivePolicyError("canonical managed hive catalog invalid")
    identity = []
    for raw in parsed.get("managed_repos", []):
        entry = ManagedRepoEntry.model_validate(raw)
        identity.append(
            (entry.provider, entry.org, entry.repo, entry.prefix, entry.kind, entry.upstream)
        )
    return tuple(identity)


def _fleet_default(snapshot: FleetConfigSnapshot) -> str:
    """The committed ``hq.default_authority_mode`` (frame-relevant, bh-taa04.3)."""
    from .hq_authority_enforce import committed_fleet_default

    return committed_fleet_default(snapshot.documents)


def config_head_tolerated(
    bound: FleetConfigSnapshot,
    current: FleetConfigSnapshot,
    policies,
    *,
    valid_until: float,
    now: float | None = None,
) -> bool:
    """Whether `current` changes nothing a frame enforces relative to the signed `bound` head.

    The SQL authority signs a ``hive_policies`` projection against one config head
    (`bound`, H0). A later head (`current`, H1) is tolerated iff it carries the same
    backend and generation, the same beadyard identity, every document other than
    ``fleet.yaml`` byte-identical (same paths, order and content: ``hosts/*.yaml``,
    ``allowed_signers``, ``beadyard.json`` ...), and its fleet catalog projects to exactly
    the signed `policies` once the projection is rebased onto H0 (so only the per-item
    ``config_head`` provenance may differ). Every ``managed_repos[].frame_policy``, prefix
    and kind therefore still fences, as does ``hq.default_authority_mode`` (bh-taa04.3): a
    signed frame must not learn a fleet default the operator has not re-signed against.

    Pure: the caller proves ancestry (H1 descends from H0) and verifies both snapshots.
    Any doubt is ``False`` (fail closed). The only predicate the frame verifier, receiver
    and authority status surfaces should use to treat a moved head as still bound.
    """
    if not isinstance(bound, FleetConfigSnapshot) or not isinstance(current, FleetConfigSnapshot):
        return False
    if bound.backend_identity != current.backend_identity or bound.generation != current.generation:
        return False

    def rest(snapshot):
        return tuple(
            (document.path, document.content)
            for document in snapshot.documents
            if document.path != FLEET_DOCUMENT
        )

    try:
        if (
            rest(bound) != rest(current)
            or bound.beadyard_id != current.beadyard_id
            or _catalog_identity(bound) != _catalog_identity(current)
            or _fleet_default(bound) != _fleet_default(current)
        ):
            return False
        rebased = replace(current, commit_revision=bound.commit_revision)
        return project_hive_policies(rebased, valid_until=valid_until, now=now) == policies
    except Exception:  # noqa: BLE001 - any doubt about the moved head fails closed
        return False
