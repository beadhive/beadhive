"""Example of applying the public port kit to an existing production adapter."""

from __future__ import annotations

import json
import subprocess
from itertools import islice

import pytest

from beadhive.testing import PortCase, assert_port_conforms

pytestmark = pytest.mark.usefixtures("config_test_scope", "plugin_test_scope")


class ExportBackend:
    """Narrow injected engine boundary used to exercise the real polling adapter."""

    def __init__(self, records):
        self.records = records

    def export_jsonl(self, _cwd, out_path, *, env=None):
        assert env is None
        out_path.write_text("".join(f"{json.dumps(record)}\n" for record in self.records))
        return subprocess.CompletedProcess([], 0, "", "")

    def list_gates(self, _cwd):
        return subprocess.CompletedProcess([], 0, "[]", "")


def test_existing_polling_state_stream_adapter_conforms(
    monkeypatch, request: pytest.FixtureRequest, tmp_path
):
    """The real adapter example constructs no root runtime or legacy aggregate fixture."""
    from beadhive import state_stream, state_stream_polling

    assert "legacy_stateful_test_scope" not in request.fixturenames
    monkeypatch.setattr(state_stream_polling.config, "hq_dir", lambda: tmp_path)
    stream_request = state_stream.StreamRequest("factory")
    records = [
        {
            "_type": "issue",
            "id": "bh-example",
            "title": "Conformance example",
            "issue_type": "task",
            "status": "open",
            "priority": 1,
            "updated_at": "2026-08-31T00:00:00Z",
            "labels": ["provider:github", "org:beadhive", "repo:beadhive"],
            "dependencies": [],
        }
    ]

    def factory():
        return state_stream_polling.PollingStateStreamProvider(
            {"managed_repos": [{"provider": "github", "org": "beadhive", "repo": "beadhive"}]},
            backend=ExportBackend(records),
            poll_interval=0,
            sleeper=lambda _seconds: None,
        )

    def emits_complete_initial_snapshot(adapter) -> None:
        [event] = list(islice(adapter.updates(stream_request), 1))
        assert isinstance(event, state_stream.ProviderSnapshot)
        assert event.scope is state_stream.StreamScope.FACTORY
        assert [issue.id for issue in event.issues] == ["bh-example"]

    assert_port_conforms(
        factory,
        [PortCase("state-stream.emits-complete-initial-snapshot", emits_complete_initial_snapshot)],
        port=state_stream.StateStreamProvider,
        subject="PollingStateStreamProvider",
    )
