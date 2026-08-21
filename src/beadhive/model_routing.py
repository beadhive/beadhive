"""Late-bound complexity-to-model selection.

The durable identity at this boundary is always ``provider/model``.  Availability discovery and
harness launch aliases are deliberately separate adapters: the pure :func:`resolve_model` core
can therefore explain exactly which evidence it used without doing I/O or accidentally leaking a
harness token into schedule payloads.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .complexity import ComplexityTier
from .config_schema import RoutingTierConfig

POLICIES = frozenset({"loose", "strict"})


@dataclass(frozen=True)
class AvailabilitySnapshot:
    """Immutable model-availability evidence for one endpoint or harness default.

    ``enumerated`` distinguishes an authoritative empty catalogue (no models are available) from
    a subscription harness that cannot enumerate models.  The latter may honestly fall back to
    explicit configuration, but must say that it did so.
    """

    models: frozenset[str] = frozenset()
    source: str = "explicit_configuration"
    enumerated: bool = False
    endpoint: str | None = None
    role: str | None = None
    harness: str | None = None
    warning: str = ""

    @classmethod
    def live(
        cls,
        models: Iterable[str],
        *,
        source: str,
        endpoint: str | None = None,
        role: str | None = None,
        harness: str | None = None,
    ) -> AvailabilitySnapshot:
        return cls(
            models=frozenset(str(model) for model in models),
            source=source,
            enumerated=True,
            endpoint=endpoint,
            role=role,
            harness=harness,
        )


@dataclass(frozen=True)
class ModelSelection:
    """A launchable canonical decision and complete user-visible provenance."""

    required_tier: ComplexityTier
    preferred_model: str | None
    selected_model: str
    policy: str
    role: str
    harness: str
    endpoint: str | None
    availability_source: str
    selection_reason: str
    warnings: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "complexity": self.required_tier.name,
            "preferred_model": self.preferred_model,
            "selected_model": self.selected_model,
            "selection_reason": self.selection_reason,
            "policy": self.policy,
            "availability_source": self.availability_source,
            "endpoint": self.endpoint,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ModelBlockedVerdict:
    """A typed, actionable refusal produced by strict routing or no candidates."""

    required_tier: ComplexityTier
    preferred_model: str | None
    policy: str
    role: str
    harness: str
    availability_source: str
    reason: str
    remediation: str
    warnings: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "complexity": self.required_tier.name,
            "preferred_model": self.preferred_model,
            "selected_model": None,
            "selection_reason": self.reason,
            "policy": self.policy,
            "availability_source": self.availability_source,
            "endpoint": None,
            "warnings": list(self.warnings),
            "blocked": True,
            "remediation": self.remediation,
        }


ModelDecision = ModelSelection | ModelBlockedVerdict


@dataclass(frozen=True)
class _Candidate:
    route: RoutingTierConfig
    snapshot: AvailabilitySnapshot
    available: bool
    order: int

    @property
    def below_floor(self) -> bool:
        return self.route.floor > self.required

    @property
    def above_ceiling(self) -> bool:
        return self.route.ceiling < self.required

    @property
    def in_range(self) -> bool:
        return not self.below_floor and not self.above_ceiling

    # Filled by _candidates using object.__setattr__ on this private frozen transport.  Keeping the
    # required tier here makes range predicates concise without exposing another public type.
    required: ComplexityTier = field(default=ComplexityTier.SIMPLE, repr=False)


def _snapshot_for(
    route: RoutingTierConfig,
    snapshots: Sequence[AvailabilitySnapshot],
    *,
    role: str,
    harness: str,
) -> AvailabilitySnapshot:
    scoped = [
        snap
        for snap in snapshots
        if snap.endpoint == route.endpoint
        and (snap.role is None or snap.role == role)
        and (snap.harness is None or snap.harness == harness)
    ]
    if scoped:
        # More specific role/harness evidence wins, then the caller's stable input order.
        return max(
            scoped, key=lambda snap: int(snap.role is not None) + int(snap.harness is not None)
        )
    return AvailabilitySnapshot(
        models=frozenset({route.model}),
        source="explicit_configuration",
        enumerated=False,
        endpoint=route.endpoint,
        role=role,
        harness=harness,
        warning="model enumeration unavailable; trusted explicit routing configuration",
    )


def _candidates(
    required: ComplexityTier,
    routes: Sequence[RoutingTierConfig],
    snapshots: Sequence[AvailabilitySnapshot],
    *,
    role: str,
    harness: str,
) -> list[_Candidate]:
    out = []
    for order, route in enumerate(routes):
        snap = _snapshot_for(route, snapshots, role=role, harness=harness)
        available = route.model in snap.models if snap.enumerated else True
        out.append(
            _Candidate(
                route=route,
                snapshot=snap,
                available=available,
                order=order,
                required=required,
            )
        )
    return out


def _source(candidates: Sequence[_Candidate]) -> str:
    values = list(dict.fromkeys(candidate.snapshot.source for candidate in candidates))
    return ",".join(values) if values else "none"


def _range_warning(candidate: _Candidate) -> str:
    if candidate.below_floor:
        return (
            f"selected {candidate.route.model} above the required range: "
            f"{candidate.required.name} is below floor {candidate.route.floor.name}"
        )
    if candidate.above_ceiling:
        return (
            f"selected {candidate.route.model} below the required range: "
            f"{candidate.required.name} is above ceiling {candidate.route.ceiling.name}"
        )
    return ""


def _least_overpowered(candidates: Sequence[_Candidate]) -> _Candidate:
    # Prefer the narrowest adequate ceiling, then the highest floor (the most specific interval),
    # finally preserve configured order as the explicit operator tie-break.
    return min(
        candidates,
        key=lambda candidate: (
            int(candidate.route.ceiling),
            -int(candidate.route.floor),
            candidate.order,
        ),
    )


def _fallback_candidate(candidates: Sequence[_Candidate]) -> _Candidate:
    # First prefer overpowered-but-capable routes (required below floor).  Only if none exist may
    # loose mode run an under-capable model (required above ceiling), which is always warned.
    below_floor = [candidate for candidate in candidates if candidate.below_floor]
    if below_floor:
        return min(
            below_floor,
            key=lambda candidate: (
                int(candidate.route.floor) - int(candidate.required),
                candidate.order,
            ),
        )
    return min(
        candidates,
        key=lambda candidate: (
            int(candidate.required) - int(candidate.route.ceiling),
            candidate.order,
        ),
    )


def _selected(
    candidate: _Candidate,
    *,
    required: ComplexityTier,
    preferred_model: str | None,
    policy: str,
    role: str,
    harness: str,
    reason: str,
    warnings: Sequence[str],
) -> ModelSelection:
    all_warnings = list(warnings)
    if candidate.snapshot.warning:
        all_warnings.append(candidate.snapshot.warning)
    range_warning = _range_warning(candidate)
    if range_warning:
        all_warnings.append(range_warning)
    return ModelSelection(
        required_tier=required,
        preferred_model=preferred_model,
        selected_model=candidate.route.model,
        policy=policy,
        role=role,
        harness=harness,
        endpoint=candidate.route.endpoint,
        availability_source=candidate.snapshot.source,
        selection_reason=reason,
        warnings=tuple(dict.fromkeys(warning for warning in all_warnings if warning)),
    )


def resolve_model(
    required_tier: ComplexityTier,
    *,
    preferred_model: str | None = None,
    policy: str = "loose",
    role: str,
    harness: str,
    routes: Sequence[RoutingTierConfig],
    availability: Sequence[AvailabilitySnapshot] = (),
) -> ModelDecision:
    """Resolve one model without I/O, mutation, ambient config, or harness translation."""
    if not isinstance(required_tier, ComplexityTier):
        raise TypeError("required_tier must be a ComplexityTier")
    if policy not in POLICIES:
        raise ValueError(f"unknown routing policy {policy!r}; expected loose or strict")

    candidates = _candidates(required_tier, routes, availability, role=role, harness=harness)
    evidence = _source(candidates)
    remediation = (
        "configure an in-range model under work.routing.tiers, make it available for this "
        f"{role}/{harness} launch, or change work.routing.policy to loose"
    )
    if not candidates:
        return ModelBlockedVerdict(
            required_tier,
            preferred_model,
            policy,
            role,
            harness,
            evidence,
            "no routing models are configured",
            remediation,
        )

    warnings: list[str] = []
    if preferred_model:
        preferred = [
            candidate for candidate in candidates if candidate.route.model == preferred_model
        ]
        usable_preferred = [
            candidate for candidate in preferred if candidate.available and candidate.in_range
        ]
        if usable_preferred:
            return _selected(
                _least_overpowered(usable_preferred),
                required=required_tier,
                preferred_model=preferred_model,
                policy=policy,
                role=role,
                harness=harness,
                reason="preferred model is available and covers the required complexity",
                warnings=warnings,
            )
        if not preferred:
            failure = f"preferred model {preferred_model} is not configured"
        elif not any(candidate.available for candidate in preferred):
            failure = f"preferred model {preferred_model} is unavailable"
        else:
            candidate = next(candidate for candidate in preferred if candidate.available)
            relation = (
                f"below floor {candidate.route.floor.name}"
                if candidate.below_floor
                else f"above ceiling {candidate.route.ceiling.name}"
            )
            failure = f"preferred model {preferred_model} is out of range ({relation})"
        if policy == "strict":
            return ModelBlockedVerdict(
                required_tier,
                preferred_model,
                policy,
                role,
                harness,
                _source(preferred or candidates),
                failure,
                remediation,
            )
        warnings.append(failure + "; loose policy selected by complexity instead")

    available = [candidate for candidate in candidates if candidate.available]
    eligible = [candidate for candidate in available if candidate.in_range]
    if eligible:
        return _selected(
            _least_overpowered(eligible),
            required=required_tier,
            preferred_model=preferred_model,
            policy=policy,
            role=role,
            harness=harness,
            reason="least-overpowered available model covering the required complexity",
            warnings=warnings,
        )
    if policy == "strict" or not available:
        reason = (
            "all configured models are unavailable for this role and harness"
            if not available
            else f"no available model covers complexity {required_tier.name}"
        )
        return ModelBlockedVerdict(
            required_tier,
            preferred_model,
            policy,
            role,
            harness,
            evidence,
            reason,
            remediation,
            tuple(warnings),
        )

    fallback = _fallback_candidate(available)
    return _selected(
        fallback,
        required=required_tier,
        preferred_model=preferred_model,
        policy=policy,
        role=role,
        harness=harness,
        reason="loose policy used the nearest available out-of-range model",
        warnings=warnings,
    )


class GatewayModelFetcher(Protocol):
    def __call__(self, url: str) -> Iterable[str]: ...


def _openai_models(url: str) -> Iterable[str]:
    """Small stdlib OpenAI/Bifrost ``/v1/models`` reader; authentication stays external."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - configured URL
        payload = json.load(response)
    rows = payload.get("data", []) if isinstance(payload, Mapping) else []
    return [str(row["id"]) for row in rows if isinstance(row, Mapping) and row.get("id")]


