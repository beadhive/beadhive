"""Best-effort liveness renewal on legacy hives (bh-ytbb.11, reshaped by bh-12hev).

Since bh-12hev expiry never gates a write: :meth:`HostLease.held_by` is clock-free, so an
established primary keeps writing while HQ is unreachable and past its cached ``expires_at``.
On a hive NOT yet cut over to the in-data ``bh_writer`` fence, ``renew_if_due`` still pushes
``expires_at`` out from the write-verb boundary (``guard_primary``) and the dispatch loop
(``localloop.HostLeaseKeeper``) so the lease stays a truthful LIVENESS hint until M8 placement:
another host's plain ``bh host adopt`` cannot take a live primary's lease ~ttl after adopt.
Renewal is best-effort — every failure is logged and swallowed and never refuses a write. A
cut-over hive does not renew (signed-mode SQL HQ is pinned in ``test_hq_sql_signed_liveness``).

"HQ unreachable" here means a REAL git remote pointed at a path that was never created. The
only mocked pieces are the wall clock (``host_lease.time.time``), hive resolution (this test
never touches a real registered hive or the operator's ``~/.beadhive``) and, for the cut-over
cases, the in-data fence adapter.
"""

from __future__ import annotations

import subprocess

import pytest

from beadhive import (
    fence_data_port,
    frame_eligibility,
    guard,
    host,
    host_lease,
    localloop,
    registry,
)
from beadhive.hq_control_plane import ControlPlaneError, HqLeaseUnknown
from beadhive.hq_sql_runtime import SqlRuntimeError
from beadhive.hq_sql_transport import SqlTransportError
from beadhive.writer_adopt import WriterRow

PREFIX = "tt"
THIS_HOST = "11111111-1111-4111-8111-111111111111"
T0 = 1_800_000_000.0
TTL = 600.0
RENEW_INTERVAL = 300.0


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


@pytest.fixture
def hq_remote_path(tmp_path):
    """A REAL bare repo standing in for Factory HQ's remote — reachable at setup time."""
    path = tmp_path / "hq.git"
    _git(["init", "--bare", "-q", str(path)], tmp_path)
    return path


@pytest.fixture
def hq_dir(tmp_path, hq_remote_path, monkeypatch):
    """This host's local HQ clone, with a REAL ``origin`` remote wired to `hq_remote_path` —
    exactly the shape ``bh hq init``/``clone`` leaves behind, so pointing `origin` at a
    nonexistent path later is a genuine, structural "HQ unreachable", not a stand-in."""
    path = tmp_path / "hq"
    path.mkdir()
    _git(["init", "-q"], path)
    _git(["config", "user.email", "t@example.invalid"], path)
    _git(["config", "user.name", "t"], path)
    _git(["remote", "add", "origin", str(hq_remote_path)], path)
    monkeypatch.setenv("BH_HQ", str(path))
    return path


def _break_hq_remote(hq_dir, tmp_path):
    """Simulate 'HQ unreachable': repoint `origin` at a path that was never created. Any
    subsequent `git ls-remote`/`git push origin ...` from `hq_dir` now fails for real."""
    bogus = tmp_path / "hq-gone.git"  # deliberately never created
    result = _git(["remote", "set-url", "origin", str(bogus)], hq_dir)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def this_host(monkeypatch):
    monkeypatch.setattr(host, "host_id", lambda: THIS_HOST)
    return THIS_HOST


@pytest.fixture
def hive(tmp_path, monkeypatch):
    """A registered hive whose prefix the guard resolves to — never a real hive checkout."""
    entry = {"provider": "github", "org": "o", "repo": "r", "prefix": PREFIX}
    monkeypatch.setattr(registry, "hive_dir_for", lambda _cfg, _hive: tmp_path / "hive")
    monkeypatch.setattr(registry, "entry_for_dir", lambda _cfg, _dir: entry)
    return entry


def _adopt_and_cache(hq_dir, *, at=T0, ttl=TTL):
    outcome = host_lease.adopt(
        "origin", PREFIX, host_id=THIS_HOST, label="lap", cwd=hq_dir, ttl=ttl, at=at
    )
    host_lease.cache(PREFIX, outcome, cwd=hq_dir)
    return outcome


def _at(monkeypatch, clock):
    monkeypatch.setattr(host_lease.time, "time", lambda: clock)


class _Recorder:
    def __init__(self):
        self.seen: list[tuple] = []

    def warning(self, event, **kw):
        self.seen.append((event, kw))

    def __getattr__(self, _name):
        return lambda *_a, **_k: None


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(host_lease.log, "get_logger", lambda *_a, **_k: rec)
    return rec


def _cached_expiry(hq_dir):
    return host_lease.read_cached(PREFIX, cwd=hq_dir).expires_at


