"""The `AgentRunSummary` read path (bh-6eu2c.2) — correlating `dispatch_log.py` records into
one `AgentRunSummary` per `session_id`, per `docs/design/agent-run-summary-projection-
contract.md`. `test_agent_run_summary.py` covers the pure single-record mapping helpers
(bh-6eu2c.1); this file covers the stateful, cross-record replay bh-6eu2c.2 adds on top.
"""

from __future__ import annotations

import json
import time

from beadhive import agent_run_summary_reader as reader
from beadhive.agent_run_summary import AgentRunState


def _write(tmp_path, records):
    path = tmp_path / "sink.jsonl"
    lines = []
    for r in records:
        if isinstance(r, str):
            lines.append(r)  # a deliberately torn/unparseable line
        else:
            lines.append(json.dumps(r))
    path.write_text("\n".join(lines) + "\n")
    return path


def _by_session(summaries, session_id):
    for s in summaries:
        if s.session_id == session_id:
            return s
    raise AssertionError(f"no summary for session {session_id!r}: {summaries}")


def test_spawn_then_confirmed_in_flight_promotes_to_active(tmp_path):
    sink = _write(
        tmp_path,
        [
            {
                "event": "seat_spawned",
                "timestamp": "2026-01-01T00:00:00Z",
                "bead": "bh-1",
                "role": "developer",
                "action": "dispatch",
                "pid": 1,
                "pgid": 1,
                "session_id": "s-1",
            },
            {
                "event": "dispatch_pass",
                "timestamp": "2026-01-01T00:00:05Z",
                "in_flight": ["bh-1"],
                "denied": [],
                "decision": {"beads": []},
            },
        ],
    )
    summaries = reader.read_from_sink(sink)
    run = _by_session(summaries, "s-1")
    assert run.bead == "bh-1"
    assert run.state == AgentRunState.ACTIVE
    assert run.owner_seat == "developer"


def test_spawn_without_a_later_confirming_pass_stays_starting(tmp_path):
    sink = _write(
        tmp_path,
        [
            {
                "event": "seat_spawned",
                "timestamp": "2026-01-01T00:00:00Z",
                "bead": "bh-1",
                "role": "developer",
                "session_id": "s-1",
            }
        ],
    )
    run = _by_session(reader.read_from_sink(sink), "s-1")
    assert run.state == AgentRunState.STARTING


def test_full_spawn_active_harvest_sequence_ends_finished(tmp_path):
    sink = _write(
        tmp_path,
        [
            {
                "event": "seat_spawned",
                "timestamp": "2026-01-01T00:00:00Z",
                "bead": "bh-1",
                "role": "developer",
                "session_id": "s-1",
            },
            {
                "event": "dispatch_pass",
                "timestamp": "2026-01-01T00:00:05Z",
                "in_flight": ["bh-1"],
                "denied": [],
                "decision": {"beads": []},
            },
            {
                "event": "seat_harvested",
                "timestamp": "2026-01-01T00:00:10Z",
                "bead": "bh-1",
                "outcome": "done",
                "exit_code": 0,
                "session_id": "s-1",
            },
        ],
    )
    summaries = reader.read_from_sink(sink)
    assert len(summaries) == 1
    run = summaries[0]
    assert run.state == AgentRunState.FINISHED  # the LATEST record wins over the earlier ACTIVE
    assert run.started_at is not None
    assert run.ended_at is not None


def test_waiting_derived_from_denied_pass_minus_in_flight(tmp_path):
    sink = _write(
        tmp_path,
        [
            {
                "event": "dispatch_pass",
                "timestamp": "2026-01-01T00:00:00Z",
                "in_flight": ["bh-2"],
                "denied": [{"reason": "concurrency_cap", "detail": ""}],
                "decision": {"beads": ["bh-1", "bh-2"]},
            }
        ],
    )
    summaries = reader.read_from_sink(sink)
    assert len(summaries) == 1
    run = summaries[0]
    assert run.bead == "bh-1"  # bh-2 is already in_flight -> never waiting
    assert run.session_id == ""
    assert run.owner_seat is None
    assert run.state == AgentRunState.WAITING


def test_waiting_ends_the_moment_seat_spawned_lands(tmp_path):
    sink = _write(
        tmp_path,
        [
            {
                "event": "dispatch_pass",
                "timestamp": "2026-01-01T00:00:00Z",
                "in_flight": [],
                "denied": [{"reason": "concurrency_cap", "detail": ""}],
                "decision": {"beads": ["bh-1"]},
            },
            {
                "event": "seat_spawned",
                "timestamp": "2026-01-01T00:00:05Z",
                "bead": "bh-1",
                "role": "developer",
                "session_id": "s-1",
            },
        ],
    )
    summaries = reader.read_from_sink(sink)
    assert len(summaries) == 1  # the waiting entry is gone, replaced by the spawned session
    run = summaries[0]
    assert run.session_id == "s-1"
    assert run.state == AgentRunState.STARTING


