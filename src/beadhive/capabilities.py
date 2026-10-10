"""The capability registry: the unit a write verb asks for (trust-profiles ADR section 1).

A **capability** names the verbs it gates and the eligibility predicates it consults. A verb is
allowed when the frame's predicates for that one capability hold; predicates a capability does
not list are not evaluated for it. Before this registry every write verb evaluated all fourteen
frame-eligibility predicates, so an operator or planner host with a stale heartbeat, a rotated
release or no dispatch capacity could not file a bead (bh-b3ebg, bh-bd09o).

This module is pure data: it imports nothing from bh and decides nothing on its own. The guard
(:func:`beadhive.guard.guard_capability` / :func:`beadhive.guard.require_capability`) asks
:func:`frame_predicates` which predicates to consult and :mod:`beadhive.frame_eligibility`
filters its decision to them.

Three predicates the eligibility code evaluates are not named in the ADR's table. They are
consulted *implicitly* (:data:`IMPLIED`), never dropped, so no capability is weaker than the
table says:

* ``authority_available`` — every capability: nothing can be evaluated without the authority.
* ``reviewed_admission_or_emergency`` — wherever ``admitted_active`` is consulted: an emergency
  admission still pending review is not an admission.
* ``executor_role`` — every capability: the frame's role grants capabilities, and a ``viewer``
  is read-only. Composable role sets (``host.roles``, bh-t1xwp) replace this with set
  membership.

Predicates in a row that are not frame-eligibility predicates (``writer_reachable``,
``bh_writer``, ``placement_rights``, ``hq_write_access``) are decided by another layer — the
write policy, the in-data writer fence, the placement CAS and HQ's own write access — and are
listed here so the table stays the one description of what each capability consults.
"""

from __future__ import annotations

from dataclasses import dataclass

AUTHOR = "author"
CLAIM = "claim"
EXECUTE = "execute"
PUBLISH = "publish"
PLACE = "place"
ADMIN = "admin"

#: The fourteen frame-eligibility predicates, in :func:`beadhive.frame_eligibility.eligible`'s
#: order. Any other predicate name in a decision is unknown here and is never filtered away.
FRAME_PREDICATES: tuple[str, ...] = (
    "authority_available",
    "admitted_active",
    "not_cordoned",
    "reviewed_admission_or_emergency",
    "current_frame_incarnation",
    "beadyard_binding",
    "authenticated_fresh_heartbeat",
    "release_matches",
    "conformance_pass",
    "capabilities_match_admission",
    "hive_requirements",
    "executor_role",
    "available_capacity",
    "dispatch_enabled",
)

#: Decided outside frame eligibility (see the module docstring).
LAYER_PREDICATES: dict[str, str] = {
    "writer_reachable": "the write policy: the hive's writer is this host or reachable through it",
    "bh_writer": "the in-data writer fence: bh_writer names this frame",
    "placement_rights": "the placement CAS: placement rights for the trust profile",
    "hq_write_access": "HQ write access as the trust profile defines it",
}


@dataclass(frozen=True)
class Capability:
    """One row of the ADR section 1 capability table."""

    name: str
    #: The verbs this capability gates, as the ADR names them.
    verbs: tuple[str, ...]
    #: The predicates this row adds, in the ADR's order.
    adds: tuple[str, ...]
    #: The capability this row builds on (``claim`` is ``author`` plus its own predicates).
    includes: str = ""

    @property
    def predicates(self) -> tuple[str, ...]:
        """Every predicate the capability consults: its base capability's, then its own."""
        base = REGISTRY[self.includes].predicates if self.includes else ()
        return (*base, *(name for name in self.adds if name not in base))


_IDENTITY = ("current_frame_incarnation", "beadyard_binding")

#: The ADR section 1 table, verbatim: verbs gated and predicates consulted per capability.
REGISTRY: dict[str, Capability] = {
    cap.name: cap
    for cap in (
        Capability(
            AUTHOR,
            verbs=(
                "bd create",
                "bd update",
                "bd comment",
                "bd dep",
                "bd label",
                "bd close",
                "plan file",
                "plan approve",
            ),
            adds=(*_IDENTITY, "admitted_active", "not_cordoned"),
        ),
        Capability(
            CLAIM,
            verbs=("work claim", "work resume", "work abandon", "work assign"),
            adds=("writer_reachable",),
            includes=AUTHOR,
        ),
        Capability(
            EXECUTE,
            verbs=("work check", "dispatch", "validation runs", "work next"),
            adds=(
                "authenticated_fresh_heartbeat",
                "release_matches",
                "conformance_pass",
                "capabilities_match_admission",
                "hive_requirements",
                "available_capacity",
                "dispatch_enabled",
            ),
            includes=CLAIM,
        ),
        Capability(
            PUBLISH,
            verbs=("work submit", "work merge", "work finish", "push_state"),
            adds=("bh_writer",),
            includes=EXECUTE,
        ),
        Capability(
            PLACE,
            verbs=("host lease adopt", "host lease release", "failover"),
            adds=("admitted_active", "placement_rights"),
        ),
        Capability(
            ADMIN,
            verbs=("hq authority", "fleet-config publish", "admission of other frames"),
            adds=("hq_write_access",),
        ),
    )
}

#: The capabilities a bead write verb asks for, least to most demanding.
WRITE_CAPABILITIES: tuple[str, ...] = (AUTHOR, CLAIM, EXECUTE, PUBLISH)

#: Frame predicates consulted without being named in the table (module docstring).
IMPLIED: dict[str, str] = {
    "authority_available": "every capability",
    "reviewed_admission_or_emergency": "with admitted_active",
    "executor_role": "every capability",
}


class UnknownCapability(ValueError):
    """A verb asked for a capability the registry does not name."""


def get(name: str) -> Capability:
    try:
        return REGISTRY[name]
    except KeyError:
        raise UnknownCapability(
            f"unknown capability {name!r} (known: {', '.join(REGISTRY)})"
        ) from None


def frame_predicates(name: str) -> tuple[str, ...]:
    """The frame-eligibility predicates `name` consults, in :data:`FRAME_PREDICATES` order:
    the table's own frame predicates plus the :data:`IMPLIED` ones."""
    consulted = set(get(name).predicates)
    consulted.add("authority_available")
    consulted.add("executor_role")
    if "admitted_active" in consulted:
        consulted.add("reviewed_admission_or_emergency")
    return tuple(name for name in FRAME_PREDICATES if name in consulted)


def layer_predicates(name: str) -> tuple[str, ...]:
    """The predicates `name` consults that another layer decides (:data:`LAYER_PREDICATES`)."""
    return tuple(p for p in get(name).predicates if p in LAYER_PREDICATES)
