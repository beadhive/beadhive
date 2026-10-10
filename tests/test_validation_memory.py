"""Memory bounds, memory-aware admission, and peak accounting for validation runs (bh-jg7fy).

Admission is exercised against a faked ``/proc/meminfo`` only — never real memory pressure.  The
single real-allocation test runs a tiny hog (at most 512 MiB requested, 16 MiB at a time) inside
its own user systemd scope with ``MemoryMax=96M`` and ``MemorySwapMax=0``; the hog itself refuses
to allocate unless it can see that bound on its own cgroup, so nothing is ever allocated outside
a scope.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest
import typer

from beadhive import (
    config_schema,
    validation_admission,
    validation_memory,
    validation_records,
    worktree,
)
from test_worktree import _ensure_hive

GIB = 1024**3


def _meminfo(path: Path, *, total: int, available: int) -> None:
    path.write_text(
        f"MemTotal:       {total // 1024} kB\n"
        f"MemFree:        {available // 1024} kB\n"
        f"MemAvailable:   {available // 1024} kB\n"
        "HugePages_Total:       0\n"
    )


@pytest.fixture
def fake_meminfo(tmp_path, monkeypatch):
    path = tmp_path / "meminfo"
    _meminfo(path, total=48 * GIB, available=24 * GIB)
    monkeypatch.setattr(validation_memory, "MEMINFO_PATH", path)
    monkeypatch.delenv("BH_VALIDATION_MEMORY", raising=False)
    monkeypatch.delenv("BH_VALIDATION_MEMORY_FLOOR", raising=False)
    return path


class _Clock:
    """A fake monotonic clock whose sleep advances time and runs a per-sleep hook."""

    def __init__(self, on_sleep=None):
        self.now = 100.0
        self.sleeps: list[float] = []
        self.on_sleep = on_sleep

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(len(self.sleeps))


# --- sizes and settings -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("50%", 24 * GIB),
        ("4G", 4 * GIB),
        ("512m", 512 * 1024**2),
        ("1048576", 1024**2),
        (1024, 1024),
        ("0", 0),
        ("none", None),
        ("infinity", None),
        ("", None),
    ],
)
def test_parse_size_resolves_systemd_style_sizes(value, expected):
    assert validation_memory.parse_size(value, 48 * GIB) == expected


@pytest.mark.parametrize("value", ["lots", "-1G", "150%", "0%", True, "1.5Q"])
def test_parse_size_rejects_malformed_sizes(value):
    with pytest.raises(ValueError):
        validation_memory.parse_size(value, 48 * GIB)


def test_percentage_without_known_total_is_unbounded_not_guessed():
    assert validation_memory.parse_size("50%", None) is None


def test_settings_defaults_overrides_and_env(monkeypatch):
    monkeypatch.delenv("BH_VALIDATION_MEMORY", raising=False)
    monkeypatch.delenv("BH_VALIDATION_MEMORY_FLOOR", raising=False)
    assert validation_memory.settings({}) == validation_memory.MemorySettings()
    configured = validation_memory.settings(
        {"work": {"validation_memory": {"memory_max": "8G", "admission_floor": "2G"}}}
    )
    assert (configured.memory_max, configured.admission_floor) == ("8G", "2G")

    monkeypatch.setenv("BH_VALIDATION_MEMORY", "false")
    monkeypatch.setenv("BH_VALIDATION_MEMORY_FLOOR", "0")
    overridden = validation_memory.settings({})
    assert overridden.enabled is False
    assert overridden.admission_floor == "0"

    monkeypatch.setenv("BH_VALIDATION_MEMORY", "sometimes")
    with pytest.raises(ValueError, match="BH_VALIDATION_MEMORY must be a boolean"):
        validation_memory.settings({})
    monkeypatch.delenv("BH_VALIDATION_MEMORY")
    with pytest.raises(ValueError, match="unknown work.validation_memory"):
        validation_memory.settings({"work": {"validation_memory": {"memory_low": "1G"}}})
    with pytest.raises(ValueError):
        validation_memory.settings({"work": {"validation_memory": {"memory_max": "lots"}}})


def test_config_contract_defaults_and_rejects_unsafe_bounds():
    work = config_schema.WorkConfig.model_validate({})
    assert work.validation_memory.memory_max == "60%"
    assert work.validation_memory.memory_high == "50%"
    assert work.validation_memory.memory_swap_max == "0"
    assert work.validation_memory.admission_floor == "10%"
    for bad in (
        {"memory_max": "0"},
        {"memory_high": "0G"},
        {"memory_max": "150%"},
        {"memory_max": "lots"},
        {"admission_floor": "-1"},
        {"admission_timeout_seconds": -1},
        {"admission_poll_seconds": 0},
        {"memory_low": "1G"},
    ):
        with pytest.raises(ValueError):
            config_schema.WorkConfig.model_validate({"validation_memory": bad})
    opted_out = config_schema.WorkConfig.model_validate(
        {"validation_memory": {"memory_max": "none", "memory_swap_max": "none", "enabled": False}}
    )
    assert opted_out.validation_memory.enabled is False


# --- admission floor (faked /proc/meminfo) -------------------------------------------------------


def test_floor_admits_immediately_when_memory_is_available(fake_meminfo):
    clock = _Clock()
    record = validation_memory.wait_for_floor(
        validation_memory.MemorySettings(), sleep=clock.sleep, clock=clock
    )
    assert record["waited"] is False
    assert record["wait_seconds"] == 0.0
    assert record["floor_bytes"] == int(48 * GIB * 0.10)
    assert record["available_bytes"] == 24 * GIB
    assert clock.sleeps == []


def test_floor_waits_without_starting_and_records_the_wait(fake_meminfo):
    lines = []

    def recover(count):
        if count == 3:
            _meminfo(fake_meminfo, total=48 * GIB, available=8 * GIB)

    _meminfo(fake_meminfo, total=48 * GIB, available=1 * GIB)
    clock = _Clock(recover)
    record = validation_memory.wait_for_floor(
        validation_memory.MemorySettings(admission_poll_seconds=5.0),
        echo=lines.append,
        sleep=clock.sleep,
        clock=clock,
    )
    assert clock.sleeps == [5.0, 5.0, 5.0]
    assert record["waited"] is True
    assert record["wait_seconds"] == 15.0
    assert record["available_bytes"] == 8 * GIB
    assert any("waiting for memory" in line for line in lines)
    assert any("reached" in line for line in lines)


def test_floor_times_out_with_a_record_instead_of_starting(fake_meminfo):
    _meminfo(fake_meminfo, total=48 * GIB, available=1 * GIB)
    clock = _Clock()
    with pytest.raises(validation_memory.MemoryAdmissionTimeout) as caught:
        validation_memory.wait_for_floor(
            validation_memory.MemorySettings(
                admission_timeout_seconds=20.0, admission_poll_seconds=5.0
            ),
            echo=lambda _line: None,
            sleep=clock.sleep,
            clock=clock,
        )
    assert caught.value.record["timed_out"] is True
    assert caught.value.record["wait_seconds"] >= 20.0
    assert "not started" in str(caught.value)


@pytest.mark.parametrize(
    "policy",
    [
        validation_memory.MemorySettings(enabled=False),
        validation_memory.MemorySettings(admission_floor="0"),
        validation_memory.MemorySettings(admission_floor="none"),
    ],
)
def test_floor_is_opt_out_able(fake_meminfo, policy):
    _meminfo(fake_meminfo, total=48 * GIB, available=1)
    clock = _Clock()
    record = validation_memory.wait_for_floor(policy, sleep=clock.sleep, clock=clock)
    assert record["waited"] is False
    assert clock.sleeps == []


def test_floor_degrades_without_meminfo(tmp_path, monkeypatch):
    monkeypatch.setattr(validation_memory, "MEMINFO_PATH", tmp_path / "absent")
    record = validation_memory.wait_for_floor(
        validation_memory.MemorySettings(admission_floor="1G")
    )
    assert record["waited"] is False
    assert "not enforced" in record["note"]


def test_host_slot_waits_for_floor_and_records_it_on_the_permit(
    tmp_path, monkeypatch, fake_meminfo
):
    _meminfo(fake_meminfo, total=48 * GIB, available=1 * GIB)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        _meminfo(fake_meminfo, total=48 * GIB, available=10 * GIB)

    original = validation_memory.wait_for_floor
    monkeypatch.setattr(
        validation_memory,
        "wait_for_floor",
        lambda policy, **kw: original(policy, **{**kw, "sleep": fake_sleep}),
    )
    monkeypatch.delenv("BH_VALIDATION_SLOTS", raising=False)
    cfg = {"work": {"validation_slots": 1, "validation_memory": {"admission_poll_seconds": 2}}}
    with validation_admission.host_slot(cfg, root=tmp_path / "slots") as permit:
        assert permit.slot == 0
        assert permit.memory["waited"] is True
        assert permit.memory["available_bytes"] == 10 * GIB
    assert sleeps == [2.0]


def test_host_slot_floor_timeout_refuses_and_releases_the_slot(tmp_path, monkeypatch, fake_meminfo):
    _meminfo(fake_meminfo, total=48 * GIB, available=1 * GIB)
    clock = _Clock()
    original = validation_memory.wait_for_floor
    monkeypatch.setattr(
        validation_memory,
        "wait_for_floor",
        lambda policy, **kw: original(policy, **{**kw, "sleep": clock.sleep, "clock": clock}),
    )
    monkeypatch.delenv("BH_VALIDATION_SLOTS", raising=False)
    cfg = {
        "work": {
            "validation_slots": 1,
            "validation_memory": {"admission_timeout_seconds": 10, "admission_poll_seconds": 5},
        }
    }
    root = tmp_path / "slots"
    with pytest.raises(typer.Exit) as caught:
        with validation_admission.host_slot(cfg, root=root):
            pytest.fail("a run below the memory floor must not start")
    assert caught.value.exit_code == 75
    # The slot was released: the next admission (floor disabled) gets it immediately.
    monkeypatch.setenv("BH_VALIDATION_MEMORY_FLOOR", "0")
    with validation_admission.host_slot(cfg, root=root) as permit:
        assert permit.slot == 0
        assert permit.queue_seconds < 5


def test_host_slot_with_admission_disabled_still_applies_the_floor(tmp_path, fake_meminfo):
    with validation_admission.host_slot(
        {"work": {"validation_slots": 0}}, root=tmp_path / "slots"
    ) as permit:
        assert permit.slot == -1
        assert permit.memory["floor_bytes"] == int(48 * GIB * 0.10)


def test_run_manifest_records_admission_memory_wait(tmp_path, monkeypatch):
    from beadhive import host

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setattr(host, "host_id", lambda: "host")
    wait = {"waited": True, "wait_seconds": 12.5, "floor_bytes": 4 * GIB}
    run = validation_records.begin_run(
        repo,
        bead="bh-x",
        phase="submit",
        branch="b",
        worktree=repo,
        sha="sha",
        tree="tree",
        command_hash="hash",
        admission={"slot": 0, "queue_seconds": 0.5, "memory": wait},
        memory={"applied": True, "peak_bytes": None},
    )
    assert run["admission"]["memory"] == wait
    validation_records.attach_memory(repo, run["run_id"], {"applied": True, "peak_bytes": 4096})
    assert validation_records.read_run(repo, run["run_id"])["memory"]["peak_bytes"] == 4096


# --- launcher -----------------------------------------------------------------------------------


def _fake_host(monkeypatch, tmp_path, *, probe_rc=0, controller=True):
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    (runtime / "bus").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.delenv("BH_VALIDATION_PRIORITY", raising=False)
    monkeypatch.setattr(
        validation_admission.shutil,
        "which",
        lambda name: (
            f"/usr/bin/{name}" if name in {"nice", "ionice", "systemd-run", "true"} else None
        ),
    )
    monkeypatch.setattr(validation_admission, "_current_nice", lambda: 0)
    monkeypatch.setattr(validation_admission, "_priority_prefix_available", lambda _prefix: True)
    monkeypatch.setattr(validation_memory, "memory_controller_available", lambda: controller)
    probes = []

    def fake_probe(cmd, **_kwargs):
        probes.append(cmd)
        memory = any(arg.startswith("--property=Memory") for arg in cmd)
        rc = probe_rc if memory else 0
        return subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(validation_admission.subprocess, "run", fake_probe)
    return probes


def test_guarded_command_bounds_memory_in_the_weighted_scope(monkeypatch, tmp_path, fake_meminfo):
    _fake_host(monkeypatch, tmp_path)
    report = tmp_path / "memory.json"

    argv, priority, memory = validation_admission.guarded_command({}, ["gate"], report_path=report)

    total = 48 * GIB
    assert argv[:10] == [
        "/usr/bin/systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--property=CPUWeight=20",
        "--property=IOWeight=20",
        f"--property=MemoryHigh={int(total * 0.5)}",
        f"--property=MemoryMax={int(total * 0.6)}",
        "--property=MemorySwapMax=0",
        "--",
    ]
    assert argv[-8:-1] == [
        sys.executable,
        "-I",
        str(validation_memory.EXEC_SCRIPT),
        "--report",
        str(report),
        "--scoped",
        "--",
    ]
    assert argv[-1] == "gate"
    assert priority["mechanism"] == "systemd-user-scope+nice+ionice"
    assert memory["applied"] is True
    assert memory["mechanism"] == "systemd-user-scope"
    assert memory["memory_max_bytes"] == int(total * 0.6)
    assert memory["peak_bytes"] is None


def test_guarded_command_degrades_with_a_note_when_memory_cannot_be_bounded(
    monkeypatch, tmp_path, fake_meminfo
):
    probes = _fake_host(monkeypatch, tmp_path, controller=False)
    argv, priority, memory = validation_admission.guarded_command({}, ["gate"])
    assert not any("Memory" in arg for arg in argv)
    assert memory["applied"] is False
    assert "memory controller" in memory["note"]
    assert priority["mechanism"] == "systemd-user-scope+nice+ionice"
    assert argv[-1] == "gate"
    assert len(probes) == 1

    probes = _fake_host(monkeypatch, tmp_path, probe_rc=1)
    argv, priority, memory = validation_admission.guarded_command({}, ["gate"])
    assert not any("Memory" in arg for arg in argv)
    assert "--property=CPUWeight=20" in argv  # the weights alone still apply
    assert memory["applied"] is False
    assert "memory bounds skipped" in memory["note"]


def test_guarded_command_honours_memory_and_priority_opt_outs(monkeypatch, tmp_path, fake_meminfo):
    _fake_host(monkeypatch, tmp_path)
    argv, priority, memory = validation_admission.guarded_command(
        {"work": {"validation_memory": {"enabled": False}}}, ["gate"]
    )
    assert memory["mechanism"] == "disabled"
    assert not any("Memory" in arg for arg in argv)

    argv, priority, memory = validation_admission.guarded_command(
        {"work": {"validation_priority": {"enabled": False}}}, ["gate"]
    )
    assert priority["mechanism"] == "disabled"
    assert "--property=CPUWeight=20" not in argv
    assert "/usr/bin/nice" not in argv
    assert any(arg.startswith("--property=MemoryMax=") for arg in argv)

    argv, priority, memory = validation_admission.guarded_command(
        {"work": {"validation_memory": {"memory_high": "none", "memory_max": "2G"}}}, ["gate"]
    )
    assert f"--property=MemoryMax={2 * GIB}" in argv
    assert not any(arg.startswith("--property=MemoryHigh=") for arg in argv)


def test_priority_command_keeps_its_memory_free_scope(monkeypatch, tmp_path, fake_meminfo):
    probes = _fake_host(monkeypatch, tmp_path)
    argv, _policy = validation_admission.priority_command({}, ["gate"])
    assert not any("Memory" in arg for arg in argv)
    assert not any("Memory" in arg for probe in probes for arg in probe)


# --- exec wrapper -------------------------------------------------------------------------------


def _exec(report: Path, *command: str, scoped: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*validation_memory.exec_prefix(report, scoped=scoped), *command],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_exec_wrapper_reports_peak_and_preserves_exit_status(tmp_path):
    report = tmp_path / "memory.json"
    result = _exec(report, sys.executable, "-c", "import sys; sys.exit(3)")
    assert result.returncode == 3
    data = json.loads(report.read_text())
    assert data["returncode"] == 3
    assert data["peak_source"] == "rusage.children.maxrss"
    assert data["peak_bytes"] > 0
    assert data["oom_kills"] is None


def test_exec_wrapper_reproduces_a_signal_death(tmp_path):
    report = tmp_path / "memory.json"
    result = _exec(report, sys.executable, "-c", "import os, signal; os.kill(os.getpid(), 15)")
    assert result.returncode == -signal.SIGTERM
    assert json.loads(report.read_text())["returncode"] == -signal.SIGTERM


def test_exec_wrapper_maps_a_missing_command_to_127(tmp_path):
    result = _exec(tmp_path / "memory.json", "definitely-not-a-beadhive-binary")
    assert result.returncode == 127


def test_finalize_and_outcome_classify_oom_kills():
    planned = {"memory_max_bytes": 96 * 1024**2, "peak_bytes": None, "oom_kills": None}
    merged = validation_memory.finalize(
        planned, {"peak_bytes": 100663296, "peak_source": "cgroup.memory.peak", "oom_kills": 1}
    )
    assert validation_memory.oom_killed(merged)
    assert "OOM-killed 1 process" in validation_memory.outcome_line(merged)
    clean = validation_memory.finalize(planned, {"peak_bytes": 1, "oom_kills": 0})
    assert not validation_memory.oom_killed(clean)
    assert validation_memory.outcome_line(clean) is None
    assert validation_memory.finalize(planned, None)["peak_bytes"] is None


def test_adhoc_cli_runs_guarded_and_reports_peak(tmp_path):
    env = {**os.environ, "BH_VALIDATION_MEMORY_FLOOR": "0", "BH_HOME": str(tmp_path / "home")}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "beadhive.validation_memory_cli",
            "--json",
            "--",
            sys.executable,
            "-c",
            "import sys; sys.exit(4)",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=120,
    )
    assert result.returncode == 4, result.stderr
    record = json.loads(result.stdout.strip().splitlines()[-1])
    assert record["peak_bytes"] > 0
    assert "memory guard" in result.stderr


# --- real bounded scope: a tiny memory hog ------------------------------------------------------

# Refuses to allocate unless its own cgroup is bounded at <= 128 MiB, then requests at most
# 32 x 16 MiB = 512 MiB, so it is OOM-killed long before that inside its scope.
_HOG = """
import pathlib, sys
cgroup = [l[3:] for l in open('/proc/self/cgroup').read().splitlines() if l.startswith('0::')]
limit = pathlib.Path('/sys/fs/cgroup' + cgroup[0], 'memory.max').read_text().strip()
if limit == 'max' or int(limit) > 128 * 1024 * 1024:
    sys.exit(3)