@dataclass
class GatewayAvailabilityAdapter:
    """Query and TTL-cache OpenAI-compatible/Bifrost model catalogues."""

    fetch: GatewayModelFetcher = _openai_models
    ttl_seconds: float = 300.0
    clock: Callable[[], float] = time.monotonic
    _cache: dict[str, tuple[float, frozenset[str]]] = field(default_factory=dict)

    def snapshot(self, endpoint: str, *, role: str, harness: str) -> AvailabilitySnapshot:
        url = (
            endpoint
            if endpoint.rstrip("/").endswith("/v1/models")
            else endpoint.rstrip("/") + "/v1/models"
        )
        now = self.clock()
        cached = self._cache.get(endpoint)
        if cached and now - cached[0] <= self.ttl_seconds:
            return AvailabilitySnapshot.live(
                cached[1], source="gateway_cache", endpoint=endpoint, role=role, harness=harness
            )
        try:
            models = frozenset(self.fetch(url))
        except Exception as exc:  # adapters convert transport failures into honest evidence
            if cached:
                return AvailabilitySnapshot(
                    models=cached[1],
                    source="gateway_stale_cache",
                    enumerated=True,
                    endpoint=endpoint,
                    role=role,
                    harness=harness,
                    warning=f"catalogue refresh failed; used stale cache ({type(exc).__name__})",
                )
            return AvailabilitySnapshot(
                source="explicit_configuration",
                endpoint=endpoint,
                role=role,
                harness=harness,
                warning=(
                    f"catalogue fetch failed; availability not enumerated ({type(exc).__name__})"
                ),
            )
        self._cache[endpoint] = (now, models)
        return AvailabilitySnapshot.live(
            models, source="gateway_live", endpoint=endpoint, role=role, harness=harness
        )


