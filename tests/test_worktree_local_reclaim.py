"""HQ-independent, fail-closed local-safe worktree reclamation (bh-o01m6).

Reproduces the 2026-10-10 partial-degradation incident shape: HQ SQL / runtime config is
unavailable (every config read raises ``SqlConfigError``) while the worktree filesystem is a
tmpfs under pressure. ``bh worktree local-reclaim`` must still reclaim the clean, unused,
provably landed worktrees — and must leave dirty, active, locked, in-progress, unlanded, fresh
and unknown trees untouched, reporting each with a reason.

Everything runs in ``tmp_path`` with a real throwaway Git repo; the process table, mount table,
``statvfs`` and ``du`` are fakes, so nothing depends on wall-clock timing or touches a real
worktree root.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import cli, cli_entrypoint, hq_sql_config
from beadhive import worktree_local_reclaim as lr

GIB = 1024**3
ROOT = Path(__file__).resolve().parents[1]


# ---- fixtures ---------------------------------------------------------------------------------


def _git(cwd: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def git_env(monkeypatch, tmp_path):
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = T\n\temail = t@example.invalid\n"
        "[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)


class Hive:
    """A real repo plus one linked worktree per safety scenario under a fake tmpfs root."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "repo"
        self.root = tmp_path / "bh-worktrees"
        self.leaves: dict[str, Path] = {}
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        (self.repo / ".gitignore").write_text(".venv/\n")
        (self.repo / "README").write_text("base\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-qm", "base")

    def leaf(self, name: str, *, landed: bool = True, commit: bool = True) -> Path:
        path = self.root / "github" / "org" / "repo" / name
        branch = f"wt/bead/issue/{name}"
        _git(self.repo, "worktree", "add", "-q", "-b", branch, str(path), "main")
        if commit:
            (path / f"{name}.txt").write_text(name)
            _git(path, "add", ".")
            _git(path, "commit", "-qm", f"feat: {name}")
        if landed and commit:
            _git(self.repo, "merge", "-q", "--no-ff", "-m", f"merge {name}", branch)
        self.leaves[name] = path
        return path


@pytest.fixture
def hive(git_env, tmp_path):
    h = Hive(tmp_path)
    h.leaf("landed-a")
    h.leaf("landed-b")
    ignored = h.leaf("landed-ignored")
    (ignored / ".venv").mkdir()
    (ignored / ".venv" / "lib.so").write_text("x" * 64)  # ignored files never block
    dirty = h.leaf("dirty")
    (dirty / "notes.txt").write_text("uncommitted")  # non-ignored untracked
    tracked = h.leaf("dirty-tracked")
    (tracked / "README").write_text("edited\n")  # tracked modification
    h.leaf("active")
    locked = h.leaf("locked")
    _git(h.repo, "worktree", "lock", str(locked))
    merging = h.leaf("merging")
    gitdir = Path(_git(merging, "rev-parse", "--path-format=absolute", "--git-dir"))
    (gitdir / "MERGE_HEAD").write_text("0" * 40 + "\n")
    validating = h.leaf("validating")
    marker = h.repo / ".git" / "bh" / "validation" / "active"
    marker.mkdir(parents=True)
    (marker / f"{validating.name}.json").write_text("{}")
    h.leaf("unlanded", landed=False)
    h.leaf("fresh", commit=False)
    unknown = h.root / "github" / "org" / "repo" / "unknown"
    unknown.mkdir(parents=True)
    (unknown / ".git").write_text("gitdir: /nonexistent/worktrees/unknown\n")
    h.leaves["unknown"] = unknown
    return h


class FakeTmpfs:
    """A 32 GiB tmpfs at the worktree root that is 66% full; each tree costs 1 GiB."""

    def __init__(self, hive: Hive, holders: dict[str, list[int]] | None = None) -> None:
        self.hive = hive
        self.total = 32 * GIB
        self.baseline_used = int(21.1 * GIB)
        self.holders = holders or {}
        self.scans = 0

    def trees(self) -> list[Path]:
        return [p for n, p in self.hive.leaves.items() if n != "unknown"]

    def used(self) -> int:
        gone = sum(1 for p in self.trees() if not p.exists())
        return self.baseline_used - gone * GIB

    def statvfs(self, path: str) -> tuple[int, int, int]:
        used = self.used()
        return self.total, used, self.total - used

    def mounts(self) -> list[tuple[str, str]]:
        return [("/", "ext4"), (str(self.hive.root.resolve()), "tmpfs")]

    def disk_usage(self, path: str) -> int | None:
        return GIB

    def process_scan(self) -> lr.ProcessScan:
        self.scans += 1
        holders = tuple(
            (pid, str((self.hive.leaves[name] / "sub").resolve()))
            for name, pids in self.holders.items()
            for pid in pids
        )
        return lr.ProcessScan(holders, True)

    def ports(self) -> lr.LocalReclaimPorts:
        return lr.LocalReclaimPorts(
            process_scan=self.process_scan,
            mounts=self.mounts,
            statvfs=self.statvfs,
            disk_usage=self.disk_usage,
        )