chunks = []
for _ in range(32):
    chunks.append(b'\\x01' * (16 * 1024 * 1024))
print('hog survived', flush=True)
"""

_HOG_MEMORY = {"memory_high": "none", "memory_max": "96M", "memory_swap_max": "0"}


@pytest.fixture
def _minted_host():
    from beadhive import host

    host.mint_if_needed()


def _require_bounded_user_scope() -> None:
    if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
        pytest.skip("cgroup v2 is unavailable")
    systemd_run = shutil.which("systemd-run")
    true_executable = shutil.which("true")
    if not systemd_run or not true_executable:
        pytest.skip("systemd-run is unavailable")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not (
        (runtime and (Path(runtime) / "bus").exists())
        or os.environ.get("DBUS_SESSION_BUS_ADDRESS", "").startswith("unix:path=")
    ):
        pytest.skip("no user systemd manager bus in this environment")
    if not validation_memory.memory_controller_available():
        pytest.skip("the user manager does not delegate the memory controller")
    probe = subprocess.run(
        [
            systemd_run,
            "--user",
            "--scope",
            "--quiet",
            "--property=MemoryMax=96M",
            "--property=MemorySwapMax=0",
            "--",
            true_executable,
        ],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if probe.returncode != 0:
        pytest.skip("a memory-bounded user systemd scope is unavailable")


def test_memory_hog_is_oom_killed_inside_its_scope_and_reported_red(
    tmp_path, monkeypatch, _minted_host
):
    _require_bounded_user_scope()
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    cfg["work"] = {"validation_memory": _HOG_MEMORY}
    command = shlex.join([sys.executable, "-c", _HOG])

    rc = worktree.clean_checkout(entry, "main", command, cfg=cfg, bead="bh-x", phase="submit")

    assert rc == 137
    run = json.loads(next((repo / ".git/bh/validation/runs").glob("*/manifest.json")).read_text())
    assert (run["lifecycle"], run["verdict"], run["reason"]) == (
        "completed",
        "red",
        "memory_limit",
    )
    memory = run["memory"]
    assert memory["applied"] is True
    assert memory["memory_max_bytes"] == 96 * 1024**2
    assert memory["oom_kills"] >= 1
    assert memory["peak_source"] == "cgroup.memory.peak"
    assert 0 < memory["peak_bytes"] <= 96 * 1024**2
    # The host stayed responsive: this process (outside the scope) still runs children.
    assert subprocess.run(["true"], check=False, timeout=10).returncode == 0


def test_bounded_green_run_records_its_scope_peak(tmp_path, monkeypatch, _minted_host):
    _require_bounded_user_scope()
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    cfg["work"] = {"validation_memory": _HOG_MEMORY}

    assert worktree.clean_checkout(entry, "main", "true", cfg=cfg, bead="bh-x") == 0

    run = json.loads(next((repo / ".git/bh/validation/runs").glob("*/manifest.json")).read_text())
    assert run["verdict"] == "green"
    assert run["memory"]["oom_kills"] == 0
    assert run["memory"]["peak_source"] == "cgroup.memory.peak"
    assert run["memory"]["peak_bytes"] > 0
