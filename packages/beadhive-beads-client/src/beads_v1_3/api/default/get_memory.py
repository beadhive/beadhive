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
        "method": "get",
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
    """Get one stored memory

     The memory stored under one key, used verbatim: no namespace completion, no case folding, no
    dash/underscore equivalence. It is the operation behind `bd recall`.

    THIS OPERATION HAS A REAL 404, and it is the one place this surface diverges from `GET
    /v0/beads/config/{key}`, which deliberately has none. The planes genuinely differ. On the settings
    plane a key nothing stored and a key stored empty are one answer that `bd config get` prints
    identically, so a 404 would publish a distinction the server would have to invent. On this plane `bd
    recall` ALREADY distinguishes a miss, by exit code, and the role answers a miss as a result rather
    than a value — so a 404 reports a distinction that exists rather than minting one.

    A MEMORY STORED AS THE EMPTY STRING IS A 404 TOO. No front door can create one — storing empty
    content is refused — but an out-of-band write to the config table can, and the role does not invent
    a distinction the storage seam beneath it cannot see. `GET /v0/beads/memories` DOES enumerate such a
    row, because its key exists, and that asymmetry is the one way a client can tell the two apart.

    MEMORY CONTENT IS SERVED IN FULL, and a configured bearer does not narrow it: `Memory` carries no
    `redacted` member and withholds nothing from any caller the token admits. See `POST
    /v0/beads/memories` for why a key-name heuristic would be worse than no promise at all.

    KEYS THIS OPERATION CANNOT REACH. `bd remember --key` accepts any string, so a stored key may carry
    a control character — and `key` is one path segment, percent-decoded once, so such a key would
    arrive here as a decoded control character in a path. This operation refuses it with a `400` rather
    than looking it up, exactly as `getSetting` does. The ROLE stays verbatim: breaking `bd recall` of
    an odd key someone already stored, to tidy a wire rule, would be the tail wagging the dog. Such a
    memory is reachable from the CLI and from `GET /v0/beads/memories`, and not by path.

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
    """Get one stored memory

     The memory stored under one key, used verbatim: no namespace completion, no case folding, no
    dash/underscore equivalence. It is the operation behind `bd recall`.

    THIS OPERATION HAS A REAL 404, and it is the one place this surface diverges from `GET
    /v0/beads/config/{key}`, which deliberately has none. The planes genuinely differ. On the settings
    plane a key nothing stored and a key stored empty are one answer that `bd config get` prints
    identically, so a 404 would publish a distinction the server would have to invent. On this plane `bd
    recall` ALREADY distinguishes a miss, by exit code, and the role answers a miss as a result rather
    than a value — so a 404 reports a distinction that exists rather than minting one.

    A MEMORY STORED AS THE EMPTY STRING IS A 404 TOO. No front door can create one — storing empty
    content is refused — but an out-of-band write to the config table can, and the role does not invent
    a distinction the storage seam beneath it cannot see. `GET /v0/beads/memories` DOES enumerate such a
    row, because its key exists, and that asymmetry is the one way a client can tell the two apart.

    MEMORY CONTENT IS SERVED IN FULL, and a configured bearer does not narrow it: `Memory` carries no
    `redacted` member and withholds nothing from any caller the token admits. See `POST
    /v0/beads/memories` for why a key-name heuristic would be worse than no promise at all.

    KEYS THIS OPERATION CANNOT REACH. `bd remember --key` accepts any string, so a stored key may carry
    a control character — and `key` is one path segment, percent-decoded once, so such a key would
    arrive here as a decoded control character in a path. This operation refuses it with a `400` rather
    than looking it up, exactly as `getSetting` does. The ROLE stays verbatim: breaking `bd recall` of
    an odd key someone already stored, to tidy a wire rule, would be the tail wagging the dog. Such a
    memory is reachable from the CLI and from `GET /v0/beads/memories`, and not by path.

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
    """Get one stored memory

     The memory stored under one key, used verbatim: no namespace completion, no case folding, no
    dash/underscore equivalence. It is the operation behind `bd recall`.

    THIS OPERATION HAS A REAL 404, and it is the one place this surface diverges from `GET
    /v0/beads/config/{key}`, which deliberately has none. The planes genuinely differ. On the settings
    plane a key nothing stored and a key stored empty are one answer that `bd config get` prints
    identically, so a 404 would publish a distinction the server would have to invent. On this plane `bd
    recall` ALREADY distinguishes a miss, by exit code, and the role answers a miss as a result rather
    than a value — so a 404 reports a distinction that exists rather than minting one.

    A MEMORY STORED AS THE EMPTY STRING IS A 404 TOO. No front door can create one — storing empty
    content is refused — but an out-of-band write to the config table can, and the role does not invent
    a distinction the storage seam beneath it cannot see. `GET /v0/beads/memories` DOES enumerate such a
    row, because its key exists, and that asymmetry is the one way a client can tell the two apart.

    MEMORY CONTENT IS SERVED IN FULL, and a configured bearer does not narrow it: `Memory` carries no
    `redacted` member and withholds nothing from any caller the token admits. See `POST
    /v0/beads/memories` for why a key-name heuristic would be worse than no promise at all.

    KEYS THIS OPERATION CANNOT REACH. `bd remember --key` accepts any string, so a stored key may carry
    a control character — and `key` is one path segment, percent-decoded once, so such a key would
    arrive here as a decoded control character in a path. This operation refuses it with a `400` rather
    than looking it up, exactly as `getSetting` does. The ROLE stays verbatim: breaking `bd recall` of
    an odd key someone already stored, to tidy a wire rule, would be the tail wagging the dog. Such a
    memory is reachable from the CLI and from `GET /v0/beads/memories`, and not by path.

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
    """Get one stored memory

     The memory stored under one key, used verbatim: no namespace completion, no case folding, no
    dash/underscore equivalence. It is the operation behind `bd recall`.

    THIS OPERATION HAS A REAL 404, and it is the one place this surface diverges from `GET
    /v0/beads/config/{key}`, which deliberately has none. The planes genuinely differ. On the settings
    plane a key nothing stored and a key stored empty are one answer that `bd config get` prints
    identically, so a 404 would publish a distinction the server would have to invent. On this plane `bd
    recall` ALREADY distinguishes a miss, by exit code, and the role answers a miss as a result rather
    than a value — so a 404 reports a distinction that exists rather than minting one.

    A MEMORY STORED AS THE EMPTY STRING IS A 404 TOO. No front door can create one — storing empty
    content is refused — but an out-of-band write to the config table can, and the role does not invent
    a distinction the storage seam beneath it cannot see. `GET /v0/beads/memories` DOES enumerate such a
    row, because its key exists, and that asymmetry is the one way a client can tell the two apart.

    MEMORY CONTENT IS SERVED IN FULL, and a configured bearer does not narrow it: `Memory` carries no
    `redacted` member and withholds nothing from any caller the token admits. See `POST
    /v0/beads/memories` for why a key-name heuristic would be worse than no promise at all.

    KEYS THIS OPERATION CANNOT REACH. `bd remember --key` accepts any string, so a stored key may carry
    a control character — and `key` is one path segment, percent-decoded once, so such a key would
    arrive here as a decoded control character in a path. This operation refuses it with a `400` rather
    than looking it up, exactly as `getSetting` does. The ROLE stays verbatim: breaking `bd recall` of
    an odd key someone already stored, to tidy a wire rule, would be the tail wagging the dog. Such a
    memory is reachable from the CLI and from `GET /v0/beads/memories`, and not by path.

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
