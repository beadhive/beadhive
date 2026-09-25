from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.problem import Problem
from ...models.remove_dependency_request import RemoveDependencyRequest
from ...models.remove_dependency_response import RemoveDependencyResponse
from ...types import Response


def _get_kwargs(
    *,
    body: RemoveDependencyRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v0/beads/dependencies:remove",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Problem | RemoveDependencyResponse | None:
    if response.status_code == 200:
        response_200 = RemoveDependencyResponse.from_dict(response.json())

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
) -> Response[Problem | RemoveDependencyResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: RemoveDependencyRequest,
) -> Response[Problem | RemoveDependencyResponse]:
    """Remove one dependency edge

     Removes exactly the edge the request names — source, target — and at most that one edge.

    IT IS IDEMPOTENT, and `removed: false` is a SUCCESS rather than a refusal. Removing an edge twice
    leaves the same graph as removing it once, so an agent replaying its own teardown does not have to
    classify an error to discover it already ran. Nothing is written for it and no event is recorded.

    THERE IS NO `404` ON THIS OPERATION, deliberately. An edge that is not there is `removed: false`,
    and an endpoint id that names nothing holds no edge either, so this operation probes no id's
    existence and has nothing it could report a miss on.

    It is a collection-level custom method rather than a `DELETE`: an edge is named by TWO endpoints, so
    there is no single-segment resource path for a `DELETE` to address, and a `DELETE` carrying a body
    is the shape proxies mangle. `DELETE /v0/beads/memories/{key}` is a `DELETE` for the opposite reason
    — one named resource, no body.

    A removal that found its edge records a `dependency_removed` entry on the source's event stream,
    attributed to `actor`. An edge FOLLOWS ITS SOURCE, so a wisp-sourced edge is removed from the
    unversioned plane and leaves no durable history entry.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (RemoveDependencyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | RemoveDependencyResponse]
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
    body: RemoveDependencyRequest,
) -> Problem | RemoveDependencyResponse | None:
    """Remove one dependency edge

     Removes exactly the edge the request names — source, target — and at most that one edge.

    IT IS IDEMPOTENT, and `removed: false` is a SUCCESS rather than a refusal. Removing an edge twice
    leaves the same graph as removing it once, so an agent replaying its own teardown does not have to
    classify an error to discover it already ran. Nothing is written for it and no event is recorded.

    THERE IS NO `404` ON THIS OPERATION, deliberately. An edge that is not there is `removed: false`,
    and an endpoint id that names nothing holds no edge either, so this operation probes no id's
    existence and has nothing it could report a miss on.

    It is a collection-level custom method rather than a `DELETE`: an edge is named by TWO endpoints, so
    there is no single-segment resource path for a `DELETE` to address, and a `DELETE` carrying a body
    is the shape proxies mangle. `DELETE /v0/beads/memories/{key}` is a `DELETE` for the opposite reason
    — one named resource, no body.

    A removal that found its edge records a `dependency_removed` entry on the source's event stream,
    attributed to `actor`. An edge FOLLOWS ITS SOURCE, so a wisp-sourced edge is removed from the
    unversioned plane and leaves no durable history entry.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (RemoveDependencyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | RemoveDependencyResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: RemoveDependencyRequest,
) -> Response[Problem | RemoveDependencyResponse]:
    """Remove one dependency edge

     Removes exactly the edge the request names — source, target — and at most that one edge.

    IT IS IDEMPOTENT, and `removed: false` is a SUCCESS rather than a refusal. Removing an edge twice
    leaves the same graph as removing it once, so an agent replaying its own teardown does not have to
    classify an error to discover it already ran. Nothing is written for it and no event is recorded.

    THERE IS NO `404` ON THIS OPERATION, deliberately. An edge that is not there is `removed: false`,
    and an endpoint id that names nothing holds no edge either, so this operation probes no id's
    existence and has nothing it could report a miss on.

    It is a collection-level custom method rather than a `DELETE`: an edge is named by TWO endpoints, so
    there is no single-segment resource path for a `DELETE` to address, and a `DELETE` carrying a body
    is the shape proxies mangle. `DELETE /v0/beads/memories/{key}` is a `DELETE` for the opposite reason
    — one named resource, no body.

    A removal that found its edge records a `dependency_removed` entry on the source's event stream,
    attributed to `actor`. An edge FOLLOWS ITS SOURCE, so a wisp-sourced edge is removed from the
    unversioned plane and leaves no durable history entry.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (RemoveDependencyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | RemoveDependencyResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    body: RemoveDependencyRequest,
) -> Problem | RemoveDependencyResponse | None:
    """Remove one dependency edge

     Removes exactly the edge the request names — source, target — and at most that one edge.

    IT IS IDEMPOTENT, and `removed: false` is a SUCCESS rather than a refusal. Removing an edge twice
    leaves the same graph as removing it once, so an agent replaying its own teardown does not have to
    classify an error to discover it already ran. Nothing is written for it and no event is recorded.

    THERE IS NO `404` ON THIS OPERATION, deliberately. An edge that is not there is `removed: false`,
    and an endpoint id that names nothing holds no edge either, so this operation probes no id's
    existence and has nothing it could report a miss on.

    It is a collection-level custom method rather than a `DELETE`: an edge is named by TWO endpoints, so
    there is no single-segment resource path for a `DELETE` to address, and a `DELETE` carrying a body
    is the shape proxies mangle. `DELETE /v0/beads/memories/{key}` is a `DELETE` for the opposite reason
    — one named resource, no body.

    A removal that found its edge records a `dependency_removed` entry on the source's event stream,
    attributed to `actor`. An edge FOLLOWS ITS SOURCE, so a wisp-sourced edge is removed from the
    unversioned plane and leaves no durable history entry.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (RemoveDependencyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | RemoveDependencyResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
