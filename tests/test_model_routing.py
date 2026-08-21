"""Deterministic model selection, availability adapters, and launch translation."""

from __future__ import annotations

from beadhive.complexity import ComplexityTier
from beadhive.config_schema import RoutingTierConfig
from beadhive.model_routing import (
    AvailabilitySnapshot,
    GatewayAvailabilityAdapter,
    HarnessAvailabilityAdapter,
    ModelBlockedVerdict,
    ModelSelection,
    discover_availability,
    launch_model,
    resolve_model,
)


def _route(model, floor="SIMPLE", ceiling="REASONING", endpoint=None):
    return RoutingTierConfig(model=model, floor=floor, ceiling=ceiling, endpoint=endpoint)


def _live(models, *, endpoint=None, role=None, harness=None, source="gateway_live"):
    return AvailabilitySnapshot.live(
        models,
        source=source,
        endpoint=endpoint,
        role=role,
        harness=harness,
    )


def _resolve(tier, routes, availability=(), **kwargs):
    return resolve_model(
        tier,
        routes=routes,
        availability=availability,
        role=kwargs.pop("role", "developer"),
        harness=kwargs.pop("harness", "claude"),
        **kwargs,
    )


def test_exact_boundaries_and_least_overpowered_selection():
    routes = [
        _route("openai/small", ceiling="MEDIUM"),
        _route("openai/large", floor="COMPLEX"),
    ]
    evidence = [_live({"openai/small", "openai/large"}, endpoint=None)]

    assert _resolve(ComplexityTier.MEDIUM, routes, evidence).selected_model == "openai/small"
    assert _resolve(ComplexityTier.COMPLEX, routes, evidence).selected_model == "openai/large"


def test_omitted_bounds_cover_the_full_range():
    decision = _resolve(
        ComplexityTier.REASONING,
        [_route("future/model")],
        [_live({"future/model"}, endpoint=None)],
    )
    assert isinstance(decision, ModelSelection)
    assert decision.selected_model == "future/model"
    assert decision.warnings == ()


def test_authoritative_empty_catalogue_blocks_in_both_policies():
    route = _route("openai/gpt", endpoint="https://gateway.example")
    empty = _live(set(), endpoint=route.endpoint)

    for policy in ("loose", "strict"):
        decision = _resolve(ComplexityTier.MEDIUM, [route], [empty], policy=policy)
        assert isinstance(decision, ModelBlockedVerdict)
        assert "unavailable" in decision.reason
        assert decision.availability_source == "gateway_live"


def test_no_enumeration_uses_explicit_config_without_claiming_live_discovery():
    route = _route("anthropic/claude-sonnet")
    decision = _resolve(ComplexityTier.MEDIUM, [route])

    assert decision.selected_model == route.model
    assert decision.availability_source == "explicit_configuration"
    assert any("enumeration unavailable" in warning for warning in decision.warnings)


def test_preferred_model_succeeds_when_available_and_in_range():
    routes = [_route("openai/small"), _route("anthropic/preferred")]
    decision = _resolve(
        ComplexityTier.COMPLEX,
        routes,
        [_live({route.model for route in routes}, endpoint=None)],
        preferred_model="anthropic/preferred",
        policy="strict",
    )
    assert decision.selected_model == "anthropic/preferred"
    assert decision.preferred_model == "anthropic/preferred"
    assert "preferred" in decision.selection_reason


def test_strict_preferred_unavailable_is_actionable_block():
    routes = [_route("openai/fallback"), _route("anthropic/preferred")]
    decision = _resolve(
        ComplexityTier.MEDIUM,
        routes,
        [_live({"openai/fallback"}, endpoint=None)],
        preferred_model="anthropic/preferred",
        policy="strict",
    )
    assert isinstance(decision, ModelBlockedVerdict)
    assert decision.preferred_model == "anthropic/preferred"
    assert "unavailable" in decision.reason
    assert "work.routing.tiers" in decision.remediation


def test_loose_preferred_failure_falls_back_with_visible_warning():
    routes = [_route("openai/fallback"), _route("anthropic/preferred")]
    decision = _resolve(
        ComplexityTier.MEDIUM,
        routes,
        [_live({"openai/fallback"}, endpoint=None)],
        preferred_model="anthropic/preferred",
        policy="loose",
    )
    assert decision.selected_model == "openai/fallback"
    assert any("preferred model anthropic/preferred is unavailable" in w for w in decision.warnings)


