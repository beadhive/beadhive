"""Self-placement: may this frame place itself on a hive? (trust-profiles ADR section 2, bh-vl0r3)

The ``place`` capability (:mod:`beadhive.capabilities`) gates ``bh host lease adopt``. Whether a
frame may place *itself* — seed a never-placed placement row, CAS placement to itself and run the
in-data fence bump in one command, with no director credential and no expected-revision
argument — is the fleet trust profile's **placement** knob
(:attr:`beadhive.trust_profiles.ResolvedProfile.self_placement`):

* ``open`` — self-place on a hive with no live writer session; **no director fallback**. A hive
  with a live writer is waited out: the refusal names the holder, when its tenure ends and when
  failover would fire, and prints no director command.
* ``trusted`` (and any profile with ``hq.placement_mode: self`` accepted) — self-place on a hive
  with no live writer session; otherwise the refusal prints the director command.
* ``verified`` / ``attested`` — the capability ceiling removes self-placement from a host
  without the director credential: the adopt refuses and prints the director command.

A **live writer session** is a holder whose tenure has not ended (its lease's ``expires_at``)
and whose session is not stale past its ``failover_after`` — the point at which the director's
failover would fire. With no session evidence for the holder the session is taken as live until
its tenure ends (fail closed: an unobserved writer is never displaced early). A holder that frame
eligibility already finds evictable (quarantined, retired, or authenticated-stale past
``host.lease.evict_after_s``) is not live either, so a profile never makes adopt stricter than
0.24.0 was about such a holder.

**Compatibility.** A host that has learned no fleet profile, and carries no accepted
``hq.placement_mode`` override, keeps 0.24.0's adopt unchanged (:attr:`Decision.mode` is
``legacy``): rollout step 2 ships the profiles with ``verified`` as the default and no behaviour
change for an existing fleet.

The decision itself (:func:`decide`) is pure: every fact — the profile, the placement read, the
holder's session and the clock — is handed in, so it is proven with fakes and a fixed clock.
:func:`evaluate` composes the reads for the CLI. Nothing here writes: the CAS that follows is the
adopt's own (:func:`beadhive.host_adopt.adopt`, pinned to the revision this decision read, so of
two frames self-placing on one hive exactly one wins).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from . import trust_profiles
from .capabilities import PLACE
from .host_lease_contracts import HostLease, _parse_stamp, now_stamp

__all__ = [
    "LEGACY",
    "SELF",
    "DIRECTOR",
    "Decision",
    "Placement",
    "Session",
    "adopt_command",
    "decide",
    "director_command",
    "evaluate",
    "guard_place",
]

#: No profile learned and no placement override: 0.24.0's adopt, unchanged.
LEGACY = "legacy"
#: The profile lets this frame place itself.
SELF = "self"
#: The profile sends placement through the director CAS.
DIRECTOR = "director"


@dataclass(frozen=True)
class Placement:
    """The hive's placement as read once, with the CAS token the adopt must still find."""

    lease: HostLease | None
    #: The placement record's revision (git HQ: the lease ref's blob sha; SQL HQ: the row's
    #: revision). ``""`` when the hive was never placed.
    revision: str = ""


@dataclass(frozen=True)
class Session:
    """The placed frame's writer session as observed now."""

    #: Seconds since the holder's session last renewed; ``None`` when not observed.
    staleness_s: float | None = None
    #: The holder's ``failover_after`` (its role's, or the hive's override); ``None`` when the
    #: holder's role never fails over.
    failover_after_s: float | None = None
    #: Frame eligibility already permits evicting the holder (quarantined, retired, or
    #: authenticated-stale past ``host.lease.evict_after_s``).
    evictable: bool = False

    @property
    def stale(self) -> bool:
        """Stale past ``failover_after``: the director's failover would already have fired."""
        return (
            self.staleness_s is not None
            and self.failover_after_s is not None
            and math.isfinite(self.staleness_s)
            and self.staleness_s > self.failover_after_s
        )


@dataclass(frozen=True)
class Decision:
    """Whether this frame may adopt the hive, and what that adopt is."""

    allowed: bool
    #: :data:`LEGACY`, :data:`SELF` or :data:`DIRECTOR`.
    mode: str
    #: ``seed`` (never placed), ``place`` (released or tenure ended), ``resume`` (already
    #: placed here), ``takeover`` (the holder's session is stale), ``evict`` (the holder is
    #: evictable), ``director`` (this host holds the director credential), ``legacy``, or the
    #: refusal: ``live_writer`` / ``director_only``.
    action: str
    prefix: str
    profile: str
    message: str = ""
    #: The director command a refusal prints (``""`` under ``open``: no director fallback).
    director_command: str = ""
    #: The revision the adopt's CAS must still find (``""``: never placed).
    revision: str = ""
    holder: str = ""
    tenure_ends_at: str = ""
    failover_at: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def takeover(self) -> bool:
        """The adopt displaces a holder whose lease has not expired (its session is stale), so
        it must CAS over an unexpired record."""
        return self.allowed and self.action == "takeover"

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "mode": self.mode,
            "action": self.action,
            "prefix": self.prefix,
            "profile": self.profile,
            "message": self.message,
            "director_command": self.director_command,
            "revision": self.revision,
            "holder": self.holder,
            "tenure_ends_at": self.tenure_ends_at,
            "failover_at": self.failover_at,
        }


