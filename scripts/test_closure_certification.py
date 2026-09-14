#!/usr/bin/env python3
"""Build and verify digest-bound prerequisite evidence for advisory test closures.

This is evidence, not a change selector.  It deliberately leaves selective execution disabled;
``bh-ck1t6.2`` owns selection and ``bh-ck1t6.3`` owns any provisional activation.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "proof" / "bh-ck1t6.1-test-closure-certification.json"
EVIDENCE_RELATIVE_PATH = DEFAULT_OUTPUT.relative_to(ROOT).as_posix()
SCHEMA_VERSION = 2
CERTIFIER_VERSION = "bh-test-closure-prerequisites-v2"
FULL_GATE_COMMAND = "just check"
FULL_GATE_COMMAND_HASH = hashlib.sha256(FULL_GATE_COMMAND.encode()).hexdigest()[:16]
RELEASE_GATE_COMMAND = "just check-all"
RELEASE_GATE_COMMAND_HASH = hashlib.sha256(RELEASE_GATE_COMMAND.encode()).hexdigest()[:16]
CHECKOUT_IDENTITY_EXCLUDES = (EVIDENCE_RELATIVE_PATH,)
REQUIRED_RELATIONSHIPS = (
    "import",
    "reverse-dependency",
    "shared-contract",
    "dynamic-discovery",
    "plugin-registration",
    "subprocess",
    "compatibility-facade",
    "generated-artifact",
    "schema",
    "test-infrastructure",
)
FALLBACK_TRIGGERS = (
    "input-digest-mismatch",
    "missing-or-stale-coverage",
    "unenforceable-public-port",
    "unknown-or-unowned-path",
    "shared-contract-or-schema-change",
    "dynamic-discovery-or-subprocess-ambiguity",
    "compatibility-facade-or-generated-artifact-change",
    "test-infrastructure-or-certifier-change",
)
GLOBAL_CERTIFICATION_INPUTS = (
    "scripts/test_closures.py",
    "scripts/test_closure_certification.py",
    "tests/closures.toml",
    "tests/test_test_closures.py",
    "tests/test_test_closure_certification.py",
)
RELATIONSHIP_EVIDENCE_REFS = {
    "import": ("implementation_boundary", "public_ports", "dependency_direction"),
    "reverse-dependency": ("reverse_dependents", "reverse_dependent_tests"),
    "shared-contract": ("shared_contracts", "mandatory_boundary_tests"),
    "dynamic-discovery": ("implementation_boundary", "fallback_triggers"),
    "plugin-registration": ("implementation_boundary", "real_adapter_tests"),
    "subprocess": ("implementation_boundary", "fallback_triggers"),
    "compatibility-facade": ("implementation_boundary", "mandatory_boundary_tests"),
    "generated-artifact": ("implementation_boundary", "mandatory_boundary_tests"),
    "schema": ("implementation_boundary", "mandatory_boundary_tests"),
    "test-infrastructure": ("global_certification_inputs", "fallback_triggers"),
}
_MISSING_MODULE = object()


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


def _load_test_closures():
    path = ROOT / "scripts" / "test_closures.py"
    spec = importlib.util.spec_from_file_location("closure_registry", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    _exec_module_without_bytecode(spec, module)
    return module


test_closures = _load_test_closures()


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *args), check=False, capture_output=True, text=True
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout.strip()


def _git_bytes(root: Path, *args: str) -> bytes:
    completed = subprocess.run(("git", "-C", str(root), *args), check=False, capture_output=True)
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout).decode(errors="replace").strip())
    return completed.stdout


def _tracked_checkout_entries(root: Path) -> tuple[tuple[str, str, bytes], ...]:
    """Return real checkout bytes, never index blob ids, for every tracked certification input."""
    completed = subprocess.run(
        ("git", "-C", str(root), "ls-files", "--stage", "-z"),
        check=False,
        capture_output=True,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.decode(errors="replace").strip())
    entries: list[tuple[str, str, bytes]] = []
    excluded = set(CHECKOUT_IDENTITY_EXCLUDES)
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        header, encoded_path = raw.split(b"\t", 1)
        mode, _object_id, stage = header.decode().split()
        if stage != "0":
            raise RuntimeError("checkout has unresolved index stages")
        relative = encoded_path.decode()
        if relative in excluded:
            continue
        path = root / relative
        content = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        entries.append((relative, mode, content))
    return tuple(sorted(entries))


def checkout_input_identity(root: Path = ROOT) -> dict[str, Any]:
    """A non-self-referential identity of tracked checkout inputs.

    The generated JSON is the only exclusion. Its digest can therefore be checked from a
    candidate checkout without requiring a cryptographic fixed point, while generator, tests,
    registry, docs, and every product source remain identity-bearing inputs.
    """
    entries = _tracked_checkout_entries(root)
    tree = hashlib.sha256()
    for relative, mode, content in entries:
        blob = hashlib.sha256(content).hexdigest()
        tree.update(f"{mode}\0{relative}\0sha256:{blob}\n".encode())
    tree_digest = "sha256:" + tree.hexdigest()
    revision_payload = {
        "algorithm": "sha256",
        "certifier": CERTIFIER_VERSION,
        "excluded_paths": list(CHECKOUT_IDENTITY_EXCLUDES),
        "file_count": len(entries),
        "tree": tree_digest,
    }
    revision = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(revision_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    return {**revision_payload, "revision": revision}


def _historical_snapshot_commit(root: Path = ROOT) -> str:
    """Commit containing the checked artifact whose green receipt anchors certification."""
    return _git(root, "log", "-1", "--format=%H", "--", EVIDENCE_RELATIVE_PATH)


@lru_cache(maxsize=16)
def _git_tree_snapshot(root: Path, revision: str) -> tuple[tuple[str, str, bytes], ...]:
    """Read one immutable Git tree in two processes, independent of its file count."""
    raw = _git_bytes(root, "ls-tree", "-r", "-z", "--full-tree", revision)
    tree_entries: list[tuple[str, str, str]] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        header, encoded_path = item.split(b"\t", 1)
        mode, object_type, object_id = header.decode().split()
        relative = encoded_path.decode()
        tree_entries.append((relative, mode, object_id if object_type == "blob" else ""))
    object_ids = [object_id for _relative, _mode, object_id in tree_entries if object_id]
    completed = subprocess.run(
        ("git", "-C", str(root), "cat-file", "--batch"),
        input=b"".join(object_id.encode() + b"\n" for object_id in object_ids),
        check=False,
        capture_output=True,
    )
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout).decode(errors="replace").strip())
    contents: dict[str, bytes] = {}
    offset = 0
    for expected_object_id in object_ids:
        header_end = completed.stdout.index(b"\n", offset)
        header = completed.stdout[offset:header_end].decode().split()
        if len(header) != 3 or header[0] != expected_object_id or header[1] != "blob":
            raise RuntimeError("git cat-file returned an unexpected historical object")
        size = int(header[2])
        content_start = header_end + 1
        content_end = content_start + size
        contents[expected_object_id] = completed.stdout[content_start:content_end]
        offset = content_end + 1
    return tuple(
        (relative, mode, contents[object_id] if object_id else object_id.encode())
        for relative, mode, object_id in tree_entries
    )


@lru_cache(maxsize=16)
def checkout_input_identity_at(
    root: Path,
    revision: str,
    certifier_version: str = CERTIFIER_VERSION,
    excluded_paths: tuple[str, ...] = CHECKOUT_IDENTITY_EXCLUDES,
) -> dict[str, Any]:
    """Recompute the certifier identity from immutable Git objects at ``revision``."""
    excluded = set(excluded_paths)
    entries = [entry for entry in _git_tree_snapshot(root, revision) if entry[0] not in excluded]
    tree = hashlib.sha256()
    for relative, mode, content in sorted(entries):
        blob = hashlib.sha256(content).hexdigest()
        tree.update(f"{mode}\0{relative}\0sha256:{blob}\n".encode())
    tree_digest = "sha256:" + tree.hexdigest()
    revision_payload = {
        "algorithm": "sha256",
        "certifier": certifier_version,
        "excluded_paths": list(excluded_paths),
        "file_count": len(entries),
        "tree": tree_digest,
    }
    recorded_revision = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(revision_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    return {**revision_payload, "revision": recorded_revision}


def _historical_evidence(root: Path = ROOT) -> tuple[str, dict[str, Any]]:
    commit = _historical_snapshot_commit(root)
    if not commit:
        raise RuntimeError("certification artifact has no committed historical snapshot")
    return commit, _historical_evidence_at(root, commit)


@lru_cache(maxsize=16)
def _historical_evidence_at(root: Path, commit: str) -> dict[str, Any]:
    payload = _git(root, "show", f"{commit}:{EVIDENCE_RELATIVE_PATH}")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise RuntimeError("historical certification artifact is not an object")
    return value


@lru_cache(maxsize=8192)
def _git_file_at(root: Path, commit: str, relative: str) -> bytes | None:
    return next(
        (content for path, _mode, content in _git_tree_snapshot(root, commit) if path == relative),
        None,
    )


PORTS: dict[str, tuple[str, ...]] = {
    "kernel": ("src/beadhive/operation_catalog.py",),
    "kernel.lifecycle": ("src/beadhive/kernel/lifecycle/contracts.py",),
    "kernel.plugins": ("src/beadhive/kernel/plugins/contracts.py",),
    "kernel.telemetry": ("src/beadhive/kernel/telemetry/contracts.py",),
    "module.agents": ("src/beadhive/modules/agents/contracts/ports.py",),
    "module.config": (
        "src/beadhive/modules/config/contracts.py",
        "src/beadhive/modules/config/domain/ports.py",
    ),
    "config.pure": (
        "src/beadhive/modules/config/contracts.py",
        "src/beadhive/modules/config/domain/ports.py",
    ),
    "config.store": ("src/beadhive/modules/config/domain/ports.py",),
    "config.fragments": ("src/beadhive/modules/config/contracts.py",),
    "module.hives": ("src/beadhive/modules/hives/contracts/ports.py",),
    "module.planning": ("src/beadhive/modules/planning/contracts/ports.py",),
    "module.state": ("src/beadhive/modules/state/contracts/ports.py",),
    "module.work": ("src/beadhive/modules/work/contracts/ports.py",),
    "module.worktrees": ("src/beadhive/modules/worktrees/contracts/ports.py",),
    "adapters": ("src/beadhive/state_stream.py",),
    "plugin.herdr": (
        "src/beadhive/kernel/plugins/contracts.py",
        "src/beadhive/integrations/herdr/application_contracts.py",
    ),
    "plugin.hitch": ("src/beadhive/kernel/plugins/contracts.py",),
    "plugin.observaloop": ("src/beadhive/kernel/plugins/contracts.py",),
    "plugin.orca": ("src/beadhive/kernel/plugins/contracts.py",),
    "plugin.repowise": ("src/beadhive/kernel/plugins/contracts.py",),
    "contract.agent-launch": (
        "src/beadhive/modules/agents/contracts/ports.py",
        "src/beadhive/seat_contracts.py",
    ),
    "contracts": ("src/beadhive/testing", "src/beadhive/state_stream.py"),
    "integration": (),
    "system-smoke": (),
}

INDEPENDENCE: dict[str, tuple[str, ...]] = {
    "kernel": ("tests/unit/test_pure_module_independence.py",),
    "kernel.lifecycle": ("tests/unit/kernel/lifecycle/test_independence.py",),
    "kernel.plugins": ("tests/unit/kernel/plugins/test_discovery.py",),
    "kernel.telemetry": ("tests/test_telemetry_contract.py",),
    "module.agents": ("tests/unit/modules/agents/test_agent_independence.py",),
    "module.config": ("tests/unit/modules/config/test_pure_independence.py",),
    "config.pure": ("tests/unit/modules/config/test_pure_independence.py",),
    "config.store": ("tests/unit/modules/config/test_yaml_store.py",),
    "config.fragments": ("tests/unit/modules/config/test_plugin_fragments.py",),
    "module.hives": ("tests/unit/modules/hives/test_hive_independence.py",),
    "module.planning": ("tests/unit/modules/planning/test_planning_independence.py",),
    "module.state": ("tests/unit/modules/state/test_state_independence.py",),
    "module.work": ("tests/unit/modules/work/test_work_independence.py",),
    "module.worktrees": ("tests/unit/modules/worktrees/test_worktrees_independence.py",),
}

REAL_ADAPTERS: dict[str, tuple[str, ...]] = {
    "kernel.telemetry": ("tests/test_semantic_otel_adapter.py",),
    "adapters": ("tests/unit/testing/test_real_adapter_conformance_example.py",),
    "plugin.herdr": ("tests/unit/integrations/test_herdr_adapter.py",),
    "plugin.hitch": ("tests/test_hitch_plugin.py",),
    "plugin.observaloop": ("tests/test_observaloop.py",),
    "plugin.orca": ("tests/test_orca.py",),
    "plugin.repowise": ("tests/test_repowise_plugin.py",),
    "contract.agent-launch": ("tests/contracts/test_agent_launch_compatibility.py",),
    "contracts": ("tests/unit/testing/test_real_adapter_conformance_example.py",),
}

HISTORICAL_TIMINGS: dict[str, tuple[float, str]] = {
    "kernel": (32.14, "dd6c75e1a847c1d3f9886c1343f52f1277807e57"),
    "module.agents": (31.350, "ad4077d7ee5ae40c966f089920ca794006538090"),
    "module.config": (51.495, "ad4077d7ee5ae40c966f089920ca794006538090"),
    "module.hives": (35.169, "ad4077d7ee5ae40c966f089920ca794006538090"),
    "module.planning": (127.998, "ad4077d7ee5ae40c966f089920ca794006538090"),
    "module.state": (17.102, "ad4077d7ee5ae40c966f089920ca794006538090"),
    "module.work": (227.467, "ad4077d7ee5ae40c966f089920ca794006538090"),
    "module.worktrees": (57.824, "ad4077d7ee5ae40c966f089920ca794006538090"),
    "adapters": (33.00, "dd6c75e1a847c1d3f9886c1343f52f1277807e57"),
    "plugin.herdr": (23.58, "dd6c75e1a847c1d3f9886c1343f52f1277807e57"),
    "plugin.hitch": (7.68, "dd6c75e1a847c1d3f9886c1343f52f1277807e57"),
    "plugin.observaloop": (7.10, "dd6c75e1a847c1d3f9886c1343f52f1277807e57"),
    "plugin.orca": (6.20, "dd6c75e1a847c1d3f9886c1343f52f1277807e57"),
    "plugin.repowise": (6.18, "dd6c75e1a847c1d3f9886c1343f52f1277807e57"),
    "contracts": (33.15, "dd6c75e1a847c1d3f9886c1343f52f1277807e57"),
    "integration": (138.84, "dd6c75e1a847c1d3f9886c1343f52f1277807e57"),
    "system-smoke": (6.98, "dd6c75e1a847c1d3f9886c1343f52f1277807e57"),
}


def _expand(root: Path, patterns: Iterable[str]) -> tuple[str, ...]:
    paths: set[str] = set()
    for pattern in patterns:
        candidate = root / pattern
        if candidate.is_file():
            paths.add(candidate.relative_to(root).as_posix())
        elif candidate.is_dir():
            paths.update(
                path.relative_to(root).as_posix()
                for path in candidate.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
            )
        else:
            paths.update(
                path.relative_to(root).as_posix()
                for path in root.glob(pattern)
                if path.is_file() and "__pycache__" not in path.parts
            )
    return tuple(sorted(paths))


def _selector_paths(selectors: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(selector.split("::", 1)[0] for selector in selectors))


def _digest(root: Path, metadata: dict[str, Any], paths: Iterable[str]) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode())
    for relative in sorted(set(paths)):
        path = root / relative
        digest.update(b"\0path\0" + relative.encode() + b"\0")
        if not path.is_file():
            digest.update(b"<missing>")
        else:
            digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()


def _object_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _relationships(closure) -> tuple[str, ...]:
    values = {"import", "test-infrastructure"}
    if closure.reverse_dependencies:
        values.add("reverse-dependency")
    if closure.shared_contracts:
        values.add("shared-contract")
    if closure.kind == "plugin" or closure.id == "kernel.plugins":
        values.update(("dynamic-discovery", "plugin-registration"))
    if closure.id in {"module.work", "module.worktrees", "adapters", "integration", "system-smoke"}:
        values.add("subprocess")
    if closure.id in {
        "module.config",
        "module.work",
        "module.worktrees",
        "adapters",
        "plugin.herdr",
    }:
        values.add("compatibility-facade")
    if closure.id in {"module.config", "config.fragments", "contracts"}:
        values.update(("generated-artifact", "schema"))
    return tuple(value for value in REQUIRED_RELATIONSHIPS if value in values)


def _direction(closure) -> str:
    if closure.kind == "module":
        return "bootstrap/adapters -> public capability port -> application/domain"
    if closure.kind == "plugin":
        return "bootstrap/plugin kernel -> plugin contract -> integration implementation"
    if closure.kind == "adapter":
        return "bootstrap -> adapter -> public capability/kernel ports"
    if closure.kind == "kernel":
        return "bootstrap/modules/adapters -> public kernel contract -> kernel implementation"
    if closure.kind == "contract":
        return "consumers/implementations -> shared public contract"
    return "full-system tests -> composed public product surface"


def _base_record(closure, root: Path, by_id: dict[str, Any]) -> dict[str, Any]:
    ports = PORTS[closure.id]
    port_inputs = _expand(root, ports)
    implementation = _expand(root, closure.source_paths)
    shared_inputs = tuple(
        path
        for shared_id in closure.shared_contracts
        for path in _expand(root, by_id[shared_id].source_paths)
    )
    selectors = closure.selectors
    boundary_tests = tuple(
        dict.fromkeys((*closure.shared_contract_tests, *INDEPENDENCE.get(closure.id, ())))
    )
    adapter_tests = REAL_ADAPTERS.get(closure.id, ())
    metadata = {
        "certifier": CERTIFIER_VERSION,
        "id": closure.id,
        "kind": closure.kind,
        "owner": f"{closure.id} closure maintainers",
        "public_ports": ports,
        "implementation_boundary": implementation,
        "dependency_direction": _direction(closure),
        "selectors": selectors,
        "shared_contracts": closure.shared_contracts,
        "shared_contract_inputs": shared_inputs,
        "reverse_dependencies": closure.reverse_dependencies,
        "relationships": _relationships(closure),
    }
    paths = (
        *implementation,
        *port_inputs,
        *shared_inputs,
        *_selector_paths(selectors),
        *boundary_tests,
        *adapter_tests,
        *GLOBAL_CERTIFICATION_INPUTS,
    )
    input_digest = _digest(root, metadata, paths)
    historical = HISTORICAL_TIMINGS.get(closure.id)
    enforceable = bool(ports) and closure.id not in {"adapters", "integration", "system-smoke"}
    reason = "fresh dynamic per-test trace is not available; selector-to-source mapping is advisory"
    if not enforceable:
        reason = "the closure has no single enforceable public port"
    return {
        "id": closure.id,
        "kind": closure.kind,
        "status": closure.status,
        "owner": metadata["owner"],
        "public_ports": list(ports),
        "enforceable_port": enforceable,
        "implementation_boundary": list(implementation),
        "dependency_direction": metadata["dependency_direction"],
        "input_digest": input_digest,
        "observed_input_digest": input_digest,
        "command": f"just test-closure {closure.id}",
        "collection": {
            "count": max(1, len(selectors)),
            "kind": "selector-count-placeholder",
            "nodeid_digest": None,
        },
        "timing": {
            "seconds": historical[0] if historical else None,
            "kind": "historical-wall" if historical else "unavailable",
            "source_revision": historical[1] if historical else None,
            "current_digest_measurement": False,
        },
        "confidence": "medium" if enforceable else "low",
        "independence_tests": list(INDEPENDENCE.get(closure.id, ())),
        "mandatory_boundary_tests": list(boundary_tests),
        "real_adapter_tests": list(adapter_tests),
        "shared_contracts": list(closure.shared_contracts),
        "reverse_dependents": list(closure.reverse_dependencies),
        "reverse_dependent_tests": list(closure.reverse_dependency_tests),
        "relationships": list(metadata["relationships"]),
        "relationship_evidence": {
            relationship: list(RELATIONSHIP_EVIDENCE_REFS[relationship])
            for relationship in metadata["relationships"]
        },
        "fallback_triggers": list(FALLBACK_TRIGGERS),
        "coverage_mapping": {
            "mode": "declared-best-available",
            "granularity": "selector-to-owned-source-scope",
            "selectors": list(selectors),
            "owned_sources": list(implementation),
            "per_test_contexts": [],
            "uncertainty": (
                "No fresh dynamic per-test contexts exist at this digest. The checked registry, "
                "static import boundary, explicit shared contracts, and reverse-dependent tests "
                "are the best available mapping and cannot alone prove execution coverage."
            ),
        },
        "certification": {
            "status": "uncertified",
            "prerequisites": "recorded",
            "eligible_for_selective_activation": False,
            "gate": "just check",
            "reason": reason,
        },
    }


def current_records(root: Path = ROOT) -> dict[str, dict[str, Any]]:
    registry = test_closures.load_registry(root / "tests" / "closures.toml")
    by_id = registry.by_id()
    return {closure.id: _base_record(closure, root, by_id) for closure in registry.closures}


def _run_collection(command: tuple[str, ...], root: Path) -> tuple[str, ...]:
    nodeids: set[str] = set()
    adjusted_parts = list(command)
    adjusted_parts[adjusted_parts.index("-qq")] = "-q"
    if "-n" in adjusted_parts:
        adjusted_parts[adjusted_parts.index("-n") + 1] = "0"
    completed = subprocess.run(
        tuple(adjusted_parts),
        cwd=root,
        env=test_closures._pytest_environment(root),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise RuntimeError(completed.stdout + completed.stderr)
    nodeids.update(
        line.strip()
        for line in completed.stdout.splitlines()
        if line.startswith("tests/") and "::" in line
    )
    return tuple(sorted(nodeids))


def _collect(closure, root: Path) -> tuple[tuple[str, ...], float]:
    commands = test_closures._pytest_commands(closure, collect_only=True)
    nodeids: set[str] = set()
    started = time.monotonic()
    for command in commands:
        nodeids.update(_run_collection(command, root))
    return tuple(sorted(nodeids)), round(time.monotonic() - started, 6)


def _collect_universe(root: Path) -> tuple[tuple[str, ...], float]:
    command = (sys.executable, "-m", "pytest", "-qq", "--collect-only", "tests")
    started = time.monotonic()
    nodeids = _run_collection(command, root)
    return nodeids, round(time.monotonic() - started, 6)


def _selected_nodeids(selectors: Iterable[str], universe: Iterable[str]) -> tuple[str, ...]:
    selected: set[str] = set()
    for selector in selectors:
        if selector == "tests":
            selected.update(universe)
        elif "::" in selector:
            selected.update(
                nodeid
                for nodeid in universe
                if nodeid == selector or nodeid.startswith(selector + "[")
            )
        else:
            prefix = selector + "::"
            selected.update(nodeid for nodeid in universe if nodeid.startswith(prefix))
    return tuple(sorted(selected))


def _collection_record(nodeids: tuple[str, ...], *, shared_universe: bool) -> dict[str, Any]:
    return {
        "count": len(nodeids),
        "kind": "current-pytest-nodeids",
        "nodeid_digest": "sha256:" + hashlib.sha256("\n".join(nodeids).encode()).hexdigest(),
        "shared_universe": shared_universe,
    }


def _collect_current_collection_records(root: Path) -> dict[str, dict[str, Any]]:
    """Recompute every material collection claim from this checkout's pytest universe."""
    registry = test_closures.load_registry(root / "tests" / "closures.toml")
    needs_universe = any(not closure.pytest_args for closure in registry.closures)
    universe, _universe_seconds = _collect_universe(root) if needs_universe else ((), 0.0)
    collections: dict[str, dict[str, Any]] = {}
    for closure in registry.closures:
        if closure.pytest_args:
            nodeids, _seconds = _collect(closure, root)
            shared_universe = False
        else:
            nodeids = _selected_nodeids(closure.selectors, universe)
            shared_universe = True
        collections[closure.id] = _collection_record(nodeids, shared_universe=shared_universe)
    return collections


