"""Read-path behaviour beyond the shared cases: enablement, scope, cursors, the wire form, the
HTTP cache switch, the factory's settings, and the planted-token leak check."""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx
import pytest

from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    Checkpoint,
    CommentQuery,
    IdQuery,
    IdSubject,
    IssueKey,
    IssueQuery,
    IssueState,
    NotVisible,
    Operation,
    OperationNotEnabled,
    OtherProject,
    PageCursor,
    PermissionDenied,
    ProjectRef,
    RetryBudget,
    TargetStateRefused,
    TrackerError,
)
from beadhive_plugins.credentials import ResolvedToken
from beadhive_tracker_github.adapter import build_adapter, diagnostics
from beadhive_tracker_github.adapter.transport import API_VERSION
from tracker_github_fake import EPOCH, FakeGitHub, adapter_for

PROJECT = ProjectRef("github", "acme/widgets")


def at(days: int):
    return EPOCH + timedelta(days=days)


# ---- enablement: a disabled read never reaches GitHub -----------------------------------------


@pytest.mark.parametrize(
    ("enabled", "call"),
    [
        (frozenset(), lambda a: a.list_issues(IssueQuery())),
        (frozenset({Operation.COMMENTS_READ}), lambda a: a.list_issues(IssueQuery())),
        (frozenset({Operation.ISSUES_READ}), lambda a: a.list_comments(CommentQuery())),
        (frozenset({Operation.COMMENTS_READ}), lambda a: a.list_ids(IdQuery(IdSubject.ISSUES))),
        (frozenset({Operation.ISSUES_READ}), lambda a: a.list_ids(IdQuery(IdSubject.COMMENTS))),
    ],
)
def test_a_disabled_read_is_refused_before_any_request_or_credential(enabled, call):
    world = FakeGitHub()
    resolved: list[int] = []

    def tokens():
        resolved.append(1)
        return ResolvedToken(world.token, "env:GITHUB_TOKEN")

    with pytest.raises(OperationNotEnabled):
        call(adapter_for(world, enabled=enabled, tokens=tokens))
    assert world.requests == [] and resolved == []


def test_identity_is_not_gated_by_a_read_flag():
    world = FakeGitHub()
    adapter = adapter_for(world, enabled=frozenset())
    assert adapter.whoami() is not None
    assert adapter.probe_capabilities().states[Operation.ISSUES_READ].enabled is False


def test_the_credential_is_resolved_once_per_operation_and_never_cached():
    world = FakeGitHub()
    world.add_issue("one", at=at(1))
    calls: list[int] = []

    def tokens():
        calls.append(1)
        return ResolvedToken(world.token, "env:GITHUB_TOKEN")

    adapter = adapter_for(world, tokens=tokens)
    adapter.list_issues(IssueQuery())
    adapter.probe_capabilities()  # two GETs, one operation
    adapter.list_issues(IssueQuery())
    assert len(calls) == 3


def test_a_failing_token_source_is_an_authentication_failure_that_repeats_nothing():
    secret = "ghp_" + "S" * 36

    def tokens():
        raise RuntimeError(f"could not read {secret}")

    with pytest.raises(TrackerError) as caught:
        adapter_for(FakeGitHub(), tokens=tokens).list_issues(IssueQuery())
    assert secret not in str(caught.value) and caught.value.__cause__ is None


# ---- scope and the wire form ------------------------------------------------------------------


def test_every_request_is_a_get_pinned_to_the_recorded_api_version():
    world = FakeGitHub()
    world.add_issue("one", at=at(1))
    adapter = adapter_for(world)
    adapter.list_issues(IssueQuery())
    adapter.list_comments(CommentQuery())
    adapter.probe_capabilities()
    assert {r.method for r in world.requests} == {"GET"}
    assert {r.headers["x-github-api-version"] for r in world.requests} == {API_VERSION}
    assert all(r.headers["authorization"] == f"token {world.token}" for r in world.requests)


def test_read_include_closed_false_narrows_listings_but_never_the_id_snapshot():
    world = FakeGitHub()
    world.add_issue("open", at=at(1))
    world.add_issue("closed", at=at(2), state=IssueState.CLOSED)
    adapter = adapter_for(world, include_closed=False)
    assert [i.title for i in adapter.list_issues(IssueQuery()).items] == ["open"]
    assert world.requests[-1].url.params["state"] == "open"
    ids = adapter.list_ids(IdQuery(IdSubject.ISSUES))
    assert len(ids.ids) == 2  # a closed issue still exists: never inferred deleted
    assert world.requests[-1].url.params["state"] == "all"


