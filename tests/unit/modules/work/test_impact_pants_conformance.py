"""PantsImpactBackend against the beadhive.testing.impact conformance kit.

The harness translates the kit's fixture into what Pants would answer — ``peek ::`` rows for the
graph and ``--changed-since ... peek`` rows for the transitive dependents — and injects it
through the backend's ``PantsQuery`` callable, so no Pants process runs.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from beadhive.adapters import impact_pants
from beadhive.adapters.impact_pants import PantsImpactBackend
from beadhive.testing import ConformanceFailure
from beadhive.testing.impact import (
    FAULT_ERROR,
    FAULT_TIMEOUT,
    HEAD_TREE,
    IMPACT_CASES,
    ImpactFixture,
    ImpactHarness,
    ScenarioSetup,
    run_impact_case,
)

PANTS_VERSION = "2.32.1"
CATEGORIES = {"code": "code", "test": "test-only", "docs": "docs", "build": "build-system"}


def address(unit_id: str) -> str:
    return f"fixture:{unit_id}"


def peek_rows(fixture: ImpactFixture, unit_ids) -> list[dict]:
    return [
        {
            "address": address(unit.id),
            "sources": list(unit.sources),
            "tags": [f"category:{CATEGORIES[unit.kind]}", *fixture.tags(unit)],
            "target_type": "python_test" if unit.kind == "test" else "python_source",
        }
        for unit in fixture.units
        if unit.id in unit_ids
    ]


@pytest.fixture
def pants_harness(tmp_path, monkeypatch) -> ImpactHarness:
    (tmp_path / "pants.toml").write_text(f'[GLOBAL]\npants_version = "{PANTS_VERSION}"\n')

    def head_tree(command, **kwargs):
        assert command[-2:] == ["rev-parse", "HEAD^{tree}"], command
        return subprocess.CompletedProcess(command, 0, f"{HEAD_TREE}\n", "")

    # Only the backend's own checkout probe is faked; the kit and core never spawn a process.
    monkeypatch.setattr(impact_pants, "subprocess", SimpleNamespace(run=head_tree))
    manifest_written = False

    def build(setup: ScenarioSetup) -> PantsImpactBackend:
        nonlocal manifest_written
        fixture, fault = setup.fixture, setup.fault
        if not manifest_written:
            proven = {
                source: {"status": "proven", "dependencies": ["fixture"]}
                for unit in fixture.units
                if unit.kind == "test" and unit.proven
                for source in unit.sources
            }
            manifest = tmp_path / "scripts" / "pants_proven_tests.json"
            manifest.parent.mkdir(exist_ok=True)
            manifest.write_text(json.dumps({"schema_version": 1, "tests": proven}))
            manifest_written = True

        def query(repo, args, timeout):
            if fault == FAULT_ERROR:
                raise RuntimeError("Pants peek failed (1): fixture engine unavailable")
            if fault == FAULT_TIMEOUT:
                raise TimeoutError(f"Pants query exceeded {timeout:g}s")
            if tuple(args) == ("peek", "::"):
                return peek_rows(fixture, {unit.id for unit in fixture.units})
            assert args[0].startswith("--changed-since=") and args[1:] == (
                "--changed-dependents=transitive",
                "peek",
            ), args
            return peek_rows(fixture, fixture.affected_units(setup.scenario.changes))

        return PantsImpactBackend(tmp_path, query=query, sleeper=lambda _: None)

    return ImpactHarness(
        backend="pants",
        build=build,
        repo=str(tmp_path),
        global_input="pants.toml",
        unit_id=address,
    )


# PantsImpactBackend checks proof only for *affected* test units, so a key whose unproven test
# the change does not reach is reported proven and carries. Amendment 1 rule 4 says an unproven
# key is invalidated by any change. Strict: this starts failing once the backend is fixed.
KNOWN_GAPS = {
    "rule4.unproven-any-change": (
        "PantsImpactBackend proves keys over affected units only; an unaffected unproven test "
        "unit carries its key (Amendment 1 rule 4 gap)"
    ),
}


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            case,
            marks=pytest.mark.xfail(
                strict=True, raises=ConformanceFailure, reason=KNOWN_GAPS[case.case_id]
            ),
        )
        if case.case_id in KNOWN_GAPS
        else case
        for case in IMPACT_CASES
    ],
    ids=lambda case: case.case_id,
)
def test_pants_backend_conforms(case, pants_harness):
    run_impact_case(pants_harness, case)
