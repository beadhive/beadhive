"""Fail-closed policy tests for closure shadow evidence and local-only activation."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "test_closure_shadow_policy.py"
EVIDENCE = ROOT / "docs" / "proof" / "bh-ck1t6.3-shadow-activation.json"
SPEC = importlib.util.spec_from_file_location("test_closure_shadow_policy_script", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
shadow = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = shadow
SPEC.loader.exec_module(shadow)


def _certified_closure(closure_id: str = "module.alpha") -> dict[str, object]:
    digest = f"sha256:{closure_id}-input"
    return {
        "id": closure_id,
        "command": f"just test-closure {closure_id}",
        "input_digest": digest,
        "observed_input_digest": digest,
        "confidence": "high",
        "enforceable_port": True,
        "simulation_control": True,
        "coverage_mapping": {
            "mode": "dynamic-per-test",
            "input_digest": digest,
            "source_revision": "sample-head",
            "per_test_contexts": [
                {
                    "source": "src/beadhive/modules/alpha/service.py",
                    "tests": ["tests/unit/modules/alpha/test_service.py"],
                }
            ],
        },
        "certification": {
            "status": "certified",
            "eligible_for_selective_activation": True,
        },
        "current_applicability": {
            "applicable": True,
            "fallback_reasons": [],
            "recorded_input_digest": digest,
            "observed_input_digest": digest,
            "recorded_registry_digest": "sha256:registry",
            "observed_registry_digest": "sha256:registry",
            "recorded_material_digest": "sha256:material",
            "observed_material_digest": "sha256:material",
        },
    }


def _sample(number: int, *, escape: bool = False) -> dict[str, object]:
    observed = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=number * 3)
    selected_tests = ["tests/unit/modules/alpha/test_service.py"]
    selector = {"version": shadow.SELECTOR_VERSION, "digest": "sha256:selector"}
    change_classes = (
        "owned-implementation",
        "public-port-or-contract",
        "reverse-dependent-or-multi-file",
    )
    return {
        "sample_id": f"simulation-{number:02d}",
        "provenance": "simulation-only",
        "change_class": change_classes[number % len(change_classes)],
        "qualifying_merged_change": True,
        "observed_at": observed.isoformat().replace("+00:00", "Z"),
        "tree": f"tree-{number:02d}",
        "source_revision": (
            "sample-head" if number == shadow.MIN_QUALIFYING_CHANGES - 1 else f"sample-{number:02d}"
        ),
        "closure_input_digest": "sha256:module.alpha-input",
        "selector": selector,
        "plan": {
            "plan_digest": f"sha256:plan-{number:02d}",
            "replay_plan_digest": f"sha256:plan-{number:02d}",
            "decision": "selective",
            "commands": ["just test-closure module.alpha"],
            "tests": selected_tests,
            "fallback_reasons": [],
            "selector": selector,
        },
        "selected": {
            "command": "just test-closure module.alpha",
            "tree": f"tree-{number:02d}",
            "tests": selected_tests,
            "exit_code": 0,
            "failures": [],
            "wall_seconds": 20.0,
            "compute_seconds": 18.0,
            "queue_seconds": 2.0,
            "flake_count": 0,
        },
        "full": {
            "command": "just check",
            "tree": f"tree-{number:02d}",
            "tests": [
                "tests/unit/modules/alpha/test_service.py",
                "tests/test_unrelated.py",
            ],
            "exit_code": 1 if escape else 0,
            "failures": ["tests/test_unrelated.py::test_escape"] if escape else [],
            "relevant_failures": ["tests/test_unrelated.py::test_escape"] if escape else [],
            "wall_seconds": 140.0,
            "compute_seconds": 130.0,
            "queue_seconds": 10.0,
            "flake_count": 0,
        },
        "omitted_tests": ["tests/test_unrelated.py"],
        "fallback_reasons": [],
        "escapes": (
            [
                {
                    "failure": "tests/test_unrelated.py::test_escape",
                    "root_cause": "missing reverse edge",
                    "correction": "register reverse edge",
                    "replay": "pending",
                }
            ]
            if escape
            else []
        ),
    }


def _graduated_samples() -> list[dict[str, object]]:
    return [_sample(index) for index in range(shadow.MIN_QUALIFYING_CHANGES)]


def _selective_plan() -> dict[str, object]:
    return {
        "decision": "selective",
        "commands": ["just test-closure module.alpha"],
        "changed_modules": ["module.alpha"],
        "fallback_reasons": [],
        "selector": {"version": shadow.SELECTOR_VERSION, "digest": "sha256:selector"},
    }


def test_checked_current_artifact_rejects_every_real_closure() -> None:
    checked = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    assert shadow.validate_checked_evidence(checked, ROOT) == ()
    assert checked["activation"] == {"enabled": False, "eligible_closures": []}
    assert len(checked["closure_decisions"]) == 23
    assert all(not item["eligible"] for item in checked["closure_decisions"])
    for item in checked["closure_decisions"]:
        assert "closure-not-certified" in item["reasons"]
        assert "missing-or-stale-dynamic-trace" in item["reasons"]
        assert item["measurements"] == {
            "status": "unavailable",
            "sample_count": 0,
            "qualifying_count": 0,
            "window_days": None,
            "selected_wall_median_seconds": None,
            "full_wall_median_seconds": None,
            "wall_savings_median_seconds": None,
            "wall_ratio": None,
            "selected_compute_median_seconds": None,
            "full_compute_median_seconds": None,
            "queue_median_seconds": None,
            "flake_count": 0,
            "miss_count": 0,
            "fallback_count": 0,
            "fallback_rate": None,
        }


def test_architecture_gate_checks_shadow_policy_without_replacing_full_gate() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")

    assert "uv run python scripts/test_closure_shadow_policy.py --check" in justfile
    assert (
        "check: lint lint-md license-check architecture-check wire-schema-compat test" in justfile
    )


def test_simulation_labelled_positive_control_meets_predeclared_bar() -> None:
    decision = shadow.evaluate_candidate(
        _certified_closure(),
        _graduated_samples(),
        selector_version=shadow.SELECTOR_VERSION,
        selector_digest="sha256:selector",
    )

    assert decision["eligible"] is True
    assert decision["reasons"] == []
    assert decision["measurements"]["qualifying_count"] == 30
    assert decision["measurements"]["window_days"] >= 60
    assert decision["measurements"]["miss_count"] == 0
    assert decision["measurements"]["selected_wall_median_seconds"] == 20.0
    assert decision["measurements"]["full_wall_median_seconds"] == 140.0
    assert decision["measurements"]["wall_savings_median_seconds"] == 120.0
    assert decision["measurements"]["wall_ratio"] == pytest.approx(1 / 7)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("too-few", "insufficient-qualifying-shadow-samples"),
        ("too-young", "shadow-window-too-young"),
        ("escape", "selected-green-full-red-escape"),
        ("tree-mismatch", "selected-full-tree-mismatch"),
        ("selector-drift", "selector-drift"),
        ("digest-drift", "closure-input-digest-mismatch"),
        ("slow", "timing-target-not-met"),
        ("fallback", "shadow-sample-fell-back"),
        ("unrepresentative", "representative-change-classes-missing"),
        ("plan-replay", "selector-plan-not-deterministic"),
        ("plan-tests", "selector-plan-run-mismatch"),
        ("omissions", "omitted-test-inventory-mismatch"),
    ),
)
def test_shadow_uncertainty_fails_closed(mutation: str, reason: str) -> None:
    samples = _graduated_samples()
    selector_digest = "sha256:selector"
    if mutation == "too-few":
        samples.pop()
    elif mutation == "too-young":
        for index, sample in enumerate(samples):
            sample["observed_at"] = (
                (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index))
                .isoformat()
                .replace("+00:00", "Z")
            )
    elif mutation == "escape":
        samples[-1] = _sample(shadow.MIN_QUALIFYING_CHANGES - 1, escape=True)
    elif mutation == "tree-mismatch":
        samples[-1]["full"]["tree"] = "different-tree"
    elif mutation == "selector-drift":
        samples[-1]["selector"]["digest"] = "sha256:old-selector"
    elif mutation == "digest-drift":
        samples[-1]["closure_input_digest"] = "sha256:old-input"
    elif mutation == "slow":
        for sample in samples:
            sample["selected"]["wall_seconds"] = 125.0
    elif mutation == "fallback":
        samples[-1]["fallback_reasons"] = ["unknown-or-unowned-path"]
    elif mutation == "unrepresentative":
        for sample in samples:
            sample["change_class"] = "owned-implementation"
    elif mutation == "plan-replay":
        samples[-1]["plan"]["replay_plan_digest"] = "sha256:different"
    elif mutation == "plan-tests":
        samples[-1]["plan"]["tests"] = ["tests/test_wrong.py"]
    elif mutation == "omissions":
        samples[-1]["omitted_tests"] = []

    decision = shadow.evaluate_candidate(
        _certified_closure(),
        samples,
        selector_version=shadow.SELECTOR_VERSION,
        selector_digest=selector_digest,
    )

    assert decision["eligible"] is False
    assert reason in decision["reasons"]


@pytest.mark.parametrize(
    "boundary",
    ["leaf-merge", "epic-finish", "workstream-submit", "workstream-review", "scheduled", "release"],
)
def test_nonlocal_or_integration_boundaries_always_use_full_gate(boundary: str) -> None:
    candidate = shadow.evaluate_candidate(
        _certified_closure(),
        _graduated_samples(),
        selector_version=shadow.SELECTOR_VERSION,
        selector_digest="sha256:selector",
    )

    route = shadow.route_validation(
        _selective_plan(),
        {"module.alpha": candidate},
        boundary=boundary,
        is_leaf=True,
        local_enabled=True,
        allow_simulation=True,
    )

    assert route["command"] == "just check"
    assert route["selective"] is False
    assert "boundary-requires-full-gate" in route["reasons"]


@pytest.mark.parametrize("boundary", ["check", "submit", "pristine-review"])
def test_simulated_eligible_closure_is_scoped_to_local_leaf_boundaries(boundary: str) -> None:
    candidate = shadow.evaluate_candidate(
        _certified_closure(),
        _graduated_samples(),
        selector_version=shadow.SELECTOR_VERSION,
        selector_digest="sha256:selector",
    )

    route = shadow.route_validation(
        _selective_plan(),
        {"module.alpha": candidate},
        boundary=boundary,
        is_leaf=True,
        local_enabled=True,
        allow_simulation=True,
    )

    assert route == {
        "command": "just test-closure module.alpha",
        "selective": True,
        "closure": "module.alpha",
        "reasons": [],
    }


def test_one_local_setting_change_rolls_an_eligible_simulation_back_to_full() -> None:
    candidate = shadow.evaluate_candidate(
        _certified_closure(),
        _graduated_samples(),
        selector_version=shadow.SELECTOR_VERSION,
        selector_digest="sha256:selector",
    )
    plan = _selective_plan()

    enabled = shadow.route_validation(
        plan,
        {"module.alpha": candidate},
        boundary="submit",
        is_leaf=True,
        local_enabled=True,
        allow_simulation=True,
    )
    rolled_back = shadow.route_validation(
        plan,
        {"module.alpha": candidate},
        boundary="submit",
        is_leaf=True,
        local_enabled=False,
        allow_simulation=True,
    )

    assert enabled["selective"] is True
    assert rolled_back == {
        "command": "just check",
        "selective": False,
        "closure": None,
        "reasons": ["local-activation-disabled"],
    }


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("full-plan", "selector-required-full-gate"),
        ("multi", "multi-module-ambiguity"),
        ("nonleaf", "non-leaf-work-item"),
        ("unknown", "closure-not-eligible"),
        ("selector-version", "selector-drift"),
        ("selector-digest", "selector-drift"),
    ),
)
def test_routing_uncertainty_falls_back_to_full(mutation: str, reason: str) -> None:
    candidate = shadow.evaluate_candidate(
        _certified_closure(),
        _graduated_samples(),
        selector_version=shadow.SELECTOR_VERSION,
        selector_digest="sha256:selector",
    )
    plan = _selective_plan()
    decisions = {"module.alpha": candidate}
    is_leaf = True
    if mutation == "full-plan":
        plan["decision"] = "full"
        plan["commands"] = ["just check"]
        plan["fallback_reasons"] = ["unknown-or-unowned-path"]
    elif mutation == "multi":
        plan["changed_modules"] = ["module.alpha", "module.beta"]
    elif mutation == "nonleaf":
        is_leaf = False
    elif mutation == "unknown":
        decisions = {}
    elif mutation == "selector-version":
        plan["selector"]["version"] = "old"
    elif mutation == "selector-digest":
        plan["selector"]["digest"] = "sha256:old"

    route = shadow.route_validation(
        plan,
        decisions,
        boundary="check",
        is_leaf=is_leaf,
        local_enabled=True,
        allow_simulation=True,
    )

    assert route["command"] == "just check"
    assert route["selective"] is False
    assert reason in route["reasons"]


def test_simulation_evidence_cannot_route_without_an_explicit_test_control() -> None:
    candidate = shadow.evaluate_candidate(
        _certified_closure(),
        _graduated_samples(),
        selector_version=shadow.SELECTOR_VERSION,
        selector_digest="sha256:selector",
    )

    route = shadow.route_validation(
        _selective_plan(),
        {"module.alpha": candidate},
        boundary="check",
        is_leaf=True,
        local_enabled=True,
    )

    assert route["command"] == "just check"
    assert route["reasons"] == ["simulation-evidence-not-activatable"]


def test_checked_evidence_mutation_is_rejected() -> None:
    checked = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    checked["activation"]["enabled"] = True

    errors = shadow.validate_checked_evidence(checked, ROOT)

    assert any("checked evidence drifted" in error for error in errors)


def test_policy_module_never_runs_tests_or_mutates_the_repository() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "subprocess" not in source
    assert "os.system" not in source
    assert "write_text" not in source
    assert "write_bytes" not in source
    assert "git " not in source
