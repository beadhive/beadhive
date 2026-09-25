from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.problem import Problem
from ...models.setting import Setting
from ...types import Response


def _get_kwargs(
    key: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v0/beads/config/{key}".format(
            key=quote(str(key), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Problem | Setting | None:
    if response.status_code == 200:
        response_200 = Setting.from_dict(response.json())

        return response_200

    if response.status_code == 400:
        response_400 = Problem.from_dict(response.json())

        return response_400

    if response.status_code == 401:
        response_401 = Problem.from_dict(response.json())

        return response_401

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
) -> Response[Problem | Setting]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    key: str,
    *,
    client: AuthenticatedClient,
) -> Response[Problem | Setting]:
    """Get one stored setting

     The value stored for one key, used verbatim: there is no namespace completion, no case folding and
    no dash/underscore equivalence.

    THERE IS NO 404 ON THIS OPERATION, deliberately. A key nothing stored and a key stored as the empty
    string are the same answer here — 200 with `value` absent — because the storage seam behind it
    cannot tell them apart and neither can `bd config get`, which prints "(not set)" for both. Answering
    404 for one of them would publish a distinction this server would have to invent.

    Args:
        key (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | Setting]
    """

    kwargs = _get_kwargs(
        key=key,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    key: str,
    *,
    client: AuthenticatedClient,
) -> Problem | Setting | None:
    """Get one stored setting

     The value stored for one key, used verbatim: there is no namespace completion, no case folding and
    no dash/underscore equivalence.

    THERE IS NO 404 ON THIS OPERATION, deliberately. A key nothing stored and a key stored as the empty
    string are the same answer here — 200 with `value` absent — because the storage seam behind it
    cannot tell them apart and neither can `bd config get`, which prints "(not set)" for both. Answering
    404 for one of them would publish a distinction this server would have to invent.

    Args:
        key (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | Setting
    """

    return sync_detailed(
        key=key,
        client=client,
    ).parsed


async def asyncio_detailed(
    key: str,
    *,
    client: AuthenticatedClient,
) -> Response[Problem | Setting]:
    """Get one stored setting

     The value stored for one key, used verbatim: there is no namespace completion, no case folding and
    no dash/underscore equivalence.

    THERE IS NO 404 ON THIS OPERATION, deliberately. A key nothing stored and a key stored as the empty
    string are the same answer here — 200 with `value` absent — because the storage seam behind it
    cannot tell them apart and neither can `bd config get`, which prints "(not set)" for both. Answering
    404 for one of them would publish a distinction this server would have to invent.

    Args:
        key (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | Setting]
    """

    kwargs = _get_kwargs(
        key=key,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    key: str,
    *,
    client: AuthenticatedClient,
) -> Problem | Setting | None:
    """Get one stored setting

     The value stored for one key, used verbatim: there is no namespace completion, no case folding and
    no dash/underscore equivalence.

    THERE IS NO 404 ON THIS OPERATION, deliberately. A key nothing stored and a key stored as the empty
    string are the same answer here — 200 with `value` absent — because the storage seam behind it
    cannot tell them apart and neither can `bd config get`, which prints "(not set)" for both. Answering
    404 for one of them would publish a distinction this server would have to invent.

    Args:
        key (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | Setting
    """

    return (
        await asyncio_detailed(
            key=key,
            client=client,
        )
    ).parsed
