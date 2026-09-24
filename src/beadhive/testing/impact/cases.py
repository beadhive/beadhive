"""The conformance cases every :class:`ImpactBackend` must pass, and the harness that runs them.

Every case drives the backend through :class:`FailClosedResolver`, exactly as production does,
so the fail-closed rules stay enforced in core. A case then checks two things:

- the **receipt**: what the rule guarantees end to end (and its shape and digest, always);
- the backend's **raw answer**, recorded on its way into core: whether the backend answered the
  three questions truthfully for the fixture. Core cannot catch every dishonest answer (it cannot
  know that a path claimed as owned is really unowned), so this is where a backend is held to
  the contract it shares with Pants, Turborepo, and Bazel.

Case ids are ``<group>.<name>``; the group is ``port``, ``receipt``, ``graph``, or ``rule1`` ..
``rule5`` for the Amendment 1 fail-closed rule the case proves.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from ...modules.work.application.impact import FailClosedResolver
from ...modules.work.contracts.impact import ImpactBackend
from ...modules.work.domain.impact import (
    NATIVE_FULL,
    NATIVE_FULL_VERSION,
    RECEIPT_SCHEMA,
    BackendImpact,
    ImpactReason,
    ImpactReceipt,
    ImpactRequest,
    ReceiptIntegrityError,
)
from ..conformance import PortCase, assert_port_conforms
from .fixture import (
    BASE_REV,
    CHAIN,
    CHAIN_ROOT,
    DEFAULT_GLOBAL_INPUT,
    DELETED_PATH,
    DELETION,
    GLOBAL,
    HEAD_REV,
    KEY_DOCS,
    KEY_GIT_METADATA,
    KEY_LEGACY,
    KEY_ORPHAN,
    KEY_UNIT,
    LEAF,
    RENAME,
    RENAMED_FROM,
    RENAMED_TO,
    UNCHANGED,
    UNOWNED,
    UNOWNED_PATH,
    UNPROVEN_EDIT,
    FixtureTreeDiff,
    ImpactFixture,
    ImpactScenario,
    build_impact_fixture,
    default_selector,
)
from .reference import FAULT_ERROR, FAULT_TIMEOUT, FAULT_VERSION_MISMATCH, FAULTS, REQUIRED_FAULTS


def _require(condition: object, message: str = "conformance check failed") -> None:
    """``assert`` that survives ``python -O``: a stripped check would pass every backend."""
    if not condition:
        raise AssertionError(message)


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REASONS = frozenset(
    value for name, value in vars(ImpactReason).items() if not name.startswith("_")
)


@dataclass(frozen=True)
class ScenarioSetup:
    """What a harness's ``build`` receives: the fixture, the scenario about to be resolved, and
    the fault (if any) its tool query must simulate. Build a fresh backend for every call."""

    fixture: ImpactFixture
    scenario: ImpactScenario
    fault: str | None = None


@dataclass(frozen=True)
class ImpactHarness:
    """How the kit reaches one backend implementation.

    - ``backend``: the backend's ``name`` (and the fixture's selector namespace).
    - ``build``: returns a fresh backend whose injected tool query answers for the setup.
    - ``repo``: the repository path passed to the resolver (a backend may check it is bound there).
    - ``selector`` / ``global_input``: the backend's native selector shape and global input path.
    - ``unit_id``: maps a fixture unit id to the backend's unit id (a Pants address, ...).
    - ``faults``: faults ``build`` can inject; error and timeout are required.
    """

    backend: str
    build: Callable[[ScenarioSetup], ImpactBackend]
    repo: str = "/beadhive-impact-fixture"
    selector: Callable[[str], str] = default_selector
    global_input: str = DEFAULT_GLOBAL_INPUT
    unit_id: Callable[[str], str] = field(default=lambda unit: unit)
    faults: frozenset[str] = REQUIRED_FAULTS
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "faults", frozenset(self.faults))
        unknown = self.faults - FAULTS
        if unknown:
            raise ValueError(f"unknown faults {sorted(unknown)}")
        missing = REQUIRED_FAULTS - self.faults
        if missing:
            raise ValueError(f"an impact harness must inject faults {sorted(missing)}")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    @property
    def name(self) -> str:  # names the subject in a ConformanceFailure
        return self.backend

    def fixture(self) -> ImpactFixture:
        return build_impact_fixture(
            self.backend, selector=self.selector, global_input=self.global_input
        )


class _ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Recorder:
    """Passes the backend's answer through unchanged, keeping a copy for the raw-answer checks.
    ``delay`` advances the resolver's clock during the call (a slow answer, rule 3)."""

    def __init__(self, backend: ImpactBackend, clock: _ManualClock, delay: float) -> None:
        self.name = backend.name
        self.version = backend.version
        self._backend = backend
        self._clock = clock
        self._delay = delay
        self.requests: list[ImpactRequest] = []
        self.answers: list[BackendImpact] = []

    def analyze(self, request: ImpactRequest) -> BackendImpact:
        self.requests.append(request)
        try:
            answer = self._backend.analyze(request)
        finally:
            self._clock.now += self._delay
        self.answers.append(answer)
        return answer


