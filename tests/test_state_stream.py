"""Backend-neutral state-stream port contract (bh-jksq.2)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from beadhive import state_stream


def issue(issue_id: str, *, status: str = "open", hive: str = "beadhive"):
    return state_stream.StreamIssue(
        id=issue_id,
        hive=hive,
        issue_type="task",
        status=status,
        priority="P1",
        title=f"Issue {issue_id}",
        updated_at="2026-08-24T00:00:00Z",
        labels=("streaming",),
    )


def work_dependency(issue_id: str = "bh-1", *, created_by: str | None = "dev/one"):
    return state_stream.WorkDependency(
        id=state_stream.projection_id(
            "work-dependency", ("beadhive", issue_id, "bh-parent", "blocks")
        ),
        hive="beadhive",
        issue_id=issue_id,
        depends_on_id="bh-parent",
        type="blocks",
        created_at=None,
        created_by=created_by,
    )


def assignment(issue_id: str = "bh-1", *, seat: str = "dev/one"):
    return state_stream.Assignment(
        id=state_stream.projection_id("assignment", ("beadhive", issue_id)),
        hive="beadhive",
        issue_id=issue_id,
        seat=seat,
    )


def snapshot(revision: str, *issues, scope=state_stream.StreamScope.HIVE, **kwargs):
    return state_stream.ProviderSnapshot(
        scope=scope,
        revision=revision,
        as_of="2026-08-24T00:00:00Z",
        issues=issues,
        **kwargs,
    )


class MemoryProvider:
    name = "memory"

    def __init__(self, events):
        self.events = events
        self.requests = []

    def updates(self, request) -> Iterator[state_stream.ProviderEvent]:
        self.requests.append(request)
        yield from self.events


class RemoteProvider:
    """A deliberately different implementation shape using a callable transport."""

    name = "remote"

    def __init__(self, fetch):
        self.fetch = fetch

    def updates(self, request) -> Iterator[state_stream.ProviderEvent]:
        yield from self.fetch(request.scope.value, request.hive, request.since_revision)


def hive_request(**kwargs):
    return state_stream.StreamRequest(scope="hive", hive="beadhive", **kwargs)


def test_structurally_different_providers_share_one_consumer_contract():
    events = [snapshot("r1", issue("bh-1")), snapshot("r2", issue("bh-1", status="closed"))]
    providers = [MemoryProvider(events), RemoteProvider(lambda *_: iter(events))]

    payloads = [
        [
            state_stream.frame_payload(frame)
            for frame in state_stream.stream_frames(p, hive_request())
        ]
        for p in providers
    ]

    assert all(isinstance(provider, state_stream.StateStreamProvider) for provider in providers)
    assert payloads[0] == payloads[1]
    assert [frame["frame"] for frame in payloads[0]] == ["snapshot", "delta"]


def test_since_still_starts_with_the_retained_full_snapshot_then_delta():
    retained = snapshot("retained", issue("bh-1"))
    current = snapshot("current", issue("bh-1", status="closed"), issue("bh-2"))
    provider = MemoryProvider([retained, current])
    request = hive_request(since_revision="retained")

    frames = list(state_stream.stream_frames(provider, request))

    assert provider.requests == [request]
    assert isinstance(frames[0], state_stream.SnapshotFrame)
    assert frames[0].revision == "retained"
    assert frames[0].reason is state_stream.SnapshotReason.INITIAL
    assert isinstance(frames[1], state_stream.DeltaFrame)
    assert frames[1].since_revision == "retained"
    assert [item.id for item in frames[1].changed] == ["bh-1", "bh-2"]


def test_unknown_since_starts_with_current_snapshot_without_leading_resync():
    provider = MemoryProvider([snapshot("current", issue("bh-1"))])

    frames = list(
        state_stream.stream_frames(provider, hive_request(since_revision="expired-adapter-token"))
    )

    assert len(frames) == 1
    assert isinstance(frames[0], state_stream.SnapshotFrame)
    assert frames[0].reason is state_stream.SnapshotReason.INITIAL


@pytest.mark.parametrize("scope", list(state_stream.StreamScope))
def test_all_canonical_scopes_use_the_same_provider_port(scope):
    request = state_stream.StreamRequest(
        scope=scope,
        hive="beadhive" if scope is state_stream.StreamScope.HIVE else None,
    )
    provider = MemoryProvider([snapshot("r1", issue("bh-1"), scope=scope)])

    [frame] = list(state_stream.stream_frames(provider, request))

    assert frame.scope is scope
    assert state_stream.frame_payload(frame)["scope"] == scope.value


def test_later_snapshots_become_full_replacement_deltas_and_sorted_removals():
    provider = MemoryProvider(
        [
            snapshot("r1", issue("bh-z"), issue("bh-a"), issue("bh-keep")),
            snapshot("r2", issue("bh-keep", status="in_progress")),
        ]
    )

    _initial, delta = list(state_stream.stream_frames(provider, hive_request()))

    assert isinstance(delta, state_stream.DeltaFrame)
    assert delta.changed == (issue("bh-keep", status="in_progress"),)
    assert delta.removed == ("bh-a", "bh-z")


def test_all_operator_siblings_share_generic_replacement_and_removal_diffs():
    dependency = work_dependency()
    assigned = assignment()
    gate = state_stream.GateRequest(
        id=state_stream.projection_id("gate-request", ("beadhive", "bh-gate")),
        hive="beadhive",
        gate_id="bh-gate",
        blocks=("bh-1",),
        gate_type="human",
        gate_kind="review",
        status="open",
        reason="bh:review abc1234",
        opened_at="2026-08-24T00:00:00Z",
        resolved_at=None,
    )
    schedule = state_stream.EpicSchedule(
        id=state_stream.projection_id("epic-schedule", ("beadhive", "bh-epic")),
        hive="beadhive",
        epic_id="bh-epic",
        groups=(),
        singletons=("bh-1",),
        coordinators=(),
    )
    first = snapshot(
        "r1",
        issue("bh-1"),
        work_dependencies=(dependency,),
        gate_requests=(gate,),
        epic_schedules=(schedule,),
        assignments=(assigned,),
    )
    changed = snapshot(
        "r2",
        issue("bh-1"),
        work_dependencies=(work_dependency(created_by="dev/two"),),
        gate_requests=(state_stream.GateRequest(**{**gate.__dict__, "status": "resolved"}),),
        epic_schedules=(
            state_stream.EpicSchedule(**{**schedule.__dict__, "singletons": ("bh-2",)}),
        ),
        assignments=(assignment(seat="dev/two"),),
    )
    removed = snapshot("r3", issue("bh-1"))

    _initial, replacement_delta, removal_delta = list(
        state_stream.stream_frames(MemoryProvider([first, changed, removed]), hive_request())
    )

    assert replacement_delta.work_dependencies_changed[0].created_by == "dev/two"
    assert replacement_delta.gate_requests_changed[0].status == "resolved"
    assert replacement_delta.epic_schedules_changed[0].singletons == ("bh-2",)
    assert replacement_delta.assignments_changed[0].seat == "dev/two"
    assert replacement_delta.work_dependencies_removed == ()
    assert removal_delta.work_dependencies_removed == (dependency.id,)
    assert removal_delta.gate_requests_removed == (gate.id,)
    assert removal_delta.epic_schedules_removed == (schedule.id,)
    assert removal_delta.assignments_removed == (assigned.id,)
    replacement_payload = state_stream.frame_payload(replacement_delta)
    assert replacement_payload["work_dependencies_changed"][0]["created_by"] == "dev/two"
    assert replacement_payload["gate_requests_changed"][0]["status"] == "resolved"
    assert replacement_payload["epic_schedules_changed"][0]["singletons"] == ["bh-2"]
    assert replacement_payload["assignments_changed"][0]["seat"] == "dev/two"
    for name in (
        "work_dependencies_changed",
        "work_dependencies_removed",
        "gate_requests_changed",
        "gate_requests_removed",
        "epic_schedules_changed",
        "epic_schedules_removed",
        "assignments_changed",
        "assignments_removed",
    ):
        assert name in replacement_payload


def test_resync_is_mid_session_and_immediately_followed_by_full_snapshot():
    provider = MemoryProvider(
        [
            snapshot("r1", issue("bh-1")),
            state_stream.ProviderReset(
                scope="hive",
                reason="adapter_error",
                as_of="2026-08-24T00:01:00Z",
            ),
            snapshot("r3", issue("bh-1", status="closed")),
        ]
    )

    frames = list(state_stream.stream_frames(provider, hive_request()))

    assert [frame.frame for frame in frames] == ["snapshot", "resync", "snapshot"]
    assert isinstance(frames[1], state_stream.ResyncFrame)
    assert not hasattr(frames[1], "revision")
    assert "revision" not in state_stream.frame_payload(frames[1])
    assert frames[2].reason is state_stream.SnapshotReason.RESYNC


@pytest.mark.parametrize(
    ("events", "message"),
    [
        ([], "before the required initial snapshot"),
        (
            [
                state_stream.ProviderReset(
                    scope="hive", reason="unknown_revision", as_of="2026-08-24T00:00:00Z"
                )
            ],
            "must start with a snapshot",
        ),
        (
            [
                snapshot("r1", issue("bh-1")),
                state_stream.ProviderReset(
                    scope="hive", reason="adapter_error", as_of="2026-08-24T00:01:00Z"
                ),
            ],
            "snapshot required by resync",
        ),
    ],
)
def test_invalid_provider_ordering_fails_closed(events, message):
    with pytest.raises(state_stream.StateStreamContractError, match=message):
        list(state_stream.stream_frames(MemoryProvider(events), hive_request()))


def test_hive_scope_rejects_records_from_another_hive():
    provider = MemoryProvider([snapshot("r1", issue("other-1", hive="other"))])

    with pytest.raises(state_stream.StateStreamContractError, match="other"):
        list(state_stream.stream_frames(provider, hive_request()))


def test_hive_scope_rejects_foreign_operator_records_too():
    foreign = state_stream.Assignment(
        id=state_stream.projection_id("assignment", ("other", "bh-1")),
        hive="other",
        issue_id="bh-1",
        seat="dev/other",
    )
    provider = MemoryProvider([snapshot("r1", assignments=(foreign,))])

    with pytest.raises(state_stream.StateStreamContractError, match="other"):
        list(state_stream.stream_frames(provider, hive_request()))


def test_scope_mismatch_from_provider_fails_closed():
    provider = MemoryProvider(
        [snapshot("r1", issue("bh-1"), scope=state_stream.StreamScope.FACTORY)]
    )

    with pytest.raises(state_stream.StateStreamContractError, match="factory state"):
        list(state_stream.stream_frames(provider, hive_request()))


def test_same_revision_cannot_name_different_states():
    provider = MemoryProvider(
        [snapshot("r1", issue("bh-1")), snapshot("r1", issue("bh-1", status="closed"))]
    )

    with pytest.raises(state_stream.StateStreamContractError, match="two different"):
        list(state_stream.stream_frames(provider, hive_request()))


def test_same_revision_cannot_name_different_operator_or_partial_state():
    provider = MemoryProvider(
        [
            snapshot("r1", assignments=(assignment(seat="dev/one"),)),
            snapshot("r1", assignments=(assignment(seat="dev/two"),)),
        ]
    )
    with pytest.raises(state_stream.StateStreamContractError, match="two different"):
        list(state_stream.stream_frames(provider, hive_request()))

    provider = MemoryProvider(
        [snapshot("r1"), snapshot("r1", partial=True, partial_reason="dependency_unavailable")]
    )
    with pytest.raises(state_stream.StateStreamContractError, match="two different"):
        list(state_stream.stream_frames(provider, hive_request()))


def test_projection_id_pins_canonical_utf8_vectors():
    assert (
        state_stream.projection_id("work-dependency", ("hive", "bh-child", "bh-parent", "blocks"))
        == "work-dependency:sha256:028328e4342d608bfb2cf9c165c949d5b5599089880671067fdcb37b22d8cc4b"
    )
    assert state_stream.projection_id("assignment", ("hive", "bh-child")) == (
        "assignment:sha256:7ef8b18520618b64b0235ff81cc5eb1856e46384a2df61f467e7c38846c361f9"
    )
    assert state_stream.projection_id("assignment", ("hivé", "任务")) == (
        "assignment:sha256:8be96df25bbef191db4ea3df7aaca62d72b3e36f159c326d2b01f295b72278ed"
    )


def test_operator_records_are_sorted_and_duplicate_ids_fail_closed():
    first = assignment("bh-z")
    second = assignment("bh-a")
    projected = snapshot("r1", assignments=(first, second))

    assert projected.assignments == tuple(sorted((first, second), key=lambda item: item.id))
    with pytest.raises(state_stream.StateStreamContractError, match="duplicate"):
        snapshot("r2", assignments=(first, first))
    with pytest.raises(state_stream.StateStreamContractError, match="stable natural-key id"):
        snapshot(
            "r3",
            assignments=(
                state_stream.Assignment(
                    id="assignment:sha256:not-canonical",
                    hive="beadhive",
                    issue_id="bh-a",
                    seat="dev/one",
                ),
            ),
        )
    with pytest.raises(state_stream.StateStreamContractError, match="wrong entity type"):
        snapshot("r4", assignments=(work_dependency(),))


def test_frame_payload_is_exactly_the_backend_neutral_wire_shape():
    dep = state_stream.StreamDependency("bh-1", "bh-0", "blocks")
    item = state_stream.StreamIssue(
        id="bh-1",
        hive="beadhive",
        issue_type="task",
        status="open",
        priority="P1",
        title="One",
        updated_at="2026-08-24T00:00:00Z",
        dependencies=(dep,),
    )
    frame = next(
        iter(state_stream.stream_frames(MemoryProvider([snapshot("opaque", item)]), hive_request()))
    )

    assert state_stream.frame_payload(frame) == {
        "schema_version": 1,
        "frame": "snapshot",
        "scope": "hive",
        "revision": "opaque",
        "as_of": "2026-08-24T00:00:00Z",
        "partial": False,
        "partial_reason": None,
        "issues": [
            {
                "id": "bh-1",
                "hive": "beadhive",
                "issue_type": "task",
                "status": "open",
                "priority": "P1",
                "title": "One",
                "labels": [],
                "assignee": None,
                "parent_id": None,
                "dependencies": [{"issue_id": "bh-1", "depends_on_id": "bh-0", "type": "blocks"}],
                "updated_at": "2026-08-24T00:00:00Z",
            }
        ],
        "work_dependencies": [],
        "gate_requests": [],
        "epic_schedules": [],
        "assignments": [],
        "reason": "initial",
    }


def test_partial_and_reason_must_be_set_together():
    with pytest.raises(state_stream.StateStreamContractError, match="requires partial_reason"):
        snapshot("r1", partial=True)
    with pytest.raises(state_stream.StateStreamContractError, match="requires partial state"):
        state_stream.ProviderReset(
            scope="hive",
            reason="adapter_error",
            as_of="2026-08-24T00:00:00Z",
            partial_reason="registry_unavailable",
        )
