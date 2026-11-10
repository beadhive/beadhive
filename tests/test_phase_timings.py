"""The measured batch ``timings`` block (``bh-17g6b``): its shape, and that the seams feed it.

Every duration here comes from an injected fake clock that the test advances by hand, so the
assertions are on the arithmetic of the collector (exclusive phases, holds, the document) and on
which seams record what; none of them waits for, or measures, real time (``bh-91j1j``).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from beadhive import intake_services, phase_timings
from beadhive.modules.tracker.contracts import observe_begin, observe_call, observe_end
from beadhive.phase_timings import PHASES, Timings


class Clock:
    """A hand-driven monotonic clock."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


def make(clock: Clock, **kwargs) -> Timings:
    return Timings(clock=clock, **kwargs)


def assert_batch_timings(document: dict) -> None:
    """The shape every batch verb's ``--json`` ``timings`` block has (the verbs' own tests call
    it), whatever the numbers: keys, types and that every phase is present."""
    assert set(document) == {
        "schema",
        "clock",
        "startup",
        "total_ms",
        "unaccounted_ms",
        "phases",
        "holds",
        "holds_omitted",
    }
    assert document["schema"] == 1 and document["clock"] == "monotonic"
    assert set(document["startup"]) == {
        "source",
        "ms",
        "interpreter_to_entry_ms",
        "entry_to_first_work_ms",
    }
    assert isinstance(document["total_ms"], int | float) and document["total_ms"] >= 0
    assert set(document["phases"]) == set(PHASES)  # the CLI sorts keys: no order here
    for row in document["phases"].values():
        assert set(row) == {"count", "ms"} and isinstance(row["count"], int)
    assert isinstance(document["holds"], list) and isinstance(document["holds_omitted"], int)
    for hold in document["holds"]:
        assert set(hold) == {"binding", "ms", "phases"}


# -- the document --------------------------------------------------------------------------------


def test_an_empty_document_carries_every_phase_at_zero(clock) -> None:
    document = make(clock).document()

    assert_batch_timings(document)
    assert list(document["phases"]) == list(PHASES)
    assert all(row == {"count": 0, "ms": 0.0} for row in document["phases"].values())
    assert (document["total_ms"], document["unaccounted_ms"]) == (0.0, 0.0)
    assert document["holds"] == [] and document["holds_omitted"] == 0
    json.dumps(document)  # plain JSON all the way down


def test_the_startup_is_unknown_under_an_injected_clock_and_known_when_given(clock) -> None:
    unknown = make(clock).document()["startup"]
    assert unknown == {
        "source": "entry",
        "ms": None,
        "interpreter_to_entry_ms": None,
        "entry_to_first_work_ms": None,
    }

    clock.advance(0)
    given = Timings(clock=clock, entry=clock() - 1.5, interpreter_age=2.0).document()["startup"]
    assert given == {
        "source": "interpreter",
        "ms": 3500.0,
        "interpreter_to_entry_ms": 2000.0,
        "entry_to_first_work_ms": 1500.0,
    }
    no_proc = Timings(clock=clock, entry=clock() - 1.5, interpreter_age=None).document()["startup"]
    assert no_proc["source"] == "entry" and no_proc["ms"] == 1500.0
    assert no_proc["interpreter_to_entry_ms"] is None


def test_the_real_collector_reports_a_startup_from_the_entry_mark() -> None:
    startup = Timings().document()["startup"]

    assert startup["entry_to_first_work_ms"] is not None
    assert startup["source"] in {"interpreter", "entry"}


def test_phases_count_and_total_and_the_total_less_them_is_unaccounted(clock) -> None:
    timings = make(clock)
    with timings.phase("config_load"):
        clock.advance(0.25)
    for step in (0.1, 0.3):
        with timings.phase("store_read"):
            clock.advance(step)
    clock.advance(0.5)  # no phase claims this

    document = timings.document()

    assert document["phases"]["config_load"] == {"count": 1, "ms": 250.0}
    assert document["phases"]["store_read"] == {"count": 2, "ms": 400.0}
    assert document["total_ms"] == 1150.0 and document["unaccounted_ms"] == 500.0


def test_a_phase_inside_another_is_subtracted_from_it(clock) -> None:
    timings = make(clock)
    with timings.phase("wiring"):
        clock.advance(0.2)
        with timings.phase("store_open"):
            clock.advance(0.3)
        clock.advance(0.1)

    phases = timings.document()["phases"]

    assert phases["wiring"] == {"count": 1, "ms": 300.0}
    assert phases["store_open"] == {"count": 1, "ms": 300.0}
    assert timings.document()["unaccounted_ms"] == 0.0


def test_a_phase_that_raises_is_still_recorded(clock) -> None:
    timings = make(clock)
    with pytest.raises(RuntimeError), timings.phase("store_write"):
        clock.advance(0.5)
        raise RuntimeError("boom")

    assert timings.document()["phases"]["store_write"] == {"count": 1, "ms": 500.0}