def _bh() -> str:
    from . import config

    return config.BINARY_ALIAS


def adopt_command(prefix: str) -> str:
    """The one-step self-placement command."""
    return f"{_bh()} host lease adopt {prefix}"


def director_command(prefix: str, frame: str, revision: str = "") -> str:
    """The director's placement CAS for `frame` on `prefix` (``bh hq placement place``)."""
    command = (
        f"{_bh()} hq placement place {prefix} --frame {frame} "
        f"--expected-revision {revision or '<revision>'} --confirm"
    )
    if revision:
        return command
    return f"{command} (read <revision> with `{_bh()} hq placement show {prefix}`)"


def _explicit(profile: trust_profiles.ResolvedProfile) -> bool:
    """A learned fleet profile, or an accepted ``hq.placement_mode`` override."""
    if profile.source != "default":
        return True
    return any(outcome.override.knob == "placement" for outcome in profile.overrides)


def decide(
    profile: trust_profiles.ResolvedProfile,
    *,
    prefix: str,
    host_id: str,
    frame: str = "",
    placement: Placement,
    session: Session | None = None,
    director_credential: bool = False,
    now: float,
) -> Decision:
    """Decide whether `host_id` may adopt `prefix` under `profile` (module docstring).

    `frame` is this host's frame id for the director command (default: `host_id`). `session` is
    the placed frame's session (``None``: not observed). `now` is the clock the tenure and
    failover times are read against."""
    frame = frame or host_id
    lease, revision = placement.lease, placement.revision
    base = {"prefix": prefix, "profile": profile.name, "revision": revision}
    if not _explicit(profile):
        return Decision(True, LEGACY, "legacy", **base)

    removed = profile.ceiling(operator_key=director_credential, director=director_credential)(
        frozenset()
    )
    if not profile.self_placement:
        command = director_command(prefix, frame, revision)
        if PLACE not in removed and director_credential:
            return Decision(True, DIRECTOR, "director", director_command=command, **base)
        return Decision(
            False,
            DIRECTOR,
            "director_only",
            message=(
                f"✗ {prefix}: self-placement is refused under the {profile.name} trust profile "
                f"(placement {profile.placement}): a frame does not place itself; placement "
                "goes through the director CAS.\n"
                f"  Director: {command}"
            ),
            director_command=command,
            **base,
        )

    if lease is None:
        return Decision(True, SELF, "seed", **base)
    if lease.is_tombstone:
        return Decision(True, SELF, "place", **base)
    if lease.host_id == host_id:
        return Decision(True, SELF, "resume", holder=lease.host_id, **base)
    holder = lease.host_id
    tenure_ends = lease.expires_at
    if lease.is_expired(now):
        return Decision(True, SELF, "place", holder=holder, tenure_ends_at=tenure_ends, **base)
    session = session or Session()
    failover_at = ""
    if session.staleness_s is not None and session.failover_after_s is not None:
        failover_at = now_stamp(now + session.failover_after_s - session.staleness_s)
    if session.stale:
        return Decision(
            True,
            SELF,
            "takeover",
            holder=holder,
            tenure_ends_at=tenure_ends,
            failover_at=failover_at,
            **base,
        )
    if session.evictable:
        return Decision(True, SELF, "evict", holder=holder, tenure_ends_at=tenure_ends, **base)

    if failover_at and _parse_stamp(failover_at) < _parse_stamp(tenure_ends):
        wait = f"failover fires at {failover_at} (tenure ends {tenure_ends})"
    elif failover_at:
        wait = f"its tenure ends at {tenure_ends} (failover would fire at {failover_at})"
    else:
        wait = (
            f"its tenure ends at {tenure_ends} (no stale session observed, so failover "
            "cannot fire before then)"
        )
    lines = [
        f"✗ {prefix}: has a live writer session — held by {lease.describe()}.",
        f"  Under the {profile.name} trust profile a frame self-places only on a hive with no "
        f"live writer session; {wait}.",
    ]
    command = ""
    if profile.director_fallback:
        command = director_command(prefix, frame, revision)
        lines.append(f"  Or have the director place this frame now: {command}")
    else:
        lines.append(
            f"  There is no director fallback under {profile.name}: wait it out, then re-run "
            f"`{adopt_command(prefix)}`."
        )
    return Decision(
        False,
        SELF,
        "live_writer",
        message="\n".join(lines),
        director_command=command,
        holder=holder,
        tenure_ends_at=tenure_ends,
        failover_at=failover_at,
        detail={"staleness_s": session.staleness_s, "failover_after_s": session.failover_after_s},
        **base,
    )