def test_seat_cancelled_maps_to_failed_and_ends_the_session(tmp_path):
    sink = _write(
        tmp_path,
        [
            {
                "event": "seat_spawned",
                "timestamp": "2026-01-01T00:00:00Z",
                "bead": "bh-1",
                "role": "developer",
                "session_id": "s-1",
            },
            {
                "event": "seat_cancelled",
                "timestamp": "2026-01-01T00:00:05Z",
                "bead": "bh-1",
                "rung": "hard",
                "exit_code": 137,
                "priced": True,
                "session_id": "s-1",
                "group_gone": False,
                "signals": ["SIGKILL"],
            },
        ],
    )
    run = _by_session(reader.read_from_sink(sink), "s-1")
    assert run.state == AgentRunState.FAILED


def test_torn_line_is_skipped_not_crashed_on(tmp_path):
    sink = _write(
        tmp_path,
        [
            {
                "event": "seat_spawned",
                "timestamp": "2026-01-01T00:00:00Z",
                "bead": "bh-1",
                "role": "developer",
                "session_id": "s-1",
            },
            "NOT JSON AT ALL {{{",
            {
                "event": "seat_harvested",
                "timestamp": "2026-01-01T00:00:05Z",
                "bead": "bh-1",
                "outcome": "done",
                "exit_code": 0,
                "session_id": "s-1",
            },
        ],
    )
    run = _by_session(reader.read_from_sink(sink), "s-1")
    assert run.state == AgentRunState.FINISHED


def test_missing_sink_returns_empty_list_not_an_error(tmp_path):
    assert reader.read_from_sink(tmp_path / "does-not-exist.jsonl") == []


def test_missing_sink_freshness_is_unknown_default(tmp_path):
    freshness = reader.compute_freshness(tmp_path / "does-not-exist.jsonl")
    assert freshness.state == "unknown"
    assert freshness.as_of is None


def test_recent_sink_without_writer_evidence_is_unknown(tmp_path):
    sink = tmp_path / "sink.jsonl"
    sink.write_text('{"event": "seat_spawned"}\n')
    freshness = reader.compute_freshness(sink, now=sink.stat().st_mtime + 1)
    assert freshness.state == "unknown"
    assert freshness.as_of == sink.stat().st_mtime
    assert freshness.detail == "writer colocation unverified; sink last written 1s ago"


def test_recent_sink_under_config_home_is_not_proof_of_colocation(monkeypatch, tmp_path):
    """Canonical placement plus recency is adversarially plausible but still not writer proof."""
    monkeypatch.setattr(reader.config, "home", lambda: tmp_path)
    sink = reader.dispatch_log.sink_path_for_slug("github-acme-widgets")
    sink.parent.mkdir()
    sink.write_text('{"event": "seat_spawned"}\n')
    mtime = sink.stat().st_mtime
    freshness = reader.compute_freshness(sink, now=mtime)
    assert sink.is_relative_to(reader.config.home())
    assert freshness.state == "unknown"
    assert freshness.as_of == mtime


def test_old_sink_without_writer_evidence_is_also_unknown(tmp_path):
    sink = tmp_path / "sink.jsonl"
    sink.write_text('{"event": "seat_spawned"}\n')
    mtime = sink.stat().st_mtime
    freshness = reader.compute_freshness(sink, now=mtime + 86_400)
    assert freshness.state == "unknown"
    assert freshness.as_of == mtime


def test_read_agent_run_summaries_resolves_hive_like_dispatch_status(monkeypatch, tmp_path):
    """Same hive-resolution convention as `dispatch_status.compute_status` — proven by
    monkeypatching the exact same seam `test_dispatch_status.py` does."""
    from beadhive import registry

    entry = {"provider": "github", "org": "acme", "repo": "widgets", "prefix": "acme"}
    monkeypatch.setattr(registry, "hive_dir_for", lambda cfg, hive: tmp_path)
    monkeypatch.setattr(registry, "entry_for_dir", lambda cfg, cwd: entry)

    slug = reader.dispatch_log.hive_slug(entry)
    sink_dir = tmp_path / "sink"
    sink_dir.mkdir()
    sink_path = sink_dir / f"{slug}.jsonl"
    monkeypatch.setattr(reader.dispatch_log, "sink_path_for_slug", lambda s: sink_path)
    sink_path.write_text(
        json.dumps(
            {
                "event": "seat_spawned",
                "timestamp": "2026-01-01T00:00:00Z",
                "bead": "bh-1",
                "role": "developer",
                "session_id": "s-1",
            }
        )
        + "\n"
    )

    summaries = reader.read_agent_run_summaries("acme/widgets", cfg={})
    assert len(summaries) == 1
    assert summaries[0].bead == "bh-1"


def test_freshness_state_never_defaults_to_fresh_without_a_sink(tmp_path):
    # The contract's whole point: no sink at all must read `unknown`, never `fresh`.
    summaries = reader.read_from_sink(tmp_path / "nope.jsonl")
    assert summaries == []
    assert reader.compute_freshness(tmp_path / "nope.jsonl").state != "fresh"


def test_compute_freshness_uses_real_wall_clock_only_for_observed_age(tmp_path):
    sink = tmp_path / "sink.jsonl"
    sink.write_text('{"event": "seat_spawned"}\n')
    before = time.time()
    freshness = reader.compute_freshness(sink)
    assert freshness.state == "unknown"
    assert freshness.as_of <= before + 1
    assert freshness.detail is not None
    assert freshness.detail.startswith("writer colocation unverified; sink last written ")
