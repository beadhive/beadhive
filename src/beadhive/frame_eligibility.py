"""Frame intake policy and protected HQ composition; legacy hosts retain their lease policy."""

from __future__ import annotations

import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import hosts
from .host_heartbeat_core import VerifiedObservation


class EligibilityError(ValueError):
    """A frame cannot accept new work under the selected authority."""


# Transitional operator escape while the heartbeat/receiver model is removed (bh-qlgmm):
# ``advisory`` keeps publishing and verifying heartbeats but stops a stale or missing beat
# from fencing the frame. Every other eligibility predicate still fails closed.
HEARTBEAT_ENV = "BH_FRAME_HEARTBEAT"
HEARTBEAT_MODES = ("required", "advisory")


def heartbeat_mode() -> str:
    raw = os.environ.get(HEARTBEAT_ENV, "required").strip() or "required"
    if raw not in HEARTBEAT_MODES:
        raise EligibilityError(
            f"{HEARTBEAT_ENV} must be 'required' or 'advisory' (got {raw[:32]!r})"
        )
    return raw


@dataclass(frozen=True)
class EligibilityFacts:
    observation: VerifiedObservation
    desired: dict
    dispatch_enabled: bool = True
    available: bool = True
    at: float | None = None
    heartbeat_advisory: bool = False


@dataclass(frozen=True)
class EligibilityDecision:
    predicates: tuple[tuple[str, bool], ...]

    @property
    def allowed(self):
        return all(value for _, value in self.predicates)

    @property
    def reason(self):
        return ", ".join(name for name, value in self.predicates if not value) or "eligible"

    def as_dict(self):
        return {
            "eligible": self.allowed,
            "predicates": dict(self.predicates),
            "reason": self.reason,
        }


def eligible(frame: hosts.HostManifest, hive: dict, facts: EligibilityFacts) -> EligibilityDecision:
    """Candidate eligibility, independent of current hive-primary lease ownership.

    Heartbeat TTL is exclusive, matching the authenticated observer's existing boundary.
    Desired state comes from protected HQ, never the mutable inventory's lifecycle fields.
    """
    beat, desired = facts.observation, facts.desired
    lease = beat.lease
    age = beat.age_seconds
    caps = frame.capabilities.model_dump() if frame.capabilities else {}
    requires = hive.get("requires", {}) if isinstance(hive, dict) else None
    compatible = isinstance(requires, dict)
    if compatible:
        for key, value in requires.items():
            if key == "harnesses":
                match = (
                    isinstance(value, list)
                    and all(isinstance(item, str) for item in value)
                    and all(item in caps.get("harnesses", []) for item in value)
                )
            elif key == "harness":
                match = isinstance(value, str) and value in caps.get("harnesses", [])
            elif key == "max_sessions":
                match = type(value) is int and value > 0 and caps.get(key, 0) >= value
            else:
                match = (
                    key in {"isolation", "trust_zone", "arch"}
                    and type(value) is str
                    and caps.get(key) == value
                )
            compatible = compatible and match
    fresh = (
        beat.verified
        and beat.fresh
        and not beat.candidate
        and lease is not None
        and type(age) in (int, float)
        and math.isfinite(age)
        and 0 <= age < lease.leaseDurationSeconds
    )
    from . import hq_authority_guard as guard

    record = {
        "desired": desired,
        "authority": desired.get("authority", {}),
        "state": desired.get("state"),
        "cordoned": desired.get("cordoned"),
    }
    if "emergency" in desired:
        record["emergency"] = desired["emergency"]
    from .frame_emergency import locally_active

    emergency = locally_active(
        record, hive.get("prefix"), facts.at if facts.at is not None else time.time()
    )
    trusted_emergency = (
        emergency
        and facts.available
        and beat.verified
        and lease is not None
        and not beat.candidate
        and type(age) in (int, float)
        and math.isfinite(age)
        and age >= 0
        and lease.key_id == record["authority"].get("key_fingerprint")
        and lease.epoch == record["authority"].get("epoch")
        and lease.audience == record["authority"].get("audience")
        and lease.config_revision == record["authority"].get("config_revision")
    )
    waived = not fresh and not trusted_emergency and facts.heartbeat_advisory
    if waived:
        print(
            f"⚠ {HEARTBEAT_ENV}=advisory: frame {frame.frame_id} heartbeat is not fresh "
            f"(status {beat.status!r}, age {age!r}s) — waived, not fenced",
            file=sys.stderr,
        )
    if trusted_emergency:
        from .frame_emergency import audit

        audit("eligibility-use-attempt", record, prefix=hive.get("prefix"))
    return EligibilityDecision(
        (
            ("authority_available", facts.available),
            (
                "admitted_active",
                desired.get("state") == "active" and desired.get("declared") is True,
            ),
            ("not_cordoned", desired.get("cordoned") is False),
            (
                "reviewed_admission_or_emergency",
                not guard.emergency_review_required(record) or bool(trusted_emergency),
            ),
            (
                "current_frame_incarnation",
                lease is not None
                and lease.frame_id == frame.frame_id
                and lease.holderIdentity == frame.host_id
                and lease.instance_ref == frame.instance_ref
                and desired.get("authority", {}).get("holder_identity") == frame.host_id
                and desired.get("authority", {}).get("instance_ref") == frame.instance_ref,
            ),
            (
                "beadyard_binding",
                lease is not None
                and frame.beadyard_id
                == lease.beadyard_id
                == desired.get("authority", {}).get("beadyard_id"),
            ),
            ("authenticated_fresh_heartbeat", bool(fresh or trusted_emergency or waived)),
            (
                "release_matches",
                lease is not None
                and frame.release is not None
                and lease.release.model_dump()
                == frame.release.model_dump()
                == desired.get("release"),
            ),
            (
                "conformance_pass",
                bool(trusted_emergency)
                or lease is not None
                and lease.conformance.status == "conformant"
                and lease.conformance.profile == desired.get("profile")
                and all(check.status != "fail" for check in lease.conformance.checks),
            ),
            ("capabilities_match_admission", bool(caps) and caps == desired.get("caps")),
            ("hive_requirements", compatible),
            ("executor_role", frame.role != "viewer"),
            (
                "available_capacity",
                type(caps.get("max_sessions")) is int and caps["max_sessions"] > 0,
            ),
            ("dispatch_enabled", facts.dispatch_enabled is True),
        )
    )


