#!/usr/bin/env python3
"""Build and verify the selective-CI operational closeout report.

The report composes checked certification, shadow, promotion, and registry artifacts. It records
unavailable measurements as null and never estimates savings from developer-focused timings.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "test_closure_operational_report.py"
CERTIFICATION_PATH = ROOT / "docs" / "proof" / "bh-ck1t6.1-test-closure-certification.json"
SHADOW_PATH = ROOT / "docs" / "proof" / "bh-ck1t6.3-shadow-activation.json"
PROMOTION_PATH = ROOT / "docs" / "proof" / "bh-ck1t6.4-promotion-policy.json"
REGISTRY_PATH = ROOT / "tests" / "closures.toml"
REGISTRY_TOOL_PATH = ROOT / "scripts" / "test_closures.py"
EVIDENCE_PATH = ROOT / "docs" / "proof" / "bh-ck1t6.5-selective-ci-operations.json"
REPORT_PATH = ROOT / "docs" / "SELECTIVE-CI-OPERATIONS.md"

SCHEMA_VERSION = 1
POLICY_VERSION = "bh-selective-ci-operations-v1"
EVIDENCE_DATE = "2026-09-10"
MEASUREMENT_FIELDS = (
    "full_suite_frequency",
    "miss_rate",
    "flake_rate",
    "measured_savings_seconds",
)
INVALIDATION = {
    "boundary-change": {
        "automatic_fallback": "just check",
        "requires_recertification": True,
        "source_trigger": "input-digest-mismatch",
    },
    "coverage-staleness": {
        "automatic_fallback": "just check",
        "requires_recertification": True,
        "source_trigger": "missing-or-stale-coverage",
    },
    "escaped-regression": {
        "automatic_fallback": "just check",
        "requires_recertification": True,
        "source_trigger": "selected-green-full-red-escape",
    },
    "schema-change": {
        "automatic_fallback": "just check",
        "requires_recertification": True,
        "source_trigger": "shared-contract-or-schema-change",
    },
    "tool-version-change": {
        "automatic_fallback": "just check",
        "requires_recertification": True,
        "source_trigger": "test-infrastructure-or-certifier-change",
    },
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    previous_bytecode = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        sys.modules[name] = module
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous_bytecode
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    return module


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _unavailable_measurements() -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": "no-qualifying-production-samples",
        "sample_count": 0,
        "before": {"wall_seconds": None, "compute_seconds": None, "queue_seconds": None},
        "after": {"wall_seconds": None, "compute_seconds": None, "queue_seconds": None},
        **{field: None for field in MEASUREMENT_FIELDS},
    }


def build_checked_evidence(root: Path = ROOT) -> dict[str, Any]:
    certification_path = root / CERTIFICATION_PATH.relative_to(ROOT)
    shadow_path = root / SHADOW_PATH.relative_to(ROOT)
    promotion_path = root / PROMOTION_PATH.relative_to(ROOT)
    registry_path = root / REGISTRY_PATH.relative_to(ROOT)
    registry_tool_path = root / REGISTRY_TOOL_PATH.relative_to(ROOT)
    certification = _read_json(certification_path)
    shadow = _read_json(shadow_path)
    promotion = _read_json(promotion_path)
    registry_tool = _load_module("closure_registry_for_operations", registry_tool_path)
    registry = registry_tool.load_registry(registry_path)
    registry_errors = registry_tool.validate_registry(registry, root)
    if registry_errors:
        raise ValueError("closure registry is invalid: " + "; ".join(registry_errors))

    closure_rows = certification.get("closures")
    shadow_rows = shadow.get("closure_decisions")
    if not isinstance(closure_rows, list) or not isinstance(shadow_rows, list):
        raise ValueError("certification and shadow closure inventories must be lists")
    registry_by_id = registry.by_id()
    shadow_by_id = {row.get("id"): row for row in shadow_rows if isinstance(row, Mapping)}
    ids = [row.get("id") for row in closure_rows if isinstance(row, Mapping)]
    if (
        len(ids) != len(closure_rows)
        or set(ids) != set(registry_by_id)
        or set(ids) != set(shadow_by_id)
    ):
        raise ValueError("registry, certification, and shadow closure inventories differ")
    if shadow.get("observations", {}).get("samples") != []:
        raise ValueError("measurement aggregation must be implemented before publishing samples")

    closures: list[dict[str, Any]] = []
    for row in closure_rows:
        assert isinstance(row, Mapping)
        closure_id = str(row["id"])
        certification_row = row.get("certification")
        if not isinstance(certification_row, Mapping):
            raise ValueError(f"closure {closure_id!r} lacks certification")
        triggers = row.get("fallback_triggers")
        if not isinstance(triggers, list) or not triggers:
            raise ValueError(f"closure {closure_id!r} lacks fail-closed triggers")
        owner = row.get("owner")
        if not isinstance(owner, str) or not owner:
            raise ValueError(f"closure {closure_id!r} lacks an owner")
        registry_row = registry_by_id[closure_id]
        closures.append(
            {
                "id": closure_id,
                "kind": registry_row.kind,
                "certification": certification_row.get("status"),
                "evidence_date": EVIDENCE_DATE,
                "owner": owner,
                "fallback": "just check",
                "recertification_triggers": sorted(str(trigger) for trigger in triggers),
            }
        )

    certified = sum(row["certification"] == "certified" for row in closures)
    uncertified = sum(row["certification"] == "uncertified" for row in closures)
    activation = promotion.get("activation")
    boundaries = promotion.get("boundaries")
    if not isinstance(activation, Mapping) or not isinstance(boundaries, Mapping):
        raise ValueError("promotion activation and boundaries must be objects")
    routes = activation.get("production_selective_routes")
    if routes != 0 or activation.get("eligible_closures") != []:
        raise ValueError("this closeout schema only supports the current zero-route state")

    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "evidence_date": EVIDENCE_DATE,
        "inputs": {
            "certification": _digest(certification_path),
            "generator": _digest(root / SCRIPT_PATH.relative_to(ROOT)),
            "promotion": _digest(promotion_path),
            "registry": _digest(registry_path),
            "shadow": _digest(shadow_path),
        },
        "summary": {
            "total": len(closures),
            "certified": certified,
            "uncertified": uncertified,
            "production_selective_routes": routes,
        },
        "closures": sorted(closures, key=lambda row: row["id"]),
        "measurements": {
            "commit": _unavailable_measurements(),
            "main-integration": _unavailable_measurements(),
            "note": (
                "No qualifying production samples exist. Focused developer timings and receipt "
                "reuse are not production selective-CI savings and are not extrapolated here."
            ),
        },
        "registration": {
            "applies_to": ["module", "plugin"],
            "requires": ["closure", "conformance-declaration"],
            "declaration_fields": [
                "owner_path",
                "source_paths",
                "tests",
                "shared_contracts-and-tests",
                "reverse_dependencies-and-tests",
            ],
            "gate": "just test-closure-check",
            "behavior": (
                "Discovery rejects a registered module or plugin without a present closure row; "
                "the row is its checked ownership and conformance declaration."
            ),
        },
        "invalidation": INVALIDATION,
        "boundaries": {
            "promoted": sorted(str(value) for value in boundaries.get("promoted", [])),
            "full_only": sorted(str(value) for value in boundaries.get("full_only", [])),
            "uncertainty_gate": boundaries.get("uncertainty_gate"),
            "safety_gate": boundaries.get("safety_gate"),
        },
        "rollback": promotion.get("rollback"),
    }


def validate_checked_evidence(evidence: Mapping[str, Any], root: Path = ROOT) -> tuple[str, ...]:
    try:
        expected = build_checked_evidence(root)
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return (f"cannot derive operational evidence: {exc}",)
    if evidence != expected:
        return ("checked operational evidence differs from derived source artifacts",)
    return ()


def _metric(value: object) -> str:
    return "unavailable" if value is None else str(value)


def render_report(evidence: Mapping[str, Any]) -> str:
    """Render the checked evidence as the operator-facing source of truth."""
    summary = evidence["summary"]
    measurements = evidence["measurements"]
    lines = [
        "# Selective-CI operational report",
        "",
        f"Evidence date: {evidence['evidence_date']} UTC",
        "",
        "## Current disposition",
        "",
        f"- Certified closures: **{summary['certified']}**",
        f"- Uncertified closures: **{summary['uncertified']}**",
        f"- Production selective routes: **{summary['production_selective_routes']}**",
        "",
        "No production selective-CI savings are claimed. Focused developer timings and",
        "exact-tree receipt reuse answer different questions and are not extrapolated into this",
        "report. Both promoted boundaries therefore retain unavailable measurements until",
        "qualifying production samples exist.",
        "",
        "## Production measurements",
    ]
    for boundary in ("commit", "main-integration"):
        row = measurements[boundary]
        lines.extend(
            [
                "",
                f"### `{boundary}`",
                "",
                f"- Qualifying samples: {row['sample_count']}",
                f"- Before wall time: {_metric(row['before']['wall_seconds'])}",
                f"- Before compute time: {_metric(row['before']['compute_seconds'])}",
                f"- Before queue delay: {_metric(row['before']['queue_seconds'])}",
                f"- After wall time: {_metric(row['after']['wall_seconds'])}",
                f"- After compute time: {_metric(row['after']['compute_seconds'])}",
                f"- After queue delay: {_metric(row['after']['queue_seconds'])}",
                f"- Full-suite frequency: {_metric(row['full_suite_frequency'])}",
                f"- Miss rate: {_metric(row['miss_rate'])}",
                f"- Flake rate: {_metric(row['flake_rate'])}",
                f"- Measured savings: {_metric(row['measured_savings_seconds'])}",
            ]
        )
    lines.extend(
        [
            "",
            "`unavailable` means there are zero qualifying observations, not zero cost or zero",
            "failures. The current policy routes all real work through a full gate because no",
            "closure is eligible.",
            "",
            "## Closure inventory",
            "",
            "Every row uses `just check` on uncertainty. A row can be reconsidered only after all",
            "listed triggers are cleared and fresh certification plus shadow evidence is checked.",
        ]
    )
    for row in evidence["closures"]:
        lines.extend(
            [
                "",
                f"### `{row['id']}`",
                "",
                f"- Kind: {row['kind']}",
                f"- State: {row['certification']}",
                f"- Evidence date: {row['evidence_date']}",
                f"- Owner: {row['owner']}",
                f"- Fallback: `{row['fallback']}`",
                "- Recertification triggers:",
                *(f"  - `{trigger}`" for trigger in row["recertification_triggers"]),
            ]
        )
    lines.extend(
        [
            "",
            "## Registration and invalidation",
            "",
            "A new module or plugin cannot register without a present closure and a conformance",
            "declaration. The checked closure row is that declaration: it names ownership, source",
            "scope, direct tests, shared-contract tests, and reverse-dependent tests.",
            "`just test-closure-check` discovers registrations and rejects missing or incomplete",
            "rows.",
            "",
            "| Trigger | Automatic fallback | Action |",
            "| --- | --- | --- |",
        ]
    )
    for trigger, policy in evidence["invalidation"].items():
        lines.append(
            f"| `{trigger}` | `{policy['automatic_fallback']}` | recertify before selective use |"
        )
    lines.extend(
        [
            "",
            "## Retained safety boundaries",
            "",
            f"The complete `{evidence['boundaries']['safety_gate']}` gate is mandatory for "
            "these boundaries:",
            "",
            *(f"- `{value}`" for value in evidence["boundaries"]["full_only"]),
            "",
            "Commit and main-integration routing may use a closure only after certification;",
            "every uncertainty uses `just check`. Changing",
            "`selective_ci.mode` from `certified` to `full` is the one-value rollback.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if args.write:
        evidence = build_checked_evidence()
        EVIDENCE_PATH.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        REPORT_PATH.write_text(render_report(evidence), encoding="utf-8")
        print(f"wrote {EVIDENCE_PATH.relative_to(ROOT)}")
        print(f"wrote {REPORT_PATH.relative_to(ROOT)}")
        return 0
    evidence = _read_json(EVIDENCE_PATH)
    errors = list(validate_checked_evidence(evidence))
    if REPORT_PATH.read_text(encoding="utf-8") != render_report(evidence):
        errors.append("checked operational Markdown report differs from derived evidence")
    if errors:
        print("selective-ci-operational-report: FAILED", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("selective-ci-operational-report: OK (24 uncertified, 0 production routes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
