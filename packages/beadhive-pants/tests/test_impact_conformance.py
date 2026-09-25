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
from beadhive_pants import impact as impact_pants
from beadhive_pants.impact import PantsImpactBackend

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
    manifest = tmp_path / "scripts" / "pants_proven_tests.json"

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

        return PantsImpactBackend(tmp_path, manifest=manifest, query=query, sleeper=lambda _: None)

    return ImpactHarness(
        backend="pants",
        build=build,
        repo=str(tmp_path),
        global_input="pants.toml",
        unit_id=address,
    )


@pytest.mark.parametrize(
    "case",
    IMPACT_CASES,
    ids=lambda case: case.case_id,
)
def test_pants_backend_conforms(case, pants_harness):
    run_impact_case(pants_harness, case)
