#!/usr/bin/env python3
"""Evaluate checked shadow evidence without executing or selecting tests.

This module is deliberately a pure policy boundary.  It can describe when a recorded selector
plan would be safe at a local leaf boundary, but it cannot run a command, change Beadhive
configuration, or activate a closure.  The checked repository evidence currently admits no
closure, so every real validation boundary continues to use ``just check``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CERTIFICATION_PATH = ROOT / "docs" / "proof" / "bh-ck1t6.1-test-closure-certification.json"
EVIDENCE_PATH = ROOT / "docs" / "proof" / "bh-ck1t6.3-shadow-activation.json"
SELECTOR_PATH = ROOT / "scripts" / "test_impact_selector.py"

SCHEMA_VERSION = 1
POLICY_VERSION = "bh-test-closure-shadow-policy-v1"
SELECTOR_VERSION = "bh-test-impact-selector-v1"
FULL_GATE = "just check"
MIN_QUALIFYING_CHANGES = 30
MIN_WINDOW_DAYS = 60
MAX_WALL_RATIO = 0.5
MIN_MEDIAN_SAVINGS_SECONDS = 30.0
REQUIRED_CHANGE_CLASSES = frozenset(
    {
        "owned-implementation",
        "public-port-or-contract",
        "reverse-dependent-or-multi-file",
    }
)
LOCAL_LEAF_BOUNDARIES = frozenset({"check", "submit", "pristine-review"})
FULL_ONLY_BOUNDARIES = frozenset(
    {
        "leaf-merge",
        "epic-finish",
        "workstream-submit",
        "workstream-review",
        "scheduled",
        "release",
    }
)
SAMPLE_FIELDS = frozenset(
    {
        "sample_id",
        "provenance",
        "change_class",
        "qualifying_merged_change",
        "observed_at",
        "commit",
        "tree",
        "closure_id",
        "source_revision",
        "closure_input_digest",
        "selector",
        "plan",
        "selected",
        "full",
        "omitted_tests",
        "fallback_reasons",
        "escapes",
    }
)
PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "selector",
        "range",
        "changes",
        "decision",
        "selection_scope",
        "confidence",
        "commands",
        "tests",
        "candidate_closures",
        "changed_modules",
        "dependency_paths",
        "contracts",
        "closure_digests",
        "exclusions",
        "fallback_reasons",
        "artifact_errors",
        "activation",
        "plan_digest",
    }
)
PLAN_RANGE_FIELDS = frozenset({"base", "head", "merge_base", "merge_base_status"})
PLAN_CANDIDATE_FIELDS = frozenset(
    {
        "id",
        "kind",
        "command",
        "tests",
        "contracts",
        "input_digest",
        "confidence",
        "relationships",
        "applicability",
    }
)
SELECTED_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "receipt_id",
        "status",
        "command",
        "commit",
        "tree",
        "tests",
        "exit_code",
        "failures",
        "wall_seconds",
        "compute_seconds",
        "queue_seconds",
        "flake_count",
        "receipt_digest",
    }
)
FULL_RECEIPT_FIELDS = SELECTED_RECEIPT_FIELDS | {"relevant_failures"}
AUTHORITY_FIELDS = frozenset(
    {
        "sample_id",
        "provenance",
        "commit",
        "tree",
        "merge_verified",
        "sample_digest",
        "plan_digest",
        "selected_receipt_digest",
        "full_receipt_digest",
    }
)
ROUTE_BINDING_FIELDS = frozenset(
    {
        "schema_version",
        "plan_digest",
        "base",
        "head",
        "merge_base",
        "tree",
        "closure_id",
        "closure_input_digest",
        "source_revision",
        "candidate_decision_digest",
        "authoritative_evidence_digest",
    }
)


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical_digest(value: object) -> str:
    return _digest_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _without_digest(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != field}


def _is_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _string_list(value: object, *, unique: bool = True) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and item for item in value)
        and (not unique or len(value) == len(set(value)))
    )


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _median(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _empty_measurements() -> dict[str, Any]:
    return {
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


def _applicability_reasons(closure: Mapping[str, Any]) -> set[str]:
    applicability = closure.get("current_applicability")
    if not isinstance(applicability, Mapping):
        return {"current-applicability-not-proven"}
    reasons = applicability.get("fallback_reasons")
    if applicability.get("applicable") is not True or not isinstance(reasons, list) or reasons:
        return {"current-applicability-not-proven"}
    for recorded, observed in (
        ("recorded_input_digest", "observed_input_digest"),
        ("recorded_registry_digest", "observed_registry_digest"),
        ("recorded_material_digest", "observed_material_digest"),
    ):
        left = applicability.get(recorded)
        right = applicability.get(observed)
        if not isinstance(left, str) or not left or left != right:
            return {"current-applicability-not-proven"}
    return set()


def _prerequisite_reasons(closure: Mapping[str, Any]) -> set[str]:
    reasons = _applicability_reasons(closure)
    certification = closure.get("certification")
    if not isinstance(certification, Mapping) or certification.get("status") != "certified":
        reasons.add("closure-not-certified")
    if not isinstance(certification, Mapping) or not certification.get(
        "eligible_for_selective_activation"
    ):
        reasons.add("closure-not-activation-eligible")
    if closure.get("confidence") != "high":
        reasons.add("confidence-below-high")
    if closure.get("enforceable_port") is not True:
        reasons.add("unenforceable-public-port")
    if closure.get("input_digest") != closure.get("observed_input_digest"):
        reasons.add("closure-input-digest-mismatch")
    coverage = closure.get("coverage_mapping")
    if (
        not isinstance(coverage, Mapping)
        or coverage.get("mode") != "dynamic-per-test"
        or not coverage.get("per_test_contexts")
        or coverage.get("input_digest") != closure.get("input_digest")
        or not coverage.get("source_revision")
    ):
        reasons.add("missing-or-stale-dynamic-trace")
    return reasons


def evaluate_candidate(
    closure: Mapping[str, Any],
    samples: Sequence[Mapping[str, Any]],
    *,
    selector_version: str,
    selector_digest: str,
    authoritative_evidence: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Return a deterministic eligibility decision from already-recorded observations."""
    reasons = _prerequisite_reasons(closure)
    closure_id = str(closure.get("id") or "")
    closure_digest = closure.get("input_digest")
    command = str(closure.get("command") or f"just test-closure {closure_id}")
    qualifying: list[Mapping[str, Any]] = []
    timestamps: list[datetime] = []
    selected_wall: list[float] = []
    full_wall: list[float] = []
    selected_compute: list[float] = []
    full_compute: list[float] = []
    queue: list[float] = []
    flake_count = 0
    miss_count = 0
    fallback_count = 0
    change_classes: set[str] = set()
    evidence_modes: set[str] = set()
    sample_ids: set[str] = set()
    commits: set[str] = set()

    authorities: dict[str, Mapping[str, Any]] = {}
    if len(authoritative_evidence) != len(samples):
        reasons.add("authoritative-evidence-cardinality-mismatch")
    for authority in authoritative_evidence:
        if set(authority) != AUTHORITY_FIELDS:
            reasons.add("invalid-authoritative-evidence-schema")
            continue
        authority_id = authority.get("sample_id")
        if not isinstance(authority_id, str) or not authority_id or authority_id in authorities:
            reasons.add("invalid-authoritative-evidence-schema")
            continue
        authorities[authority_id] = authority

    for sample in samples:
        if set(sample) != SAMPLE_FIELDS:
            reasons.add("invalid-shadow-sample-schema")
        sample_fallbacks = sample.get("fallback_reasons")
        if not _string_list(sample_fallbacks):
            reasons.add("invalid-shadow-sample")
            sample_fallbacks = ["invalid-shadow-sample"]
        if sample_fallbacks:
            fallback_count += 1
            reasons.add("shadow-sample-fell-back")
        if sample.get("qualifying_merged_change") is not True:
            reasons.add("invalid-shadow-sample-cardinality")
            continue
        qualifying.append(sample)
        sample_id = sample.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id or sample_id in sample_ids:
            reasons.add("duplicate-or-invalid-shadow-sample-id")
        else:
            sample_ids.add(sample_id)
        provenance = sample.get("provenance")
        if provenance not in {"qualifying-merged-change", "simulation-only"}:
            reasons.add("invalid-shadow-provenance")
        else:
            evidence_modes.add(str(provenance))
        change_class = sample.get("change_class")
        if not isinstance(change_class, str):
            reasons.add("invalid-shadow-sample")
        else:
            change_classes.add(change_class)
        timestamp = _parse_timestamp(sample.get("observed_at"))
        if timestamp is None or timestamp.tzinfo is None:
            reasons.add("invalid-shadow-sample")
        else:
            timestamps.append(timestamp)

        commit = sample.get("commit")
        tree = sample.get("tree")
        if not _is_sha(commit) or not _is_sha(tree):
            reasons.add("invalid-authoritative-git-provenance")
        if isinstance(commit, str) and commit in commits:
            reasons.add("duplicate-shadow-commit")
        elif isinstance(commit, str):
            commits.add(commit)

        coverage = closure.get("coverage_mapping")
        if (
            sample.get("closure_id") != closure_id
            or sample.get("closure_input_digest") != closure_digest
            or not isinstance(coverage, Mapping)
            or sample.get("source_revision") != coverage.get("source_revision")
        ):
            reasons.add("closure-source-binding-mismatch")
        if sample.get("closure_input_digest") != closure_digest:
            reasons.add("closure-input-digest-mismatch")

        selected = sample.get("selected")
        full = sample.get("full")
        if not isinstance(selected, Mapping) or not isinstance(full, Mapping):
            reasons.add("invalid-shadow-sample")
            continue
        if selected.get("tree") != tree or full.get("tree") != tree:
            reasons.add("selected-full-tree-mismatch")
        if selected.get("commit") != commit or full.get("commit") != commit:
            reasons.add("selected-full-commit-mismatch")
        selector = sample.get("selector")
        if (
            not isinstance(selector, Mapping)
            or set(selector) != {"version", "digest"}
            or selector.get("version") != selector_version
            or selector.get("digest") != selector_digest
            or not _is_digest(selector.get("digest"))
        ):
            reasons.add("selector-drift")
        plan = sample.get("plan")
        if not isinstance(plan, Mapping):
            reasons.add("missing-selector-plan")
        else:
            if set(plan) != PLAN_FIELDS:
                reasons.add("invalid-selector-plan-schema")
            recomputed_plan_digest = _canonical_digest(_without_digest(plan, "plan_digest"))
            if plan.get("plan_digest") != recomputed_plan_digest:
                reasons.add("selector-plan-digest-mismatch")
            if plan.get("selector") != selector:
                reasons.add("selector-drift")
            if plan.get("decision") != "selective" or plan.get("fallback_reasons"):
                reasons.add("shadow-sample-fell-back")
            if plan.get("commands") != [command] or plan.get("tests") != selected.get("tests"):
                reasons.add("selector-plan-run-mismatch")
            plan_range = plan.get("range")
            if (
                not isinstance(plan_range, Mapping)
                or set(plan_range) != PLAN_RANGE_FIELDS
                or plan_range.get("head") != commit
                or plan_range.get("merge_base_status") != "resolved"
                or not all(
                    _is_sha(plan_range.get(field)) for field in ("base", "head", "merge_base")
                )
            ):
                reasons.add("selector-plan-range-mismatch")
            candidates = plan.get("candidate_closures")
            candidate = None
            if isinstance(candidates, list) and len(candidates) == 1:
                candidate = candidates[0]
            if (
                not isinstance(candidate, Mapping)
                or set(candidate) != PLAN_CANDIDATE_FIELDS
                or candidate.get("id") != closure_id
                or candidate.get("command") != command
                or candidate.get("input_digest") != closure_digest
                or candidate.get("tests") != selected.get("tests")
                or candidate.get("applicability") != closure.get("current_applicability")
                or plan.get("changed_modules") != [closure_id]
                or plan.get("closure_digests") != {closure_id: closure_digest}
            ):
                reasons.add("selector-plan-closure-binding-mismatch")
        if selected.get("command") != command or full.get("command") != FULL_GATE:
            reasons.add("invalid-shadow-command")
        if set(selected) != SELECTED_RECEIPT_FIELDS or set(full) != FULL_RECEIPT_FIELDS:
            reasons.add("invalid-receipt-schema")
        for result in (selected, full):
            digest = result.get("receipt_digest")
            if not _is_digest(digest) or digest != _canonical_digest(
                _without_digest(result, "receipt_digest")
            ):
                reasons.add("invalid-receipt-digest")
            if result.get("schema_version") != 1 or result.get("status") != "completed":
                reasons.add("incomplete-receipt")
            if not _string_list(result.get("tests")) or not _string_list(result.get("failures")):
                reasons.add("incomplete-test-inventory")
            exit_code = result.get("exit_code")
            failures = result.get("failures")
            if (
                isinstance(exit_code, bool)
                or not isinstance(exit_code, int)
                or exit_code < 0
                or not isinstance(failures, list)
                or (exit_code == 0) != (not failures)
            ):
                reasons.add("inconsistent-test-result")
        selected_tests = selected.get("tests")
        full_tests = full.get("tests")
        omitted_tests = sample.get("omitted_tests")
        inventories = (selected_tests, full_tests, omitted_tests)
        if all(_string_list(value) for value in inventories):
            if not set(selected_tests).issubset(full_tests):
                reasons.add("selected-tests-not-subset-of-full-inventory")
            expected_omitted = sorted(set(full_tests) - set(selected_tests))
            if sorted(omitted_tests) != expected_omitted:
                reasons.add("omitted-test-inventory-mismatch")
        relevant_failures = full.get("relevant_failures")
        escapes = sample.get("escapes")
        if not _string_list(relevant_failures) or not isinstance(escapes, list):
            reasons.add("invalid-shadow-sample")
            relevant_failures = []
            escapes = []
        elif not set(relevant_failures).issubset(full.get("failures", ())):
            reasons.add("inconsistent-relevant-failure-inventory")
        selected_green = selected.get("exit_code") == 0
        if (
            selected_green
            and full.get("exit_code") != 0
            and set(relevant_failures) != set(full.get("failures", ()))
        ):
            reasons.add("inconsistent-relevant-failure-inventory")
        if selected_green and relevant_failures:
            miss_count += 1
            reasons.add("selected-green-full-red-escape")
            if (
                not escapes
                or {escape.get("failure") for escape in escapes if isinstance(escape, Mapping)}
                != set(relevant_failures)
                or any(
                    not isinstance(escape, Mapping)
                    or set(escape) != {"failure", "root_cause", "correction", "replay"}
                    or not escape.get("root_cause")
                    or not escape.get("correction")
                    or escape.get("replay") != "passed"
                    for escape in escapes
                )
            ):
                reasons.add("unexplained-or-unreplayed-escape")
        elif escapes:
            reasons.add("escape-ledger-inconsistent")

        authority = authorities.get(str(sample_id))
        if (
            authority is None
            or authority.get("provenance") != provenance
            or authority.get("commit") != commit
            or authority.get("tree") != tree
            or authority.get("sample_digest") != _canonical_digest(sample)
            or authority.get("plan_digest")
            != (plan.get("plan_digest") if isinstance(plan, Mapping) else None)
            or authority.get("selected_receipt_digest") != selected.get("receipt_digest")
            or authority.get("full_receipt_digest") != full.get("receipt_digest")
            or (
                provenance == "qualifying-merged-change"
                and authority.get("merge_verified") is not True
            )
            or (provenance == "simulation-only" and authority.get("merge_verified") is not False)
        ):
            reasons.add("receipt-or-provenance-not-authoritatively-validated")

        values = {
            "selected_wall": _number(selected.get("wall_seconds")),
            "full_wall": _number(full.get("wall_seconds")),
            "selected_compute": _number(selected.get("compute_seconds")),
            "full_compute": _number(full.get("compute_seconds")),
            "selected_queue": _number(selected.get("queue_seconds")),
            "full_queue": _number(full.get("queue_seconds")),
        }
        if any(value is None for value in values.values()):
            reasons.add("incomplete-timing-measurement")
        else:
            selected_wall.append(values["selected_wall"] or 0.0)
            full_wall.append(values["full_wall"] or 0.0)
            selected_compute.append(values["selected_compute"] or 0.0)
            full_compute.append(values["full_compute"] or 0.0)
            queue.append((values["selected_queue"] or 0.0) + (values["full_queue"] or 0.0))
        selected_flakes = selected.get("flake_count")
        full_flakes = full.get("flake_count")
        if (
            isinstance(selected_flakes, bool)
            or not isinstance(selected_flakes, int)
            or selected_flakes < 0
            or isinstance(full_flakes, bool)
            or not isinstance(full_flakes, int)
            or full_flakes < 0
        ):
            reasons.add("incomplete-flake-measurement")
        else:
            flake_count += selected_flakes + full_flakes

    if len(qualifying) < MIN_QUALIFYING_CHANGES:
        reasons.add("insufficient-qualifying-shadow-samples")
    if not REQUIRED_CHANGE_CLASSES.issubset(change_classes):
        reasons.add("representative-change-classes-missing")
    if "simulation-only" in evidence_modes and closure.get("simulation_control") is not True:
        reasons.add("simulation-evidence-not-authoritative")
    if len(evidence_modes) > 1:
        reasons.add("mixed-shadow-provenance")
    if not samples:
        reasons.add("closure-input-digest-mismatch")
        reasons.add("dynamic-trace-revision-mismatch")
    unique_trees = {sample.get("tree") for sample in qualifying if sample.get("tree")}
    if len(unique_trees) != len(qualifying):
        reasons.add("duplicate-shadow-tree")
    window_days: int | None = None
    if len(timestamps) >= 2:
        window_days = (max(timestamps) - min(timestamps)).days
    if window_days is None or window_days < MIN_WINDOW_DAYS:
        reasons.add("shadow-window-too-young")

    selected_wall_median = _median(selected_wall)
    full_wall_median = _median(full_wall)
    savings = (
        full_wall_median - selected_wall_median
        if selected_wall_median is not None and full_wall_median is not None
        else None
    )
    ratio = (
        selected_wall_median / full_wall_median
        if selected_wall_median is not None and full_wall_median not in (None, 0.0)
        else None
    )
    if (
        ratio is None
        or savings is None
        or ratio > MAX_WALL_RATIO
        or savings < MIN_MEDIAN_SAVINGS_SECONDS
    ):
        reasons.add("timing-target-not-met")

    measurements = _empty_measurements()
    if samples:
        measurements = {
            "status": "measured",
            "sample_count": len(samples),
            "qualifying_count": len(qualifying),
            "window_days": window_days,
            "selected_wall_median_seconds": selected_wall_median,
            "full_wall_median_seconds": full_wall_median,
            "wall_savings_median_seconds": savings,
            "wall_ratio": ratio,
            "selected_compute_median_seconds": _median(selected_compute),
            "full_compute_median_seconds": _median(full_compute),
            "queue_median_seconds": _median(queue),
            "flake_count": flake_count,
            "miss_count": miss_count,
            "fallback_count": fallback_count,
            "fallback_rate": fallback_count / len(samples),
        }
    evidence_digest = _canonical_digest(
        sorted(authoritative_evidence, key=lambda item: str(item.get("sample_id", "")))
    )
    decision = {
        "id": closure_id,
        "command": command,
        "eligible": not reasons,
        "reasons": sorted(reasons),
        "selector": {"version": selector_version, "digest": selector_digest},
        "input_digest": closure_digest,
        "evidence_mode": (
            next(iter(evidence_modes), "none") if len(evidence_modes) <= 1 else "mixed"
        ),
        "measurements": measurements,
    }
    if samples:
        decision["source_revision"] = (
            (closure.get("coverage_mapping") or {}).get("source_revision")
            if isinstance(closure.get("coverage_mapping"), Mapping)
            else None
        )
        decision["authoritative_evidence_digest"] = evidence_digest
        decision["decision_digest"] = _canonical_digest(decision)
    return decision


