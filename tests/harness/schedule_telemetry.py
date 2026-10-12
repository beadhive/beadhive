"""Opt-in xdist schedule and Dolt-slot telemetry (bh-xqtp8). Off unless asked for.

``BH_TEST_SCHEDULE_TELEMETRY=1`` records, for one pytest run:

* per test: which worker ran it, when its protocol started and ended, and its setup / call /
  teardown durations;
* per Dolt slot acquisition (``harness.world.dolt_server_slot``): the queue wait, the hold, and
  where in the owning test the slot was taken and released;
* per worker: session start, collection finish, and therefore its idle time — startup before the
  first test, gaps between tests, and the tail after its last test;
* on the controller: a host sample (load average, MemAvailable, live ``dolt`` processes) every
  few seconds, so a slot-cap change can be argued from headroom instead of guessed.

The summary goes to ``<junit stem>.schedule.json`` next to the JUnit report when there is one
(the validation run's ``reports/`` dir), else to the path the variable names when it is not a
plain truthy value. A one-line digest is printed in the terminal summary either way.

Events are appended to one JSONL file under the run's TMPDIR, which every xdist worker of the run
shares (the same mechanism ``BH_DOLT_SLOT_EVENTS`` already used), and folded by the controller.
``time.monotonic`` is a system-wide clock on Linux and macOS, so worker timestamps compare.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

ENV = "BH_TEST_SCHEDULE_TELEMETRY"
EVENTS_ENV = "BH_TEST_SCHEDULE_EVENTS"
SLOT_EVENTS_ENV = "BH_DOLT_SLOT_EVENTS"
SAMPLE_SECONDS = 5.0
_TRUE = frozenset({"1", "true", "yes", "on"})
_MAX_EVENTS_BYTES = 32 * 1024 * 1024
# Bound at import, before any test runs: tests monkeypatch ``time.monotonic`` with finite fake
# clocks, and the protocol hook's ``test_end`` event fires while that patch is still in force
# (a StopIteration there killed the worker with INTERNALERROR, bh-518dc).
_monotonic = time.monotonic


def enabled(env: Mapping[str, str] | None = None) -> bool:
    value = (os.environ if env is None else env).get(ENV, "").strip()
    return bool(value) and value.lower() not in {"0", "false", "no", "off"}


def output_path(xmlpath: str | None, env: Mapping[str, str] | None = None) -> Path | None:
    """Next to the JUnit report when there is one; else an explicit path in the variable."""
    if xmlpath:
        junit = Path(xmlpath)
        return junit.with_name(f"{junit.stem}.schedule.json")
    value = (os.environ if env is None else env).get(ENV, "").strip()
    if value and value.lower() not in _TRUE:
        return Path(value)
    return None


def worker_id() -> str:
    return os.environ.get("PYTEST_XDIST_WORKER", "main")


def emit(kind: str, path: str | None = None, **fields: Any) -> None:
    """Append one event; never raises (telemetry must not be the thing that fails a run)."""
    target = path or os.environ.get(EVENTS_ENV)
    if not target:
        return
    payload = {"event": kind, "worker": worker_id(), "monotonic": _monotonic(), **fields}
    with contextlib.suppress(OSError):
        handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            if os.fstat(handle).st_size < _MAX_EVENTS_BYTES:
                os.write(handle, (json.dumps(payload) + "\n").encode())
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)


def read_events(paths: Iterable[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        with contextlib.suppress(OSError):
            for line in path.read_text(encoding="utf-8").splitlines():
                with contextlib.suppress(ValueError):
                    rows.append(json.loads(line))
    return rows


def _round(value: float) -> float:
    return round(value, 3)


def _occupancy(intervals: list[tuple[float, float]]) -> tuple[int, float]:
    """Peak concurrency and the time-weighted mean concurrency over the intervals' span."""
    if not intervals:
        return 0, 0.0
    edges = sorted([(start, 1) for start, _ in intervals] + [(end, -1) for _, end in intervals])
    peak = current = 0
    area = 0.0
    last = edges[0][0]
    for at, delta in edges:
        area += current * (at - last)
        last = at
        current += delta
        peak = max(peak, current)
    span = edges[-1][0] - edges[0][0]
    return peak, (area / span if span > 0 else 0.0)


def _saturated_seconds(
    holds: list[tuple[float, float]], waits: list[tuple[float, float]], slots: int
) -> float:
    """Seconds during which every slot was held AND at least one test queued for one."""
    if slots <= 0 or not waits:
        return 0.0
    edges = sorted(
        [(s, 0, 1) for s, _ in holds]
        + [(e, 0, -1) for _, e in holds]
        + [(s, 1, 1) for s, _ in waits]
        + [(e, 1, -1) for _, e in waits]
    )
    held = queued = 0
    total = 0.0
    last = edges[0][0]
    for at, kind, delta in edges:
        if held >= slots and queued > 0:
            total += at - last
        last = at
        if kind == 0:
            held += delta
        else:
            queued += delta
    return total


