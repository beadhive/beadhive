"""``host_adopt.adopt`` on a cut-over hive: placement first, ref in lockstep, data step 2
(bh-4c7p4). HQ placement (``refs/bh/lease/<prefix>``) and ``refs/bh/epoch`` are REAL git refs on
scratch bare remotes under ``tmp_path``; only the hive's Dolt data is a fake
:class:`~beadhive.writer_adopt.FenceData` (the real-Dolt run is ``test_writer_adopt_int.py``).
"""

from __future__ import annotations

import subprocess

import pytest

from beadhive import doctor, host_adopt, host_fence, host_lease, writer_adopt

PREFIX = "bh"
HOST_A = "aaaaaaaa-1111-4111-8111-111111111111"
HOST_B = "bbbbbbbb-2222-4222-8222-222222222222"
T0 = 1_800_000_000.0
TTL = 600.0


class Crash(BaseException):
    """An injected process death."""


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


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


class Data:
    """A minimal fake hive head: the bump lands on push, which never races here."""

    def __init__(self, writer: writer_adopt.WriterRow | None, triggers=44):
        self.remote = writer
        self.local = writer
        self.pending = None
        self.history = writer.epoch if writer else 0
        self.triggers = triggers
        self.calls: list[str] = []
        self.log: list[str] | None = None

    def remote_writer(self):
        self.calls.append("remote_writer")
        return self.remote

    def sync_to_remote(self):
        self.calls.append("sync")
        self.local, self.pending = self.remote, None

    def writer(self):
        return self.local

    def history_max_epoch(self):
        return self.history

    def trigger_count(self):
        return self.triggers

    def commit_bump(self, statements, message):
        frame, epoch = message.removeprefix(writer_adopt.ADOPT_COMMIT_PREFIX).rsplit("@", 1)
        self.local = writer_adopt.WriterRow(frame, int(epoch), "fresh")
        if self.log is not None:
            self.log.append("data")

    def push(self):
        self.remote = self.local
        self.history = max(self.history, self.local.epoch)
        return True


@pytest.fixture
def world(tmp_path):
    return {
        "hive_remote": _bare(tmp_path, "hive.git"),
        "hq_remote": _bare(tmp_path, "hq.git"),
        "hive_cwd": _clone(tmp_path, "hive"),
        "hq_cwd": _clone(tmp_path, "hq"),
    }


def _adopt(world, host_id=HOST_A, at=T0, **kw):
    return host_adopt.adopt(
        prefix=PREFIX,
        hive_remote=world["hive_remote"],
        hq_remote=world["hq_remote"],
        hive_cwd=world["hive_cwd"],
        hq_cwd=world["hq_cwd"],
        host_id=host_id,
        label="laptop",
        at=at,
        ttl=TTL,
        **kw,
    )


def _fence(world):
    return host_fence.read_fence(world["hive_remote"], cwd=world["hive_cwd"])[1]


def _lease(world):
    return host_lease.read(world["hq_remote"], PREFIX, cwd=world["hq_cwd"])


def _spy_order(monkeypatch, data: Data) -> list[str]:
    order: list[str] = []
    real_fence, real_lease = host_fence.install_fence, host_lease.adopt

    def spy_fence(*a, **kw):
        order.append("ref")
        return real_fence(*a, **kw)

    def spy_lease(*a, **kw):
        order.append("placement")
        return real_lease(*a, **kw)

    monkeypatch.setattr(host_adopt.host_fence, "install_fence", spy_fence)
    monkeypatch.setattr(host_adopt.host_lease, "adopt", spy_lease)
    data.log = order
    return order


def _cut_over(world) -> Data:
    """Legacy adopt by A at epoch 1, then the hive's data is seeded ``bh_writer = (A, 1)``."""
    assert _adopt(world, host_id=HOST_A).epoch == 1
    return Data(writer_adopt.WriterRow(HOST_A, 1, "seed"))


def test_a_cut_over_hive_is_adopted_placement_first_with_the_ref_in_lockstep(world, monkeypatch):
    data = _cut_over(world)
    order = _spy_order(monkeypatch, data)
    outcome = _adopt(world, host_id=HOST_B, at=T0 + TTL + 1, fence_data=data)
    assert order == ["placement", "ref", "data"]
    assert outcome.coexistence is not None and not outcome.coexistence.resumed
    assert outcome.epoch == 2
    assert (_lease(world).host_id, _lease(world).epoch) == (HOST_B, 2)
    assert (_fence(world).host_id, _fence(world).epoch, _fence(world).seq) == (HOST_B, 2, 0)
    assert (data.remote.frame, data.remote.epoch) == (HOST_B, 2)
    assert (
        outcome.fence_sha == host_fence.read_fence(world["hive_remote"], cwd=world["hive_cwd"])[0]
    )


