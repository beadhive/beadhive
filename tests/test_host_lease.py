"""``refs/bh/lease/<prefix>`` — the HQ-side **host lease** (bh-ytbb.6).

Covers the acceptance bar directly:
  * record shape ``{host_id, label, epoch, adopted_at, expires_at}``.
  * adopt   — CAS from expired-or-absent, ``epoch + 1``; a loser is REJECTED, not retried.
  * renew   — CAS from own value, SAME epoch, new ``expires_at``.
  * release — CAS from own value to a tombstone.
  * takeover— CAS from an unexpired value only with ``force``, logged loudly.
  * the configurable renew-interval / TTL keys and their 5 min / 30 min defaults.
  * the concurrent-adopt race: one winner, one rejection.

Every remote is a scratch BARE repo under the test's own ``tmp_path`` (a plain local path,
never a network remote); the autouse ``_sandbox_bh_home`` fixture additionally guarantees no
test here can resolve the operator's real ``~/.beadhive/hq``.

Time is injected via each operation's ``at=`` parameter — expiry is asserted by arithmetic,
never by sleeping.
"""

from __future__ import annotations

import subprocess

import pytest

from beadhive import config, config_partition, config_schema, gitref, host_lease

PREFIX = "bh"
HOST_A = "11111111-1111-4111-8111-111111111111"
HOST_B = "22222222-2222-4222-8222-222222222222"
T0 = 1_800_000_000.0  # a fixed epoch-seconds instant; all expiry math is relative to it


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


@pytest.fixture
def hq_remote(tmp_path):
    """A scratch BARE repo standing in for the HQ remote (`<owner>/beadhive-hq`)."""
    path = tmp_path / "hq.git"
    _git(["init", "--bare", "-q", str(path)], tmp_path)
    return str(path)


