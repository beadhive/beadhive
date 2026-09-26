from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.claim_request import ClaimRequest
from ...models.claim_response import ClaimResponse
from ...models.problem import Problem
from ...types import Response


def _get_kwargs(
    id: str,
    *,
    body: ClaimRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v0/beads/issues/{id}:claim".format(
            id=quote(str(id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ClaimResponse | Problem | None:
    if response.status_code == 200:
        response_200 = ClaimResponse.from_dict(response.json())

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
) -> Response[ClaimResponse | Problem]:
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
    body: ClaimRequest,
) -> Response[ClaimResponse | Problem]:
    """Claim an issue for an actor

     Compare-and-set claim. The caller always names the actor: the server never infers one, because its
    own identity is meaningless for remote callers.

    A re-claim by the CURRENT holder is idempotent — 200 with `already_claimed: true` — matching CLI
    semantics. A claim held by a different actor is 409 `already_claimed` and carries the holder in the
    `assignee` extension member, read inside the same transaction; an issue in a non-claimable state is
    409 `not_claimable` and carries `issue_status`. Neither needs the client to parse prose.

    Args:
        id (str):
        body (ClaimRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ClaimResponse | Problem]
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
    body: ClaimRequest,
) -> ClaimResponse | Problem | None:
    """Claim an issue for an actor

     Compare-and-set claim. The caller always names the actor: the server never infers one, because its
    own identity is meaningless for remote callers.

    A re-claim by the CURRENT holder is idempotent — 200 with `already_claimed: true` — matching CLI
    semantics. A claim held by a different actor is 409 `already_claimed` and carries the holder in the
    `assignee` extension member, read inside the same transaction; an issue in a non-claimable state is
    409 `not_claimable` and carries `issue_status`. Neither needs the client to parse prose.

    Args:
        id (str):
        body (ClaimRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ClaimResponse | Problem
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
    body: ClaimRequest,
) -> Response[ClaimResponse | Problem]:
    """Claim an issue for an actor

     Compare-and-set claim. The caller always names the actor: the server never infers one, because its
    own identity is meaningless for remote callers.

    A re-claim by the CURRENT holder is idempotent — 200 with `already_claimed: true` — matching CLI
    semantics. A claim held by a different actor is 409 `already_claimed` and carries the holder in the
    `assignee` extension member, read inside the same transaction; an issue in a non-claimable state is
    409 `not_claimable` and carries `issue_status`. Neither needs the client to parse prose.

    Args:
        id (str):
        body (ClaimRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ClaimResponse | Problem]
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
    body: ClaimRequest,
) -> ClaimResponse | Problem | None:
    """Claim an issue for an actor

     Compare-and-set claim. The caller always names the actor: the server never infers one, because its
    own identity is meaningless for remote callers.

    A re-claim by the CURRENT holder is idempotent — 200 with `already_claimed: true` — matching CLI
    semantics. A claim held by a different actor is 409 `already_claimed` and carries the holder in the
    `assignee` extension member, read inside the same transaction; an issue in a non-claimable state is
    409 `not_claimable` and carries `issue_status`. Neither needs the client to parse prose.

    Args:
        id (str):
        body (ClaimRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ClaimResponse | Problem
    """

    return (
        await asyncio_detailed(
            id=id,
            client=client,
            body=body,
        )
    ).parsed