def summarize(events: list[dict[str, Any]], slots: int, workers: int) -> dict[str, Any]:
    """Fold raw events into per-test, per-worker and run-level timing. Pure; unit-tested."""
    start = next((e["monotonic"] for e in events if e["event"] == "controller_start"), None)
    end = next((e["monotonic"] for e in events if e["event"] == "controller_end"), None)
    stamps = [e["monotonic"] for e in events]
    if start is None:
        start = min(stamps, default=0.0)
    if end is None:
        end = max(stamps, default=start)
    tests: dict[tuple[str, str], dict[str, Any]] = {}
    for e in events:
        key = (e.get("worker", "main"), e.get("nodeid", ""))
        if e["event"] == "test_start":
            tests[key] = {"nodeid": e["nodeid"], "worker": key[0], "start": e["monotonic"]}
        elif e["event"] == "test_end" and key in tests:
            tests[key]["end"] = e["monotonic"]
        elif e["event"] == "phase" and key in tests:
            tests[key][e["when"]] = e["duration"]
    done = sorted((t for t in tests.values() if "end" in t), key=lambda t: t["start"])

    acquisitions = []
    for e in events:
        if e["event"] != "acquired" or e.get("slot") is None:
            continue
        acquisitions.append(
            {
                "worker": e.get("worker", "main"),
                "label": e.get("test", ""),
                "queued": e["monotonic"] - e.get("queue_seconds", 0.0),
                "acquired": e["monotonic"],
            }
        )
    releases = [e for e in events if e["event"] == "released"]
    for acq in acquisitions:
        match = next(
            (
                r
                for r in releases
                if r.get("worker", "main") == acq["worker"]
                and r.get("test", "") == acq["label"]
                and r["monotonic"] >= acq["acquired"]
            ),
            None,
        )
        if match is not None:
            releases.remove(match)
            acq["released"] = match["monotonic"]
    holds = [(a["acquired"], a["released"]) for a in acquisitions if "released" in a]
    waits = [(a["queued"], a["acquired"]) for a in acquisitions]

    per_test = []
    for t in done:
        owned = [
            a
            for a in acquisitions
            if a["worker"] == t["worker"] and t["start"] <= a["queued"] <= t["end"]
        ]
        row = {
            "nodeid": t["nodeid"],
            "worker": t["worker"],
            "start": _round(t["start"] - start),
            "duration": _round(t["end"] - t["start"]),
            "setup": _round(t.get("setup", 0.0)),
            "call": _round(t.get("call", 0.0)),
            "teardown": _round(t.get("teardown", 0.0)),
            "slot_wait": _round(sum(a["acquired"] - a["queued"] for a in owned)),
            "slot_hold": _round(sum(a.get("released", t["end"]) - a["acquired"] for a in owned)),
        }
        if owned:
            row["slot_taken_at"] = _round(owned[0]["queued"] - t["start"])
            row["slot_released_before_end"] = _round(
                t["end"] - max(a.get("released", t["end"]) for a in owned)
            )
        per_test.append(row)

    per_worker: dict[str, dict[str, Any]] = {}
    for e in events:
        if e["event"] in {"worker_start", "worker_collected"}:
            per_worker.setdefault(e["worker"], {})[e["event"]] = _round(e["monotonic"] - start)
    for row, t in zip(per_test, done, strict=True):
        w = per_worker.setdefault(row["worker"], {})
        w.setdefault("tests", 0)
        w["tests"] += 1
        w["busy"] = w.get("busy", 0.0) + row["duration"]
        w["slot_wait"] = w.get("slot_wait", 0.0) + row["slot_wait"]
        w["slot_hold"] = w.get("slot_hold", 0.0) + row["slot_hold"]
        w["first_test"] = min(w.get("first_test", row["start"]), row["start"])
        w["last_end"] = max(w.get("last_end", 0.0), t["end"] - start)
        if "_prev_end" in w:
            w["gaps"] = w.get("gaps", 0.0) + max(0.0, t["start"] - w["_prev_end"])
        w["_prev_end"] = t["end"]
    wall = end - start
    for w in per_worker.values():
        w.pop("_prev_end", None)
        w.setdefault("tests", 0)
        busy = w.get("busy", 0.0)
        w["idle"] = wall - busy
        w["startup_idle"] = w.get("first_test", wall)
        w["tail_idle"] = wall - w.get("last_end", wall)
        for key, value in list(w.items()):
            if isinstance(value, float):
                w[key] = _round(value)

    busy_total = sum(r["duration"] for r in per_test)
    wait_total = sum(r["slot_wait"] for r in per_test)
    peak, mean = _occupancy(holds)
    return {
        "schema": 1,
        "workers": workers,
        "dolt_slots": slots,
        "wall": _round(wall),
        "tests": len(per_test),
        "busy_total": _round(busy_total),
        "ideal_wall_busy_over_workers": _round(busy_total / workers) if workers else None,
        "slot_wait_total": _round(wait_total),
        "slot_hold_total": _round(sum(e - s for s, e in holds)),
        "ideal_wall_without_slot_wait": _round((busy_total - wait_total) / workers)
        if workers
        else None,
        "idle_total": _round(sum(w["idle"] for w in per_worker.values())),
        "startup_idle_total": _round(sum(w["startup_idle"] for w in per_worker.values())),
        "gap_idle_total": _round(sum(w.get("gaps", 0.0) for w in per_worker.values())),
        "tail_idle_total": _round(sum(w["tail_idle"] for w in per_worker.values())),
        "slot_peak_held": peak,
        "slot_mean_held": _round(mean),
        "slot_saturated_seconds": _round(_saturated_seconds(holds, waits, slots)),
        "per_worker": dict(sorted(per_worker.items())),
        "per_test": per_test,
        "host": [
            {k: v for k, v in e.items() if k not in {"event", "worker"}}
            | {"t": _round(e["monotonic"] - start)}
            for e in events
            if e["event"] == "host_sample"
        ],
    }


