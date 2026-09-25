from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.problem import Problem
from ...models.stats_response import StatsResponse
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    assignee: str | Unset = UNSET,
    skip_blocked: bool | Unset = False,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["assignee"] = assignee

    params["skip_blocked"] = skip_blocked

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v0/beads/stats",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Problem | StatsResponse | None:
    if response.status_code == 200:
        response_200 = StatsResponse.from_dict(response.json())

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
) -> Response[Problem | StatsResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    assignee: str | Unset = UNSET,
    skip_blocked: bool | Unset = False,
) -> Response[Problem | StatsResponse]:
    """Workspace summary statistics

     The counts `bd status` prints and its `bd stats` alias reprints: rows by status across the durable
    plane, plus the two DEPENDENCY-AWARE numbers — the blocked count and the readiness derived from it —
    that make this a different question from a filtered count.

    It is `stats` rather than `status` in the path deliberately. `status` on an HTTP surface reads as
    the SERVER's condition, which is `/healthz`; this operation answers about the WORKSPACE and touches
    the database to do it.

    THE WISP TIER IS NOT INCLUDED in the workspace-wide answer, so a workspace whose work lives in
    ephemeral rows reports zeros here. Supplying `assignee` changes that, along with three other
    definitions — see the parameter.

    THERE IS NO PREDICATE and there will not be one. A count of a set the caller describes is a
    different question with a different answer shape; this operation is the summary or it is nothing.

    Args:
        assignee (str | Unset):
        skip_blocked (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | StatsResponse]
    """

    kwargs = _get_kwargs(
        assignee=assignee,
        skip_blocked=skip_blocked,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    assignee: str | Unset = UNSET,
    skip_blocked: bool | Unset = False,
) -> Problem | StatsResponse | None:
    """Workspace summary statistics

     The counts `bd status` prints and its `bd stats` alias reprints: rows by status across the durable
    plane, plus the two DEPENDENCY-AWARE numbers — the blocked count and the readiness derived from it —
    that make this a different question from a filtered count.

    It is `stats` rather than `status` in the path deliberately. `status` on an HTTP surface reads as
    the SERVER's condition, which is `/healthz`; this operation answers about the WORKSPACE and touches
    the database to do it.

    THE WISP TIER IS NOT INCLUDED in the workspace-wide answer, so a workspace whose work lives in
    ephemeral rows reports zeros here. Supplying `assignee` changes that, along with three other
    definitions — see the parameter.

    THERE IS NO PREDICATE and there will not be one. A count of a set the caller describes is a
    different question with a different answer shape; this operation is the summary or it is nothing.

    Args:
        assignee (str | Unset):
        skip_blocked (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | StatsResponse
    """

    return sync_detailed(
        client=client,
        assignee=assignee,
        skip_blocked=skip_blocked,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    assignee: str | Unset = UNSET,
    skip_blocked: bool | Unset = False,
) -> Response[Problem | StatsResponse]:
    """Workspace summary statistics

     The counts `bd status` prints and its `bd stats` alias reprints: rows by status across the durable
    plane, plus the two DEPENDENCY-AWARE numbers — the blocked count and the readiness derived from it —
    that make this a different question from a filtered count.

    It is `stats` rather than `status` in the path deliberately. `status` on an HTTP surface reads as
    the SERVER's condition, which is `/healthz`; this operation answers about the WORKSPACE and touches
    the database to do it.

    THE WISP TIER IS NOT INCLUDED in the workspace-wide answer, so a workspace whose work lives in
    ephemeral rows reports zeros here. Supplying `assignee` changes that, along with three other
    definitions — see the parameter.

    THERE IS NO PREDICATE and there will not be one. A count of a set the caller describes is a
    different question with a different answer shape; this operation is the summary or it is nothing.

    Args:
        assignee (str | Unset):
        skip_blocked (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | StatsResponse]
    """

    kwargs = _get_kwargs(
        assignee=assignee,
        skip_blocked=skip_blocked,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    assignee: str | Unset = UNSET,
    skip_blocked: bool | Unset = False,
) -> Problem | StatsResponse | None:
    """Workspace summary statistics

     The counts `bd status` prints and its `bd stats` alias reprints: rows by status across the durable
    plane, plus the two DEPENDENCY-AWARE numbers — the blocked count and the readiness derived from it —
    that make this a different question from a filtered count.

    It is `stats` rather than `status` in the path deliberately. `status` on an HTTP surface reads as
    the SERVER's condition, which is `/healthz`; this operation answers about the WORKSPACE and touches
    the database to do it.

    THE WISP TIER IS NOT INCLUDED in the workspace-wide answer, so a workspace whose work lives in
    ephemeral rows reports zeros here. Supplying `assignee` changes that, along with three other
    definitions — see the parameter.

    THERE IS NO PREDICATE and there will not be one. A count of a set the caller describes is a
    different question with a different answer shape; this operation is the summary or it is nothing.

    Args:
        assignee (str | Unset):
        skip_blocked (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | StatsResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            assignee=assignee,
            skip_blocked=skip_blocked,
        )
    ).parsed
