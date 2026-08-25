"""Generate the pinned OperatorEvent SSE fixture through the production relay."""

from __future__ import annotations

import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from beadhive import host_daemon, operator_feed, operator_sources, operator_sse, state_stream
from beadhive.agent_run_summary import Freshness
from beadhive.public_readers import AgentRunSnapshot, Coverage

HIVE = "github/beadhive/beadhive"
NOW = datetime(2026, 8, 24, tzinfo=UTC).isoformat().replace("+00:00", "Z")
EPOCH = "916ec694696d417ca5b662914f96d921"


def snapshot(revision: str, status: str) -> state_stream.ProviderSnapshot:
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
                title="SSE relay",
                updated_at=NOW,
            ),
        ),
    )


class ControlledProvider:
    def __init__(self) -> None:
        self.current = snapshot("beads-1", "open")

    def refresh(self, _request):
        return self.current


def generate(path: Path) -> None:
    provider = ControlledProvider()
    cfg = {
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
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        sources = operator_sources.OperatorSources(
            cfg=cfg,
            host_id="host-1",
            provider=provider,
            summary_reader=lambda _path, host, source: AgentRunSnapshot(
                host_id=host,
                source_id=source,
                revision="runtime-1",
                summaries=(),
                coverage=Coverage.UNKNOWN,
                coverage_reason="source_missing",
                freshness=Freshness(),
            ),
            journal_base=temp,
            dispatch_sink_for_entry=lambda _cfg, _entry: temp / "dispatch.jsonl",
        )
        with patch(
            "beadhive.operator_feed.uuid.uuid4",
            return_value=SimpleNamespace(hex=EPOCH),
        ):
            feed = operator_feed.OperatorFeed(sources, now_millis=lambda: 1000)
            relay = operator_sse.OperatorEventRelay(
                feed,
                host_daemon.DaemonRuntime(),
                now_millis=lambda: 2000,
            )
            feed.snapshot_with_cursor(HIVE)
            provider.current = snapshot("beads-ui-conformance", "closed")
            feed.snapshot_with_cursor(HIVE)
            frame = relay._hives[HIVE].history[0].frame
    path.write_bytes(frame)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: generate_operator_sse_fixture.py OUTPUT")
    generate(Path(sys.argv[1]))
