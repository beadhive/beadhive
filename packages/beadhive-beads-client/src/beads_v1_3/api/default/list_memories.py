from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.memories_page import MemoriesPage
from ...models.problem import Problem
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    search: str | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["search"] = search

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v0/beads/memories",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> MemoriesPage | Problem | None:
    if response.status_code == 200:
        response_200 = MemoriesPage.from_dict(response.json())

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
) -> Response[MemoriesPage | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    search: str | Unset = UNSET,
) -> Response[MemoriesPage | Problem]:
    """List the workspace's stored memories

     Every memory this workspace holds, optionally narrowed by `search`. It is the plane `bd memories`
    reads.

    ONLY THE MEMORY PLANE. Memories ride in the same database table as the workspace's settings and as
    the generic `bd kv` namespace, and neither of those appears here, whatever it contains — including a
    memory whose key SHADOWS a settings name: a memory called `issue_prefix` is a memory, and the
    workspace's real issue prefix is not one. That separation is the role's, pinned by its conformance
    contract, not a filter applied to this answer.

    MEMORY CONTENT IS SERVED IN FULL, and authentication does not narrow that. Every entry carries its
    value verbatim; there is no `redacted` member and nothing is withheld. A bearer decides WHO may
    call, not what a caller who is let in may read, so a token-protected server serves every memory to
    every holder of the token. See `POST /v0/beads/memories` for why a key-name heuristic would be worse
    here than no promise at all. This is the operation that makes stored memories DISCOVERABLE rather
    than merely readable by a caller who already knows a key — an operator binding beyond loopback is
    accepting exactly that for everyone the token admits.

    A memory stored as the EMPTY STRING is enumerated here, because its key exists, while `GET
    /v0/beads/memories/{key}` answers `404` for it. That asymmetry is the one way a client can tell a
    row stored empty from a row that is not there, and it is the storage seam's conflation showing
    through rather than a rule this surface invented.

    The envelope is the paginated one and `has_more` is ALWAYS false: memories are a keyed namespace a
    workspace holds tens of, not a collection to scan, so the whole plane comes back in one page. There
    is no `limit` and no cursor. The envelope is used anyway because entries are ordered by key, which
    makes a keyset cursor expressible later without a breaking change.

    Args:
        search (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[MemoriesPage | Problem]
    """

    kwargs = _get_kwargs(
        search=search,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    search: str | Unset = UNSET,
) -> MemoriesPage | Problem | None:
    """List the workspace's stored memories

     Every memory this workspace holds, optionally narrowed by `search`. It is the plane `bd memories`
    reads.

    ONLY THE MEMORY PLANE. Memories ride in the same database table as the workspace's settings and as
    the generic `bd kv` namespace, and neither of those appears here, whatever it contains — including a
    memory whose key SHADOWS a settings name: a memory called `issue_prefix` is a memory, and the
    workspace's real issue prefix is not one. That separation is the role's, pinned by its conformance
    contract, not a filter applied to this answer.

    MEMORY CONTENT IS SERVED IN FULL, and authentication does not narrow that. Every entry carries its
    value verbatim; there is no `redacted` member and nothing is withheld. A bearer decides WHO may
    call, not what a caller who is let in may read, so a token-protected server serves every memory to
    every holder of the token. See `POST /v0/beads/memories` for why a key-name heuristic would be worse
    here than no promise at all. This is the operation that makes stored memories DISCOVERABLE rather
    than merely readable by a caller who already knows a key — an operator binding beyond loopback is
    accepting exactly that for everyone the token admits.

    A memory stored as the EMPTY STRING is enumerated here, because its key exists, while `GET
    /v0/beads/memories/{key}` answers `404` for it. That asymmetry is the one way a client can tell a
    row stored empty from a row that is not there, and it is the storage seam's conflation showing
    through rather than a rule this surface invented.

    The envelope is the paginated one and `has_more` is ALWAYS false: memories are a keyed namespace a
    workspace holds tens of, not a collection to scan, so the whole plane comes back in one page. There
    is no `limit` and no cursor. The envelope is used anyway because entries are ordered by key, which
    makes a keyset cursor expressible later without a breaking change.

    Args:
        search (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        MemoriesPage | Problem
    """

    return sync_detailed(
        client=client,
        search=search,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    search: str | Unset = UNSET,
) -> Response[MemoriesPage | Problem]:
    """List the workspace's stored memories

     Every memory this workspace holds, optionally narrowed by `search`. It is the plane `bd memories`
    reads.

    ONLY THE MEMORY PLANE. Memories ride in the same database table as the workspace's settings and as
    the generic `bd kv` namespace, and neither of those appears here, whatever it contains — including a
    memory whose key SHADOWS a settings name: a memory called `issue_prefix` is a memory, and the
    workspace's real issue prefix is not one. That separation is the role's, pinned by its conformance
    contract, not a filter applied to this answer.

    MEMORY CONTENT IS SERVED IN FULL, and authentication does not narrow that. Every entry carries its
    value verbatim; there is no `redacted` member and nothing is withheld. A bearer decides WHO may
    call, not what a caller who is let in may read, so a token-protected server serves every memory to
    every holder of the token. See `POST /v0/beads/memories` for why a key-name heuristic would be worse
    here than no promise at all. This is the operation that makes stored memories DISCOVERABLE rather
    than merely readable by a caller who already knows a key — an operator binding beyond loopback is
    accepting exactly that for everyone the token admits.

    A memory stored as the EMPTY STRING is enumerated here, because its key exists, while `GET
    /v0/beads/memories/{key}` answers `404` for it. That asymmetry is the one way a client can tell a
    row stored empty from a row that is not there, and it is the storage seam's conflation showing
    through rather than a rule this surface invented.

    The envelope is the paginated one and `has_more` is ALWAYS false: memories are a keyed namespace a
    workspace holds tens of, not a collection to scan, so the whole plane comes back in one page. There
    is no `limit` and no cursor. The envelope is used anyway because entries are ordered by key, which
    makes a keyset cursor expressible later without a breaking change.

    Args:
        search (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[MemoriesPage | Problem]
    """

    kwargs = _get_kwargs(
        search=search,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    search: str | Unset = UNSET,
) -> MemoriesPage | Problem | None:
    """List the workspace's stored memories

     Every memory this workspace holds, optionally narrowed by `search`. It is the plane `bd memories`
    reads.

    ONLY THE MEMORY PLANE. Memories ride in the same database table as the workspace's settings and as
    the generic `bd kv` namespace, and neither of those appears here, whatever it contains — including a
    memory whose key SHADOWS a settings name: a memory called `issue_prefix` is a memory, and the
    workspace's real issue prefix is not one. That separation is the role's, pinned by its conformance
    contract, not a filter applied to this answer.

    MEMORY CONTENT IS SERVED IN FULL, and authentication does not narrow that. Every entry carries its
    value verbatim; there is no `redacted` member and nothing is withheld. A bearer decides WHO may
    call, not what a caller who is let in may read, so a token-protected server serves every memory to
    every holder of the token. See `POST /v0/beads/memories` for why a key-name heuristic would be worse
    here than no promise at all. This is the operation that makes stored memories DISCOVERABLE rather
    than merely readable by a caller who already knows a key — an operator binding beyond loopback is
    accepting exactly that for everyone the token admits.

    A memory stored as the EMPTY STRING is enumerated here, because its key exists, while `GET
    /v0/beads/memories/{key}` answers `404` for it. That asymmetry is the one way a client can tell a
    row stored empty from a row that is not there, and it is the storage seam's conflation showing
    through rather than a rule this surface invented.

    The envelope is the paginated one and `has_more` is ALWAYS false: memories are a keyed namespace a
    workspace holds tens of, not a collection to scan, so the whole plane comes back in one page. There
    is no `limit` and no cursor. The envelope is used anyway because entries are ordered by key, which
    makes a keyset cursor expressible later without a breaking change.

    Args:
        search (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        MemoriesPage | Problem
    """

    return (
        await asyncio_detailed(
            client=client,
            search=search,
        )
    ).parsed
