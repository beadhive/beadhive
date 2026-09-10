"""Digest-bound certification evidence for advisory test closures."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "test_closure_certification.py"
EVIDENCE = ROOT / "docs" / "proof" / "bh-ck1t6.1-test-closure-certification.json"
SPEC = importlib.util.spec_from_file_location("test_closure_certification_script", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
certification = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = certification
SPEC.loader.exec_module(certification)


def test_checked_certification_evidence_is_a_valid_historical_snapshot() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    assert certification.validate_evidence(evidence, ROOT) == ()


def test_checked_source_identity_is_recomputed_from_historical_git_objects() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    snapshot = certification._historical_snapshot_commit(ROOT)
    identity = certification.checkout_input_identity_at(ROOT, snapshot)

    assert evidence["certification_input_identity"] == identity
    assert evidence["source_revision"] == identity["revision"]
    assert evidence["source_tree"] == identity["tree"]
    assert evidence["same_tree_full_gate_oracle"]["input_identity"] == identity


def test_unrelated_descendant_does_not_invalidate_the_historical_snapshot() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    snapshot = certification._historical_snapshot_commit(ROOT)

    assert snapshot != certification._git(ROOT, "rev-parse", "HEAD")
    assert certification.validate_evidence(evidence, ROOT) == ()
    applicability = certification.current_applicability(evidence, ROOT)
    assert set(applicability) == {row["id"] for row in evidence["closures"]}


def test_receipt_lookup_targets_current_candidate_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    receipt = {
        "schema": 1,
        "tree": "candidate-tree",
        "command": "just check",
        "command_hash": certification.FULL_GATE_COMMAND_HASH,
        "bead": "bh-ck1t6.5",
        "phase": "check",
        "lifecycle": "completed",
        "verdict": "green",
        "exit_code": 0,
        "signal": None,
    }

    def fake_git(_root: Path, *args: str) -> str:
        if args[0] == "status":
            return ""
        if args[0] == "rev-parse":
            assert args[1] == "HEAD^{tree}"
            return "candidate-tree"
        raise AssertionError(args)

    monkeypatch.setattr(certification, "_git", fake_git)
    monkeypatch.setattr(certification, "_receipt_manifests", lambda _root: (receipt,))

    assert certification.validate_full_gate_receipt(evidence, ROOT) == ()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("tree", "stale-tree"),
        ("bead", "bh-ck1t6.4"),
        ("phase", "submit"),
        ("command_hash", "0000000000000000"),
    ),
)
def test_receipt_admission_rejects_wrong_candidate_authority_binding(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    receipt = {
        "schema": 1,
        "tree": "candidate-tree",
        "command": "just check",
        "command_hash": certification.FULL_GATE_COMMAND_HASH,
        "bead": "bh-ck1t6.5",
        "phase": "check",
        "lifecycle": "completed",
        "verdict": "green",
        "exit_code": 0,
        "signal": None,
    }
    receipt[field] = value
    monkeypatch.setattr(
        certification,
        "_git",
        lambda _root, *args: "" if args[0] == "status" else "candidate-tree",
    )
    monkeypatch.setattr(certification, "_receipt_manifests", lambda _root: (receipt,))

    assert certification.validate_full_gate_receipt(evidence, ROOT) == (
        "candidate checkout has no authoritative matching full-gate receipt",
    )


@pytest.mark.parametrize(
    ("field", "expected_error"),
    (
        ("source_revision", "source revision"),
        ("source_tree", "source tree"),
        ("certification_input_identity", "certification input identity"),
    ),
)
def test_stale_checkout_identity_fields_fail_closed(field: str, expected_error: str) -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    mutated = json.loads(json.dumps(evidence))
    mutated[field] = "stale" if field != "certification_input_identity" else {"digest": "stale"}

    errors = certification.validate_evidence(mutated, ROOT)

    assert any(expected_error in error for error in errors)


def test_oracle_identity_or_provenance_mismatch_fails_closed() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    stale_identity = json.loads(json.dumps(evidence))
    stale_identity["same_tree_full_gate_oracle"]["input_identity"] = {"digest": "stale"}
    stale_provenance = json.loads(json.dumps(evidence))
    stale_provenance["same_tree_full_gate_oracle"]["receipt_provenance"]["command_hash"] = (
        "0000000000000000"
    )

    identity_errors = certification.validate_evidence(stale_identity, ROOT)
    provenance_errors = certification.validate_evidence(stale_provenance, ROOT)

    assert any("oracle input identity" in error for error in identity_errors)
    assert any("oracle receipt provenance" in error for error in provenance_errors)


def test_identity_excluded_artifact_cannot_forge_material_record_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    expected_collections = {
        record["id"]: dict(record["collection"]) for record in evidence["closures"]
    }
    target = next(record for record in evidence["closures"] if record["id"] == "kernel")
    target["certification"] = {
        "status": "certified",
        "prerequisites": "complete",
        "eligible_for_selective_activation": True,
        "gate": "just test-closure kernel",
        "reason": "forged",
    }
    target["confidence"] = "high"
    target["coverage_mapping"]["mode"] = "dynamic-per-test"
    target["independence_tests"] = ["tests/unit/test_forged_independence.py"]
    target["timing"]["seconds"] = 0.001
    monkeypatch.setattr(
        certification,
        "_collect_current_collection_records",
        lambda _root: expected_collections,
        raising=False,
    )
    monkeypatch.setattr(certification, "validate_full_gate_receipt", lambda *_args: ())

    errors = certification.validate_evidence(evidence, ROOT, verify_receipt=True)

    for expected_error in (
        "certification drifted",
        "confidence drifted",
        "coverage_mapping drifted",
        "independence_tests drifted",
        "timing drifted",
    ):
        assert any(expected_error in error for error in errors)


def test_identity_excluded_artifact_cannot_forge_collection_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    expected_collections = {
        record["id"]: dict(record["collection"]) for record in evidence["closures"]
    }
    target = next(record for record in evidence["closures"] if record["id"] == "kernel")
    target["collection"]["count"] = 999_999
    target["collection"]["nodeid_digest"] = "sha256:" + "0" * 64
    monkeypatch.setattr(
        certification,
        "_collect_current_collection_records",
        lambda _root: expected_collections,
    )
    monkeypatch.setattr(certification, "validate_full_gate_receipt", lambda *_args: ())

    errors = certification.validate_evidence(evidence, ROOT, verify_receipt=True)

    assert any("collection count drifted" in error for error in errors)
    assert any("collection nodeid_digest drifted" in error for error in errors)

    target["collection"]["seconds"] = 0.001
    errors = certification.validate_evidence(evidence, ROOT)

    assert any("collection fields drifted" in error for error in errors)


def test_identity_excluded_artifact_cannot_forge_material_top_level_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    expected_collections = {
        record["id"]: dict(record["collection"]) for record in evidence["closures"]
    }
    forged = {
        "scope": "forged scope",
        "refresh_provenance": {},
        "policy": {"activation": "disabled"},
        "same_tree_full_gate_oracle": {},
        "focused_boundary_oracle": {"verdict": "green"},
        "recertification": {"unrelated_closure_digests_remain_valid": True},
        "required_relationship_classes": ["import"],
        "global_certification_inputs": [],
    }
    evidence.update(forged)
    monkeypatch.setattr(
        certification,
        "_collect_current_collection_records",
        lambda _root: expected_collections,
        raising=False,
    )
    monkeypatch.setattr(certification, "validate_full_gate_receipt", lambda *_args: ())

    errors = certification.validate_evidence(evidence, ROOT, verify_receipt=True)

    for field in forged:
        assert any(f"{field} drifted" in error for error in errors)


@pytest.mark.parametrize("position", ("before", "after"))
def test_identity_excluded_artifact_rejects_a_forged_duplicate_closure_row(
    monkeypatch: pytest.MonkeyPatch, position: str
) -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    expected_collections = {
        record["id"]: dict(record["collection"]) for record in evidence["closures"]
    }
    kernel_index = next(
        index for index, record in enumerate(evidence["closures"]) if record["id"] == "kernel"
    )
    forged = json.loads(json.dumps(evidence["closures"][kernel_index]))
    forged["certification"] = {
        "status": "certified",
        "prerequisites": "complete",
        "eligible_for_selective_activation": True,
        "gate": "just test-closure kernel",
        "reason": "forged duplicate",
    }
    offset = 0 if position == "before" else 1
    evidence["closures"].insert(kernel_index + offset, forged)
    monkeypatch.setattr(
        certification,
        "_collect_current_collection_records",
        lambda _root: expected_collections,
    )
    monkeypatch.setattr(certification, "validate_full_gate_receipt", lambda *_args: ())

    errors = certification.validate_evidence(evidence, ROOT, verify_receipt=True)

    assert any("closure row cardinality drifted" in error for error in errors)
    assert any("duplicate closure ids" in error for error in errors)


@pytest.mark.parametrize("mutation", ("missing", "extra", "reordered"))
def test_identity_excluded_artifact_requires_the_canonical_closure_row_sequence(
    monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    expected_collections = {
        record["id"]: dict(record["collection"]) for record in evidence["closures"]
    }
    if mutation == "missing":
        evidence["closures"].pop()
    elif mutation == "extra":
        extra = json.loads(json.dumps(evidence["closures"][-1]))
        extra["id"] = "forged.extra"
        evidence["closures"].append(extra)
    else:
        evidence["closures"][0], evidence["closures"][1] = (
            evidence["closures"][1],
            evidence["closures"][0],
        )
    monkeypatch.setattr(
        certification,
        "_collect_current_collection_records",
        lambda _root: expected_collections,
    )
    monkeypatch.setattr(certification, "validate_full_gate_receipt", lambda *_args: ())

    errors = certification.validate_evidence(evidence, ROOT, verify_receipt=True)

    if mutation in {"missing", "extra"}:
        assert any("closure row cardinality drifted" in error for error in errors)
        assert any("closure ids drifted" in error for error in errors)
    else:
        assert any("canonical registry order" in error for error in errors)


def test_full_gate_receipt_is_resolved_from_candidate_tree_not_artifact_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    evidence["same_tree_full_gate_oracle"]["input_identity"]["tree"] = "stale-tree"

    def fake_git(_root: Path, *args: str) -> str:
        return "" if args[0] == "status" else "candidate-tree"

    receipt = {
        "schema": 1,
        "tree": "stale-tree",
        "command": "just check",
        "command_hash": certification.FULL_GATE_COMMAND_HASH,
        "bead": "bh-ck1t6.5",
        "phase": "check",
        "lifecycle": "completed",
        "verdict": "green",
        "exit_code": 0,
        "signal": None,
    }
    monkeypatch.setattr(certification, "_git", fake_git)
    monkeypatch.setattr(certification, "_receipt_manifests", lambda _root: (receipt,))

    assert certification.validate_full_gate_receipt(evidence, ROOT) == (
        "candidate checkout has no authoritative matching full-gate receipt",
    )
    receipt["tree"] = "candidate-tree"
    assert certification.validate_full_gate_receipt(evidence, ROOT) == ()


@pytest.mark.parametrize(
    ("case", "owner", "live", "state", "observed_start"),
    (
        (
            "foreign-host",
            {"host": "foreign-host", "pid": os.getpid(), "start_token": "current-start"},
            True,
            "S",
            "current-start",
        ),
        (
            "stale-start-token",
            {"host": "current-host", "pid": os.getpid(), "start_token": "stale-start"},
            True,
            "S",
            "current-start",
        ),
        (
            "zombie",
            {"host": "current-host", "pid": os.getpid(), "start_token": "current-start"},
            True,
            "Z",
            "current-start",
        ),
        (
            "nonlive-pid",
            {"host": "current-host", "pid": 999_999_999, "start_token": "current-start"},
            False,
            "",
            "",
        ),
    ),
)
def test_running_receipt_requires_exact_live_process_identity(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    owner: dict[str, object],
    live: bool,
    state: str,
    observed_start: str,
) -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    receipt = {
        "tree": "candidate-tree",
        "command": "just check",
        "command_hash": certification.FULL_GATE_COMMAND_HASH,
        "bead": "bh-j5uyb.1",
        "phase": "check",
        "lifecycle": "running",
        "owner": owner,
    }
    monkeypatch.setattr(
        certification,
        "_git",
        lambda _root, *args: "" if args[0] == "status" else "candidate-tree",
    )
    monkeypatch.setattr(certification, "_receipt_manifests", lambda _root: (receipt,))
    monkeypatch.setattr(certification, "_current_host_id", lambda: "current-host", raising=False)
    monkeypatch.setattr(certification, "_pid_exists", lambda _pid: live, raising=False)
    monkeypatch.setattr(certification, "_process_state", lambda _pid: state, raising=False)
    monkeypatch.setattr(
        certification, "_process_start_token", lambda _pid: observed_start, raising=False
    )

    assert certification.validate_full_gate_receipt(evidence, ROOT) == (
        "candidate checkout has no authoritative matching full-gate receipt",
    ), case


def test_running_receipt_accepts_exact_current_process_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    receipt = {
        "tree": "candidate-tree",
        "command": "just check",
        "command_hash": certification.FULL_GATE_COMMAND_HASH,
        "bead": "bh-ck1t6.5",
        "phase": "check",
        "lifecycle": "running",
        "owner": {"host": "current-host", "pid": 1234, "start_token": "current-start"},
    }
    monkeypatch.setattr(
        certification,
        "_git",
        lambda _root, *args: "" if args[0] == "status" else "candidate-tree",
    )
    monkeypatch.setattr(certification, "_receipt_manifests", lambda _root: (receipt,))
    monkeypatch.setattr(certification, "_current_host_id", lambda: "current-host", raising=False)
    monkeypatch.setattr(certification, "_pid_exists", lambda _pid: True, raising=False)
    monkeypatch.setattr(certification, "_process_state", lambda _pid: "S", raising=False)
    monkeypatch.setattr(
        certification, "_process_start_token", lambda _pid: "current-start", raising=False
    )

    assert certification.validate_full_gate_receipt(evidence, ROOT) == ()


def test_running_receipt_accepts_exact_live_downstream_bead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    receipt = {
        "tree": "candidate-tree",
        "command": "just check",
        "command_hash": certification.FULL_GATE_COMMAND_HASH,
        "bead": "bh-j5uyb.1",
        "phase": "check",
        "lifecycle": "running",
        "owner": {"host": "current-host", "pid": 1234, "start_token": "current-start"},
    }
    monkeypatch.setattr(
        certification,
        "_git",
        lambda _root, *args: "" if args[0] == "status" else "candidate-tree",
    )
    monkeypatch.setattr(certification, "_receipt_manifests", lambda _root: (receipt,))
    monkeypatch.setattr(certification, "_current_host_id", lambda: "current-host", raising=False)
    monkeypatch.setattr(certification, "_pid_exists", lambda _pid: True, raising=False)
    monkeypatch.setattr(certification, "_process_state", lambda _pid: "S", raising=False)
    monkeypatch.setattr(
        certification, "_process_start_token", lambda _pid: "current-start", raising=False
    )

    assert certification.validate_full_gate_receipt(evidence, ROOT) == ()


def test_completed_green_receipt_remains_bound_to_provenance_bead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    receipt = {
        "schema": 1,
        "tree": "candidate-tree",
        "command": "just check",
        "command_hash": certification.FULL_GATE_COMMAND_HASH,
        "bead": "bh-j5uyb.1",
        "phase": "check",
        "lifecycle": "completed",
        "verdict": "green",
        "exit_code": 0,
        "signal": None,
    }
    monkeypatch.setattr(
        certification,
        "_git",
        lambda _root, *args: "" if args[0] == "status" else "candidate-tree",
    )
    monkeypatch.setattr(certification, "_receipt_manifests", lambda _root: (receipt,))

    assert certification.validate_full_gate_receipt(evidence, ROOT) == (
        "candidate checkout has no authoritative matching full-gate receipt",
    )


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    (
        ("tree", "other-tree"),
        ("command", "just check-all"),
        ("command_hash", "wrong-command-hash"),
        ("phase", "submit"),
    ),
)
def test_downstream_running_receipt_rejects_gate_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    wrong_value: str,
) -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    receipt = {
        "tree": "candidate-tree",
        "command": "just check",
        "command_hash": certification.FULL_GATE_COMMAND_HASH,
        "bead": "bh-j5uyb.1",
        "phase": "check",
        "lifecycle": "running",
        "owner": {"host": "current-host", "pid": 1234, "start_token": "current-start"},
    }
    receipt[field] = wrong_value
    monkeypatch.setattr(
        certification,
        "_git",
        lambda _root, *args: "" if args[0] == "status" else "candidate-tree",
    )
    monkeypatch.setattr(certification, "_receipt_manifests", lambda _root: (receipt,))
    monkeypatch.setattr(certification, "_current_host_id", lambda: "current-host", raising=False)
    monkeypatch.setattr(certification, "_pid_exists", lambda _pid: True, raising=False)
    monkeypatch.setattr(certification, "_process_state", lambda _pid: "S", raising=False)
    monkeypatch.setattr(
        certification, "_process_start_token", lambda _pid: "current-start", raising=False
    )

    assert certification.validate_full_gate_receipt(evidence, ROOT) == (
        "candidate checkout has no authoritative matching full-gate receipt",
    )


@pytest.mark.parametrize("rogue_path", ("rogue.py", "tests/test_rogue.py"))
def test_completed_receipt_rejects_nonignored_untracked_source_or_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rogue_path: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        completed = subprocess.run(
            ("git", "-C", str(repo), *args),
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    git("init", "--quiet")
    (repo / ".gitignore").write_text(".venv/\n.repowise/\n.ruff_cache/\n", encoding="utf-8")
    (repo / "tracked.py").write_text("TRACKED = True\n", encoding="utf-8")
    git("add", ".gitignore", "tracked.py")
    git(
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=Closure Test",
        "-c",
        "user.email=closure@example.test",
        "commit",
        "--quiet",
        "-m",
        "test fixture",
    )
    tree = git("rev-parse", "HEAD^{tree}")
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    receipt = {
        "schema": 1,
        "tree": tree,
        "command": "just check",
        "command_hash": certification.FULL_GATE_COMMAND_HASH,
        "bead": "bh-ck1t6.5",
        "phase": "check",
        "lifecycle": "completed",
        "verdict": "green",
        "exit_code": 0,
        "signal": None,
    }
    monkeypatch.setattr(certification, "_receipt_manifests", lambda _root: (receipt,))

    assert certification.validate_full_gate_receipt(evidence, repo) == ()
    for ignored in (".venv/cache.bin", ".repowise/state.json", ".ruff_cache/cache"):
        path = repo / ignored
        path.parent.mkdir(exist_ok=True)
        path.write_text("ignored\n", encoding="utf-8")
    assert certification.validate_full_gate_receipt(evidence, repo) == ()

    rogue = repo / rogue_path
    rogue.parent.mkdir(exist_ok=True)
    rogue.write_text("ROGUE = True\n", encoding="utf-8")

    assert git("status", "--porcelain", "--untracked-files=all") == f"?? {rogue_path}"
    assert certification.validate_full_gate_receipt(evidence, repo) == (
        "full-gate receipt lookup requires a clean checkout with no untracked inputs",
    )


def test_every_advisory_closure_has_a_complete_digest_bound_record() -> None:
    registry = certification.test_closures.load_registry()
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    records = {record["id"]: record for record in evidence["closures"]}

    assert set(records) == set(registry.by_id())
    assert len(records) == 24
    required = {
        "owner",
        "public_ports",
        "implementation_boundary",
        "dependency_direction",
        "input_digest",
        "command",
        "collection",
        "timing",
        "confidence",
        "mandatory_boundary_tests",
        "real_adapter_tests",
        "reverse_dependents",
        "relationships",
        "relationship_evidence",
        "fallback_triggers",
        "coverage_mapping",
        "certification",
    }
    for record in records.values():
        assert required <= set(record)
        assert record["input_digest"].startswith("sha256:")
        assert record["command"] == f"just test-closure {record['id']}"
        assert record["collection"]["count"] >= 1
        assert record["timing"]["seconds"] is None or record["timing"]["seconds"] >= 0
        assert record["fallback_triggers"]
        assert set(record["relationship_evidence"]) == set(record["relationships"])
        assert all(record["relationship_evidence"].values())


def test_relationship_inventory_covers_every_non_import_edge_class() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    observed = {
        relationship for record in evidence["closures"] for relationship in record["relationships"]
    }

    assert set(certification.REQUIRED_RELATIONSHIPS) <= observed
    assert {
        "dynamic-discovery",
        "plugin-registration",
        "subprocess",
        "compatibility-facade",
        "generated-artifact",
        "schema",
        "test-infrastructure",
    } <= observed


def test_uncertain_or_stale_record_fails_closed_without_invalidating_others() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    mutated = json.loads(json.dumps(evidence))
    target = next(record for record in mutated["closures"] if record["id"] == "module.agents")
    target["observed_input_digest"] = "sha256:" + "0" * 64

    errors = certification.validate_evidence(mutated, ROOT)

    assert any("module.agents" in error and "stale" in error for error in errors)
    assert not any("module.config" in error for error in errors)
    assert certification.effective_certification(target) == {
        "status": "uncertified",
        "gate": "just check",
        "reason": "stale closure evidence",
    }


def test_an_unrelated_module_change_preserves_other_closure_digests(tmp_path: Path) -> None:
    config = tmp_path / "config.py"
    agents = tmp_path / "agents.py"
    config.write_text("CONFIG = 1\n", encoding="utf-8")
    agents.write_text("AGENT = 1\n", encoding="utf-8")
    config_before = certification._digest(tmp_path, {"id": "module.config"}, ("config.py",))
    agents_before = certification._digest(tmp_path, {"id": "module.agents"}, ("agents.py",))

    config.write_text("CONFIG = 2\n", encoding="utf-8")

    assert config_before != certification._digest(tmp_path, {"id": "module.config"}, ("config.py",))
    assert agents_before == certification._digest(tmp_path, {"id": "module.agents"}, ("agents.py",))


def test_one_collection_universe_projects_file_and_parameterized_node_selectors() -> None:
    universe = (
        "tests/test_alpha.py::test_one",
        "tests/test_alpha.py::test_many[a]",
        "tests/test_alpha.py::test_many[b]",
        "tests/test_beta.py::test_other",
    )

    assert set(
        certification._selected_nodeids(
            ("tests/test_alpha.py", "tests/test_beta.py::test_other"), universe
        )
    ) == set(universe)
    assert certification._selected_nodeids(("tests/test_alpha.py::test_many",), universe) == tuple(
        sorted(universe[1:3])
    )


def test_module_and_adapter_records_preserve_isolation_and_real_boundary_proof() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    records = {record["id"]: record for record in evidence["closures"]}

    for closure_id in (
        "module.agents",
        "module.config",
        "module.hives",
        "module.planning",
        "module.state",
        "module.work",
        "module.worktrees",
    ):
        record = records[closure_id]
        assert record["public_ports"]
        assert record["enforceable_port"] is True
        assert record["independence_tests"]
        assert record["mandatory_boundary_tests"]
    for closure_id in (
        "adapters",
        "plugin.herdr",
        "plugin.hitch",
        "plugin.observaloop",
        "plugin.orca",
        "plugin.repowise",
    ):
        assert records[closure_id]["real_adapter_tests"]


def test_evidence_keeps_selection_and_activation_out_of_this_leaf() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    assert evidence["policy"] == {
        "activation": "disabled",
        "ordinary_full_gate": "just check",
        "release_gate": "just check-all",
        "selection_owner": "bh-ck1t6.2",
        "activation_owner": "bh-ck1t6.3",
    }
    oracle = evidence["same_tree_full_gate_oracle"]
    assert oracle["input_identity"] == evidence["certification_input_identity"]
    assert oracle["receipt_provenance"] == certification._expected_receipt_provenance()
    assert evidence["focused_boundary_oracle"] == {
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
    }
    assert evidence["recertification"]["unrelated_closure_digests_remain_valid"] is True
    assert evidence["recertification"]["affected_stale_or_unenforceable_gate"] == "just check"
