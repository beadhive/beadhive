"""Two-phase fail-closed adopt — fence first, lease second (bh-ytbb.8).

The acceptance bar wants the crash window *verified*, not argued, so the injected-failure
test here is a genuine mid-sequence failure and not a mocked shortcut:

  * phase 1 (the epoch-fence CAS) runs for REAL — a real `git push --force-with-lease` to a
    real scratch bare remote, leaving a real ref behind;
  * the injection makes phase 2 (`host_lease.adopt`) raise, exactly as a crashed/killed
    process or an unreachable HQ would;
  * the assertions then read BOTH scratch remotes back with real git;
  * and recovery re-runs the REAL, un-patched `adopt` against the half-state that was
    actually left on those remotes.

Both remotes are bare repos under the test's own `tmp_path`; nothing here can resolve a
network remote or the operator's `~/.beadhive/hq`.
"""

from __future__ import annotations

import subprocess

import pytest

from beadhive import host_adopt, host_fence, host_lease

PREFIX = "bh"
HOST_A = "aaaaaaaa-1111-4111-8111-111111111111"
HOST_B = "bbbbbbbb-2222-4222-8222-222222222222"
T0 = 1_800_000_000.0


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


def _bare(tmp_path, name):
    path = tmp_path / name
    _git(["init", "--bare", "-q", str(path)], tmp_path)
    return str(path)