def build_evidence(root: Path = ROOT, *, collect: bool = False) -> dict[str, Any]:
    registry = test_closures.load_registry(root / "tests" / "closures.toml")
    records = current_records(root)
    input_identity = checkout_input_identity(root)
    if collect:
        collections = _collect_current_collection_records(root)
        for closure in registry.closures:
            record = records[closure.id]
            record["collection"] = collections[closure.id]
            if not record["collection"]["count"]:
                record["certification"] = {
                    "status": "uncertified",
                    "prerequisites": "incomplete",
                    "eligible_for_selective_activation": False,
                    "gate": "just check",
                    "reason": "current selector collected no tests",
                }
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "bh-ck1t6.1 digest-bound test-closure prerequisite certification",
        "source_revision": input_identity["revision"],
        "source_tree": input_identity["tree"],
        "certification_input_identity": input_identity,
        "refresh_provenance": {
            "from": "311d30db44d3a04120d908bc8ce61c40fe7fcdc4",
            "to": "ced493be87c9b36736cb081906c5ddb97989bd6c",
            "kind": "telemetry-closure promotion refresh after bh-id9pp landed",
        },
        "policy": {
            "activation": "disabled",
            "ordinary_full_gate": registry.full_gate,
            "release_gate": registry.release_gate,
            "selection_owner": "bh-ck1t6.2",
            "activation_owner": "bh-ck1t6.3",
        },
        "same_tree_full_gate_oracle": {
            "input_identity": input_identity,
            "receipt_provenance": {
                "authority": "beadhive-git-private-validation-ledger",
                "bead": "bh-ck1t6.5",
                "phase": "check",
                "command": FULL_GATE_COMMAND,
                "command_hash": FULL_GATE_COMMAND_HASH,
                "required_lifecycle": "completed",
                "required_verdict": "green",
                "lookup_tree": "candidate HEAD^{tree}",
            },
            "note": (
                "The checked JSON declares the receipt lookup identity, not a self-asserted run. "
                "--check resolves the candidate's Git tree against the authoritative git-private "
                "ledger; the matching in-flight work-check is accepted only to bootstrap that "
                "same gate, and later checks require its completed green receipt."
            ),
        },
        "focused_boundary_oracle": {
            "scope": "module isolation sentinels plus focused real-adapter contract tests",
            "verdict": "green",
            "passed": 323,
            "skipped": 1,
            "warnings": 1,
            "pytest_seconds": 13.55,
            "note": (
                "Focused implementation evidence only; it does not replace just check or add "
                "dynamic per-test coverage contexts."
            ),
        },
        "recertification": {
            "rule": (
                "Recompute only records whose own input digest or referenced shared boundary "
                "changed; stale, missing, or unenforceable affected records use just check."
            ),
            "unrelated_closure_digests_remain_valid": True,
            "affected_stale_or_unenforceable_gate": "just check",
            "certifier_or_test_infrastructure_change_invalidates_all_records": True,
        },
        "required_relationship_classes": list(REQUIRED_RELATIONSHIPS),
        "global_certification_inputs": list(GLOBAL_CERTIFICATION_INPUTS),
        "closures": [records[closure.id] for closure in registry.closures],
    }


