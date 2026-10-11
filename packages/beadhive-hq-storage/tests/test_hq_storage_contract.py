"""The reusable contract suite, run against the in-memory fake (the ``fake`` parameter only).

``beadhive-hq-storage-dolt`` runs the very same scenarios parametrized over ``[fake, dolt]``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from beadhive_hq_storage.contract import (
    SCENARIOS,
    ContractAdapter,
    FakeFenceHarness,
    FakeHqHarness,
    Scenario,
    adapter_fixture,
    fake_adapter,
    parametrize_scenarios,
    run_scenario,
)

README = Path(__file__).resolve().parents[1] / "README.md"

adapter = adapter_fixture({"fake": fake_adapter})


@parametrize_scenarios()
def test_contract(scenario: Scenario, adapter: ContractAdapter) -> None:
    run_scenario(scenario, adapter)


def _git_like_adapter() -> ContractAdapter:
    """The fake configured like an adapter that refuses the combined atomic read (Git)."""
    return ContractAdapter(
        id="fake-no-consistent-read",
        make_hq=lambda: FakeHqHarness(consistent_read=False),
        make_fence=FakeFenceHarness,
        clock_controlled=False,
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_contract_without_clock_control_or_consistent_read(scenario: Scenario) -> None:
    """The same suite on an adapter shape with no controllable clock: clock cases skip, the
    rest still pass (what the Dolt parameter sees)."""
    if scenario.needs_clock:
        with pytest.raises(pytest.skip.Exception):
            run_scenario(scenario, _git_like_adapter())
        return
    run_scenario(scenario, _git_like_adapter())


def test_scenario_names_are_unique() -> None:
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names))


def _readme_table() -> dict[str, set[str]]:
    """``operation -> scenarios`` from the README coverage table."""
    rows: dict[str, set[str]] = {}
    in_table = False
    for line in README.read_text(encoding="utf-8").splitlines():
        if line.startswith("| Operation | Contract scenarios |"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            cells = [c.strip() for c in line.strip("|").split("|")]
            if set(cells[0]) <= {"-", " "}:
                continue
            operation = cells[0].strip("`")
            rows[operation] = set(re.findall(r"`([a-z0-9_]+)`", cells[1]))
    return rows


def test_readme_coverage_table_maps_one_to_one_to_the_scenarios() -> None:
    table = _readme_table()
    covered: dict[str, set[str]] = {}
    for scenario in SCENARIOS:
        for operation in scenario.operations:
            covered.setdefault(operation, set()).add(scenario.name)
    assert table == covered


def test_every_port_operation_has_a_contract_scenario() -> None:
    import beadhive_hq_storage as port

    protocols = (
        port.VersionedRecords,
        port.AppendLog,
        port.Liveness,
        port.CommittedHistory,
        port.HiveFence,
        port.EpochRefStore,
        port.PlacementStore,
        port.CapabilityHost,
    )
    operations = {
        f"{proto.__name__}.{name}"
        for proto in protocols
        for name in vars(proto)
        if not name.startswith("_") and callable(vars(proto)[name])
    }
    covered = {op for scenario in SCENARIOS for op in scenario.operations}
    assert operations <= covered, sorted(operations - covered)