def load_facts(frame, *, hq_dir, cfg=None, at=None):
    """Read through the selected provider, rejecting a changed policy around observation."""
    from . import config
    from .hq_control_plane import control_plane

    settings = cfg if cfg is not None else config.load_host()
    enabled = settings.get("host", {}).get("dispatch", {}).get("enabled", True)
    if at is not None and (type(at) not in (int, float) or not math.isfinite(at)):
        return EligibilityFacts(VerifiedObservation("invalid-clock"), {}, enabled, False)
    try:
        plane = control_plane(hq_dir)
        _revision, desired, observation = plane.read_eligibility(frame, now=at)
        return EligibilityFacts(
            observation, desired, enabled, True, at, heartbeat_mode() == "advisory"
        )
    except (ValueError, OSError, RuntimeError):
        return EligibilityFacts(VerifiedObservation("authority-unavailable"), {}, enabled, False)


def decision_for(host_id, hive=None, *, hq_dir=None, cfg=None, at=None):
    """None denotes an unbound legacy host; a declared binding always fails closed."""
    from . import config
    from .hq_control_plane import control_plane

    try:
        bootstrap = config.load_host()
    except FileNotFoundError:
        # Historical raw lease recovery has no host config. A declared frame manifest below
        # still enters protected authority checks; malformed/unreadable config never falls back.
        bootstrap = {}
        missing_host_config = True
    else:
        missing_host_config = False
    settings = cfg if cfg is not None else bootstrap
    root = Path(hq_dir) if hq_dir is not None else config.hq_dir()
    binding = bootstrap.get("hq", {})
    # Configuration storage alone does not enroll a legacy executor as a frame.
    enrolled = bool(bootstrap.get("host", {}).get("frame_id"))
    if anchor := binding.get("authority_anchor"):
        try:
            # Operator/observer bootstrap access does not enroll an executor.
            from .hq_control_plane import verified_anchor_role

            role = verified_anchor_role(root, anchor)
            enrolled = enrolled or role not in {"operator", "observer"}
        except (OSError, ValueError, AttributeError):
            enrolled = True
    plane = None
    try:
        # Raw legacy recovery can have a Git manifest before host config exists.
        # Once HOST exists, selecting the plane must succeed before absence can
        # count as a legacy host.
        if not missing_host_config:
            plane = control_plane(root)
        frame = hosts.load(root, host_id) if plane is None else plane.load_host_manifest(host_id)
    except FileNotFoundError as exc:
        if plane is not None and getattr(plane, "config_backend", None) == "sql":
            if not plane.verified_manifest_absence(exc):
                return EligibilityDecision((("authority_available", False),))
        elif plane is None and not missing_host_config:
            return EligibilityDecision((("authority_available", False),))
        if not enrolled:
            return None
        return EligibilityDecision((("authority_available", False),))
    except (ValueError, OSError, RuntimeError):
        return EligibilityDecision((("authority_available", False),))
    if not frame.frame_id:
        if not enrolled:
            return None
        return EligibilityDecision((("authority_available", False),))
    # A frame's HOST bootstrap pin is independent of whichever HQ remote is
    # selected today. Without this equality a same-named foreign HQ whose own
    # signed documents agree with each other could replace the whole read.
    if binding.get("beadyard_id") != frame.beadyard_id:
        return EligibilityDecision((("beadyard_binding", False),))
    return eligible(frame, hive or {}, load_facts(frame, hq_dir=root, cfg=settings, at=at))


