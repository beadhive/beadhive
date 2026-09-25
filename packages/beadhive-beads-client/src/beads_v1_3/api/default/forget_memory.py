from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.memory import Memory
from ...models.problem import Problem
from ...types import Response


def _get_kwargs(
    key: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "delete",
        "url": "/v0/beads/memories/{key}".format(
            key=quote(str(key), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Memory | Problem | None:
    if response.status_code == 200:
        response_200 = Memory.from_dict(response.json())

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
) -> Response[Memory | Problem]:
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
) -> Response[Memory | Problem]:
    """Forget one stored memory

     Removes the memory stored under one key and answers with what it held — the operation behind `bd
    forget`. IT IS DESTRUCTIVE and nothing it removes comes back, on a surface that has NO
    AUTHENTICATION: every process that can reach the port can erase any memory whose key it knows,
    exactly as `issues:sweep` and `issues:delete` state for beads. The recovery is the workspace's
    version control, not this API.

    THE FIRST `DELETE` METHOD ON THIS SURFACE, and the shape is why. The two destructive issue
    operations are collection-level custom methods because they act on a SET the request describes and
    carry flags that change what is erased — a query string is exactly where a dropped parameter widens
    a deletion. This one names ONE resource by path, carries no body and takes no flags, which is what
    the `DELETE` method already means. The alternative spelling `POST /v0/beads/memories/{key}:forget`
    would in addition recreate the claim route's wildcard contortion, since `{key}:forget` is not a
    router pattern, for no gain.

    REMOVING EXACTLY THE NAMED ROW is the ROLE's promise, pinned by its conformance contract rather than
    restated here: the memory plane shares one table with the workspace's settings and with the generic
    `bd kv` namespace, and a memory called `issue_prefix` is not the workspace's issue prefix.

    Forgetting a key nothing stored is a `404` and removes nothing. Forgetting the same key twice is
    therefore a `200` and then a `404`, which is what a retrying client actually sees; the second answer
    is not a failure to act, it is the same fact reported after the act.

    The same keys are unreachable here as on the `GET` beside it: a stored key carrying a control
    character is refused with a `400` rather than looked up, while the role stays verbatim, so such a
    memory can be forgotten from the CLI and not through this operation.

    Hooks do not fire, as for every write on this surface. There is no `dry_run`: one named row is not a
    set to cost first.

    Args:
        key (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Memory | Problem]
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
) -> Memory | Problem | None:
    """Forget one stored memory

     Removes the memory stored under one key and answers with what it held — the operation behind `bd
    forget`. IT IS DESTRUCTIVE and nothing it removes comes back, on a surface that has NO
    AUTHENTICATION: every process that can reach the port can erase any memory whose key it knows,
    exactly as `issues:sweep` and `issues:delete` state for beads. The recovery is the workspace's
    version control, not this API.

    THE FIRST `DELETE` METHOD ON THIS SURFACE, and the shape is why. The two destructive issue
    operations are collection-level custom methods because they act on a SET the request describes and
    carry flags that change what is erased — a query string is exactly where a dropped parameter widens
    a deletion. This one names ONE resource by path, carries no body and takes no flags, which is what
    the `DELETE` method already means. The alternative spelling `POST /v0/beads/memories/{key}:forget`
    would in addition recreate the claim route's wildcard contortion, since `{key}:forget` is not a
    router pattern, for no gain.

    REMOVING EXACTLY THE NAMED ROW is the ROLE's promise, pinned by its conformance contract rather than
    restated here: the memory plane shares one table with the workspace's settings and with the generic
    `bd kv` namespace, and a memory called `issue_prefix` is not the workspace's issue prefix.

    Forgetting a key nothing stored is a `404` and removes nothing. Forgetting the same key twice is
    therefore a `200` and then a `404`, which is what a retrying client actually sees; the second answer
    is not a failure to act, it is the same fact reported after the act.

    The same keys are unreachable here as on the `GET` beside it: a stored key carrying a control
    character is refused with a `400` rather than looked up, while the role stays verbatim, so such a
    memory can be forgotten from the CLI and not through this operation.

    Hooks do not fire, as for every write on this surface. There is no `dry_run`: one named row is not a
    set to cost first.

    Args:
        key (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Memory | Problem
    """

    return sync_detailed(
        key=key,
        client=client,
    ).parsed


async def asyncio_detailed(
    key: str,
    *,
    client: AuthenticatedClient,
) -> Response[Memory | Problem]:
    """Forget one stored memory

     Removes the memory stored under one key and answers with what it held — the operation behind `bd
    forget`. IT IS DESTRUCTIVE and nothing it removes comes back, on a surface that has NO
    AUTHENTICATION: every process that can reach the port can erase any memory whose key it knows,
    exactly as `issues:sweep` and `issues:delete` state for beads. The recovery is the workspace's
    version control, not this API.

    THE FIRST `DELETE` METHOD ON THIS SURFACE, and the shape is why. The two destructive issue
    operations are collection-level custom methods because they act on a SET the request describes and
    carry flags that change what is erased — a query string is exactly where a dropped parameter widens
    a deletion. This one names ONE resource by path, carries no body and takes no flags, which is what
    the `DELETE` method already means. The alternative spelling `POST /v0/beads/memories/{key}:forget`
    would in addition recreate the claim route's wildcard contortion, since `{key}:forget` is not a
    router pattern, for no gain.

    REMOVING EXACTLY THE NAMED ROW is the ROLE's promise, pinned by its conformance contract rather than
    restated here: the memory plane shares one table with the workspace's settings and with the generic
    `bd kv` namespace, and a memory called `issue_prefix` is not the workspace's issue prefix.

    Forgetting a key nothing stored is a `404` and removes nothing. Forgetting the same key twice is
    therefore a `200` and then a `404`, which is what a retrying client actually sees; the second answer
    is not a failure to act, it is the same fact reported after the act.

    The same keys are unreachable here as on the `GET` beside it: a stored key carrying a control
    character is refused with a `400` rather than looked up, while the role stays verbatim, so such a
    memory can be forgotten from the CLI and not through this operation.

    Hooks do not fire, as for every write on this surface. There is no `dry_run`: one named row is not a
    set to cost first.

    Args:
        key (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Memory | Problem]
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
) -> Memory | Problem | None:
    """Forget one stored memory

     Removes the memory stored under one key and answers with what it held — the operation behind `bd
    forget`. IT IS DESTRUCTIVE and nothing it removes comes back, on a surface that has NO
    AUTHENTICATION: every process that can reach the port can erase any memory whose key it knows,
    exactly as `issues:sweep` and `issues:delete` state for beads. The recovery is the workspace's
    version control, not this API.

    THE FIRST `DELETE` METHOD ON THIS SURFACE, and the shape is why. The two destructive issue
    operations are collection-level custom methods because they act on a SET the request describes and
    carry flags that change what is erased — a query string is exactly where a dropped parameter widens
    a deletion. This one names ONE resource by path, carries no body and takes no flags, which is what
    the `DELETE` method already means. The alternative spelling `POST /v0/beads/memories/{key}:forget`
    would in addition recreate the claim route's wildcard contortion, since `{key}:forget` is not a
    router pattern, for no gain.

    REMOVING EXACTLY THE NAMED ROW is the ROLE's promise, pinned by its conformance contract rather than
    restated here: the memory plane shares one table with the workspace's settings and with the generic
    `bd kv` namespace, and a memory called `issue_prefix` is not the workspace's issue prefix.

    Forgetting a key nothing stored is a `404` and removes nothing. Forgetting the same key twice is
    therefore a `200` and then a `404`, which is what a retrying client actually sees; the second answer
    is not a failure to act, it is the same fact reported after the act.

    The same keys are unreachable here as on the `GET` beside it: a stored key carrying a control
    character is refused with a `400` rather than looked up, while the role stays verbatim, so such a
    memory can be forgotten from the CLI and not through this operation.

    Hooks do not fire, as for every write on this surface. There is no `dry_run`: one named row is not a
    set to cost first.

    Args:
        key (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Memory | Problem
    """

    return (
        await asyncio_detailed(
            key=key,
            client=client,
        )
    ).parsed
