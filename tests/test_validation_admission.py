from __future__ import annotations

import json
import os
import time

import pytest

from beadhive import validation_admission, worktree_verify
from harness.processes import process_context


def _hold(root, entered, release):
    with validation_admission.host_slot({}, root=root):
        entered.set()
        release.wait(5)


def _exact_caller_worker(
    state_path,
    hive_root,
    arrivals,
    starts,
    uses,
    running,
    release,
    results,
    verdict,
    exit_code,
):
    def latest(*_args, **_kwargs):
        return json.loads(state_path.read_text()) if state_path.exists() else None

    def reuse(*_args, **_kwargs):
        value = latest()
        return bool(
            value and value.get("lifecycle") == "completed" and value.get("verdict") == "green"
        )

    def execute(*_args, **_kwargs):
        with starts.get_lock():
            starts.value += 1
        state_path.write_text(json.dumps({"run_id": "leader", "lifecycle": "running"}))
        running.set()
        release.wait(5)
        state_path.write_text(
            json.dumps(
                {
                    "run_id": "leader",
                    "lifecycle": "completed",
                    "verdict": verdict,
                    "exit_code": exit_code,
                }
            )
        )
        return exit_code

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
    with arrivals.get_lock():
        arrivals.value += 1
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
        starts,
        uses,
        running,
        release,
        results,
        verdict,
        exit_code,
    )
    leader = ctx.Process(target=_exact_caller_worker, args=worker_args)
    leader.start()
    assert running.wait(3)
    followers = [ctx.Process(target=_exact_caller_worker, args=worker_args) for _ in range(4)]
    for follower in followers:
        follower.start()
    deadline = time.monotonic() + 3
    while arrivals.value != 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert arrivals.value == 5
    time.sleep(0.1)
    release.set()
    for process in [leader, *followers]:
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
