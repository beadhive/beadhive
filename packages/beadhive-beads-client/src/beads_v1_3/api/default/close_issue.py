from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.close_issue_request import CloseIssueRequest
from ...models.close_issue_response import CloseIssueResponse
from ...models.problem import Problem
from ...types import Response


def _get_kwargs(
    id: str,
    *,
    body: CloseIssueRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v0/beads/issues/{id}:close".format(
            id=quote(str(id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> CloseIssueResponse | Problem | None:
    if response.status_code == 200:
        response_200 = CloseIssueResponse.from_dict(response.json())

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
) -> Response[CloseIssueResponse | Problem]:
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
    body: CloseIssueRequest,
) -> Response[CloseIssueResponse | Problem]:
    """Close one issue

     Closes the issue this path names, moving it to the literal `closed` status including from a
    configured done status. It is the half of the agent loop — claim, work, close — that this surface
    did not serve before.

    A named lifecycle action rather than a status patch, because the close carries semantics a patch has
    nowhere to put: the reason and session under first-close-wins, the done-status normalization, and
    the close POLICY vocabulary below.

    THE FIRST CLOSE WINS. A re-close of an already-closed issue is idempotent — 200 with
    `already_closed: true` — and writes neither `reason` nor `session`, so a replayed close cannot
    rewrite the record of why the work ended. The stored pair keeps what the first close gave it until a
    reopen clears both.

    ## Close policy

    An unforced close is refused with `409` / `not_closable` when the issue has open children (the
    refusal carries `open_children`, the count the refusing transaction observed) or a live blocker (no
    such member). The MEMBER'S PRESENCE is the discriminator, so telling the two apart never requires
    reading `detail`. Both refusals are the ROLE's, so `force` bypasses those two and nothing else —
    this endpoint cannot skip a guard by forgetting one exists.

    ## The guard

    `expected_version` is a compare-and-set precondition on the row's revision, checked FIRST — before
    close policy and before the idempotent re-close. A miss refuses the whole request with `409
    precondition_failed` and writes nothing.

    POLICY AND PRECONDITION ARE DIFFERENT THINGS, and `force` is the bypass for exactly one of them. A
    forced close still answers to the guard: the two members say "close it even though the graph
    objects" and "only if this is still the row I read", which are unrelated claims.

    The token travels back on `revision`, so a read-modify-write chain that ends in a close composes its
    expectation from the value the previous write answered with — or, where the chain STARTS with a
    read, from `GET /v0/beads/issues/{id}`'s `revision`, which is the same token and agrees with this
    one.

    ## Planes

    The id resolves across BOTH planes, unlike `POST /v0/beads/issues/{id}:claim`. A close whose target
    is a wisp lands on the unversioned plane and records no durable history entry.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        id (str):
        body (CloseIssueRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CloseIssueResponse | Problem]
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
    body: CloseIssueRequest,
) -> CloseIssueResponse | Problem | None:
    """Close one issue

     Closes the issue this path names, moving it to the literal `closed` status including from a
    configured done status. It is the half of the agent loop — claim, work, close — that this surface
    did not serve before.

    A named lifecycle action rather than a status patch, because the close carries semantics a patch has
    nowhere to put: the reason and session under first-close-wins, the done-status normalization, and
    the close POLICY vocabulary below.

    THE FIRST CLOSE WINS. A re-close of an already-closed issue is idempotent — 200 with
    `already_closed: true` — and writes neither `reason` nor `session`, so a replayed close cannot
    rewrite the record of why the work ended. The stored pair keeps what the first close gave it until a
    reopen clears both.

    ## Close policy

    An unforced close is refused with `409` / `not_closable` when the issue has open children (the
    refusal carries `open_children`, the count the refusing transaction observed) or a live blocker (no
    such member). The MEMBER'S PRESENCE is the discriminator, so telling the two apart never requires
    reading `detail`. Both refusals are the ROLE's, so `force` bypasses those two and nothing else —
    this endpoint cannot skip a guard by forgetting one exists.

    ## The guard

    `expected_version` is a compare-and-set precondition on the row's revision, checked FIRST — before
    close policy and before the idempotent re-close. A miss refuses the whole request with `409
    precondition_failed` and writes nothing.

    POLICY AND PRECONDITION ARE DIFFERENT THINGS, and `force` is the bypass for exactly one of them. A
    forced close still answers to the guard: the two members say "close it even though the graph
    objects" and "only if this is still the row I read", which are unrelated claims.

    The token travels back on `revision`, so a read-modify-write chain that ends in a close composes its
    expectation from the value the previous write answered with — or, where the chain STARTS with a
    read, from `GET /v0/beads/issues/{id}`'s `revision`, which is the same token and agrees with this
    one.

    ## Planes

    The id resolves across BOTH planes, unlike `POST /v0/beads/issues/{id}:claim`. A close whose target
    is a wisp lands on the unversioned plane and records no durable history entry.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        id (str):
        body (CloseIssueRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CloseIssueResponse | Problem
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
    body: CloseIssueRequest,
) -> Response[CloseIssueResponse | Problem]:
    """Close one issue

     Closes the issue this path names, moving it to the literal `closed` status including from a
    configured done status. It is the half of the agent loop — claim, work, close — that this surface
    did not serve before.

    A named lifecycle action rather than a status patch, because the close carries semantics a patch has
    nowhere to put: the reason and session under first-close-wins, the done-status normalization, and
    the close POLICY vocabulary below.

    THE FIRST CLOSE WINS. A re-close of an already-closed issue is idempotent — 200 with
    `already_closed: true` — and writes neither `reason` nor `session`, so a replayed close cannot
    rewrite the record of why the work ended. The stored pair keeps what the first close gave it until a
    reopen clears both.

    ## Close policy

    An unforced close is refused with `409` / `not_closable` when the issue has open children (the
    refusal carries `open_children`, the count the refusing transaction observed) or a live blocker (no
    such member). The MEMBER'S PRESENCE is the discriminator, so telling the two apart never requires
    reading `detail`. Both refusals are the ROLE's, so `force` bypasses those two and nothing else —
    this endpoint cannot skip a guard by forgetting one exists.

    ## The guard

    `expected_version` is a compare-and-set precondition on the row's revision, checked FIRST — before
    close policy and before the idempotent re-close. A miss refuses the whole request with `409
    precondition_failed` and writes nothing.

    POLICY AND PRECONDITION ARE DIFFERENT THINGS, and `force` is the bypass for exactly one of them. A
    forced close still answers to the guard: the two members say "close it even though the graph
    objects" and "only if this is still the row I read", which are unrelated claims.

    The token travels back on `revision`, so a read-modify-write chain that ends in a close composes its
    expectation from the value the previous write answered with — or, where the chain STARTS with a
    read, from `GET /v0/beads/issues/{id}`'s `revision`, which is the same token and agrees with this
    one.

    ## Planes

    The id resolves across BOTH planes, unlike `POST /v0/beads/issues/{id}:claim`. A close whose target
    is a wisp lands on the unversioned plane and records no durable history entry.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        id (str):
        body (CloseIssueRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CloseIssueResponse | Problem]
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
    body: CloseIssueRequest,
) -> CloseIssueResponse | Problem | None:
    """Close one issue

     Closes the issue this path names, moving it to the literal `closed` status including from a
    configured done status. It is the half of the agent loop — claim, work, close — that this surface
    did not serve before.

    A named lifecycle action rather than a status patch, because the close carries semantics a patch has
    nowhere to put: the reason and session under first-close-wins, the done-status normalization, and
    the close POLICY vocabulary below.

    THE FIRST CLOSE WINS. A re-close of an already-closed issue is idempotent — 200 with
    `already_closed: true` — and writes neither `reason` nor `session`, so a replayed close cannot
    rewrite the record of why the work ended. The stored pair keeps what the first close gave it until a
    reopen clears both.

    ## Close policy

    An unforced close is refused with `409` / `not_closable` when the issue has open children (the
    refusal carries `open_children`, the count the refusing transaction observed) or a live blocker (no
    such member). The MEMBER'S PRESENCE is the discriminator, so telling the two apart never requires
    reading `detail`. Both refusals are the ROLE's, so `force` bypasses those two and nothing else —
    this endpoint cannot skip a guard by forgetting one exists.

    ## The guard

    `expected_version` is a compare-and-set precondition on the row's revision, checked FIRST — before
    close policy and before the idempotent re-close. A miss refuses the whole request with `409
    precondition_failed` and writes nothing.

    POLICY AND PRECONDITION ARE DIFFERENT THINGS, and `force` is the bypass for exactly one of them. A
    forced close still answers to the guard: the two members say "close it even though the graph
    objects" and "only if this is still the row I read", which are unrelated claims.

    The token travels back on `revision`, so a read-modify-write chain that ends in a close composes its
    expectation from the value the previous write answered with — or, where the chain STARTS with a
    read, from `GET /v0/beads/issues/{id}`'s `revision`, which is the same token and agrees with this
    one.

    ## Planes

    The id resolves across BOTH planes, unlike `POST /v0/beads/issues/{id}:claim`. A close whose target
    is a wisp lands on the unversioned plane and records no durable history entry.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        id (str):
        body (CloseIssueRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CloseIssueResponse | Problem
    """

    return (
        await asyncio_detailed(
            id=id,
            client=client,
            body=body,
        )
    ).parsed
