from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.cycles_page import CyclesPage
from ...models.problem import Problem
from ...types import Response


def _get_kwargs() -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v0/beads/dependencies/cycles",
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> CyclesPage | Problem | None:
    if response.status_code == 200:
        response_200 = CyclesPage.from_dict(response.json())

        return response_200

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
) -> Response[CyclesPage | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
) -> Response[CyclesPage | Problem]:
    """List dependency cycles

     Every circular blocking dependency in the workspace, the same sweep `bd dep cycles` performs. Items
    are `Cycle` — the element type that command's `--json` emits — so the two surfaces carry one shape.

    THE ANSWER IS CANONICAL: each cycle's members are rotated so the lowest id comes first, and the
    cycles are sorted against each other, so two calls against an unchanged workspace return the same
    bytes. A client may diff two snapshots and read a difference as a real change.

    EDGES ARE NARROWER THAN THEY ARE AT WRITE TIME. The walk follows `blocks` and `conditional-blocks`
    only: `waits-for` is gate semantics, so a mutual wait is not a deadlock, and `parent-child` is
    walked by the refusal a dependency WRITE performs but not by this report. A workspace this operation
    calls clean can therefore still refuse an edge.

    Both dependency planes — durable and ephemeral — are one graph here, so a cycle that runs issue →
    wisp → issue is reported.

    THERE IS NO `limit` AND NO CURSOR, and `has_more` is therefore always false in v0. Truncating would
    shrink the count, and the count is the number an operator acts on; the response is bounded by the
    number of cycles in the workspace, which in a healthy one is zero. `has_more` is present so that
    adding a bound later is additive rather than a new envelope.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CyclesPage | Problem]
    """

    kwargs = _get_kwargs()

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
) -> CyclesPage | Problem | None:
    """List dependency cycles

     Every circular blocking dependency in the workspace, the same sweep `bd dep cycles` performs. Items
    are `Cycle` — the element type that command's `--json` emits — so the two surfaces carry one shape.

    THE ANSWER IS CANONICAL: each cycle's members are rotated so the lowest id comes first, and the
    cycles are sorted against each other, so two calls against an unchanged workspace return the same
    bytes. A client may diff two snapshots and read a difference as a real change.

    EDGES ARE NARROWER THAN THEY ARE AT WRITE TIME. The walk follows `blocks` and `conditional-blocks`
    only: `waits-for` is gate semantics, so a mutual wait is not a deadlock, and `parent-child` is
    walked by the refusal a dependency WRITE performs but not by this report. A workspace this operation
    calls clean can therefore still refuse an edge.

    Both dependency planes — durable and ephemeral — are one graph here, so a cycle that runs issue →
    wisp → issue is reported.

    THERE IS NO `limit` AND NO CURSOR, and `has_more` is therefore always false in v0. Truncating would
    shrink the count, and the count is the number an operator acts on; the response is bounded by the
    number of cycles in the workspace, which in a healthy one is zero. `has_more` is present so that
    adding a bound later is additive rather than a new envelope.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CyclesPage | Problem
    """

    return sync_detailed(
        client=client,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
) -> Response[CyclesPage | Problem]:
    """List dependency cycles

     Every circular blocking dependency in the workspace, the same sweep `bd dep cycles` performs. Items
    are `Cycle` — the element type that command's `--json` emits — so the two surfaces carry one shape.

    THE ANSWER IS CANONICAL: each cycle's members are rotated so the lowest id comes first, and the
    cycles are sorted against each other, so two calls against an unchanged workspace return the same
    bytes. A client may diff two snapshots and read a difference as a real change.

    EDGES ARE NARROWER THAN THEY ARE AT WRITE TIME. The walk follows `blocks` and `conditional-blocks`
    only: `waits-for` is gate semantics, so a mutual wait is not a deadlock, and `parent-child` is
    walked by the refusal a dependency WRITE performs but not by this report. A workspace this operation
    calls clean can therefore still refuse an edge.

    Both dependency planes — durable and ephemeral — are one graph here, so a cycle that runs issue →
    wisp → issue is reported.

    THERE IS NO `limit` AND NO CURSOR, and `has_more` is therefore always false in v0. Truncating would
    shrink the count, and the count is the number an operator acts on; the response is bounded by the
    number of cycles in the workspace, which in a healthy one is zero. `has_more` is present so that
    adding a bound later is additive rather than a new envelope.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CyclesPage | Problem]
    """

    kwargs = _get_kwargs()

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
) -> CyclesPage | Problem | None:
    """List dependency cycles

     Every circular blocking dependency in the workspace, the same sweep `bd dep cycles` performs. Items
    are `Cycle` — the element type that command's `--json` emits — so the two surfaces carry one shape.

    THE ANSWER IS CANONICAL: each cycle's members are rotated so the lowest id comes first, and the
    cycles are sorted against each other, so two calls against an unchanged workspace return the same
    bytes. A client may diff two snapshots and read a difference as a real change.

    EDGES ARE NARROWER THAN THEY ARE AT WRITE TIME. The walk follows `blocks` and `conditional-blocks`
    only: `waits-for` is gate semantics, so a mutual wait is not a deadlock, and `parent-child` is
    walked by the refusal a dependency WRITE performs but not by this report. A workspace this operation
    calls clean can therefore still refuse an edge.

    Both dependency planes — durable and ephemeral — are one graph here, so a cycle that runs issue →
    wisp → issue is reported.

    THERE IS NO `limit` AND NO CURSOR, and `has_more` is therefore always false in v0. Truncating would
    shrink the count, and the count is the number an operator acts on; the response is bounded by the
    number of cycles in the workspace, which in a healthy one is zero. `has_more` is present so that
    adding a bound later is additive rather than a new envelope.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CyclesPage | Problem
    """

    return (
        await asyncio_detailed(
            client=client,
        )
    ).parsed