@dataclass(frozen=True)
class Resolution:
    """One scenario resolved through core: the receipt, and the raw answer if there was one."""

    fixture: ImpactFixture
    scenario: ImpactScenario
    receipt: ImpactReceipt
    raw: BackendImpact | None
    asked: int

    def require_raw(self) -> BackendImpact:
        if self.raw is None:
            raise AssertionError(f"{self.scenario.name}: the backend gave no answer")
        return self.raw


def resolve_scenario(
    harness: ImpactHarness, scenario: str, *, fault: str | None = None, slow: bool = False
) -> Resolution:
    """Resolve one fixture scenario with a fresh backend through :class:`FailClosedResolver`, and
    assert the receipt's shape and digest before returning it."""
    fixture = harness.fixture()
    chosen = fixture.scenario(scenario)
    backend = harness.build(ScenarioSetup(fixture, chosen, fault))
    clock = _ManualClock()
    recorder = _Recorder(backend, clock, harness.timeout_seconds + 1 if slow else 0.0)
    resolver = FailClosedResolver(
        recorder, FixtureTreeDiff(chosen), timeout_seconds=harness.timeout_seconds, clock=clock
    )
    receipt = resolver.resolve(harness.repo, BASE_REV, HEAD_REV, fixture.keys)
    assert_receipt_conforms(
        receipt,
        fixture=fixture,
        scenario=chosen,
        backend=backend.name,
        backend_version=backend.version,
    )
    return Resolution(
        fixture=fixture,
        scenario=chosen,
        receipt=receipt,
        raw=recorder.answers[-1] if recorder.answers else None,
        asked=len(recorder.requests),
    )


# --- receipt shape and digest ------------------------------------------------------------------


def assert_receipt_digest_stable(receipt: ImpactReceipt) -> None:
    """The digest is well-formed, survives a JSON round trip, ignores wall time, and refuses
    tampered content."""
    digest = receipt.digest
    _require(_DIGEST.fullmatch(digest), f"malformed receipt digest {digest!r}")
    restored = ImpactReceipt.from_json(receipt.to_json())
    _require(restored == receipt, "receipt did not survive a JSON round trip")
    _require(restored.digest == digest, "receipt digest changed across a JSON round trip")
    retimed = dataclasses.replace(receipt, elapsed_ms=receipt.elapsed_ms + 1000)
    _require(retimed.digest == digest, "receipt digest depends on elapsed time")
    tampered = receipt.to_dict()
    tampered["fallback_reason"] = receipt.fallback_reason + "tampered"
    try:
        ImpactReceipt.from_dict(tampered)
    except ReceiptIntegrityError:
        pass
    else:
        raise AssertionError("a tampered receipt passed its digest check")


