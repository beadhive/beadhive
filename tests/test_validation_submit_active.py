from __future__ import annotations

from types import SimpleNamespace

import pytest

from beadhive import work_submission


class Exit(Exception):
    def __init__(self, code):
        self.exit_code = code


def _api(active):
    messages = []
    return SimpleNamespace(
        registry=SimpleNamespace(hive_dir=lambda entry: "/hive"),
        worktree=SimpleNamespace(
            _branch_sha=lambda *a: "sha", _pid_alive=lambda pid: True, _pid_start=lambda pid: "s"
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


def test_submit_refuses_live_conflicting_command_without_checkout():
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
    api.validation_records.abandon_run = lambda *a, **k: abandoned.append((a, k))
    api.worktree.clean_checkout = lambda *a, **k: 0
    api.time = SimpleNamespace(perf_counter=lambda: 0)
    api.otel = SimpleNamespace(
        record_validation_duration=lambda *a: None, count_validation=lambda *a: None
    )
    api._vres = lambda rc: "pass"
    api._hive = lambda entry: "hive"
    work_submission.impl__validate_submit_checkout(api, {}, "branch", {}, bead="bh-x")
    assert abandoned and abandoned[0][0][-1] == "run-dead"
    assert any("run-dead" in message and "abandoned" in message for message in api.messages)
