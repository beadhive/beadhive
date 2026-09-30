from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from beadhive import config_schema, validation_admission, work_submission, worktree_verify
from harness.processes import process_context


def test_priority_command_defaults_on_and_applies_nice_ionice_to_child(monkeypatch):
    monkeypatch.delenv("BH_VALIDATION_PRIORITY", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(
        validation_admission.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"nice", "ionice"} else None,
    )

    argv, policy = validation_admission.priority_command(
        {},
        [
            sys.executable,
            "-c",
            "import os; print(os.getpriority(os.PRIO_PROCESS, 0))",
        ],
    )
    result = subprocess.run(argv, capture_output=True, text=True, check=True)

    assert result.stdout.strip() == "10"
    assert policy["enabled"] is True
    assert policy["applied"] is True
    assert policy["mechanism"] == "nice+ionice"


def test_priority_command_applies_configured_nice_and_ionice_to_real_child(monkeypatch):
    nice = shutil.which("nice")
    ionice = shutil.which("ionice")
    if not nice or not ionice:
        pytest.skip("nice and ionice executables are required for the inheritance proof")
    monkeypatch.delenv("BH_VALIDATION_PRIORITY", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)

    script = (
        "import json, os, subprocess; "
        "io=subprocess.run(['ionice','-p',str(os.getpid())],"
        "capture_output=True,text=True,check=True); "
        "print(json.dumps({'nice':os.getpriority(os.PRIO_PROCESS,0),'ionice':io.stdout.strip()}))"
    )
    argv, policy = validation_admission.priority_command(
        {
            "work": {
                "validation_priority": {
                    "nice": 13,
                    "ionice_class": 2,
                    "ionice_priority": 6,
                }
            }
        },
        [sys.executable, "-c", script],
    )
    payload = json.loads(subprocess.run(argv, capture_output=True, text=True, check=True).stdout)

    assert payload["nice"] == 13
    assert payload["ionice"] == "best-effort: prio 6"
    assert policy["nice"] == 13
    assert policy["ionice_class"] == 2
    assert policy["ionice_priority"] == 6


def test_priority_config_rejects_priority_raising_or_invalid_values(monkeypatch):
    for value in (-1, 20):
        with pytest.raises(ValueError):
            config_schema.WorkConfig.model_validate({"validation_priority": {"nice": value}})
    with pytest.raises(ValueError):
        config_schema.WorkConfig.model_validate({"validation_priority": {"ionice_class": 1}})
    for value in (-1, 8):
        with pytest.raises(ValueError):
            config_schema.WorkConfig.model_validate(
                {"validation_priority": {"ionice_priority": value}}
            )

    monkeypatch.setenv("BH_VALIDATION_PRIORITY", "sometimes")
    with pytest.raises(ValueError, match="BH_VALIDATION_PRIORITY must be a boolean"):
        validation_admission.priority_command({}, ["gate"])


def test_priority_command_supports_emergency_disable_and_graceful_degrade(monkeypatch, tmp_path):
    command = ["gate"]
    monkeypatch.setattr(validation_admission.shutil, "which", lambda _name: None)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)

    monkeypatch.setenv("BH_VALIDATION_PRIORITY", "false")
    disabled_argv, disabled = validation_admission.priority_command({}, command)
    assert disabled_argv == command
    assert disabled["mechanism"] == "disabled"
    assert disabled["applied"] is False

    monkeypatch.delenv("BH_VALIDATION_PRIORITY")
    configured_argv, configured = validation_admission.priority_command(
        {"work": {"validation_priority": {"enabled": False}}}, command
    )
    assert configured_argv == command
    assert configured["mechanism"] == "disabled"

    unavailable_argv, unavailable = validation_admission.priority_command({}, command)
    assert unavailable_argv == command
    assert unavailable["mechanism"] == "unavailable"
    assert unavailable["enabled"] is True

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "bus").touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(
        validation_admission.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"nice", "ionice", "systemd-run"} else None,
    )
    monkeypatch.setattr(
        validation_admission.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1),
    )
    fallback_argv, fallback = validation_admission.priority_command({}, command)
    assert fallback_argv[-1:] == command
    assert fallback["mechanism"] == "nice+ionice"


