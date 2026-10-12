"""The shared port contract suite, parametrized over ``[fake, dolt]``, and the differential state
machine fake against real Dolt (hq-storage-port-adr section 8).

The ``dolt`` parameter self-skips without a ``dolt`` binary (or without the PyMySQL driver the
test injects; the adapter itself never imports one). One owned ``dolt sql-server`` per module and
xdist worker holds one ``BH_DOLT_SLOTS`` slot; every case gets fresh databases on it.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from beadhive_hq_storage.contract import (
    GATE_PROFILE,
    ContractAdapter,
    Scenario,
    adapter_fixture,
    build_state_machine,
    fake_adapter,
    load_hypothesis_profile,
    no_shrink_phases,
    parametrize_scenarios,
    run_scenario,
)
from beadhive_hq_storage_dolt.contract import DoltSqlServer, dolt_adapter

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server]

PROFILE = load_hypothesis_profile()
DOLT_MAX_EXAMPLES = 100
"""The real-Dolt machine's per-run cap outside the gate (settled by bh-br1cd): every step is a
real SQL round trip, so the nightly profile's 500 examples would run for most of an hour. The cap
bounds the budget only; the comparison is never weakened."""

DOLT_SMOKE_EXAMPLES = 2
DOLT_SMOKE_STEPS = 8
"""The gate's fake-against-Dolt examples (bh-xxkf7.3); the full budget runs out of the gate."""

_servers: list[DoltSqlServer] = []


def _server() -> DoltSqlServer:
    """The module's server, started on first use (so a fake-only selection starts none)."""
    if shutil.which("dolt") is None:
        pytest.skip("dolt binary unavailable")
    pymysql = pytest.importorskip("pymysql")
    if not _servers:
        import tempfile

        base = Path(tempfile.mkdtemp(prefix="bh-hq-storage-dolt-"))
        _servers.append(DoltSqlServer(base, pymysql.connect).__enter__())
    return _servers[0]


@pytest.fixture(scope="module", autouse=True)
def _stop_server() -> Iterator[None]:
    yield
    while _servers:
        server = _servers.pop()
        try:
            server.__exit__(None, None, None)
        finally:
            shutil.rmtree(server.base, ignore_errors=True)  # /tmp may be RAM-backed


def _dolt() -> ContractAdapter:
    return dolt_adapter(_server())


adapter = adapter_fixture({"fake": fake_adapter, "dolt": _dolt})


@parametrize_scenarios()
def test_contract(scenario: Scenario, adapter: ContractAdapter) -> None:
    run_scenario(scenario, adapter)


def _lazy_dolt() -> ContractAdapter:
    """The ``dolt`` adapter, resolving the server only when the machine builds a side."""
    return ContractAdapter(
        id="dolt",
        make_hq=lambda: dolt_adapter(_server()).make_hq(),
        make_fence=lambda cut_over: dolt_adapter(_server()).make_fence(cut_over),
        clock_controlled=False,
    )


def _run_dolt_machine(budget_for_gate: dict[str, int] | None) -> None:
    """Run the fake-against-Dolt machine. ``budget_for_gate`` overrides the examples under the
    gate profile (the smoke); ``None`` keeps the gate profile's full budget. The gate profile
    never shrinks against real Dolt; any other profile shrinks, capped at DOLT_MAX_EXAMPLES."""
    from hypothesis import settings
    from hypothesis.stateful import run_state_machine_as_test

    _server()  # skip before hypothesis starts when Dolt is unavailable
    machine = build_state_machine(fake_adapter(), _lazy_dolt())
    if PROFILE == GATE_PROFILE:
        budget = settings(settings.default, phases=no_shrink_phases(), **(budget_for_gate or {}))
    else:
        budget = settings(
            settings.default,
            max_examples=min(settings.default.max_examples, DOLT_MAX_EXAMPLES),
        )
    run_state_machine_as_test(machine, settings=budget)


def test_state_machine_fake_against_dolt_smoke() -> None:
    """The gate's real-Dolt slice: a few derandomized, unshrunk examples (bh-xxkf7.3).
    The full budget is ``dolt_machine_full`` below, run by ``just hq-storage-dolt-machine``."""
    _run_dolt_machine(
        {"max_examples": DOLT_SMOKE_EXAMPLES, "stateful_step_count": DOLT_SMOKE_STEPS}
    )


@pytest.mark.dolt_machine_full
def test_state_machine_fake_against_dolt_full() -> None:
    """The full fake-against-Dolt budget (the gate profile's 25 x 20, or the nightly cap),
    deselected from the gate and run by ``just hq-storage-dolt-machine`` (bh-xxkf7.3)."""
    _run_dolt_machine(None)