def require_eligible(host_id, hive=None, **kwargs):
    decision = decision_for(host_id, hive, **kwargs)
    if decision is not None and hive and set(hive) == {"prefix"}:
        from . import config, registry

        try:
            entry = registry.resolve_hive(config.load(), hive["prefix"])
        except (ValueError, KeyError, FileNotFoundError):
            raise EligibilityError("frame ineligible: hive_catalog_available") from None
        decision = decision_for(host_id, entry, **kwargs)
    if decision is not None and not decision.allowed:
        raise EligibilityError(f"frame {host_id} ineligible: {decision.reason}")
    return decision


def require_local(hive="", *, cfg=None, hive_dir=None):
    from . import config, host, registry

    try:
        identity = host.host_id()
    except FileNotFoundError:
        identity = ""
    settings = cfg if cfg is not None else config.load()
    entry = None
    try:
        directory = hive_dir if hive_dir is not None else registry.hive_dir_for(settings, hive)
        entry = registry.entry_for_dir(settings, directory)
    except (ValueError, KeyError, FileNotFoundError, RuntimeError):
        pass
    decision = require_eligible(identity, entry or {}, cfg=settings)
    if decision is not None and entry is None:
        raise EligibilityError("frame ineligible: hive_catalog_available")
    return decision


def evictable(host_id, *, hq_dir, at=None, evict_after_s=None):
    """Only authenticated stale evidence or protected quarantine/retirement permits eviction.

    Unavailable authority, parked/draining state, drift or a dispatch preference cannot
    justify seizing a live incumbent's lease. Retirement has no watch-state by design.
    """
    from . import config
    from .hq_control_plane import control_plane

    if evict_after_s is None:
        evict_after_s = (
            config.load_host().get("host", {}).get("lease", {}).get("evict_after_s", 900)
        )
    if (
        type(evict_after_s) not in (int, float)
        or not math.isfinite(evict_after_s)
        or evict_after_s <= 0
    ):
        return False
    if at is not None and (type(at) not in (int, float) or not math.isfinite(at)):
        return False
    try:
        plane = control_plane(hq_dir)
        status = plane.eligibility_authority_status()
        # Public status carries incarnation state and identity; no local mtime authority.
        expiry = status.get("state", {}).get("expires_at")
        clock = time.time() if at is None else at
        if (
            status.get("authority_ready") is not True
            or type(expiry) not in (int, float)
            or not math.isfinite(expiry)
            or clock >= expiry
        ):
            return False
        records = status.get("state", {}).get("frames", {})
        matching = [
            record
            for entry in records.values()
            for record in (entry.get("active"), entry.get("candidate"), *entry.get("retired", []))
            if record and record.get("authority", {}).get("holder_identity") == host_id
        ]
        current = [record for record in matching if record.get("state") != "retired"]
        if len(current) > 1:
            return False
        if current:
            if current[0].get("state") == "quarantined":
                return True
            from .hq_authority_guard import emergency_active

            scope = current[0].get("emergency", {}).get("prefix")
            if emergency_active(current[0], scope, clock):
                return False
        elif len(matching) == 1 and matching[0].get("state") == "retired":
            return True
    except (ValueError, OSError, RuntimeError):
        pass
    try:
        frame = control_plane(hq_dir).load_host_manifest(host_id)
        if not frame.frame_id:
            return False
        facts = load_facts(frame, hq_dir=hq_dir, at=at)
        beat = facts.observation
        return (
            facts.available
            and beat.status == "stale"
            and beat.verified
            and not beat.candidate
            and type(beat.age_seconds) in (int, float)
            and math.isfinite(beat.age_seconds)
            and beat.age_seconds > evict_after_s
        )
    except (ValueError, OSError, RuntimeError):
        return False