def assert_receipt_conforms(
    receipt: ImpactReceipt,
    *,
    fixture: ImpactFixture,
    scenario: ImpactScenario,
    backend: str,
    backend_version: str,
) -> None:
    """The receipt's shape for one resolved scenario: identity, trees, a complete and disjoint
    key partition with evidence for every key, fallback coherence, and a stable digest."""
    names = set(fixture.key_names)
    _require(receipt.schema == RECEIPT_SCHEMA, f"receipt schema {receipt.schema!r}")
    _require(
        (receipt.base_tree, receipt.head_tree) == (scenario.base_tree, scenario.head_tree),
        "receipt trees differ from the scenario's",
    )
    _require(
        receipt.changed_paths == scenario.changed_paths,
        f"receipt changed paths {receipt.changed_paths} != {scenario.changed_paths}",
    )
    invalidated, unaffected = set(receipt.invalidated_keys), set(receipt.unaffected_keys)
    _require(not invalidated & unaffected, "a key is both invalidated and unaffected")
    _require(
        invalidated | unaffected == names,
        f"receipt partitions {sorted(invalidated | unaffected)}, not every key {sorted(names)}",
    )
    _require(set(receipt.evidence) == names, "receipt lacks evidence for some key")
    unknown = {e.reason for e in receipt.evidence.values()} - _REASONS
    _require(not unknown, f"unknown evidence reasons {sorted(unknown)}")
    _require(set(receipt.unowned_paths) <= set(receipt.changed_paths), "unowned path never changed")
    _require(
        set(receipt.global_inputs_hit) <= set(receipt.changed_paths), "global hit never changed"
    )
    if receipt.unowned_paths or receipt.global_inputs_hit:
        _require(invalidated == names, "an unowned path or global input left a key unaffected")
    for name in unaffected:
        _require(receipt.evidence[name].reason in {ImpactReason.UNAFFECTED, ImpactReason.UNCHANGED})
        _require(receipt.is_unaffected(name), f"unaffected key {name!r} is not vouched for")
    for name in invalidated:
        _require(not receipt.is_unaffected(name), f"invalidated key {name!r} is vouched for")
    if receipt.is_fallback:
        _require((receipt.backend, receipt.backend_version) == (NATIVE_FULL, NATIVE_FULL_VERSION))
        _require(
            backend in receipt.fallback_reason,
            f"fallback reason {receipt.fallback_reason!r} does not name backend {backend!r}",
        )
        if scenario.changes:
            _require(invalidated == names, "a fallback receipt left a key unaffected")
    else:
        _require(
            (receipt.backend, receipt.backend_version) == (backend, backend_version),
            f"receipt names {receipt.backend} {receipt.backend_version}, "
            f"not {backend} {backend_version}",
        )
    assert_receipt_digest_stable(receipt)


# --- helpers shared by the cases -----------------------------------------------------------------


def _all_invalidated(resolution: Resolution, reason: str) -> None:
    receipt = resolution.receipt
    names = set(resolution.fixture.key_names)
    _require(
        set(receipt.invalidated_keys) == names,
        f"{resolution.scenario.name}: left {list(receipt.unaffected_keys)} unaffected",
    )
    wrong = {k: e.reason for k, e in receipt.evidence.items() if e.reason != reason}
    _require(not wrong, f"{resolution.scenario.name}: expected {reason!r} evidence, got {wrong}")


def _invalidated(resolution: Resolution, key: str) -> None:
    receipt = resolution.receipt
    _require(
        key in receipt.invalidated_keys,
        f"{resolution.scenario.name}: key {key!r} carried "
        f"({receipt.evidence[key].reason}, fallback={receipt.fallback_reason!r})",
    )


def _fallback(resolution: Resolution, *, containing: str) -> None:
    receipt = resolution.receipt
    _require(
        receipt.is_fallback,
        f"{resolution.scenario.name}: expected a native-full fallback, got a "
        f"{receipt.backend} answer carrying {list(receipt.unaffected_keys)}",
    )
    _require(
        containing in receipt.fallback_reason.lower(),
        f"fallback reason {receipt.fallback_reason!r} does not mention {containing!r}",
    )
    _all_invalidated(resolution, ImpactReason.FALLBACK)


