"""Process-level proof for the closure-certification receipt handshake.

The unit tests in ``test_test_closure_certification.py`` exhaustively cover the
admission predicates.  This file keeps one deliberately outer test around the
failure found during the final modularization review: a real clean-checkout run
must publish a receipt that the standalone certifier process can consume.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from scripts.pants_launcher import launcher as pants_launcher

from beadhive import host, validation_ledger, validation_records

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "test_closure_certification.py"
EVIDENCE = ROOT / "docs" / "proof" / "bh-ck1t6.1-test-closure-certification.json"
MISSING_AUTHORITY = "candidate checkout has no authoritative matching full-gate receipt"
PROCESS_TIMEOUT_SECONDS = 30.0
TERMINATION_GRACE_SECONDS = 2.0
PRODUCTION_JUST = shutil.which("just")
# Captured while pytest imports this module, before the compatibility fixture
# replaces HOME with its isolated test home.
PRODUCTION_PANTS = pants_launcher()


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Terminate and reap a bounded probe together with every nested child."""
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.communicate(timeout=TERMINATION_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.communicate()


def _bounded_run(
    args: tuple[str, ...],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = PROCESS_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run one isolated process group with a finite deadline and complete reaping."""
    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        raise AssertionError(f"timed out after {timeout}s: {' '.join(args)}") from exc
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def _git(repo: Path, *args: str) -> str:
    completed = _bounded_run(("git", "-C", str(repo), *args))
    completed.check_returncode()
    return completed.stdout.strip()


def _candidate_clone(tmp_path: Path, name: str) -> tuple[dict[str, object], dict[str, str], Path]:
    workspace = tmp_path / name / "workspace"
    repo = workspace / "github" / "beadhive" / "beadhive"
    repo.parent.mkdir(parents=True)
    completed = _bounded_run(
        ("git", "clone", "--quiet", "--local", "--no-hardlinks", str(ROOT), str(repo)),
    )
    completed.check_returncode()
    _git(repo, "branch", "-M", "main")
    branch = f"wt/bead/issue/bh-{name}"
    _git(repo, "branch", branch)
    entry: dict[str, object] = {
        "provider": "github",
        "org": "beadhive",
        "repo": "beadhive",
        "prefix": "bh",
    }
    environment = {
        "GIT_WORKSPACE": str(workspace),
        "BH_WORKTREES": str(tmp_path / name / "worktrees"),
    }
    return entry, environment, repo


def _install_recipe_probe(tmp_path: Path, monkeypatch) -> Path:
    """Install a bounded ``just`` probe that executes the real failing entry point.

    The lifecycle still receives the canonical command strings.  Replacing the
    expensive recipe body keeps this regression hermetic while the subprocess
    executes the production certifier from the clean candidate checkout.
    """
    executable = tmp_path / "bin" / "just"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "recipe = sys.argv[-1] if len(sys.argv) > 1 else ''\n"
        "if recipe not in {'check', 'architecture-check', 'check-all'}:\n"
        "    raise SystemExit(64)\n"
        "with Path(os.environ['BH_RECIPE_LOG']).open('a', encoding='utf-8') as out:\n"
        "    out.write(recipe + '\\n')\n"
        "os.execv(\n"
        "    sys.executable,\n"
        "    [sys.executable, 'scripts/test_closure_certification.py', '--check'],\n"
        ")\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    recipe_log = tmp_path / "recipe.log"
    monkeypatch.setenv("PATH", f"{executable.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BH_RECIPE_LOG", str(recipe_log))
    return recipe_log


def _record_untrusted_green_receipts(repo: Path, command: str) -> None:
    """Use the production writer to install stale, foreign, and crossed controls."""
    sha = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    command_hash = validation_ledger.cmd_hash(command)
    controls = (
        ("stale-tree", command, "bh-candidate", "wt/bead/issue/bh-candidate"),
        (tree, command, "bh-foreign", "wt/bead/issue/bh-other"),
        (
            tree,
            "just check-all" if command == "just check" else "just check",
            "bh-candidate" if command == "just check" else None,
            "wt/bead/issue/bh-candidate" if command == "just check" else "main",
        ),
    )
    for receipt_tree, receipt_command, bead, branch in controls:
        run = validation_records.begin_run(
            repo,
            bead=bead,
            phase="check" if command == "just check" else "validation",
            branch=branch,
            worktree=repo,
            sha=sha,
            tree=receipt_tree,
            command_hash=command_hash,
            command=receipt_command,
            owner_start="completed-control",
        )
        assert run is not None
        completed = validation_records.finish_run(repo, run["run_id"], exit_code=0)
        assert completed is not None and completed["verdict"] == "green"


def _architecture_check(
    repo: Path, *, executable: str = "just", timeout: float = PROCESS_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    return _bounded_run(
        (executable, "--justfile", str(repo / "justfile"), "architecture-check"),
        cwd=repo,
        env=os.environ.copy(),
        timeout=timeout,
    )


def _clean_checkout(
    entry: dict[str, object],
    branch: str,
    command: str,
    *,
    bead: str | None,
    phase: str,
    reuse: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Drive the production lifecycle inside the bounded process group."""
    driver = (
        "import json, sys; "
        "from beadhive import worktree; "
        "payload = json.loads(sys.argv[1]); "
        "raise SystemExit(worktree.clean_checkout("
        "payload['entry'], payload['branch'], payload['command'], "
        "cfg={'managed_repos': [payload['entry']]}, bead=payload['bead'], "
        "phase=payload['phase'], reuse=payload['reuse']))"
    )
    payload = json.dumps(
        {
            "entry": entry,
            "branch": branch,
            "command": command,
            "bead": bead,
            "phase": phase,
            "reuse": reuse,
        },
        sort_keys=True,
    )
    return _bounded_run((sys.executable, "-c", driver, payload), env=os.environ.copy())


def _production_recipe_graph(repo: Path) -> dict[str, object]:
    assert PRODUCTION_JUST is not None, "the production just executable is required"
    completed = _bounded_run(
        (
            PRODUCTION_JUST,
            "--justfile",
            str(repo / "justfile"),
            "--dump",
            "--dump-format",
            "json",
        ),
        cwd=repo,
    )
    completed.check_returncode()
    return json.loads(completed.stdout)


def _assert_production_full_gate_wiring(repo: Path) -> None:
    """Prove the production Just graph retains its architecture and full-only phases."""
    graph = _production_recipe_graph(repo)
    recipes = graph["recipes"]
    assert isinstance(recipes, dict)
    architecture = recipes["architecture-check"]
    selective_architecture = recipes["architecture-structural-check"]
    pants_architecture = recipes["architecture-pants-check"]
    attest_architecture = recipes["attest-architecture-contracts"]
    check = recipes["check"]
    check_all = recipes["check-all"]
    check_all_native = recipes["check-all-native"]
    assert isinstance(architecture, dict)
    assert isinstance(selective_architecture, dict)
    assert isinstance(pants_architecture, dict)
    assert isinstance(attest_architecture, dict)
    assert isinstance(check, dict)
    assert isinstance(check_all, dict)
    assert isinstance(check_all_native, dict)
    architecture_body = [row[0] for row in architecture["body"]]
    assert architecture_body == [
        "uv run python scripts/check_import_boundaries.py",
        "uv run python scripts/check_package_imports.py",
        "uv run python scripts/test_closure_certification.py --check",
        "uv run python scripts/test_closure_shadow_policy.py --check",
        "uv run python scripts/test_closure_promotion_policy.py --check",
        "uv run python scripts/test_closure_operational_report.py --check",
        "uv run python scripts/pants_shadow_evidence.py",
        "uv run python scripts/check_pants_ownership.py",
        "uv run python scripts/check_pants_proven.py",
        "uv run python scripts/pants_ci.py verify",
        "uv run python scripts/pants_ci_benchmark.py check",
    ]
    selective_body = [row[0] for row in selective_architecture["body"]]
    assert selective_body == [
        "uv run python scripts/check_import_boundaries.py",
        "uv run python scripts/check_package_imports.py",
        "uv run python scripts/test_closure_certification.py --check-structural",
        "uv run python scripts/test_closure_shadow_policy.py --check",
        "uv run python scripts/test_closure_promotion_policy.py --check",
        "uv run python scripts/test_closure_operational_report.py --check",
        "just transport-artifact-check",
        "just wire-schema-compat",
        "just proof-digest-check",
    ]
    pants_body = [row[0] for row in pants_architecture["body"]]
    assert pants_body == architecture_body[6:]
    attest_body = [row[0] for row in attest_architecture["body"]]
    assert "just architecture-structural-check" in attest_body
    assert "just architecture-check" not in attest_body
    check_dependencies = {item["recipe"] for item in check["dependencies"]}
    check_all_dependencies = {item["recipe"] for item in check_all["dependencies"]}
    full_only = {
        "require-bd",
        "pants-attest",
        "test-integration-land",
        "demo-local-loop",
        "demo-live-ingress",
    }
    assert "architecture-structural-check" in check_dependencies & check_all_dependencies
    assert "architecture-check" not in check_dependencies | check_all_dependencies
    assert full_only.isdisjoint(check_dependencies)
    assert full_only <= check_all_dependencies
    assert check_all_dependencies != check_dependencies
    check_test = next(item for item in check["dependencies"] if item["recipe"] == "test-changed")
    assert check_test["arguments"] == []
    assert {"stateful-pants", "stateful-native"} <= check_all_dependencies
    assert "architecture-pants-check" in check_all_dependencies
    assert "test" not in check_all_dependencies
    native_dependencies = {item["recipe"] for item in check_all_native["dependencies"]}
    assert "architecture-structural-check" in native_dependencies
    assert "architecture-pants-check" not in native_dependencies
    assert "stateful-pants" not in native_dependencies
    assert "pants-attest" not in native_dependencies

    def recipe_closure(name: str, seen: set[str] | None = None) -> set[str]:
        visited = set() if seen is None else seen
        if name in visited:
            return visited
        visited.add(name)
        for dependency in recipes[name]["dependencies"]:
            recipe_closure(dependency["recipe"], visited)
        return visited

    native_graph = recipe_closure("check-all-native")
    assert not {"architecture-pants-check", "stateful-pants", "pants-attest"} & native_graph
    for name in native_graph:
        for row in recipes[name]["body"]:
            assert "pants" not in row[0].lower(), f"native gate launches Pants via {name}: {row[0]}"


def _manifests(repo: Path) -> list[dict[str, object]]:
    root = repo / ".git" / "bh" / "validation" / "runs"
    return [json.loads(path.read_text()) for path in sorted(root.glob("*/manifest.json"))]


def test_structural_mode_bootstraps_without_receipt_but_remains_fail_closed(
    tmp_path: Path,
) -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    valid = tmp_path / "valid.json"
    stale = tmp_path / "stale.json"
    valid.write_text(json.dumps(evidence), encoding="utf-8")
    evidence["source_revision"] = "sha256:" + "0" * 64
    stale.write_text(json.dumps(evidence), encoding="utf-8")

    structural = _bounded_run(
        (sys.executable, str(SCRIPT), "--check-structural", "--output", str(valid)), cwd=ROOT
    )
    strict = _bounded_run(
        (sys.executable, str(SCRIPT), "--check", "--output", str(valid)), cwd=ROOT
    )
    stale_structural = _bounded_run(
        (sys.executable, str(SCRIPT), "--check-structural", "--output", str(stale)), cwd=ROOT
    )
    stale_strict = _bounded_run(
        (sys.executable, str(SCRIPT), "--check", "--output", str(stale)), cwd=ROOT
    )

    assert structural.returncode == 0, structural.stdout + structural.stderr
    assert strict.returncode == 1
    assert "receipt" in strict.stdout
    assert stale_structural.returncode == 1
    assert "source revision" in stale_structural.stdout
    assert stale_strict.returncode == 1
    assert "source revision" in stale_strict.stdout


def _assert_scratch_clean(repo: Path, worktrees_root: Path) -> None:
    registered = [
        line.removeprefix("worktree ")
        for line in _git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]
    assert registered == [str(repo)]
    assert not list(worktrees_root.glob("github/beadhive/beadhive/verify-*"))
    assert not list((repo / ".git/bh/validation/active").glob("*.json"))
    assert all(manifest["lifecycle"] == "completed" for manifest in _manifests(repo))


def test_selective_architecture_recipe_uses_only_explicit_structural_mode() -> None:
    _assert_production_full_gate_wiring(ROOT)


def test_bounded_probe_terminates_and_reaps_nested_children(tmp_path: Path) -> None:
    """The timeout path kills the whole session, not just its direct driver."""
    child_pid_path = tmp_path / "child.pid"
    driver = (
        "import signal, subprocess, sys, time; "
        "from pathlib import Path; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "signal.signal(signal.SIGTERM, lambda *_: (child.wait(), sys.exit(143))); "
        "Path(sys.argv[1]).write_text(str(child.pid)); "
        "time.sleep(60)"
    )
    with pytest.raises(AssertionError, match="timed out after"):
        _bounded_run(
            (sys.executable, "-c", driver, str(child_pid_path)),
            timeout=0.5,
        )

    child_pid = int(child_pid_path.read_text())
    deadline = time.monotonic() + TERMINATION_GRACE_SECONDS
    while time.monotonic() < deadline:
        try:
            Path(f"/proc/{child_pid}/stat").read_text()
        except (FileNotFoundError, IndexError, OSError):
            break
        time.sleep(0.02)
    else:
        pytest.fail(f"nested probe child {child_pid} survived process-group timeout cleanup")


def test_real_gate_receipts_authorize_only_their_exact_candidate_process(
    tmp_path: Path, monkeypatch
) -> None:
    """Reproduce the review failure, then prove both canonical bootstrap paths.

    This test is RED on the reviewed ``071ebdf`` implementation: the completed
    downstream check is rejected and the independent check-all run cannot
    bootstrap from its own command identity.  It is GREEN with ``bh-uvotu.1``.
    """
    host.mint_if_needed()
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", sys.prefix)
    monkeypatch.setenv("UV_NO_SYNC", "1")
    monkeypatch.setenv("UV_OFFLINE", "1")
    # The candidate clone lives at a new, intentionally disposable path.  Resolve
    # Pants from the trusted source checkout before entering that clone so mise's
    # per-directory trust policy cannot make the production architecture recipe
    # look launcher-less.
    monkeypatch.setenv("PANTS_BIN", PRODUCTION_PANTS)

    # Ordinary check: stale and foreign green evidence stays red; the real running
    # manifest bootstraps the certifier; its completion then authorizes both reuse
    # and the exact standalone architecture-check reproduction from final review.
    check_entry, check_environment, check_repo = _candidate_clone(tmp_path, "check-proof")
    for key, value in check_environment.items():
        monkeypatch.setenv(key, value)
    check_log = _install_recipe_probe(tmp_path / "check-probe", monkeypatch)
    _record_untrusted_green_receipts(check_repo, "just check")
    rejected = _architecture_check(check_repo)
    assert rejected.returncode == 1
    assert MISSING_AUTHORITY in rejected.stdout

    check_branch = "wt/bead/issue/bh-check-proof"
    checked = _clean_checkout(
        check_entry,
        check_branch,
        "just check",
        bead="bh-check-proof",
        phase="check",
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    check_runs = _manifests(check_repo)
    exact_check = [
        item
        for item in check_runs
        if item["tree"] == _git(check_repo, "rev-parse", "HEAD^{tree}")
        and item["command"] == "just check"
        and item["bead"] == "bh-check-proof"
    ]
    assert len(exact_check) == 1
    assert exact_check[0]["command_hash"] == validation_ledger.cmd_hash("just check")

    runs_before_reuse = len(check_runs)
    log_before_reuse = check_log.read_text().splitlines()
    reused = _clean_checkout(
        check_entry,
        check_branch,
        "just check",
        reuse=True,
        bead="bh-check-proof",
        phase="check",
    )
    assert reused.returncode == 0, reused.stdout + reused.stderr
    assert len(_manifests(check_repo)) == runs_before_reuse
    assert check_log.read_text().splitlines() == log_before_reuse
    standalone = _architecture_check(check_repo)
    assert standalone.returncode == 0, standalone.stdout + standalone.stderr
    assert "test-closure certification: OK" in standalone.stdout
    assert "check-all" not in check_log.read_text().splitlines()

    # This is independent of the synthetic lifecycle probe above: the production
    # Just parser must expose the real entry point and release-only dependency set,
    # then the actual production architecture recipe must execute successfully.
    _assert_production_full_gate_wiring(check_repo)
    assert PRODUCTION_JUST is not None
    production_architecture = _architecture_check(
        check_repo,
        executable=PRODUCTION_JUST,
        timeout=60.0,
    )
    assert production_architecture.returncode == 0, (
        production_architecture.stdout + production_architecture.stderr
    )
    assert "import-boundary-check: OK" in production_architecture.stdout
    assert "test-closure certification: OK" in production_architecture.stdout

    # Release gate: use an independent Git store so no completed check can mask
    # whether the running check-all receipt authorizes its own architecture phase.
    release_entry, release_environment, release_repo = _candidate_clone(tmp_path, "release-proof")
    for key, value in release_environment.items():
        monkeypatch.setenv(key, value)
    release_log = _install_recipe_probe(tmp_path / "release-probe", monkeypatch)
    _record_untrusted_green_receipts(release_repo, "just check-all")
    rejected = _architecture_check(release_repo)
    assert rejected.returncode == 1
    assert MISSING_AUTHORITY in rejected.stdout

    released = _clean_checkout(
        release_entry,
        "main",
        "just check-all",
        bead=None,
        phase="validation",
    )
    assert released.returncode == 0, released.stdout + released.stderr
    release_runs = _manifests(release_repo)
    exact_release = [
        item
        for item in release_runs
        if item["tree"] == _git(release_repo, "rev-parse", "HEAD^{tree}")
        and item["command"] == "just check-all"
        and item["bead"] is None
    ]
    assert len(exact_release) == 1
    assert exact_release[0]["command_hash"] == validation_ledger.cmd_hash("just check-all")
    assert exact_release[0]["command_hash"] != exact_check[0]["command_hash"]
    assert release_log.read_text().splitlines()[-1] == "check-all"

    completed_release = _architecture_check(release_repo)
    assert completed_release.returncode == 0, completed_release.stdout + completed_release.stderr
    _assert_scratch_clean(check_repo, Path(check_environment["BH_WORKTREES"]))
    _assert_scratch_clean(release_repo, Path(release_environment["BH_WORKTREES"]))