def route_validation(
    plan: Mapping[str, Any],
    candidate_decisions: Mapping[str, Mapping[str, Any]],
    *,
    boundary: str,
    is_leaf: bool,
    local_enabled: bool,
    allow_simulation: bool = False,
    route_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve one already-built plan; uncertainty always returns the ordinary full gate."""

    def full(*reasons: str) -> dict[str, Any]:
        return {
            "command": FULL_GATE,
            "selective": False,
            "closure": None,
            "reasons": sorted(set(reasons)),
        }

    if not local_enabled:
        return full("local-activation-disabled")
    if boundary not in LOCAL_LEAF_BOUNDARIES:
        return full("boundary-requires-full-gate")
    if not is_leaf:
        return full("non-leaf-work-item")
    fallback_reasons = plan.get("fallback_reasons")
    if plan.get("decision") != "selective" or fallback_reasons:
        reasons = ["selector-required-full-gate"]
        if isinstance(fallback_reasons, list):
            reasons.extend(str(reason) for reason in fallback_reasons)
        return full(*reasons)
    closures = plan.get("changed_modules")
    if not isinstance(closures, list) or len(closures) != 1:
        return full("multi-module-ambiguity")
    closure_id = str(closures[0])
    candidate = candidate_decisions.get(closure_id)
    if not isinstance(candidate, Mapping) or candidate.get("eligible") is not True:
        return full("closure-not-eligible")
    if candidate.get("evidence_mode") != "qualifying-merged-change" and not allow_simulation:
        return full("simulation-evidence-not-activatable")
    selector = plan.get("selector")
    if not isinstance(selector, Mapping) or selector != candidate.get("selector"):
        return full("selector-drift")
    commands = plan.get("commands")
    expected_command = candidate.get("command")
    if commands != [expected_command] or not isinstance(expected_command, str):
        return full("selector-command-drift")
    plan_range = plan.get("range")
    candidate_rows = plan.get("candidate_closures")
    candidate_row = None
    if isinstance(candidate_rows, list) and len(candidate_rows) == 1:
        candidate_row = candidate_rows[0]
    if (
        set(plan) != PLAN_FIELDS
        or plan.get("plan_digest") != _canonical_digest(_without_digest(plan, "plan_digest"))
        or not isinstance(plan_range, Mapping)
        or set(plan_range) != PLAN_RANGE_FIELDS
        or plan_range.get("merge_base_status") != "resolved"
        or not all(_is_sha(plan_range.get(field)) for field in ("base", "head", "merge_base"))
        or not isinstance(candidate_row, Mapping)
        or set(candidate_row) != PLAN_CANDIDATE_FIELDS
        or candidate_row.get("id") != closure_id
        or candidate_row.get("command") != expected_command
        or candidate_row.get("input_digest") != candidate.get("input_digest")
        or candidate_row.get("tests") != plan.get("tests")
        or plan.get("closure_digests") != {closure_id: candidate.get("input_digest")}
    ):
        return full("current-selector-plan-mismatch")
    if candidate.get("decision_digest") != _canonical_digest(
        _without_digest(candidate, "decision_digest")
    ):
        return full("candidate-decision-digest-mismatch")
    if route_binding is None or set(route_binding) != ROUTE_BINDING_FIELDS:
        return full("missing-or-invalid-route-binding")
    if (
        route_binding.get("schema_version") != 1
        or route_binding.get("plan_digest") != plan.get("plan_digest")
        or route_binding.get("base") != plan_range.get("base")
        or route_binding.get("head") != plan_range.get("head")
        or route_binding.get("merge_base") != plan_range.get("merge_base")
        or not _is_sha(route_binding.get("tree"))
        or route_binding.get("closure_id") != closure_id
        or route_binding.get("closure_input_digest") != candidate.get("input_digest")
        or route_binding.get("source_revision") != candidate.get("source_revision")
        or route_binding.get("candidate_decision_digest") != candidate.get("decision_digest")
        or route_binding.get("authoritative_evidence_digest")
        != candidate.get("authoritative_evidence_digest")
    ):
        return full("current-route-evidence-mismatch")
    return {
        "command": expected_command,
        "selective": True,
        "closure": closure_id,
        "reasons": [],
    }


def build_checked_evidence(root: Path = ROOT) -> dict[str, Any]:
    certification_path = root / CERTIFICATION_PATH.relative_to(ROOT)
    selector_path = root / SELECTOR_PATH.relative_to(ROOT)
    policy_path = root / Path(__file__).resolve().relative_to(ROOT)
    certification = _load_json(certification_path)
    selector_digest = _digest_bytes(selector_path.read_bytes())
    decisions = [
        evaluate_candidate(
            closure,
            (),
            selector_version=SELECTOR_VERSION,
            selector_digest=selector_digest,
        )
        for closure in certification.get("closures", ())
    ]
    eligible = sorted(item["id"] for item in decisions if item["eligible"])
    inputs = {
        "certification_artifact": {
            "path": CERTIFICATION_PATH.relative_to(ROOT).as_posix(),
            "digest": _digest_bytes(certification_path.read_bytes()),
        },
        "selector": {
            "path": SELECTOR_PATH.relative_to(ROOT).as_posix(),
            "version": SELECTOR_VERSION,
            "digest": selector_digest,
        },
        "shadow_policy": {
            "path": policy_path.relative_to(root).as_posix(),
            "version": POLICY_VERSION,
            "digest": _digest_bytes(policy_path.read_bytes()),
        },
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "local-leaf-shadow-policy-only",
        "inputs": inputs,
        "input_digest": _canonical_digest(inputs),
        "policy": {
            "minimum_qualifying_merged_changes": MIN_QUALIFYING_CHANGES,
            "minimum_window_days": MIN_WINDOW_DAYS,
            "required_change_classes": sorted(REQUIRED_CHANGE_CLASSES),
            "zero_selected_green_full_red_escapes": True,
            "maximum_selected_to_full_wall_ratio": MAX_WALL_RATIO,
            "minimum_median_wall_savings_seconds": MIN_MEDIAN_SAVINGS_SECONDS,
            "local_leaf_boundaries": sorted(LOCAL_LEAF_BOUNDARIES),
            "full_only_boundaries": sorted(FULL_ONLY_BOUNDARIES),
            "rollback": [
                "local-activation-disabled",
                "selected-green-full-red-escape",
                "selector-or-input-digest-drift",
                "ownership-or-confidence-degradation",
                "uncertain-or-full-selector-plan",
            ],
        },
        "activation": {"enabled": False, "eligible_closures": eligible},
        "observations": {
            "status": "not-started-no-certified-candidates",
            "samples": [],
            "note": "No real shadow result is claimed; simulation-only controls live in tests.",
        },
        "closure_decisions": decisions,
        "current_gate": {
            "command": FULL_GATE,
            "applies_to": (
                "all check, submit, review, merge, epic, workstream, scheduled, and "
                "release boundaries"
            ),
            "receipt_reuse": (
                "exact-tree full-gate receipts may be reused by existing lifecycle policy"
            ),
        },
    }


def validate_checked_evidence(evidence: Mapping[str, Any], root: Path = ROOT) -> tuple[str, ...]:
    expected = build_checked_evidence(root)
    if evidence != expected:
        return ("checked evidence drifted from current certification, selector, or policy inputs",)
    if evidence.get("activation") != {"enabled": False, "eligible_closures": []}:
        return ("checked evidence must leave real activation disabled",)
    return ()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the checked evidence")
    args = parser.parse_args(argv)
    if args.check:
        errors = validate_checked_evidence(_load_json(EVIDENCE_PATH), ROOT)
        for error in errors:
            print(f"error: {error}")
        if not errors:
            print("shadow policy evidence valid: 23 closures rejected, activation disabled")
        return 1 if errors else 0
    print(json.dumps(build_checked_evidence(ROOT), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
