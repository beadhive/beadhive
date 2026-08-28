from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from beadhive import work_submission, worktree_verify


class Exit(Exception):
    def __init__(self, code):
        self.exit_code = code


def _api(active):
    messages = []
    return SimpleNamespace(
        registry=SimpleNamespace(hive_dir=lambda entry: "/hive"),
        worktree=SimpleNamespace(
            _branch_sha=lambda *a: "sha",
            _pid_alive=lambda pid: True,
            _pid_start=lambda pid: "s",
            _pid_state=lambda pid: "S",
        ),
        validation_ledger=SimpleNamespace(tree_of=lambda *a: "tree", cmd_hash=lambda c: c),
        validation_records=SimpleNamespace(
            running_runs=lambda *a, **k: active, abandon_run=lambda *a, **k: None
        ),
        config=SimpleNamespace(validate_cmd=lambda *a: "new"),
        host=SimpleNamespace(host_id=lambda: "host"),
        typer=SimpleNamespace(
            echo=lambda message, *a, **k: messages.append(str(message)), Exit=Exit
        ),
        RETRYABLE_VALIDATION_EXIT=75,
        messages=messages,
    )


@pytest.mark.parametrize("owner_state", ["S", ""])
def test_submit_refuses_live_or_unprobeable_conflicting_command_without_checkout(owner_state):
    active = [
        {
            "run_id": "run-1",
            "bead": "bh-x",
            "tree": "tree",
            "phase": "submit",
            "command_hash": "old",
            "owner": {"host": "host", "pid": 7, "start_token": "s"},
        }
    ]
    api = _api(active)
    api.worktree._pid_state = lambda pid: owner_state
    with pytest.raises(Exit) as caught:
        work_submission.impl__validate_submit_checkout(api, {}, "branch", {}, bead="bh-x")
    assert caught.value.exit_code == 75
    assert any("run run-1 owned by pid 7" in message for message in api.messages)
    assert any("conflicts with active" in message for message in api.messages)


def test_submit_reaps_dead_owner_before_replacement(monkeypatch):
    active = [
        {
            "run_id": "run-dead",
            "bead": "bh-x",
            "tree": "tree",
            "phase": "submit",
            "command_hash": "new",
            "owner": {"host": "host", "pid": 7, "start_token": "s"},
        }
    ]
    api = _api(active)
    api.worktree._pid_alive = lambda pid: False
    abandoned = []
    checkout_kwargs = []
    api.validation_records.abandon_run = lambda *a, **k: abandoned.append((a, k))
    api.worktree.clean_checkout = lambda *a, **k: checkout_kwargs.append(k) or 0
    api.time = SimpleNamespace(perf_counter=lambda: 0)
    api.otel = SimpleNamespace(
        record_validation_duration=lambda *a: None, count_validation=lambda *a: None
    )
    api._vres = lambda rc: "pass"
    api._hive = lambda entry: "hive"
    work_submission.impl__validate_submit_checkout(api, {}, "branch", {}, bead="bh-x")
    assert abandoned and abandoned[0][0][-1] == "run-dead"
    assert checkout_kwargs[0]["observed_active_run_id"] is None
    assert any("run-dead" in message and "abandoned" in message for message in api.messages)


def test_submit_reaps_zombie_owner_before_replacement():
    active = [
        {
            "run_id": "run-zombie",
            "bead": "bh-x",
            "tree": "tree",
            "phase": "submit",
            "command_hash": "new",
            "owner": {"host": "host", "pid": 7, "start_token": "s"},
        }
    ]
    api = _api(active)
    api.worktree._pid_state = lambda pid: "Z"
    abandoned = []
    checkout_kwargs = []
    api.validation_records.abandon_run = lambda *a, **k: abandoned.append((a, k))
    api.worktree.clean_checkout = lambda *a, **k: checkout_kwargs.append(k) or 0
    api.time = SimpleNamespace(perf_counter=lambda: 0)
    api.otel = SimpleNamespace(
        record_validation_duration=lambda *a: None, count_validation=lambda *a: None
    )
    api._vres = lambda rc: "pass"
    api._hive = lambda entry: "hive"

    work_submission.impl__validate_submit_checkout(api, {}, "branch", {}, bead="bh-x")

    assert abandoned and abandoned[0][0][-1] == "run-zombie"
    assert checkout_kwargs[0]["observed_active_run_id"] is None
    assert any("run-zombie" in message and "abandoned" in message for message in api.messages)


