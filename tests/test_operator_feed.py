"""Atomic snapshot/activity installation and cursor tests."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from beadhive import operator_feed, operator_sources, run_journal, state_stream
from beadhive.agent_run_summary import Freshness
from beadhive.public_readers import AgentRunSnapshot, Coverage

NOW = datetime(2026, 8, 24, tzinfo=UTC).isoformat().replace("+00:00", "Z")
DIGEST = "sha256:" + "a" * 64
HIVE = "github/beadhive/beadhive"


def _cfg() -> dict:
    return {
        "managed_repos": [
            {
                "provider": "github",
                "org": "beadhive",
                "repo": "beadhive",
                "prefix": "bh",
                "kind": "org-native",
            }
        ]
    }


def _snapshot(revision: str, status: str) -> state_stream.ProviderSnapshot:
    return state_stream.ProviderSnapshot(
        scope="hive",
        revision=revision,
        as_of=NOW,
        issues=(
            state_stream.StreamIssue(
                id="bh-1",
                hive=HIVE,
                issue_type="task",
                status=status,
                priority="P1",
                title="Atomic feed",
                updated_at=NOW,
            ),
        ),
    )


def _runtime(host: str, source: str) -> AgentRunSnapshot:
    return AgentRunSnapshot(
        host_id=host,
        source_id=source,
        revision="runtime-1",
        summaries=(),
        coverage=Coverage.UNKNOWN,
        coverage_reason="source_missing",
        freshness=Freshness(),
    )


class MutableProvider:
    def __init__(self) -> None:
        self.current = _snapshot("beads-1", "open")
        self.captured: threading.Event | None = None
        self.release: threading.Event | None = None

    def refresh(self, _request):
        captured = self.current
        if self.captured is not None:
            self.captured.set()
        if self.release is not None:
            assert self.release.wait(2)
        return captured


def _sources(tmp_path: Path, provider: MutableProvider) -> operator_sources.OperatorSources:
    return operator_sources.OperatorSources(
        cfg=_cfg(),
        host_id="host-1",
        provider=provider,
        summary_reader=lambda _path, host, source: _runtime(host, source),
        journal_base=tmp_path,
        dispatch_sink_for_entry=lambda _cfg, _entry: tmp_path / "dispatch.jsonl",
    )


def _record(revision: str, timestamp: int) -> dict:
    return {
        "version": run_journal.VERSION,
        "source_revision": revision,
        "timestamp_ms": timestamp,
        "run_id": "run-1",
        "hive": HIVE,
        "bead": "bh-1",
        "driver": "baml",
        "provider": "claude-code",
        "manifest_digest": DIGEST,
        "provider_continuation": None,
        "writer": run_journal.WRITER_LOCAL_LOOP,
        "activity": {"kind": "run.created" if timestamp == 1 else "process.spawned"},
    }


def _journal(tmp_path: Path, records: list[dict]) -> Path:
    path = run_journal.journal_path_for_hive(HIVE, "run-1", base=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def test_snapshot_is_direct_canonical_and_same_revision_keeps_cursor(tmp_path: Path) -> None:
    provider = MutableProvider()
    feed = operator_feed.OperatorFeed(_sources(tmp_path, provider), now_millis=lambda: 1000)
    first = feed.snapshot_with_cursor(HIVE)
    second = feed.snapshot_with_cursor(HIVE)

    assert first is second
    assert first["hive"]["prefix"] == HIVE
    assert first["workItems"][0]["ref"]["hiveId"] == HIVE
    assert first["cursor"] == {
        "subscriptionId": f"hive:{HIVE}",
        "producerEpoch": first["cursor"]["producerEpoch"],
        "sequence": 0,
        "observedAt": 1000,
    }


def test_concurrent_change_is_old_snapshot_then_strictly_later_install(tmp_path: Path) -> None:
    provider = MutableProvider()
    captured = provider.captured = threading.Event()
    release = provider.release = threading.Event()
    feed = operator_feed.OperatorFeed(_sources(tmp_path, provider), now_millis=lambda: 1000)
    installs: list[operator_feed.FeedInstall] = []
    feed.register_install_observer(installs.append)
    responses = []

    worker = threading.Thread(target=lambda: responses.append(feed.snapshot_with_cursor(HIVE)))
    worker.start()
    assert captured.wait(2)
    provider.current = _snapshot("beads-2", "closed")
    release.set()
    worker.join(2)
    assert not worker.is_alive()

    old = responses[0]
    provider.captured = provider.release = None
    new = feed.snapshot_with_cursor(HIVE)
    assert old["workItems"][0]["record"]["status"] == "open"
    assert new["workItems"][0]["record"]["status"] == "closed"
    assert old["cursor"]["sequence"] == 0
    assert new["cursor"]["sequence"] == 1
    assert installs[-1].current["cursor"]["sequence"] > old["cursor"]["sequence"]


def test_relay_transition_handler_allocates_exact_event_count_before_snapshot_returns(
    tmp_path: Path,
) -> None:
    provider = MutableProvider()
    feed = operator_feed.OperatorFeed(_sources(tmp_path, provider), now_millis=lambda: 1000)
    first = feed.snapshot_with_cursor(HIVE)
    transitions: list[operator_feed.FeedTransition] = []
    finalized: list[operator_feed.FeedInstall] = []
    feed.register_transition_handler(lambda transition: transitions.append(transition) or 3)
    feed.register_install_observer(finalized.append)

    provider.current = _snapshot("beads-2", "closed")
    second = feed.snapshot_with_cursor(HIVE)
    assert transitions[0].base_sequence == first["cursor"]["sequence"] == 0
    assert transitions[0].current["cursor"]["sequence"] == 3
    assert second["cursor"]["sequence"] == 3
    assert finalized[0].current["cursor"]["sequence"] == 3


def test_activity_snapshot_and_delta_keep_epoch_and_sequence_separate_from_revision(
    tmp_path: Path,
) -> None:
    provider = MutableProvider()
    feed = operator_feed.OperatorFeed(_sources(tmp_path, provider))
    first_record = _record("opaque:first", 1)
    _journal(tmp_path, [first_record])

    first = feed.activity_with_cursor("run-1")
    epoch = first["producerEpoch"]
    assert first["kind"] == "snapshot"
    assert first["sequence"] == 1
    assert first["sourceRevision"] == "opaque:first"
    assert first["activities"][0]["sequence"] == 1
    assert first["activities"][0]["payload"]["text"] is None

    second_record = _record("opaque:second", 2)
    _journal(tmp_path, [first_record, second_record])
    delta = feed.activity_with_cursor("run-1", after=(epoch, 1))
    assert delta["kind"] == "delta"
    assert delta["producerEpoch"] == epoch
    assert (delta["baseSequence"], delta["sequence"]) == (1, 2)
    assert [item["sequence"] for item in delta["activities"]] == [2]
    assert delta["activities"][0]["sourceRevision"] == "opaque:second"


def test_activity_rewrite_rotates_epoch_and_expires_old_cursor(tmp_path: Path) -> None:
    provider = MutableProvider()
    feed = operator_feed.OperatorFeed(_sources(tmp_path, provider))
    _journal(tmp_path, [_record("opaque:first", 1)])
    first = feed.activity_with_cursor("run-1")

    _journal(tmp_path, [_record("opaque:replacement", 3)])
    with pytest.raises(operator_sources.OperatorSourceError) as expired:
        feed.activity_with_cursor(
            "run-1", after=(str(first["producerEpoch"]), int(first["sequence"]))
        )
    assert (expired.value.code, expired.value.status_code) == (
        "activity_cursor_expired",
        410,
    )
    replacement = feed.activity_with_cursor("run-1")
    assert replacement["producerEpoch"] != first["producerEpoch"]
    assert replacement["sequence"] == 1
