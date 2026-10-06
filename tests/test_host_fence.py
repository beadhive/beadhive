"""``refs/bh/epoch`` — the fence record and managed reservations.

Production ``BdEngine`` cannot join its fence ref to bd's transient data ref. Its current
contract is tested here: managed reserve-before-bd plus exact postflight verification. A stale
host loses before data is attempted; a takeover in the non-atomic CAS->push window may land data
before postflight detects it, and raw OS-level bd bypasses the boundary.

(The legacy ``fenced_push`` family and its tests were deleted in bh-vwbxy; the transport-repo
lookup tests live in ``tests/test_transport_locator.py``.)

Two hosts are modeled as two working clones of one bare "hive remote", all under the test's
own ``tmp_path``: local paths only, never a network remote, never the operator's HQ.
"""

from __future__ import annotations

import subprocess

import pytest

from beadhive import engine, gitref, guard, host_fence

HOST_A = "aaaaaaaa-1111-4111-8111-111111111111"
HOST_B = "bbbbbbbb-2222-4222-8222-222222222222"


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _clone(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    _git(["init", "-q"], path)
    _git(["config", "user.email", "t@example.invalid"], path)
    _git(["config", "user.name", "t"], path)
    _git(["commit", "-q", "--allow-empty", "-m", "init"], path)
    return path


@pytest.fixture
def hive_remote(tmp_path):
    """The hive's own remote — where BOTH refs/dolt/data and refs/bh/epoch live."""
    path = tmp_path / "hive.git"
    _git(["init", "--bare", "-q", str(path)], tmp_path)
    return str(path)


@pytest.fixture
def host_a(tmp_path):
    return _clone(tmp_path, "host-a")


@pytest.fixture
def host_b(tmp_path):
    return _clone(tmp_path, "host-b")


# ---- the fence ref itself ---------------------------------------------------------


def test_the_fence_lives_outside_the_dolt_data_ref():
    """It must never be reachable by a Dolt merge — a sibling ref, not a row in the DB."""
    assert host_fence.EPOCH_REF == "refs/bh/epoch"
    assert not host_fence.EPOCH_REF.startswith("refs/dolt/")
    assert not host_fence.DATA_REF.startswith(host_fence.EPOCH_REF)
    assert not host_fence.EPOCH_REF.startswith(host_fence.DATA_REF)


def test_the_default_data_ref_matches_the_engines_state_channel(tmp_path):
    """Keeps the fallback default honest against the Engine seam rather than a stale copy."""
    assert host_fence.DATA_REF == engine.BdEngine().state_channel(tmp_path)


def test_installing_a_fence_writes_the_documented_record(hive_remote, host_a):
    host_fence.install_fence(
        hive_remote,
        host_fence.EpochFence(epoch=3, host_id=HOST_A),
        expected=gitref.ABSENT,
        cwd=host_a,
    )
    _sha, fence = host_fence.read_fence(hive_remote, cwd=host_a)
    assert (fence.epoch, fence.host_id, fence.seq) == (3, HOST_A, 0)


def test_installing_a_fence_over_a_moved_ref_is_rejected(hive_remote, host_a, host_b):
    held = host_fence.install_fence(
        hive_remote,
        host_fence.EpochFence(epoch=1, host_id=HOST_A),
        expected=gitref.ABSENT,
        cwd=host_a,
    )
    host_fence.install_fence(
        hive_remote,
        host_fence.EpochFence(epoch=2, host_id=HOST_B),
        expected=held,
        cwd=host_b,
    )
    with pytest.raises(host_fence.FenceRejected):
        host_fence.install_fence(
            hive_remote,
            host_fence.EpochFence(epoch=2, host_id=HOST_A),
            expected=held,
            cwd=host_a,
        )


# ---- the production bd boundary: sequenced reservation + postflight -------------------


class _Lease:
    def __init__(self, epoch, holder=HOST_A, live=True):
        self.epoch = epoch
        self.holder = holder
        self.live = live

    def held_by(self, host_id):
        return self.live and self.holder == host_id


def _primary(monkeypatch, *, epoch=1, holder=HOST_A, this_host=HOST_A, live=True):
    monkeypatch.setattr(
        guard,
        "primary_state",
        lambda **_kw: ("tt", this_host, _Lease(epoch, holder=holder, live=live)),
    )


def test_managed_reservation_is_absent_only_before_multi_host_adoption(monkeypatch, host_a):
    monkeypatch.setattr(guard, "primary_state", lambda **_kw: None)
    monkeypatch.setattr(
        host_fence,
        "read_fence",
        lambda *_a, **_kw: pytest.fail("single-host path must not touch a remote fence"),
    )
    assert host_fence.reserve_managed_push("origin", cwd=host_a, cfg={}) is None


def test_managed_reservation_bumps_remote_ticket_and_postflight_accepts_exact_ticket(
    monkeypatch, hive_remote, host_a
):
    _primary(monkeypatch)
    initial = host_fence.install_fence(
        hive_remote,
        host_fence.EpochFence(epoch=1, host_id=HOST_A),
        expected=gitref.ABSENT,
        cwd=host_a,
    )

    reservation = host_fence.reserve_managed_push(hive_remote, cwd=host_a, cfg={})

    assert reservation is not None
    assert reservation.held != initial
    assert reservation.fence == host_fence.EpochFence(epoch=1, host_id=HOST_A, seq=1)
    host_fence.verify_managed_push(hive_remote, cwd=host_a, reservation=reservation)


@pytest.mark.parametrize(
    ("epoch", "holder", "this_host", "live"),
    [(1, HOST_B, HOST_A, True), (1, HOST_A, HOST_A, False)],
)
def test_non_holder_or_expired_lease_is_rejected_before_remote_access(
    monkeypatch, host_a, epoch, holder, this_host, live
):
    _primary(monkeypatch, epoch=epoch, holder=holder, this_host=this_host, live=live)
    monkeypatch.setattr(
        host_fence,
        "read_fence",
        lambda *_a, **_kw: pytest.fail("invalid local lease grants no remote attempt"),
    )
    with pytest.raises(host_fence.FenceRejected, match="before data transfer"):
        host_fence.reserve_managed_push("origin", cwd=host_a, cfg={})


def test_forged_local_fence_never_grants_authority(monkeypatch, hive_remote, host_a, host_b):
    """The remote record is authoritative: a matching local ref cannot mask a takeover."""
    _primary(monkeypatch, epoch=1)
    old = host_fence.install_fence(
        hive_remote,
        host_fence.EpochFence(epoch=1, host_id=HOST_A),
        expected=gitref.ABSENT,
        cwd=host_a,
    )
    host_fence.install_fence(
        hive_remote,
        host_fence.EpochFence(epoch=2, host_id=HOST_B),
        expected=old,
        cwd=host_b,
    )
    forged = gitref.write_object(
        host_fence.EpochFence(epoch=1, host_id=HOST_A, seq=99).to_record(), cwd=host_a
    )
    gitref.set_local(host_fence.EPOCH_REF, forged, cwd=host_a)

    with pytest.raises(host_fence.FenceRejected, match="remote epoch fence"):
        host_fence.reserve_managed_push(hive_remote, cwd=host_a, cfg={})


def test_managed_reservation_losing_the_cas_never_returns_a_ticket(
    monkeypatch, hive_remote, host_a
):
    _primary(monkeypatch)
    host_fence.install_fence(
        hive_remote,
        host_fence.EpochFence(epoch=1, host_id=HOST_A),
        expected=gitref.ABSENT,
        cwd=host_a,
    )
    monkeypatch.setattr(
        gitref,
        "cas",
        lambda *_a, **_kw: gitref.CasResult(
            ok=False,
            ref=host_fence.EPOCH_REF,
            sha="losing-ticket",
            detail="! [rejected] stale info",
        ),
    )

    with pytest.raises(host_fence.FenceRejected, match="not attempted and no data landed"):
        host_fence.reserve_managed_push(hive_remote, cwd=host_a, cfg={})


def test_postflight_detects_takeover_and_never_claims_no_data_landed(
    monkeypatch, hive_remote, host_a, host_b
):
    _primary(monkeypatch)
    host_fence.install_fence(
        hive_remote,
        host_fence.EpochFence(epoch=1, host_id=HOST_A),
        expected=gitref.ABSENT,
        cwd=host_a,
    )
    reservation = host_fence.reserve_managed_push(hive_remote, cwd=host_a, cfg={})
    assert reservation is not None
    host_fence.install_fence(
        hive_remote,
        host_fence.EpochFence(epoch=2, host_id=HOST_B),
        expected=reservation.held,
        cwd=host_b,
    )

    with pytest.raises(host_fence.FenceViolation, match="DATA MAY HAVE LANDED"):
        host_fence.verify_managed_push(hive_remote, cwd=host_a, reservation=reservation)