def test_a_hive_without_bh_writer_keeps_the_legacy_order(world, monkeypatch):
    data = Data(None)
    order = _spy_order(monkeypatch, data)
    outcome = _adopt(world, fence_data=data)
    assert order == ["ref", "placement"]  # legacy: fence first, lease second
    assert outcome.coexistence is None and outcome.epoch == 1
    assert data.calls == ["remote_writer"]  # the local data was never reset


def test_the_coexistence_path_is_dormant_until_an_adapter_is_registered(world, monkeypatch):
    assert host_adopt.fence_data_for(PREFIX, world["hive_cwd"]) is None
    order = _spy_order(monkeypatch, Data(None))
    assert _adopt(world).coexistence is None
    assert order == ["ref", "placement"]


def test_a_registered_resolver_switches_the_path_on_data_alone(world):
    data = _cut_over(world)
    seen = []

    def resolver(prefix, hive_dir):
        seen.append((prefix, hive_dir))
        return data

    host_adopt.set_fence_data_resolver(resolver)
    try:
        outcome = _adopt(world, host_id=HOST_B, at=T0 + TTL + 1)
    finally:
        host_adopt.set_fence_data_resolver(None)
    assert seen == [(PREFIX, world["hive_cwd"])]
    assert outcome.coexistence is not None and outcome.epoch == 2
    assert host_adopt.fence_data_for(PREFIX, world["hive_cwd"]) is None


def test_a_crash_after_placement_resumes_without_a_second_bump(world, monkeypatch):
    data = _cut_over(world)
    real = host_fence.install_fence
    crashed = []

    def crash_once(*a, **kw):
        # One-shot, NOT monkeypatch.undo(): undo would also drop the BH_HOME sandbox.
        if not crashed:
            crashed.append(True)
            raise Crash("killed between the placement CAS and the ref CAS")
        return real(*a, **kw)

    monkeypatch.setattr(host_adopt.host_fence, "install_fence", crash_once)
    with pytest.raises(Crash):
        _adopt(world, host_id=HOST_B, at=T0 + TTL + 1, fence_data=data)
    # The half-state: placement names B@2, the ref and the data are still at A@1.
    assert (_lease(world).host_id, _lease(world).epoch) == (HOST_B, 2)
    assert _fence(world).epoch == 1 and data.remote.epoch == 1
    report = writer_adopt.adopt_report(
        PREFIX, writer_adopt.PlacementView(HOST_B, 2), data.remote_writer()
    )
    assert report is not None and "bh host lease adopt bh" in report.describe(host_id=HOST_B)

    outcome = _adopt(world, host_id=HOST_B, at=T0 + TTL + 2, fence_data=data)
    assert outcome.coexistence.resumed and outcome.epoch == 2
    assert _lease(world).epoch == _fence(world).epoch == data.remote.epoch == 2
    # The resumed adopt mirrored placement locally, as a won CAS does.
    assert host_lease.read_cached(PREFIX, cwd=world["hq_cwd"]).epoch == 2


def test_guard_incomplete_surfaces_adopt_incomplete_through_adopt_error(world):
    data = _cut_over(world)
    data.triggers = 2
    with pytest.raises(host_adopt.AdoptError) as excinfo:
        _adopt(world, host_id=HOST_B, at=T0 + TTL + 1, fence_data=data)
    assert isinstance(excinfo.value, host_adopt.AdoptIncomplete)
    assert "adopt incomplete" in str(excinfo.value)
    assert "bh host lease adopt bh" in str(excinfo.value)
    # The placement CAS was cached as soon as it was won: doctor's local read sees it.
    assert host_lease.read_cached(PREFIX, cwd=world["hq_cwd"]).epoch == 2


def test_a_converged_re_adopt_by_the_holder_is_a_no_op(world, monkeypatch):
    data = _cut_over(world)
    first = _adopt(world, host_id=HOST_B, at=T0 + TTL + 1, fence_data=data)
    order = _spy_order(monkeypatch, data)
    again = _adopt(world, host_id=HOST_B, at=T0 + TTL + 2, fence_data=data)
    assert again.coexistence.resumed and again.epoch == first.epoch == 2
    assert order == []  # nothing written anywhere


def test_the_lease_cas_honours_an_explicit_expectation(world):
    _adopt(world, host_id=HOST_A)
    before = _lease(world)
    with pytest.raises(host_lease.HostLeaseRejected, match="placement moved"):
        host_lease.adopt(
            world["hq_remote"],
            PREFIX,
            host_id=HOST_A,
            label="laptop",
            cwd=world["hq_cwd"],
            at=T0 + 1,
            epoch=9,
            expected="0" * 40,
        )
    assert _lease(world) == before


