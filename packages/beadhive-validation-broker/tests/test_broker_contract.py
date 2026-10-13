"""The shared conformance scenarios, run against the in-memory fake."""

from __future__ import annotations

from beadhive_validation_broker import contract
from beadhive_validation_broker.contract import (
    SCENARIOS,
    ScenarioSkipped,
    fake_harness,
    parametrize_scenarios,
    run_scenario,
)


@parametrize_scenarios()
def test_fake_passes_the_scenario(scenario: contract.Scenario) -> None:
    run_scenario(scenario, fake_harness())


def test_scenarios_cover_safety_nested_and_killed_client() -> None:
    assert {s.group for s in SCENARIOS} == {"safety", "nested", "killed-client"}
    assert len({s.name for s in SCENARIOS}) == len(SCENARIOS)


def test_a_scenario_with_no_pool_skips_rather_than_passing_vacuously() -> None:
    harness = fake_harness()
    harness.budget = type(harness.budget)(harness.budget.cpu, harness.budget.memory_bytes, {})
    pools = next(s for s in SCENARIOS if s.name == "named_pools_are_counted")
    try:
        run_scenario(pools, harness)
    except ScenarioSkipped:
        return
    raise AssertionError("expected ScenarioSkipped")


def test_the_scenarios_detect_an_engine_that_over_admits() -> None:
    """A broken engine that ignores the budget must fail the safety scenario (it bites)."""
    harness = fake_harness()
    broken = harness.broker
    broken._budget = type(harness.budget)(1e9, 1 << 60, {"dolt-servers": 99})  # type: ignore[attr-defined]
    over = next(s for s in SCENARIOS if s.name == "whole_reservation_never_over_admits")
    try:
        run_scenario(over, harness)
    except AssertionError:
        return
    raise AssertionError("an over-admitting engine passed the safety scenario")
