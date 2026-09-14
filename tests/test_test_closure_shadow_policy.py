"""Fail-closed policy tests for closure shadow evidence and local-only activation."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "test_closure_shadow_policy.py"
VERIFIER_SCRIPT = ROOT / "scripts" / "test_closure_shadow_verifier.py"
EVIDENCE = ROOT / "docs" / "proof" / "bh-ck1t6.3-shadow-activation.json"
SPEC = importlib.util.spec_from_file_location("test_closure_shadow_policy", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
shadow = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = shadow
SPEC.loader.exec_module(shadow)
VERIFIER_SPEC = importlib.util.spec_from_file_location(
    "test_closure_shadow_verifier", VERIFIER_SCRIPT
)
assert VERIFIER_SPEC is not None and VERIFIER_SPEC.loader is not None
verifier = importlib.util.module_from_spec(VERIFIER_SPEC)
sys.modules[VERIFIER_SPEC.name] = verifier
VERIFIER_SPEC.loader.exec_module(verifier)

SELECTOR_DIGEST = "sha256:" + "1" * 64
SOURCE_REVISION = "2" * 40


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


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
            "source_revision": SOURCE_REVISION,
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


def _plan(number: int, selected_tests: list[str]) -> dict[str, object]:
    commit = f"{number + 1:040x}"
    base = "3" * 40
    plan: dict[str, object] = {
        "schema_version": 1,
        "selector": {"version": shadow.SELECTOR_VERSION, "digest": SELECTOR_DIGEST},
        "range": {
            "base": base,
            "head": commit,
            "merge_base": base,
            "merge_base_status": "resolved",
        },
        "changes": [{"status": "M", "path": "src/beadhive/modules/alpha/service.py"}],
        "decision": "selective",
        "selection_scope": "closure",
        "confidence": "high",
        "commands": ["just test-closure module.alpha"],
        "tests": selected_tests,
        "candidate_closures": [
            {
                "id": "module.alpha",
                "kind": "module",
                "command": "just test-closure module.alpha",
                "tests": selected_tests,
                "contracts": [],
                "input_digest": "sha256:module.alpha-input",
                "confidence": "high",
                "relationships": [],
                "applicability": _certified_closure()["current_applicability"],
            }
        ],
        "changed_modules": ["module.alpha"],
        "dependency_paths": [],
        "contracts": [],
        "closure_digests": {"module.alpha": "sha256:module.alpha-input"},
        "exclusions": [],
        "fallback_reasons": [],
        "artifact_errors": [],
        "activation": {"enabled": False, "owner": "bh-ck1t6.3"},
    }
    plan["plan_digest"] = _digest(plan)
    return plan


def _receipt(
    number: int,
    *,
    full: bool,
    tests: list[str],
    exit_code: int,
    failures: list[str],
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": 1,
        "receipt_id": f"{'full' if full else 'selected'}-{number:02d}",
        "status": "completed",
        "command": "just check" if full else "just test-closure module.alpha",
        "commit": f"{number + 1:040x}",
        "tree": f"{number + 1001:040x}",
        "tests": tests,
        "exit_code": exit_code,
        "failures": failures,
        "wall_seconds": 140.0 if full else 20.0,
        "compute_seconds": 130.0 if full else 18.0,
        "queue_seconds": 10.0 if full else 2.0,
        "flake_count": 0,
    }
    if full:
        result["relevant_failures"] = failures.copy()
    result["receipt_digest"] = _digest(result)
    return result


def _sample(number: int, *, escape: bool = False) -> dict[str, object]:
    observed = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=number * 3)
    selected_tests = ["tests/unit/modules/alpha/test_service.py"]
    selector = {"version": shadow.SELECTOR_VERSION, "digest": SELECTOR_DIGEST}
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
        "commit": f"{number + 1:040x}",
        "tree": f"{number + 1001:040x}",
        "closure_id": "module.alpha",
        "source_revision": SOURCE_REVISION,
        "closure_input_digest": "sha256:module.alpha-input",
        "selector": selector,
        "plan": _plan(number, selected_tests),
        "selected": _receipt(number, full=False, tests=selected_tests, exit_code=0, failures=[]),
        "full": _receipt(
            number,
            full=True,
            tests=[
                "tests/unit/modules/alpha/test_service.py",
                "tests/test_unrelated.py",
            ],
            exit_code=1 if escape else 0,
            failures=["tests/test_unrelated.py::test_escape"] if escape else [],
        ),
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


def _authority(sample: dict[str, object]) -> dict[str, object]:
    return {
        "sample_id": sample["sample_id"],
        "provenance": sample["provenance"],
        "commit": sample["commit"],
        "tree": sample["tree"],
        "merge_verified": sample["provenance"] == "qualifying-merged-change",
        "sample_digest": _digest(sample),
        "plan_digest": sample["plan"]["plan_digest"],
        "selected_receipt_digest": sample["selected"]["receipt_digest"],
        "full_receipt_digest": sample["full"]["receipt_digest"],
    }


def _evaluate(
    samples: list[dict[str, object]],
    *,
    closure: dict[str, object] | None = None,
    authoritative_evidence: list[dict[str, object]] | None = None,
    selector_digest: str = SELECTOR_DIGEST,
) -> dict[str, object]:
    return shadow.evaluate_candidate(
        closure or _certified_closure(),
        samples,
        selector_version=shadow.SELECTOR_VERSION,
        selector_digest=selector_digest,
        authoritative_evidence=(
            authoritative_evidence
            if authoritative_evidence is not None
            else [_authority(sample) for sample in samples]
        ),
    )


def _graduated_decision() -> dict[str, object]:
    return _evaluate(_graduated_samples())


def _selective_plan() -> dict[str, object]:
    return _plan(shadow.MIN_QUALIFYING_CHANGES - 1, ["tests/unit/modules/alpha/test_service.py"])


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repo), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _rehash_sample(sample: dict[str, object]) -> None:
    plan = sample["plan"]
    plan["plan_digest"] = _digest(
        {key: value for key, value in plan.items() if key != "plan_digest"}
    )
    for receipt_name in ("selected", "full"):
        receipt = sample[receipt_name]
        receipt["receipt_digest"] = _digest(
            {key: value for key, value in receipt.items() if key != "receipt_digest"}
        )


def _store_receipts(receipt_root: Path, sample: dict[str, object]) -> None:
    for receipt_name in ("selected", "full"):
        receipt = sample[receipt_name]
        (receipt_root / f"{receipt['receipt_id']}.json").write_text(
            json.dumps(receipt, sort_keys=True),
            encoding="utf-8",
        )


def _real_production_fixture(tmp_path: Path) -> dict[str, object]:
    repo = tmp_path / "repo"
    receipts = tmp_path / "receipts"
    repo.mkdir()
    receipts.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Shadow Test")
    _git(repo, "config", "user.email", "shadow@example.invalid")
    _git(repo, "config", "commit.gpgsign", "false")
    tracked = repo / "tracked.txt"
    tracked.write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "seed")
    previous = _git(repo, "rev-parse", "HEAD")
    revisions: list[tuple[str, str, str]] = []
    for index in range(shadow.MIN_QUALIFYING_CHANGES):
        tracked.write_text(f"sample {index}\n", encoding="utf-8")
        _git(repo, "add", "tracked.txt")
        _git(repo, "commit", "-m", f"sample {index}")
        commit = _git(repo, "rev-parse", "HEAD")
        tree = _git(repo, "rev-parse", "HEAD^{tree}")
        revisions.append((previous, commit, tree))
        previous = commit

    closure = _certified_closure()
    closure["coverage_mapping"]["source_revision"] = previous
    samples = _graduated_samples()
    for sample, (base, commit, tree) in zip(samples, revisions, strict=True):
        sample["provenance"] = "qualifying-merged-change"
        sample["commit"] = commit
        sample["tree"] = tree
        sample["source_revision"] = previous
        sample["plan"]["range"].update({"base": base, "head": commit, "merge_base": base})
        for receipt_name in ("selected", "full"):
            sample[receipt_name]["commit"] = commit
            sample[receipt_name]["tree"] = tree
        _rehash_sample(sample)
        _store_receipts(receipts, sample)
    authorities = [_authority(sample) for sample in samples]
    candidate = _evaluate(
        samples,
        closure=closure,
        authoritative_evidence=authorities,
    )
    return {
        "repo": repo,
        "receipt_root": receipts,
        "closure": closure,
        "samples": samples,
        "authorities": authorities,
        "candidate": candidate,
        "plan": samples[-1]["plan"],
        "git": verifier._SubprocessGitEvidence(repo),
        "receipts": verifier._LocalReceiptDirectory(receipts),
    }


def _prepare_public_production_fixture(
    fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    selector_digest = shadow._digest_bytes(shadow.SELECTOR_PATH.read_bytes())
    samples = fixture["samples"]
    for sample in samples:
        sample["selector"]["digest"] = selector_digest
        sample["plan"]["selector"]["digest"] = selector_digest
        _rehash_sample(sample)
    authorities = [_authority(sample) for sample in samples]
    fixture["authorities"] = authorities
    fixture["candidate"] = _evaluate(
        samples,
        closure=fixture["closure"],
        authoritative_evidence=authorities,
        selector_digest=selector_digest,
    )
    receipt_root = fixture["repo"] / ".git" / "bh" / "shadow-validation" / "receipts"
    receipt_root.mkdir(parents=True)
    for sample in samples:
        _store_receipts(receipt_root, sample)
    monkeypatch.setattr(shadow, "ROOT", fixture["repo"])


def _route_real_fixture(
    fixture: dict[str, object],
    *,
    receipts: object | None = None,
) -> dict[str, object]:
    return verifier._route_production_validation_with_ports(
        closure=fixture["closure"],
        samples=fixture["samples"],
        authoritative_evidence=fixture["authorities"],
        candidate=fixture["candidate"],
        current_plan=fixture["plan"],
        boundary="submit",
        is_leaf=True,
        local_enabled=True,
        git=fixture["git"],
        receipts=receipts or fixture["receipts"],
        selector_digest=SELECTOR_DIGEST,
    )


def test_checked_current_artifact_rejects_every_real_closure() -> None:
    checked = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    assert shadow.validate_checked_evidence(checked, ROOT) == ()
    assert checked["activation"] == {"enabled": False, "eligible_closures": []}
    assert len(checked["closure_decisions"]) == 24
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
        "check: lint lint-md license-check architecture-check transport-artifact-check "
        "wire-schema-compat proof-digest-check test" in justfile
    )


def test_simulation_labelled_positive_control_meets_predeclared_bar() -> None:
    decision = _graduated_decision()

    assert decision["eligible"] is True
    assert decision["reasons"] == []
    assert decision["measurements"]["qualifying_count"] == 30
    assert decision["measurements"]["window_days"] >= 60
    assert decision["measurements"]["miss_count"] == 0
    assert decision["measurements"]["selected_wall_median_seconds"] == 20.0
    assert decision["measurements"]["full_wall_median_seconds"] == 140.0
    assert decision["measurements"]["wall_savings_median_seconds"] == 120.0
    assert decision["measurements"]["wall_ratio"] == pytest.approx(1 / 7)


def test_forged_thirty_sample_evidence_cannot_activate_production_routing() -> None:
    samples = _graduated_samples()
    for index, sample in enumerate(samples):
        sample["sample_id"] = "duplicated-sample"
        sample["provenance"] = "qualifying-merged-change"
        sample["tree"] = f"not-a-git-tree-{index}"
        sample["selected"]["tree"] = sample["tree"]
        sample["full"]["tree"] = sample["tree"]
        sample["selected"]["receipt"] = {"status": "passed", "verified": True}
        sample["full"]["receipt"] = {"status": "failed", "verified": True}
        sample["plan"]["plan_digest"] = "sha256:arbitrary-equal-value"
        sample["plan"]["replay_plan_digest"] = "sha256:arbitrary-equal-value"
        if index < shadow.MIN_QUALIFYING_CHANGES - 1:
            sample["closure_input_digest"] = "sha256:stale-input"
            sample["source_revision"] = "stale-source-revision"

    decision = shadow.evaluate_candidate(
        _certified_closure(),
        samples,
        selector_version=shadow.SELECTOR_VERSION,
        selector_digest="sha256:selector",
    )

    assert decision["eligible"] is False
    assert decision["reasons"]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("stale-29-of-30", "closure-source-binding-mismatch"),
        ("duplicate-ids", "duplicate-or-invalid-shadow-sample-id"),
        ("non-git-provenance", "invalid-authoritative-git-provenance"),
        ("forged-receipt", "invalid-receipt-digest"),
        ("red-full-inconsistency", "inconsistent-relevant-failure-inventory"),
        ("selected-outside-full", "selected-tests-not-subset-of-full-inventory"),
    ),
)
def test_named_evidence_forgeries_fail_closed(mutation: str, reason: str) -> None:
    samples = _graduated_samples()
    if mutation == "stale-29-of-30":
        for sample in samples[:-1]:
            sample["closure_input_digest"] = "sha256:stale-input"
            sample["source_revision"] = "4" * 40
    elif mutation == "duplicate-ids":
        for sample in samples:
            sample["sample_id"] = "duplicate"
    elif mutation == "non-git-provenance":
        sample = samples[-1]
        sample["commit"] = "not-a-commit"
        sample["tree"] = "not-a-tree"
        sample["plan"]["range"]["head"] = sample["commit"]
        sample["plan"]["plan_digest"] = _digest(
            {key: value for key, value in sample["plan"].items() if key != "plan_digest"}
        )
        for receipt_name in ("selected", "full"):
            receipt = sample[receipt_name]
            receipt["commit"] = sample["commit"]
            receipt["tree"] = sample["tree"]
            receipt["receipt_digest"] = _digest(
                {key: value for key, value in receipt.items() if key != "receipt_digest"}
            )
    elif mutation == "forged-receipt":
        samples[-1]["full"]["receipt_digest"] = "sha256:" + "9" * 64
    elif mutation == "red-full-inconsistency":
        full = samples[-1]["full"]
        full["exit_code"] = 1
        full["failures"] = ["tests/test_unrelated.py::test_escape"]
        full["relevant_failures"] = []
        full["receipt_digest"] = _digest(
            {key: value for key, value in full.items() if key != "receipt_digest"}
        )
    elif mutation == "selected-outside-full":
        sample = samples[-1]
        selected_tests = ["tests/test_not_in_full.py"]
        sample["selected"]["tests"] = selected_tests
        sample["selected"]["receipt_digest"] = _digest(
            {key: value for key, value in sample["selected"].items() if key != "receipt_digest"}
        )
        sample["plan"]["tests"] = selected_tests
        sample["plan"]["candidate_closures"][0]["tests"] = selected_tests
        sample["plan"]["plan_digest"] = _digest(
            {key: value for key, value in sample["plan"].items() if key != "plan_digest"}
        )

    decision = _evaluate(samples)

    assert decision["eligible"] is False
    assert reason in decision["reasons"]


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
        ("plan-digest", "selector-plan-digest-mismatch"),
        ("plan-tests", "selector-plan-run-mismatch"),
        ("omissions", "omitted-test-inventory-mismatch"),
    ),
)
def test_shadow_uncertainty_fails_closed(mutation: str, reason: str) -> None:
    samples = _graduated_samples()
    selector_digest = SELECTOR_DIGEST
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
    elif mutation == "plan-digest":
        samples[-1]["plan"]["plan_digest"] = "sha256:" + "9" * 64
    elif mutation == "plan-tests":
        samples[-1]["plan"]["tests"] = ["tests/test_wrong.py"]
    elif mutation == "omissions":
        samples[-1]["omitted_tests"] = []

    decision = _evaluate(samples, selector_digest=selector_digest)

    assert decision["eligible"] is False
    assert reason in decision["reasons"]


@pytest.mark.parametrize(
    "boundary",
    ["leaf-merge", "epic-finish", "workstream-submit", "workstream-review", "scheduled", "release"],
)
def test_nonlocal_or_integration_boundaries_always_use_full_gate(boundary: str) -> None:
    candidate = _graduated_decision()

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
    candidate = _graduated_decision()
    plan = _selective_plan()

    route = shadow.route_validation(
        plan,
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
    candidate = _graduated_decision()
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
    candidate = _graduated_decision()
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
    candidate = _graduated_decision()

    route = shadow.route_validation(
        _selective_plan(),
        {"module.alpha": candidate},
        boundary="check",
        is_leaf=True,
        local_enabled=True,
    )

    assert route["command"] == "just check"
    assert route["reasons"] == ["simulation-evidence-not-activatable"]


def test_caller_forged_authority_for_nonexistent_shas_never_routes_production() -> None:
    samples = _graduated_samples()
    for sample in samples:
        sample["provenance"] = "qualifying-merged-change"
    candidate = _evaluate(samples)
    plan = _selective_plan()

    route = shadow.route_validation(
        plan,
        {"module.alpha": candidate},
        boundary="submit",
        is_leaf=True,
        local_enabled=True,
    )

    assert route["selective"] is False
    assert route["command"] == "just check"


def test_policy_exposes_no_production_capability_constructor_or_binding_input() -> None:
    samples = _graduated_samples()
    for sample in samples:
        sample["provenance"] = "qualifying-merged-change"
    candidate = _evaluate(
        samples,
        authoritative_evidence=[_authority(sample) for sample in samples],
    )

    assert candidate["eligible"] is True
    assert not hasattr(shadow, "_ATTESTATION_ISSUER")
    assert not hasattr(shadow, "_VerifiedProductionAttestation")
    assert not hasattr(shadow, "_issue_verified_production_attestation")
    assert (
        "integration_ref" not in inspect.signature(verifier.route_production_validation).parameters
    )
    route = shadow.route_validation(
        samples[-1]["plan"],
        {"module.alpha": candidate},
        boundary="submit",
        is_leaf=True,
        local_enabled=True,
    )

    assert route == {
        "command": "just check",
        "selective": False,
        "closure": None,
        "reasons": ["production-verifier-required"],
    }


def test_real_git_and_local_receipts_select_a_production_route_atomically(
    tmp_path: Path,
) -> None:
    fixture = _real_production_fixture(tmp_path)

    route = verifier._route_production_validation_with_ports(
        closure=fixture["closure"],
        samples=fixture["samples"],
        authoritative_evidence=fixture["authorities"],
        candidate=fixture["candidate"],
        current_plan=fixture["plan"],
        boundary="submit",
        is_leaf=True,
        local_enabled=True,
        git=fixture["git"],
        receipts=fixture["receipts"],
        selector_digest=SELECTOR_DIGEST,
    )

    assert route == {
        "command": "just test-closure module.alpha",
        "selective": True,
        "closure": "module.alpha",
        "reasons": [],
    }


@pytest.mark.parametrize("dirty_kind", ["tracked", "untracked"])
def test_production_route_rejects_dirty_or_unknown_live_checkout(
    tmp_path: Path,
    dirty_kind: str,
) -> None:
    fixture = _real_production_fixture(tmp_path)
    repo = fixture["repo"]
    if dirty_kind == "tracked":
        (repo / "tracked.txt").write_text("dirty behavior change\n", encoding="utf-8")
    else:
        (repo / "unknown-global-config.py").write_text("unknown behavior\n", encoding="utf-8")

    route = _route_real_fixture(fixture)

    assert route["command"] == "just check"
    assert route["selective"] is False
    assert "live-checkout-not-verifiable" in route["reasons"]


def test_production_route_allows_git_ignored_cache_in_clean_checkout(tmp_path: Path) -> None:
    fixture = _real_production_fixture(tmp_path)
    repo = fixture["repo"]
    (repo / ".git" / "info" / "exclude").write_text("ignored-cache/\n", encoding="utf-8")
    cache = repo / "ignored-cache" / "state"
    cache.parent.mkdir()
    cache.write_text("irrelevant cache\n", encoding="utf-8")

    route = _route_real_fixture(fixture)

    assert route["selective"] is True


def test_production_route_rejects_grafted_false_ancestry(tmp_path: Path) -> None:
    fixture = _real_production_fixture(tmp_path)
    repo = fixture["repo"]
    main = _git(repo, "rev-parse", "HEAD")
    main_parent = _git(repo, "rev-parse", "HEAD^")
    sample = fixture["samples"][0]
    base = sample["plan"]["range"]["base"]
    _git(repo, "checkout", "-b", "unmerged-graft", base)
    (repo / "tracked.txt").write_text("unmerged graft sample\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "unmerged graft sample")
    unmerged = _git(repo, "rev-parse", "HEAD")
    unmerged_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    _git(repo, "checkout", "main")
    assert (
        subprocess.run(
            (
                "git",
                "--no-replace-objects",
                "-C",
                str(repo),
                "merge-base",
                "--is-ancestor",
                unmerged,
                main,
            ),
            check=False,
        ).returncode
        == 1
    )
    sample["commit"] = unmerged
    sample["tree"] = unmerged_tree
    sample["plan"]["range"].update({"head": unmerged, "merge_base": base})
    for receipt_name in ("selected", "full"):
        sample[receipt_name]["commit"] = unmerged
        sample[receipt_name]["tree"] = unmerged_tree
    _rehash_sample(sample)
    _store_receipts(fixture["receipt_root"], sample)
    fixture["authorities"] = [_authority(item) for item in fixture["samples"]]
    fixture["candidate"] = _evaluate(
        fixture["samples"],
        closure=fixture["closure"],
        authoritative_evidence=fixture["authorities"],
    )
    (repo / ".git" / "info" / "grafts").write_text(
        f"{main} {main_parent} {unmerged}\n",
        encoding="utf-8",
    )

    route = _route_real_fixture(fixture)

    assert route["command"] == "just check"
    assert route["selective"] is False
    assert "live-checkout-not-verifiable" in route["reasons"]


def test_git_adapter_rejects_repository_alternates(tmp_path: Path) -> None:
    fixture = _real_production_fixture(tmp_path)
    repo = fixture["repo"]
    alternates = repo / ".git" / "objects" / "info" / "alternates"
    alternates.write_text(str(tmp_path / "external-objects") + "\n", encoding="utf-8")

    assert fixture["git"].checkout_snapshot() is None


def test_receipt_directory_rejects_root_and_intermediate_symlinks(tmp_path: Path) -> None:
    actual = tmp_path / "actual" / "receipts"
    actual.mkdir(parents=True)
    root_link = tmp_path / "root-link"
    root_link.symlink_to(actual, target_is_directory=True)
    intermediate_link = tmp_path / "intermediate-link"
    intermediate_link.symlink_to(actual.parent, target_is_directory=True)

    with pytest.raises(OSError):
        verifier._LocalReceiptDirectory(root_link)
    with pytest.raises(OSError):
        verifier._LocalReceiptDirectory(intermediate_link / "receipts")


def test_receipt_directory_validates_leaf_names_types_sizes_and_json(tmp_path: Path) -> None:
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    valid = {"receipt": "valid"}
    (receipts / "valid.json").write_text(json.dumps(valid), encoding="utf-8")
    (receipts / "other.json").symlink_to(receipts / "valid.json")
    (receipts / "directory.json").mkdir()
    (receipts / "oversize.json").write_bytes(b"x" * 1_000_001)
    (receipts / "duplicate.json").write_text('{"receipt": 1, "receipt": 2}', encoding="utf-8")
    (receipts / "valid.json.json").write_text(
        json.dumps({"receipt": "distinct-suffix"}), encoding="utf-8"
    )
    store = verifier._LocalReceiptDirectory(receipts)

    assert store.load("valid") == valid
    assert store.load("../valid") is None
    assert store.load("other") is None
    assert store.load("directory") is None
    assert store.load("oversize") is None
    assert store.load("duplicate") is None
    assert store.load("valid.json") == {"receipt": "distinct-suffix"}


def test_production_route_rejects_duplicate_receipt_binding(tmp_path: Path) -> None:
    fixture = _real_production_fixture(tmp_path)
    duplicate_id = fixture["samples"][0]["selected"]["receipt_id"]
    fixture["samples"][1]["selected"]["receipt_id"] = duplicate_id
    _rehash_sample(fixture["samples"][1])
    fixture["authorities"] = [_authority(item) for item in fixture["samples"]]
    fixture["candidate"] = _evaluate(
        fixture["samples"],
        closure=fixture["closure"],
        authoritative_evidence=fixture["authorities"],
    )

    route = _route_real_fixture(fixture)

    assert route["command"] == "just check"
    assert "duplicate-or-invalid-local-receipt" in route["reasons"]


def test_production_route_rechecks_receipts_on_second_pass(tmp_path: Path) -> None:
    fixture = _real_production_fixture(tmp_path)

    class ChangingReceipts:
        def __init__(self) -> None:
            self.loads = 0

        def load(self, receipt_id: str) -> object:
            self.loads += 1
            stored = fixture["receipts"].load(receipt_id)
            if self.loads > shadow.MIN_QUALIFYING_CHANGES * 2 and isinstance(stored, dict):
                return {**stored, "wall_seconds": stored["wall_seconds"] + 1}
            return stored

    changing = ChangingReceipts()
    route = _route_real_fixture(fixture, receipts=changing)

    assert changing.loads == shadow.MIN_QUALIFYING_CHANGES * 4
    assert route["command"] == "just check"
    assert "production-authority-snapshot-changed" in route["reasons"]
    assert "completed-local-receipt-not-verifiable" in route["reasons"]


def test_production_route_rechecks_cleanliness_after_receipt_pass(tmp_path: Path) -> None:
    fixture = _real_production_fixture(tmp_path)
    repo = fixture["repo"]

    class DirtyingReceipts:
        def __init__(self) -> None:
            self.loads = 0

        def load(self, receipt_id: str) -> object:
            self.loads += 1
            stored = fixture["receipts"].load(receipt_id)
            if self.loads == shadow.MIN_QUALIFYING_CHANGES * 4:
                (repo / "unknown-after-verification.py").write_text(
                    "late unknown input\n", encoding="utf-8"
                )
            return stored

    route = _route_real_fixture(fixture, receipts=DirtyingReceipts())

    assert route["command"] == "just check"
    assert "production-authority-snapshot-changed" in route["reasons"]


def test_git_adapter_ignores_replace_refs_and_ambient_git_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _real_production_fixture(tmp_path)
    repo = fixture["repo"]
    original = _git(repo, "rev-parse", "HEAD")
    original_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    (repo / "tracked.txt").write_text("replacement object\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "replacement")
    replacement = _git(repo, "rev-parse", "HEAD")
    _git(repo, "reset", "--hard", original)
    _git(repo, "replace", original, replacement)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git(decoy, "init", "-b", "main")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(decoy / ".git" / "objects"))
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(repo / ".git" / "objects"))

    assert fixture["git"].commit_tree(original) == original_tree
    assert fixture["git"].checkout_snapshot() is not None


def test_git_adapter_uses_pinned_real_git_when_ambient_path_is_hostile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _real_production_fixture(tmp_path)
    expected_head = _git(fixture["repo"], "rev-parse", "HEAD")
    hostile_bin = tmp_path / "hostile-bin"
    hostile_bin.mkdir()
    marker = tmp_path / "hostile-git-ran"
    hostile_git = hostile_bin / "git"
    hostile_git.write_text(
        f"#!/bin/sh\n/usr/bin/touch '{marker}'\nexit 99\n",
        encoding="utf-8",
    )
    hostile_git.chmod(0o755)
    monkeypatch.setenv("PATH", str(hostile_bin))

    snapshot = fixture["git"].checkout_snapshot()

    assert snapshot is not None
    assert snapshot.ref == "refs/heads/main"
    assert snapshot.head == expected_head
    assert marker.exists() is False


def test_public_production_route_derives_a_stable_live_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _real_production_fixture(tmp_path)
    _prepare_public_production_fixture(fixture, monkeypatch)

    route = verifier.route_production_validation(
        closure=fixture["closure"],
        samples=fixture["samples"],
        authoritative_evidence=fixture["authorities"],
        candidate=fixture["candidate"],
        current_plan=fixture["plan"],
        boundary="submit",
        is_leaf=True,
        local_enabled=True,
    )

    assert route == {
        "command": "just test-closure module.alpha",
        "selective": True,
        "closure": "module.alpha",
        "reasons": [],
    }


def test_production_route_rejects_replayed_plan_after_live_head_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _real_production_fixture(tmp_path)
    _prepare_public_production_fixture(fixture, monkeypatch)
    repo = fixture["repo"]
    (repo / "tracked.txt").write_text("advanced live checkout\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "advance live checkout")

    route = verifier.route_production_validation(
        closure=fixture["closure"],
        samples=fixture["samples"],
        authoritative_evidence=fixture["authorities"],
        candidate=fixture["candidate"],
        current_plan=fixture["plan"],
        boundary="submit",
        is_leaf=True,
        local_enabled=True,
    )

    assert route["selective"] is False
    assert route["command"] == "just check"
    assert "current-plan-does-not-match-live-checkout" in route["reasons"]


def test_production_route_rejects_head_drift_during_final_receipt_verification(
    tmp_path: Path,
) -> None:
    fixture = _real_production_fixture(tmp_path)
    repo = fixture["repo"]

    class HeadAdvancingReceipts:
        def __init__(self) -> None:
            self.loads = 0

        def load(self, receipt_id: str) -> object:
            stored = fixture["receipts"].load(receipt_id)
            self.loads += 1
            if self.loads == shadow.MIN_QUALIFYING_CHANGES * 4:
                (repo / "tracked.txt").write_text("mid-verification drift\n", encoding="utf-8")
                _git(repo, "add", "tracked.txt")
                _git(repo, "commit", "-m", "drift during verification")
            return stored

    route = verifier._route_production_validation_with_ports(
        closure=fixture["closure"],
        samples=fixture["samples"],
        authoritative_evidence=fixture["authorities"],
        candidate=fixture["candidate"],
        current_plan=fixture["plan"],
        boundary="submit",
        is_leaf=True,
        local_enabled=True,
        git=fixture["git"],
        receipts=HeadAdvancingReceipts(),
        selector_digest=SELECTOR_DIGEST,
    )

    assert route["selective"] is False
    assert route["command"] == "just check"
    assert "production-authority-snapshot-changed" in route["reasons"]


def test_verifier_rejects_nonexistent_commit_even_with_rehashed_caller_authority(
    tmp_path: Path,
) -> None:
    fixture = _real_production_fixture(tmp_path)
    sample = fixture["samples"][0]
    sample["commit"] = "f" * 40
    sample["tree"] = "e" * 40
    sample["plan"]["range"]["head"] = sample["commit"]
    for receipt_name in ("selected", "full"):
        sample[receipt_name]["commit"] = sample["commit"]
        sample[receipt_name]["tree"] = sample["tree"]
    _rehash_sample(sample)
    _store_receipts(fixture["receipt_root"], sample)
    authorities = [_authority(item) for item in fixture["samples"]]
    candidate = _evaluate(
        fixture["samples"],
        closure=fixture["closure"],
        authoritative_evidence=authorities,
    )

    verified = verifier._verify_production_evidence(
        closure=fixture["closure"],
        samples=fixture["samples"],
        authoritative_evidence=authorities,
        candidate=candidate,
        current_plan=fixture["plan"],
        git=fixture["git"],
        receipts=fixture["receipts"],
        selector_digest=SELECTOR_DIGEST,
    )

    assert verified.route_binding is None
    assert "sample-commit-tree-or-ancestry-not-verifiable" in verified.errors


def test_verifier_rejects_rehashed_caller_receipt_not_in_local_store(tmp_path: Path) -> None:
    fixture = _real_production_fixture(tmp_path)
    sample = fixture["samples"][0]
    sample["full"]["wall_seconds"] = 139.0
    _rehash_sample(sample)
    authorities = [_authority(item) for item in fixture["samples"]]
    candidate = _evaluate(
        fixture["samples"],
        closure=fixture["closure"],
        authoritative_evidence=authorities,
    )

    verified = verifier._verify_production_evidence(
        closure=fixture["closure"],
        samples=fixture["samples"],
        authoritative_evidence=authorities,
        candidate=candidate,
        current_plan=fixture["plan"],
        git=fixture["git"],
        receipts=fixture["receipts"],
        selector_digest=SELECTOR_DIGEST,
    )

    assert verified.route_binding is None
    assert "completed-local-receipt-not-verifiable" in verified.errors


def test_verifier_rejects_real_commit_outside_configured_integration_ancestry(
    tmp_path: Path,
) -> None:
    fixture = _real_production_fixture(tmp_path)
    repo = fixture["repo"]
    sample = fixture["samples"][0]
    base = sample["plan"]["range"]["base"]
    _git(repo, "checkout", "-b", "unmerged", base)
    (repo / "tracked.txt").write_text("unmerged\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "unmerged sample")
    sample["commit"] = _git(repo, "rev-parse", "HEAD")
    sample["tree"] = _git(repo, "rev-parse", "HEAD^{tree}")
    _git(repo, "checkout", "main")
    sample["plan"]["range"]["head"] = sample["commit"]
    for receipt_name in ("selected", "full"):
        sample[receipt_name]["commit"] = sample["commit"]
        sample[receipt_name]["tree"] = sample["tree"]
    _rehash_sample(sample)
    _store_receipts(fixture["receipt_root"], sample)
    authorities = [_authority(item) for item in fixture["samples"]]
    candidate = _evaluate(
        fixture["samples"],
        closure=fixture["closure"],
        authoritative_evidence=authorities,
    )

    verified = verifier._verify_production_evidence(
        closure=fixture["closure"],
        samples=fixture["samples"],
        authoritative_evidence=authorities,
        candidate=candidate,
        current_plan=fixture["plan"],
        git=fixture["git"],
        receipts=fixture["receipts"],
        selector_digest=SELECTOR_DIGEST,
    )

    assert verified.route_binding is None
    assert "sample-commit-tree-or-ancestry-not-verifiable" in verified.errors


def test_checked_evidence_mutation_is_rejected() -> None:
    checked = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    checked["activation"]["enabled"] = True

    errors = shadow.validate_checked_evidence(checked, ROOT)

    assert any("checked evidence drifted" in error for error in errors)


def test_policy_module_never_runs_tests_or_mutates_the_repository() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    verifier_source = VERIFIER_SCRIPT.read_text(encoding="utf-8")

    assert "subprocess" not in source
    assert "os.system" not in source
    assert "write_text" not in source
    assert "write_bytes" not in source
    assert "git " not in source
    assert "write_text" not in verifier_source
    assert "write_bytes" not in verifier_source
    assert 'self._run("push"' not in verifier_source
    assert 'self._run("checkout"' not in verifier_source
    assert 'self._run("commit"' not in verifier_source
    assert "requests" not in verifier_source
    assert "socket" not in verifier_source
