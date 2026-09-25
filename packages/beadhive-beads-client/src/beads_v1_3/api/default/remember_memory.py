from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.problem import Problem
from ...models.remember_request import RememberRequest
from ...models.remembered_memory import RememberedMemory
from ...types import Response


def _get_kwargs(
    *,
    body: RememberRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v0/beads/memories",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Problem | RememberedMemory | None:
    if response.status_code == 200:
        response_200 = RememberedMemory.from_dict(response.json())

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
) -> Response[Problem | RememberedMemory]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: RememberRequest,
) -> Response[Problem | RememberedMemory]:
    """Store one memory

     Stores one memory in the workspace's persistent memory plane — the operation behind `bd remember`.
    It is an UPSERT: a key that already holds a memory is overwritten, and `replaced` in the response
    reports which of the two happened.

    `key` is OPTIONAL, and omitting it is the normal case. The server then derives the key from
    `content` using the one derivation `bd remember` has used since it shipped, and the response's `key`
    is where the caller learns what to recall. A `key` that IS supplied is used verbatim: no trimming,
    no slugging, no charset restriction, because a stored key has to stay recallable under the exact
    bytes the caller used.

    MEMORY CONTENT IS SERVED IN FULL, here and on every other operation of this plane, and a bearer does
    not change that — it gates WHO may call, never what a caller who is let in may read. There is no
    `redacted` member and no value withholding: the settings surface's redaction is a heuristic over the
    KEY NAME, and memory keys are derived from the content, so the same rule would withhold a memory
    ABOUT tokens while serving one that CONTAINS a token under an innocuous slug. A promise that cannot
    be kept is not made. Do not store credentials in this plane.

    There is no `201`/`200` split. `replaced` already says whether a row existed, saying it twice in a
    second vocabulary would add nothing, and an upsert whose key the server may derive has no stable
    `Location` to point at.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for the other
    write operations on this surface. The only durable effect is the single storage commit the role
    makes in its own transaction — which also observes whether a previous value existed, so `replaced`
    is a statement about the row this request wrote rather than about a row some earlier read happened
    to see.

    Args:
        body (RememberRequest): What to remember, and optionally under what key.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | RememberedMemory]
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
    body: RememberRequest,
) -> Problem | RememberedMemory | None:
    """Store one memory

     Stores one memory in the workspace's persistent memory plane — the operation behind `bd remember`.
    It is an UPSERT: a key that already holds a memory is overwritten, and `replaced` in the response
    reports which of the two happened.

    `key` is OPTIONAL, and omitting it is the normal case. The server then derives the key from
    `content` using the one derivation `bd remember` has used since it shipped, and the response's `key`
    is where the caller learns what to recall. A `key` that IS supplied is used verbatim: no trimming,
    no slugging, no charset restriction, because a stored key has to stay recallable under the exact
    bytes the caller used.

    MEMORY CONTENT IS SERVED IN FULL, here and on every other operation of this plane, and a bearer does
    not change that — it gates WHO may call, never what a caller who is let in may read. There is no
    `redacted` member and no value withholding: the settings surface's redaction is a heuristic over the
    KEY NAME, and memory keys are derived from the content, so the same rule would withhold a memory
    ABOUT tokens while serving one that CONTAINS a token under an innocuous slug. A promise that cannot
    be kept is not made. Do not store credentials in this plane.

    There is no `201`/`200` split. `replaced` already says whether a row existed, saying it twice in a
    second vocabulary would add nothing, and an upsert whose key the server may derive has no stable
    `Location` to point at.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for the other
    write operations on this surface. The only durable effect is the single storage commit the role
    makes in its own transaction — which also observes whether a previous value existed, so `replaced`
    is a statement about the row this request wrote rather than about a row some earlier read happened
    to see.

    Args:
        body (RememberRequest): What to remember, and optionally under what key.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | RememberedMemory
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: RememberRequest,
) -> Response[Problem | RememberedMemory]:
    """Store one memory

     Stores one memory in the workspace's persistent memory plane — the operation behind `bd remember`.
    It is an UPSERT: a key that already holds a memory is overwritten, and `replaced` in the response
    reports which of the two happened.

    `key` is OPTIONAL, and omitting it is the normal case. The server then derives the key from
    `content` using the one derivation `bd remember` has used since it shipped, and the response's `key`
    is where the caller learns what to recall. A `key` that IS supplied is used verbatim: no trimming,
    no slugging, no charset restriction, because a stored key has to stay recallable under the exact
    bytes the caller used.

    MEMORY CONTENT IS SERVED IN FULL, here and on every other operation of this plane, and a bearer does
    not change that — it gates WHO may call, never what a caller who is let in may read. There is no
    `redacted` member and no value withholding: the settings surface's redaction is a heuristic over the
    KEY NAME, and memory keys are derived from the content, so the same rule would withhold a memory
    ABOUT tokens while serving one that CONTAINS a token under an innocuous slug. A promise that cannot
    be kept is not made. Do not store credentials in this plane.

    There is no `201`/`200` split. `replaced` already says whether a row existed, saying it twice in a
    second vocabulary would add nothing, and an upsert whose key the server may derive has no stable
    `Location` to point at.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for the other
    write operations on this surface. The only durable effect is the single storage commit the role
    makes in its own transaction — which also observes whether a previous value existed, so `replaced`
    is a statement about the row this request wrote rather than about a row some earlier read happened
    to see.

    Args:
        body (RememberRequest): What to remember, and optionally under what key.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | RememberedMemory]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    body: RememberRequest,
) -> Problem | RememberedMemory | None:
    """Store one memory

     Stores one memory in the workspace's persistent memory plane — the operation behind `bd remember`.
    It is an UPSERT: a key that already holds a memory is overwritten, and `replaced` in the response
    reports which of the two happened.

    `key` is OPTIONAL, and omitting it is the normal case. The server then derives the key from
    `content` using the one derivation `bd remember` has used since it shipped, and the response's `key`
    is where the caller learns what to recall. A `key` that IS supplied is used verbatim: no trimming,
    no slugging, no charset restriction, because a stored key has to stay recallable under the exact
    bytes the caller used.

    MEMORY CONTENT IS SERVED IN FULL, here and on every other operation of this plane, and a bearer does
    not change that — it gates WHO may call, never what a caller who is let in may read. There is no
    `redacted` member and no value withholding: the settings surface's redaction is a heuristic over the
    KEY NAME, and memory keys are derived from the content, so the same rule would withhold a memory
    ABOUT tokens while serving one that CONTAINS a token under an innocuous slug. A promise that cannot
    be kept is not made. Do not store credentials in this plane.

    There is no `201`/`200` split. `replaced` already says whether a row existed, saying it twice in a
    second vocabulary would add nothing, and an upsert whose key the server may derive has no stable
    `Location` to point at.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for the other
    write operations on this surface. The only durable effect is the single storage commit the role
    makes in its own transaction — which also observes whether a previous value existed, so `replaced`
    is a statement about the row this request wrote rather than about a row some earlier read happened
    to see.

    Args:
        body (RememberRequest): What to remember, and optionally under what key.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | RememberedMemory
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
