from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.add_dependencies_request import AddDependenciesRequest
from ...models.add_dependencies_response import AddDependenciesResponse
from ...models.problem import Problem
from ...types import Response


def _get_kwargs(
    *,
    body: AddDependenciesRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v0/beads/dependencies:add",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> AddDependenciesResponse | Problem | None:
    if response.status_code == 200:
        response_200 = AddDependenciesResponse.from_dict(response.json())

        return response_200

    if response.status_code == 400:
        response_400 = Problem.from_dict(response.json())

        return response_400

    if response.status_code == 401:
        response_401 = Problem.from_dict(response.json())

        return response_401

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
) -> Response[AddDependenciesResponse | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: AddDependenciesRequest,
) -> Response[AddDependenciesResponse | Problem]:
    """Assert dependency edges as one act

     Asserts every edge in the request, or none of them.

    IT IS ALL-OR-NOTHING, and unlike a batch close that is not a policy choice but the shape of the
    question. Edges asserted together describe a GRAPH, and half a graph is a graph nobody asked for —
    the cycle a caller was refused for is exactly the state a partial commit would leave behind. A
    client that gets a 4xx knows nothing was written, and can fix its payload and resend it unchanged.
    There is no per-edge outcome because there is no outcome but the request's.

    AN EDGE IS IDEMPOTENT AT ITS OWN TYPE. A pair that already carries an edge of the requested type
    refuses nothing and is still echoed in `added`; a pair that carries a DIFFERENT type is a `409`
    `dependency_exists`, whichever of the two types was stored first. Repetition WITHIN one request
    answers the same way: the second occurrence of a pair finds the first already written, and two
    different types for one pair in one request is the same `409`.

    A TARGET NEED NOT BE AN ISSUE THIS DATABASE HOLDS. An `external:` reference and an id belonging to
    another repository are legitimate targets — the vocabulary is open — so only an absence this
    database can SEE is refused. A SOURCE has no such latitude: an edge follows its source, so a source
    this database holds no row for has no plane to land in.

    Edges are applied parent-child first regardless of request order, so the complete planned hierarchy
    is visible before any blocking edge is validated against it, and a whole-graph gate runs once at the
    end. Both hold ACROSS the durable and ephemeral planes: a request may mix them, and it is still one
    transaction.

    A request that wrote no genuinely new durable edge records no history entry, and a request made
    entirely of wisp-sourced edges records none either — an edge follows its source, and the wisp plane
    is not versioned. Each genuinely new edge records a `dependency_added` entry on its source's event
    stream, attributed to `actor`.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (AddDependenciesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[AddDependenciesResponse | Problem]
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
    body: AddDependenciesRequest,
) -> AddDependenciesResponse | Problem | None:
    """Assert dependency edges as one act

     Asserts every edge in the request, or none of them.

    IT IS ALL-OR-NOTHING, and unlike a batch close that is not a policy choice but the shape of the
    question. Edges asserted together describe a GRAPH, and half a graph is a graph nobody asked for —
    the cycle a caller was refused for is exactly the state a partial commit would leave behind. A
    client that gets a 4xx knows nothing was written, and can fix its payload and resend it unchanged.
    There is no per-edge outcome because there is no outcome but the request's.

    AN EDGE IS IDEMPOTENT AT ITS OWN TYPE. A pair that already carries an edge of the requested type
    refuses nothing and is still echoed in `added`; a pair that carries a DIFFERENT type is a `409`
    `dependency_exists`, whichever of the two types was stored first. Repetition WITHIN one request
    answers the same way: the second occurrence of a pair finds the first already written, and two
    different types for one pair in one request is the same `409`.

    A TARGET NEED NOT BE AN ISSUE THIS DATABASE HOLDS. An `external:` reference and an id belonging to
    another repository are legitimate targets — the vocabulary is open — so only an absence this
    database can SEE is refused. A SOURCE has no such latitude: an edge follows its source, so a source
    this database holds no row for has no plane to land in.

    Edges are applied parent-child first regardless of request order, so the complete planned hierarchy
    is visible before any blocking edge is validated against it, and a whole-graph gate runs once at the
    end. Both hold ACROSS the durable and ephemeral planes: a request may mix them, and it is still one
    transaction.

    A request that wrote no genuinely new durable edge records no history entry, and a request made
    entirely of wisp-sourced edges records none either — an edge follows its source, and the wisp plane
    is not versioned. Each genuinely new edge records a `dependency_added` entry on its source's event
    stream, attributed to `actor`.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (AddDependenciesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        AddDependenciesResponse | Problem
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: AddDependenciesRequest,
) -> Response[AddDependenciesResponse | Problem]:
    """Assert dependency edges as one act

     Asserts every edge in the request, or none of them.

    IT IS ALL-OR-NOTHING, and unlike a batch close that is not a policy choice but the shape of the
    question. Edges asserted together describe a GRAPH, and half a graph is a graph nobody asked for —
    the cycle a caller was refused for is exactly the state a partial commit would leave behind. A
    client that gets a 4xx knows nothing was written, and can fix its payload and resend it unchanged.
    There is no per-edge outcome because there is no outcome but the request's.

    AN EDGE IS IDEMPOTENT AT ITS OWN TYPE. A pair that already carries an edge of the requested type
    refuses nothing and is still echoed in `added`; a pair that carries a DIFFERENT type is a `409`
    `dependency_exists`, whichever of the two types was stored first. Repetition WITHIN one request
    answers the same way: the second occurrence of a pair finds the first already written, and two
    different types for one pair in one request is the same `409`.

    A TARGET NEED NOT BE AN ISSUE THIS DATABASE HOLDS. An `external:` reference and an id belonging to
    another repository are legitimate targets — the vocabulary is open — so only an absence this
    database can SEE is refused. A SOURCE has no such latitude: an edge follows its source, so a source
    this database holds no row for has no plane to land in.

    Edges are applied parent-child first regardless of request order, so the complete planned hierarchy
    is visible before any blocking edge is validated against it, and a whole-graph gate runs once at the
    end. Both hold ACROSS the durable and ephemeral planes: a request may mix them, and it is still one
    transaction.

    A request that wrote no genuinely new durable edge records no history entry, and a request made
    entirely of wisp-sourced edges records none either — an edge follows its source, and the wisp plane
    is not versioned. Each genuinely new edge records a `dependency_added` entry on its source's event
    stream, attributed to `actor`.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (AddDependenciesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[AddDependenciesResponse | Problem]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    body: AddDependenciesRequest,
) -> AddDependenciesResponse | Problem | None:
    """Assert dependency edges as one act

     Asserts every edge in the request, or none of them.

    IT IS ALL-OR-NOTHING, and unlike a batch close that is not a policy choice but the shape of the
    question. Edges asserted together describe a GRAPH, and half a graph is a graph nobody asked for —
    the cycle a caller was refused for is exactly the state a partial commit would leave behind. A
    client that gets a 4xx knows nothing was written, and can fix its payload and resend it unchanged.
    There is no per-edge outcome because there is no outcome but the request's.

    AN EDGE IS IDEMPOTENT AT ITS OWN TYPE. A pair that already carries an edge of the requested type
    refuses nothing and is still echoed in `added`; a pair that carries a DIFFERENT type is a `409`
    `dependency_exists`, whichever of the two types was stored first. Repetition WITHIN one request
    answers the same way: the second occurrence of a pair finds the first already written, and two
    different types for one pair in one request is the same `409`.

    A TARGET NEED NOT BE AN ISSUE THIS DATABASE HOLDS. An `external:` reference and an id belonging to
    another repository are legitimate targets — the vocabulary is open — so only an absence this
    database can SEE is refused. A SOURCE has no such latitude: an edge follows its source, so a source
    this database holds no row for has no plane to land in.

    Edges are applied parent-child first regardless of request order, so the complete planned hierarchy
    is visible before any blocking edge is validated against it, and a whole-graph gate runs once at the
    end. Both hold ACROSS the durable and ephemeral planes: a request may mix them, and it is still one
    transaction.

    A request that wrote no genuinely new durable edge records no history entry, and a request made
    entirely of wisp-sourced edges records none either — an edge follows its source, and the wisp plane
    is not versioned. Each genuinely new edge records a `dependency_added` entry on its source's event
    stream, attributed to `actor`.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (AddDependenciesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        AddDependenciesResponse | Problem
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