def effective_certification(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("observed_input_digest") != record.get("input_digest"):
        return {"status": "uncertified", "gate": "just check", "reason": "stale closure evidence"}
    if not record.get("enforceable_port"):
        return {
            "status": "uncertified",
            "gate": "just check",
            "reason": "unenforceable public port",
        }
    return dict(record["certification"])


def _expected_receipt_provenance() -> dict[str, Any]:
    return {
        "authority": "beadhive-git-private-validation-ledger",
        "bead": "bh-ck1t6.5",
        "phase": "check",
        "command": FULL_GATE_COMMAND,
        "command_hash": FULL_GATE_COMMAND_HASH,
        "required_lifecycle": "completed",
        "required_verdict": "green",
        "lookup_tree": "candidate HEAD^{tree}",
    }


def _current_host_id() -> str:
    try:
        from beadhive import host

        return host.host_id()
    except (ImportError, KeyError, OSError, TypeError, ValueError):
        return ""


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, ValueError, OSError):
        return False
    return True


def _process_state(pid: int) -> str:
    try:
        completed = subprocess.run(
            ("ps", "-o", "stat=", "-p", str(pid)),
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode:
        return ""
    return (completed.stdout.strip().split(maxsplit=1) or [""])[0]


def _process_start_token(pid: int) -> str:
    """Match the portable token Beadhive records when it starts a validation run."""
    try:
        completed = subprocess.run(
            ("ps", "-o", "lstart=", "-p", str(pid)),
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _owner_is_live(manifest: dict[str, Any]) -> bool:
    """Whether a running receipt is owned by this exact still-executing process identity."""
    owner = manifest.get("owner") or {}
    local_host = _current_host_id()
    if not local_host or owner.get("host") != local_host:
        return False
    pid = owner.get("pid")
    if type(pid) is not int or pid <= 0:
        return False
    if not _pid_exists(pid):
        return False
    state = _process_state(pid)
    if not state or state.startswith("Z"):
        return False
    recorded_start = owner.get("start_token")
    observed_start = _process_start_token(pid)
    return (
        isinstance(recorded_start, str)
        and bool(recorded_start)
        and bool(observed_start)
        and recorded_start == observed_start
    )


def _receipt_manifests(root: Path) -> tuple[dict[str, Any], ...]:
    common = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    directory = common / "bh" / "validation" / "runs"
    manifests: list[dict[str, Any]] = []
    for child in (
        sorted(directory.iterdir(), key=lambda path: path.name) if directory.is_dir() else ()
    ):
        if not child.is_dir():
            continue
        path = child / "manifest.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("run_id") == child.name:
            manifests.append(value)
    return tuple(manifests)


def _managed_bead_binding(manifest: dict[str, Any]) -> bool:
    bead = manifest.get("bead")
    branch = manifest.get("branch")
    return (
        isinstance(bead, str)
        and bool(bead)
        and branch
        in {
            f"wt/bead/issue/{bead}",
            f"wt/bead/epic/{bead}",
        }
    )


def _canonical_gate_binding(manifest: dict[str, Any]) -> bool:
    """Admit only lifecycle shapes that can execute this repository's two full gates."""
    command = manifest.get("command")
    command_hash = manifest.get("command_hash")
    phase = manifest.get("phase")
    if command == FULL_GATE_COMMAND and command_hash == FULL_GATE_COMMAND_HASH:
        if phase in {"check", "submit"}:
            return _managed_bead_binding(manifest)
        return phase == "validation" and manifest.get("bead") is None
    return (
        command == RELEASE_GATE_COMMAND
        and command_hash == RELEASE_GATE_COMMAND_HASH
        and phase == "validation"
        and manifest.get("bead") is None
    )


def _completed_green_receipt(manifest: dict[str, Any]) -> bool:
    return (
        type(manifest.get("schema")) is int
        and manifest.get("schema") == 1
        and isinstance(manifest.get("run_id"), str)
        and bool(manifest.get("run_id"))
        and manifest.get("lifecycle") == "completed"
        and manifest.get("verdict") == "green"
        and type(manifest.get("exit_code")) is int
        and manifest.get("exit_code") == 0
        and manifest.get("signal") is None
    )


def _running_receipt_owned_by_checkout(manifest: dict[str, Any], root: Path) -> bool:
    worktree_value = manifest.get("worktree")
    if not isinstance(worktree_value, str) or not worktree_value:
        return False
    try:
        worktree = Path(worktree_value).resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return (
        type(manifest.get("schema")) is int
        and manifest.get("schema") == 1
        and isinstance(manifest.get("run_id"), str)
        and bool(manifest.get("run_id"))
        and manifest.get("lifecycle") == "running"
        and manifest.get("verdict") == "none"
        and manifest.get("exit_code") is None
        and manifest.get("signal") is None
        and worktree == root.resolve()
        and _owner_is_live(manifest)
    )


def validate_full_gate_receipt(evidence: dict[str, Any], root: Path = ROOT) -> tuple[str, ...]:
    """Resolve lifecycle admission against the current candidate's authoritative manifest.

    Certification content and input identity remain anchored to the immutable historical snapshot
    by ``validate_evidence``.  The lifecycle receipt is a separate authority boundary: it admits
    only this clean candidate's exact tree and may bootstrap only from that run's live owner.
    """
    oracle = evidence.get("same_tree_full_gate_oracle") or {}
    provenance = oracle.get("receipt_provenance") or {}
    if provenance != _expected_receipt_provenance():
        return ("same-tree oracle receipt provenance does not match the full gate",)
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        return ("full-gate receipt lookup requires a clean checkout with no untracked inputs",)
    candidate_tree = _git(root, "rev-parse", "HEAD^{tree}")
    identity_matches = [
        manifest
        for manifest in _receipt_manifests(root)
        if manifest.get("tree") == candidate_tree and _canonical_gate_binding(manifest)
    ]
    if any(_completed_green_receipt(manifest) for manifest in identity_matches):
        return ()
    if any(_running_receipt_owned_by_checkout(manifest, root) for manifest in identity_matches):
        return ()
    return ("candidate checkout has no authoritative matching full-gate receipt",)


def current_applicability(evidence: dict[str, Any], root: Path = ROOT) -> dict[str, dict[str, Any]]:
    """Compare immutable certification material and closure-local inputs with current state."""
    snapshot, historical_evidence = _historical_evidence(root)
    historical_rows = {
        str(record["id"]): record
        for record in historical_evidence.get("closures", ())
        if isinstance(record, dict) and "id" in record
    }
    supplied_rows = {
        str(record["id"]): record
        for record in evidence.get("closures", ())
        if isinstance(record, dict) and "id" in record
    }
    historical_registry_bytes = _git_file_at(root, snapshot, "tests/closures.toml")
    if historical_registry_bytes is None:
        raise RuntimeError("historical certification snapshot has no closure registry")
    historical_registry = test_closures.loads_registry(historical_registry_bytes.decode())
    historical_definition = test_closures.registry_definition(historical_registry)
    historical_registry_digest = _object_digest(historical_definition)
    current_definition: dict[str, object] | None = None
    current_registry_error: str | None = None
    try:
        current_registry = test_closures.load_registry(root / "tests" / "closures.toml")
        current_definition = test_closures.registry_definition(current_registry)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        current_registry_error = str(exc)
    current_registry_digest = _object_digest(current_definition)
    registry_drift = current_definition != historical_definition
    historical_ids = tuple(closure.id for closure in historical_registry.closures)
    current_ids = (
        tuple(str(row["id"]) for row in current_definition["closures"])
        if current_definition is not None
        else ()
    )
    shape_current = current_ids == tuple(sorted(historical_ids))

    def material(record: dict[str, Any] | None) -> dict[str, Any] | None:
        if record is None:
            return None
        return {key: value for key, value in record.items() if key != "current_applicability"}

    def applicability_inputs(record: dict[str, Any]) -> tuple[str, ...]:
        paths = {
            *record.get("implementation_boundary", ()),
            *record.get("public_ports", ()),
            *record.get("mandatory_boundary_tests", ()),
            *record.get("real_adapter_tests", ()),
            *record.get("reverse_dependent_tests", ()),
            *(
                selector.split("::", 1)[0]
                for selector in (record.get("coverage_mapping") or {}).get("selectors", ())
            ),
        }
        for shared_id in record.get("shared_contracts", ()):
            shared = historical_rows.get(str(shared_id), {})
            paths.update(shared.get("implementation_boundary", ()))
            paths.update(shared.get("public_ports", ()))
        return tuple(sorted(str(path) for path in paths))

    def applicability_metadata(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "algorithm": "bh-closure-applicability-v1",
            "id": record.get("id"),
            "kind": record.get("kind"),
            "dependency_direction": record.get("dependency_direction"),
            "relationships": record.get("relationships"),
            "reverse_dependents": record.get("reverse_dependents"),
            "shared_contracts": record.get("shared_contracts"),
        }

    def historical_digest(metadata: dict[str, Any], paths: tuple[str, ...]) -> str:
        digest = hashlib.sha256()
        digest.update(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode())
        for relative in paths:
            digest.update(b"\0path\0" + relative.encode() + b"\0")
            content = _git_file_at(root, snapshot, relative)
            digest.update(content if content is not None else b"<missing>")
        return "sha256:" + digest.hexdigest()

    result: dict[str, dict[str, Any]] = {}
    for closure_id, recorded in historical_rows.items():
        paths = applicability_inputs(recorded)
        metadata = applicability_metadata(recorded)
        recorded_digest = historical_digest(metadata, paths)
        observed_digest = _digest(root, metadata, paths)
        recorded_material = material(recorded)
        observed_material = material(supplied_rows.get(closure_id))
        reasons: list[str] = []
        if not shape_current:
            reasons.append("registry-shape-change")
        if registry_drift:
            reasons.append("registry-definition-drift")
        if observed_material != recorded_material:
            reasons.append("certification-material-drift")
        if observed_digest != recorded_digest:
            reasons.append("input-digest-mismatch")
        result[closure_id] = {
            "recorded_input_digest": recorded_digest,
            "observed_input_digest": observed_digest,
            "recorded_registry_digest": historical_registry_digest,
            "observed_registry_digest": current_registry_digest,
            "recorded_material_digest": _object_digest(recorded_material),
            "observed_material_digest": _object_digest(observed_material),
            **(
                {"registry_error": current_registry_error}
                if current_registry_error is not None
                else {}
            ),
            "applicable": not reasons,
            "fallback_reasons": reasons,
        }
    return result


def validate_evidence(
    evidence: dict[str, Any], root: Path = ROOT, *, verify_receipt: bool = False
) -> tuple[str, ...]:
    errors: list[str] = []
    try:
        snapshot_commit, expected_evidence = _historical_evidence(root)
        recorded_identity = expected_evidence.get("certification_input_identity") or {}
        snapshot_identity = checkout_input_identity_at(
            root,
            snapshot_commit,
            str(recorded_identity.get("certifier", "")),
            tuple(str(path) for path in recorded_identity.get("excluded_paths", ())),
        )
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        return (f"certification historical snapshot is unavailable: {exc}",)
    if evidence.get("schema_version") != SCHEMA_VERSION:
        errors.append("certification evidence has the wrong schema version")
    if set(evidence) != set(expected_evidence):
        errors.append("certification evidence top-level fields drifted")
    derived_top_level = (
        "scope",
        "refresh_provenance",
        "policy",
        "same_tree_full_gate_oracle",
        "focused_boundary_oracle",
        "recertification",
        "required_relationship_classes",
        "global_certification_inputs",
    )
    for field in derived_top_level:
        if evidence.get(field) != expected_evidence[field]:
            errors.append(f"{field} drifted from the candidate checkout")
    expected_rows = expected_evidence["closures"]
    expected_ids = [record["id"] for record in expected_rows]
    current = {record["id"]: record for record in expected_rows}
    rows = evidence.get("closures")
    if not isinstance(rows, list):
        return ("certification evidence closures must be a list",)
    if len(rows) != len(expected_rows):
        errors.append("certification evidence closure row cardinality drifted")
    if not all(isinstance(row, dict) for row in rows):
        errors.append("certification evidence closure rows must be objects")
    object_rows = [row for row in rows if isinstance(row, dict)]
    row_ids = [str(row.get("id")) for row in object_rows]
    duplicate_ids = sorted(
        closure_id for closure_id in set(row_ids) if row_ids.count(closure_id) > 1
    )
    if duplicate_ids:
        errors.append(f"certification evidence has duplicate closure ids: {duplicate_ids}")
    if set(row_ids) != set(expected_ids):
        errors.append("certification evidence closure ids drifted from the registry")
    if row_ids != expected_ids:
        errors.append("certification evidence closures drifted from canonical registry order")
    by_id = {str(row.get("id")): row for row in object_rows}
    immutable = (
        "kind",
        "status",
        "owner",
        "public_ports",
        "enforceable_port",
        "implementation_boundary",
        "dependency_direction",
        "input_digest",
        "command",
        "mandatory_boundary_tests",
        "real_adapter_tests",
        "shared_contracts",
        "reverse_dependents",
        "reverse_dependent_tests",
        "relationships",
        "relationship_evidence",
        "fallback_triggers",
        "timing",
        "confidence",
        "independence_tests",
        "coverage_mapping",
        "certification",
    )
    for closure_id, expected in current.items():
        actual = by_id.get(closure_id)
        if actual is None:
            continue
        if set(actual) != set(expected):
            errors.append(f"closure {closure_id} fields drifted")
        for field in immutable:
            if actual.get(field) != expected[field]:
                errors.append(f"closure {closure_id} {field} drifted")
        collection = actual.get("collection") or {}
        expected_collection = expected.get("collection") or {}
        if set(collection) != {"count", "kind", "nodeid_digest", "shared_universe"}:
            errors.append(f"closure {closure_id} collection fields drifted")
        count = collection.get("count")
        if type(count) is not int or count < 1:
            errors.append(f"closure {closure_id} has no collected tests")
        if collection.get("kind") != "current-pytest-nodeids":
            errors.append(f"closure {closure_id} collection kind drifted")
        nodeid_digest = collection.get("nodeid_digest")
        if (
            not isinstance(nodeid_digest, str)
            or not nodeid_digest.startswith("sha256:")
            or len(nodeid_digest) != len("sha256:") + 64
        ):
            errors.append(f"closure {closure_id} collection nodeid_digest is invalid")
        if collection.get("shared_universe") is not expected_collection.get("shared_universe"):
            errors.append(f"closure {closure_id} collection shared_universe drifted")
        for field in ("count", "kind", "nodeid_digest"):
            if collection.get(field) != expected_collection.get(field):
                errors.append(f"closure {closure_id} collection {field} drifted")
        if actual.get("observed_input_digest") != actual.get("input_digest"):
            errors.append(f"closure {closure_id} evidence is stale")
    observed = {
        relationship for row in object_rows for relationship in row.get("relationships", ())
    }
    missing = set(REQUIRED_RELATIONSHIPS) - observed
    if missing:
        errors.append(f"relationship inventory is incomplete: {sorted(missing)}")
    policy = evidence.get("policy") or {}
    if policy.get("activation") != "disabled":
        errors.append("bh-ck1t6.1 must not activate selective validation")
    if evidence.get("source_revision") != snapshot_identity["revision"]:
        errors.append("certification source revision does not match the historical snapshot")
    if evidence.get("source_tree") != snapshot_identity["tree"]:
        errors.append("certification source tree does not match the historical snapshot")
    if evidence.get("certification_input_identity") != snapshot_identity:
        errors.append("certification input identity does not match the historical snapshot")
    oracle = evidence.get("same_tree_full_gate_oracle") or {}
    if oracle.get("input_identity") != snapshot_identity:
        errors.append("same-tree oracle input identity does not match the historical snapshot")
    if oracle.get("receipt_provenance") != _expected_receipt_provenance():
        errors.append("same-tree oracle receipt provenance does not match the full gate")
    if verify_receipt and not errors:
        errors.extend(validate_full_gate_receipt(evidence, root))
    return tuple(errors)


def _render(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if args.check:
        if not args.output.exists():
            print(f"test-closure certification evidence missing: {args.output}")
            return 1
        evidence = json.loads(args.output.read_text(encoding="utf-8"))
        errors = validate_evidence(evidence, ROOT, verify_receipt=True)
        if errors:
            print("test-closure certification: FAILED")
            print("\n".join(f"- {error}" for error in errors))
            return 1
        print(
            "test-closure certification: OK "
            f"({len(evidence['closures'])} records; activation disabled)"
        )
        return 0
    evidence = build_evidence(ROOT, collect=args.collect)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(_render(evidence), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
