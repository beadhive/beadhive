"""The differential state machine: fake against fake (the self-check), and a mutant it must
catch. Runs under the ``gate`` profile unless ``BH_HYPOTHESIS_PROFILE`` says otherwise."""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import settings
from hypothesis.stateful import run_state_machine_as_test

from beadhive_hq_storage import Applied, InMemoryHqStorage
from beadhive_hq_storage.contract import (
    ContractAdapter,
    FakeFenceHarness,
    FakeHqHarness,
    build_state_machine,
    fake_adapter,
    no_shrink_phases,
)

TestFakeAgainstFake = build_state_machine(fake_adapter(), fake_adapter()).TestCase


class _StickyRevisionStore(InMemoryHqStorage):
    """A broken adapter: a CAS over an existing record keeps its token (condition 5 violated)."""

    def compare_and_set(self, kind, key, body, *, expected, operation):  # type: ignore[override]
        out = super().compare_and_set(kind, key, body, expected=expected, operation=operation)
        if isinstance(out, Applied) and isinstance(expected, str):
            record = self._records[(kind, key)]
            record.versioned = replace(record.versioned, revision=expected)
            return Applied(expected, operation)
        return out


class _StickyHarness(FakeHqHarness):
    def __init__(self) -> None:
        super().__init__()
        self.store = _StickyRevisionStore(self.clock)
        self.records = self.log = self.liveness = self.history = self.host = self.store
        self.placements = self.store.placements()


def test_machine_catches_an_adapter_that_reuses_revisions() -> None:
    mutant = ContractAdapter("sticky", _StickyHarness, FakeFenceHarness, clock_controlled=True)
    machine = build_state_machine(fake_adapter(), mutant)
    budget = settings(
        settings.get_profile("gate"),
        max_examples=200,
        stateful_step_count=20,
        phases=no_shrink_phases(),
    )
    with pytest.raises(AssertionError, match="disagree|diverged"):
        run_state_machine_as_test(machine, settings=budget)
