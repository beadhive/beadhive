"""The explicit operation-routing table built from the bh-97fo0.3 parity evidence.

Every named Beads operation Beadhive cares about — work, planning, gate, lease, heartbeat,
reclaim and merge-slot — is exactly one of four routes, taken verbatim from the classification
recorded in :func:`beadhive_beads_client.load_operation_matrix`:

* **api-ready** — a typed :class:`beadhive_beads_client.BeadsSession` method, proven against a
  real service. Selecting one checks the negotiated session's capabilities before any caller can
  reach the mutation.
* **cli-compatibility** / **administrative** — an explicit, named ``bd`` path. The table only
  names *why* (the matrix's ``reason``); it never executes the operation itself. Concurrency-
  sensitive coordination (lease, heartbeat, reclaim, merge-slot, gate) stays here because those
  guarantees come from the real service or the real ``bd`` binary, never from an in-memory
  stand-in.
* **denied** — outside the approved surface (``issues.delete``, ``issues.sweep``). Unroutable by
  either selector, regardless of capability.

This is deliberately not a provider framework: there is no generic "backend" abstraction, no
runtime storage selection, and no automatic fallback from an API route to a CLI route. An
ambiguous HTTP write (:class:`beadhive_beads_client.IndeterminateWrite`) is reported by the
session for the caller to reconcile by reading; it is never quietly replayed here through
``bd``. A caller selects exactly one named route before it does anything else, and that
selection is observable so the chosen route can be attributed in telemetry.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from beadhive_beads_client import BeadsSession, CapabilityMissing, load_operation_matrix


class RouteKind(Enum):
    """The four classifications the bh-97fo0.3 matrix assigns; no fifth kind exists."""

    API_READY = "api-ready"
    CLI_COMPATIBILITY = "cli-compatibility"
    ADMINISTRATIVE = "administrative"
    DENIED = "denied"


@dataclass(frozen=True)
class ApiRoute:
    """A typed ``BeadsSession`` method, proven by real-service evidence."""

    name: str
    capability: str
    session_method: str
    evidence: str
    kind: RouteKind = field(default=RouteKind.API_READY, init=False)


@dataclass(frozen=True)
class CompatibilityRoute:
    """A named CLI path: ``cli-compatibility`` (unsupported/ambiguous over HTTP) or
    ``administrative`` (backup/migration/sync). The table carries only the named reason; the
    compatibility shell owns the actual ``bd`` invocation (see ``GateOperations`` /
    ``StateOperations`` in :mod:`beadhive_core.review` and their ``bh`` composition-seam
    implementations).
    """

    name: str
    kind: RouteKind
    reason: str

    def __post_init__(self) -> None:
        if self.kind not in (RouteKind.CLI_COMPATIBILITY, RouteKind.ADMINISTRATIVE):
            raise ValueError(f"{self.name}: not a compatibility route ({self.kind})")


@dataclass(frozen=True)
class DeniedRoute:
    """A destructive operation outside the approved surface. Never selectable."""

    name: str
    reason: str
    kind: RouteKind = field(default=RouteKind.DENIED, init=False)


Route = ApiRoute | CompatibilityRoute | DeniedRoute


class UnknownOperation(KeyError):
    """No row in the installed operation matrix names this operation."""


class OperationDenied(RuntimeError):
    """The operation is classified ``denied``; there is no route to select, ever."""

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason
        super().__init__(f"{name} is denied: {reason}")


class RouteMismatch(RuntimeError):
    """The caller asked for a kind of route this operation does not have.

    Raised instead of silently returning the wrong route — e.g. asking ``select_cli`` for an
    api-ready operation, which would otherwise look like an unapproved HTTP-to-CLI fallback.
    """

    def __init__(self, name: str, wanted: str, actual: RouteKind) -> None:
        self.name = name
        self.wanted = wanted
        self.actual = actual
        super().__init__(f"{name} is {actual.value}, not {wanted}")


class RoutingObserver(Protocol):
    """Observability hook: notified with the operation and the route kind actually selected."""

    def selected(self, name: str, kind: str) -> None: ...


class NullRoutingObserver:
    def selected(self, name: str, kind: str) -> None:
        return None


def _build_route(row: Mapping[str, Any]) -> Route:
    name = str(row["name"])
    classification = RouteKind(str(row["classification"]))
    if classification is RouteKind.API_READY:
        return ApiRoute(
            name=name,
            capability=str(row["capability"]),
            session_method=str(row["session_method"]),
            evidence=str(row.get("evidence", "")),
        )
    if classification is RouteKind.DENIED:
        return DeniedRoute(name=name, reason=str(row.get("reason", "")))
    return CompatibilityRoute(name=name, kind=classification, reason=str(row.get("reason", "")))


@dataclass(frozen=True)
class RoutingTable:
    """Every named operation, resolved to exactly one route.

    Built once from :func:`beadhive_beads_client.load_operation_matrix`; :func:`default_table`
    caches the singleton every caller should use. Construct one directly only to test against a
    fixture matrix (see ``test_core_routing_policy.py``).
    """

    _routes: Mapping[str, Route]

    @classmethod
    def from_matrix(cls, matrix: Mapping[str, Any] | None = None) -> RoutingTable:
        payload = matrix if matrix is not None else load_operation_matrix()
        routes = {str(row["name"]): _build_route(row) for row in payload["operations"]}
        return cls(routes)

    def names(self) -> frozenset[str]:
        return frozenset(self._routes)

    def route(self, name: str) -> Route:
        """Every in-scope operation has exactly one named route — look it up or fail loudly."""
        try:
            return self._routes[name]
        except KeyError:
            raise UnknownOperation(name) from None

    def select_api(
        self,
        name: str,
        capabilities: Collection[str],
        *,
        observer: RoutingObserver | None = None,
    ) -> ApiRoute:
        """The named ``BeadsSession`` method for ``name``, capability-checked before any write.

        Raises :class:`OperationDenied` for a denied operation, :class:`RouteMismatch` when
        ``name`` is not api-ready (a cli-compatibility or administrative operation has no API
        route to fall back to — selecting one is a caller bug, not an ambiguous write), and
        :class:`beadhive_beads_client.CapabilityMissing` when the negotiated session does not
        advertise the required capability. All three checks happen before the caller can reach
        the session method, so an unsupported capability fails before any mutation.
        """
        route = self.route(name)
        if isinstance(route, DeniedRoute):
            raise OperationDenied(name, route.reason)
        if not isinstance(route, ApiRoute):
            raise RouteMismatch(name, "api-ready", route.kind)
        if route.capability not in capabilities:
            raise CapabilityMissing(f"Beads capability missing: {route.capability}")
        (observer or NullRoutingObserver()).selected(name, route.kind.value)
        return route

    def select_cli(
        self, name: str, *, observer: RoutingObserver | None = None
    ) -> CompatibilityRoute:
        """The named CLI-compatibility route for ``name`` (never administrative)."""
        return self._select_compatibility(name, RouteKind.CLI_COMPATIBILITY, observer)

    def select_administrative(
        self, name: str, *, observer: RoutingObserver | None = None
    ) -> CompatibilityRoute:
        """The named administrative route for ``name`` (backup, migration, sync)."""
        return self._select_compatibility(name, RouteKind.ADMINISTRATIVE, observer)

    def _select_compatibility(
        self, name: str, wanted: RouteKind, observer: RoutingObserver | None
    ) -> CompatibilityRoute:
        route = self.route(name)
        if isinstance(route, DeniedRoute):
            raise OperationDenied(name, route.reason)
        if not isinstance(route, CompatibilityRoute) or route.kind is not wanted:
            raise RouteMismatch(name, wanted.value, route.kind)
        (observer or NullRoutingObserver()).selected(name, route.kind.value)
        return route

    def call_api(
        self,
        session: BeadsSession,
        name: str,
        *args: object,
        observer: RoutingObserver | None = None,
        **kwargs: object,
    ) -> object:
        """Select ``name``'s api route against ``session``'s negotiated capabilities and call it.

        The one call point downstream lifecycle/planning cohorts use instead of constructing
        their own dispatch: the session method invoked is always the one the matrix names for
        ``name``, never chosen generically at runtime.
        """
        if session.context is None:
            raise RuntimeError("Beads session is not open")
        route = self.select_api(name, session.context.capabilities, observer=observer)
        method: Callable[..., object] = getattr(session, route.session_method)
        return method(*args, **kwargs)


_default_table: RoutingTable | None = None


def default_table() -> RoutingTable:
    """The routing table built from the installed operation matrix, cached process-wide."""
    global _default_table
    if _default_table is None:
        _default_table = RoutingTable.from_matrix()
    return _default_table


#: Concurrency-sensitive coordination operations that must never move to an api-ready route
#: without a deliberate, evidenced bead: their exclusivity/staleness guarantees come from the
#: real service or the real ``bd`` binary (see
#: ``packages/beadhive-bd-cli/tests/test_coordination_int.py`` and ``tests/test_merge_slot.py``),
#: never from an in-memory stand-in. ``test_core_routing_policy``
#: fails the day the installed matrix reclassifies any of these.
COORDINATION_OPERATIONS: tuple[str, ...] = (
    "work.gate.lookup",
    "work.gate.create",
    "work.gate.resolve",
    "work.lease.acquire",
    "work.lease.heartbeat",
    "work.lease.reclaim",
    "work.lease.release",
    "work.merge-slot.check",
    "work.merge-slot.create",
    "work.merge-slot.acquire",
    "work.merge-slot.release",
)
