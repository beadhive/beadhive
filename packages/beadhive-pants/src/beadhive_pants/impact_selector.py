#!/usr/bin/env python3
"""Build an advisory, fail-closed impacted-test plan for a Git change range.

The plan is evidence only.  It never executes tests and never changes the configured validation
gate; ``bh-ck1t6.3`` owns shadow validation and any provisional local activation.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .repository import find_repository

ROOT = find_repository()
DEFAULT_EVIDENCE = ROOT / "docs" / "proof" / "bh-ck1t6.1-test-closure-certification.json"
SELECTOR_VERSION = "bh-test-impact-selector-v1"
PLAN_SCHEMA_VERSION = 1
FULL_GATE = "just check"
_MISSING_MODULE = object()
_CURRENT_APPLICABILITY_FIELDS = frozenset(
    {
        "recorded_input_digest",
        "observed_input_digest",
        "recorded_registry_digest",
        "observed_registry_digest",
        "recorded_material_digest",
        "observed_material_digest",
        "applicable",
        "fallback_reasons",
    }
)
_CURRENT_DIGEST_PAIRS = (
    ("recorded_input_digest", "observed_input_digest", "current-input-digest-mismatch"),
    (
        "recorded_registry_digest",
        "observed_registry_digest",
        "current-registry-digest-mismatch",
    ),
    (
        "recorded_material_digest",
        "observed_material_digest",
        "current-material-digest-mismatch",
    ),
)


class GitPort(Protocol):
    """Read-only Git queries needed to turn a revision range into change facts."""

    def run(self, *args: str) -> bytes: ...


class SubprocessGit:
    """Local Git adapter.  Every supported operation is read-only and network-free."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def run(self, *args: str) -> bytes:
        completed = subprocess.run(
            ("git", "-C", str(self.root), *args),
            check=False,
            capture_output=True,
        )
        if completed.returncode:
            message = (completed.stderr or completed.stdout).decode(errors="replace").strip()
            raise RuntimeError(message or f"git {' '.join(args)} failed")
        return completed.stdout


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def selector_digest() -> str:
    payload = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "source": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "version": SELECTOR_VERSION,
    }
    return _digest(payload)


def _exec_module_without_bytecode(spec: Any, module: Any) -> None:
    """Execute one source dependency without leaving bytecode or interpreter state behind."""
    previous_bytecode = sys.dont_write_bytecode
    previous_module = sys.modules.get(spec.name, _MISSING_MODULE)
    try:
        sys.dont_write_bytecode = True
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous_bytecode
        if previous_module is _MISSING_MODULE:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous_module