def test_a_hold_lists_its_own_phases_and_still_counts_them_in_the_totals(clock) -> None:
    timings = make(clock)
    with timings.hold("github:acme/widget"):
        with timings.phase("host_check"):
            clock.advance(1.0)
        with timings.phase("github_request"):
            clock.advance(0.5)
    with timings.hold("github:acme/widget"):
        timings.record("host_check_reused", 0.0)

    document = timings.document()

    first, second = document["holds"]
    assert first["binding"] == "github:acme/widget" and first["ms"] == 1500.0
    assert first["phases"] == {
        "host_check": {"count": 1, "ms": 1000.0},
        "github_request": {"count": 1, "ms": 500.0},
    }
    assert second["phases"] == {"host_check_reused": {"count": 1, "ms": 0.0}}
    assert document["phases"]["host_check"] == {"count": 1, "ms": 1000.0}
    assert document["phases"]["host_check_reused"]["count"] == 1


def test_a_phase_after_a_hold_is_not_attributed_to_it_and_nested_holds_restore(clock) -> None:
    timings = make(clock)
    with timings.hold("outer"):
        with timings.hold("inner"):
            timings.record("store_read", 0.1)
        timings.record("store_write", 0.2)  # back in the outer hold
    timings.record("github_request", 0.3)  # after every hold

    holds = {h["binding"]: h["phases"] for h in timings.document()["holds"]}

    assert set(holds["inner"]) == {"store_read"}
    assert set(holds["outer"]) == {"store_write"}
    assert timings.document()["phases"]["github_request"]["count"] == 1


def test_a_refused_hold_is_listed_with_what_it_spent_before_refusing(clock, tmp_path) -> None:
    class Refuse:
        def __call__(self) -> int:
            clock.advance(1.0)
            raise intake_services.IntakeRefused(
                intake_services.RefusalReason.NOT_PRIMARY, "not this host"
            )

    lock = intake_services.FlockIntakeLock(tmp_path / "locks", admit=Refuse(), clock=clock)
    with phase_timings.recording(make(clock)) as timings:
        with pytest.raises(intake_services.IntakeRefused), lock.hold("k"):
            pass

    (hold,) = timings.document()["holds"]  # type: ignore[union-attr]
    assert hold["phases"]["host_check"] == {"count": 1, "ms": 1000.0}


def test_the_listed_holds_are_capped_and_the_rest_counted(clock, monkeypatch) -> None:
    monkeypatch.setattr(phase_timings, "HOLD_LIMIT", 2)
    timings = make(clock)
    for _ in range(5):
        with timings.hold("k"):
            pass

    document = timings.document()

    assert len(document["holds"]) == 2 and document["holds_omitted"] == 3


# -- the context ---------------------------------------------------------------------------------


def test_nothing_is_current_outside_a_recording_and_nothing_leaks_after_one(clock) -> None:
    assert phase_timings.current() is None
    with phase_timings.recording(make(clock)) as timings:
        assert phase_timings.current() is timings
        with phase_timings.phase("config_load"):
            clock.advance(0.1)
    assert phase_timings.current() is None
    with phase_timings.phase("config_load"):  # not measuring: a plain no-op
        pass
    assert timings is not None and timings.document()["phases"]["config_load"]["count"] == 1


def test_a_recording_is_reset_when_the_command_raises(clock) -> None:
    with pytest.raises(RuntimeError), phase_timings.recording(make(clock)):
        raise RuntimeError("boom")

    assert phase_timings.current() is None
    assert observe_begin("github_request") is None  # the adapter's observer is gone too


def test_two_recordings_one_after_the_other_share_nothing(clock) -> None:
    with phase_timings.recording(make(clock)) as first:
        with phase_timings.phase("store_read"):
            clock.advance(1)
    with phase_timings.recording(make(clock)) as second:
        pass

    assert first is not second and second is not None
    assert second.document()["phases"]["store_read"]["count"] == 0


def test_recording_nothing_installs_nothing() -> None:
    with phase_timings.recording(None) as timings:
        assert timings is None and phase_timings.current() is None


def test_the_adapters_marks_land_in_the_github_phases(clock) -> None:
    with phase_timings.recording(make(clock)) as timings:
        observe_call("github_token", clock.advance, 0.07)
        observe_call("github_client", clock.advance, 0.2)
        for _ in range(3):
            mark = observe_begin("github_request")
            clock.advance(0.35)
            observe_end(mark)

    phases = timings.document()["phases"]  # type: ignore[union-attr]
    assert phases["github_token"] == {"count": 1, "ms": 70.0}
    assert phases["github_client"] == {"count": 1, "ms": 200.0}
    assert phases["github_request"] == {"count": 3, "ms": 1050.0}


# -- the store's I/O -----------------------------------------------------------------------------