@pytest.fixture
def cut_over(monkeypatch):
    """Register an in-data fence adapter whose local ``bh_writer`` names THIS host."""

    class _Data:
        def writer(self):
            return WriterRow(THIS_HOST, 7, "seed")

    monkeypatch.setattr(fence_data_port, "_fence_data_resolver", lambda _p, _d: _Data())


# ---- a legacy hive renews on schedule -------------------------------------------------------


def test_a_legacy_hive_renews_from_the_write_boundary_once_due(
    hq_dir, hq_remote_path, hive, this_host, monkeypatch
):
    _adopt_and_cache(hq_dir, at=T0, ttl=TTL)

    def not_yet(*_a, **_kw):
        raise AssertionError("no HQ round trip before the renew interval elapses")

    real_renew = host_lease.renew
    monkeypatch.setattr(host_lease, "renew", not_yet)
    _at(monkeypatch, T0 + RENEW_INTERVAL - 1)  # not due yet: purely local
    guard.guard_primary("", cfg={"host": {"lease": {"ttl": TTL, "renew_interval": RENEW_INTERVAL}}})
    assert _cached_expiry(hq_dir) == host_lease.now_stamp(T0 + TTL)

    monkeypatch.setattr(host_lease, "renew", real_renew)
    _at(monkeypatch, T0 + RENEW_INTERVAL + 1)  # due: a REAL renewal lands and is cached
    guard.guard_primary("", cfg={"host": {"lease": {"ttl": TTL, "renew_interval": RENEW_INTERVAL}}})
    assert _cached_expiry(hq_dir) == host_lease.now_stamp(T0 + RENEW_INTERVAL + 1 + TTL)
    remote = host_lease.read("origin", PREFIX, cwd=hq_dir)
    assert remote.expires_at == host_lease.now_stamp(T0 + RENEW_INTERVAL + 1 + TTL)


def test_the_dispatch_loop_keeper_renews_a_legacy_hive_while_workers_are_active(
    hq_dir, hq_remote_path, this_host, monkeypatch
):
    _adopt_and_cache(hq_dir, at=T0, ttl=TTL)
    keeper = localloop.HostLeaseKeeper(
        prefix=PREFIX, host_id=THIS_HOST, hq_dir=hq_dir, ttl=TTL, renew_interval=RENEW_INTERVAL
    )
    _at(monkeypatch, T0 + RENEW_INTERVAL + 1)
    idle = keeper.renew(active=False)  # idle host: let the lease lapse (the intended handoff)
    assert idle.held and not idle.renewed
    status = keeper.renew(active=True)
    assert status.held and status.renewed, status
    assert _cached_expiry(hq_dir) == host_lease.now_stamp(T0 + RENEW_INTERVAL + 1 + TTL)


# ---- a failed renewal never refuses a write -------------------------------------------------


def test_primary_keeps_writing_through_an_unreachable_hq_past_its_cached_expiry(
    hq_dir, hq_remote_path, hive, this_host, tmp_path, monkeypatch, recorder
):
    _adopt_and_cache(hq_dir, at=T0, ttl=TTL)  # a REAL adopt, HQ reachable at this point
    _break_hq_remote(hq_dir, tmp_path)  # every renewal from here on fails for real

    for clock in (T0 + 1, T0 + RENEW_INTERVAL + 1, T0 + TTL + 1, T0 + 100 * TTL):
        _at(monkeypatch, clock)
        guard.guard_primary("", cfg={})  # no raise: an established primary keeps working
        # A failed renewal never fraudulently extends the hint.
        assert _cached_expiry(hq_dir) == host_lease.now_stamp(T0 + TTL)

    failures = [kw for evt, kw in recorder.seen if evt == "host_lease_renew_if_due_failed"]
    assert len(failures) == 3  # one per due boundary; none before the interval
    assert all(kw["hive_prefix"] == PREFIX for kw in failures)


@pytest.mark.parametrize(
    "make_error",
    [
        lambda: HqLeaseUnknown("req-1", "sha-1", 7),
        lambda: ControlPlaneError("down"),
        lambda: SqlRuntimeError("stale"),
        lambda: SqlTransportError("tmo"),
        lambda: RuntimeError("anything at all"),
    ],
    ids=["HqLeaseUnknown", "ControlPlaneError", "SqlRuntimeError", "SqlTransportError", "other"],
)
def test_a_renew_failure_inside_guard_primary_allows_the_write_and_logs(
    hq_dir, hive, this_host, monkeypatch, make_error, recorder
):
    _adopt_and_cache(hq_dir, at=T0, ttl=TTL)

    def boom(*_a, **_k):
        raise make_error()

    monkeypatch.setattr(host_lease, "renew", boom)
    _at(monkeypatch, T0 + TTL + 1)  # due, and past the hint
    guard.guard_primary("", cfg={})  # no raise: the write is allowed
    failures = [kw for evt, kw in recorder.seen if evt == "host_lease_renew_if_due_failed"]
    assert len(failures) == 1 and failures[0]["hive_prefix"] == PREFIX
    assert _cached_expiry(hq_dir) == host_lease.now_stamp(T0 + TTL)