def test_priority_command_uses_safe_configured_idle_class(monkeypatch):
    monkeypatch.delenv("BH_VALIDATION_PRIORITY", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(
        validation_admission.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"nice", "ionice"} else None,
    )

    argv, policy = validation_admission.priority_command(
        {
            "work": {
                "validation_priority": {
                    "enabled": True,
                    "nice": 4,
                    "ionice_class": 3,
                    "ionice_priority": 1,
                }
            }
        },
        ["gate"],
    )

    assert argv == ["/usr/bin/ionice", "-c3", "/usr/bin/nice", "-n", "4", "gate"]
    assert policy["nice"] == 4
    assert policy["ionice_class"] == 3
    assert policy["ionice_priority"] == 1


def test_priority_command_adds_user_systemd_scope_when_bus_is_available(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
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
    monkeypatch.setattr(
        validation_admission.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
    )

    argv, policy = validation_admission.priority_command({}, ["gate"])

    assert argv[:7] == [
        "/usr/bin/systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--property=CPUWeight=20",
        "--property=IOWeight=20",
        "--",
    ]
    assert policy["mechanism"] == "systemd-user-scope+nice+ionice"


def test_priority_command_real_systemd_scope_has_reduced_cgroup_weight(monkeypatch):
    if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
        pytest.skip("cgroup v2 is unavailable")
    if not shutil.which("systemd-run"):
        pytest.skip("systemd-run is unavailable")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not ((runtime and (Path(runtime) / "bus").exists()) or bus.startswith("unix:path=")):
        pytest.skip("user systemd bus is unavailable")
    monkeypatch.delenv("BH_VALIDATION_PRIORITY", raising=False)

    script = """
import json
import os
import subprocess
from pathlib import Path

cgroup = next(line.split(':', 2)[2] for line in Path('/proc/self/cgroup').read_text().splitlines()
              if line.startswith('0::'))
root = Path('/sys/fs/cgroup') / cgroup.lstrip('/')
io = subprocess.run(['ionice', '-p', str(os.getpid())], capture_output=True, text=True, check=True)
print(json.dumps({
    'cgroup': cgroup,
    'cpu_weight': (root / 'cpu.weight').read_text().strip(),
    'io_weight': (
        (root / 'io.weight').read_text().strip() if (root / 'io.weight').exists() else None
    ),
    'nice': os.getpriority(os.PRIO_PROCESS, 0),
    'ionice': io.stdout.strip(),
}))
"""
    argv, policy = validation_admission.priority_command({}, [sys.executable, "-c", script])
    if "systemd-user-scope" not in policy["mechanism"]:
        pytest.skip("user systemd scope rejected the reduced-weight probe")
    payload = json.loads(subprocess.run(argv, capture_output=True, text=True, check=True).stdout)

    assert ".scope" in payload["cgroup"]
    assert payload["cpu_weight"] == "20"
    if payload["io_weight"] is not None:
        assert payload["io_weight"] in {"20", "default 20"}
    assert payload["nice"] == 10
    assert payload["ionice"] == "best-effort: prio 7"


def _hold(root, entered, release):
    with validation_admission.host_slot({}, root=root):
        entered.set()
        release.wait(5)


def _parallel_check_worker(slot_root, hive_root, cohort, results):
    """Run the selective-validation shape with real host and identity flocks."""
    from pathlib import Path
    from types import SimpleNamespace

    os.environ["BH_VALIDATION_SLOT_ROOT"] = str(slot_root)
    cfg = {"work": {"validation_slots": 2}}
    entry = {"prefix": "proof"}
    target = Path(hive_root)
    target.mkdir(parents=True, exist_ok=True)
    worktree_verify._branch_sha = lambda *_args: "a" * 40
    worktree_verify.registry.hive_dir = lambda *_args: target
    worktree_verify.validation_ledger.tree_of = lambda *_args: "tree"
    worktree_verify._impl_clean_checkout_unadmitted = lambda *args, **kwargs: 0

    def run_selective(*_args, runner, **_kwargs):
        cohort.wait(5)
        return runner("true")

    api = SimpleNamespace(
        config=SimpleNamespace(
            load=lambda: cfg,
            validate_cmd=lambda *_args: "true",
            integration_branch=lambda *_args: "main",
        ),
        worktree=SimpleNamespace(
            locate=lambda *_args: (entry, target, target, "wt/bead/issue/proof"),
            in_bead_worktree=lambda *_args: True,
            head_full_sha=lambda *_args: "a" * 40,
            clean_checkout=worktree_verify.impl_clean_checkout,
            integration_base=lambda *_args: "main",
        ),
        validation_admission=validation_admission,
        validation_ledger=worktree_verify.validation_ledger,
        selective_validation=SimpleNamespace(configured=lambda *_args: True, run=run_selective),
        otel=SimpleNamespace(set_bead=lambda *_args: None),
        _batch_worktree=lambda *_args: (None, None),
        _checked_sha=lambda *_args: "a" * 40,
        typer=SimpleNamespace(echo=lambda *_args, **_kwargs: None),
    )
    results.put(work_submission.impl_check(api, "proof", None))


def _exact_caller_worker(
    state_path,
    hive_root,
    arrivals,
    cohort_ready,
    starts,
    uses,
    running,
    release,
    results,
    verdict,
    exit_code,
):
    prior_observed = False

    def latest(*_args, **_kwargs):
        nonlocal prior_observed
        value = json.loads(state_path.read_text()) if state_path.exists() else None
        if not prior_observed:
            prior_observed = True
            with arrivals.get_lock():
                arrivals.value += 1
                if arrivals.value == 5:
                    cohort_ready.set()
        return value

    def reuse(*_args, **_kwargs):
        value = latest()
        return bool(
            value and value.get("lifecycle") == "completed" and value.get("verdict") == "green"
        )

    def execute(*_args, **_kwargs):
        with starts.get_lock():
            starts.value += 1
        write_state({"run_id": "leader", "lifecycle": "running"})
        running.set()
        release.wait(5)
        write_state(
            {
                "run_id": "leader",
                "lifecycle": "completed",
                "verdict": verdict,
                "exit_code": exit_code,
            }
        )
        return exit_code

    def write_state(value):
        staged = state_path.with_name(f"{state_path.name}.{os.getpid()}.tmp")
        staged.write_text(json.dumps(value))
        os.replace(staged, state_path)

    def record_use(*_args, **_kwargs):
        with uses.get_lock():
            uses.value += 1

    worktree_verify._branch_sha = lambda *_args: "a" * 40
    worktree_verify.registry.hive_dir = lambda *_args: hive_root
    worktree_verify.validation_ledger.tree_of = lambda *_args: "tree"
    worktree_verify.validation_ledger.cmd_hash = lambda *_args: "command"
    worktree_verify.validation_records.latest_run = latest
    worktree_verify.validation_records.record_use = record_use
    worktree_verify._reuse_verdict_hit = reuse
    worktree_verify._impl_clean_checkout_unadmitted = execute
    results.put(worktree_verify.impl_clean_checkout({}, "main", "true", cfg={}, reuse=True))


def _abandoned_caller_worker(state_path, hive_root, starts):
    def latest(*_args, **_kwargs):
        return json.loads(state_path.read_text())

    def read_run(_main, run_id):
        if run_id != "dead":
            return None
        return {
            "run_id": "dead",
            "tree": "tree",
            "command_hash": "command",
            "lifecycle": "abandoned",
            "verdict": "none",
        }

    def execute(*_args, **_kwargs):
        with starts.get_lock():
            starts.value += 1
        staged = state_path.with_name(f"run-{os.getpid()}.tmp")
        staged.write_text(
            json.dumps(
                {
                    "run_id": "replacement",
                    "lifecycle": "completed",
                    "verdict": "green",
                    "exit_code": 0,
                }
            )
        )
        os.replace(staged, state_path)
        return 0

    worktree_verify._branch_sha = lambda *_args: "a" * 40
    worktree_verify.registry.hive_dir = lambda *_args: hive_root
    worktree_verify.validation_ledger.tree_of = lambda *_args: "tree"
    worktree_verify.validation_ledger.cmd_hash = lambda *_args: "command"
    worktree_verify.validation_records.latest_run = latest
    worktree_verify.validation_records.read_run = read_run
    worktree_verify._reuse_verdict_hit = lambda *_args, **_kwargs: (
        json.loads(state_path.read_text()).get("verdict") == "green"
    )
    worktree_verify._impl_clean_checkout_unadmitted = execute
    worktree_verify.impl_clean_checkout(
        {},
        "main",
        "true",
        cfg={},
        reuse=True,
        observed_active_run_id="dead",
    )


def test_host_slot_contends_across_processes_and_releases(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_VALIDATION_SLOTS", "1")
    ctx = process_context()
    entered, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_hold, args=(tmp_path, entered, release))
    child.start()
    assert entered.wait(3)
    second = ctx.Event()
    waiter = ctx.Process(target=_hold, args=(tmp_path, second, release))
    waiter.start()
    time.sleep(0.15)
    assert not second.is_set()
    release.set()
    assert second.wait(3)
    child.join(3)
    waiter.join(3)
    assert child.exitcode == waiter.exitcode == 0


def test_slot_owner_death_releases_kernel_permit(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_VALIDATION_SLOTS", "1")
    ctx = process_context()
    entered, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_hold, args=(tmp_path, entered, release))
    child.start()
    assert entered.wait(3)
    child.kill()
    child.join(3)
    with validation_admission.host_slot({}, root=tmp_path) as permit:
        assert permit.slot == 0


@pytest.mark.parametrize("value", ["-1", "wat"])
def test_invalid_environment_capacity_is_rejected(monkeypatch, value):
    monkeypatch.setenv("BH_VALIDATION_SLOTS", value)
    with pytest.raises(ValueError, match="non-negative integer"):
        validation_admission.configured_slots({})


def test_host_capacity_ignores_per_hive_overrides(monkeypatch):
    monkeypatch.delenv("BH_VALIDATION_SLOTS", raising=False)
    cfg = {"work": {"validation_slots": 3}}
    hive_a = {"work": {"validation_slots": 1}}
    hive_b = {"work": {"validation_slots": 9}}
    assert validation_admission.configured_slots(cfg, hive_a) == 3
    assert validation_admission.configured_slots(cfg, hive_b) == 3


def test_admission_emits_bounded_telemetry_and_visible_execution(tmp_path, monkeypatch, capsys):
    waits = []
    admitted = []
    monkeypatch.setattr(
        validation_admission.otel,
        "record_validation_queue_wait",
        lambda seconds, attrs: waits.append((seconds, attrs)),
    )
    monkeypatch.setattr(
        validation_admission.otel,
        "count_validation_admitted",
        lambda attrs: admitted.append(attrs),
    )
    monkeypatch.setenv("BH_VALIDATION_SLOTS", "1")
    entry = {"prefix": "mr", "path": "/sensitive/hive"}

    with validation_admission.host_slot({}, entry, phase="submit", root=tmp_path):
        pass

    assert waits[0][0] >= 0
    assert waits[0][1] == {"bh.hive": "mr", "bh.work.phase": "submit"}
    assert admitted == [{"bh.hive": "mr", "bh.work.phase": "submit"}]
    output = capsys.readouterr().out
    assert "admitted" in output and "executing" in output
    assert "/sensitive/hive" not in output


def test_clean_checkout_reuse_hit_bypasses_admission(monkeypatch):
    monkeypatch.setattr(worktree_verify, "_branch_sha", lambda *_: "a" * 40)
    monkeypatch.setattr(worktree_verify, "_reuse_verdict_hit", lambda *a, **k: True)
    monkeypatch.setattr(worktree_verify.registry, "hive_dir", lambda *_: "/hive")
    monkeypatch.setattr(worktree_verify.validation_ledger, "tree_of", lambda *a: "tree")
    monkeypatch.setattr(worktree_verify.validation_records, "latest_run", lambda *a, **k: None)
    monkeypatch.setattr(
        validation_admission,
        "identity_lock",
        lambda *a, **k: validation_admission.contextlib.nullcontext(),
    )
    monkeypatch.setattr(
        validation_admission,
        "host_slot",
        lambda *a, **k: pytest.fail("a ledger hit must not enter admission"),
    )
    assert worktree_verify.impl_clean_checkout({}, "main", "true", cfg={}, reuse=True) == 0


def test_reusable_gate_takes_identity_before_host(monkeypatch, tmp_path):
    order = []

    @validation_admission.contextlib.contextmanager
    def identity(*args, **kwargs):
        order.append("identity-enter")
        yield
        order.append("identity-exit")

    @validation_admission.contextlib.contextmanager
    def host(*args, **kwargs):
        order.append("host-enter")
        yield
        order.append("host-exit")

    monkeypatch.setattr(worktree_verify, "_branch_sha", lambda *_: "a" * 40)
    monkeypatch.setattr(worktree_verify, "_reuse_verdict_hit", lambda *a, **k: False)
    monkeypatch.setattr(worktree_verify.registry, "hive_dir", lambda *_: tmp_path)
    monkeypatch.setattr(worktree_verify.validation_ledger, "tree_of", lambda *a: "tree")
    monkeypatch.setattr(worktree_verify.validation_records, "latest_run", lambda *a, **k: None)
    monkeypatch.setattr(validation_admission, "identity_lock", identity)
    monkeypatch.setattr(validation_admission, "host_slot", host)
    monkeypatch.setattr(worktree_verify, "_impl_clean_checkout_unadmitted", lambda *a, **k: 0)
    assert worktree_verify.impl_clean_checkout({}, "main", "true", cfg={}, reuse=True) == 0
    assert order == ["identity-enter", "host-enter", "host-exit", "identity-exit"]


def test_clean_checkout_under_parent_permit_keeps_identity_without_second_slot(
    monkeypatch, tmp_path
):
    order = []
    parent = validation_admission.Permit(slot=1, queue_seconds=0.25)

    @validation_admission.contextlib.contextmanager
    def identity(*args, **kwargs):
        order.append("identity-enter")
        yield
        order.append("identity-exit")

    monkeypatch.setattr(worktree_verify, "_branch_sha", lambda *_: "a" * 40)
    monkeypatch.setattr(worktree_verify.registry, "hive_dir", lambda *_: tmp_path)
    monkeypatch.setattr(worktree_verify.validation_ledger, "tree_of", lambda *a: "tree")
    monkeypatch.setattr(validation_admission, "identity_lock", identity)
    monkeypatch.setattr(
        validation_admission,
        "host_slot",
        lambda *a, **k: pytest.fail("a nested validation must inherit its parent's permit"),
    )
    monkeypatch.setattr(
        worktree_verify,
        "_impl_clean_checkout_unadmitted",
        lambda *a, **k: order.append(("run", k["permit"])) or 0,
    )

    assert (
        worktree_verify.impl_clean_checkout({}, "main", "true", cfg={}, reuse=False, permit=parent)
        == 0
    )
    assert order == ["identity-enter", ("run", parent), "identity-exit"]


def test_two_parallel_selective_checks_share_their_outer_permits(tmp_path, monkeypatch):
    """Both checks hold the two host slots before their key runners enter clean checkout."""
    monkeypatch.setenv("BH_VALIDATION_SLOT_ROOT", str(tmp_path / "slots"))
    ctx = process_context()
    cohort = ctx.Barrier(2)
    results = ctx.Queue()
    workers = [
        ctx.Process(
            target=_parallel_check_worker,
            args=(tmp_path / "slots", tmp_path / "hive", cohort, results),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
    try:
        assert all(not worker.is_alive() for worker in workers), (
            "parallel checks deadlocked while nested clean checkouts waited for second host slots"
        )
        assert [worker.exitcode for worker in workers] == [0, 0]
        assert [results.get(timeout=1) for _ in workers] == [None, None]
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.kill()
                worker.join(3)


@pytest.mark.parametrize(
    ("verdict", "exit_code", "expected"),
    [("green", 0, 0), ("red", 3, 3), ("none", 75, 75)],
)
def test_exact_callers_start_one_child_and_share_terminal_result(
    tmp_path, monkeypatch, verdict, exit_code, expected
):
    """Real processes contend on the production flocks; only the leader enters the child seam."""
    ctx = process_context()
    state = tmp_path / "run.json"
    arrivals = ctx.Value("i", 0)
    cohort_ready = ctx.Event()
    starts = ctx.Value("i", 0)
    uses = ctx.Value("i", 0)
    running = ctx.Event()
    release = ctx.Event()
    results = ctx.Queue()
    monkeypatch.setenv("BH_VALIDATION_SLOT_ROOT", str(tmp_path / "locks"))
    monkeypatch.setenv("BH_VALIDATION_SLOTS", "2")
    worker_args = (
        state,
        tmp_path,
        arrivals,
        cohort_ready,
        starts,
        uses,
        running,
        release,
        results,
        verdict,
        exit_code,
    )
    callers = [ctx.Process(target=_exact_caller_worker, args=worker_args) for _ in range(5)]
    with validation_admission.identity_lock(tmp_path, "tree", "command"):
        for caller in callers:
            caller.start()
        assert cohort_ready.wait(5)
        assert arrivals.value == 5
    assert running.wait(3)
    release.set()
    for process in callers:
        process.join(5)
        assert process.exitcode == 0
    assert starts.value == 1
    assert sorted(results.get(timeout=1) for _ in range(5)) == [expected] * 5
    if verdict != "green":
        assert uses.value == 4


def test_abandoned_identity_allows_one_replacement_execution(tmp_path, monkeypatch):
    """An abandoned owner is not a terminal cohort result; one waiter becomes the new leader."""
    state = tmp_path / "run.json"

    def write_state(value):
        # Mirror validation_records' atomic manifest replacement: a concurrent reader must never
        # observe the empty truncate/write window that plain Path.write_text creates.
        staged = state.with_name(f"run-{os.getpid()}.tmp")
        staged.write_text(json.dumps(value))
        os.replace(staged, state)

    write_state({"run_id": "dead", "lifecycle": "abandoned", "verdict": "none"})
    ctx = process_context()
    starts = ctx.Value("i", 0)
    monkeypatch.setenv("BH_VALIDATION_SLOT_ROOT", str(tmp_path / "locks"))
    processes = [
        ctx.Process(target=_abandoned_caller_worker, args=(state, tmp_path, starts))
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(5)
        assert process.exitcode == 0
    assert starts.value == 1


@pytest.mark.parametrize("verdict,rc", [("green", 0), ("red", 3), ("none", 75)])
def test_coalesced_cli_names_blocker_owner_and_typed_outcome(capsys, verdict, rc):
    worktree_verify._echo_coalesced_outcome(
        {
            "run_id": "run-blocker",
            "verdict": verdict,
            "owner": {"host": "host-a", "pid": 42, "start_token": "token-a"},
        },
        rc,
    )
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "coalesced with blocker run run-blocker" in output
    assert "host host-a, pid 42, start token-a" in output
    assert verdict in output
