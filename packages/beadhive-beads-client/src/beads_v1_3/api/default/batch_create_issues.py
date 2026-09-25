from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.batch_create_request import BatchCreateRequest
from ...models.batch_create_response import BatchCreateResponse
from ...models.problem import Problem
from ...types import Response


def _get_kwargs(
    *,
    body: BatchCreateRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v0/beads/issues:batchCreate",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> BatchCreateResponse | Problem | None:
    if response.status_code == 200:
        response_200 = BatchCreateResponse.from_dict(response.json())

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
) -> Response[BatchCreateResponse | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: BatchCreateRequest,
) -> Response[BatchCreateResponse | Problem]:
    """Create many issues as one act

     Creates every item in the request, or none of them. There is no partial outcome and no per-item
    status: the whole request is one transaction, so a client that gets a 4xx knows nothing was written
    and can fix its payload and resend it unchanged.

    That is the OPPOSITE of what a batch close would do, and it is the right default here for one
    reason: half a created plan cannot be re-sent without duplicating the half that landed, and nothing
    in the response would say which half that was.

    THE SERVER ASSIGNS EVERY ID. There is no `id` member on an item, so this operation can never adopt
    or overwrite a stored row — `bd import` is the upsert surface and it is not published here. The
    generated ids come back in `items`, in request order, which is the only place a client can learn
    them.

    A dependency target may name an issue this workspace holds, an `external:` reference, or an id
    belonging to another repository. A target that is none of those is a `400` and nothing is created —
    an edge silently dropped from a created issue is a relationship the client has no way to discover is
    missing.

    AN ITEM OF THIS REQUEST CANNOT BE A DEPENDENCY TARGET, and this operation is the narrow fast path
    rather than the one to reach for when it needs to be. The server assigns every id, an item has no
    name a later item could spell, and only an id a caller already held could ever have addressed a row
    — so a plan whose edges point at its own new issues is `POST /v0/beads/issues:batchApply`, where a
    create item may NAME itself and later items address it by that name.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (BatchCreateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BatchCreateResponse | Problem]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    body: BatchCreateRequest,
) -> BatchCreateResponse | Problem | None:
    """Create many issues as one act

     Creates every item in the request, or none of them. There is no partial outcome and no per-item
    status: the whole request is one transaction, so a client that gets a 4xx knows nothing was written
    and can fix its payload and resend it unchanged.

    That is the OPPOSITE of what a batch close would do, and it is the right default here for one
    reason: half a created plan cannot be re-sent without duplicating the half that landed, and nothing
    in the response would say which half that was.

    THE SERVER ASSIGNS EVERY ID. There is no `id` member on an item, so this operation can never adopt
    or overwrite a stored row — `bd import` is the upsert surface and it is not published here. The
    generated ids come back in `items`, in request order, which is the only place a client can learn
    them.

    A dependency target may name an issue this workspace holds, an `external:` reference, or an id
    belonging to another repository. A target that is none of those is a `400` and nothing is created —
    an edge silently dropped from a created issue is a relationship the client has no way to discover is
    missing.

    AN ITEM OF THIS REQUEST CANNOT BE A DEPENDENCY TARGET, and this operation is the narrow fast path
    rather than the one to reach for when it needs to be. The server assigns every id, an item has no
    name a later item could spell, and only an id a caller already held could ever have addressed a row
    — so a plan whose edges point at its own new issues is `POST /v0/beads/issues:batchApply`, where a
    create item may NAME itself and later items address it by that name.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (BatchCreateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BatchCreateResponse | Problem
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: BatchCreateRequest,
) -> Response[BatchCreateResponse | Problem]:
    """Create many issues as one act

     Creates every item in the request, or none of them. There is no partial outcome and no per-item
    status: the whole request is one transaction, so a client that gets a 4xx knows nothing was written
    and can fix its payload and resend it unchanged.

    That is the OPPOSITE of what a batch close would do, and it is the right default here for one
    reason: half a created plan cannot be re-sent without duplicating the half that landed, and nothing
    in the response would say which half that was.

    THE SERVER ASSIGNS EVERY ID. There is no `id` member on an item, so this operation can never adopt
    or overwrite a stored row — `bd import` is the upsert surface and it is not published here. The
    generated ids come back in `items`, in request order, which is the only place a client can learn
    them.

    A dependency target may name an issue this workspace holds, an `external:` reference, or an id
    belonging to another repository. A target that is none of those is a `400` and nothing is created —
    an edge silently dropped from a created issue is a relationship the client has no way to discover is
    missing.

    AN ITEM OF THIS REQUEST CANNOT BE A DEPENDENCY TARGET, and this operation is the narrow fast path
    rather than the one to reach for when it needs to be. The server assigns every id, an item has no
    name a later item could spell, and only an id a caller already held could ever have addressed a row
    — so a plan whose edges point at its own new issues is `POST /v0/beads/issues:batchApply`, where a
    create item may NAME itself and later items address it by that name.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (BatchCreateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BatchCreateResponse | Problem]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    body: BatchCreateRequest,
) -> BatchCreateResponse | Problem | None:
    """Create many issues as one act

     Creates every item in the request, or none of them. There is no partial outcome and no per-item
    status: the whole request is one transaction, so a client that gets a 4xx knows nothing was written
    and can fix its payload and resend it unchanged.

    That is the OPPOSITE of what a batch close would do, and it is the right default here for one
    reason: half a created plan cannot be re-sent without duplicating the half that landed, and nothing
    in the response would say which half that was.

    THE SERVER ASSIGNS EVERY ID. There is no `id` member on an item, so this operation can never adopt
    or overwrite a stored row — `bd import` is the upsert surface and it is not published here. The
    generated ids come back in `items`, in request order, which is the only place a client can learn
    them.

    A dependency target may name an issue this workspace holds, an `external:` reference, or an id
    belonging to another repository. A target that is none of those is a `400` and nothing is created —
    an edge silently dropped from a created issue is a relationship the client has no way to discover is
    missing.

    AN ITEM OF THIS REQUEST CANNOT BE A DEPENDENCY TARGET, and this operation is the narrow fast path
    rather than the one to reach for when it needs to be. The server assigns every id, an item has no
    name a later item could spell, and only an id a caller already held could ever have addressed a row
    — so a plan whose edges point at its own new issues is `POST /v0/beads/issues:batchApply`, where a
    create item may NAME itself and later items address it by that name.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (BatchCreateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BatchCreateResponse | Problem
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
