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

load_hypothesis_profile()

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


def test_state_machine_fake_against_dolt() -> None:
    """The gate profile's derandomized budget, with shrinking excluded (ADR section 8)."""
    from hypothesis import settings
    from hypothesis.stateful import run_state_machine_as_test

    _server()  # skip before hypothesis starts when Dolt is unavailable
    machine = build_state_machine(fake_adapter(), _lazy_dolt())
    run_state_machine_as_test(
        machine, settings=settings(settings.default, phases=no_shrink_phases())
    )
