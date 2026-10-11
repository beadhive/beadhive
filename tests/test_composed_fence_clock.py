"""The composed-fence harness Director runs on an injected clock (bh-qwbrz)."""

from __future__ import annotations

from dataclasses import dataclass

from harness.clock import FakeClock
from harness.composed_fence import Director
from harness.writer_fencing import Placement


@dataclass
class _HQ:
    """Just what ``Director.due`` reads: placement, the server staleness, reachability."""

    stale: float | None = 0.0
    up: bool = True

    def placement(self, hive: str = "hive") -> Placement:
        return Placement("a", 1)

    def staleness(self, frame: str) -> float | None:
        return self.stale


def test_director_window_is_virtual_time_and_restarts_after_an_unobserved_gap():
    clock, hq = FakeClock(), _HQ(stale=100.0)  # the server says "long dead" throughout
    director = Director(hq, failover_after=4.0, clock=clock.monotonic)
    assert not director.due()  # first look: window 0
    for _ in range(4):
        clock.advance(1.0)
        assert not director.due()  # window 1..4: never more than failover_after
    clock.advance(1.0)
    assert director.due()  # window 5 > 4, staleness 100 > 4

    clock.advance(3.0)  # a gap longer than failover_after / 2: unobserved, window restarts
    assert not director.due()
    hq.up = False
    clock.advance(1.0)
    assert not director.due()  # HQ unreachable: observe nothing
    assert director.observed_since is None