def _clone(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    _git(["init", "-q"], path)
    _git(["config", "user.email", "t@example.invalid"], path)
    _git(["config", "user.name", "t"], path)
    _git(["commit", "-q", "--allow-empty", "-m", "init"], path)
    return path


@pytest.fixture
def world(tmp_path):
    """Two remotes (the hive's own + HQ) and two local clones for one host."""
    return {
        "hive_remote": _bare(tmp_path, "hive.git"),
        "hq_remote": _bare(tmp_path, "hq.git"),
        "hive_cwd": _clone(tmp_path, "hive"),
        "hq_cwd": _clone(tmp_path, "hq"),
    }


def _adopt(world, host_id=HOST_A, label="laptop", at=T0, **kw):
    return host_adopt.adopt(
        prefix=PREFIX,
        hive_remote=world["hive_remote"],
        hq_remote=world["hq_remote"],
        hive_cwd=world["hive_cwd"],
        hq_cwd=world["hq_cwd"],
        host_id=host_id,
        label=label,
        at=at,
        **kw,
    )


def _fence(world):
    _sha, fence = host_fence.read_fence(world["hive_remote"], cwd=world["hive_cwd"])
    return fence


def _lease(world):
    return host_lease.read(world["hq_remote"], PREFIX, cwd=world["hq_cwd"])


# ---- the happy path --------------------------------------------------------------


def test_adopt_sets_the_fence_and_records_the_lease_at_the_same_epoch(world):
    outcome = _adopt(world)
    assert outcome.epoch == 1
    assert _fence(world).epoch == 1
    assert _lease(world).epoch == 1
    assert _lease(world).host_id == HOST_A


def test_adopt_writes_the_fence_before_the_lease(world, monkeypatch):
    """Ordering is the safety property, so it is asserted directly rather than inferred."""
    order: list[str] = []
    real_fence, real_lease = host_fence.install_fence, host_lease.adopt

    def spy_fence(*a, **kw):
        order.append("fence")
        return real_fence(*a, **kw)

    def spy_lease(*a, **kw):
        order.append("lease")
        return real_lease(*a, **kw)

    monkeypatch.setattr(host_adopt.host_fence, "install_fence", spy_fence)
    monkeypatch.setattr(host_adopt.host_lease, "adopt", spy_lease)
    _adopt(world)
    assert order == ["fence", "lease"]


def test_a_live_foreign_lease_is_refused_before_either_remote_is_touched(world):
    _adopt(world, host_id=HOST_A, ttl=600.0)
    with pytest.raises(host_lease.HostLeaseRejected):
        host_adopt.adopt(
            prefix=PREFIX,
            hive_remote=world["hive_remote"],
            hq_remote=world["hq_remote"],
            hive_cwd=world["hive_cwd"],
            hq_cwd=world["hq_cwd"],
            host_id=HOST_B,
            label="desk",
            at=T0 + 1,
        )
    # the refusal left NO half-state: the fence is untouched at A's epoch
    assert _fence(world).epoch == 1 and _fence(world).host_id == HOST_A


def test_the_second_host_loses_the_fence_when_it_races(world, tmp_path):
    """Two hosts adopting concurrently: the fence CAS is the linearization point, so the
    loser is stopped in phase 1 and never reaches HQ at all."""
    _adopt(world, host_id=HOST_A, ttl=600.0)
    b_hive = _clone(tmp_path, "hive-b")
    b_hq = _clone(tmp_path, "hq-b")
    stale_sha, _f = host_fence.read_fence(world["hive_remote"], cwd=b_hive)
    _adopt(world, host_id=HOST_A, ttl=600.0)  # A re-adopts, moving the fence under B

    with pytest.raises(host_fence.FenceRejected):
        host_fence.install_fence(
            world["hive_remote"],
            host_fence.EpochFence(epoch=99, host_id=HOST_B),
            expected=stale_sha,
            cwd=b_hive,
        )
    assert host_lease.read(world["hq_remote"], PREFIX, cwd=b_hq).host_id == HOST_A


# ---- the crash between the phases -------------------------------------------------


def _crash_after_fence(monkeypatch, message="simulated crash between the two phases"):
    """Inject a REAL failure into phase 2. Phase 1 has already run for real by the time this
    fires, so the state the assertions read back is the state a crashed process leaves."""

    def boom(*_a, **_kw):
        raise RuntimeError(message)

    monkeypatch.setattr(host_adopt.host_lease, "adopt", boom)


def test_a_crash_between_the_phases_leaves_the_fence_set_and_the_lease_unrecorded(
    world, monkeypatch
):
    _crash_after_fence(monkeypatch)
    with pytest.raises(host_adopt.AdoptHalfDone) as excinfo:
        _adopt(world)

    assert "fail-closed" in str(excinfo.value)
    assert _fence(world) is not None, "phase 1 must have really landed"
    assert _fence(world).epoch == 1
    assert _lease(world) is None, "phase 2 must NOT have landed"


def test_the_half_state_means_nobody_may_write(world, monkeypatch, tmp_path):
    """The point of the ordering: fence held, lease absent.

    - the crashed host is not primary — HQ names nobody, so `guard_primary` refuses it;
    - every other host's fence CAS now expects a superseded value, so it cannot write either.
    """
    _crash_after_fence(monkeypatch)
    with pytest.raises(host_adopt.AdoptHalfDone):
        _adopt(world)
    monkeypatch.undo()

    assert _lease(world) is None  # nobody is the recorded primary

    b_hive = _clone(tmp_path, "hive-b")
    with pytest.raises(host_fence.FenceRejected):
        host_fence.install_fence(  # B still holds its pre-crash view: the empty fence
            world["hive_remote"],
            host_fence.EpochFence(epoch=1, host_id=HOST_B),
            expected="",
            cwd=b_hive,
        )


def test_the_half_state_is_recovered_by_simply_re_adopting(world, monkeypatch):
    """Starting from exactly the half-state, an ordinary adopt completes cleanly — no
    ref surgery, no flags, no manual reconciliation."""
    _crash_after_fence(monkeypatch)
    with pytest.raises(host_adopt.AdoptHalfDone):
        _adopt(world)
    monkeypatch.undo()  # the crash is over; the REAL code path runs from here

    assert (_fence(world).epoch, _lease(world)) == (1, None)  # the half-state, verified

    outcome = _adopt(world)

    assert outcome.epoch == 2  # past BOTH objects' generations, never a reused number
    assert _fence(world).epoch == 2
    assert _lease(world).epoch == 2
    assert _lease(world).host_id == HOST_A


def test_recovery_works_for_a_different_host_too(world, monkeypatch, tmp_path):
    """The half-state is not owned by whoever crashed — any host may adopt out of it (the
    lease names nobody), which is what keeps a dead laptop from stranding the hive."""
    _crash_after_fence(monkeypatch)
    with pytest.raises(host_adopt.AdoptHalfDone):
        _adopt(world, host_id=HOST_A)
    monkeypatch.undo()

    other = {
        **world,
        "hive_cwd": _clone(tmp_path, "hive-b"),
        "hq_cwd": _clone(tmp_path, "hq-b"),
    }
    outcome = _adopt(other, host_id=HOST_B, label="desk", at=T0 + 5)
    assert outcome.epoch == 2 and outcome.lease.host_id == HOST_B


def test_recovery_never_reuses_the_orphaned_epoch(world, monkeypatch):
    """The orphaned fence's generation must not be minted twice — otherwise a token from the
    crashed attempt would be indistinguishable from a fresh one."""
    _crash_after_fence(monkeypatch)
    with pytest.raises(host_adopt.AdoptHalfDone):
        _adopt(world)
    monkeypatch.undo()
    orphaned = _fence(world).epoch
    assert _adopt(world).epoch > orphaned


def test_a_phase_two_failure_never_rolls_the_fence_back(world, monkeypatch):
    """Rolling back would hand the write right to a host this adopt already superseded."""
    _adopt(world, host_id=HOST_A, ttl=600.0)  # epoch 1 established
    before, _f = host_fence.read_fence(world["hive_remote"], cwd=world["hive_cwd"])
    _crash_after_fence(monkeypatch)
    with pytest.raises(host_adopt.AdoptHalfDone):
        _adopt(world, host_id=HOST_A, at=T0 + 1)
    after, fence = host_fence.read_fence(world["hive_remote"], cwd=world["hive_cwd"])
    assert after != before and fence.epoch == 2  # advanced, and left advanced


# ---- the rejected reverse ordering is documented in the code -------------------------


def test_the_unsafe_reverse_ordering_is_documented_in_the_adopt_docstring():
    """The AC asks for the reason to live where a future editor will see it — in the function
    itself, not only in a PR description. Pinned so a docstring rewrite can't quietly drop it."""
    doc = host_adopt.adopt.__doc__ or ""
    assert "DO NOT" in doc
    assert "lease → fence (rejected)" in doc
    assert "split-brain" in doc
    assert "4796" in doc


def test_the_epoch_is_one_past_whichever_object_is_furthest_ahead():
    """Unit-level statement of the recovery invariant `_next_epoch` encodes."""
    fence = host_fence.EpochFence(epoch=7, host_id=HOST_A)
    lease = host_lease.HostLease(HOST_A, "a", 5, "t", "t")
    assert host_adopt._next_epoch(fence, lease) == 8
    assert host_adopt._next_epoch(None, lease) == 6
    assert host_adopt._next_epoch(fence, None) == 8
    assert host_adopt._next_epoch(None, None) == 1