@dataclass(frozen=True)
class HarnessAvailabilityAdapter:
    """Role-scoped subscription/default availability, where enumeration may be impossible."""

    models: Mapping[tuple[str, str], Iterable[str] | None] = field(default_factory=dict)

    def snapshot(
        self,
        *,
        role: str,
        harness: str,
        configured_models: Iterable[str],
    ) -> AvailabilitySnapshot:
        value = self.models.get((harness, role))
        if value is None:
            return AvailabilitySnapshot(
                models=frozenset(configured_models),
                source="explicit_configuration",
                enumerated=False,
                role=role,
                harness=harness,
                warning=(
                    "harness cannot enumerate subscription models; trusted explicit configuration"
                ),
            )
        return AvailabilitySnapshot.live(
            value, source="harness_default", role=role, harness=harness
        )


def discover_availability(
    routes: Sequence[RoutingTierConfig],
    *,
    role: str,
    harness: str,
    gateway: GatewayAvailabilityAdapter | None = None,
    harness_defaults: HarnessAvailabilityAdapter | None = None,
) -> tuple[AvailabilitySnapshot, ...]:
    """Run the appropriate adapter once per distinct endpoint plus one harness-default query."""
    gateway = gateway or GatewayAvailabilityAdapter()
    harness_defaults = harness_defaults or HarnessAvailabilityAdapter()
    snapshots = []
    endpoints = list(dict.fromkeys(route.endpoint for route in routes if route.endpoint))
    snapshots.extend(
        gateway.snapshot(endpoint, role=role, harness=harness) for endpoint in endpoints
    )
    default_models = [route.model for route in routes if route.endpoint is None]
    if default_models:
        snapshots.append(
            harness_defaults.snapshot(role=role, harness=harness, configured_models=default_models)
        )
    return tuple(snapshots)


class LaunchModelAdapter(Protocol):
    """Translate canonical identity only at the final harness launch boundary."""

    def translate(self, canonical_model: str) -> str: ...


class IdentityLaunchModelAdapter:
    def translate(self, canonical_model: str) -> str:
        return canonical_model


class ClaudeLaunchModelAdapter:
    """Claude's model flag names Anthropic models without the durable provider prefix."""

    def translate(self, canonical_model: str) -> str:
        provider, separator, model = canonical_model.partition("/")
        return model if separator and provider == "anthropic" else canonical_model


def launch_model(canonical_model: str, harness: str) -> str:
    """Return the harness token while leaving the decision's canonical identity untouched."""
    adapter: LaunchModelAdapter
    adapter = ClaudeLaunchModelAdapter() if harness == "claude" else IdentityLaunchModelAdapter()
    return adapter.translate(canonical_model)