def test_even_a_renew_if_due_that_raises_cannot_refuse_the_write(
    hq_dir, hive, this_host, monkeypatch
):
    """Belt and braces: the guard's own wrapper swallows anything escaping renew_if_due."""
    _adopt_and_cache(hq_dir, at=T0, ttl=TTL)

    def boom(*_a, **_k):
        raise RuntimeError("renewal exploded")

    monkeypatch.setattr(host_lease, "renew_if_due", boom)
    _at(monkeypatch, T0 + TTL + 1)
    guard.guard_primary("", cfg={})  # no raise


def test_a_failed_keeper_renewal_still_reports_the_lease_held(
    hq_dir, hq_remote_path, this_host, monkeypatch
):
    _adopt_and_cache(hq_dir, at=T0, ttl=TTL)

    def boom(*_a, **_k):
        raise RuntimeError("renewal exploded")

    monkeypatch.setattr(host_lease, "renew_if_due", boom)
    keeper = localloop.HostLeaseKeeper(
        prefix=PREFIX, host_id=THIS_HOST, hq_dir=hq_dir, ttl=TTL, renew_interval=RENEW_INTERVAL
    )
    _at(monkeypatch, T0 + TTL + 1)
    status = keeper.renew(active=True)
    assert status.held and not status.renewed


# ---- a cut-over hive does not renew ---------------------------------------------------------


def test_renew_if_due_is_a_no_op_on_a_cut_over_hive(hq_dir, tmp_path, cut_over, monkeypatch):
    _adopt_and_cache(hq_dir, at=T0, ttl=TTL)

    def boom(*_a, **_k):
        raise AssertionError("a cut-over hive must not renew the lease")

    monkeypatch.setattr(host_lease, "renew", boom)
    out = host_lease.renew_if_due(
        "origin", PREFIX, host_id=THIS_HOST, cwd=hq_dir, at=T0 + TTL - 1, hive_dir=tmp_path
    )
    assert out is None
    assert _cached_expiry(hq_dir) == host_lease.now_stamp(T0 + TTL)


def test_an_unreadable_bh_writer_does_not_renew_either(hq_dir, tmp_path, monkeypatch):
    class _Broken:
        def writer(self):
            raise OSError("dolt is down")

    monkeypatch.setattr(fence_data_port, "_fence_data_resolver", lambda _p, _d: _Broken())
    _adopt_and_cache(hq_dir, at=T0, ttl=TTL)
    monkeypatch.setattr(host_lease, "renew", lambda *a, **k: pytest.fail("must not renew"))
    out = host_lease.renew_if_due(
        "origin", PREFIX, host_id=THIS_HOST, cwd=hq_dir, at=T0 + TTL - 1, hive_dir=tmp_path
    )
    assert out is None


def test_cut_over_write_boundary_and_dispatch_loop_never_renew(
    hq_dir, hive, this_host, cut_over, monkeypatch
):
    _adopt_and_cache(hq_dir, at=T0, ttl=TTL)

    def boom(*_a, **_k):
        raise AssertionError("a cut-over hive must not renew the lease")

    monkeypatch.setattr(host_lease, "renew", boom)
    monkeypatch.setattr(host_lease, "renew_if_due", boom)
    monkeypatch.setattr(frame_eligibility, "require_local", lambda *a, **k: None)
    _at(monkeypatch, T0 + TTL + 1)
    guard.guard_primary("", cfg={})  # decided by bh_writer; no renewal

    keeper = localloop.lease_keeper_for("", cfg={}, hive_dir=hq_dir.parent / "hive")
    assert isinstance(keeper.keeper, localloop.HostLeaseKeeper)
    assert keeper.keeper.liveness_renewal is False
    status = keeper.keeper.renew(active=True)
    assert status.held and not status.renewed


def test_the_factory_keeps_liveness_renewal_on_for_a_legacy_hive(
    hq_dir, hive, this_host, monkeypatch
):
    _adopt_and_cache(hq_dir, at=T0, ttl=TTL)
    monkeypatch.setattr(frame_eligibility, "require_local", lambda *a, **k: None)
    keeper = localloop.lease_keeper_for("", cfg={}, hive_dir=hq_dir.parent / "hive")
    assert keeper.keeper.liveness_renewal is True