@pytest.fixture
def hq_down(monkeypatch):
    """HQ SQL / verified runtime config is unavailable: opening it raises. (That config is
    never even imported on this path — see the subprocess module-load test below.)"""
    calls: list[str] = []

    def unavailable(*_args, **_kwargs):
        calls.append("hq")
        raise hq_sql_config.SqlConfigError("verified HQ config connection unavailable")

    monkeypatch.setattr(hq_sql_config.SqlFleetConfigRevisionStore, "_open", unavailable)
    return calls


def _run(argv: list[str], ports: lr.LocalReclaimPorts) -> tuple[int, dict]:
    out = io.StringIO()
    code = lr.main([*argv, "--json"], ports=ports, out=out, err=io.StringIO())
    return code, json.loads(out.getvalue())


def _by_leaf(report: dict) -> dict[str, dict]:
    return {Path(t["path"]).name: t for t in report["targets"]}


# ---- the incident -----------------------------------------------------------------------------


def test_local_reclaim_under_hq_outage_and_tmpfs_pressure(hive, hq_down):
    fs = FakeTmpfs(hive, holders={"active": [4242]})

    code, report = _run(["--root", str(hive.root)], fs.ports())

    assert code == 0
    assert hq_down == []  # HQ was never consulted
    rows = _by_leaf(report)
    removed = {name for name, row in rows.items() if row["status"] == lr.REMOVED}
    assert removed == {"landed-a", "landed-b", "landed-ignored"}
    for name in removed:
        assert not hive.leaves[name].exists()
        assert rows[name]["reclaimed_bytes"] == GIB
        assert rows[name]["landed_on"] == "main"
    expected_kept = {
        "dirty": "dirty",
        "dirty-tracked": "dirty",
        "active": "in_use",
        "locked": "locked",
        "merging": "in_progress",
        "validating": "validation_active",
        "unlanded": "not_landed",
        "fresh": "no_landed_work",
        "unknown": "not_registered",
    }
    for name, reason in expected_kept.items():
        assert rows[name]["status"] == lr.KEPT, name
        assert reason in rows[name]["reasons"], (name, rows[name])
        assert hive.leaves[name].exists(), name
    assert rows["active"]["holder_pids"] == [4242]
    # branches are the durable artifact and are never deleted
    assert "wt/bead/issue/landed-a" in _git(hive.repo, "branch", "--list", "wt/*")

    assert report["hq_independent"] is True
    assert report["reclaimed_bytes"] == 3 * GIB
    assert report["summary"] == {"removed": 3, "would_remove": 0, "kept": 9, "failed": 0}
    (tmpfs,) = [fs_ for fs_ in report["filesystems"] if fs_["fstype"] == "tmpfs"]
    assert tmpfs["memory_backed"] is True
    assert tmpfs["before"]["used_bytes"] == int(21.1 * GIB)
    assert tmpfs["after"]["used_bytes"] == int(21.1 * GIB) - 3 * GIB
    assert tmpfs["freed_bytes"] == 3 * GIB
    assert tmpfs["before"]["used_percent"] > tmpfs["after"]["used_percent"]


def test_dry_run_removes_nothing_and_reports_reclaimable(hive, hq_down):
    fs = FakeTmpfs(hive)

    code, report = _run(["--root", str(hive.root), "--dry-run"], fs.ports())

    assert code == 0
    assert all(path.exists() for path in hive.leaves.values())
    assert report["summary"]["would_remove"] == 4  # active is not held in this scan
    assert report["reclaimed_bytes"] == 0
    assert report["reclaimable_bytes"] == 4 * GIB


def test_incomplete_process_scan_keeps_everything(hive, hq_down):
    fs = FakeTmpfs(hive)
    ports = fs.ports()
    ports.process_scan = lambda: lr.ProcessScan((), False, "could not inspect own process 7")

    code, report = _run(["--root", str(hive.root)], ports)

    assert code == 0
    assert report["summary"]["removed"] == 0
    assert all(path.exists() for path in hive.leaves.values())
    assert "ambiguous" in _by_leaf(report)["landed-a"]["reasons"]


