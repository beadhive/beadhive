"""Contract and golden tests for the advisory impacted-test selector."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "test_impact_selector.py"
GOLDEN = ROOT / "tests" / "fixtures" / "test-impact-selector" / "cases.json"
SPEC = importlib.util.spec_from_file_location("test_impact_selector_script", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
selector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = selector
SPEC.loader.exec_module(selector)


def _record(
    closure_id: str,
    *,
    kind: str,
    sources: list[str],
    tests: list[str],
    reverse_dependents: list[str] | None = None,
    reverse_tests: list[str] | None = None,
) -> dict[str, object]:
    digest = f"sha256:{closure_id.replace('.', '-')}-digest"
    applicability_digest = f"sha256:{closure_id.replace('.', '-')}-applicability"
    registry_digest = "sha256:fixture-registry"
    material_digest = f"sha256:{closure_id.replace('.', '-')}-material"
    return {
        "id": closure_id,
        "kind": kind,
        "status": "present",
        "command": f"just test-closure {closure_id}",
        "input_digest": digest,
        "observed_input_digest": digest,
        "confidence": "high",
        "enforceable_port": True,
        "implementation_boundary": sources,
        "public_ports": [sources[0]],
        "mandatory_boundary_tests": tests[:1],
        "real_adapter_tests": tests[-1:],
        "shared_contracts": [],
        "reverse_dependents": reverse_dependents or [],
        "reverse_dependent_tests": reverse_tests or [],
        "relationships": ["import", "reverse-dependency", "test-infrastructure"],
        "relationship_evidence": {
            "import": ["implementation_boundary", "public_ports"],
            "reverse-dependency": ["reverse_dependents", "reverse_dependent_tests"],
            "test-infrastructure": ["global_certification_inputs"],
        },
        "fallback_triggers": ["input-digest-mismatch", "missing-or-stale-coverage"],
        "coverage_mapping": {
            "mode": "dynamic-per-test",
            "input_digest": digest,
            "source_revision": "fixture-head",
            "owned_sources": sources,
            "selectors": [*tests, *(reverse_tests or [])],
            "per_test_contexts": [{"source": source, "tests": tests} for source in sources],
        },
        "certification": {
            "status": "certified",
            "eligible_for_selective_activation": True,
            "gate": f"just test-closure {closure_id}",
            "reason": "fixture exact coverage",
        },
        "current_applicability": {
            "recorded_input_digest": applicability_digest,
            "observed_input_digest": applicability_digest,
            "recorded_registry_digest": registry_digest,
            "observed_registry_digest": registry_digest,
            "recorded_material_digest": material_digest,
            "observed_material_digest": material_digest,
            "applicable": True,
            "fallback_reasons": [],
        },
    }


@pytest.fixture
def evidence() -> dict[str, object]:
    alpha_tests = ["tests/unit/modules/alpha/test_service.py"]
    alpha_reverse = ["tests/test_alpha_consumer.py"]
    return {
        "schema_version": 2,
        "source_revision": "fixture-head",
        "policy": {
            "activation": "disabled",
            "ordinary_full_gate": "just check",
            "selection_owner": "bh-ck1t6.2",
            "activation_owner": "bh-ck1t6.3",
        },
        "global_certification_inputs": [
            "scripts/test_closures.py",
            "scripts/test_closure_certification.py",
            "tests/closures.toml",
        ],
        "closures": [
            _record(
                "module.alpha",
                kind="module",
                sources=[
                    "src/beadhive/modules/alpha/contracts.py",
                    "src/beadhive/modules/alpha/service.py",
                ],
                tests=alpha_tests,
                reverse_dependents=["plugin.echo"],
                reverse_tests=alpha_reverse,
            ),
            _record(
                "module.beta",
                kind="module",
                sources=["src/beadhive/modules/beta/service.py"],
                tests=["tests/unit/modules/beta/test_service.py"],
            ),
            _record(
                "plugin.echo",
                kind="plugin",
                sources=["src/beadhive/echo_plugin.py"],
                tests=["tests/test_echo_plugin.py"],
            ),
            _record(
                "contracts",
                kind="contract",
                sources=["src/beadhive/testing/conformance.py"],
                tests=["tests/unit/testing/test_conformance.py"],
            ),
        ],
    }


@pytest.mark.parametrize(
    "case",
    json.loads(GOLDEN.read_text(encoding="utf-8")),
    ids=lambda case: case["name"],
)
def test_golden_impact_plans(case: dict[str, object], evidence: dict[str, object]) -> None:
    plan = selector.build_plan(
        case["changes"],
        evidence,
        base="fixture-base",
        head="fixture-head",
        merge_base="fixture-base",
    )

    for field in ("decision", "commands", "changed_modules", "fallback_reasons"):
        assert plan[field] == case[field]
    assert plan["selector"]["version"] == selector.SELECTOR_VERSION
    assert plan["selector"]["digest"].startswith("sha256:")
    assert plan["plan_digest"].startswith("sha256:")
    assert plan["activation"] == {"enabled": False, "owner": "bh-ck1t6.3"}


def test_module_plan_reports_reverse_dependency_path_and_exact_tests(
    evidence: dict[str, object],
) -> None:
    plan = selector.build_plan(
        [{"status": "M", "path": "src/beadhive/modules/alpha/contracts.py"}],
        evidence,
        base="fixture-base",
        head="fixture-head",
        merge_base="fixture-base",
    )

    assert plan["tests"] == [
        "tests/test_alpha_consumer.py",
        "tests/unit/modules/alpha/test_service.py",
    ]
    assert plan["dependency_paths"] == [
        {
            "from": "src/beadhive/modules/alpha/contracts.py",
            "relationship": "ownership/import",
            "tests": [],
            "to": "module.alpha",
        },
        {
            "from": "module.alpha",
            "relationship": "reverse-dependency",
            "tests": ["tests/test_alpha_consumer.py"],
            "to": "plugin.echo",
        },
    ]
    assert plan["contracts"] == []
    assert plan["closure_digests"] == {"module.alpha": "sha256:module-alpha-digest"}
    assert {item["closure"] for item in plan["exclusions"]} == {
        "contracts",
        "module.beta",
        "plugin.echo",
    }
    assert all(item["reason"] == "unaffected-current-digests-stable" for item in plan["exclusions"])
    assert all(item["status"] == "unaffected" for item in plan["exclusions"])
    assert all(item["current_applicability"]["applicable"] is True for item in plan["exclusions"])


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("observed-digest", "input-digest-mismatch"),
        ("coverage-digest", "missing-or-stale-coverage"),
        ("coverage-revision", "missing-or-stale-coverage"),
        ("uncertified", "closure-not-certified"),
        ("artifact", "certification-artifact-invalid"),
        ("merge-base", "merge-base-ambiguity"),
    ),
)
def test_uncertainty_fails_closed(mutation: str, reason: str, evidence: dict[str, object]) -> None:
    alpha = evidence["closures"][0]
    kwargs: dict[str, object] = {}
    if mutation == "observed-digest":
        alpha["observed_input_digest"] = "sha256:stale"
    elif mutation == "coverage-digest":
        alpha["coverage_mapping"]["input_digest"] = "sha256:stale"
    elif mutation == "coverage-revision":
        alpha["coverage_mapping"]["source_revision"] = "stale-revision"
    elif mutation == "uncertified":
        alpha["certification"]["status"] = "uncertified"
        alpha["certification"]["eligible_for_selective_activation"] = False
    elif mutation == "artifact":
        kwargs["artifact_errors"] = ("forged policy",)
    else:
        kwargs["merge_base_status"] = "ambiguous"
    plan = selector.build_plan(
        [{"status": "M", "path": "src/beadhive/modules/alpha/service.py"}],
        evidence,
        base="fixture-base",
        head="fixture-head",
        merge_base="fixture-base",
        **kwargs,
    )

    assert plan["decision"] == "full"
    assert plan["commands"] == ["just check"]
    assert reason in plan["fallback_reasons"]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("missing", "missing-current-applicability"),
        ("false-empty", "current-applicability-not-proven"),
        ("partial", "invalid-current-applicability"),
        ("malformed", "invalid-current-applicability"),
        ("unknown", "invalid-current-applicability"),
        ("input-digest", "current-input-digest-mismatch"),
        ("registry-digest", "current-registry-digest-mismatch"),
        ("material-digest", "current-material-digest-mismatch"),
    ),
)
def test_incomplete_or_forged_current_applicability_fails_closed(
    mutation: str, reason: str, evidence: dict[str, object]
) -> None:
    alpha = evidence["closures"][0]
    applicability = alpha["current_applicability"]
    if mutation == "missing":
        alpha.pop("current_applicability")
    elif mutation == "false-empty":
        applicability["applicable"] = False
    elif mutation == "partial":
        applicability.pop("observed_material_digest")
    elif mutation == "malformed":
        alpha["current_applicability"] = []
    elif mutation == "unknown":
        applicability["forged"] = True
    else:
        field = {
            "input-digest": "observed_input_digest",
            "registry-digest": "observed_registry_digest",
            "material-digest": "observed_material_digest",
        }[mutation]
        applicability[field] = "sha256:forged"

    plan = selector.build_plan(
        [{"status": "M", "path": "src/beadhive/modules/alpha/service.py"}],
        evidence,
        base="fixture-base",
        head="fixture-head",
        merge_base="fixture-base",
    )

    assert plan["decision"] == "full"
    assert plan["commands"] == ["just check"]
    assert reason in plan["fallback_reasons"]


def test_stable_unrelated_closure_has_honest_current_exclusion(
    evidence: dict[str, object],
) -> None:
    plan = selector.build_plan(
        [{"status": "M", "path": "src/beadhive/modules/alpha/service.py"}],
        evidence,
        base="fixture-base",
        head="fixture-head",
        merge_base="fixture-base",
    )
    beta = next(item for item in plan["exclusions"] if item["closure"] == "module.beta")

    assert plan["decision"] == "selective"
    assert beta == {
        "closure": "module.beta",
        "status": "unaffected",
        "reason": "unaffected-current-digests-stable",
        "historical_input_digest": "sha256:module-beta-digest",
        "current_applicability": evidence["closures"][1]["current_applicability"],
    }


def test_plan_is_canonical_deterministic_and_selector_bound(evidence: dict[str, object]) -> None:
    changes = [{"path": "src/beadhive/modules/alpha/service.py", "status": "M"}]

    first = selector.build_plan(
        changes,
        evidence,
        base="fixture-base",
        head="fixture-head",
        merge_base="fixture-base",
    )
    second = selector.build_plan(
        list(reversed(changes)),
        json.loads(json.dumps(evidence)),
        base="fixture-base",
        head="fixture-head",
        merge_base="fixture-base",
    )
    changed_selector = selector.build_plan(
        changes,
        evidence,
        base="fixture-base",
        head="fixture-head",
        merge_base="fixture-base",
        selector_digest="sha256:different-selector",
    )

    assert first == second
    assert first["plan_digest"] != changed_selector["plan_digest"]


class _RecordingGit:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str) -> bytes:
        self.calls.append(args)
        values = {
            ("rev-parse", "--verify", "base^{commit}"): b"base\n",
            ("rev-parse", "--verify", "head^{commit}"): b"head\n",
            ("merge-base", "--all", "base", "head"): b"base\n",
            ("diff", "--name-status", "-z", "--find-renames", "base..head"): (
                b"M\0src/beadhive/modules/alpha/service.py\0"
            ),
            ("show", "head:src/beadhive/modules/alpha/service.py"): b"VALUE = 2\n",
        }
        return values[args]


def test_range_adapter_is_read_only_network_free_and_uses_only_git_queries(
    evidence: dict[str, object], tmp_path: Path
) -> None:
    marker = tmp_path / "untouched"
    marker.write_text("same\n", encoding="utf-8")
    before = tuple((path.name, path.read_bytes()) for path in sorted(tmp_path.iterdir()))
    git = _RecordingGit()

    plan = selector.select_range(git, evidence, base="base", head="head")

    after = tuple((path.name, path.read_bytes()) for path in sorted(tmp_path.iterdir()))
    assert plan["decision"] == "selective"
    assert before == after
    assert git.calls
    assert {call[0] for call in git.calls} <= {"rev-parse", "merge-base", "diff", "show"}
    assert "socket" not in SCRIPT.read_text(encoding="utf-8")


def _filesystem_identity(root: Path) -> dict[str, tuple[int, bytes | None]]:
    identity: dict[str, tuple[int, bytes | None]] = {}
    for path in sorted((root, *root.rglob("*")), key=lambda item: item.as_posix()):
        relative = "." if path == root else path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISREG(metadata.st_mode):
            content: bytes | None = path.read_bytes()
        elif stat.S_ISLNK(metadata.st_mode):
            content = os.fsencode(path.readlink())
        else:
            content = None
        identity[relative] = (metadata.st_mode, content)
    return identity


def test_real_cli_is_byte_identical_and_preserves_every_clone_path(
    tmp_path: Path,
) -> None:
    seed = tmp_path / "seed"
    for relative in (
        "scripts/test_impact_selector.py",
        "scripts/test_closure_certification.py",
        "scripts/test_closures.py",
        "docs/proof/bh-ck1t6.1-test-closure-certification.json",
        "tests/closures.toml",
    ):
        target = seed / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    (seed / "README.md").write_text("baseline\n", encoding="utf-8")

    def git(directory: Path, *args: str) -> None:
        subprocess.run(
            ("git", "-C", str(directory), *args),
            check=True,
            capture_output=True,
        )

    git(seed, "init", "--quiet")
    git(seed, "add", ".")
    git(
        seed,
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=Selector Test",
        "-c",
        "user.email=selector@example.test",
        "commit",
        "--quiet",
        "-m",
        "baseline",
    )
    (seed / "README.md").write_text("candidate\n", encoding="utf-8")
    git(seed, "add", "README.md")
    git(
        seed,
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=Selector Test",
        "-c",
        "user.email=selector@example.test",
        "commit",
        "--quiet",
        "-m",
        "candidate",
    )
    clone = tmp_path / "clone"
    subprocess.run(
        ("git", "clone", "--quiet", "--local", "--no-hardlinks", str(seed), str(clone)),
        check=True,
        capture_output=True,
    )
    network_guard = tmp_path / "network-guard"
    network_guard.mkdir()
    (network_guard / "sitecustomize.py").write_text(
        "import socket\n"
        "def forbidden(*args, **kwargs):\n"
        "    raise RuntimeError('network access forbidden')\n"
        "socket.socket = forbidden\n"
        "socket.create_connection = forbidden\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    environment["PYTHONPATH"] = str(network_guard)
    command = (
        sys.executable,
        str(clone / "scripts/test_impact_selector.py"),
        "--base",
        "HEAD^",
        "--head",
        "HEAD",
    )
    before = _filesystem_identity(clone)

    first = subprocess.run(
        command,
        cwd=clone,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    second = subprocess.run(
        command,
        cwd=clone,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(first.stdout)["activation"]["enabled"] is False
    assert first.stdout == second.stdout
    assert _filesystem_identity(clone) == before


@pytest.mark.parametrize("fails", (False, True), ids=("success", "failure"))
def test_dependency_loader_restores_bytecode_and_module_state(
    monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    module_name = "test_closure_certification_for_selector"
    previous_module = object()
    loaded_module = object()
    observed_flags: list[bool] = []

    class Loader:
        def exec_module(self, _module: object) -> None:
            observed_flags.append(sys.dont_write_bytecode)
            if fails:
                raise RuntimeError("loader failed")

    fake_spec = SimpleNamespace(name=module_name, loader=Loader())
    monkeypatch.setitem(sys.modules, module_name, previous_module)
    monkeypatch.setattr(selector.importlib.util, "spec_from_file_location", lambda *_: fake_spec)
    monkeypatch.setattr(selector.importlib.util, "module_from_spec", lambda _: loaded_module)
    original_flag = sys.dont_write_bytecode

    if fails:
        with pytest.raises(RuntimeError, match="loader failed"):
            selector._load_certification()
    else:
        assert selector._load_certification() is loaded_module

    assert observed_flags == [True]
    assert sys.dont_write_bytecode is original_flag
    assert sys.modules[module_name] is previous_module


def test_real_dependency_loading_leaves_no_module_registry_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = ("test_closure_certification_for_selector", "closure_registry")
    for name in names:
        monkeypatch.delitem(sys.modules, name, raising=False)
    original_flag = sys.dont_write_bytecode

    certification = selector._load_certification()

    assert callable(certification.current_applicability)
    assert sys.dont_write_bytecode is original_flag
    assert not set(names) & set(sys.modules)


def test_nested_dependency_loader_restores_state_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    certification = selector._load_certification()
    module_name = "closure_registry"
    previous_module = object()
    loaded_module = object()
    observed_flags: list[bool] = []

    class Loader:
        def exec_module(self, _module: object) -> None:
            observed_flags.append(sys.dont_write_bytecode)
            raise RuntimeError("nested loader failed")

    fake_spec = SimpleNamespace(name=module_name, loader=Loader())
    monkeypatch.setitem(sys.modules, module_name, previous_module)
    monkeypatch.setattr(
        certification.importlib.util, "spec_from_file_location", lambda *_: fake_spec
    )
    monkeypatch.setattr(certification.importlib.util, "module_from_spec", lambda _: loaded_module)
    original_flag = sys.dont_write_bytecode

    with pytest.raises(RuntimeError, match="nested loader failed"):
        certification._load_test_closures()

    assert observed_flags == [True]
    assert sys.dont_write_bytecode is original_flag
    assert sys.modules[module_name] is previous_module


def test_checked_evidence_remains_uncertified_and_disabled() -> None:
    checked = json.loads(
        (ROOT / "docs/proof/bh-ck1t6.1-test-closure-certification.json").read_text(encoding="utf-8")
    )

    assert len(checked["closures"]) == 24
    assert checked["policy"]["activation"] == "disabled"
    assert all(row["certification"]["status"] == "uncertified" for row in checked["closures"])
    assert not any(
        row["certification"]["eligible_for_selective_activation"] for row in checked["closures"]
    )


def test_current_root_descendant_consumes_snapshot_and_reports_other_closure_drift() -> None:
    checked = json.loads(
        (ROOT / "docs/proof/bh-ck1t6.1-test-closure-certification.json").read_text(encoding="utf-8")
    )
    certification = selector._load_certification()
    artifact_errors = certification.validate_evidence(checked, ROOT)
    applicability = certification.current_applicability(checked, ROOT)
    projected = selector.apply_current_applicability(checked, applicability)
    plan = selector.build_plan(
        [
            {
                "status": "M",
                "path": "src/beadhive/modules/hives/application/services.py",
            }
        ],
        projected,
        base=certification._historical_snapshot_commit(ROOT),
        head=certification._git(ROOT, "rev-parse", "HEAD"),
        merge_base=certification._historical_snapshot_commit(ROOT),
        artifact_errors=artifact_errors,
    )

    assert artifact_errors == ()
    assert sum(item["applicable"] for item in applicability.values()) >= 1
    assert plan["changed_modules"] == ["module.hives"]
    assert plan["decision"] == "full"
    assert "certification-artifact-invalid" not in plan["fallback_reasons"]
    assert {
        "closure-not-certified",
        "missing-or-stale-coverage",
    } <= set(plan["fallback_reasons"])
    # The remote config-policy change intentionally invalidates the historical config-store
    # closure. Selective validation is still disabled, and the selector must report that
    # unrelated current drift instead of implying the whole historical snapshot is current.
    assert {
        "current-applicability-not-proven",
        "current-input-digest-mismatch",
        "input-digest-mismatch",
    } <= set(plan["fallback_reasons"])
    # module.work is intentionally changed by bh-z57s1, so use another unrelated closure whose
    # current inputs still match the historical snapshot. The assertion remains the same proof:
    # one drifting closure must not make every unaffected exclusion look inapplicable.
    state_exclusion = next(item for item in plan["exclusions"] if item["closure"] == "module.state")
    assert state_exclusion["status"] == "unaffected"
    assert state_exclusion["reason"] == "unaffected-current-digests-stable"
    assert state_exclusion["current_applicability"]["applicable"] is True
    assert "applicability_fallback_reasons" not in state_exclusion


def test_real_git_snapshot_keeps_unrelated_closure_current_and_invalidates_impacted_one(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "src/beadhive/modules/alpha").mkdir(parents=True)
    (repo / "src/beadhive/modules/beta").mkdir(parents=True)
    (repo / "tests/unit/modules/alpha").mkdir(parents=True)
    (repo / "tests/unit/modules/beta").mkdir(parents=True)
    (repo / "docs/proof").mkdir(parents=True)

    def git(*args: str) -> str:
        completed = subprocess.run(
            ("git", "-C", str(repo), *args),
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    alpha_source = repo / "src/beadhive/modules/alpha/service.py"
    beta_source = repo / "src/beadhive/modules/beta/service.py"
    alpha_test = repo / "tests/unit/modules/alpha/test_service.py"
    beta_test = repo / "tests/unit/modules/beta/test_service.py"
    alpha_source.write_text("ALPHA = 1\n", encoding="utf-8")
    beta_source.write_text("BETA = 1\n", encoding="utf-8")
    alpha_test.write_text("def test_alpha(): pass\n", encoding="utf-8")
    beta_test.write_text("def test_beta(): pass\n", encoding="utf-8")
    registry = """\
