from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.context_response import ContextResponse
from ...models.problem import Problem
from ...types import Response


def _get_kwargs() -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v0/beads/context",
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ContextResponse | Problem | None:
    if response.status_code == 200:
        response_200 = ContextResponse.from_dict(response.json())

        return response_200

    if response.status_code == 401:
        response_401 = Problem.from_dict(response.json())

        return response_401

    if response.status_code == 500:
        response_500 = Problem.from_dict(response.json())

        return response_500

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ContextResponse | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
) -> Response[ContextResponse | Problem]:
    """Workspace and API identity

     A startup snapshot of the served workspace plus the API's own identity. Serves a fixed field
    allowlist; it does not reflect the server's whole configuration, and in particular never carries a
    sync remote URL (those routinely embed credentials). v0 answers from the snapshot without touching
    the database, which is why no 503 is documented here.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ContextResponse | Problem]
    """

    kwargs = _get_kwargs()

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
) -> ContextResponse | Problem | None:
    """Workspace and API identity

     A startup snapshot of the served workspace plus the API's own identity. Serves a fixed field
    allowlist; it does not reflect the server's whole configuration, and in particular never carries a
    sync remote URL (those routinely embed credentials). v0 answers from the snapshot without touching
    the database, which is why no 503 is documented here.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ContextResponse | Problem
    """

    return sync_detailed(
        client=client,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
) -> Response[ContextResponse | Problem]:
    """Workspace and API identity

     A startup snapshot of the served workspace plus the API's own identity. Serves a fixed field
    allowlist; it does not reflect the server's whole configuration, and in particular never carries a
    sync remote URL (those routinely embed credentials). v0 answers from the snapshot without touching
    the database, which is why no 503 is documented here.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ContextResponse | Problem]
    """

    kwargs = _get_kwargs()

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
) -> ContextResponse | Problem | None:
    """Workspace and API identity

     A startup snapshot of the served workspace plus the API's own identity. Serves a fixed field
    allowlist; it does not reflect the server's whole configuration, and in particular never carries a
    sync remote URL (those routinely embed credentials). v0 answers from the snapshot without touching
    the database, which is why no 503 is documented here.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ContextResponse | Problem
    """

    return (
        await asyncio_detailed(
            client=client,
        )
    ).parsed