def _owners_via_base(harness: ImpactHarness, resolution: Resolution, path: str, owner: str) -> None:
    """A deleted path is attributed through the base tree or not at all — never elsewhere."""
    claimed = set(resolution.require_raw().owners.get(path) or ())
    _require(
        not claimed or harness.unit_id(owner) in claimed,
        f"deleted {path!r} attributed to {sorted(claimed)}, not its base owner "
        f"{harness.unit_id(owner)!r}",
    )


def _not_proven(resolution: Resolution, key: str) -> None:
    raw = resolution.require_raw()
    _require(
        key not in raw.proven_keys,
        f"{resolution.scenario.name}: backend claims key {key!r} proven, but it selects an "
        "unproven unit",
    )


def _no_units_for(resolution: Resolution, key: str) -> None:
    raw = resolution.require_raw()
    units = tuple(raw.key_units.get(key) or ())
    _require(not units, f"backend selected {units} for key {key!r}, which has no selector for it")
    _require(key not in raw.proven_keys, f"backend claims selector-less key {key!r} proven")


# --- the cases -----------------------------------------------------------------------------------


def _port_runtime_shape(h: ImpactHarness) -> None:
    fixture = h.fixture()
    backend = h.build(ScenarioSetup(fixture, fixture.scenario(LEAF)))
    _require(isinstance(backend, ImpactBackend), "does not implement ImpactBackend")
    _require(backend.name == h.backend, f"backend name {backend.name!r} != {h.backend!r}")
    _require(backend.name != NATIVE_FULL, f"{NATIVE_FULL!r} is reserved for the default resolver")
    _require(isinstance(backend.version, str) and backend.version, "backend version must be set")


def _receipt_unchanged_trees(h: ImpactHarness) -> None:
    resolution = resolve_scenario(h, UNCHANGED)
    _require(not resolution.receipt.is_fallback)
    _require(resolution.receipt.unaffected_keys == tuple(sorted(resolution.fixture.key_names)))
    _require({e.reason for e in resolution.receipt.evidence.values()} == {ImpactReason.UNCHANGED})


def _receipt_every_scenario(h: ImpactHarness) -> None:
    for name in h.fixture().scenarios:
        resolve_scenario(h, name)  # asserts shape and digest


def _receipt_deterministic(h: ImpactHarness) -> None:
    first, second = resolve_scenario(h, CHAIN), resolve_scenario(h, CHAIN)
    _require(
        first.receipt.digest == second.receipt.digest,
        "two fresh backends answered the same scenario with different receipts",
    )


def _graph_answers(h: ImpactHarness) -> None:
    for name in h.fixture().scenarios:
        receipt = resolve_scenario(h, name).receipt
        _require(not receipt.is_fallback, f"{name}: fell back ({receipt.fallback_reason})")


def _graph_transitive_dependents(h: ImpactHarness) -> None:
    resolution = resolve_scenario(h, CHAIN)
    evidence = resolution.receipt.evidence[KEY_UNIT]
    _require(
        (evidence.reason, evidence.units) == (ImpactReason.AFFECTED, (h.unit_id("cli-tests"),))
    )
    raw = resolution.require_raw()
    _require(h.unit_id("core") in (raw.owners.get(CHAIN_ROOT) or ()), "chain root owner missing")
    missing = {h.unit_id(u) for u in ("service", "cli", "cli-tests")} - set(raw.affected_units)
    _require(not missing, f"transitive dependents missing from affected units: {sorted(missing)}")


def _graph_carries_unaffected(h: ImpactHarness) -> None:
    for scenario, key, unit in ((CHAIN, KEY_DOCS, "docs"), (LEAF, KEY_UNIT, "cli-tests")):
        evidence = resolve_scenario(h, scenario).receipt.evidence[key]
        _require(
            (evidence.reason, evidence.units) == (ImpactReason.UNAFFECTED, (h.unit_id(unit),)),
            f"{scenario}: key {key!r} should carry, got {evidence.reason} {evidence.units}",
        )