def test_below_floor_and_above_ceiling_fallbacks_are_distinct():
    below_floor = _resolve(
        ComplexityTier.SIMPLE,
        [_route("openai/medium", floor="MEDIUM")],
        [_live({"openai/medium"}, endpoint=None)],
    )
    assert any("below floor MEDIUM" in warning for warning in below_floor.warnings)

    above_ceiling = _resolve(
        ComplexityTier.REASONING,
        [_route("openai/medium", ceiling="MEDIUM")],
        [_live({"openai/medium"}, endpoint=None)],
    )
    assert any("above ceiling MEDIUM" in warning for warning in above_ceiling.warnings)
    assert "out-of-range" in above_ceiling.selection_reason


def test_strict_no_hint_still_selects_by_complexity_but_blocks_out_of_range():
    routes = [_route("openai/simple", ceiling="SIMPLE"), _route("openai/complex", floor="COMPLEX")]
    facts = [_live({route.model for route in routes}, endpoint=None)]
    selected = _resolve(ComplexityTier.COMPLEX, routes, facts, policy="strict")
    assert selected.selected_model == "openai/complex"

    blocked = _resolve(
        ComplexityTier.MEDIUM,
        [_route("openai/simple", ceiling="SIMPLE")],
        [_live({"openai/simple"}, endpoint=None)],
        policy="strict",
    )
    assert isinstance(blocked, ModelBlockedVerdict)
    assert "covers complexity MEDIUM" in blocked.reason


def test_conflicting_endpoints_are_not_combined_by_model_name():
    routes = [
        _route("openai/shared", endpoint="https://empty.example"),
        _route("openai/shared", endpoint="https://live.example"),
    ]
    facts = [
        _live(set(), endpoint="https://empty.example"),
        _live({"openai/shared"}, endpoint="https://live.example"),
    ]
    decision = _resolve(ComplexityTier.MEDIUM, routes, facts)
    assert decision.endpoint == "https://live.example"


def test_role_specific_availability_is_respected():
    route = _route("anthropic/opus")
    facts = [
        _live(
            {"anthropic/opus"},
            endpoint=None,
            role="dispatcher",
            harness="claude",
            source="harness_default",
        ),
        _live(
            set(),
            endpoint=None,
            role="developer",
            harness="claude",
            source="harness_default",
        ),
    ]
    assert not _resolve(ComplexityTier.REASONING, [route], facts, role="dispatcher").blocked
    assert _resolve(ComplexityTier.REASONING, [route], facts, role="developer").blocked


def test_gateway_adapter_caches_and_uses_stale_cache_on_fetch_failure():
    clock = [0.0]
    calls = []

    def fetch(url):
        calls.append(url)
        if len(calls) > 1:
            raise OSError("offline")
        return ["openai/gpt"]

    adapter = GatewayAvailabilityAdapter(fetch=fetch, ttl_seconds=5, clock=lambda: clock[0])
    live = adapter.snapshot("https://gateway.example", role="developer", harness="opencode")
    cached = adapter.snapshot("https://gateway.example", role="developer", harness="opencode")
    clock[0] = 10
    stale = adapter.snapshot("https://gateway.example", role="developer", harness="opencode")

    assert live.source == "gateway_live"
    assert cached.source == "gateway_cache"
    assert stale.source == "gateway_stale_cache"
    assert stale.models == frozenset({"openai/gpt"})
    assert "stale cache" in stale.warning
    assert calls[0].endswith("/v1/models")


def test_gateway_fetch_failure_without_cache_degrades_to_explicit_evidence():
    def fail(_url):
        raise TimeoutError

    route = _route("openai/gpt", endpoint="https://gateway.example")
    facts = discover_availability(
        [route],
        role="developer",
        harness="opencode",
        gateway=GatewayAvailabilityAdapter(fetch=fail),
    )
    decision = _resolve(ComplexityTier.MEDIUM, [route], facts, harness="opencode")
    assert decision.selected_model == route.model
    assert decision.availability_source == "explicit_configuration"
    assert any("fetch failed" in warning for warning in decision.warnings)


def test_harness_default_adapter_distinguishes_enumeration_from_explicit_config():
    adapter = HarnessAvailabilityAdapter(
        models={
            ("claude", "developer"): {"anthropic/sonnet"},
            ("claude", "dispatcher"): None,
        }
    )
    dev = adapter.snapshot(
        role="developer", harness="claude", configured_models=["anthropic/sonnet"]
    )
    dispatcher = adapter.snapshot(
        role="dispatcher", harness="claude", configured_models=["anthropic/opus"]
    )
    assert dev.enumerated and dev.source == "harness_default"
    assert not dispatcher.enumerated and dispatcher.source == "explicit_configuration"


def test_harness_translation_is_independent_and_preserves_other_providers():
    canonical = "anthropic/claude-opus-4-1"
    assert launch_model(canonical, "claude") == "claude-opus-4-1"
    assert launch_model(canonical, "opencode") == canonical
    assert launch_model("openai/gpt-5", "claude") == "openai/gpt-5"