def test_the_since_parameter_is_github_iso_8601_with_z():
    world = FakeGitHub()
    adapter_for(world).list_issues(IssueQuery(since=Checkpoint("github:2026-01-02T03:04:05Z")))
    assert world.requests[0].url.params["since"] == "2026-01-02T03:04:05Z"


def test_a_checkpoint_or_cursor_from_elsewhere_is_refused_before_any_request():
    world = FakeGitHub()
    adapter = adapter_for(world)
    with pytest.raises(ValueError, match="another adapter"):
        adapter.list_issues(IssueQuery(since=Checkpoint("memory:2026-01-01T00:00:00+00:00")))
    for token in (
        '{"hw": null, "next": "https://evil.example/repos/acme/widgets/issues?page=2"}',
        '{"hw": null, "next": "http://api.github.com/repositories/9001/issues?page=2"}',
        '{"hw": null, "next": "https://user@api.github.com/repositories/9001/issues"}',
        "https://api.github.com/repositories/9001/issues?page=2",  # not this adapter's form
        "not json",
    ):
        with pytest.raises(ValueError, match="another adapter or host"):
            adapter.list_issues(IssueQuery(cursor=PageCursor(token)))
    assert world.requests == []


def test_a_comment_listing_for_another_projects_issue_is_refused():
    world = FakeGitHub()
    with pytest.raises(OtherProject):
        adapter_for(world).list_comments(
            CommentQuery(issue=IssueKey("#1", "https://github.com/someone/else/issues/1"))
        )
    assert world.requests == []


def test_issues_disabled_on_the_repository_is_a_state_refusal():
    world = FakeGitHub()
    world.repo_meta["has_issues"] = False
    with pytest.raises(TargetStateRefused, match="410 issues are disabled"):
        adapter_for(world).list_issues(IssueQuery())


def test_a_payload_the_mapping_cannot_read_is_a_tracker_error_naming_no_value():
    world = FakeGitHub()
    world.scripted.append(httpx.Response(200, json=[{"number": "x", "html_url": "u"}]))
    with pytest.raises(TrackerError) as caught:
        adapter_for(world).list_issues(IssueQuery())
    assert "x" not in str(caught.value).split()  # the field is named, not the value
    world.scripted.append(httpx.Response(200, json={"not": "a list"}))
    with pytest.raises(TrackerError, match="not a list"):
        adapter_for(world).list_issues(IssueQuery())


# ---- the HTTP cache switch --------------------------------------------------------------------


def _etag_world() -> FakeGitHub:
    world = FakeGitHub()
    world.add_issue("one", at=at(1))
    plain = world.handler

    def with_etags(request):
        if request.headers.get("if-none-match") == 'W/"v1"':
            world.requests.append(request)
            return httpx.Response(304, headers={"etag": 'W/"v1"'})
        response = plain(request)
        response.headers["etag"] = 'W/"v1"'
        response.headers["cache-control"] = "private, max-age=60, s-maxage=60"
        return response

    world.handler = with_etags  # type: ignore[method-assign]
    return world


def test_the_http_cache_is_off_by_default_so_no_request_is_conditional():
    world = _etag_world()
    adapter = adapter_for(world)
    for _ in range(2):
        adapter.list_issues(IssueQuery())
    assert all("if-none-match" not in r.headers for r in world.requests)


def test_the_http_cache_opt_in_revalidates_with_the_etag_and_answers_from_its_own_store():
    world = _etag_world()
    adapter = adapter_for(world, http_cache=True)
    first = adapter.list_issues(IssueQuery())
    second = adapter.list_issues(IssueQuery())
    assert "if-none-match" not in world.requests[0].headers
    assert world.requests[1].headers["if-none-match"] == 'W/"v1"'
    assert [i.key for i in second.items] == [i.key for i in first.items]
    other = adapter_for(world, http_cache=True)  # another adapter: another store
    other.list_issues(IssueQuery())
    assert "if-none-match" not in world.requests[2].headers


# ---- the factory's settings -------------------------------------------------------------------


def test_the_settings_become_explicit_adapter_arguments():
    world = FakeGitHub()
    settings = {
        "enabled": True,
        "base_url": None,
        "read": {
            "issues": {"enabled": True},
            "comments": {"enabled": False},
            "include_closed": False,
            "page_size": 7,
        },
        "http_cache": {"enabled": False},
        "retry": {"max_retries": 1, "max_single_wait_seconds": 5, "max_total_wait_seconds": 9.5},
        "write": {"comment": {"enabled": True}, "labels": {"enabled": True, "owned_prefixes": []}},
    }
    adapter = build_adapter(
        settings,
        lambda: ResolvedToken(world.token, "env:GH_TOKEN"),
        project=PROJECT,
        sleep=world.sleep,
        clock=world.clock.now,
        transport=world.transport(),
    )
    # labels.change needs an owned prefix, so only comment.create is enabled among the writes.
    assert adapter.enabled == {Operation.ISSUES_READ, Operation.COMMENT_CREATE}
    summary = adapter.settings_summary
    assert summary["include_closed"] is False and summary["page_size"] == 7
    assert summary["retry"] == RetryBudget(1, timedelta(seconds=5), timedelta(seconds=9.5))
    adapter.list_issues(IssueQuery())
    assert world.requests[0].url.params["per_page"] == "7"
    assert world.requests[0].url.params["state"] == "open"