def _rule1_unowned(h: ImpactHarness) -> None:
    resolution = resolve_scenario(h, UNOWNED)
    claimed = tuple(resolution.require_raw().owners.get(UNOWNED_PATH) or ())
    _require(not claimed, f"backend claims owners {claimed} for unowned {UNOWNED_PATH!r}")
    _require(resolution.receipt.unowned_paths == (UNOWNED_PATH,))
    _all_invalidated(resolution, ImpactReason.UNOWNED_PATH)


def _rule1_deleted(h: ImpactHarness) -> None:
    resolution = resolve_scenario(h, DELETION)
    _owners_via_base(h, resolution, DELETED_PATH, "service")
    for key in sorted(resolution.fixture.graph_invalidated(resolution.scenario)):
        _invalidated(resolution, key)


def _rule1_renamed(h: ImpactHarness) -> None:
    resolution = resolve_scenario(h, RENAME)
    _owners_via_base(h, resolution, RENAMED_FROM, "core")
    added = tuple(resolution.require_raw().owners.get(RENAMED_TO) or ())
    _require(h.unit_id("core") in added, f"renamed-to {RENAMED_TO!r} owned by {added}")
    for key in sorted(resolution.fixture.graph_invalidated(resolution.scenario)):
        _invalidated(resolution, key)


def _rule2_global_input(h: ImpactHarness) -> None:
    resolution = resolve_scenario(h, GLOBAL)
    global_input = resolution.fixture.global_input
    _require(
        resolution.receipt.global_inputs_hit == (global_input,),
        f"global input {global_input!r} not reported (hit {resolution.receipt.global_inputs_hit}, "
        f"unowned {resolution.receipt.unowned_paths})",
    )
    _all_invalidated(resolution, ImpactReason.GLOBAL_INPUT)


def _rule3_error(h: ImpactHarness) -> None:
    _fallback(resolve_scenario(h, CHAIN, fault=FAULT_ERROR), containing="error")


def _rule3_timeout(h: ImpactHarness) -> None:
    _fallback(resolve_scenario(h, CHAIN, fault=FAULT_TIMEOUT), containing="timeout")


def _rule3_slow_answer(h: ImpactHarness) -> None:
    _fallback(resolve_scenario(h, CHAIN, slow=True), containing="timeout")


def _rule3_version_mismatch(h: ImpactHarness) -> None:
    if FAULT_VERSION_MISMATCH not in h.faults:
        return  # the tool reports no version of its own to disagree with
    resolution = resolve_scenario(h, CHAIN, fault=FAULT_VERSION_MISMATCH)
    _fallback(resolution, containing="backend-version-mismatch")


def _rule4_unproven_affected(h: ImpactHarness) -> None:
    resolution = resolve_scenario(h, UNPROVEN_EDIT)
    _not_proven(resolution, KEY_LEGACY)
    _invalidated(resolution, KEY_LEGACY)


def _rule4_unproven_any_change(h: ImpactHarness) -> None:
    for scenario in (LEAF, CHAIN):
        resolution = resolve_scenario(h, scenario)
        _not_proven(resolution, KEY_LEGACY)
        evidence = resolution.receipt.evidence[KEY_LEGACY]
        _require(
            evidence.reason == ImpactReason.UNPROVEN,
            f"{scenario}: unproven key {KEY_LEGACY!r} landed as {evidence.reason}",
        )


def _rule4_selector_without_units(h: ImpactHarness) -> None:
    resolution = resolve_scenario(h, LEAF)
    units = tuple(resolution.require_raw().key_units.get(KEY_ORPHAN) or ())
    _require(not units, f"backend selected {units} for {KEY_ORPHAN!r}, which selects nothing")
    _require(resolution.receipt.evidence[KEY_ORPHAN].reason == ImpactReason.UNPROVEN)


def _rule5_git_metadata(h: ImpactHarness) -> None:
    for scenario in (LEAF, CHAIN, UNPROVEN_EDIT):
        resolution = resolve_scenario(h, scenario)
        _no_units_for(resolution, KEY_GIT_METADATA)
        evidence = resolution.receipt.evidence[KEY_GIT_METADATA]
        _require(
            evidence.reason == ImpactReason.NO_SELECTOR,
            f"{scenario}: git-metadata key landed as {evidence.reason}",
        )