[registry]
schema_version = 1
full_gate = "just check"
release_gate = "just check-all"
expected_modules = ["alpha", "beta"]

[[closures]]
id = "module.alpha"
kind = "module"
status = "present"
owner_path = "src/beadhive/modules/alpha"
source_paths = ["src/beadhive/modules/alpha/**/*.py"]
tests = ["tests/unit/modules/alpha/test_service.py"]
shared_contracts = []
shared_contract_tests = []
reverse_dependencies = []
reverse_dependency_tests = []

[[closures]]
id = "module.beta"
kind = "module"
status = "present"
owner_path = "src/beadhive/modules/beta"
source_paths = ["src/beadhive/modules/beta/**/*.py"]
tests = ["tests/unit/modules/beta/test_service.py"]
shared_contracts = []
shared_contract_tests = []
reverse_dependencies = []
reverse_dependency_tests = []
"""
    (repo / "tests/closures.toml").write_text(registry, encoding="utf-8")
    snapshot = {
        "closures": [
            _record(
                "module.alpha",
                kind="module",
                sources=["src/beadhive/modules/alpha/service.py"],
                tests=["tests/unit/modules/alpha/test_service.py"],
            ),
            _record(
                "module.beta",
                kind="module",
                sources=["src/beadhive/modules/beta/service.py"],
                tests=["tests/unit/modules/beta/test_service.py"],
            ),
        ]
    }
    evidence_path = repo / "docs/proof/bh-ck1t6.1-test-closure-certification.json"
    evidence_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    git("init", "--quiet")
    git("add", ".")
    git(
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=Selector Test",
        "-c",
        "user.email=selector@example.test",
        "commit",
        "--quiet",
        "-m",
        "baseline",
    )
    certification = selector._load_certification()

    baseline = certification.current_applicability(snapshot, repo)
    assert baseline["module.alpha"]["applicable"] is True
    assert baseline["module.beta"]["applicable"] is True

    beta_source.write_text("BETA = 2\n", encoding="utf-8")
    git("add", str(beta_source.relative_to(repo)))
    git(
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=Selector Test",
        "-c",
        "user.email=selector@example.test",
        "commit",
        "--quiet",
        "-m",
        "change beta",
    )
    after_unrelated_module = certification.current_applicability(snapshot, repo)
    assert after_unrelated_module["module.alpha"]["applicable"] is True
    assert after_unrelated_module["module.beta"]["fallback_reasons"] == ["input-digest-mismatch"]

    alpha_source.write_text("ALPHA = 2\n", encoding="utf-8")
    git("add", str(alpha_source.relative_to(repo)))
    git(
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=Selector Test",
        "-c",
        "user.email=selector@example.test",
        "commit",
        "--quiet",
        "-m",
        "change alpha",
    )
    after_impacted_module = certification.current_applicability(snapshot, repo)
    assert after_impacted_module["module.alpha"]["fallback_reasons"] == ["input-digest-mismatch"]
    assert after_impacted_module["module.beta"]["fallback_reasons"] == ["input-digest-mismatch"]


def _minimal_applicability_repo(
    tmp_path: Path,
) -> tuple[Path, dict[str, object], object]:
    repo = tmp_path / "registry-repo"
    (repo / "src/beadhive/modules/alpha").mkdir(parents=True)
    (repo / "tests/unit/modules/alpha").mkdir(parents=True)
    (repo / "docs/proof").mkdir(parents=True)
    (repo / "src/beadhive/modules/alpha/service.py").write_text("ALPHA = 1\n", encoding="utf-8")
    (repo / "tests/unit/modules/alpha/test_service.py").write_text(
        "def test_alpha(): pass\n", encoding="utf-8"
    )
    registry = """\
