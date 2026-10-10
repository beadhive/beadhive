"""Fleet trust profiles: one setting for a fleet's trust posture (trust-profiles ADR section 2).

A **profile** sets five knobs at once — authority, admission, placement, off-primary write
policy and tenure — and, through them, a sixth dimension, revocation latency. It also sets a
**capability ceiling** that removes capabilities from every host regardless of its roles
(:mod:`beadhive.host_roles`). Profiles are ordered by the trust they require of the
environment, least to most: ``open``, ``trusted``, ``verified`` (the default: 0.24.0's
behaviour), ``attested``.

This module is the profile table and the override rule, and nothing else: it is pure, imports
only :mod:`beadhive.capabilities`, and decides from the facts it is handed. Which profile a
host has learned, and which overrides it carries, is read by :mod:`beadhive.hq_trust_profile`.
The beads that enforce a knob read it from :class:`ResolvedProfile`:

* admission — ``ResolvedProfile.admission`` / :attr:`ResolvedProfile.admission_policy`
  (:mod:`beadhive.hq_admission` enforces it, bh-sfp9o.4);
* placement — ``ResolvedProfile.placement`` / :attr:`ResolvedProfile.self_placement`
  (bh-vl0r3);
* write policy — ``ResolvedProfile.write_policy`` (bh-sxhnq);
* tenure — ``ResolvedProfile.tenure`` (:class:`Tenure`, bh-ofszz).

**The override rule.** A host-local override (``hq.placement_mode``, ``hive.write_policy``,
``hq.tenure``) or the fleet's ``hq.admission_policy`` may only move one knob *towards more trust
required* than the profile. An override that would loosen a knob takes effect only when the
operator key signs it (:attr:`Override.signed`); otherwise it is **refused**: the knob keeps
the profile's value and the refusal names the profile (:attr:`OverrideOutcome.message`). So a
key-less HQ writer, or a host editing its own ``host.yaml``, cannot loosen a ``verified``
fleet.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace

from .capabilities import ADMIN, AUTHOR, PLACE

OPEN = "open"
TRUSTED = "trusted"
VERIFIED = "verified"
ATTESTED = "attested"

#: Least to most trust required of the environment (the ADR table's row order).
PROFILES: tuple[str, ...] = (OPEN, TRUSTED, VERIFIED, ATTESTED)

#: ``hq.trust_profile`` absent means ``verified``, which equals 0.24.0's defaults.
DEFAULT = VERIFIED

#: The five knobs a profile sets, in the ADR table's column order.
KNOBS: tuple[str, ...] = ("authority", "admission", "placement", "write_policy", "tenure")

# ---- knob values, each with its rank (higher = more trust required) ---------------------------

#: Authority: whether HQ's authority rows carry signatures and expiry.
AUTHORITY_RANK: dict[str, int] = {
    "unsigned": 0,  # open: nothing signed; any host with HQ write access holds admin
    "trusted": 1,  # unsigned, content honoured (0.24.0 ``trusted``)
    "signed": 2,  # signed, non-expiring (0.24.0 ``signed``)
    "signed-expiring": 3,  # signed with a finite --duration
}

#: Admission: how a registered host becomes ``admitted_active``.
ADMISSION_RANK: dict[str, int] = {
    "registration": 0,  # registration alone admits (hq.admission_policy: open)
    "join": 1,  # bh hq authority join (0.24.0 trusted-fleet open admission)
    "operator-granted": 2,  # an operator grant (hq.admission_policy: manual)
    "granted-attested": 3,  # a grant plus three fresh authenticated heartbeats
}

#: Placement: who may make a frame a hive's writer. ``director`` and ``director-failover`` are
#: the same trust (the director CAS); the failover loop is liveness, not trust.
PLACEMENT_RANK: dict[str, int] = {
    "self": 0,  # self-place on a hive with no live writer session; no director fallback
    "self-or-director": 1,  # self-place when no live writer session, otherwise the director
    "director": 2,  # director CAS only
    "director-failover": 2,  # director CAS, failover loop on
}

#: Off-primary write policy (ADR section 4).
WRITE_POLICY_RANK: dict[str, int] = {"open": 0, "forward": 1, "fenced": 2}

#: Tenure kinds: what bounds how long a placed frame works from its tenure cache.
TENURE_KINDS: tuple[str, ...] = ("ceiling", "failover", "bounded", "ttl")

#: ``open``'s tenure ceiling when ``hq.tenure`` is unset: 24 h, never unbounded.
OPEN_TENURE_CEILING_S = 86400.0

#: The predicate ``attested`` adds to ``author`` (and so to every capability built on it).
FRESH_HEARTBEAT = "authenticated_fresh_heartbeat"


@dataclass(frozen=True)
class Tenure:
    """The tenure knob (ADR section 3); bh-ofszz enforces it.

    ``kind`` names what bounds the cache: ``ceiling`` (``open``: ``ceiling_s`` alone),
    ``failover`` (``trusted``: the hive's ``failover_after``), ``bounded`` (``verified``: the
    shorter of ``hq.tenure`` and ``failover_after``) or ``ttl`` (``attested``: the heartbeat
    TTL, no offline grace). ``ceiling_s`` is ``hq.tenure`` when set (it can only shorten the
    tenure outside ``open``)."""

    kind: str
    ceiling_s: float | None = None

    def seconds(self, failover_after_s: float, heartbeat_ttl_s: float | None = None) -> float:
        """The tenure for a hive with `failover_after_s` (and the frame's heartbeat TTL)."""
        if self.kind == "ceiling":
            return self.ceiling_s if self.ceiling_s is not None else OPEN_TENURE_CEILING_S
        base = (
            heartbeat_ttl_s
            if self.kind == "ttl" and heartbeat_ttl_s is not None
            else failover_after_s
        )
        return base if self.ceiling_s is None else min(base, self.ceiling_s)

    @property
    def offline_grace(self) -> bool:
        """Whether the cache keeps a frame working while HQ is unreachable (not ``attested``)."""
        return self.kind != "ttl"

    def describe(self) -> str:
        bound = "" if self.ceiling_s is None else f", at most {_duration(self.ceiling_s)}"
        return {
            "ceiling": f"hq.tenure ceiling {_duration(self.seconds(0.0))}",
            "failover": f"the hive's failover_after{bound}",
            "bounded": f"min(hq.tenure, failover_after){bound}",
            "ttl": f"heartbeat TTL, no offline grace{bound}",
        }[self.kind]


@dataclass(frozen=True)
class Profile:
    """One row of the ADR section 2 table."""

    name: str
    authority: str
    admission: str
    placement: str
    write_policy: str
    tenure: Tenure
    #: Revocation latency: not a knob, it follows from how the tenure cache refreshes.
    revocation_latency: str
    #: Whether the ceiling removes ``admin`` (without the operator key) and self-placement.
    restricts: bool
    #: Capabilities that continue after tenure lapses (``publish`` and ``place`` never do).
    after_lapse: tuple[str, ...]
    #: Extra frame predicates per capability (``attested``: ``author`` needs a fresh heartbeat).
    capability_adds: tuple[tuple[str, tuple[str, ...]], ...] = ()


#: The ADR section 2 table, verbatim.
TABLE: dict[str, Profile] = {
    profile.name: profile
    for profile in (
        Profile(
            OPEN,
            authority="unsigned",
            admission="registration",
            placement="self",
            write_policy="open",
            tenure=Tenure("ceiling"),
            revocation_latency="next successful refresh",
            restricts=False,
            after_lapse=(AUTHOR, "claim"),
        ),
        Profile(
            TRUSTED,
            authority="trusted",
            admission="join",
            placement="self-or-director",
            write_policy="forward",
            tenure=Tenure("failover"),
            revocation_latency="one refresh interval",
            restricts=False,
            after_lapse=(AUTHOR, "claim"),
        ),
        Profile(
            VERIFIED,
            authority="signed",
            admission="operator-granted",
            placement="director",
            write_policy="forward",
            tenure=Tenure("bounded"),
            revocation_latency="one refresh interval",
            restricts=True,
            after_lapse=(),
        ),
        Profile(
            ATTESTED,
            authority="signed-expiring",
            admission="granted-attested",
            placement="director-failover",
            write_policy="forward",
            tenure=Tenure("ttl"),
            revocation_latency="immediate",
            restricts=True,
            after_lapse=(),
            capability_adds=((AUTHOR, (FRESH_HEARTBEAT,)),),
        ),
    )
}

#: Each knob's rank table (tenure ranks by its effective ceiling, see :func:`_tenure_rank`).
_RANKS: dict[str, dict[str, int]] = {
    "authority": AUTHORITY_RANK,
    "admission": ADMISSION_RANK,
    "placement": PLACEMENT_RANK,
    "write_policy": WRITE_POLICY_RANK,
}

#: Override keys and the knob each moves. ``hq.placement_mode`` speaks the ADR's two values.
OVERRIDE_KEYS: dict[str, str] = {
    "hq.placement_mode": "placement",
    "hive.write_policy": "write_policy",
    "hq.tenure": "tenure",
    "hq.default_authority_mode": "authority",
    "hq.authority_mode": "authority",
    "hq.admission_policy": "admission",
}
#: ``hq.admission_policy`` speaks two values: ``open`` (registration alone admits) and
#: ``manual`` (an operator grant admits; under ``attested`` the grant still needs its fresh
#: heartbeats, so ``manual`` never loosens ``attested``, see :func:`_requested`).
ADMISSION_POLICIES: dict[str, str] = {"open": "registration", "manual": "operator-granted"}
PLACEMENT_MODES: dict[str, str] = {"self": "self-or-director", "director": "director"}
AUTHORITY_MODES: dict[str, str] = {"trusted": "trusted", "signed": "signed"}


class UnknownProfile(ValueError):
    """A profile name outside :data:`PROFILES`."""


def get(name: str) -> Profile:
    try:
        return TABLE[name]
    except KeyError:
        raise UnknownProfile(
            f"unknown trust profile {name!r} (known: {', '.join(PROFILES)})"
        ) from None


def rank(name: str) -> int:
    """`name`'s position on the spectrum: higher requires more trust of the environment."""
    get(name)
    return PROFILES.index(name)


def looser(candidate: str, than: str) -> bool:
    """Whether `candidate` requires less trust than `than` (moving towards ``open``)."""
    return rank(candidate) < rank(than)


def authority_mode(authority: str) -> str:
    """The 0.24.0 authority mode a profile's authority column runs: ``trusted`` or ``signed``."""
    return "trusted" if AUTHORITY_RANK[authority] < AUTHORITY_RANK["signed"] else "signed"


@dataclass(frozen=True)
class Override:
    """One per-knob override as read from its source."""

    knob: str
    key: str
    value: object
    #: Where it was read (``host.yaml``, ``fleet.yaml``, ``env``).
    source: str
    #: The operator key signed it: it may loosen the knob.
    signed: bool = False
    #: A frame-local pin with its own 0.24.0 semantics (``hq.authority_mode``): reported, and
    #: never refused by the direction rule.
    pin: bool = False


@dataclass(frozen=True)
class OverrideOutcome:
    override: Override
    #: ``tightened`` | ``unchanged`` | ``loosened`` (signed or a pin) | ``refused``.
    effect: str
    #: The knob value the override asked for, normalised to the knob's vocabulary.
    requested: object
    message: str = ""

    @property
    def accepted(self) -> bool:
        return self.effect != "refused"


def _duration(seconds: float) -> str:
    seconds = float(seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{int(seconds // size)}{unit}"
    return f"{seconds:g}s"


def _tenure_rank(tenure: Tenure) -> float:
    """A tenure ranks by how long it can run unrefreshed: shorter requires more trust. Outside
    ``open`` an unset ``hq.tenure`` leaves the hive's own bound, so any ceiling tightens it."""
    if tenure.kind == "ceiling":
        return -tenure.seconds(0.0)
    return float("-inf") if tenure.ceiling_s is None else -tenure.ceiling_s


@dataclass(frozen=True)
class ResolvedProfile:
    """A profile with its overrides applied: what every knob reads on this host."""

    profile: Profile
    #: ``default`` (nothing learned: ``verified``) or ``fleet`` (learned from HQ).
    source: str = "default"
    authority: str = ""
    admission: str = ""
    placement: str = ""
    write_policy: str = ""
    tenure: Tenure = field(default_factory=lambda: Tenure("bounded"))
    outcomes: tuple[OverrideOutcome, ...] = ()

    @property
    def name(self) -> str:
        return self.profile.name

    @property
    def knobs(self) -> dict[str, object]:
        return {knob: getattr(self, knob) for knob in KNOBS}

    @property
    def authority_mode(self) -> str:
        """The effective authority column as a 0.24.0 mode (``signed`` / ``trusted``)."""
        return authority_mode(self.authority)

    @property
    def admission_policy(self) -> str:
        """``hq.admission_policy`` as the profile sets it: ``open`` or ``manual``."""
        return "open" if self.admission == "registration" else "manual"

    @property
    def self_placement(self) -> bool:
        """Whether a frame may place itself (``bh host lease adopt`` without the director)."""
        return PLACEMENT_RANK[self.placement] < PLACEMENT_RANK["director"]

    @property
    def director_fallback(self) -> bool:
        """Whether a hive with a live writer can be placed through the director (not ``open``)."""
        return self.placement != "self"

    @property
    def failover_loop(self) -> bool:
        return self.placement == "director-failover"

    @property
    def revocation_latency(self) -> str:
        return self.profile.revocation_latency

    @property
    def lab_posture(self) -> bool:
        """``open``: unsigned, and any host with HQ write access holds ``admin``."""
        return self.authority == "unsigned"

    @property
    def overrides(self) -> tuple[OverrideOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.accepted)

    @property
    def refusals(self) -> tuple[OverrideOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if not outcome.accepted)

    def capability_adds(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Extra frame predicates per capability (``attested``: ``author`` needs a fresh
        authenticated heartbeat; every capability that builds on ``author`` inherits it)."""
        return self.profile.capability_adds

    def ceiling(
        self, *, operator_key: bool, director: bool
    ) -> Callable[[frozenset[str]], frozenset[str]]:
        """The capability ceiling for a host (a :data:`beadhive.host_roles.Ceiling`).

        ``open`` and ``trusted`` remove nothing. ``verified`` and ``attested`` remove ``admin``
        from a host without the operator key, and remove self-placement: ``place`` stays only
        with a host holding the director credential, so placement goes through the director
        CAS."""
        removed: set[str] = set()
        if self.profile.restricts:
            if not operator_key:
                removed.add(ADMIN)
            if not director:
                removed.add(PLACE)
        frozen = frozenset(removed)

        def ceiling(_capabilities: frozenset[str]) -> frozenset[str]:
            return frozen

        return ceiling

    def grants(self, *, hq_write_access: bool) -> frozenset[str]:
        """Capabilities the profile adds regardless of roles: under ``open`` any host with HQ
        write access holds ``admin`` (the single-operator lab posture)."""
        return frozenset({ADMIN}) if self.lab_posture and hq_write_access else frozenset()

    def continues_after_lapse(self, capability: str) -> bool:
        """Whether `capability` keeps working once tenure lapses unrefreshed (ADR section 3)."""
        return capability in self.profile.after_lapse

    def doctor_line(self) -> str:
        """``profile trusted; placement overridden to director (host.yaml)``: the effective
        profile and every override on one line."""
        parts = [f"profile {self.name}" + (" (default)" if self.source == "default" else "")]
        for outcome in self.outcomes:
            override = outcome.override
            if outcome.accepted:
                value = (
                    outcome.requested.describe()
                    if isinstance(outcome.requested, Tenure)
                    else outcome.requested
                )
                verb = "pinned" if override.pin else "overridden"
                parts.append(
                    f"{override.knob} {verb} to {value} ({override.key}, {override.source})"
                )
            else:
                parts.append(
                    f"{override.knob} override refused ({override.key}={override.value}, "
                    f"{override.source})"
                )
        return "; ".join(parts)

    def as_dict(self) -> dict:
        return {
            "profile": self.name,
            "source": self.source,
            "knobs": {
                "authority": self.authority,
                "admission": self.admission,
                "placement": self.placement,
                "write_policy": self.write_policy,
                "tenure": {"kind": self.tenure.kind, "ceiling_s": self.tenure.ceiling_s},
            },
            "authority_mode": self.authority_mode,
            "admission_policy": self.admission_policy,
            "revocation_latency": self.revocation_latency,
            "lab_posture": self.lab_posture,
            "overrides": [
                {
                    "knob": outcome.override.knob,
                    "key": outcome.override.key,
                    "value": outcome.override.value,
                    "source": outcome.override.source,
                    "signed": outcome.override.signed,
                    "effect": outcome.effect,
                    **({"message": outcome.message} if outcome.message else {}),
                }
                for outcome in self.outcomes
            ],
            "line": self.doctor_line(),
        }


def _requested(profile: Profile, override: Override):
    """`override`'s value in its knob's vocabulary (raises ValueError when it is invalid)."""
    value = override.value
    if override.knob == "tenure":
        if isinstance(value, bool) or not isinstance(value, int | float) or not value > 0:
            raise ValueError(f"{override.key} must be a positive number of seconds")
        if profile.tenure.kind == "ceiling":
            return Tenure("ceiling", float(value))
        return replace(profile.tenure, ceiling_s=float(value))
    if override.knob == "admission" and value == "manual":
        # ``manual`` asks for an operator grant: a profile that already requires one (and, under
        # ``attested``, its fresh heartbeats) keeps its own value rather than reading it as a
        # loosening.
        granted = ADMISSION_POLICIES["manual"]
        return max(granted, profile.admission, key=ADMISSION_RANK.__getitem__)
    vocabulary = {
        "admission": ADMISSION_POLICIES,
        "placement": PLACEMENT_MODES,
        "authority": AUTHORITY_MODES,
        "write_policy": {name: name for name in WRITE_POLICY_RANK},
    }.get(override.knob)
    if vocabulary is None or value not in vocabulary:
        allowed = ", ".join(vocabulary or ())
        raise ValueError(f"{override.key} must be one of {allowed} (got {value!r})")
    return vocabulary[value]


def _compare(knob: str, current, requested) -> int:
    """``>0`` when `requested` requires more trust than `current`, ``0`` equal, ``<0`` looser."""
    if knob == "tenure":
        delta = _tenure_rank(requested) - _tenure_rank(current)
    else:
        delta = _RANKS[knob][requested] - _RANKS[knob][current]
    return (delta > 0) - (delta < 0)


def resolve(
    name: str | None = None, overrides: Iterable[Override] = (), *, source: str | None = None
) -> ResolvedProfile:
    """`name` (``None``: :data:`DEFAULT`) with `overrides` applied in order.

    An override that requires more trust is applied; one that is equal changes nothing; one
    that loosens is applied only when signed (or a frame-local pin), else refused with a
    message naming the profile and the knob keeps its value."""
    profile = get(name or DEFAULT)
    current = {knob: getattr(profile, knob) for knob in KNOBS}
    outcomes = []
    for override in overrides:
        try:
            requested = _requested(profile, override)
        except ValueError as exc:
            outcomes.append(OverrideOutcome(override, "refused", override.value, str(exc)))
            continue
        direction = _compare(override.knob, current[override.knob], requested)
        if direction > 0:
            effect = "tightened"
        elif direction == 0:
            effect = "unchanged"
        elif override.signed or override.pin:
            effect = "loosened"
        else:
            outcomes.append(
                OverrideOutcome(
                    override,
                    "refused",
                    requested,
                    f"{override.key}: {override.value} ({override.source}) would loosen "
                    f"{override.knob} below the {profile.name} trust profile; a per-knob "
                    "override may only require more trust unless the operator key signs it — "
                    f"refused, {override.knob} stays at the {profile.name} profile's value",
                )
            )
            continue
        if effect != "unchanged":
            current[override.knob] = requested
        outcomes.append(OverrideOutcome(override, effect, requested))
    return ResolvedProfile(
        profile,
        source or ("default" if name is None else "fleet"),
        outcomes=tuple(outcomes),
        **current,
    )
