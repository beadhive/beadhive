"""Conformance kit for ``ImpactBackend`` implementations (``beadhive.testing.impact``).

The shared definition of done for build-system impact backends (Attested Green ADR,
Amendment 1): a backend is conforming when it passes :data:`IMPACT_CASES` against the fixture
repository from :func:`build_impact_fixture`. Each case drives the backend through core's
:class:`~beadhive.modules.work.application.impact.FailClosedResolver`, so the five fail-closed
rules stay enforced in core; the kit checks the resulting receipts and the backend's raw answers.

A backend package supplies an :class:`ImpactHarness` whose ``build`` returns a fresh backend with
its tool query injected (answering for the fixture), then runs::

    @pytest.mark.parametrize("case", IMPACT_CASES, ids=lambda case: case.case_id)
    def test_my_backend_conforms(case):
        run_impact_case(my_harness(), case)

Real-tool runs belong in an optional, separately marked lane of the backend's own tests.

Unlike the rest of :mod:`beadhive.testing`, this subpackage imports the work module's impact
domain, contracts, and ``FailClosedResolver``: it tests that contract, so it needs it.
"""

from .cases import (
    IMPACT_CASES,
    ImpactHarness,
    Resolution,
    ScenarioSetup,
    assert_impact_backend_conforms,
    assert_receipt_conforms,
    assert_receipt_digest_stable,
    failing_cases,
    impact_case,
    resolve_scenario,
    run_impact_case,
)
from .fixture import (
    BASE_REV,
    BASE_TREE,
    DEFAULT_GLOBAL_INPUT,
    HEAD_REV,
    HEAD_TREE,
    UNIT_KINDS,
    FixtureTreeDiff,
    FixtureUnit,
    ImpactFixture,
    ImpactScenario,
    build_impact_fixture,
    default_selector,
)
from .reference import (
    FAULT_ERROR,
    FAULT_TIMEOUT,
    FAULT_VERSION_MISMATCH,
    FAULTS,
    REQUIRED_FAULTS,
    ReferenceBackend,
)

__all__ = (
    "BASE_REV",
    "BASE_TREE",
    "DEFAULT_GLOBAL_INPUT",
    "FAULTS",
    "FAULT_ERROR",
    "FAULT_TIMEOUT",
    "FAULT_VERSION_MISMATCH",
    "HEAD_REV",
    "HEAD_TREE",
    "IMPACT_CASES",
    "REQUIRED_FAULTS",
    "UNIT_KINDS",
    "FixtureTreeDiff",
    "FixtureUnit",
    "ImpactFixture",
    "ImpactHarness",
    "ImpactScenario",
    "ReferenceBackend",
    "Resolution",
    "ScenarioSetup",
    "assert_impact_backend_conforms",
    "assert_receipt_conforms",
    "assert_receipt_digest_stable",
    "build_impact_fixture",
    "default_selector",
    "failing_cases",
    "impact_case",
    "resolve_scenario",
    "run_impact_case",
)
