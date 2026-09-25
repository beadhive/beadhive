"""The impact-backend conformance kit accepts a correct backend and rejects broken ones.

Each broken backend below is the reference backend with one Amendment 1 fail-closed rule
violated in its raw answer. The kit must reject every one of them on exactly the cases of the
rule it breaks, and nothing else, so a rejection always names the right rule.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from beadhive.modules.work.domain.impact import BackendImpact, ImpactRequest
from beadhive.testing import ConformanceFailure
from beadhive.testing.impact import (
    FAULTS,
    IMPACT_CASES,
    ImpactHarness,
    ReferenceBackend,
    ScenarioSetup,
    assert_impact_backend_conforms,
    build_impact_fixture,
    failing_cases,
    run_impact_case,
)


def reference_harness(backend_class=ReferenceBackend) -> ImpactHarness:
    def build(setup: ScenarioSetup):
        return backend_class(setup.fixture, fault=setup.fault)

    return ImpactHarness(backend="reference", build=build, faults=FAULTS)


class CatchAllOwner(ReferenceBackend):
    """Rule 1: claims a catch-all owner for paths nothing owns."""

    def analyze(self, request: ImpactRequest) -> BackendImpact:
        raw = super().analyze(request)
        owners = {path: units or ("catch-all",) for path, units in raw.owners.items()}
        return dataclasses.replace(raw, owners=owners)


class DeletionThroughHead(ReferenceBackend):
    """Rule 1: attributes deleted paths to an unrelated head-tree unit, not their base owner."""

    def analyze(self, request: ImpactRequest) -> BackendImpact:
        raw = super().analyze(request)
        owners = dict(raw.owners)
        for change in request.changed:
            if change.deleted:
                owners[change.path] = ("docs",)
        return dataclasses.replace(raw, owners=owners)


class NoGlobalInputs(ReferenceBackend):
    """Rule 2: forgets its global build inputs."""

    def analyze(self, request: ImpactRequest) -> BackendImpact:
        return dataclasses.replace(super().analyze(request), global_inputs=())


class SwallowsToolFaults(ReferenceBackend):
    """Rule 3: turns a failed or mismatched tool into a partial answer instead of failing."""

    def analyze(self, request: ImpactRequest) -> BackendImpact:
        fault, self.fault = self.fault, None
        raw = super().analyze(request)
        if fault is None:
            return raw
        return dataclasses.replace(raw, affected_units=frozenset())  # the dependents query failed


class ProvesEverything(ReferenceBackend):
    """Rule 4: reports every key with selected units as proven."""

    def analyze(self, request: ImpactRequest) -> BackendImpact:
        raw = super().analyze(request)
        proven = frozenset(name for name, units in raw.key_units.items() if units)
        return dataclasses.replace(raw, proven_keys=proven)


class InventsUnitsForUnmatchedSelectors(ReferenceBackend):
    """Rule 4: maps a selector that matches nothing onto some unit and calls the key proven."""

    def analyze(self, request: ImpactRequest) -> BackendImpact:
        raw = super().analyze(request)
        key_units = dict(raw.key_units)
        proven = set(raw.proven_keys)
        for key in request.keys:
            if key.selector(self.name) is not None and not key_units.get(key.name):
                key_units[key.name] = ("docs",)
                proven.add(key.name)
        return dataclasses.replace(raw, key_units=key_units, proven_keys=frozenset(proven))


class GuessesSelectors(ReferenceBackend):
    """Rule 5: falls back to a name-convention selector for a key that has none."""

    def analyze(self, request: ImpactRequest) -> BackendImpact:
        raw = super().analyze(request)
        fixture = self.fixture
        key_units = dict(raw.key_units)
        proven = set(raw.proven_keys)
        for key in request.keys:
            if key.selector(self.name) is None:
                tag = fixture.selector(key.name)
                units = tuple(u.id for u in fixture.units if tag in fixture.tags(u))
                key_units[key.name] = units
                if units:
                    proven.add(key.name)
        return dataclasses.replace(raw, key_units=key_units, proven_keys=frozenset(proven))


BROKEN = {
    "rule1": (CatchAllOwner, DeletionThroughHead),
    "rule2": (NoGlobalInputs,),
    "rule3": (SwallowsToolFaults,),
    "rule4": (ProvesEverything, InventsUnitsForUnmatchedSelectors),
    "rule5": (GuessesSelectors,),
}


def rule_cases(rule: str) -> tuple[str, ...]:
    return tuple(case.case_id for case in IMPACT_CASES if case.case_id.startswith(f"{rule}."))


def test_kit_covers_all_five_fail_closed_rules():
    groups = {case.case_id.split(".", 1)[0] for case in IMPACT_CASES}
    assert {f"rule{n}" for n in range(1, 6)} <= groups
    assert {"port", "receipt", "graph"} <= groups


@pytest.mark.parametrize("case", IMPACT_CASES, ids=lambda case: case.case_id)
def test_reference_backend_conforms(case):
    run_impact_case(reference_harness(), case)


def test_reference_backend_conforms_in_one_call():
    assert_impact_backend_conforms(reference_harness())


@pytest.mark.parametrize(
    ("rule", "backend_class"),
    [(rule, cls) for rule, classes in BROKEN.items() for cls in classes],
    ids=lambda value: value if isinstance(value, str) else value.__name__,
)
def test_broken_backend_is_rejected_on_exactly_its_rule(rule, backend_class):
    harness = reference_harness(backend_class)
    failed = failing_cases(harness)

    assert failed, f"{backend_class.__name__} passed the kit"
    assert {case_id.split(".", 1)[0] for case_id in failed} == {rule}, failed
    with pytest.raises(ConformanceFailure) as rejected:
        assert_impact_backend_conforms(harness)
    assert rejected.value.case_id == failed[0]
    assert rejected.value.case_id in rule_cases(rule)


def test_every_rule_case_rejects_some_broken_backend():
    """No rule case is vacuous: each one is what rejects at least one broken backend, except
    the core-enforced slow-answer check that no backend answer can break."""
    rejecting = {
        case_id
        for classes in BROKEN.values()
        for cls in classes
        for case_id in failing_cases(reference_harness(cls))
    }
    rule_ids = {c.case_id for c in IMPACT_CASES if c.case_id.startswith("rule")}
    assert rule_ids - rejecting == {"rule3.slow-answer"}


def test_harness_must_inject_error_and_timeout_faults():
    with pytest.raises(ValueError, match="must inject faults"):
        ImpactHarness(backend="x", build=lambda setup: None, faults=frozenset({"error"}))
    with pytest.raises(ValueError, match="unknown faults"):
        ImpactHarness(backend="x", build=lambda setup: None, faults=FAULTS | {"flaky"})


def test_fixture_ground_truth():
    fixture = build_impact_fixture("reference")
    chain = fixture.scenario("chain")
    assert fixture.affected_units(chain.changes) == {"core", "service", "cli", "cli-tests"}
    assert fixture.graph_invalidated(chain) == {"unit"}
    assert fixture.owners(fixture.scenario("deletion").changes[0]) == ("service",)
    assert fixture.selected_units("release-pin") == ()
    assert fixture.selected_units("orphan") == ()
    assert "stray/notes.txt" in fixture.tree_paths("head")
    assert "app/helpers.py" in fixture.tree_paths("base")
    assert "app/helpers.py" not in fixture.tree_paths("head")
    with pytest.raises(ValueError, match="collides"):
        build_impact_fixture("reference", global_input="app/core.py")


def test_kit_imports_only_the_impact_contract_and_the_testing_package():
    """A backend package imports the kit through the ``beadhive.testing`` allowlist entry; the kit
    must not drag adapters, bootstrap, or another module's internals in behind it."""
    import beadhive.testing.impact as kit

    allowed = {
        "beadhive.modules.work.domain.impact",
        "beadhive.modules.work.contracts.impact",
        "beadhive.modules.work.application.impact",
    }
    package = Path(kit.__file__).parent
    imported = set()
    for source in package.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level:
                base = "beadhive.testing.impact".split(".")[: 3 - (node.level - 1)]
                imported.add(".".join([*base, node.module or ""]).rstrip("."))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [node.module]
                    if isinstance(node, ast.ImportFrom)
                    else [alias.name for alias in node.names]
                )
                imported.update(name for name in names if name and name.startswith("beadhive"))
    beadhive_imports = {name for name in imported if name.startswith("beadhive")}
    outside = {
        name
        for name in beadhive_imports
        if name not in allowed and not name.startswith("beadhive.testing")
    }
    assert not outside, sorted(outside)
    assert allowed <= beadhive_imports