def _load_certification():
    path = ROOT / "scripts" / "test_closure_certification.py"
    spec = importlib.util.spec_from_file_location("test_closure_certification_for_selector", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    _exec_module_without_bytecode(spec, module)
    return module


def apply_current_applicability(
    evidence: Mapping[str, Any], applicability: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """Project current digests onto an immutable historical evidence snapshot."""
    projected = json.loads(json.dumps(evidence))
    for row in projected.get("closures", ()):
        current = applicability.get(str(row.get("id")), {})
        row["current_applicability"] = current
    return projected


def _safe_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        return None
    return path.as_posix()


def _test_path(value: str) -> str:
    return value.split("::", 1)[0]


def _row_paths(row: Mapping[str, Any]) -> set[str]:
    paths: set[str] = set()
    for field in (
        "implementation_boundary",
        "public_ports",
        "mandatory_boundary_tests",
        "real_adapter_tests",
        "reverse_dependent_tests",
    ):
        paths.update(_test_path(str(value)) for value in row.get(field, ()))
    coverage = row.get("coverage_mapping") or {}
    paths.update(str(value) for value in coverage.get("owned_sources", ()))
    paths.update(_test_path(str(value)) for value in coverage.get("selectors", ()))
    return paths


def _critical_path_reason(path: str) -> str | None:
    if path.startswith("docs/schemas/") or "/schemas/" in path or "schema_artifact" in path:
        return "schema-or-generated-artifact-change"
    if path in {"pyproject.toml", "uv.lock", "justfile"} or path.startswith(
        (".github/", ".mise", "scripts/hermetic", "scripts/main-push-gate")
    ):
        return "build-bootstrap-or-validation-config-change"
    if path in {
        "src/beadhive/cli.py",
        "src/beadhive/mcp.py",
        "src/beadhive/__init__.py",
        "src/beadhive/__main__.py",
    } or path.endswith("/__init__.py"):
        return "compatibility-facade-or-transport-change"
    if path.startswith("src/beadhive/validation") or "/validation_" in path:
        return "build-bootstrap-or-validation-config-change"
    return None


def _coverage_is_fresh(row: Mapping[str, Any], source_revision: object) -> bool:
    coverage = row.get("coverage_mapping") or {}
    return (
        coverage.get("mode") == "dynamic-per-test"
        and bool(coverage.get("per_test_contexts"))
        and coverage.get("input_digest") == row.get("input_digest")
        and coverage.get("source_revision") == source_revision
    )


def _current_applicability_fallbacks(row: Mapping[str, Any]) -> set[str]:
    if "current_applicability" not in row:
        return {"missing-current-applicability"}
    applicability = row["current_applicability"]
    if not isinstance(applicability, Mapping) or set(applicability) != set(
        _CURRENT_APPLICABILITY_FIELDS
    ):
        return {"invalid-current-applicability"}
    reasons_value = applicability["fallback_reasons"]
    if not isinstance(reasons_value, list) or any(
        not isinstance(value, str) for value in reasons_value
    ):
        return {"invalid-current-applicability"}
    reasons = set(reasons_value)
    if applicability["applicable"] is not True or reasons:
        reasons.add("current-applicability-not-proven")
    for recorded_field, observed_field, reason in _CURRENT_DIGEST_PAIRS:
        recorded = applicability[recorded_field]
        observed = applicability[observed_field]
        if (
            not isinstance(recorded, str)
            or not recorded
            or not isinstance(observed, str)
            or not observed
        ):
            reasons.add("invalid-current-applicability")
        elif recorded != observed:
            reasons.add(reason)
    return reasons


def _row_fallbacks(row: Mapping[str, Any], source_revision: object) -> set[str]:
    reasons = _current_applicability_fallbacks(row)
    if row.get("observed_input_digest") != row.get("input_digest"):
        reasons.add("input-digest-mismatch")
    if not row.get("enforceable_port"):
        reasons.add("unenforceable-public-port")
    certification = row.get("certification") or {}
    if certification.get("status") != "certified" or not certification.get(
        "eligible_for_selective_activation"
    ):
        reasons.add("closure-not-certified")
    if not _coverage_is_fresh(row, source_revision):
        reasons.add("missing-or-stale-coverage")
    relationships = tuple(str(value) for value in row.get("relationships", ()))
    relationship_evidence = row.get("relationship_evidence") or {}
    if any(not relationship_evidence.get(relationship) for relationship in relationships):
        reasons.add("incomplete-relationship-evidence")
    return reasons


def _normalized_changes(changes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for change in changes:
        path = _safe_path(change.get("path"))
        old_path = _safe_path(change.get("old_path")) if change.get("old_path") else None
        signals = sorted({str(value) for value in change.get("signals", ())})
        normalized.append(
            {
                "status": str(change.get("status", "")),
                "path": path or str(change.get("path", "")),
                **({"old_path": old_path} if old_path else {}),
                **({"signals": signals} if signals else {}),
                **({"invalid_path": True} if path is None else {}),
            }
        )
    return sorted(
        normalized,
        key=lambda item: (item["path"], item["status"], item.get("old_path", "")),
    )


def build_plan(
    changes: Sequence[Mapping[str, Any]],
    evidence: Mapping[str, Any],
    *,
    base: str,
    head: str,
    merge_base: str | None,
    merge_base_status: str = "resolved",
    artifact_errors: Iterable[str] = (),
    selector_digest: str | None = None,
) -> dict[str, Any]:
    """Return a canonical plan without reading, writing, importing product code, or networking."""
    normalized = _normalized_changes(changes)
    rows = [row for row in evidence.get("closures", ()) if isinstance(row, Mapping)]
    rows = sorted(rows, key=lambda row: str(row.get("id", "")))
    by_id = {str(row.get("id")): row for row in rows}
    ownership: dict[str, set[str]] = {}
    for row in rows:
        closure_id = str(row.get("id", ""))
        for path in _row_paths(row):
            ownership.setdefault(path, set()).add(closure_id)

    reasons: set[str] = set()
    if tuple(artifact_errors):
        reasons.add("certification-artifact-invalid")
    if merge_base_status != "resolved" or not merge_base:
        reasons.add("merge-base-ambiguity")
    for row in rows:
        reasons.update(_current_applicability_fallbacks(row))
    global_inputs = {str(value) for value in evidence.get("global_certification_inputs", ())}
    impacted: set[str] = set()
    owned_changes: list[tuple[str, str]] = []
    ambiguous_path = False
    for change in normalized:
        path = str(change["path"])
        old_path = str(change.get("old_path", ""))
        owners = set(ownership.get(path, ())) | set(ownership.get(old_path, ()))
        impacted.update(owners)
        owned_changes.extend((path, owner) for owner in owners)
        critical = _critical_path_reason(path) or (
            _critical_path_reason(old_path) if old_path else None
        )
        if len(owners) > 1:
            ambiguous_path = True
        if change.get("invalid_path"):
            reasons.add("unknown-or-unowned-path")
        if str(change["status"]).startswith(("D", "R", "C")):
            reasons.add("deleted-or-renamed-path")
        if not owners and not critical:
            reasons.add("unknown-or-unowned-path")
        if path in global_inputs or old_path in global_inputs:
            reasons.add("global-certification-input-change")
        if critical:
            reasons.add(critical)
        signals = set(change.get("signals", ()))
        if signals & {"dynamic-import", "subprocess", "runtime-discovery"}:
            reasons.add("dynamic-import-or-subprocess-ambiguity")

    if ambiguous_path or len(impacted) > 1:
        reasons.add("multi-module-ambiguity")
    for closure_id in sorted(impacted):
        row = by_id[closure_id]
        if row.get("kind") == "kernel":
            reasons.add("kernel-change")
        if row.get("kind") == "contract":
            reasons.add("shared-contract-change")
        if closure_id.startswith("config.") or closure_id == "module.config":
            reasons.add("build-bootstrap-or-validation-config-change")
        reasons.update(_row_fallbacks(row, evidence.get("source_revision")))

    candidate_rows: list[dict[str, Any]] = []
    tests: set[str] = set()
    contracts: set[str] = set()
    dependency_paths: list[dict[str, Any]] = []
    closure_digests: dict[str, str] = {}
    dependency_paths.extend(
        {
            "from": path,
            "relationship": "ownership/import",
            "tests": [],
            "to": closure_id,
        }
        for path, closure_id in sorted(set(owned_changes))
    )
    for closure_id in sorted(impacted):
        row = by_id[closure_id]
        row_tests = {
            str(value)
            for value in (
                *((row.get("coverage_mapping") or {}).get("selectors", ())),
                *row.get("mandatory_boundary_tests", ()),
                *row.get("real_adapter_tests", ()),
                *row.get("reverse_dependent_tests", ()),
            )
        }
        tests.update(row_tests)
        row_contracts = sorted(str(value) for value in row.get("shared_contracts", ()))
        contracts.update(row_contracts)
        closure_digests[closure_id] = str(row.get("input_digest", ""))
        candidate_rows.append(
            {
                "id": closure_id,
                "kind": row.get("kind"),
                "command": row.get("command"),
                "tests": sorted(row_tests),
                "contracts": row_contracts,
                "input_digest": row.get("input_digest"),
                "confidence": row.get("confidence"),
                "relationships": sorted(str(value) for value in row.get("relationships", ())),
                "applicability": row.get("current_applicability"),
            }
        )
        reverse_tests = sorted(str(value) for value in row.get("reverse_dependent_tests", ()))
        for dependent in sorted(str(value) for value in row.get("reverse_dependents", ())):
            dependency_paths.append(
                {
                    "from": closure_id,
                    "relationship": "reverse-dependency",
                    "tests": reverse_tests,
                    "to": dependent,
                }
            )

    decision = "full" if reasons else "selective"
    commands = (
        [FULL_GATE]
        if decision == "full"
        else sorted({str(by_id[closure_id].get("command")) for closure_id in impacted})
    )
    exclusions: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("id")) in impacted:
            continue
        applicability_reasons = sorted(_current_applicability_fallbacks(row))
        stable = not applicability_reasons
        exclusions.append(
            {
                "closure": str(row.get("id")),
                "status": "unaffected" if stable else "inapplicable",
                "reason": (
                    "unaffected-current-digests-stable"
                    if stable
                    else "current-applicability-not-proven"
                ),
                "historical_input_digest": row.get("input_digest"),
                "current_applicability": row.get("current_applicability"),
                **(
                    {"applicability_fallback_reasons": applicability_reasons}
                    if applicability_reasons
                    else {}
                ),
            }
        )
    digest = selector_digest or globals()["selector_digest"]()
    plan: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "selector": {"version": SELECTOR_VERSION, "digest": digest},
        "range": {
            "base": base,
            "head": head,
            "merge_base": merge_base,
            "merge_base_status": merge_base_status,
        },
        "changes": normalized,
        "decision": decision,
        "selection_scope": "authoritative-full-gate" if decision == "full" else "closure",
        "confidence": "low" if decision == "full" else "high",
        "commands": commands,
        "tests": sorted(tests),
        "candidate_closures": candidate_rows,
        "changed_modules": sorted(impacted),
        "dependency_paths": dependency_paths,
        "contracts": sorted(contracts),
        "closure_digests": closure_digests,
        "exclusions": exclusions,
        "fallback_reasons": sorted(reasons),
        "artifact_errors": sorted(str(error) for error in artifact_errors),
        "activation": {
            "enabled": False,
            "owner": str((evidence.get("policy") or {}).get("activation_owner", "bh-ck1t6.3")),
        },
    }
    plan["plan_digest"] = _digest(plan)
    return plan


def _parse_name_status(raw: bytes) -> list[dict[str, Any]]:
    fields = raw.split(b"\0")
    if fields and not fields[-1]:
        fields.pop()
    changes: list[dict[str, Any]] = []
    index = 0
    while index < len(fields):
        status = fields[index].decode(errors="strict")
        index += 1
        if status.startswith(("R", "C")):
            old_path = fields[index].decode(errors="strict")
            path = fields[index + 1].decode(errors="strict")
            index += 2
            changes.append({"status": status, "old_path": old_path, "path": path})
        else:
            path = fields[index].decode(errors="strict")
            index += 1
            changes.append({"status": status, "path": path})
    return changes


def _source_signals(content: bytes) -> list[str]:
    signals: set[str] = set()
    text = content.decode(errors="replace")
    if any(token in text for token in ("importlib.import_module", "__import__(", "entry_points(")):
        signals.add("dynamic-import")
    if any(token in text for token in ("subprocess.", "os.system(", "os.exec")):
        signals.add("subprocess")
    return sorted(signals)


def select_range(
    git: GitPort,
    evidence: Mapping[str, Any],
    *,
    base: str,
    head: str,
    artifact_errors: Iterable[str] = (),
) -> dict[str, Any]:
    """Read a Git range through ``GitPort`` and return its advisory plan."""
    try:
        base_commit = git.run("rev-parse", "--verify", f"{base}^{{commit}}").decode().strip()
        head_commit = git.run("rev-parse", "--verify", f"{head}^{{commit}}").decode().strip()
        merge_bases = [
            value
            for value in git.run("merge-base", "--all", base_commit, head_commit)
            .decode()
            .splitlines()
            if value
        ]
    except (RuntimeError, UnicodeDecodeError):
        return build_plan(
            (),
            evidence,
            base=base,
            head=head,
            merge_base=None,
            merge_base_status="unresolved",
            artifact_errors=artifact_errors,
        )
    if len(merge_bases) != 1:
        return build_plan(
            (),
            evidence,
            base=base_commit,
            head=head_commit,
            merge_base=None,
            merge_base_status="ambiguous",
            artifact_errors=artifact_errors,
        )
    merge_base = merge_bases[0]
    try:
        changes = _parse_name_status(
            git.run(
                "diff",
                "--name-status",
                "-z",
                "--find-renames",
                f"{merge_base}..{head_commit}",
            )
        )
        for change in changes:
            if str(change["status"]).startswith("D"):
                continue
            try:
                content = git.run("show", f"{head_commit}:{change['path']}")
            except RuntimeError:
                change.setdefault("signals", []).append("unreadable-head-path")
            else:
                change["signals"] = _source_signals(content)
    except (IndexError, RuntimeError, UnicodeDecodeError):
        return build_plan(
            (),
            evidence,
            base=base_commit,
            head=head_commit,
            merge_base=merge_base,
            merge_base_status="unresolved",
            artifact_errors=artifact_errors,
        )
    return build_plan(
        changes,
        evidence,
        base=base_commit,
        head=head_commit,
        merge_base=merge_base,
        artifact_errors=artifact_errors,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="target revision used to find one merge base")
    parser.add_argument("--head", default="HEAD", help="candidate revision (default: HEAD)")
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    args = parser.parse_args(argv)
    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    certification = _load_certification()
    artifact_errors = certification.validate_evidence(evidence, ROOT, verify_receipt=True)
    applicability = certification.current_applicability(evidence, ROOT)
    current_evidence = apply_current_applicability(evidence, applicability)
    plan = select_range(
        SubprocessGit(ROOT),
        current_evidence,
        base=args.base,
        head=args.head,
        artifact_errors=artifact_errors,
    )
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