IMPACT_CASES: tuple[PortCase[ImpactHarness], ...] = (
    PortCase("port.runtime-shape", _port_runtime_shape, "implements ImpactBackend under its name"),
    PortCase(
        "receipt.unchanged-trees", _receipt_unchanged_trees, "identical trees carry every key"
    ),
    PortCase("receipt.shape-and-digest", _receipt_every_scenario, "every receipt is well-formed"),
    PortCase("receipt.deterministic", _receipt_deterministic, "same question, same digest"),
    PortCase("graph.answers", _graph_answers, "no fallback without a fault"),
    PortCase(
        "graph.transitive-dependents",
        _graph_transitive_dependents,
        "a chain root change invalidates the key selecting its transitive dependent",
    ),
    PortCase(
        "graph.carries-unaffected",
        _graph_carries_unaffected,
        "a proven key the change cannot reach carries",
    ),
    PortCase("rule1.unowned-path", _rule1_unowned, "an unowned path invalidates every key"),
    PortCase(
        "rule1.deleted-path", _rule1_deleted, "a deletion is attributed through the base tree"
    ),
    PortCase("rule1.renamed-path", _rule1_renamed, "a rename is a base deletion plus a head add"),
    PortCase("rule2.global-input", _rule2_global_input, "a global input invalidates every key"),
    PortCase("rule3.tool-error", _rule3_error, "a tool error falls back to native-full"),
    PortCase("rule3.tool-timeout", _rule3_timeout, "a tool timeout falls back to native-full"),
    PortCase("rule3.slow-answer", _rule3_slow_answer, "an answer past the budget falls back"),
    PortCase("rule3.version-mismatch", _rule3_version_mismatch, "a version mismatch falls back"),
    PortCase(
        "rule4.unproven-affected",
        _rule4_unproven_affected,
        "an affected unproven unit is never reported proven",
    ),
    PortCase(
        "rule4.unproven-any-change",
        _rule4_unproven_any_change,
        "an unproven key is invalidated by any change, even one its units do not reach",
    ),
    PortCase(
        "rule4.selector-without-units",
        _rule4_selector_without_units,
        "a selector that selects nothing never carries",
    ),
    PortCase("rule5.git-metadata", _rule5_git_metadata, "a git-metadata key never carries"),
)


def impact_case(case_id: str) -> PortCase[ImpactHarness]:
    return next(case for case in IMPACT_CASES if case.case_id == case_id)


def run_impact_case(harness: ImpactHarness, case: PortCase[ImpactHarness]) -> None:
    """Run one case (for ``pytest.mark.parametrize("case", IMPACT_CASES)``); a rejection raises
    :class:`~beadhive.testing.ConformanceFailure` naming the case."""
    assert_impact_backend_conforms(harness, cases=(case,))


def assert_impact_backend_conforms(
    harness: ImpactHarness, *, cases: Sequence[PortCase[ImpactHarness]] = IMPACT_CASES
) -> None:
    """Run every case (or ``cases``) against ``harness``, stopping at the first rejection."""
    assert_port_conforms(lambda: harness, cases, subject=f"{harness.backend} impact backend")


def failing_cases(
    harness: ImpactHarness, cases: Iterable[PortCase[ImpactHarness]] = IMPACT_CASES
) -> tuple[str, ...]:
    """Every case id the harness fails, rather than only the first."""
    failed = []
    for case in cases:
        try:
            run_impact_case(harness, case)
        except AssertionError:
            failed.append(case.case_id)
    return tuple(failed)


__all__ = [
    "IMPACT_CASES",
    "ImpactHarness",
    "Resolution",
    "ScenarioSetup",
    "assert_impact_backend_conforms",
    "assert_receipt_conforms",
    "assert_receipt_digest_stable",
    "failing_cases",
    "impact_case",
    "resolve_scenario",
    "run_impact_case",
]
