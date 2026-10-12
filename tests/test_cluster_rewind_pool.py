"""Pool policy of the rewindable cluster fixture (bh-i970d), on a fake target: no bd or Dolt.

Build once per composition, restore between uses, park on release, rebuild on the opt-out flag,
after a destructive test and after a failed leak guard.
"""

from __future__ import annotations

import contextlib

import pytest

from harness import cluster_rewind
from harness.cluster_rewind import ClusterPool, RewindLeak


class FakeTarget:
    def __init__(self, key):
        self.key = key
        self.snapshots = self.restores = self.stops = 0
        self.closed = False
        self.state = "boot"

    def snapshot(self):
        self.snapshots += 1

    def restore(self):
        self.restores += 1
        self.state = "boot"
        return {"stop": 0.0, "rm": 0.0, "copy": 0.0, "start": 0.0, "total": 0.0}

    def stop_processes(self, *, discard=False):
        assert discard  # parking: the next restore replaces every file
        self.stops += 1


@pytest.fixture
def fake(monkeypatch, tmp_path):
    built: list[FakeTarget] = []

    @contextlib.contextmanager
    def builder(key, root):
        target = FakeTarget(key)
        built.append(target)
        try:
            yield target
        finally:
            target.closed = True

    monkeypatch.setattr(cluster_rewind, "fingerprint", lambda target: {"state": target.state})

    def make(**kwargs):
        return ClusterPool(tmp_path, builder, **kwargs), built

    return make


def test_one_build_per_composition_restored_between_uses(fake):
    pool, built = fake(rewind=True, guard=False)
    first = pool.acquire("x")
    first.state = "dirty"
    pool.release("x")
    assert first.stops == 1  # parked: no server outlives the test that used it
    assert pool.acquire("x") is first and first.restores == 1 and first.state == "boot"
    pool.release("x")
    other = pool.acquire("y")
    assert other is not first and len(built) == 2 and pool.builds == 2
    assert len(pool.restores("x")) == 1
    pool.close()
    assert first.closed and other.closed


def test_a_clean_target_is_handed_out_again_without_a_restore(fake):
    pool, _ = fake(rewind=True, guard=False)
    target = pool.acquire("x")
    assert pool.acquire("x") is target and target.restores == 0


def test_opt_out_flag_builds_fresh_for_every_acquisition(fake, monkeypatch):
    monkeypatch.setenv("BH_CLUSTER_REWIND", "0")
    pool, built = fake()
    assert not pool.rewind
    for _ in range(3):
        with pool.use("x") as target:
            assert target.snapshots == 0  # no template is taken when rewinding is off
    assert len(built) == 3 and all(t.closed for t in built)


def test_a_destructive_test_makes_the_next_acquisition_rebuild(fake):
    pool, built = fake(rewind=True, guard=False)
    with pool.use("x", destructive=True) as first:
        pass
    assert first.closed
    assert pool.acquire("x") is not first and len(built) == 2


def test_a_failed_guard_fails_loudly_and_rebuilds(fake):
    pool, built = fake(rewind=True, guard=True)
    first = pool.acquire("x")
    pool.release("x")
    first.restore = lambda: setattr(first, "state", "leaked") or {"total": 0.0}
    with pytest.raises(RewindLeak, match="/state"):
        pool.acquire("x")
    assert first.closed
    assert pool.acquire("x") is not first and len(built) == 2


def test_guard_is_opt_in_by_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("BH_CLUSTER_REWIND_GUARD", raising=False)
    assert not ClusterPool(tmp_path, None).guard
    monkeypatch.setenv("BH_CLUSTER_REWIND_GUARD", "1")
    assert ClusterPool(tmp_path, None).guard


def test_a_live_process_under_the_root_is_still_a_stray(tmp_path):
    """The exiting-process skip (empty argv, bh-u5mag) must not hide a real leak."""
    import subprocess
    import sys

    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], cwd=tmp_path, stdin=subprocess.PIPE
    )
    try:
        assert cluster_rewind.processes_print(tmp_path, {}) == ["stray:" + child_name(child)]
        assert cluster_rewind.processes_print(tmp_path, {child.pid: "server:a"}) == ["server:a"]
    finally:
        child.communicate()


def child_name(child) -> str:
    import os
    import sys

    return os.path.basename(sys.executable)
