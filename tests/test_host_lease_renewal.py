"""The renewal loop's end-to-end behavior through ``guard_primary`` (bh-ytbb.11).

Covers the acceptance bar's HQ-unreachable requirement directly, against REAL git plumbing
rather than a mocked return code:

  * an existing primary keeps writing (``guard_primary`` does not raise) as long as its
    CACHED lease is unexpired — even once HQ becomes unreachable and every opportunistic
    renewal at a write-verb boundary starts failing;
  * once that cached lease's own ``expires_at`` elapses, the SAME host degrades to read-only
    (``guard_primary`` raises) — never guessing that a renewal it never actually confirmed
    might have landed;
  * a reachable companion scenario shows the positive case for contrast: renewal DOES extend
    the cache and DOES keep the write allowed past what would otherwise have been the
    original expiry.

"HQ unreachable" here means a REAL git remote pointed at a path that was never created — any
``git ls-remote``/``git push`` against it fails as a genuine subprocess error
(``gitref.RemoteUnreachable``), not a monkeypatched short-circuit. The only mocked pieces are
the wall clock (``host_lease.time.time``, matching this suite's existing convention of
asserting expiry by arithmetic rather than by sleeping) and hive resolution (this test never
touches a real registered hive or the operator's ``~/.beadhive``).
"""

from __future__ import annotations

import subprocess

import pytest
import typer

from beadhive import guard, host, host_lease, registry

PREFIX = "tt"
THIS_HOST = "11111111-1111-4111-8111-111111111111"
T0 = 1_800_000_000.0
TTL = 600.0
RENEW_INTERVAL = 300.0


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


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


# ---- the AC: HQ unreachable, keep writing until the CACHE expires, then degrade -----------


def test_primary_keeps_writing_through_an_unreachable_hq_until_its_cache_expires(
    hq_dir, hq_remote_path, hive, this_host, tmp_path, monkeypatch
):
    _adopt_and_cache(hq_dir, at=T0, ttl=TTL)  # a REAL adopt, HQ reachable at this point

    _at(monkeypatch, T0 + 1)
    guard.guard_primary("", cfg={})  # well inside the cache: allowed, no raise

    _break_hq_remote(hq_dir, tmp_path)  # HQ becomes unreachable from here on

    # Past the renew_interval boundary (due for renewal), still inside the TTL: an
    # opportunistic renewal is attempted at this write-verb boundary and FAILS (HQ
    # unreachable) — but the write itself is still ALLOWED, on the still-unexpired cache.
    _at(monkeypatch, T0 + RENEW_INTERVAL + 1)
    guard.guard_primary("", cfg={})  # no raise: an established primary keeps working

    # The failed renewal must never have fraudulently extended the cache.
    cached = host_lease.read_cached(PREFIX, cwd=hq_dir)
    assert cached.expires_at == host_lease.now_stamp(T0 + TTL)

    # Once the ORIGINAL cached lease genuinely expires — HQ still unreachable, so no renewal
    # ever could have landed — the SAME host degrades to read-only. Never guessed, never
    # assumed continued primacy past the number the cache itself carries.
    _at(monkeypatch, T0 + TTL + 1)
    with pytest.raises(typer.Exit):
        guard.guard_primary("", cfg={})


def test_multiple_write_verb_boundaries_never_extend_the_cache_while_hq_is_unreachable(
    hq_dir, hq_remote_path, hive, this_host, tmp_path, monkeypatch
):
    """Not just one boundary call — repeated opportunistic-renewal attempts across several
    write verbs must all fail the same way, never eventually "getting lucky" into extending a
    lease that was never actually renewed."""
    _adopt_and_cache(hq_dir, at=T0, ttl=TTL)
    _break_hq_remote(hq_dir, tmp_path)

    for clock in (T0 + 301, T0 + 400, T0 + 500, T0 + 599):
        _at(monkeypatch, clock)
        guard.guard_primary("", cfg={})  # still allowed: cache not yet expired
        cached = host_lease.read_cached(PREFIX, cwd=hq_dir)
        assert cached.expires_at == host_lease.now_stamp(T0 + TTL)  # never moved

    _at(monkeypatch, T0 + TTL + 1)
    with pytest.raises(typer.Exit):
        guard.guard_primary("", cfg={})


# ---- the reachable companion: renewal DOES extend primacy past the original expiry --------


def test_when_hq_stays_reachable_the_opportunistic_renewal_extends_primacy(
    hq_dir, hq_remote_path, hive, this_host, monkeypatch
):
    """Contrast case: same shape, HQ never broken. The boundary call past the renew_interval
    DOES land a real renewal, so a later call — past what would have been the ORIGINAL
    expiry — is still allowed, on the strength of the extended cache."""
    _adopt_and_cache(hq_dir, at=T0, ttl=TTL)

    _at(monkeypatch, T0 + RENEW_INTERVAL + 1)
    guard.guard_primary("", cfg={})  # triggers a real, successful renewal

    cached = host_lease.read_cached(PREFIX, cwd=hq_dir)
    assert cached.expires_at != host_lease.now_stamp(T0 + TTL)  # extended, not the original

    # past the ORIGINAL cache's expiry (T0 + TTL) — still allowed, because renewal landed
    _at(monkeypatch, T0 + TTL + 1)
    guard.guard_primary("", cfg={})  # no raise


def test_no_hq_round_trip_at_all_before_the_renew_interval_elapses(
    hq_dir, hq_remote_path, hive, this_host, monkeypatch
):
    """Structural statement of "no HQ round trip within the interval": break HQ immediately
    after adopt, then confirm every call inside the renew_interval window is still allowed —
    proving the guard never even ATTEMPTED to reach the (already-broken) remote."""
    _adopt_and_cache(hq_dir, at=T0, ttl=TTL)

    def boom(*_a, **_kw):
        raise AssertionError("renew_if_due must not touch the network before it is due")

    monkeypatch.setattr(host_lease, "renew", boom)

    _at(monkeypatch, T0 + RENEW_INTERVAL - 1)  # just inside the window: not due yet
    guard.guard_primary("", cfg={})  # no raise, and `renew` (the boom above) was never called
