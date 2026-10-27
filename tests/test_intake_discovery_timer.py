"""The opt-in host-daemon discovery timer (bh-df64b): off by default, on a MOCKED clock.

``DiscoveryTimer.poll`` is one scheduling decision against an injected monotonic clock, so every
case moves a fake clock by hand: nothing here reads the host clock, sleeps for real or opens a
socket. The ``run`` loop is driven with a fake ``sleep`` that advances the same clock. The pass
over the hives is a fake ``discover`` (the real one is covered by the composition tests), so the
schedule, the full-pass cadence, failure isolation and the daemon wiring are proven in isolation.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from beadhive import host_daemon, intake_discovery, intake_services, registry
from beadhive.intake_discovery import DiscoveryRefused, DiscoveryTimer, HiveRun, build_loop

ON = {
    "intake": {
        "external": {
            "enabled": True,
            "discovery": {"enabled": True, "interval_seconds": 600, "full_reconcile_every": 3},
        }
    }
}
WIRED = object()  # a stand-in for the process's TrackerAdapters: the timer only passes it on


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def ok(full: bool = False) -> intake_services.DiscoverResult:
    return intake_services.DiscoverResult(0, full=full)


class Passes:
    """A fake pass over the hives that records which ticks were full."""

    def __init__(self) -> None:
        self.calls: list[bool] = []

    def __call__(self, full: bool):
        self.calls.append(full)
        return [HiveRun("g/o/r", ok(full))]


def timer(passes, clock, *, every: int = 3, interval: float = 600.0) -> DiscoveryTimer:
    return DiscoveryTimer(passes, interval, every, clock, clock.sleep)


# -- off by default --


@pytest.mark.parametrize(
    "cfg",
    [
        None,
        {},
        {"intake": {"external": {"enabled": True}}},
        {"intake": {"external": {"enabled": True, "discovery": {"enabled": False}}}},
        {"intake": {"external": {"enabled": True, "discovery": {"enabled": True}}}},
        {
            "intake": {
                "external": {
                    "enabled": False,
                    "discovery": {"enabled": True, "interval_seconds": 60},
                }
            }
        },
    ],
    ids=["none", "empty", "intake-only", "timer-off", "no-interval", "intake-off"],
)
def test_no_timer_is_built_unless_everything_is_explicitly_on(cfg) -> None:
    assert build_loop(cfg, adapters=WIRED) is None
    assert host_daemon._intake_discovery_loop(cfg or {}) is None


def test_enabling_the_timer_without_wired_credentials_refuses_startup_not_silently() -> None:
    with pytest.raises(DiscoveryRefused, match="bh-kzs0b"):
        build_loop(ON)  # process_adapters() is None until the credential wiring bead lands
    with pytest.raises(host_daemon.DaemonError, match="bh-kzs0b"):
        host_daemon._intake_discovery_loop(ON)


def test_an_enabled_timer_is_built_from_the_chosen_settings() -> None:
    built = build_loop(ON, adapters=WIRED)

    assert isinstance(built, DiscoveryTimer)
    assert (built.interval, built.full_reconcile_every) == (600.0, 3)
    assert built.ticks == 0  # building it runs nothing


# -- the schedule, on a mocked clock --


def test_it_runs_on_schedule_and_never_between_ticks() -> None:
    clock, passes = Clock(), Passes()
    tick = timer(passes, clock)

    assert tick.due() and tick.poll() is not None  # the first poll is due at once
    assert passes.calls == [False] and not tick.due()
    clock.now += 599.9
    assert tick.poll() is None and passes.calls == [False]  # not yet
    assert tick.seconds_until_due() == pytest.approx(0.1)
    clock.now += 0.1
    assert tick.due() and tick.poll() is not None
    assert passes.calls == [False, False] and tick.ticks == 2


def test_a_long_gap_runs_once_not_a_burst() -> None:
    clock, passes = Clock(), Passes()
    tick = timer(passes, clock)
    tick.poll()

    clock.now += 600.0 * 10  # the host slept through ten intervals
    assert tick.poll() is not None
    assert tick.poll() is None  # and is not owed the missed ones

    assert passes.calls == [False, False]


def test_every_nth_run_is_a_full_pass() -> None:
    clock, passes = Clock(), Passes()
    tick = timer(passes, clock, every=3)

    for _ in range(7):
        tick.poll()
        clock.now += 600.0

    assert passes.calls == [False, False, True, False, False, True, False]


def test_every_run_full_when_the_cadence_is_one() -> None:
    clock, passes = Clock(), Passes()
    tick = timer(passes, clock, every=1)

    tick.poll()
    clock.now += 600.0
    tick.poll()

    assert passes.calls == [True, True]


def test_a_failing_run_never_stops_the_schedule(caplog) -> None:
    clock = Clock()
    calls: list[bool] = []

    def explode(full: bool):
        calls.append(full)
        raise RuntimeError("bd is down")

    tick = timer(explode, clock)
    with caplog.at_level(logging.ERROR):
        assert tick.poll() == ()
        assert tick.poll() is None  # the next run is still a whole interval away
        clock.now += 600.0
        assert tick.poll() == ()

    assert calls == [False, False] and tick.errors == 2 and tick.ticks == 2


def test_a_hives_refusal_is_counted_unless_it_is_the_normal_skip(caplog) -> None:
    clock = Clock()
    results = [
        HiveRun("a", intake_services.DiscoverResult(1, "no tracker", "intake_disabled")),
        HiveRun("b", intake_services.DiscoverResult(1, "not primary", "not_primary")),
        HiveRun("c", intake_services.DiscoverResult(1, "held", "intake_busy")),
        HiveRun("d", intake_services.DiscoverResult(5, "bd failed", "store_error")),
        HiveRun("e", ok()),
    ]
    tick = timer(lambda full: results, clock)

    with caplog.at_level(logging.WARNING):
        tick.poll()

    assert tick.errors == 1  # only the store error is worth a warning
    assert "d refused: bd failed" in caplog.text and "no tracker" not in caplog.text


# -- the pass over the hives --


def test_the_pass_discovers_every_hive_with_the_actor_the_wiring_and_the_full_flag(
    monkeypatch,
) -> None:
    entries = [
        {"provider": "g", "org": "o", "repo": "one", "prefix": "a"},
        {"provider": "g", "org": "o", "repo": "two", "prefix": "b"},
    ]
    cfg = {
        **ON,
        "managed_repos": [*entries, {"kind": "hq", "provider": "g", "org": "o", "repo": "hq"}],
    }
    monkeypatch.setattr(registry, "hive_dir", lambda e: Path("/hives") / e["repo"])
    seen: list[tuple] = []

    def fake(cfg_, entry, directory, actor, **kwargs):
        seen.append((entry["repo"], directory, actor, kwargs))
        return ok(kwargs["full"])

    clock = Clock()
    built = build_loop(cfg, adapters=WIRED, discover=fake, clock=clock, sleep=clock.sleep)
    assert built is not None

    runs = built.poll()

    assert runs is not None and [r.hive for r in runs] == ["g/o/one", "g/o/two"]  # not the HQ
    assert [(repo, str(d), actor) for repo, d, actor, _ in seen] == [
        ("one", "/hives/one", intake_discovery.ACTOR),
        ("two", "/hives/two", intake_discovery.ACTOR),
    ]
    assert all(
        k == {"full": False, "adapters": WIRED, "hive": "g/o/" + repo} for repo, _, _, k in seen
    )


def test_one_hive_raising_never_stops_the_others(monkeypatch) -> None:
    cfg = {
        **ON,
        "managed_repos": [
            {"provider": "g", "org": "o", "repo": "bad", "prefix": "a"},
            {"provider": "g", "org": "o", "repo": "good", "prefix": "b"},
        ],
    }
    monkeypatch.setattr(registry, "hive_dir", lambda e: Path("/hives") / e["repo"])

    def fake(cfg_, entry, directory, actor, **kwargs):
        if entry["repo"] == "bad":
            raise OSError("checkout missing")
        return ok()

    built = build_loop(cfg, adapters=WIRED, discover=fake)
    assert built is not None

    runs = built.poll()

    assert runs is not None
    assert [(r.hive, r.result.code, r.result.reason) for r in runs] == [
        ("g/o/bad", 1, "error"),
        ("g/o/good", 0, ""),
    ]


# -- the async loop and the daemon component, still without a real sleep --


def test_the_run_loop_polls_then_waits_the_remaining_interval() -> None:
    clock, passes = Clock(), Passes()
    tick = timer(passes, clock, every=2)

    async def drive() -> None:
        real_sleep = clock.sleep

        async def stop_after_three(seconds: float) -> None:
            await real_sleep(seconds)
            if len(clock.sleeps) == 3:
                tick._closed = True

        tick.sleep = stop_after_three
        await tick.run()

    asyncio.run(drive())

    assert passes.calls == [False, True, False]
    assert clock.sleeps == [600.0, 600.0, 600.0] and tick.ticks == 3


def test_the_component_starts_the_loop_and_cancels_it_on_shutdown() -> None:
    clock = Clock()
    started = asyncio.Event
    passes = Passes()
    tick = timer(passes, clock)

    async def drive() -> None:
        parked = started()

        async def park(seconds: float) -> None:  # the loop waits here, never on the host clock
            parked.set()
            await asyncio.Event().wait()

        tick.sleep = park
        component = tick.component()
        assert component.name == "intake-discovery"
        async with component.lifespan(None):
            await parked.wait()
            assert passes.calls == [False]  # it polled once before waiting
        assert tick._closed is True

    asyncio.run(drive())


def test_the_daemon_application_runs_the_timer_as_a_lifespan_component(tmp_path: Path) -> None:
    """``build_product_application`` starts the loop's component with the daemon and stops it."""
    from contextlib import asynccontextmanager

    from beadhive import daemon_state_broker
    from beadhive.kernel.lifecycle import HostDaemonLifespanComponent, HostDaemonShutdownPhase

    events: list[str] = []

    class Loop:
        def component(self):
            @asynccontextmanager
            async def lifespan(_app):
                events.append("start")
                try:
                    yield
                finally:
                    events.append("stop")

            return HostDaemonLifespanComponent(
                name="intake-discovery",
                lifespan=lifespan,
                shutdown_phase=HostDaemonShutdownPhase.CANCEL_PROCESSES,
            )

    record = host_daemon.ControlRecord(
        contract=host_daemon.CONTRACT_VERSION,
        account_id="uid:1234",
        bh_home=str(tmp_path),
        host_id="host-1",
        instance_id="instance-1",
        pid=1234,
        process_start="test:1",
        listener_host="127.0.0.1",
        listener_port=8737,
        started_at="2026-08-24T00:00:00Z",
    )

    def application(**kwargs):
        return host_daemon.build_product_application(
            runtime=host_daemon.DaemonRuntime(),
            state_broker_factory=daemon_state_broker.DaemonStateBroker.for_host,
            control_record=record,
            listener_host="127.0.0.1",
            listener_port=8737,
            cfg={"managed_repos": []},
            **kwargs,
        )

    async def run(app) -> None:
        async with app.router.lifespan_context(app):
            events.append("serving")

    asyncio.run(run(application(intake_discovery_loop=Loop())))
    assert events == ["start", "serving", "stop"]

    events.clear()
    asyncio.run(run(application()))  # off by default: nothing is started
    assert events == ["serving"]