def test_active_completion_before_checkout_snapshot_stays_coalesced(tmp_path, monkeypatch, capsys):
    """Force the finish-gate race in its exact order.

    Submit first observes an active exact blocker. The clean-checkout callback then completes
    that leader *before* the lower layer takes its prior snapshot, so snapshot-only inference
    would call this ordinary historical reuse. The carried run identity must retain coalesced
    attribution without launching a replacement.
    """
    active = {
        "run_id": "run-raced",
        "bead": "bh-x",
        "tree": "tree",
        "phase": "submit",
        "command_hash": "new",
        "lifecycle": "running",
        "owner": {"host": "host", "pid": 7, "start_token": "s"},
    }
    api = _api([active])
    api.registry.hive_dir = lambda entry: tmp_path
    uses = []

    monkeypatch.setattr(worktree_verify, "_branch_sha", lambda *_: "sha")
    monkeypatch.setattr(worktree_verify.registry, "hive_dir", lambda *_: tmp_path)
    monkeypatch.setattr(worktree_verify.validation_ledger, "tree_of", lambda *a: "tree")
    monkeypatch.setattr(worktree_verify.validation_ledger, "cmd_hash", lambda command: command)
    monkeypatch.setattr(
        worktree_verify.validation_records,
        "latest_run",
        lambda *a, **k: dict(active),
    )
    monkeypatch.setattr(
        worktree_verify.validation_records,
        "read_run",
        lambda _main, run_id: dict(active) if run_id == active["run_id"] else None,
    )
    monkeypatch.setattr(
        worktree_verify.validation_records,
        "record_use",
        lambda *a, **k: uses.append(k),
    )
    monkeypatch.setattr(worktree_verify, "_reuse_verdict_hit", lambda *a, **k: True)
    monkeypatch.setattr(
        worktree_verify.validation_admission,
        "identity_lock",
        lambda *a, **k: contextlib.nullcontext(),
    )
    monkeypatch.setattr(
        worktree_verify.validation_admission,
        "host_slot",
        lambda *a, **k: pytest.fail("a completed observed blocker must not consume a new slot"),
    )
    monkeypatch.setattr(
        worktree_verify,
        "_impl_clean_checkout_unadmitted",
        lambda *a, **k: pytest.fail("a completed observed blocker must not launch a child"),
    )

    def complete_then_snapshot(*args, **kwargs):
        assert kwargs["observed_active_run_id"] == "run-raced"
        active.update(lifecycle="completed", verdict="green", exit_code=0)
        return worktree_verify.impl_clean_checkout(*args, cfg={}, **kwargs)

    api.worktree.clean_checkout = complete_then_snapshot
    api.time = SimpleNamespace(perf_counter=lambda: 0)
    api.otel = SimpleNamespace(
        record_validation_duration=lambda *a: None, count_validation=lambda *a: None
    )
    api._vres = lambda rc: "pass"
    api._hive = lambda entry: "hive"

    work_submission.impl__validate_submit_checkout(api, {}, "branch", {}, bead="bh-x")

    assert uses == [
        {
            "run_id": "run-raced",
            "bead": "bh-x",
            "phase": "submit",
            "branch": "branch",
            "worktree": None,
            "sha": "sha",
            "tree": "tree",
            "command_hash": "new",
            "reused": True,
            "coalesced": True,
        }
    ]
    assert any("validation already active" in message for message in api.messages)
    assert "coalesced with blocker run run-raced" in capsys.readouterr().out
