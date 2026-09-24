"""Promotion policy for certified affected closures at commit/integration boundaries."""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "test_closure_promotion_policy.py"
EVIDENCE = ROOT / "docs" / "proof" / "bh-ck1t6.4-promotion-policy.json"
CONFIG = ROOT / "tests" / "selective-ci-policy.toml"
SPEC = importlib.util.spec_from_file_location("test_closure_promotion_policy", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
promotion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion
SPEC.loader.exec_module(promotion)


def _plan(*, decision: str = "selective") -> dict[str, object]:
    fallback = [] if decision == "selective" else ["shared-contract-change"]
    return {
        "selector": {"version": "bh-test-impact-selector-v1", "digest": "sha256:" + "1" * 64},
        "decision": decision,
        "confidence": "high" if decision == "selective" else "low",
        "commands": (
            ["just test-closure module.alpha"] if decision == "selective" else ["just check"]
        ),
        "tests": [
            "tests/unit/modules/alpha/test_service.py",
            "tests/contracts/test_alpha_contract.py",
            "tests/test_alpha_compat.py",
            "tests/test_semantic_otel_adapter.py",
            "tests/test_alpha_integration.py",
            "tests/test_alpha_smoke.py",
        ],
        "changed_modules": ["module.alpha"],
        "candidate_closures": [
            {
                "id": "module.alpha",
                "input_digest": "sha256:" + "2" * 64,
                "confidence": "high",
            }
        ],
        "closure_digests": {"module.alpha": "sha256:" + "2" * 64},
        "contracts": ["contracts", "kernel.telemetry"],
        "dependency_paths": [
            {"relationship": "reverse-dependency", "to": "adapters"},
            {"relationship": "shared-contract", "to": "contracts"},
        ],
        "exclusions": [
            {
                "closure": "plugin.orca",
                "status": "unaffected",
                "reason": "unaffected-current-digests-stable",
            }
        ],
        "fallback_reasons": fallback,
        "plan_digest": "sha256:" + "3" * 64,
    }


def _verified_route() -> dict[str, object]:
    return {
        "command": "just test-closure module.alpha",
        "selective": True,
        "closure": "module.alpha",
        "reasons": [],
    }


@pytest.mark.parametrize("boundary", ["commit", "main-integration"])
def test_certified_verified_closure_is_promoted_only_at_explicit_boundaries(boundary: str) -> None:
    route = promotion._route_verified_plan(
        _plan(),
        _verified_route(),
        boundary=boundary,
        mode="certified",
        telemetry_ready=True,
        next_full="2026-09-11T00:00:00Z scheduled",
    )

    assert route["selective"] is True
    assert route["command"] == "just test-closure module.alpha"
    assert route["ran"] == {
        "commands": ["just test-closure module.alpha"],
        "tests": _plan()["tests"],
    }
    assert route["skipped"] == [
        {
            "closure": "plugin.orca",
            "reason": "unaffected-current-digests-stable",
        }
    ]
    assert route["why"] == ["certified-affected-closure"]
    assert route["closure_digests"] == {"module.alpha": "sha256:" + "2" * 64}
    assert route["selector_digest"] == "sha256:" + "1" * 64
    assert route["confidence"] == "high"
    assert route["next_full"] == "2026-09-11T00:00:00Z scheduled"


@pytest.mark.parametrize(
    "boundary",
    [
        "leaf-merge",
        "child-epic-finish",
        "final-workstream-submit",
        "final-workstream-review",
        "scheduled",
        "release",
    ],
)
def test_safety_boundaries_remain_full(boundary: str) -> None:
    route = promotion._route_verified_plan(
        _plan(),
        _verified_route(),
        boundary=boundary,
        mode="certified",
        telemetry_ready=True,
        next_full="now",
    )

    assert route["command"] == "just check-all"
    assert route["selective"] is False
    assert "boundary-requires-full-suite" in route["why"]
    assert route["ran"] == {"commands": ["just check-all"], "tests": ["complete-suite"]}
    assert route["skipped"] == []


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("selector-full", "shared-contract-change"),
        ("unverified", "production-evidence-not-verified"),
        ("telemetry-stale", "telemetry-readiness-not-current"),
    ],
)
def test_shared_unknown_high_risk_or_stale_authority_fails_closed(
    mutation: str, reason: str
) -> None:
    plan = _plan()
    verified = _verified_route()
    telemetry_ready = True
    if mutation == "selector-full":
        plan = _plan(decision="full")
    elif mutation == "unverified":
        verified = {
            "command": "just check",
            "selective": False,
            "closure": None,
            "reasons": ["production-evidence-not-verified"],
        }
    else:
        telemetry_ready = False

    route = promotion._route_verified_plan(
        plan,
        verified,
        boundary="main-integration",
        mode="certified",
        telemetry_ready=telemetry_ready,
        next_full="now",
    )

    assert route["command"] == "just check"
    assert route["selective"] is False
    assert reason in route["why"]
    assert route["skipped"] == []


def test_one_configuration_value_rolls_back_to_full() -> None:
    selective = promotion._route_verified_plan(
        _plan(),
        _verified_route(),
        boundary="commit",
        mode="certified",
        telemetry_ready=True,
        next_full="now",
    )
    rolled_back = promotion._route_verified_plan(
        _plan(),
        _verified_route(),
        boundary="commit",
        mode="full",
        telemetry_ready=True,
        next_full="now",
    )

    assert selective["selective"] is True
    assert rolled_back["selective"] is False
    assert rolled_back["command"] == "just check"
    assert rolled_back["why"] == ["selective-ci-mode-full"]


def test_checked_policy_is_digest_bound_and_current_activation_is_honest() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    assert promotion.validate_checked_evidence(evidence, ROOT) == ()
    assert evidence["telemetry"]["closure"] == "kernel.telemetry"
    assert evidence["telemetry"]["current"] is True
    assert evidence["activation"] == {
        "mode": "certified",
        "eligible_closures": [],
        "production_selective_routes": 0,
    }
    assert evidence["rollback"] == {
        "file": "tests/selective-ci-policy.toml",
        "change": "selective_ci.mode = 'certified' -> 'full'",
    }


def test_checked_policy_rejects_mutation() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    forged = deepcopy(evidence)
    forged["activation"]["production_selective_routes"] = 1

    assert promotion.validate_checked_evidence(forged, ROOT)


def test_public_production_entrypoint_never_accepts_a_caller_verified_route() -> None:
    parameters = inspect.signature(promotion.route_production_validation).parameters

    assert "verified_route" not in parameters
    assert "mode" not in parameters
    assert "telemetry_ready" not in parameters


def test_just_gate_checks_promotion_evidence_without_renaming_full_gates() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")

    assert "uv run python scripts/test_closure_promotion_policy.py --check" in justfile
    assert "check-native: lint lint-md license-check architecture-structural-check" in justfile
    assert (
        "check-all-native: require-bd lint lint-md license-check architecture-structural-check"
        in justfile
    )
    assert promotion.load_policy(CONFIG).mode == "certified"