def test_kernel_hidden_own_processes_are_reported_and_strict_mode_keeps_all(hive, hq_down):
    fs = FakeTmpfs(hive)
    ports = fs.ports()
    ports.process_scan = lambda: lr.ProcessScan((), True, opaque=((7, "sshd-session"),))

    code, report = _run(["--root", str(hive.root), "--dry-run"], ports)
    assert code == 0
    assert report["summary"]["would_remove"] == 4
    assert report["process_scan"]["opaque_own_processes"] == [{"pid": 7, "comm": "sshd-session"}]

    code, report = _run(["--root", str(hive.root), "--strict-process-scan"], ports)
    assert code == 0
    assert report["summary"]["removed"] == 0
    assert all(path.exists() for path in hive.leaves.values())
    assert "ambiguous" in _by_leaf(report)["landed-a"]["reasons"]


def test_holder_appearing_before_removal_is_rechecked(hive, hq_down):
    fs = FakeTmpfs(hive)
    ports = fs.ports()
    target = str(hive.leaves["landed-a"].resolve())

    def scan() -> lr.ProcessScan:
        fs.scans += 1
        held = ((99, target),) if fs.scans > 1 else ()
        return lr.ProcessScan(held, True)

    ports.process_scan = scan

    code, report = _run([target], ports)

    assert code == 1  # a named rm target was kept
    assert hive.leaves["landed-a"].exists()
    row = _by_leaf(report)["landed-a"]
    assert row["status"] == lr.KEPT and "in_use" in row["reasons"]


def test_named_paths_exit_nonzero_when_kept(hive, hq_down):
    fs = FakeTmpfs(hive)
    targets = [str(hive.leaves["landed-b"]), str(hive.leaves["dirty"])]

    code, report = _run([*targets], fs.ports())

    assert code == 1  # dirty was kept
    rows = _by_leaf(report)
    assert rows["landed-b"]["status"] == lr.REMOVED
    assert rows["dirty"]["status"] == lr.KEPT
    assert hive.leaves["dirty"].exists()


def test_targets_from_file_and_main_worktree_refused(hive, hq_down, tmp_path):
    fs = FakeTmpfs(hive)
    listing = tmp_path / "leaves.txt"
    listing.write_text(f"# precomputed\n{hive.leaves['landed-a']}\n{hive.repo}\n")

    code, report = _run(["--targets-from", str(listing)], fs.ports())

    assert code == 0
    rows = _by_leaf(report)
    assert rows["landed-a"]["status"] == lr.REMOVED
    assert rows["repo"]["reasons"] == ["main_worktree"]
    assert hive.repo.exists()


def test_repo_discovery_considers_only_bh_branches(hive, hq_down):
    fs = FakeTmpfs(hive)
    other = hive.root.parent / "manual"
    _git(hive.repo, "worktree", "add", "-q", "-b", "feature/manual", str(other), "main")

    code, report = _run(["--repo", str(hive.repo)], fs.ports())

    assert code == 0
    assert other.exists()
    assert "manual" not in _by_leaf(report)
    assert _by_leaf(report)["landed-a"]["status"] == lr.REMOVED


def test_text_output_names_reasons_and_tmpfs(hive, hq_down):
    fs = FakeTmpfs(hive)
    out = io.StringIO()

    code = lr.main(
        ["--root", str(hive.root), "--dry-run"],
        ports=fs.ports(),
        out=out,
        err=io.StringIO(),
    )

    text = out.getvalue()
    assert code == 0
    assert "HQ not consulted" in text
    assert "[RAM-backed]" in text
    assert "dirty" in text and "not_landed" in text


def test_usage_errors_exit_two(tmp_path):
    err = io.StringIO()
    assert lr.main(["--force"], out=io.StringIO(), err=err) == 2
    missing = tmp_path / "missing.txt"
    assert lr.main(["--targets-from", str(missing)], out=io.StringIO(), err=err) == 2


def test_no_default_root_is_a_usage_error(monkeypatch, tmp_path):
    monkeypatch.delenv("BH_WORKTREES", raising=False)
    monkeypatch.delenv("WS_WORKTREES", raising=False)
    monkeypatch.setattr(lr, "_default_roots", list)
    assert lr.main([], out=io.StringIO(), err=io.StringIO()) == 2