# ---- bh doctor ----------------------------------------------------------------------------


def _doctor_with(monkeypatch, data, lease_epoch, holder=HOST_B, this_host=HOST_B):
    lease = host_lease.HostLease(
        host_id=holder, label="x", epoch=lease_epoch, adopted_at="", expires_at=""
    )
    monkeypatch.setattr(doctor.guard, "primary_state", lambda **_kw: (PREFIX, this_host, lease))
    host_adopt.set_fence_data_resolver(lambda _p, _d: data)


def test_doctor_reports_adopt_incomplete_with_the_recovery_command(monkeypatch, tmp_path):
    _doctor_with(monkeypatch, Data(writer_adopt.WriterRow(HOST_A, 1)), lease_epoch=2)
    try:
        warning = doctor._adopt_incomplete_warning({}, {"prefix": PREFIX}, tmp_path)
    finally:
        host_adopt.set_fence_data_resolver(None)
    assert warning is not None
    assert "adopt incomplete (placement_ahead)" in warning
    assert "re-run `bh host lease adopt bh` here" in warning


@pytest.mark.parametrize(
    "data,lease_epoch",
    [(Data(writer_adopt.WriterRow(HOST_B, 2)), 2), (Data(None), 5), (None, 5)],
    ids=["converged", "legacy-hive", "dormant"],
)
def test_doctor_is_quiet_unless_placement_is_ahead(monkeypatch, tmp_path, data, lease_epoch):
    _doctor_with(monkeypatch, data, lease_epoch)
    try:
        assert doctor._adopt_incomplete_warning({}, {"prefix": PREFIX}, tmp_path) is None
    finally:
        host_adopt.set_fence_data_resolver(None)


# ---- failover reclaim through host_adopt (bh-4z2rx, M3) ------------------------------------


class ReclaimingData(Data):
    """A fence adapter that also reads claims and policy (``ReclaimData``): reclaim switches on."""

    def __init__(self, writer, claims, policy):
        super().__init__(writer)
        self._claims, self._policy = claims, policy
        self.bumps: list[list[str]] = []

    def config_rows(self):
        return dict(self._policy)

    def claims(self):
        return list(self._claims)

    def commit_bump(self, statements, message):
        self.bumps.append(list(statements))
        super().commit_bump(statements, message)


def _reclaiming(world) -> ReclaimingData:
    from beadhive import failover_reclaim as fr

    assert _adopt(world, host_id=HOST_A).epoch == 1
    # The backup probe fetches from the hive clone's origin (no backup refs there: unbacked).
    _git(["remote", "add", "origin", world["hive_remote"]], world["hive_cwd"])
    claims = [
        fr.Claim("bh-dead", "dev/x", frozenset({f"claim-frame:{HOST_A}"})),
        fr.Claim("bh-live", "dev/y", frozenset({"claim-frame:frame-other"})),
    ]
    policy = {"bh.pairing.enabled": "true", "bh.reclaim.failover.mode": "apply"}
    return ReclaimingData(writer_adopt.WriterRow(HOST_A, 1, "seed"), claims, policy)


def test_an_expired_primary_is_failed_over_and_its_claims_reclaimed_in_the_bump(world):
    from beadhive import failover_reclaim as fr

    data = _reclaiming(world)
    outcome = _adopt(world, host_id=HOST_B, at=T0 + TTL + 1, fence_data=data)
    plan = outcome.coexistence.step2.reclaim
    assert plan is not None and plan.applied and plan.dead_frame == HOST_A
    assert [(r.bead, r.outcome) for r in plan.rows] == [
        ("bh-dead", fr.Outcome.REWOUND),
        ("bh-live", fr.Outcome.OTHER_FRAME),
    ]
    [bump] = data.bumps
    assert bump[0].startswith("UPDATE bh_writer")
    assert any("WHERE id = 'bh-dead'" in s for s in bump)
    assert not any("bh-live" in s for s in bump)


def test_a_released_primary_is_a_planned_handoff_and_reclaims_nothing(world):
    data = _reclaiming(world)
    host_lease.release(world["hq_remote"], PREFIX, host_id=HOST_A, cwd=world["hq_cwd"])
    outcome = _adopt(world, host_id=HOST_B, at=T0 + 1, fence_data=data)
    plan = outcome.coexistence.step2.reclaim
    assert plan is not None and not plan.applied and "planned handoff" in plan.skipped
    assert not any("issues" in s for s in data.bumps[0])