# ---- reads (the CLI's composition) ----------------------------------------------------------


def holder_session(holder: str, *, hq_dir, at: float | None = None) -> Session | None:
    """The placed frame's session as frame eligibility observes it (its verified heartbeat or,
    in dolt-server HQ, its session row), with its role's ``failover_after``. ``None`` when it
    cannot be observed. Never raises."""
    from . import frame_eligibility
    from .failover_policy import failover_after_for
    from .hq_control_plane import control_plane

    try:
        evictable = bool(frame_eligibility.evictable(holder, hq_dir=hq_dir, at=at))
    except Exception:  # noqa: BLE001 - no eviction evidence: not evictable
        evictable = False
    try:
        manifest = control_plane(hq_dir).load_host_manifest(holder)
        failover_after = failover_after_for(getattr(manifest, "roles", None) or manifest.role)
        if not manifest.frame_id:
            return Session(failover_after_s=failover_after, evictable=evictable)
        facts = frame_eligibility.load_facts(manifest, hq_dir=hq_dir, at=at)
        beat = facts.observation
        age = beat.age_seconds
        staleness = (
            float(age)
            if facts.available
            and beat.verified
            and type(age) in (int, float)
            and math.isfinite(age)
            else None
        )
        return Session(staleness, failover_after, evictable)
    except Exception:  # noqa: BLE001 - an unobservable session is reported as unobserved
        return Session(evictable=evictable) if evictable else None


def _frame_id(hq_dir, host_id: str) -> str:
    from .hq_control_plane import control_plane

    try:
        return control_plane(hq_dir).load_host_manifest(host_id).frame_id or host_id
    except Exception:  # noqa: BLE001 - the director command falls back to the host id
        return host_id


def evaluate(
    prefix: str,
    *,
    host_id: str,
    hq_dir,
    cfg=None,
    hq_remote: str = "origin",
    at: float | None = None,
    profile: trust_profiles.ResolvedProfile | None = None,
) -> Decision:
    """:func:`decide` over this host's facts: the resolved trust profile, one placement read,
    the holder's session, the director credential and the clock. A legacy host reads nothing
    but its profile. Raises what the placement read raises (HQ unreachable)."""
    from . import host_lease, hq_authority_enforce, hq_trust_profile

    if profile is None:
        profile = hq_authority_enforce.trust_profile(cfg)
    now = time.time() if at is None else at
    if not _explicit(profile):
        return decide(profile, prefix=prefix, host_id=host_id, placement=Placement(None), now=now)
    sha, lease = host_lease.read_record(hq_remote, prefix, cwd=hq_dir)
    session = None
    if lease is not None and not lease.is_tombstone and lease.host_id != host_id:
        session = holder_session(lease.host_id, hq_dir=hq_dir, at=at)
    return decide(
        profile,
        prefix=prefix,
        host_id=host_id,
        frame=_frame_id(hq_dir, host_id),
        placement=Placement(lease, sha or ""),
        session=session,
        director_credential=hq_trust_profile.holds_operator_key(),
        now=now,
    )


def guard_place(hive: str = "", *, cfg=None, verb: str = "", at: float | None = None) -> Decision:
    """The ``place`` check :func:`beadhive.guard.guard_capability` delegates to, for ``bh host
    lease adopt``: may this host place itself on `hive` under the fleet trust profile?

    Returns the :class:`Decision` (the adopt pins its CAS to the revision it read; a
    ``takeover`` CASes over the stale holder's unexpired record). On refusal echoes the
    decision's message ending with the ``bh hq why`` pointer and raises ``typer.Exit(1)``,
    like every other write guard."""
    from . import config, guard, host, log, registry

    settings = cfg if cfg is not None else config.load()
    entry = registry.resolve_hive(settings, hive) if hive else {}
    prefix = str(entry.get("prefix") or hive)
    try:
        host_id = host.host_id()
    except FileNotFoundError as exc:
        raise guard._refuse(f"✗ {exc}", prefix) from None
    try:
        decision = evaluate(prefix, host_id=host_id, hq_dir=config.hq_dir(), at=at)
    except (ValueError, OSError, RuntimeError) as exc:
        raise guard._refuse(f"✗ {prefix}: placement could not be read ({exc})", prefix) from exc
    if not decision.allowed:
        log.get_logger(__name__).warning(
            "self_placement_refused",
            hive_prefix=prefix,
            host_id=host_id,
            verb=verb,
            profile=decision.profile,
            action=decision.action,
            holder=decision.holder,
        )
        raise guard._refuse(decision.message, prefix)
    return decision


def _register() -> None:
    from . import guard

    guard.register_place_guard(guard_place)


_register()