def _clone(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    _git(["init", "-q"], path)
    _git(["config", "user.email", "t@example.invalid"], path)
    _git(["config", "user.name", "t"], path)
    return path


@pytest.fixture
def host_a(tmp_path):
    """Host A's local HQ clone (the object db its CAS pushes from)."""
    return _clone(tmp_path, "host-a")


@pytest.fixture
def host_b(tmp_path):
    return _clone(tmp_path, "host-b")


def _adopt(remote, cwd, host_id, label="lap", **kw):
    return host_lease.adopt(
        remote, PREFIX, host_id=host_id, label=label, cwd=cwd, at=T0, **kw
    )


# ---- record shape ---------------------------------------------------------------


def test_lease_ref_is_namespaced_per_prefix():
    assert host_lease.lease_ref("bh") == "refs/bh/lease/bh"
    with pytest.raises(ValueError):
        host_lease.lease_ref("")


def test_record_carries_exactly_the_five_documented_fields(hq_remote, host_a):
    _adopt(hq_remote, host_a, HOST_A, label="laptop")
    _sha, record = gitref.read_remote(hq_remote, host_lease.lease_ref(PREFIX), cwd=host_a)
    assert set(record) == {"host_id", "label", "epoch", "adopted_at", "expires_at"}
    assert record["host_id"] == HOST_A
    assert record["label"] == "laptop"
    assert record["epoch"] == 1


def test_from_record_fails_loudly_on_a_missing_field():
    with pytest.raises(ValueError, match="expires_at"):
        host_lease.HostLease.from_record(
            {"host_id": "x", "label": "y", "epoch": 1, "adopted_at": "z"}
        )


def test_a_malformed_expiry_reads_as_expired_not_as_valid_forever():
    """Fail-closed: a corrupt stamp must never make a lease look infinitely valid."""
    lease = host_lease.HostLease(HOST_A, "a", 1, "garbage", "garbage")
    assert lease.is_expired(T0)


# ---- adopt ----------------------------------------------------------------------


def test_adopt_from_absent_starts_at_epoch_one(hq_remote, host_a):
    out = _adopt(hq_remote, host_a, HOST_A)
    assert out.lease.epoch == 1
    assert out.previous is None
    assert host_lease.read(hq_remote, PREFIX, cwd=host_a).host_id == HOST_A


def test_adopt_sets_expiry_from_the_ttl(hq_remote, host_a):
    out = _adopt(hq_remote, host_a, HOST_A, ttl=600.0)
    assert out.lease.adopted_at == host_lease.now_stamp(T0)
    assert out.lease.expires_at == host_lease.now_stamp(T0 + 600.0)
    assert not out.lease.is_expired(T0 + 599)
    assert out.lease.is_expired(T0 + 601)


def test_adopt_over_an_expired_lease_bumps_the_epoch(hq_remote, host_a, host_b):
    _adopt(hq_remote, host_a, HOST_A, ttl=600.0)
    later = T0 + 601.0
    out = host_lease.adopt(
        hq_remote, PREFIX, host_id=HOST_B, label="desk", cwd=host_b, at=later
    )
    assert out.lease.epoch == 2
    assert out.previous.host_id == HOST_A


def test_adopt_is_refused_while_another_host_holds_an_unexpired_lease(
    hq_remote, host_a, host_b
):
    _adopt(hq_remote, host_a, HOST_A, label="laptop", ttl=600.0)
    with pytest.raises(host_lease.HostLeaseRejected) as excinfo:
        host_lease.adopt(
            hq_remote, PREFIX, host_id=HOST_B, label="desk", cwd=host_b, at=T0 + 1
        )
    # the refusal names the holder AND its expiry (so the operator knows what to do)
    assert HOST_A in str(excinfo.value)
    assert "laptop" in str(excinfo.value)
    assert host_lease.now_stamp(T0 + 600.0) in str(excinfo.value)


def test_re_adopting_our_own_live_lease_succeeds_and_bumps_the_epoch(hq_remote, host_a):
    """The recovery path bh-ytbb.8 depends on: a fresh epoch invalidates every fence token
    minted under the old one, so a half-adopted state is fixed by simply re-adopting."""
    first = _adopt(hq_remote, host_a, HOST_A, ttl=600.0)
    again = host_lease.adopt(
        hq_remote, PREFIX, host_id=HOST_A, label="lap", cwd=host_a, at=T0 + 1, ttl=600.0
    )
    assert (first.lease.epoch, again.lease.epoch) == (1, 2)


def test_adopt_over_a_tombstone_continues_the_epoch_sequence(hq_remote, host_a, host_b):
    _adopt(hq_remote, host_a, HOST_A)
    host_lease.release(hq_remote, PREFIX, host_id=HOST_A, cwd=host_a, at=T0 + 5)
    out = host_lease.adopt(
        hq_remote, PREFIX, host_id=HOST_B, label="desk", cwd=host_b, at=T0 + 10
    )
    assert out.lease.epoch == 2  # NOT back to 1 — a stale epoch-1 fence must stay invalid


# ---- the concurrent-adopt race ---------------------------------------------------


def test_concurrent_adopt_has_exactly_one_winner_and_one_rejection(
    hq_remote, host_a, host_b, monkeypatch
):
    """Two hosts adopt an ABSENT lease from the same observed state — the shape of a genuine
    race, reproduced deterministically by interleaving at the CAS.

    Host B's read happens (so it, too, sees "absent"), then A's CAS lands underneath it, then
    B's CAS goes out against the now-stale expectation. Exactly one push can win, because the
    remote is a linearization point; the loser is REJECTED, not silently retried."""
    winner_sha: dict[str, str] = {}
    real_cas = gitref.cas

    def racing_cas(remote, ref, record, *, expected, cwd):
        # B (identified by its own cwd) gets pre-empted by A between its read and its CAS.
        if cwd == host_b and not winner_sha:
            winner_sha["a"] = _adopt(hq_remote, host_a, HOST_A).sha
        return real_cas(remote, ref, record, expected=expected, cwd=cwd)

    monkeypatch.setattr(gitref, "cas", racing_cas)

    with pytest.raises(host_lease.HostLeaseRejected) as excinfo:
        host_lease.adopt(
            hq_remote, PREFIX, host_id=HOST_B, label="desk", cwd=host_b, at=T0
        )
    assert "NOT retried" in str(excinfo.value)

    # exactly one winner is recorded at HQ, and it is A
    held = host_lease.read(hq_remote, PREFIX, cwd=host_a)
    assert held.host_id == HOST_A
    assert held.epoch == 1


# ---- renew ------------------------------------------------------------------------


def test_renew_keeps_the_epoch_and_extends_the_expiry(hq_remote, host_a):
    first = _adopt(hq_remote, host_a, HOST_A, ttl=600.0)
    out = host_lease.renew(
        hq_remote, PREFIX, host_id=HOST_A, cwd=host_a, ttl=600.0, at=T0 + 100
    )
    assert out.lease.epoch == first.lease.epoch  # a renewal is NOT a handoff
    assert out.lease.adopted_at == first.lease.adopted_at
    assert out.lease.expires_at == host_lease.now_stamp(T0 + 700.0)


def test_renew_is_refused_for_a_lease_this_host_does_not_hold(hq_remote, host_a, host_b):
    _adopt(hq_remote, host_a, HOST_A)
    with pytest.raises(host_lease.HostLeaseRejected, match="does not hold"):
        host_lease.renew(hq_remote, PREFIX, host_id=HOST_B, cwd=host_b, at=T0 + 1)


def test_renew_is_refused_when_no_lease_exists(hq_remote, host_a):
    with pytest.raises(host_lease.HostLeaseRejected, match="nothing to renew"):
        host_lease.renew(hq_remote, PREFIX, host_id=HOST_A, cwd=host_a, at=T0)


def test_a_lapsed_renewal_loses_to_a_takeover_that_already_landed(
    hq_remote, host_a, host_b
):
    """Renewing past expiry is allowed, and safe: the CAS is from our OWN value, so if
    another host adopted meanwhile the ref has moved and the renewal is rejected."""
    _adopt(hq_remote, host_a, HOST_A, ttl=600.0)
    host_lease.adopt(
        hq_remote, PREFIX, host_id=HOST_B, label="desk", cwd=host_b, at=T0 + 601
    )
    with pytest.raises(host_lease.HostLeaseRejected, match="does not hold"):
        host_lease.renew(hq_remote, PREFIX, host_id=HOST_A, cwd=host_a, at=T0 + 602)


# ---- release ----------------------------------------------------------------------


def test_release_writes_a_tombstone_that_preserves_the_epoch(hq_remote, host_a):
    _adopt(hq_remote, host_a, HOST_A)
    out = host_lease.release(hq_remote, PREFIX, host_id=HOST_A, cwd=host_a, at=T0 + 5)
    assert out.lease.is_tombstone
    assert out.lease.epoch == 1  # NOT deleted: the epoch sequence must never restart
    held = host_lease.read(hq_remote, PREFIX, cwd=host_a)
    assert held.is_tombstone and held.is_expired(T0 + 5)


def test_release_is_refused_for_a_lease_this_host_does_not_hold(hq_remote, host_a, host_b):
    _adopt(hq_remote, host_a, HOST_A)
    with pytest.raises(host_lease.HostLeaseRejected, match="does not hold"):
        host_lease.release(hq_remote, PREFIX, host_id=HOST_B, cwd=host_b, at=T0 + 1)


def test_releasing_twice_is_refused(hq_remote, host_a):
    _adopt(hq_remote, host_a, HOST_A)
    host_lease.release(hq_remote, PREFIX, host_id=HOST_A, cwd=host_a, at=T0 + 5)
    with pytest.raises(host_lease.HostLeaseRejected, match="already released"):
        host_lease.release(hq_remote, PREFIX, host_id=HOST_A, cwd=host_a, at=T0 + 6)


# ---- takeover ---------------------------------------------------------------------


def test_takeover_of_an_unexpired_lease_is_refused_without_force(hq_remote, host_a, host_b):
    _adopt(hq_remote, host_a, HOST_A, ttl=600.0)
    with pytest.raises(host_lease.HostLeaseRejected):
        host_lease.takeover(
            hq_remote, PREFIX, host_id=HOST_B, label="desk", cwd=host_b, at=T0 + 1
        )


def test_forced_takeover_wins_and_is_logged_loudly(hq_remote, host_a, host_b, monkeypatch):
    _adopt(hq_remote, host_a, HOST_A, ttl=600.0)
    seen: list[tuple] = []

    class _Recorder:
        def warning(self, event, **kw):
            seen.append((event, kw))

    monkeypatch.setattr(host_lease.log, "get_logger", lambda *_a, **_k: _Recorder())
    out = host_lease.takeover(
        hq_remote, PREFIX, host_id=HOST_B, label="desk", cwd=host_b, at=T0 + 1, force=True
    )
    assert out.lease.host_id == HOST_B and out.lease.epoch == 2
    assert seen and seen[0][0] == "host_lease_forced_takeover"
    assert seen[0][1]["from_host"] == HOST_A


def test_takeover_of_an_expired_lease_needs_no_force(hq_remote, host_a, host_b):
    _adopt(hq_remote, host_a, HOST_A, ttl=600.0)
    out = host_lease.takeover(
        hq_remote, PREFIX, host_id=HOST_B, label="desk", cwd=host_b, at=T0 + 601
    )
    assert out.lease.host_id == HOST_B


# ---- the local cache ---------------------------------------------------------------


def test_cache_round_trips_the_lease_without_the_network(hq_remote, host_a):
    out = _adopt(hq_remote, host_a, HOST_A)
    host_lease.cache(PREFIX, out, cwd=host_a)
    cached = host_lease.read_cached(PREFIX, cwd=host_a)
    assert cached == out.lease


def test_read_cached_is_none_when_this_host_never_adopted(host_a):
    assert host_lease.read_cached(PREFIX, cwd=host_a) is None


# ---- lease_state: held / expiring / free (bh-ytbb.11 / bh-ytbb.13) -----------------------


def test_lease_state_is_free_for_none_a_tombstone_or_an_expired_lease():
    tombstone = host_lease.HostLease("", "", 3, "t", host_lease.now_stamp(T0))
    expired = host_lease.HostLease(HOST_A, "a", 1, host_lease.now_stamp(T0), host_lease.now_stamp(T0 + 1))
    assert host_lease.lease_state(None, at=T0) == "free"
    assert host_lease.lease_state(tombstone, at=T0 + 5) == "free"
    assert host_lease.lease_state(expired, at=T0 + 2) == "free"


def test_lease_state_is_held_with_more_than_a_renew_interval_of_runway_left():
    lease = host_lease.HostLease(HOST_A, "a", 1, host_lease.now_stamp(T0), host_lease.now_stamp(T0 + 600))
    assert host_lease.lease_state(lease, at=T0, renew_interval=300.0) == "held"


def test_lease_state_is_expiring_within_one_renew_interval_of_its_own_expiry():
    lease = host_lease.HostLease(HOST_A, "a", 1, host_lease.now_stamp(T0), host_lease.now_stamp(T0 + 600))
    # exactly at the boundary (300s remaining, renew_interval=300) and just inside it
    assert host_lease.lease_state(lease, at=T0 + 300, renew_interval=300.0) == "expiring"
    assert host_lease.lease_state(lease, at=T0 + 599, renew_interval=300.0) == "expiring"
    assert host_lease.lease_state(lease, at=T0 + 299, renew_interval=300.0) == "held"


# ---- renew_if_due: the renewal loop's body (bh-ytbb.11) -----------------------------------


def test_renew_if_due_makes_no_hq_round_trip_before_the_renew_interval_elapses(
    hq_remote, host_a, monkeypatch
):
    out = _adopt(hq_remote, host_a, HOST_A, ttl=600.0)
    host_lease.cache(PREFIX, out, cwd=host_a)

    def boom(*_a, **_kw):
        raise AssertionError("renew_if_due must not touch the network before it is due")

    monkeypatch.setattr(host_lease, "renew", boom)
    result = host_lease.renew_if_due(
        hq_remote, PREFIX, host_id=HOST_A, cwd=host_a, renew_interval=300.0, at=T0 + 100,
    )
    assert result is None


def test_renew_if_due_renews_and_updates_the_local_cache_once_due(hq_remote, host_a):
    out = _adopt(hq_remote, host_a, HOST_A, ttl=600.0)
    host_lease.cache(PREFIX, out, cwd=host_a)

    result = host_lease.renew_if_due(
        hq_remote, PREFIX, host_id=HOST_A, cwd=host_a, ttl=600.0, renew_interval=300.0,
        at=T0 + 301,
    )

    assert result is not None
    assert result.lease.expires_at == host_lease.now_stamp(T0 + 301 + 600.0)
    cached = host_lease.read_cached(PREFIX, cwd=host_a)
    assert cached.expires_at == host_lease.now_stamp(T0 + 301 + 600.0)


def test_renew_if_due_is_a_noop_when_the_cache_names_no_lease(host_a):
    assert host_lease.renew_if_due(
        "unused", PREFIX, host_id=HOST_A, cwd=host_a, at=T0
    ) is None


def test_renew_if_due_is_a_noop_when_the_cache_names_another_host(hq_remote, host_a):
    out = _adopt(hq_remote, host_a, HOST_B, ttl=600.0)
    host_lease.cache(PREFIX, out, cwd=host_a)
    assert host_lease.renew_if_due(
        hq_remote, PREFIX, host_id=HOST_A, cwd=host_a, at=T0 + 301
    ) is None


def test_renew_if_due_swallows_an_hq_unreachable_failure_and_logs(
    hq_remote, host_a, tmp_path, monkeypatch
):
    """A REAL unreachable remote (a path that never existed) — not a mocked return code — so
    the failure genuinely exercises gitref's subprocess-failure path."""
    out = _adopt(hq_remote, host_a, HOST_A, ttl=600.0)
    host_lease.cache(PREFIX, out, cwd=host_a)
    bogus_remote = str(tmp_path / "does-not-exist.git")

    seen: list[tuple] = []

    class _Recorder:
        def warning(self, event, **kw):
            seen.append((event, kw))

    monkeypatch.setattr(host_lease.log, "get_logger", lambda *_a, **_k: _Recorder())

    result = host_lease.renew_if_due(
        bogus_remote, PREFIX, host_id=HOST_A, cwd=host_a, renew_interval=300.0, at=T0 + 301,
    )

    assert result is None
    assert seen and seen[0][0] == "host_lease_renew_if_due_failed"
    # the cache is UNCHANGED — a failed renewal must never fraudulently extend the expiry
    cached = host_lease.read_cached(PREFIX, cwd=host_a)
    assert cached.expires_at == host_lease.now_stamp(T0 + 600.0)


def test_renew_if_due_swallows_a_lost_cas_when_another_host_already_took_over(
    hq_remote, host_a, host_b
):
    """The lease-side counterpart to an unreachable HQ: HQ IS reachable, but another host has
    already taken over, so our own-value CAS loses. Also swallowed, also logged, also leaves
    the (now-stale) local cache exactly as it was — its own natural expiry is still what
    decides when THIS host's guard_primary starts refusing, never this function's outcome."""
    out = _adopt(hq_remote, host_a, HOST_A, ttl=600.0)
    host_lease.cache(PREFIX, out, cwd=host_a)
    host_lease.takeover(
        hq_remote, PREFIX, host_id=HOST_B, label="desk", cwd=host_b, at=T0 + 1, force=True
    )

    result = host_lease.renew_if_due(
        hq_remote, PREFIX, host_id=HOST_A, cwd=host_a, renew_interval=300.0, at=T0 + 301,
    )

    assert result is None
    cached = host_lease.read_cached(PREFIX, cwd=host_a)
    assert cached.host_id == HOST_A  # unchanged: A's stale-but-not-yet-expired local view


# ---- configurable renew interval + TTL ----------------------------------------------


def test_the_config_keys_exist_with_the_adr_defaults():
    """The exact key names bh-ytbb.11's renewal loop reads: `host.lease.renew_interval` and
    `host.lease.ttl`, defaulting to 5 min / 30 min (ADR Amendment 1 §3)."""
    keys = config_schema.known_keys()
    assert "host.lease.renew_interval" in keys
    assert "host.lease.ttl" in keys
    assert config.host_lease_renew_interval({}) == 300.0
    assert config.host_lease_ttl({}) == 1800.0


def test_the_config_keys_are_overridable():
    cfg = {"host": {"lease": {"renew_interval": 60.0, "ttl": 240.0}}}
    assert config.host_lease_renew_interval(cfg) == 60.0
    assert config.host_lease_ttl(cfg) == 240.0


def test_the_lease_keys_are_fleet_scoped_not_host_scoped():
    """Load-bearing: two hosts disagreeing about expiry would disagree about who may write."""
    assert config_partition.partition_of("host.lease.ttl") == config_partition.FLEET
    assert config_partition.partition_of("host.lease.renew_interval") == config_partition.FLEET


def test_ttl_scales_with_the_hosts_role():
    """Amendment 1 §3 answers the TTL trade-off with a ROLE, not a better number."""
    base = 1800.0
    assert host_lease.ttl_for_role("adopt-on-demand", base) == base
    assert host_lease.ttl_for_role("primary-default", base) > base
    assert host_lease.ttl_for_role("some-future-role", base) == base  # never unbounded


def test_a_worker_role_can_never_take_a_lease():
    with pytest.raises(host_lease.HostLeaseRejected, match="worker"):
        host_lease.ttl_for_role("worker")