def local_intake_decision(hive="", *, cfg=None, hive_dir=None, legacy_primary=None):
    """Public dispatch adapter seam: candidate policy plus live hive lease ownership."""
    try:
        decision = require_local(hive, cfg=cfg, hive_dir=hive_dir)
        if decision is None and legacy_primary is None:
            return EligibilityDecision((("legacy_primary_reader_available", False),))
        primary = (
            authoritative_primary(hive, cfg=cfg, hive_dir=hive_dir)
            if decision is not None
            else legacy_primary(hive, cfg=cfg, hive_dir=hive_dir)
        )
        held = primary is not None and primary[2] is not None and primary[2].held_by(primary[1])
        if decision is None:
            return EligibilityDecision((("legacy_primary", primary is None or held),))
        return EligibilityDecision((*decision.predicates, ("current_hive_lease_holder", held)))
    except EligibilityError as exc:
        return EligibilityDecision(((str(exc), False),))


def authoritative_primary(hive="", *, cfg=None, hive_dir=None):
    """Frame operation ownership uses current remote lease, never unsigned cache."""
    from . import config, host, registry
    from .hq_control_plane import control_plane

    settings = cfg if cfg is not None else config.load()
    directory = hive_dir if hive_dir is not None else registry.hive_dir_for(settings, hive)
    entry = registry.entry_for_dir(settings, directory)
    if not entry or not entry.get("prefix"):
        raise EligibilityError("frame ineligible: hive_catalog_available")
    try:
        lease = control_plane(config.hq_dir()).read_hive_lease(
            str(entry["prefix"]), holder_identity=host.host_id()
        )
    except (ValueError, OSError, RuntimeError) as exc:
        raise EligibilityError("frame ineligible: authoritative_hive_lease_available") from exc
    return str(entry["prefix"]), host.host_id(), lease


def incumbent_primary(hive="", *, cfg=None, hive_dir=None):
    """Read a bound incumbent lease for finishing work, without new-intake policy.

    Draining and heartbeat staleness stop new claims, but do not change the
    protected lease generation on which an existing claim was minted.
    """
    from . import config, host, registry
    from .hq_control_plane import control_plane

    settings = cfg if cfg is not None else config.load()
    if hive and hive_dir is None:
        entry = registry.resolve_hive(settings, hive)
    else:
        directory = hive_dir if hive_dir is not None else registry.hive_dir_for(settings, hive)
        entry = registry.entry_for_dir(settings, directory)
    if not entry or not entry.get("prefix"):
        raise EligibilityError("frame ineligible: hive_catalog_available")
    identity = host.host_id()
    plane = control_plane(config.hq_dir())
    if getattr(plane, "config_backend", None) != "sql":
        raise EligibilityError("frame ineligible: AUTHORITY_NOT_READY")
    try:
        _revision, lease = plane.read_hive_lease_record(
            str(entry["prefix"]), incumbent_identity=identity
        )
    except (ValueError, OSError, RuntimeError) as exc:
        raise EligibilityError("frame ineligible: authoritative_hive_lease_available") from exc
    if lease is None:
        raise EligibilityError("frame ineligible: current_hive_lease_holder")
    return str(entry["prefix"]), identity, lease


def require_intake(hive="", *, cfg=None, hive_dir=None):
    from .hq_authority_expiry import warn_if_expiring

    warn_if_expiring()
    decision = require_local(hive, cfg=cfg, hive_dir=hive_dir)
    if decision is not None:
        _prefix, identity, lease = authoritative_primary(hive, cfg=cfg, hive_dir=hive_dir)
        if lease is None or not lease.held_by(identity):
            raise EligibilityError("frame ineligible: current_hive_lease_holder")
        # Ownership reads cannot mask a concurrent admission or receipt revocation.
        decision = require_local(hive, cfg=cfg, hive_dir=hive_dir)
    return decision


class GuardedClaimSession:
    """Recheck immediately at each API write, after candidate reads/negotiation."""

    def __init__(self, session, main):
        self.session, self.main = session, main

    def __getattr__(self, name):
        return getattr(self.session, name)

    def claim_issue(self, *args, **kwargs):
        require_intake(hive_dir=self.main)
        return self.session.claim_issue(*args, **kwargs)

    def claim_next(self, *args, **kwargs):
        require_intake(hive_dir=self.main)
        return self.session.claim_next(*args, **kwargs)