def test_default_root_comes_from_env_without_config(monkeypatch, hive, hq_down):
    fs = FakeTmpfs(hive)
    monkeypatch.setenv("BH_WORKTREES", str(hive.root))

    code, report = _run(["--dry-run"], fs.ports())

    assert code == 0
    assert report["summary"]["would_remove"] == 4
    assert hq_down == []


# ---- HQ is never read: entrypoint fast path and --help ----------------------------------------


def test_entrypoint_selects_local_reclaim_before_the_cli_tree(monkeypatch, hive, hq_down, capsys):
    fs = FakeTmpfs(hive)
    monkeypatch.setattr(lr, "_default_ports", fs.ports)
    monkeypatch.setattr(sys, "argv", ["bh", "wt", "local-reclaim", "--root", str(hive.root)])

    with pytest.raises(SystemExit) as exc:
        cli_entrypoint.main()

    assert exc.value.code == 0
    assert hq_down == []
    assert not hive.leaves["landed-a"].exists()
    assert "HQ not consulted" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["worktree", "local-reclaim"], []),
        (["wt", "local-reclaim", "/x", "--json"], ["/x", "--json"]),
        (["worktree", "prune"], None),
        (["worktree"], None),
        (["work", "local-reclaim"], None),
        (["--hive", "x", "worktree", "local-reclaim"], None),
    ],
)
def test_entrypoint_routing(argv, expected):
    assert cli_entrypoint._local_reclaim_argv(argv) == expected


def test_local_reclaim_help_imports_no_config_cli_or_hq(tmp_path):
    """``--help`` and a real (empty-root) run load neither the CLI tree nor config/HQ."""
    script = f"""
import json, sys
from beadhive.cli_entrypoint import main
codes = []
for argv in (["worktree", "local-reclaim", "--help"],
             ["worktree", "local-reclaim", "--root", {str(tmp_path)!r}, "--json"]):
    sys.argv = ["bh", *argv]
    try:
        main()
    except SystemExit as exc:
        codes.append(exc.code)
print(json.dumps({{"codes": codes, "loaded": sorted(m for m in sys.modules if m in {{
    "beadhive.cli", "beadhive.config", "beadhive.registry", "beadhive.hq_sql_config",
    "beadhive.hq_sql_runtime", "beadhive.worktree"}})}}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    last = json.loads(result.stdout.strip().splitlines()[-1])
    assert last == {"codes": [0, 0], "loaded": []}
    assert "first-line partial-degradation procedure" in " ".join(result.stdout.split()).lower()


@pytest.mark.parametrize("verb", ["prune", "rm", "local-reclaim"])
def test_typer_help_never_loads_config(monkeypatch, verb):
    calls: list[str] = []
    monkeypatch.setattr(cli, "_init_telemetry_best_effort", lambda: calls.append("load"))
    monkeypatch.setattr(cli, "_migrate_hive_keys_best_effort", lambda: calls.append("migrate"))
    monkeypatch.setattr(cli, "_migrate_home_best_effort", lambda: calls.append("home"))
    monkeypatch.setattr(cli.sys, "argv", ["bh", "worktree", verb, "--help"])

    result = CliRunner().invoke(cli.app, ["worktree", verb, "--help"])

    assert result.exit_code == 0, result.output
    assert "Usage" in result.stdout
    assert calls == []


def test_typer_local_reclaim_delegates(monkeypatch, hive, hq_down):
    fs = FakeTmpfs(hive)
    monkeypatch.setattr(lr, "_default_ports", fs.ports)
    monkeypatch.setattr(cli.sys, "argv", ["bh", "worktree", "local-reclaim"])

    result = CliRunner().invoke(
        cli.app,
        ["worktree", "local-reclaim", "--root", str(hive.root), "--dry-run", "--json"],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout[result.stdout.index("{") :])
    assert report["op"] == "worktree.local-reclaim" and report["dry_run"] is True
    assert all(path.exists() for path in hive.leaves.values())


def test_hq_config_error_on_worktree_command_points_at_local_reclaim(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "argv", ["bh", "worktree", "rm", "leaf"])

    cli._handle_cli_error(hq_sql_config.SqlConfigError("verified HQ config connection unavailable"))

    assert "worktree local-reclaim" in capsys.readouterr().err


def test_real_process_scan_sees_own_cwd(tmp_path, monkeypatch):
    if not Path("/proc/self/cwd").exists():
        pytest.skip("no /proc on this platform")
    monkeypatch.chdir(tmp_path)
    scan = lr._real_process_scan()
    if scan.complete:
        assert os.getpid() in scan.holders_of(str(tmp_path.resolve()))
