"""``guard_primary`` with an unreachable HQ, now that renewal is retired (bh-ytbb.11 -> bh-12hev).

ADR ``hive-writer-partitioning-adr.md`` §4 and its Amendment 2 to Amendment 1 §4: "an
established primary keeps writing indefinitely while HQ is unreachable." ``expires_at`` is a
failover hint, :meth:`HostLease.held_by` no longer consults it, and ``renew_if_due`` is a
documented no-op, so these tests now pin the reversed behaviour against REAL git plumbing:

  * an existing primary keeps writing through an unreachable HQ, before AND after its cached
    ``expires_at`` — and no write-verb boundary ever tries to reach HQ;
  * the cache is never touched by a write verb, reachable HQ or not;
  * a control-plane failure can no longer surface through ``guard_primary``, because nothing
    on the allow path calls the control plane.

"HQ unreachable" here means a REAL git remote pointed at a path that was never created. The
only mocked pieces are the wall clock (``host_lease.time.time``) and hive resolution (this test
never touches a real registered hive or the operator's ``~/.beadhive``).
"""

from __future__ import annotations

import subprocess

import pytest

from beadhive import guard, host, host_lease, registry
from beadhive.hq_control_plane import ControlPlaneError, HqLeaseUnknown
from beadhive.hq_sql_runtime import SqlRuntimeError
from beadhive.hq_sql_transport import SqlTransportError

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


def _forbid_network(monkeypatch):
    def boom(*_a, **_kw):
        raise AssertionError("a write-verb boundary must not reach HQ")

    monkeypatch.setattr(host_lease, "renew", boom)
    monkeypatch.setattr(host_lease, "refresh_cached", boom)


# ---- the AC: HQ unreachable, keep writing indefinitely --------------------------------------


def test_primary_keeps_writing_through_an_unreachable_hq_past_its_cached_expiry(
    hq_dir, hq_remote_path, hive, this_host, tmp_path, monkeypatch
):
    _adopt_and_cache(hq_dir, at=T0, ttl=TTL)  # a REAL adopt, HQ reachable at this point
    _break_hq_remote(hq_dir, tmp_path)  # HQ becomes unreachable from here on
    _forbid_network(monkeypatch)

    for clock in (T0 + 1, T0 + RENEW_INTERVAL + 1, T0 + TTL + 1, T0 + 100 * TTL):
        _at(monkeypatch, clock)
        guard.guard_primary("", cfg={})  # no raise: an established primary keeps working
        cached = host_lease.read_cached(PREFIX, cwd=hq_dir)
        assert cached.expires_at == host_lease.now_stamp(T0 + TTL)  # never moved


def test_a_reachable_hq_is_not_renewed_by_write_verbs_either(
    hq_dir, hq_remote_path, hive, this_host, monkeypatch
):
    """Contrast case: HQ reachable, still no renewal — the write path is local-only."""
    _adopt_and_cache(hq_dir, at=T0, ttl=TTL)
    _forbid_network(monkeypatch)
    _at(monkeypatch, T0 + RENEW_INTERVAL + 1)
    guard.guard_primary("", cfg={})
    assert host_lease.read_cached(PREFIX, cwd=hq_dir).expires_at == host_lease.now_stamp(T0 + TTL)


# ---- bh-jto52's concern is now structural: the allow path never calls the control plane ----


@pytest.mark.parametrize(
    "make_error",
    [
        lambda: HqLeaseUnknown("req-1", "sha-1", 7),
        lambda: ControlPlaneError("down"),
        lambda: SqlRuntimeError("stale"),
        lambda: SqlTransportError("tmo"),
    ],
    ids=["HqLeaseUnknown", "ControlPlaneError", "SqlRuntimeError", "SqlTransportError"],
)
def test_a_control_plane_failure_cannot_reach_guard_primary(
    hq_dir, hive, this_host, monkeypatch, make_error
):
    _adopt_and_cache(hq_dir, at=T0, ttl=TTL)

    def boom(*_a, **_k):
        raise make_error()

    monkeypatch.setattr(host_lease, "renew", boom)
    _at(monkeypatch, T0 + RENEW_INTERVAL + 1)
    guard.guard_primary("", cfg={})  # no raise: the write is allowed
    assert host_lease.read_cached(PREFIX, cwd=hq_dir).expires_at == host_lease.now_stamp(T0 + TTL)
