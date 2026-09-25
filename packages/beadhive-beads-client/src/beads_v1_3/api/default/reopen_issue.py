from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.problem import Problem
from ...models.reopen_issue_request import ReopenIssueRequest
from ...models.reopen_issue_response import ReopenIssueResponse
from ...types import Response


def _get_kwargs(
    id: str,
    *,
    body: ReopenIssueRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v0/beads/issues/{id}:reopen".format(
            id=quote(str(id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Problem | ReopenIssueResponse | None:
    if response.status_code == 200:
        response_200 = ReopenIssueResponse.from_dict(response.json())

        return response_200

    if response.status_code == 400:
        response_400 = Problem.from_dict(response.json())

        return response_400

    if response.status_code == 401:
        response_401 = Problem.from_dict(response.json())

        return response_401

    if response.status_code == 404:
        response_404 = Problem.from_dict(response.json())

        return response_404

    if response.status_code == 409:
        response_409 = Problem.from_dict(response.json())

        return response_409

    if response.status_code == 500:
        response_500 = Problem.from_dict(response.json())

        return response_500

    if response.status_code == 503:
        response_503 = Problem.from_dict(response.json())

        return response_503

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Problem | ReopenIssueResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    id: str,
    *,
    client: AuthenticatedClient,
    body: ReopenIssueRequest,
) -> Response[Problem | ReopenIssueResponse]:
    """Reopen one issue

     Moves the literal `closed` status and every configured done status back to `open`. It is `POST
    /v0/beads/issues/{id}:close`'s mirror, and it completes the lifecycle pair so a recovery flow works
    end to end over this surface.

    A reopen of an issue that is NOT done changes nothing and succeeds — 200 with `already_open: true`,
    the re-claim's and the re-close's answer to the same question. An agent replaying its own recovery
    should not have to classify an error to learn it already ran.

    Reopening CLEARS `close_reason` and `closed_by_session`, because they describe a closure that no
    longer holds. That is what makes the close's first-close-wins rule survivable: the way to write a
    new reason is to reopen and close again, not to re-close.

    ## Where the reason is recorded

    On the `reopened` EVENT this move records, not on a field of the issue, and the response does not
    carry it. A caller that wants it back reads the issue's events — the same place the actor
    attribution for the reopen lives. A reopen with no reason still records that entry; it simply
    carries none.

    ## No POLICY conflict to name

    There is still no policy refusal here, and the absence is deliberate: close has one — open children,
    a live blocker — and reopen is the direction that removes an issue from the done category rather
    than adding it, so there is nothing for a policy to refuse. `not_closable` is not in this
    operation's vocabulary and never will be.

    The one `409` it does document is a PRECONDITION rather than a policy. `expected_version` is a
    compare-and-set on the row's revision, checked FIRST — before the non-done no-op — and a miss
    refuses the whole request with `precondition_failed` and writes nothing. It is the caller's own
    guard rather than a rule of the graph, which is why the operation can carry it while carrying no
    policy conflict at all.

    The token travels back on `revision`, so a reopen-then-re-close recovery composes its next
    expectation from the value this operation answered with, and a recovery that starts by reading
    composes its first from `GET /v0/beads/issues/{id}`'s `revision`.

    ## Planes

    The id resolves across BOTH planes, as the close does. A reopen whose target is a wisp lands on the
    unversioned plane and records no durable history entry.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        id (str):
        body (ReopenIssueRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | ReopenIssueResponse]
    """

    kwargs = _get_kwargs(
        id=id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    id: str,
    *,
    client: AuthenticatedClient,
    body: ReopenIssueRequest,
) -> Problem | ReopenIssueResponse | None:
    """Reopen one issue

     Moves the literal `closed` status and every configured done status back to `open`. It is `POST
    /v0/beads/issues/{id}:close`'s mirror, and it completes the lifecycle pair so a recovery flow works
    end to end over this surface.

    A reopen of an issue that is NOT done changes nothing and succeeds — 200 with `already_open: true`,
    the re-claim's and the re-close's answer to the same question. An agent replaying its own recovery
    should not have to classify an error to learn it already ran.

    Reopening CLEARS `close_reason` and `closed_by_session`, because they describe a closure that no
    longer holds. That is what makes the close's first-close-wins rule survivable: the way to write a
    new reason is to reopen and close again, not to re-close.

    ## Where the reason is recorded

    On the `reopened` EVENT this move records, not on a field of the issue, and the response does not
    carry it. A caller that wants it back reads the issue's events — the same place the actor
    attribution for the reopen lives. A reopen with no reason still records that entry; it simply
    carries none.

    ## No POLICY conflict to name

    There is still no policy refusal here, and the absence is deliberate: close has one — open children,
    a live blocker — and reopen is the direction that removes an issue from the done category rather
    than adding it, so there is nothing for a policy to refuse. `not_closable` is not in this
    operation's vocabulary and never will be.

    The one `409` it does document is a PRECONDITION rather than a policy. `expected_version` is a
    compare-and-set on the row's revision, checked FIRST — before the non-done no-op — and a miss
    refuses the whole request with `precondition_failed` and writes nothing. It is the caller's own
    guard rather than a rule of the graph, which is why the operation can carry it while carrying no
    policy conflict at all.

    The token travels back on `revision`, so a reopen-then-re-close recovery composes its next
    expectation from the value this operation answered with, and a recovery that starts by reading
    composes its first from `GET /v0/beads/issues/{id}`'s `revision`.

    ## Planes

    The id resolves across BOTH planes, as the close does. A reopen whose target is a wisp lands on the
    unversioned plane and records no durable history entry.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        id (str):
        body (ReopenIssueRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | ReopenIssueResponse
    """

    return sync_detailed(
        id=id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    id: str,
    *,
    client: AuthenticatedClient,
    body: ReopenIssueRequest,
) -> Response[Problem | ReopenIssueResponse]:
    """Reopen one issue

     Moves the literal `closed` status and every configured done status back to `open`. It is `POST
    /v0/beads/issues/{id}:close`'s mirror, and it completes the lifecycle pair so a recovery flow works
    end to end over this surface.

    A reopen of an issue that is NOT done changes nothing and succeeds — 200 with `already_open: true`,
    the re-claim's and the re-close's answer to the same question. An agent replaying its own recovery
    should not have to classify an error to learn it already ran.

    Reopening CLEARS `close_reason` and `closed_by_session`, because they describe a closure that no
    longer holds. That is what makes the close's first-close-wins rule survivable: the way to write a
    new reason is to reopen and close again, not to re-close.

    ## Where the reason is recorded

    On the `reopened` EVENT this move records, not on a field of the issue, and the response does not
    carry it. A caller that wants it back reads the issue's events — the same place the actor
    attribution for the reopen lives. A reopen with no reason still records that entry; it simply
    carries none.

    ## No POLICY conflict to name

    There is still no policy refusal here, and the absence is deliberate: close has one — open children,
    a live blocker — and reopen is the direction that removes an issue from the done category rather
    than adding it, so there is nothing for a policy to refuse. `not_closable` is not in this
    operation's vocabulary and never will be.

    The one `409` it does document is a PRECONDITION rather than a policy. `expected_version` is a
    compare-and-set on the row's revision, checked FIRST — before the non-done no-op — and a miss
    refuses the whole request with `precondition_failed` and writes nothing. It is the caller's own
    guard rather than a rule of the graph, which is why the operation can carry it while carrying no
    policy conflict at all.

    The token travels back on `revision`, so a reopen-then-re-close recovery composes its next
    expectation from the value this operation answered with, and a recovery that starts by reading
    composes its first from `GET /v0/beads/issues/{id}`'s `revision`.

    ## Planes

    The id resolves across BOTH planes, as the close does. A reopen whose target is a wisp lands on the
    unversioned plane and records no durable history entry.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        id (str):
        body (ReopenIssueRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | ReopenIssueResponse]
    """

    kwargs = _get_kwargs(
        id=id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    id: str,
    *,
    client: AuthenticatedClient,
    body: ReopenIssueRequest,
) -> Problem | ReopenIssueResponse | None:
    """Reopen one issue

     Moves the literal `closed` status and every configured done status back to `open`. It is `POST
    /v0/beads/issues/{id}:close`'s mirror, and it completes the lifecycle pair so a recovery flow works
    end to end over this surface.

    A reopen of an issue that is NOT done changes nothing and succeeds — 200 with `already_open: true`,
    the re-claim's and the re-close's answer to the same question. An agent replaying its own recovery
    should not have to classify an error to learn it already ran.

    Reopening CLEARS `close_reason` and `closed_by_session`, because they describe a closure that no
    longer holds. That is what makes the close's first-close-wins rule survivable: the way to write a
    new reason is to reopen and close again, not to re-close.

    ## Where the reason is recorded

    On the `reopened` EVENT this move records, not on a field of the issue, and the response does not
    carry it. A caller that wants it back reads the issue's events — the same place the actor
    attribution for the reopen lives. A reopen with no reason still records that entry; it simply
    carries none.

    ## No POLICY conflict to name

    There is still no policy refusal here, and the absence is deliberate: close has one — open children,
    a live blocker — and reopen is the direction that removes an issue from the done category rather
    than adding it, so there is nothing for a policy to refuse. `not_closable` is not in this
    operation's vocabulary and never will be.

    The one `409` it does document is a PRECONDITION rather than a policy. `expected_version` is a
    compare-and-set on the row's revision, checked FIRST — before the non-done no-op — and a miss
    refuses the whole request with `precondition_failed` and writes nothing. It is the caller's own
    guard rather than a rule of the graph, which is why the operation can carry it while carrying no
    policy conflict at all.

    The token travels back on `revision`, so a reopen-then-re-close recovery composes its next
    expectation from the value this operation answered with, and a recovery that starts by reading
    composes its first from `GET /v0/beads/issues/{id}`'s `revision`.

    ## Planes

    The id resolves across BOTH planes, as the close does. A reopen whose target is a wisp lands on the
    unversioned plane and records no durable history entry.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        id (str):
        body (ReopenIssueRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | ReopenIssueResponse
    """

    return (
        await asyncio_detailed(
            id=id,
            client=client,
            body=body,
        )
    ).parsed