[registry]
schema_version = 1
full_gate = "just check"
release_gate = "just check-all"
expected_modules = ["alpha"]

[[closures]]
id = "module.alpha"
kind = "module"
status = "present"
owner_path = "src/beadhive/modules/alpha"
source_paths = ["src/beadhive/modules/alpha/**/*.py"]
pytest_args = []
tests = ["tests/unit/modules/alpha/test_service.py"]
shared_contracts = []
shared_contract_tests = []
reverse_dependencies = []
reverse_dependency_tests = []
"""
    (repo / "tests/closures.toml").write_text(registry, encoding="utf-8")
    snapshot: dict[str, object] = {
        "closures": [
            _record(
                "module.alpha",
                kind="module",
                sources=["src/beadhive/modules/alpha/service.py"],
                tests=["tests/unit/modules/alpha/test_service.py"],
            )
        ]
    }
    (repo / "docs/proof/bh-ck1t6.1-test-closure-certification.json").write_text(
        json.dumps(snapshot, indent=2), encoding="utf-8"
    )

    def git(*args: str) -> str:
        completed = subprocess.run(
            ("git", "-C", str(repo), *args),
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    git("init", "--quiet")
    git("add", ".")
    git(
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=Selector Test",
        "-c",
        "user.email=selector@example.test",
        "commit",
        "--quiet",
        "-m",
        "baseline",
    )
    return repo, snapshot, selector._load_certification()


@pytest.mark.parametrize(
    "replacements",
    (
        (('owner_path = "src/beadhive/modules/alpha"', 'owner_path = "src/other"'),),
        (
            (
                'source_paths = ["src/beadhive/modules/alpha/**/*.py"]',
                'source_paths = ["src/beadhive/**/*.py"]',
            ),
        ),
        (("pytest_args = []", 'pytest_args = ["-n", "2"]'),),
        (
            (
                'tests = ["tests/unit/modules/alpha/test_service.py"]',
                'tests = ["tests/test_other.py"]',
            ),
        ),
        (("shared_contracts = []", 'shared_contracts = ["contracts"]'),),
        (
            (
                "shared_contract_tests = []",
                'shared_contract_tests = ["tests/test_contract.py"]',
            ),
        ),
        (("reverse_dependencies = []", 'reverse_dependencies = ["plugin.hitch"]'),),
        (
            (
                "reverse_dependency_tests = []",
                'reverse_dependency_tests = ["tests/test_hitch_plugin.py"]',
            ),
        ),
        (('kind = "module"', 'kind = "contract"'),),
        (('status = "present"', 'status = "absent"'),),
        (('expected_modules = ["alpha"]', 'expected_modules = ["alpha", "beta"]'),),
        (('full_gate = "just check"', 'full_gate = "just verify"'),),
        (('release_gate = "just check-all"', 'release_gate = "just release"'),),
        (
            ('owner_path = "src/beadhive/modules/alpha"', 'owner_path = "src/other"'),
            ("pytest_args = []", 'pytest_args = ["-n", "2"]'),
            ("reverse_dependencies = []", 'reverse_dependencies = ["plugin.hitch"]'),
            (
                "reverse_dependency_tests = []",
                'reverse_dependency_tests = ["tests/test_hitch_plugin.py"]',
            ),
        ),
    ),
    ids=(
        "owner",
        "source-ownership",
        "pytest-args",
        "selectors",
        "shared-contracts",
        "shared-contract-tests",
        "reverse-dependencies-review-regression",
        "reverse-dependency-tests",
        "kind",
        "status",
        "registry-metadata",
        "full-gate",
        "release-gate",
        "combined",
    ),
)
def test_same_id_registry_definition_drift_fails_closed(
    tmp_path: Path, replacements: tuple[tuple[str, str], ...]
) -> None:
    repo, snapshot, certification = _minimal_applicability_repo(tmp_path)
    registry_path = repo / "tests/closures.toml"
    current = registry_path.read_text(encoding="utf-8")
    for old, new in replacements:
        assert old in current
        current = current.replace(old, new)
    registry_path.write_text(current, encoding="utf-8")

    applicability = certification.current_applicability(snapshot, repo)
    projected = selector.apply_current_applicability(snapshot, applicability)
    plan = selector.build_plan(
        [{"status": "M", "path": "src/beadhive/modules/alpha/service.py"}],
        projected,
        base="baseline",
        head="candidate",
        merge_base="baseline",
    )

    assert applicability["module.alpha"]["applicable"] is False
    assert "registry-definition-drift" in applicability["module.alpha"]["fallback_reasons"]
    assert (
        applicability["module.alpha"]["recorded_registry_digest"]
        != applicability["module.alpha"]["observed_registry_digest"]
    )
    assert plan["decision"] == "full"
    assert plan["commands"] == ["just check"]
    assert "registry-definition-drift" in plan["fallback_reasons"]
    assert plan["candidate_closures"][0]["applicability"] == applicability["module.alpha"]


@pytest.mark.parametrize(
    "field",
    (
        "public_ports",
        "mandatory_boundary_tests",
        "real_adapter_tests",
        "reverse_dependent_tests",
        "relationships",
        "relationship_evidence",
        "fallback_triggers",
        "coverage_mapping",
    ),
)
def test_selector_material_drift_fails_closed(tmp_path: Path, field: str) -> None:
    repo, snapshot, certification = _minimal_applicability_repo(tmp_path)
    mutated = json.loads(json.dumps(snapshot))
    mutated["closures"][0][field] = ["forged"] if field != "coverage_mapping" else {}

    applicability = certification.current_applicability(mutated, repo)

    assert applicability["module.alpha"]["applicable"] is False
    assert "certification-material-drift" in applicability["module.alpha"]["fallback_reasons"]
    assert (
        applicability["module.alpha"]["recorded_material_digest"]
        != applicability["module.alpha"]["observed_material_digest"]
    )
