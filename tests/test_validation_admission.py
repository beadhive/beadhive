from __future__ import annotations

import multiprocessing as mp
import time

import pytest

from beadhive import validation_admission, worktree_verify


def _hold(root, entered, release):
    with validation_admission.host_slot({}, root=root):
        entered.set()
        release.wait(5)


def test_host_slot_contends_across_processes_and_releases(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_VALIDATION_SLOTS", "1")
    entered, release = mp.Event(), mp.Event()
    child = mp.Process(target=_hold, args=(tmp_path, entered, release))
    child.start()
    assert entered.wait(3)
    second = mp.Event()
    waiter = mp.Process(target=_hold, args=(tmp_path, second, release))
    waiter.start()
    time.sleep(.15)
    assert not second.is_set()
    release.set()
    assert second.wait(3)
    child.join(3)
    waiter.join(3)
    assert child.exitcode == waiter.exitcode == 0


def test_slot_owner_death_releases_kernel_permit(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_VALIDATION_SLOTS", "1")
    entered, release = mp.Event(), mp.Event()
    child = mp.Process(target=_hold, args=(tmp_path, entered, release))
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


def test_clean_checkout_reuse_hit_bypasses_admission(monkeypatch):
    monkeypatch.setattr(worktree_verify, "_branch_sha", lambda *_: "a" * 40)
    monkeypatch.setattr(worktree_verify, "_reuse_verdict_hit", lambda *a, **k: True)
    monkeypatch.setattr(
        validation_admission,
        "host_slot",
        lambda *a, **k: pytest.fail("a ledger hit must not enter admission"),
    )
    assert worktree_verify.impl_clean_checkout({}, "main", "true", cfg={}, reuse=True) == 0