def digest(summary: Mapping[str, Any]) -> str:
    ideal = summary["ideal_wall_busy_over_workers"]
    return (
        f"schedule telemetry: wall {summary['wall']:.0f}s, busy/{summary['workers']} "
        f"{ideal:.0f}s, slot wait {summary['slot_wait_total']:.0f}s "
        f"(saturated {summary['slot_saturated_seconds']:.0f}s, peak {summary['slot_peak_held']}/"
        f"{summary['dolt_slots']}), idle startup {summary['startup_idle_total']:.0f}s "
        f"gaps {summary['gap_idle_total']:.0f}s tail {summary['tail_idle_total']:.0f}s"
    )


def _host_sample() -> dict[str, Any]:
    sample: dict[str, Any] = {}
    with contextlib.suppress(OSError):
        sample["load1"] = os.getloadavg()[0]
    with contextlib.suppress(OSError, ValueError, IndexError):
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                sample["mem_available_mb"] = int(line.split()[1]) // 1024
    with contextlib.suppress(OSError):
        count = 0
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                with contextlib.suppress(OSError):
                    if (entry / "comm").read_text().strip() == "dolt":
                        count += 1
        sample["dolt_processes"] = count
    return sample


class _Sampler(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="bh-schedule-telemetry", daemon=True)
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            emit("host_sample", **_host_sample())
            self.stop.wait(SAMPLE_SECONDS)


_sampler: _Sampler | None = None


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config) -> None:
    """Controller: own one events file and point the slot events at it before workers spawn."""
    if not enabled() or hasattr(config, "workerinput"):
        return
    path = Path(tempfile.gettempdir()) / f"bh-schedule-events-{os.getpid()}.jsonl"
    path.unlink(missing_ok=True)
    os.environ[EVENTS_ENV] = str(path)
    os.environ[SLOT_EVENTS_ENV] = str(path)
    config._bh_schedule_events = path


def pytest_sessionstart(session) -> None:
    if not enabled():
        return
    if hasattr(session.config, "workerinput"):
        emit("worker_start")
        return
    global _sampler
    emit("controller_start")
    _sampler = _Sampler()
    _sampler.start()


def pytest_collection_finish(session) -> None:
    if enabled() and hasattr(session.config, "workerinput"):
        emit("worker_collected", items=len(session.items))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    del nextitem
    if not enabled():
        yield
        return
    emit("test_start", nodeid=item.nodeid)
    try:
        yield
    finally:
        emit("test_end", nodeid=item.nodeid)


def pytest_runtest_logreport(report) -> None:
    # xdist replays every worker report on the controller with ``report.node`` set; the worker
    # already recorded it, so only the side that ran the test emits.
    if enabled() and not hasattr(report, "node"):
        emit("phase", nodeid=report.nodeid, when=report.when, duration=report.duration)


@pytest.hookimpl(trylast=True)
def pytest_terminal_summary(terminalreporter, config) -> None:
    path = getattr(config, "_bh_schedule_events", None)
    if path is None:
        return
    emit("controller_end")
    if _sampler is not None:
        _sampler.stop.set()
    workers = int(getattr(config.option, "numprocesses", 0) or 1)
    slots = int(os.environ.get("BH_DOLT_SLOTS", "8"))
    summary = summarize(read_events([path]), slots=slots, workers=workers)
    terminalreporter.write_line(digest(summary))
    target = output_path(getattr(config.option, "xmlpath", None))
    if target is None:
        terminalreporter.write_line(
            f"schedule telemetry: no JUnit report and {ENV} names no path; summary not saved"
        )
        return
    try:
        target.write_text(json.dumps(summary, indent=1, sort_keys=True), encoding="utf-8")
        terminalreporter.write_line(f"schedule telemetry: wrote {target}")
    except OSError as exc:
        terminalreporter.write_line(f"schedule telemetry: could not write {target}: {exc}")


def pytest_unconfigure(config) -> None:
    path = getattr(config, "_bh_schedule_events", None)
    if path is None:
        return
    if _sampler is not None:
        _sampler.stop.set()
    path.unlink(missing_ok=True)
    for name in (EVENTS_ENV, SLOT_EVENTS_ENV):
        os.environ.pop(name, None)
