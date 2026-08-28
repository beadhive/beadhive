"""Polling snapshot provider over dependency-capable export reads (bh-jksq.3)."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from beadhive import state_stream, state_stream_polling

NOW = datetime(2026, 8, 24, tzinfo=UTC)


def raw_issue(
    issue_id="bh-1",
    *,
    status="open",
    labels=None,
    dependencies=None,
    assignee=None,
    priority=1,
):
    return {
        "_type": "issue",
        "id": issue_id,
        "title": f"Issue {issue_id}",
        "issue_type": "task",
        "status": status,
        "priority": priority,
        "updated_at": "2026-08-24T00:00:00Z",
        "labels": labels or [],
        "dependencies": dependencies or [],
        "assignee": assignee,
    }


class ExportBackend:
    """Only the Engine export operation exists; query fan-out is impossible by construction."""

    def __init__(self, records_by_target, *, gates_by_target=None):
        self.records_by_target = {
            str(target): list(refreshes) for target, refreshes in records_by_target.items()
        }
        self.gates_by_target = {
            str(target): list(refreshes) for target, refreshes in (gates_by_target or {}).items()
        }
        self.calls = []
        self.gate_calls = []

    def export_jsonl(self, cwd, out_path, *, env=None):
        self.calls.append((str(cwd), str(out_path), env))
        refreshes = self.records_by_target[str(cwd)]
        records = refreshes.pop(0) if len(refreshes) > 1 else refreshes[0]
        out_path.write_text("".join(f"{json.dumps(row)}\n" for row in records))
        return subprocess.CompletedProcess([], 0, "", "")

    def list_gates(self, cwd):
        self.gate_calls.append(str(cwd))
        refreshes = self.gates_by_target.get(str(cwd), [[]])
        records = refreshes.pop(0) if len(refreshes) > 1 else refreshes[0]
        return subprocess.CompletedProcess([], 0, json.dumps(records), "")


@pytest.fixture
def world(tmp_path, monkeypatch):
    factory = tmp_path / "hq"
    hub = tmp_path / "hub"
    hive = tmp_path / "hive"
    for path in (factory, hub, hive):
        path.mkdir()
    entry = {
        "provider": "github",
        "org": "beadhive",
        "repo": "beadhive",
        "prefix": "bh",
    }
    cfg = {"managed_repos": [entry]}
    monkeypatch.setattr(state_stream_polling.config, "hq_dir", lambda: factory)
    monkeypatch.setattr(state_stream_polling.config, "hub_dir", lambda: hub)
    monkeypatch.setattr(state_stream_polling.registry, "hive_dir", lambda _entry: hive)
    return cfg, entry, factory, hub, hive


def provider(cfg, backend, **kwargs):
    poll_interval = kwargs.pop("poll_interval", 0)
    now = kwargs.pop("now", lambda: NOW)
    return state_stream_polling.PollingStateStreamProvider(
        cfg,
        backend=backend,
        poll_interval=poll_interval,
        sleeper=lambda _seconds: None,
        now=now,
        **kwargs,
    )


def test_factory_hub_and_hive_each_take_one_export_shaped_initial_snapshot(world):
    cfg, _entry, factory, hub, hive = world
    identity = ["provider:github", "org:beadhive", "repo:beadhive"]
    backend = ExportBackend(
        {
            factory: [[raw_issue("bh-f", labels=identity)]],
            hub: [[raw_issue("bh-u", labels=identity)]],
            hive: [[raw_issue("bh-h")]],
        }
    )
    adapter = provider(cfg, backend)
    requests = [
        state_stream.StreamRequest("factory"),
        state_stream.StreamRequest("hub"),
        state_stream.StreamRequest("hive", hive="beadhive"),
    ]

    snapshots = [adapter.refresh(request) for request in requests]

    assert [snapshot.scope.value for snapshot in snapshots] == ["factory", "hub", "hive"]
    assert [[issue.id for issue in snapshot.issues] for snapshot in snapshots] == [
        ["bh-f"],
        ["bh-u"],
        ["bh-h"],
    ]
    assert [call[0] for call in backend.calls] == [str(factory), str(hub), str(hive)]
    assert backend.gate_calls == [str(factory), str(hub), str(hive)]
    assert all(snapshot.as_of == "2026-08-24T00:00:00Z" for snapshot in snapshots)


def test_export_records_are_normalized_without_backend_fields(world):
    cfg, _entry, _factory, _hub, hive = world
    backend = ExportBackend(
        {
            hive: [
                [
                    {
                        **raw_issue(
                            priority=0,
                            labels=["z", "a"],
                            dependencies=[
                                {
                                    "issue_id": "bh-1",
                                    "depends_on_id": "bh-parent",
                                    "type": "parent-child",
                                    "created_at": "backend-only",
                                    "metadata": {"ignored": True},
                                }
                            ],
                        ),
                        "metadata": {"git.commits": "backend-only"},
                        "lease_expires_at": "backend-only",
                        "description": "Full description",
                        "design": "Design notes",
                        "acceptance_criteria": "Acceptance criteria",
                        "notes": "Operator notes",
                        "mol_type": "workflow",
                        "owner": "owner@example.test",
                        "created_by": "creator@example.test",
                        "created_at": "2026-08-20T00:00:00Z",
                        "closed_at": "2026-08-24T00:00:00Z",
                    }
                ]
            ]
        }
    )
    adapter = provider(cfg, backend)

    snapshot = adapter.refresh(state_stream.StreamRequest("hive", hive="beadhive"))
    [item] = snapshot.issues

    assert item.priority == "P0"
    assert item.labels == ("a", "z")
    assert item.parent_id == "bh-parent"
    assert item.dependencies == (
        state_stream.StreamDependency("bh-1", "bh-parent", "parent-child"),
    )
    assert item.description == "Full description"
    assert item.design == "Design notes"
    assert item.acceptance_criteria == "Acceptance criteria"
    assert item.notes == "Operator notes"
    assert item.mol_type == "workflow"
    assert item.owner == "owner@example.test"
    assert item.created_by == "creator@example.test"
    assert item.created_at == "2026-08-20T00:00:00Z"
    assert item.closed_at == "2026-08-24T00:00:00Z"
    assert item.lease_expires_at == "backend-only"
    payload = state_stream.frame_payload(
        next(
            state_stream.stream_frames(adapter, state_stream.StreamRequest("hive", hive="beadhive"))
        )
    )
    assert "metadata" not in payload["issues"][0]
    assert "lease_expires_at" not in payload["issues"][0]
    assert "description" not in payload["issues"][0]


def test_one_export_projects_dependency_provenance_and_verbatim_assignment(world):
    cfg, _entry, _factory, _hub, hive = world
    backend = ExportBackend(
        {
            hive: [
                [
                    raw_issue(
                        "bh-child",
                        assignee="dev/operator-core",
                        dependencies=[
                            {
                                "issue_id": "bh-child",
                                "depends_on_id": "bh-parent",
                                "type": "blocks",
                                "created_at": "2026-08-24T00:00:00Z",
                                "created_by": "planner/one",
                                "metadata": {"ignored": True},
                            },
                            {
                                "issue_id": "bh-child",
                                "depends_on_id": "bh-origin",
                                "type": "related",
                            },
                        ],
                    )
                ]
            ]
        }
    )

    snapshot = provider(cfg, backend).refresh(state_stream.StreamRequest("hive", hive="beadhive"))

    assert len(backend.calls) == 1
    assert snapshot.assignments == (
        state_stream.Assignment(
            id=state_stream.projection_id("assignment", ("beadhive", "bh-child")),
            hive="beadhive",
            issue_id="bh-child",
            seat="dev/operator-core",
        ),
    )
    dependencies = {item.depends_on_id: item for item in snapshot.work_dependencies}
    assert dependencies["bh-parent"].created_at == "2026-08-24T00:00:00Z"
    assert dependencies["bh-parent"].created_by == "planner/one"
    assert dependencies["bh-origin"].created_at is None
    assert dependencies["bh-origin"].created_by is None


@pytest.mark.parametrize("assignee", [None, ""])
def test_empty_assignee_has_no_assignment(world, assignee):
    cfg, _entry, _factory, _hub, hive = world
    backend = ExportBackend({hive: [[raw_issue(assignee=assignee)]]})

    snapshot = provider(cfg, backend).refresh(state_stream.StreamRequest("hive", hive="beadhive"))

    assert snapshot.assignments == ()


def test_missing_dependency_carrier_is_partial_not_false_empty_truth(world):
    cfg, _entry, _factory, _hub, hive = world
    raw = raw_issue(assignee="dev/operator-core")
    del raw["dependencies"]
    backend = ExportBackend({hive: [[raw]]})

    snapshot = provider(cfg, backend).refresh(state_stream.StreamRequest("hive", hive="beadhive"))

    assert [item.id for item in snapshot.issues] == ["bh-1"]
    assert snapshot.assignments[0].seat == "dev/operator-core"
    assert snapshot.work_dependencies == ()
    assert snapshot.partial is True
    assert snapshot.partial_reason == "dependency_data_unavailable"


def test_operator_only_change_advances_revision_and_emits_replacement(world):
    cfg, _entry, _factory, _hub, hive = world

    def record(created_by):
        return raw_issue(
            dependencies=[
                {
                    "issue_id": "bh-1",
                    "depends_on_id": "bh-parent",
                    "type": "blocks",
                    "created_by": created_by,
                }
            ]
        )

    backend = ExportBackend({hive: [[record("dev/one")], [record("dev/two")]]})
    adapter = provider(cfg, backend)
    request = state_stream.StreamRequest("hive", hive="beadhive")
    first = adapter.refresh(request)
    second = adapter.refresh(request)

    assert first.issues == second.issues
    assert first.revision != second.revision
    _initial, delta = list(
        state_stream.stream_frames(
            type(
                "ProviderDouble",
                (),
                {"name": "double", "updates": lambda _self, _request: iter((first, second))},
            )(),
            request,
        )
    )
    assert delta.work_dependencies_changed[0].created_by == "dev/two"
    assert delta.work_dependencies_removed == ()


def test_gate_change_and_retention_expiry_share_revision_diff_and_removal(world):
    cfg, _entry, _factory, _hub, hive = world
    gate_id = "gate-1"
    dependency = {
        "issue_id": "bh-1",
        "depends_on_id": gate_id,
        "type": "blocks",
    }
    raw_gate = {
        "id": gate_id,
        "issue_type": "gate",
        "status": "open",
        "await_type": "human",
        "description": "Reason: bh:review abc1234",
        "created_at": "2026-08-23T22:00:00Z",
    }
    resolved = {**raw_gate, "status": "closed", "closed_at": "2026-08-24T00:00:00Z"}
    backend = ExportBackend(
        {hive: [[raw_issue(dependencies=[dependency])]]},
        gates_by_target={hive: [[raw_gate], [resolved], [resolved]]},
    )
    instants = iter((NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=25)))
    adapter = provider(cfg, backend, now=lambda: next(instants))
    request = state_stream.StreamRequest("hive", hive="beadhive")
    snapshots = [adapter.refresh(request) for _ in range(3)]

    assert len({snapshot.revision for snapshot in snapshots}) == 3
    _initial, resolved_delta, expired_delta = list(
        state_stream.stream_frames(
            type(
                "ProviderDouble",
                (),
                {"name": "double", "updates": lambda _self, _request: iter(snapshots)},
            )(),
            request,
        )
    )
    assert resolved_delta.gate_requests_changed[0].status == "resolved"
    assert expired_delta.gate_requests_changed == ()
    assert expired_delta.gate_requests_removed == (snapshots[0].gate_requests[0].id,)
    assert len(backend.calls) == len(backend.gate_calls) == 3


@pytest.mark.parametrize(
    "gate_result",
    [
        subprocess.CompletedProcess([], 1, "", "gate backend unavailable"),
        subprocess.CompletedProcess([], 0, "", ""),
        subprocess.CompletedProcess([], 0, "null", ""),
        subprocess.CompletedProcess([], 0, "not-json", ""),
        subprocess.CompletedProcess([], 0, "{}", ""),
    ],
)
def test_gate_source_failure_degrades_the_frame_without_suppressing_issues(world, gate_result):
    cfg, _entry, _factory, _hub, hive = world

    class FailedGateBackend(ExportBackend):
        def list_gates(self, cwd):
            self.gate_calls.append(str(cwd))
            return gate_result

    backend = FailedGateBackend({hive: [[raw_issue()]]})

    snapshot = provider(cfg, backend).refresh(state_stream.StreamRequest("hive", hive="beadhive"))

    assert [item.id for item in snapshot.issues] == ["bh-1"]
    assert snapshot.gate_requests == ()
    assert snapshot.partial is True
    assert snapshot.partial_reason == "gate_source_unavailable"
    assert len(backend.calls) == len(backend.gate_calls) == 1


@pytest.mark.parametrize(
    ("core", "source", "projector", "expected"),
    [
        (
            "dependency_data_unavailable",
            "gate_source_unavailable",
            "invalid_gate_record",
            "dependency_data_unavailable",
        ),
        (None, "gate_source_unavailable", "invalid_gate_record", "gate_source_unavailable"),
        (None, None, "invalid_gate_record", "invalid_gate_record"),
        (None, None, None, None),
    ],
)
def test_partial_reason_precedence_is_core_then_gate_source_then_gate_projector(
    core, source, projector, expected
):
    assert state_stream_polling._partial_reason(core, source, projector) == expected


def test_gate_source_outage_advances_revision_and_removes_previously_visible_gate(world):
    cfg, _entry, _factory, _hub, hive = world
    dependency = {
        "issue_id": "bh-1",
        "depends_on_id": "gate-1",
        "type": "blocks",
    }
    raw_gate = {
        "id": "gate-1",
        "issue_type": "gate",
        "status": "open",
        "await_type": "human",
        "description": "Reason: unknown-marker: manual",
        "created_at": "2026-08-24T00:00:00Z",
    }

    class OutageBackend(ExportBackend):
        def list_gates(self, cwd):
            self.gate_calls.append(str(cwd))
            if len(self.gate_calls) == 1:
                return subprocess.CompletedProcess([], 0, json.dumps([raw_gate]), "")
            return subprocess.CompletedProcess([], 1, "", "gate source unavailable")

    backend = OutageBackend({hive: [[raw_issue(dependencies=[dependency])]]})
    adapter = provider(cfg, backend)
    request = state_stream.StreamRequest("hive", hive="beadhive")
    first = adapter.refresh(request)
    outage = adapter.refresh(request)

    assert first.gate_requests[0].gate_kind == "other"
    assert outage.gate_requests == ()
    assert outage.partial_reason == "gate_source_unavailable"
    assert first.revision != outage.revision
    _snapshot_frame, delta = state_stream.stream_frames(
        type(
            "ProviderDouble",
            (),
            {"name": "double", "updates": lambda _self, _request: iter((first, outage))},
        )(),
        request,
    )
    assert delta.gate_requests_changed == ()
    assert delta.gate_requests_removed == (first.gate_requests[0].id,)
    assert len(backend.calls) == len(backend.gate_calls) == 2


def test_aggregate_operator_hive_comes_from_accepted_registry_identity(world):
    cfg, _entry, factory, _hub, _hive = world
    identity = ["provider:github", "org:beadhive", "repo:beadhive"]
    backend = ExportBackend(
        {
            factory: [
                [
                    raw_issue(
                        "not-a-hive-prefix",
                        labels=identity,
                        assignee="dev/operator-core",
                        dependencies=[
                            {
                                "issue_id": "not-a-hive-prefix",
                                "depends_on_id": "another-id",
                                "type": "blocks",
                            }
                        ],
                    )
                ]
            ]
        }
    )

    snapshot = provider(cfg, backend).refresh(state_stream.StreamRequest("factory"))

    assert snapshot.assignments[0].hive == "beadhive"
    assert snapshot.work_dependencies[0].hive == "beadhive"


def test_polling_emits_only_changed_snapshots(world):
    cfg, _entry, _factory, _hub, hive = world
    unchanged = [raw_issue()]
    changed = [raw_issue(status="closed")]
    backend = ExportBackend({hive: [unchanged, unchanged, changed]})
    adapter = provider(cfg, backend)
    request = state_stream.StreamRequest("hive", hive="beadhive")
    updates = adapter.updates(request)

    first = next(updates)
    second = next(updates)

    assert first.issues[0].status == "open"
    assert second.issues[0].status == "closed"
    assert len(backend.calls) == 3


def test_recognized_since_starts_from_retained_full_snapshot(world):
    cfg, _entry, _factory, _hub, hive = world
    backend = ExportBackend({hive: [[raw_issue()], [raw_issue(status="closed")]]})
    adapter = provider(cfg, backend)
    request = state_stream.StreamRequest("hive", hive="beadhive")
    retained = adapter.refresh(request)
    reconnect = state_stream.StreamRequest(
        "hive", hive="beadhive", since_revision=retained.revision
    )

    updates = adapter.updates(reconnect)

    assert next(updates) == retained
    assert next(updates).issues[0].status == "closed"


def test_unknown_since_starts_from_current_full_snapshot(world):
    cfg, _entry, _factory, _hub, hive = world
    backend = ExportBackend({hive: [[raw_issue()]]})
    adapter = provider(cfg, backend)

    [frame] = [
        next(
            state_stream.stream_frames(
                adapter,
                state_stream.StreamRequest(
                    "hive", hive="beadhive", since_revision="another-process:missing"
                ),
            )
        )
    ]

    assert isinstance(frame, state_stream.SnapshotFrame)
    assert frame.reason is state_stream.SnapshotReason.INITIAL


class RecoveringExportBackend(ExportBackend):
    def __init__(self, records_by_target, *, fail_on_calls):
        super().__init__(records_by_target)
        self.fail_on_calls = set(fail_on_calls)

    def export_jsonl(self, cwd, out_path, *, env=None):
        if len(self.calls) + 1 in self.fail_on_calls:
            self.calls.append((str(cwd), str(out_path), env))
            return subprocess.CompletedProcess([], 1, "", "backend unavailable")
        return super().export_jsonl(cwd, out_path, env=env)


def test_midstream_export_failure_requests_resync_then_recovers(world):
    cfg, _entry, _factory, _hub, hive = world
    backend = RecoveringExportBackend(
        {hive: [[raw_issue()], [raw_issue(status="closed")]]}, fail_on_calls={2}
    )
    adapter = provider(cfg, backend)
    updates = adapter.updates(state_stream.StreamRequest("hive", hive="beadhive"))

    first = next(updates)
    reset = next(updates)
    recovered = next(updates)

    assert first.issues[0].status == "open"
    assert reset == state_stream.ProviderReset(
        scope=state_stream.StreamScope.HIVE,
        reason=state_stream.ResyncReason.ADAPTER_ERROR,
        as_of="2026-08-24T00:00:00Z",
    )
    assert recovered.issues[0].status == "closed"
    assert len(backend.calls) == 3


def test_initial_export_failure_never_substitutes_reset_for_required_snapshot(world):
    cfg, _entry, _factory, _hub, hive = world
    backend = RecoveringExportBackend({hive: [[raw_issue()]]}, fail_on_calls={1})
    adapter = provider(cfg, backend)

    with pytest.raises(state_stream_polling.PollingSnapshotError, match="backend unavailable"):
        next(adapter.updates(state_stream.StreamRequest("hive", hive="beadhive")))


class BlockingExportBackend(ExportBackend):
    def __init__(self, records_by_target):
        super().__init__(records_by_target)
        self.started = threading.Event()
        self.release = threading.Event()

    def export_jsonl(self, cwd, out_path, *, env=None):
        self.started.set()
        assert self.release.wait(2)
        return super().export_jsonl(cwd, out_path, env=env)


def test_concurrent_same_scope_refreshes_share_one_export(world):
    cfg, _entry, _factory, _hub, hive = world
    backend = BlockingExportBackend({hive: [[raw_issue()]]})
    adapter = provider(cfg, backend)
    request = state_stream.StreamRequest("hive", hive="beadhive")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(adapter.refresh, request)
        assert backend.started.wait(2)
        second = pool.submit(adapter.refresh, request)
        time.sleep(0.05)
        backend.release.set()
        snapshots = [first.result(), second.result()]

    assert len(backend.calls) == 1
    assert len(backend.gate_calls) == 1
    assert snapshots[0] is snapshots[1]


def test_sequential_consumers_share_the_scope_refresh_during_one_poll_cadence(world):
    cfg, _entry, _factory, _hub, hive = world
    backend = ExportBackend({hive: [[raw_issue()]]})
    adapter = provider(cfg, backend, poll_interval=2, monotonic=lambda: 10.0)
    request = state_stream.StreamRequest("hive", hive="beadhive")

    snapshots = [adapter.refresh(request), adapter.refresh(request)]

    assert len(backend.calls) == 1
    assert snapshots[0] is snapshots[1]


def test_aggregate_identity_comes_from_registry_labels_not_issue_prefix(world):
    cfg, _entry, factory, _hub, _hive = world
    backend = ExportBackend(
        {
            factory: [
                [
                    raw_issue(
                        "totally-unrelated-prefix-1",
                        labels=["provider:github", "org:beadhive", "repo:beadhive"],
                    )
                ]
            ]
        }
    )
    adapter = provider(cfg, backend)

    snapshot = adapter.refresh(state_stream.StreamRequest("factory"))

    assert snapshot.issues[0].hive == "beadhive"


def test_unresolvable_or_malformed_records_degrade_instead_of_suppressing_snapshot(world):
    cfg, _entry, factory, _hub, _hive = world
    backend = ExportBackend(
        {
            factory: [
                [
                    raw_issue("unknown", labels=["repo:not-registered"]),
                    raw_issue(
                        "known",
                        labels=["provider:github", "org:beadhive", "repo:beadhive"],
                    ),
                ]
            ]
        }
    )
    adapter = provider(cfg, backend)

    snapshot = adapter.refresh(state_stream.StreamRequest("factory"))

    assert [item.id for item in snapshot.issues] == ["known"]
    assert snapshot.partial is True
    assert snapshot.partial_reason == "hive_identity_unavailable"


def test_provider_is_substitutable_through_the_landed_port(world, monkeypatch):
    cfg, _entry, _factory, _hub, hive = world
    backend = ExportBackend({hive: [[raw_issue()]]})
    adapter = provider(cfg, backend)
    monkeypatch.setattr(state_stream_polling.engine, "get_engine", lambda _cfg: backend)

    assert isinstance(adapter, state_stream.StateStreamProvider)
    assert isinstance(
        state_stream_polling.get_polling_provider(cfg), state_stream.StateStreamProvider
    )