class Routes:
    """Stands in for the ``bd`` CLI routes: reads and writes by the names the store calls."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.label = "routes"

    def issue_list(self) -> str:
        self._clock.advance(0.5)
        return "listed"

    def issue_close(self, bead: str) -> str:
        self._clock.advance(1.5)
        return f"closed {bead}"


def test_store_routes_are_timed_as_reads_and_writes_when_measuring(clock) -> None:
    with phase_timings.recording(make(clock)) as timings:
        routes = phase_timings.timed_routes(Routes(clock))
        assert routes.issue_list() == "listed" and routes.issue_close("b-1") == "closed b-1"
        assert routes.label == "routes"  # not a method: passed through untouched

    phases = timings.document()["phases"]  # type: ignore[union-attr]
    assert phases["store_read"] == {"count": 1, "ms": 500.0}
    assert phases["store_write"] == {"count": 1, "ms": 1500.0}


def test_store_routes_and_the_api_are_returned_as_they_are_when_not_measuring(clock) -> None:
    routes, api = Routes(clock), SimpleNamespace(issue=lambda bead: bead)

    assert phase_timings.timed_routes(routes) is routes
    assert phase_timings.timed_api(api) is api
    assert phase_timings.timed_api(None) is None
    select = lambda: api  # noqa: E731
    assert phase_timings.timed_selector(select) is select


def test_the_route_decision_is_store_open_and_the_api_it_returns_is_timed(clock) -> None:
    class Api:
        def issue(self, bead: str) -> str:
            clock.advance(0.25)
            return bead

        def update(self, bead: str) -> str:
            clock.advance(0.75)
            return bead

    api = Api()

    def select() -> Api:
        clock.advance(2.0)  # opening the service session
        return api

    with phase_timings.recording(make(clock)) as timings:
        selected = phase_timings.timed_selector(select)
        first = selected()
        assert selected() is first  # one proxy for one api
        first.issue("b-1")
        first.update("b-1")

    phases = timings.document()["phases"]  # type: ignore[union-attr]
    assert phases["store_open"] == {"count": 1, "ms": 2000.0}
    assert phases["store_read"] == {"count": 1, "ms": 250.0}
    assert phases["store_write"] == {"count": 1, "ms": 750.0}


def test_a_cli_route_selection_is_store_open_and_has_no_api(clock) -> None:
    with phase_timings.recording(make(clock)) as timings:
        selected = phase_timings.timed_selector(lambda: None)
        assert selected() is None and selected() is None

    assert timings.document()["phases"]["store_open"]["count"] == 1  # type: ignore[union-attr]


def test_the_report_store_is_built_over_timed_routes_only_while_measuring(
    clock, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(intake_services.bd_cli, "routes", lambda directory: Routes(clock))

    plain = intake_services.report_store(tmp_path, lambda: None, cli_only=True)
    with phase_timings.recording(make(clock)):
        measured = intake_services.report_store(tmp_path, lambda: None, cli_only=True)
        measured._routes.issue_list()

    assert isinstance(plain._routes, Routes)
    assert not isinstance(measured._routes, Routes)


# -- the intake lock -----------------------------------------------------------------------------


class Admit:
    def __init__(self, clock: Clock, cost: float) -> None:
        self._clock, self._cost = clock, cost
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        self._clock.advance(self._cost)
        return 7


def test_a_hold_records_the_host_check_the_flock_and_a_hold_entry(clock, tmp_path) -> None:
    admit = Admit(clock, 1.85)
    lock = intake_services.FlockIntakeLock(tmp_path / "locks", admit=admit, clock=clock)

    with phase_timings.recording(make(clock)) as timings:
        with lock.hold("github:acme/widget") as epoch:
            clock.advance(0.5)
    document = timings.document()  # type: ignore[union-attr]

    assert epoch == 7
    assert document["phases"]["host_check"] == {"count": 1, "ms": 1850.0}
    assert document["phases"]["intake_lock"]["count"] == 1
    (hold,) = document["holds"]
    assert hold["binding"] == "github:acme/widget" and hold["ms"] == 2350.0
    assert set(hold["phases"]) == {"host_check", "intake_lock"}


def test_a_batch_that_reuses_the_check_counts_the_reuse(clock, tmp_path) -> None:
    admit = Admit(clock, 1.0)
    lock = intake_services.FlockIntakeLock(
        tmp_path / "locks", admit=admit, recheck_seconds=300.0, clock=clock
    )

    with phase_timings.recording(make(clock)) as timings:
        with lock.batch():
            for key in ("a", "b", "c"):
                with lock.hold(key):
                    pass
    document = timings.document()  # type: ignore[union-attr]

    assert admit.calls == 1
    assert document["phases"]["host_check"]["count"] == 1
    assert document["phases"]["host_check_reused"]["count"] == 2
    assert document["phases"]["intake_lock"]["count"] == 3
    assert len(document["holds"]) == 3


def test_a_hold_outside_a_recording_records_nothing_and_still_holds(clock, tmp_path) -> None:
    admit = Admit(clock, 1.0)
    lock = intake_services.FlockIntakeLock(tmp_path / "locks", admit=admit, clock=clock)

    with lock.hold("k") as epoch:
        pass

    assert epoch == 7 and admit.calls == 1
