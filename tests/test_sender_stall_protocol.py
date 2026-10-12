"""The Φ3 sender-stall soak check (O4) on the HQ storage port's fake, on virtual time (bh-utvyj).

``tests/test_fence_composed_int.py`` ran this against a scratch ``dolt sql-server`` and made
the server's staleness data-driven by restamping the session row before every poll, because the
server clock could not be injected. The HQ storage port's fake owns an injected clock
(``docs/design/hq-storage-port-adr.md`` section 3), so here ONE :class:`FakeClock` is the
sender's cadence, the store clock that stamps every renewal and measures every age, and the
director observer's window. Nothing sleeps and nothing is restamped.

The product pieces are the ones that decide the timing: the renewal loop
(:meth:`beadhive.hq_sql_session.SessionRenewer.run`), the director's port read of the session
age (:func:`beadhive.failover_observer.session_staleness` over ``Liveness.ages``), eligibility's
freshness rule (:func:`beadhive.hq_sql_session.within_ttl`) and the failover decision
(:class:`beadhive.failover_observer.FailoverObserver`). Each renewal is the port's
``Liveness.renew`` of the session row. The SQL those map onto on Dolt — the one-``UPDATE``
renewal and the eligibility statement whose predicate agrees with the server-clock age — is
proven on a real server by ``tests/test_hq_sql_session_int.py``.
"""

from __future__ import annotations

import json
import os

import pytest

from beadhive.failover_observer import FailoverObserver, session_key, session_staleness
from beadhive.hq_sql_session import SessionRenewer, within_ttl
from beadhive_hq_storage import InMemoryHqStorage, LivenessKind
from harness.clock import FakeClock


def test_sender_stall_longer_than_the_ttl_is_stale_but_fails_over_only_past_failover_after():
    """M9's renewal sender stalls (a host under load, a hung timer) longer than
    ``session_ttl_s``: eligibility goes stale at once, but the director's observer (M8) only
    fails the frame over once the stall outlasts ``failover_after``; a stall that ends before
    then is a fresh session again and never a failover. Time decides WHEN to reassign, never
    who may write (the hive's data does that). ``BH_M10_SESSION_TTL_S`` scales the run.

    During the long stall the director polls every ``ttl / 2`` starting ``ttl / 4`` past a
    renewal, so ``failover_after`` sits half a poll from every observed age."""
    ttl = int(os.environ.get("BH_M10_SESSION_TTL_S", "2"))
    failover_after = 4.0 * ttl
    interval = ttl / 4  # the sender's cadence
    step = ttl / 8  # the director's poll step: binary-exact for integer ttl, no float drift
    long_step = ttl / 2  # its step through the long stall, off the failover_after boundary
    clock = FakeClock()
    store = InMemoryHqStorage(clock)  # the store clock IS the virtual clock
    key = session_key("frame_a", 1)
    errors: list[BaseException] = []

    renewer = SessionRenewer(lambda: pytest.fail("the fake renews through the port, not SQL"))
    renewed: list[float] = []  # the virtual instant each renewal was stamped

    def port_tick():
        stamped = store.renew(LivenessKind.SESSION, key)
        assert stamped == clock.now()  # the store stamps it, never the caller
        renewer.renewals += 1
        renewed.append(clock.monotonic())

    renewer.tick = port_tick
    observer = FailoverObserver(failover_after, clock=clock.monotonic)

    def poll() -> tuple[bool, object]:
        age = session_staleness(store, [key])[key]
        assert age == clock.monotonic() - renewed[-1]  # measured by the store clock
        fresh = within_ttl(round(age * 1_000_000), ttl)  # eligibility's freshness rule
        return fresh, observer.observe(age)

    def stalled(seconds: float) -> list[tuple[bool, object]]:
        """The sender is stuck: virtual time passes, the director polls, nothing renews."""
        out = []
        for _ in range(round(seconds / step)):
            clock.advance(step)
            out.append(poll())
        return out

    def renewing(seconds: float) -> list[tuple[bool, object]]:
        """The sender runs on cadence; between its ticks the director polls."""
        out, polls = [], round(seconds / step)

        def stop(wait_s: float) -> bool:
            nonlocal polls
            assert wait_s == interval  # virtual ticks take no time
            for _ in range(round(wait_s / step)):
                clock.advance(step)
                out.append(poll())
                polls -= 1
            return polls <= 0

        renewer.run(interval_s=interval, stop=stop, on_error=errors.append, clock=clock.monotonic)
        return out

    healthy = renewing(2 * ttl)
    assert all(fresh for fresh, _ in healthy) and not any(d.due for _, d in healthy)

    short = stalled(2.5 * ttl)  # stall 1: longer than the TTL, shorter than failover_after
    assert not short[-1][0], "eligibility must go stale past the session TTL"
    assert not any(d.due for _, d in short), "a stall shorter than failover_after"
    recovered = renewing(1.5 * ttl)
    assert recovered[-1][0] and not any(d.due for _, d in recovered)

    # renewing() ends one interval (ttl / 4) after the last renewal.
    assert clock.monotonic() - renewed[-1] == interval
    began, due_after = clock.monotonic(), None  # stall 2: longer than failover_after
    while clock.monotonic() - began < failover_after + 6 * ttl:
        clock.advance(long_step)
        fresh, decision = poll()
        if decision.due:
            due_after = clock.monotonic() - began
            since_renewal = clock.monotonic() - renewed[-1]
            break
    # Measured from the last renewal actually made, on the virtual clock (bh-eeyxt).
    assert due_after is not None, "a stall past failover_after must fail over"
    assert since_renewal == failover_after + interval, since_renewal
    assert decision.staleness > failover_after and decision.window > failover_after
    assert not fresh
    assert clock.sleeps == []  # the fake clock was advanced, never slept on
    assert errors == [] and renewer.renewals == len(renewed) > 0
    print(
        "BH_M10",
        json.dumps(
            {
                "sender_stall": {
                    "session_ttl_s": ttl,
                    "failover_after_s": failover_after,
                    "due_after_stall_s": round(due_after, 2),
                    "renewals": renewer.renewals,
                }
            }
        ),
    )
