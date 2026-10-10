"""Admission per trust profile (trust-profiles ADR section 2, admission column; bh-sfp9o.4).

Admission is how a registered host becomes ``admitted_active``, the predicate every capability
consults. The fleet trust profile sets the rule (:attr:`beadhive.trust_profiles.ResolvedProfile
.admission`, which already carries a ``hq.admission_policy`` override) and this module enforces
it:

* ``open`` (``registration``) — registering admits: the frame's own registration writes its
  incarnation ACTIVE (:func:`beadhive.hq._open_admission`), with no join or grant step;
* ``trusted`` (``join``) — admitted through ``bh hq authority join``, which a registering frame
  runs itself, exactly 0.24.0's trusted-fleet auto-admit;
* ``verified`` (``operator-granted``) — an operator grant (``bh hq authority grant`` and the
  lifecycle admit); join is refused, so an ungranted host stays not admitted;
* ``attested`` (``granted-attested``) — the grant, and then :data:`ATTESTATION_BEATS` fresh
  authenticated heartbeats before ``admitted_active`` holds. A stale beat restarts the count
  (:func:`beadhive.host_heartbeat_core.next_streak`); an unauthenticated or stale latest beat
  attests nothing (:func:`attested_beats`). Carriers that record no streak (the SQL session
  and inbox carriers) fail closed under ``attested``.

**0.24.0 compatibility.** A fleet that set ``hq.default_authority_mode: trusted`` but no
``hq.trust_profile`` resolves the default ``verified`` profile with its authority column
overridden; it keeps 0.24.0's join auto-admit (:func:`join_permitted`). Naming a profile is what
opts a fleet into that profile's admission rule.

**The attestation count** is not kept by any reader. It is advanced where each beat lands: the
heartbeat publisher computes the new beat's streak from the beat it replaces
(:func:`beadhive.host_heartbeat_core.next_streak`, the clock being the beats' own
``observed_at``) and records it in the signed beat commit; eligibility reads it statelessly
(:func:`attested_beats`), so a frame read once after three good beats is admitted.

This module imports only :mod:`beadhive.trust_profiles`.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import trust_profiles
from .trust_profiles import ADMISSION_RANK

#: Fresh authenticated heartbeats an ``attested`` grant needs before it is ``admitted_active``.
ATTESTATION_BEATS = 3

_DESCRIPTIONS: dict[str, str] = {
    "registration": "registration admits (no join or grant step)",
    "join": "admitted through `bh hq authority join` (a registering frame joins itself)",
    "operator-granted": "an operator grant admits (`bh hq authority grant`, lifecycle admit)",
    "granted-attested": (
        f"an operator grant plus {ATTESTATION_BEATS} fresh authenticated heartbeats admit; "
        "a stale heartbeat restarts the count"
    ),
}


@dataclass(frozen=True)
class AdmissionRule:
    """The effective admission rule a resolved profile sets."""

    #: The admission knob: ``registration`` | ``join`` | ``operator-granted`` |
    #: ``granted-attested``.
    admission: str
    #: The trust profile it comes from.
    profile: str

    @property
    def policy(self) -> str:
        """``hq.admission_policy`` as it reads: ``open`` or ``manual``."""
        return "open" if self.admission == "registration" else "manual"

    @property
    def registration_admits(self) -> bool:
        return self.admission == "registration"

    @property
    def join_admits(self) -> bool:
        """Whether a frame is admitted without an operator grant (registration or join)."""
        return ADMISSION_RANK[self.admission] <= ADMISSION_RANK["join"]

    @property
    def grant_required(self) -> bool:
        return not self.join_admits

    @property
    def attestation_beats(self) -> int:
        """Fresh authenticated heartbeats a grant needs before it is active (``0``: none)."""
        return ATTESTATION_BEATS if self.admission == "granted-attested" else 0

    def describe(self) -> str:
        return (
            f"{self.admission}: {_DESCRIPTIONS[self.admission]} "
            f"(hq.admission_policy: {self.policy})"
        )

    def as_dict(self) -> dict:
        return {
            "profile": self.profile,
            "admission": self.admission,
            "admission_policy": self.policy,
            "registration_admits": self.registration_admits,
            "join_admits": self.join_admits,
            "grant_required": self.grant_required,
            "attestation_beats": self.attestation_beats,
            "line": self.describe(),
        }


def rule(profile: trust_profiles.ResolvedProfile) -> AdmissionRule:
    """The admission rule `profile` (with its overrides applied) sets."""
    return AdmissionRule(profile.admission, profile.name)


def join_permitted(profile: trust_profiles.ResolvedProfile) -> bool:
    """Whether a frame may be admitted without an operator grant under `profile`: its admission
    rule is ``registration`` or ``join``, or no profile has been learned (0.24.0: a trusted
    fleet default alone auto-admits). The trusted-fleet guard of
    :func:`beadhive.hq_control_plane.open_admission_guard` still applies on top."""
    return rule(profile).join_admits or profile.source == "default"


def join_refusal(profile: trust_profiles.ResolvedProfile) -> str:
    """Why `profile` refuses join (the message :func:`beadhive.hq_open_admission.join` raises)."""
    current = rule(profile)
    return (
        f"the {profile.name} trust profile admits by {current.admission}, not by join: "
        f"{_DESCRIPTIONS[current.admission]}"
    )


# ---- the attested count ----------------------------------------------------------------------


def attested_beats(observation) -> int:
    """How many consecutive fresh authenticated heartbeats `observation` attests, read
    statelessly from the beat HQ stores: the streak its signed commit records
    (:data:`beadhive.host_heartbeat_core.STREAK_TRAILER`, computed where each beat lands, by the
    publisher, from the beat it replaces), counted only when this beat itself verified, is fresh
    and is not a candidate's. Every reader computes the same answer however often it reads.

    A carrier that records no streak attests nothing (``0``), so ``attested`` fails closed
    there: the SQL session and inbox carriers keep only their latest renewal."""
    if getattr(observation, "carrier", "") == "session":
        return 0
    if (
        not getattr(observation, "verified", False)
        or not getattr(observation, "fresh", False)
        or getattr(observation, "candidate", False)
    ):
        return 0
    streak = getattr(observation, "attestation_streak", 0)
    return streak if type(streak) is int and streak > 0 else 0
