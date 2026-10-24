"""One conformance suite, two implementations (spike bh-gj1hb).

``memory`` is the in-memory fake (proposed ``beadhive.testing.tracker``); ``github`` is the
GitHub adapter prototype over the offline REST stand-in (``httpx.MockTransport``). Both must
pass every case in ``conformance.CASES``.
"""

from __future__ import annotations

import httpx
import pytest
from conformance import CASES, Subject, TrackerCase
from fake_github_rest import API, OWNER, REPO, FakeGitHubREST
from github_adapter import GitHubTrackerAdapter
from memory_tracker import MemoryTracker, MemoryWorld
from tracker_contracts import Operation, RetryBudget


def memory_factory(*, enabled=frozenset(), page_size=2, budget=None) -> Subject:
    world = MemoryWorld()
    adapter = MemoryTracker(
        world, enabled=enabled, page_size=page_size, budget=budget or RetryBudget()
    )
    return Subject(adapter, world)


def github_factory(*, enabled=frozenset(), page_size=2, budget=None) -> Subject:
    server = FakeGitHubREST()
    client = httpx.Client(
        base_url=API,
        transport=server.transport(),
        headers={"Authorization": "token not-a-real-token"},
    )
    adapter = GitHubTrackerAdapter(
        client,
        owner=OWNER,
        repo=REPO,
        sleep=server.sleep,
        enabled=enabled,
        budget=budget or RetryBudget(),
        page_size=page_size,
        credential_source="env:GITHUB_TOKEN",
    )
    return Subject(adapter, server)


FACTORIES = {"memory": memory_factory, "github": github_factory}


@pytest.mark.parametrize("impl", sorted(FACTORIES))
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_conforms(impl: str, case: TrackerCase) -> None:
    case.check(FACTORIES[impl])


def test_github_probe_reports_unknown_for_fine_grained_tokens() -> None:
    subject = github_factory(enabled=frozenset({Operation.COMMENT_CREATE}))
    subject.world.scopes = None  # no X-OAuth-Scopes header: grants are unreadable
    report = subject.adapter.probe_capabilities()
    state = report.states[Operation.COMMENT_CREATE]
    assert state.enabled and state.probe.value == "unknown" and state.usable
    assert report.credential_source == "env:GITHUB_TOKEN"  # provenance, never a value
    assert {r.method for r in subject.world.requests} == {"GET"}


def test_github_probe_is_definitive_no_for_an_archived_repository() -> None:
    subject = github_factory(enabled=frozenset({Operation.LABELS_CHANGE}))
    subject.world.repo["archived"] = True
    state = subject.adapter.probe_capabilities().states[Operation.LABELS_CHANGE]
    assert state.probe.value == "no" and not state.usable


def test_github_quoted_marker_is_not_ownership() -> None:
    from github_adapter import decode_marker

    quoted = "> <!-- bh:v1 hive=acme/widgets install=i outbox=o -->\n\nI agree"
    assert decode_marker(quoted)[1] is None
