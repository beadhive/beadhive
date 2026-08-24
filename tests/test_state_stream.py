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
