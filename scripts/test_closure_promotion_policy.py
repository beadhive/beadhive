#!/usr/bin/env python3
"""Fail-closed promotion policy for certified affected test closures.

The policy does not execute tests.  Production eligibility still comes from the trusted shadow
verifier; this layer only widens a verified route to the explicitly configured commit and
main-integration boundaries, renders an auditable run plan, and keeps every safety boundary full.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "tests" / "selective-ci-policy.toml"
REGISTRY_PATH = ROOT / "tests" / "closures.toml"
CERTIFICATION_PATH = ROOT / "docs" / "proof" / "bh-ck1t6.1-test-closure-certification.json"
SHADOW_PATH = ROOT / "docs" / "proof" / "bh-ck1t6.3-shadow-activation.json"
EVIDENCE_PATH = ROOT / "docs" / "proof" / "bh-ck1t6.4-promotion-policy.json"
SELECTOR_PATH = ROOT / "scripts" / "test_impact_selector.py"
VERIFIER_PATH = ROOT / "scripts" / "test_closure_shadow_verifier.py"
TELEMETRY_RELEASE_PATH = ROOT / "docs" / "proof" / "official-v1-contract-compatibility.json"

SCHEMA_VERSION = 1
POLICY_VERSION = "bh-test-closure-promotion-v1"
FAST_FULL_GATE = "just check"
COMPLETE_FULL_GATE = "just check-all"
PROMOTED_BOUNDARIES = frozenset({"commit", "main-integration"})
FULL_ONLY_BOUNDARIES = frozenset(
    {
        "leaf-merge",
        "child-epic-finish",
        "final-workstream-submit",
        "final-workstream-review",
        "scheduled",
        "release",
    }
)


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


def _digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _digest(value: object) -> str:
    return _digest_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


@dataclass(frozen=True)
class Policy:
    mode: str
    promoted_boundaries: tuple[str, ...]
    full_only_boundaries: tuple[str, ...]


def load_policy(path: Path = CONFIG_PATH) -> Policy:
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    if set(payload) != {"selective_ci"} or not isinstance(payload["selective_ci"], dict):
        raise ValueError("selective-CI policy must contain only [selective_ci]")
    raw = payload["selective_ci"]
    if set(raw) != {"schema_version", "mode", "promoted_boundaries", "full_only_boundaries"}:
        raise ValueError("selective-CI policy fields drifted")
    if raw["schema_version"] != SCHEMA_VERSION or raw["mode"] not in {"certified", "full"}:
        raise ValueError("selective-CI policy version or mode is invalid")
    promoted = tuple(str(value) for value in raw["promoted_boundaries"])
    full_only = tuple(str(value) for value in raw["full_only_boundaries"])
    if set(promoted) != PROMOTED_BOUNDARIES or len(promoted) != len(PROMOTED_BOUNDARIES):
        raise ValueError("promoted boundaries must match the checked policy")
    if set(full_only) != FULL_ONLY_BOUNDARIES or len(full_only) != len(FULL_ONLY_BOUNDARIES):
        raise ValueError("full-only boundaries must match the checked policy")
    return Policy(str(raw["mode"]), promoted, full_only)


def _skipped(plan: Mapping[str, Any]) -> list[dict[str, str]]:
    exclusions = plan.get("exclusions")
    if not isinstance(exclusions, list):
        return []
    result: list[dict[str, str]] = []
    for item in exclusions:
        if not isinstance(item, Mapping):
            continue
        closure = item.get("closure")
        reason = item.get("reason")
        if isinstance(closure, str) and closure and isinstance(reason, str) and reason:
            result.append({"closure": closure, "reason": reason})
    return sorted(result, key=lambda item: item["closure"])


def _report(
    plan: Mapping[str, Any],
    *,
    boundary: str,
    command: str,
    selective: bool,
    closure: str | None,
    why: Sequence[str],
    next_full: str,
    complete: bool = False,
) -> dict[str, Any]:
    selector = plan.get("selector")
    selector_digest = selector.get("digest") if isinstance(selector, Mapping) else None
    commands = plan.get("commands") if selective else [command]
    tests = (
        plan.get("tests") if selective else ["complete-suite" if complete else "full-fast-suite"]
    )
    return {
        "boundary": boundary,
        "command": command,
        "selective": selective,
        "closure": closure if selective else None,
        "ran": {
            "commands": list(commands) if isinstance(commands, list) else [command],
            "tests": list(tests) if isinstance(tests, list) else [],
        },
        "skipped": _skipped(plan) if selective else [],
        "why": sorted(set(str(reason) for reason in why)),
        "closure_digests": dict(plan.get("closure_digests", {})),
        "selector_digest": selector_digest,
        "plan_digest": plan.get("plan_digest"),
        "confidence": plan.get("confidence", "low") if selective else "full",
        "next_full": next_full,
    }


def _route_verified_plan(
    plan: Mapping[str, Any],
    verified_route: Mapping[str, Any],
    *,
    boundary: str,
    mode: str,
    telemetry_ready: bool,
    next_full: str,
) -> dict[str, Any]:
    """Promote an already authority-verified route, or explain the exact full fallback."""
    if boundary in FULL_ONLY_BOUNDARIES:
        return _report(
            plan,
            boundary=boundary,
            command=COMPLETE_FULL_GATE,
            selective=False,
            closure=None,
            why=("boundary-requires-full-suite",),
            next_full="now",
            complete=True,
        )
    if boundary not in PROMOTED_BOUNDARIES:
        return _report(
            plan,
            boundary=boundary,
            command=FAST_FULL_GATE,
            selective=False,
            closure=None,
            why=("boundary-not-promoted",),
            next_full=next_full,
        )
    if mode != "certified":
        return _report(
            plan,
            boundary=boundary,
            command=FAST_FULL_GATE,
            selective=False,
            closure=None,
            why=("selective-ci-mode-full",),
            next_full=next_full,
        )
    if not telemetry_ready:
        return _report(
            plan,
            boundary=boundary,
            command=FAST_FULL_GATE,
            selective=False,
            closure=None,
            why=("telemetry-readiness-not-current",),
            next_full=next_full,
        )
    fallbacks = plan.get("fallback_reasons")
    if plan.get("decision") != "selective" or fallbacks:
        reasons = ["selector-required-full-gate"]
        if isinstance(fallbacks, list):
            reasons.extend(str(reason) for reason in fallbacks)
        return _report(
            plan,
            boundary=boundary,
            command=FAST_FULL_GATE,
            selective=False,
            closure=None,
            why=reasons,
            next_full=next_full,
        )
    if verified_route.get("selective") is not True:
        reasons = verified_route.get("reasons")
        return _report(
            plan,
            boundary=boundary,
            command=FAST_FULL_GATE,
            selective=False,
            closure=None,
            why=(
                list(str(reason) for reason in reasons)
                if isinstance(reasons, list) and reasons
                else ["production-evidence-not-verified"]
            ),
            next_full=next_full,
        )
    changed = plan.get("changed_modules")
    commands = plan.get("commands")
    closure = verified_route.get("closure")
    command = verified_route.get("command")
    digests = plan.get("closure_digests")
    if (
        not isinstance(changed, list)
        or len(changed) != 1
        or closure != changed[0]
        or not isinstance(commands, list)
        or commands != [command]
        or not isinstance(command, str)
        or not isinstance(digests, Mapping)
        or set(digests) != {closure}
        or not isinstance(digests.get(closure), str)
        or not str(digests[closure]).startswith("sha256:")
    ):
        return _report(
            plan,
            boundary=boundary,
            command=FAST_FULL_GATE,
            selective=False,
            closure=None,
            why=("verified-route-plan-mismatch",),
            next_full=next_full,
        )
    return _report(
        plan,
        boundary=boundary,
        command=command,
        selective=True,
        closure=str(closure),
        why=("certified-affected-closure",),
        next_full=next_full,
    )


def route_production_validation(
    *,
    closure: Mapping[str, Any],
    samples: Sequence[Mapping[str, Any]],
    authoritative_evidence: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
    current_plan: Mapping[str, Any],
    boundary: str,
    next_full: str,
) -> dict[str, Any]:
    """Derive a production route without accepting a caller-asserted verification result."""
    policy = load_policy(CONFIG_PATH)
    telemetry = _telemetry_evidence(ROOT)
    if boundary in PROMOTED_BOUNDARIES and policy.mode == "certified" and telemetry["current"]:
        verifier = _load_module("promotion_shadow_verifier", VERIFIER_PATH)
        verified = verifier.route_production_validation(
            closure=closure,
            samples=samples,
            authoritative_evidence=authoritative_evidence,
            candidate=candidate,
            current_plan=current_plan,
            boundary="submit",
            is_leaf=True,
            local_enabled=True,
        )
    else:
        verified = {
            "command": FAST_FULL_GATE,
            "selective": False,
            "closure": None,
            "reasons": ["production-verifier-not-entered"],
        }
    return _route_verified_plan(
        current_plan,
        verified,
        boundary=boundary,
        mode=policy.mode,
        telemetry_ready=bool(telemetry["current"]),
        next_full=next_full,
    )


def _git_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }


def _git(*args: str) -> str:
    completed = subprocess.run(
        ("/usr/bin/git", "-C", str(ROOT), *args),
        check=False,
        capture_output=True,
        text=True,
        env=_git_environment(),
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _git_success(*args: str) -> bool:
    return (
        subprocess.run(
            ("/usr/bin/git", "-C", str(ROOT), *args),
            check=False,
            capture_output=True,
            env=_git_environment(),
        ).returncode
        == 0
    )


def _telemetry_evidence(root: Path) -> dict[str, Any]:
    closures = _load_module("promotion_test_closures", root / "scripts" / "test_closures.py")
    registry = closures.load_registry(root / "tests" / "closures.toml")
    row = registry.by_id().get("kernel.telemetry")
    errors = list(closures.validate_registry(registry, root))
    if row is None:
        errors.append("kernel.telemetry closure missing")
        row_definition: object = None
    else:
        row_definition = closures.registry_definition(registry)
        row_definition = next(
            item for item in row_definition["closures"] if item["id"] == "kernel.telemetry"
        )
    landed_commit = _git(
        "log",
        "-1",
        "--format=%H",
        "--fixed-strings",
        "--grep=chore(merge): molecule bh-id9pp",
    )
    if len(landed_commit) != 40 or not _git_success(
        "merge-base", "--is-ancestor", landed_commit, "HEAD"
    ):
        errors.append("bh-id9pp telemetry molecule is not landed in HEAD ancestry")
    if not (root / TELEMETRY_RELEASE_PATH.relative_to(ROOT)).is_file():
        errors.append("official telemetry contract compatibility evidence missing")
    release_bytes = (root / TELEMETRY_RELEASE_PATH.relative_to(ROOT)).read_bytes()
    return {
        "closure": "kernel.telemetry",
        "closure_digest": _digest(row_definition),
        "official_release": TELEMETRY_RELEASE_PATH.relative_to(ROOT).as_posix(),
        "official_release_digest": _digest_bytes(release_bytes),
        "molecule": "bh-id9pp",
        "landed_commit": landed_commit or None,
        "current": not errors,
        "fallback_reasons": sorted(set(errors)),
    }


def build_checked_evidence(root: Path = ROOT) -> dict[str, Any]:
    policy = load_policy(root / CONFIG_PATH.relative_to(ROOT))
    certification = _read_json(root / CERTIFICATION_PATH.relative_to(ROOT))
    shadow = _read_json(root / SHADOW_PATH.relative_to(ROOT))
    telemetry = _telemetry_evidence(root)
    eligible = sorted(
        str(row["id"])
        for row in shadow.get("closure_decisions", ())
        if isinstance(row, Mapping) and row.get("eligible") is True
    )
    inputs = {
        "config": _digest_bytes((root / CONFIG_PATH.relative_to(ROOT)).read_bytes()),
        "registry": _digest_bytes((root / REGISTRY_PATH.relative_to(ROOT)).read_bytes()),
        "certification": _digest_bytes((root / CERTIFICATION_PATH.relative_to(ROOT)).read_bytes()),
        "shadow": _digest_bytes((root / SHADOW_PATH.relative_to(ROOT)).read_bytes()),
        "selector": _digest_bytes((root / SELECTOR_PATH.relative_to(ROOT)).read_bytes()),
        "verifier": _digest_bytes((root / VERIFIER_PATH.relative_to(ROOT)).read_bytes()),
        "policy": _digest_bytes(Path(__file__).read_bytes()),
    }
    real_certified = {
        str(row["id"])
        for row in certification.get("closures", ())
        if isinstance(row, Mapping)
        and (row.get("certification") or {}).get("status") == "certified"
        and (row.get("certification") or {}).get("eligible_for_selective_activation") is True
    }
    eligible = sorted(set(eligible) & real_certified)
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "inputs": inputs,
        "input_digest": _digest(inputs),
        "telemetry": telemetry,
        "boundaries": {
            "promoted": sorted(policy.promoted_boundaries),
            "full_only": sorted(policy.full_only_boundaries),
            "uncertainty_gate": FAST_FULL_GATE,
            "safety_gate": COMPLETE_FULL_GATE,
        },
        "activation": {
            "mode": policy.mode,
            "eligible_closures": eligible,
            "production_selective_routes": len(eligible) if telemetry["current"] else 0,
        },
        "output_contract": {
            "required": [
                "ran",
                "skipped",
                "why",
                "closure_digests",
                "selector_digest",
                "confidence",
                "next_full",
            ]
        },
        "rollback": {
            "file": CONFIG_PATH.relative_to(ROOT).as_posix(),
            "change": "selective_ci.mode = 'certified' -> 'full'",
        },
        "note": (
            "The promotion boundary is provisioned but no repository closure currently "
            "satisfies the "
            "certification and shadow-evidence bar, so every production route remains full."
        ),
    }


def validate_checked_evidence(evidence: Mapping[str, Any], root: Path = ROOT) -> tuple[str, ...]:
    errors: list[str] = []
    try:
        expected = build_checked_evidence(root)
    except (OSError, RuntimeError, ValueError) as exc:
        return (f"cannot derive promotion policy: {exc}",)
    if evidence != expected:
        errors.append("checked promotion evidence drifted from policy or authority inputs")
    telemetry = expected["telemetry"]
    if telemetry["current"] is not True:
        errors.append("telemetry closure/release evidence is not current")
    activation = expected["activation"]
    if activation["production_selective_routes"] != len(activation["eligible_closures"]):
        errors.append("promotion activation count is inconsistent")
    return tuple(errors)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    evidence = build_checked_evidence(ROOT)
    if args.check:
        errors = validate_checked_evidence(_read_json(EVIDENCE_PATH), ROOT)
        for error in errors:
            print(f"error: {error}")
        if not errors:
            activation = evidence["activation"]
            print(
                "promotion policy valid: telemetry current; "
                f"{activation['production_selective_routes']} production selective routes"
            )
        return 1 if errors else 0
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