def test_default_settings_enable_nothing():
    world = FakeGitHub()
    adapter = build_adapter({}, lambda: None, project=PROJECT, sleep=world.sleep)
    assert adapter.enabled == frozenset()
    assert adapter.settings_summary["http_cache"] is False
    assert adapter.settings_summary["retry"] == RetryBudget()
    with pytest.raises(OperationNotEnabled):
        adapter.list_issues(IssueQuery())


@pytest.mark.parametrize(
    "settings",
    [
        {"read": {"issues": {"enabled": "true"}}},  # strict: a string is not a bool
        {"read": {"page_size": 0}},
        {"read": {"page_size": 101}},
        {"retry": {"max_retries": -1}},
        {"retry": {"max_single_wait_seconds": 0}},
        {"retry": {"max_single_wait_seconds": 30, "max_total_wait_seconds": 10}},
        {"http_cache": {"enabled": 1}},
        {"base_url": "http://ghe.example/api/v3"},
        {"base_url": "https://ghe.example/api/v3?x=1"},
    ],
)
def test_invalid_settings_are_refused_with_a_static_message(settings):
    with pytest.raises(ValueError) as caught:
        build_adapter(settings, lambda: None, project=PROJECT, sleep=lambda _s: None)
    assert "true" not in str(caught.value) and "ghe.example" not in str(caught.value)


# ---- the planted-token leak check ---------------------------------------------------------------


def _leaky_world(token: str, other: str) -> FakeGitHub:
    """Every refusal echoes the credential back, as a hostile or careless proxy might."""
    world = FakeGitHub(token=token)
    world.add_issue("one", at=at(1))
    echo = f"token {token} rejected (also saw {other})"
    world.scripted.extend(
        [
            httpx.Response(403, json={"message": f"Resource not accessible: {echo}"}),
            httpx.Response(404, json={"message": echo}),
            httpx.Response(422, json={"message": echo}),
            *(httpx.Response(502, json={"message": echo}) for _ in range(4)),
        ]
    )
    return world


def _haystack(token: str, other: str, caplog, tmp_path) -> str:
    world = _leaky_world(token, other)
    adapter = adapter_for(world, tokens=lambda: ResolvedToken(token, "env:GITHUB_TOKEN"))
    seen: list[str] = [repr(adapter), str(adapter), repr(adapter.settings_summary)]
    with caplog.at_level(logging.DEBUG):
        for _ in range(4):
            try:
                adapter.list_issues(IssueQuery())
            except (PermissionDenied, NotVisible, TrackerError) as exc:
                seen += [str(exc), repr(exc), repr(exc.args), str(vars(exc))]
                (tmp_path / f"error-{len(seen)}.txt").write_text(str(exc), encoding="utf-8")
        report = adapter.probe_capabilities()
        page = adapter.list_issues(IssueQuery())
    seen += [repr(report), repr(page), caplog.text]
    seen += [p.read_text(encoding="utf-8") for p in tmp_path.iterdir()]
    return "\n".join(seen)


def test_a_planted_token_never_appears_in_errors_reprs_logs_or_files(caplog, tmp_path):
    token = "ghp_" + "PlantedSecretValue" * 2 + "Zq"
    other = "github_pat_" + "AnotherPlantedOne" * 3
    haystack = _haystack(token, other, caplog, tmp_path)
    assert token not in haystack and other not in haystack
    assert "***" in haystack  # the echoes were there, and were scrubbed


def test_negative_control_the_leak_check_detects_an_unscrubbed_echo(caplog, tmp_path, monkeypatch):
    """With the scrub disabled the same haystack DOES contain the token: the check is not
    vacuous."""
    monkeypatch.setattr(diagnostics, "scrub", lambda text, secrets=(): text)
    token = "ghp_" + "PlantedSecretValue" * 2 + "Zq"
    other = "github_pat_" + "AnotherPlantedOne" * 3
    assert token in _haystack(token, other, caplog, tmp_path)


def test_reads_enabled_by_default_in_the_conformance_helper_only():
    """Guard for the helper itself: production defaults enable nothing (see above)."""
    assert adapter_for(FakeGitHub()).enabled == READ_OPERATIONS
