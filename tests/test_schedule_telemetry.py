"""The schedule-telemetry fold (bh-xqtp8): pure arithmetic over synthetic events, no clock."""

from __future__ import annotations

import json
from pathlib import Path

from harness import schedule_telemetry as st


def _e(kind: str, at: float, worker: str = "gw0", **fields) -> dict:
    return {"event": kind, "monotonic": at, "worker": worker, **fields}


def _run() -> list[dict]:
    """Two workers, one slot. gw0's test holds the slot 10-40; gw1's test queues for it from 12
    and holds 40-60, then gw1 idles until the run ends at 70."""
    return [
        _e("controller_start", 0.0, "main"),
        _e("worker_start", 1.0),
        _e("worker_start", 1.0, "gw1"),
        _e("worker_collected", 5.0, items=2),
        _e("worker_collected", 5.0, "gw1", items=2),
        _e("test_start", 10.0, nodeid="a"),
        _e("acquired", 10.0, test="a", slot=0, queue_seconds=0.0),
        _e("released", 40.0, test="a", slot=0, hold_seconds=30.0),
        _e("test_end", 41.0, nodeid="a"),
        _e("phase", 41.0, nodeid="a", when="call", duration=30.5),
        _e("test_start", 11.0, "gw1", nodeid="b"),
        _e("acquired", 40.0, "gw1", test="b", slot=0, queue_seconds=28.0),
        _e("released", 60.0, "gw1", test="b", slot=0, hold_seconds=20.0),
        _e("test_end", 60.0, "gw1", nodeid="b"),
        _e("host_sample", 30.0, "main", load1=3.0, mem_available_mb=1000, dolt_processes=2),
        _e("controller_end", 70.0, "main"),
    ]


def test_summary_separates_slot_wait_from_work_and_idle():
    summary = st.summarize(_run(), slots=1, workers=2)

    assert summary["wall"] == 70.0
    assert summary["busy_total"] == 80.0  # 31 + 49
    assert summary["slot_wait_total"] == 28.0
    assert summary["ideal_wall_without_slot_wait"] == 26.0
    assert summary["slot_saturated_seconds"] == 28.0
    assert summary["slot_peak_held"] == 1
    gw0, gw1 = summary["per_worker"]["gw0"], summary["per_worker"]["gw1"]
    assert (gw0["startup_idle"], gw0["tail_idle"], gw0["idle"]) == (10.0, 29.0, 39.0)
    assert (gw1["slot_wait"], gw1["slot_hold"], gw1["tail_idle"]) == (28.0, 20.0, 10.0)
    by_id = {row["nodeid"]: row for row in summary["per_test"]}
    assert by_id["a"]["slot_released_before_end"] == 1.0
    assert by_id["b"]["slot_taken_at"] == 1.0
    assert by_id["a"]["call"] == 30.5
    assert summary["host"][0]["dolt_processes"] == 2


def test_unsaturated_queueing_is_not_counted_as_saturation():
    # Two slots for the same pair: nothing ever waits on a full set.
    events = [e for e in _run() if e["event"] not in {"acquired", "released"}]
    events += [
        _e("acquired", 10.0, test="a", slot=0, queue_seconds=0.0),
        _e("released", 40.0, test="a", slot=0, hold_seconds=30.0),
        _e("acquired", 12.0, "gw1", test="b", slot=1, queue_seconds=0.0),
        _e("released", 60.0, "gw1", test="b", slot=1, hold_seconds=48.0),
    ]
    summary = st.summarize(events, slots=2, workers=2)
    assert summary["slot_wait_total"] == 0.0
    assert summary["slot_saturated_seconds"] == 0.0
    assert summary["slot_peak_held"] == 2


def test_digest_names_the_gap_components():
    line = st.digest(st.summarize(_run(), slots=1, workers=2))
    assert line.startswith("schedule telemetry: wall 70s, busy/2 40s, slot wait 28s")


def test_off_by_default_and_output_lands_next_to_junit(tmp_path):
    assert not st.enabled({})
    assert not st.enabled({st.ENV: "0"})
    assert st.enabled({st.ENV: "1"})
    junit = tmp_path / "reports" / "pytest-6-abc.xml"
    expected = junit.with_name("pytest-6-abc.schedule.json")
    assert st.output_path(str(junit), {st.ENV: "1"}) == expected
    assert st.output_path(None, {st.ENV: "1"}) is None
    assert st.output_path(None, {st.ENV: "/x/s.json"}) == Path("/x/s.json")


def test_emit_appends_one_json_line_per_event(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    st.emit("test_start", str(path), nodeid="x")
    st.emit("test_end", str(path), nodeid="x")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [(r["event"], r["worker"]) for r in rows] == [("test_start", "gw3"), ("test_end", "gw3")]
    assert st.read_events([path, tmp_path / "missing"]) == rows
