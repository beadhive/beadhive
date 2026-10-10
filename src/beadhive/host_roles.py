"""Composable host roles: a role is a named bundle of capabilities (trust-profiles ADR section 1).

A host declares one or more roles in ``host.roles``; a single ``host.role`` string reads as a
one-element set, so existing manifests need no migration (bh-t1xwp). From the declared set:

* the **effective capability set** is the union of the bundles, then the fleet trust profile's
  **ceiling** removes capabilities from every host regardless of its roles. The ceiling is a
  pluggable input (:data:`Ceiling`); :data:`NO_CEILING` removes nothing, which is the
  ``open``/``trusted`` profile behaviour and the default until the profile table supplies one
  (bh-bc2rj).
* **tenure** follows the highest-tenure role in the set (:func:`tenure_role`): a set containing
  ``executor`` uses the executor ``failover_after`` row, else ``transient``'s; a set of roles
  that are never placed is never placed.
* **admission** approves the set: :func:`admission_grants` projects the declared set into the
  capability list carried by the admission grant, which ``capabilities_match_admission``
  compares. A set that reads exactly like a legacy single role (``executor`` / ``transient`` /
  ``viewer``) projects to ``None`` (no list), so every existing grant keeps matching unchanged.

Deprecated role spellings resolve here too (:func:`canonical_role`, bh-7ztwe), so this module
imports nothing from the manifest model that validates against it.

Pure: imports only :mod:`beadhive.capabilities` (and structlog, to warn on a deprecated name).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

import structlog

from .capabilities import ADMIN, AUTHOR, CLAIM, EXECUTE, PLACE, PUBLISH, REGISTRY

# Deprecated spellings, kept resolving so a rename does not strand every already-registered
# host at clone time — an HQ manifest carries the role STRING, and that is the one failure mode
# this rename must not have. Warned on use, removed in a later release.
#
# The old names were replaced because each was wrong rather than merely long, `worker` most of
# all: it named the ONE role that can do no work (it cannot claim, submit or merge), and the
# word was already taken twice over — bd's worker-on-an-issue lease, and "the agent doing the
# work" throughout work.py's prose. Three independent readers hit it in one day and all drew
# the same wrong conclusion; v0.8.0 shipped documentation stating the exact opposite.
DEPRECATED_ROLE_ALIASES: dict[str, str] = {
    "primary-default": "executor",
    "adopt-on-demand": "transient",
    "worker": "viewer",
}


def canonical_role(role: str) -> str:
    """`role` with a deprecated spelling resolved to its current name, warning on use.

    An unknown role passes through UNTOUCHED — validation belongs to the caller (the manifest
    model's ``Literal``, or ``host init``'s explicit check), and quietly absorbing a typo here
    would turn it into a silent default."""
    current = DEPRECATED_ROLE_ALIASES.get(role)
    if current is None:
        return role
    structlog.get_logger("beadhive.hosts").warning(
        "deprecated_host_role",
        deprecated=role,
        replacement=current,
        reason=(
            "host role renamed (bh-7ztwe) — the old names still resolve but will be removed in "
            "a later release; re-record it with "
            "`bh host init --role " + current + " --force`"
        ),
    )
    return current


VIEWER = "viewer"
PLANNER = "planner"
OPERATOR = "operator"
TRANSIENT = "transient"
EXECUTOR = "executor"
DIRECTOR = "director"

#: The closed role set and each role's capability bundle (ADR section 1, verbatim).
ROLE_BUNDLES: dict[str, frozenset[str]] = {
    VIEWER: frozenset(),
    PLANNER: frozenset({AUTHOR}),
    OPERATOR: frozenset({AUTHOR, ADMIN}),
    TRANSIENT: frozenset({AUTHOR, CLAIM, EXECUTE, PUBLISH}),
    EXECUTOR: frozenset({AUTHOR, CLAIM, EXECUTE, PUBLISH, PLACE}),
    DIRECTOR: frozenset({PLACE, ADMIN}),
}

#: Every role name, in the ADR table's order.
ROLES: tuple[str, ...] = tuple(ROLE_BUNDLES)

#: The single-role values a pre-bh-t1xwp manifest can carry. A set equal to one of these reads
#: exactly as that manifest did, so it projects no grant list (:func:`admission_grants`).
LEGACY_ROLES: tuple[str, ...] = (EXECUTOR, TRANSIENT, VIEWER)

#: Highest tenure first. ``executor`` and ``transient`` are the only placed roles (they own the
#: ``hq_live_failover_policy`` rows); the rest are never placed, ordered so the most capable
#: never-placed role names the set.
TENURE_ORDER: tuple[str, ...] = (EXECUTOR, TRANSIENT, OPERATOR, DIRECTOR, PLANNER, VIEWER)

#: Roles that hold a placement tenure (a ``failover_after``).
PLACED_ROLES: tuple[str, ...] = (EXECUTOR, TRANSIENT)

#: A capability ceiling: given the union of a host's bundles, the capabilities to REMOVE. The
#: fleet trust profile supplies it (bh-bc2rj); a host's roles can never widen past it.
Ceiling = Callable[[frozenset[str]], Iterable[str]]


def NO_CEILING(_capabilities: frozenset[str]) -> frozenset[str]:  # noqa: N802 - a constant
    """The ``open`` / ``trusted`` ceiling: removes nothing, so the roles alone decide."""
    return frozenset()


class UnknownRole(ValueError):
    """A role outside the closed set :data:`ROLES`."""


def role_set(roles: str | Iterable[str]) -> tuple[str, ...]:
    """`roles` (one role string or an iterable of them) as a validated, de-duplicated tuple in
    :data:`ROLES` order. Deprecated spellings resolve first; an empty set or an unknown name
    raises :class:`UnknownRole`."""
    names = [roles] if isinstance(roles, str) else list(roles)
    resolved = set()
    for name in names:
        if not isinstance(name, str):
            raise UnknownRole(f"host role must be a string (got {type(name).__name__})")
        role = canonical_role(name)
        if role not in ROLE_BUNDLES:
            raise UnknownRole(f"unknown host role {name!r} (known: {', '.join(ROLES)})")
        resolved.add(role)
    if not resolved:
        raise UnknownRole("a host declares at least one role")
    return tuple(role for role in ROLES if role in resolved)


def declared_capabilities(roles: str | Iterable[str]) -> frozenset[str]:
    """The union of the bundles of `roles`, before any ceiling."""
    union: frozenset[str] = frozenset()
    for role in role_set(roles):
        union |= ROLE_BUNDLES[role]
    return union


def effective_capabilities(
    roles: str | Iterable[str], ceiling: Ceiling = NO_CEILING
) -> frozenset[str]:
    """Union, then ceiling: the capabilities a host declaring `roles` may exercise."""
    union = declared_capabilities(roles)
    removed = frozenset(ceiling(union))
    unknown = removed - set(REGISTRY)
    if unknown:
        raise ValueError(f"capability ceiling names unknown capabilities: {sorted(unknown)}")
    return union - removed


def tenure_role(roles: str | Iterable[str]) -> str:
    """The highest-tenure role in `roles`: the role whose ``failover_after`` row the host uses."""
    declared = role_set(roles)
    return next(role for role in TENURE_ORDER if role in declared)


def resolve_tenure_role(role: str | Iterable[str]) -> str:
    """:func:`tenure_role` for a reader that must not raise on a manifest from a newer bh: one
    role or a role set; deprecated spellings resolve; an unknown single role passes through
    untouched (callers give it their executor / baseline default); unknown names inside a set
    are ignored, and a set naming no known role reads as ``executor`` (the longest tenure, so a
    newer role never fails over early)."""
    if isinstance(role, str):
        role = canonical_role(role)
        return role if role not in ROLE_BUNDLES else tenure_role(role)
    known = [canonical_role(name) for name in role if isinstance(name, str)]
    known = [name for name in known if name in ROLE_BUNDLES]
    return tenure_role(known) if known else EXECUTOR


def placed(roles: str | Iterable[str]) -> bool:
    """Whether a host declaring `roles` is ever placed (holds a placement tenure)."""
    return tenure_role(roles) in PLACED_ROLES


def admission_grants(roles: str | Iterable[str]) -> list[str] | None:
    """The capability list the admission grant carries for `roles`, sorted; ``None`` for a set
    that is exactly one legacy role (the grant then carries no list, as before bh-t1xwp)."""
    declared = role_set(roles)
    if len(declared) == 1 and declared[0] in LEGACY_ROLES:
        return None
    return sorted(declared_capabilities(declared))
