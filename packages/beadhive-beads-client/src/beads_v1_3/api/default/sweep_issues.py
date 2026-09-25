from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.problem import Problem
from ...models.sweep_request import SweepRequest
from ...models.sweep_result import SweepResult
from ...types import Response


def _get_kwargs(
    *,
    body: SweepRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v0/beads/issues:sweep",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Problem | SweepResult | None:
    if response.status_code == 200:
        response_200 = SweepResult.from_dict(response.json())

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
) -> Response[Problem | SweepResult]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: SweepRequest,
) -> Response[Problem | SweepResult]:
    """Delete closed beads in bulk

     Bulk clearance of CLOSED beads from ONE tier: the operation behind `bd purge` (`tier: ephemeral`)
    and `bd prune` (`tier: durable`). It is one of the two DESTRUCTIVE operations on this surface — the
    other is `issues:delete` — and nothing it deletes comes back.

    A collection-level custom method rather than `DELETE /v0/beads/issues?...`: this describes a SET and
    acts on it, and a `DELETE` with a filtering query string is the shape where a dropped parameter
    widens what is erased. Sending a body makes every narrowing term a member the server refuses by name
    if it does not know it.

    ## What it selects, in order

    The closed rows of `tier`, then `pattern`, then the two protections — pinned beads are never swept
    and `protect_referenced` holds back beads another live bead cites. The ORDER is part of the answer:
    `skipped` counts what the request actually reached, so a pinned bead the pattern excluded is not
    counted as protected.

    ## The safety gate is the ROLE'S, not this handler's

    A `durable` sweep with neither `closed_before` nor `pattern` is refused with `400` /
    `invalid_argument`. That refusal comes from the same library surface `bd prune` calls, not from a
    check written here, which is what makes this endpoint incapable of erasing every closed bead in a
    workspace by omission. A caller that really means everything closed sends `pattern: "*"`.

    ## One transaction

    The selection and the deletion share one transaction, so the set the response describes IS the set
    that was deleted. The cost is that a sweep is all-or-nothing: one large enough to exceed the
    backend's write timeout fails whole and deletes nothing. Narrow the request rather than expecting
    progress.

    `dry_run: true` answers the same question and changes nothing — including history. Ask it first.

    Args:
        body (SweepRequest): Which closed beads to clear. The predicate is FIXED at "closed beads
            of one tier" and the two narrowing members only narrow it: there is no status, no
            assignee, no label and no free-text query here, because every one of those would be
            another way to spell a destructive selection that a caller could get subtly wrong.

            `additionalProperties: false`, so an unknown member is a `400` naming the member — the
            same posture the query-parameter rule takes, and for the same reason: on this operation a
            silently ignored narrowing term widens what is erased.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | SweepResult]
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
    body: SweepRequest,
) -> Problem | SweepResult | None:
    """Delete closed beads in bulk

     Bulk clearance of CLOSED beads from ONE tier: the operation behind `bd purge` (`tier: ephemeral`)
    and `bd prune` (`tier: durable`). It is one of the two DESTRUCTIVE operations on this surface — the
    other is `issues:delete` — and nothing it deletes comes back.

    A collection-level custom method rather than `DELETE /v0/beads/issues?...`: this describes a SET and
    acts on it, and a `DELETE` with a filtering query string is the shape where a dropped parameter
    widens what is erased. Sending a body makes every narrowing term a member the server refuses by name
    if it does not know it.

    ## What it selects, in order

    The closed rows of `tier`, then `pattern`, then the two protections — pinned beads are never swept
    and `protect_referenced` holds back beads another live bead cites. The ORDER is part of the answer:
    `skipped` counts what the request actually reached, so a pinned bead the pattern excluded is not
    counted as protected.

    ## The safety gate is the ROLE'S, not this handler's

    A `durable` sweep with neither `closed_before` nor `pattern` is refused with `400` /
    `invalid_argument`. That refusal comes from the same library surface `bd prune` calls, not from a
    check written here, which is what makes this endpoint incapable of erasing every closed bead in a
    workspace by omission. A caller that really means everything closed sends `pattern: "*"`.

    ## One transaction

    The selection and the deletion share one transaction, so the set the response describes IS the set
    that was deleted. The cost is that a sweep is all-or-nothing: one large enough to exceed the
    backend's write timeout fails whole and deletes nothing. Narrow the request rather than expecting
    progress.

    `dry_run: true` answers the same question and changes nothing — including history. Ask it first.

    Args:
        body (SweepRequest): Which closed beads to clear. The predicate is FIXED at "closed beads
            of one tier" and the two narrowing members only narrow it: there is no status, no
            assignee, no label and no free-text query here, because every one of those would be
            another way to spell a destructive selection that a caller could get subtly wrong.

            `additionalProperties: false`, so an unknown member is a `400` naming the member — the
            same posture the query-parameter rule takes, and for the same reason: on this operation a
            silently ignored narrowing term widens what is erased.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | SweepResult
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: SweepRequest,
) -> Response[Problem | SweepResult]:
    """Delete closed beads in bulk

     Bulk clearance of CLOSED beads from ONE tier: the operation behind `bd purge` (`tier: ephemeral`)
    and `bd prune` (`tier: durable`). It is one of the two DESTRUCTIVE operations on this surface — the
    other is `issues:delete` — and nothing it deletes comes back.

    A collection-level custom method rather than `DELETE /v0/beads/issues?...`: this describes a SET and
    acts on it, and a `DELETE` with a filtering query string is the shape where a dropped parameter
    widens what is erased. Sending a body makes every narrowing term a member the server refuses by name
    if it does not know it.

    ## What it selects, in order

    The closed rows of `tier`, then `pattern`, then the two protections — pinned beads are never swept
    and `protect_referenced` holds back beads another live bead cites. The ORDER is part of the answer:
    `skipped` counts what the request actually reached, so a pinned bead the pattern excluded is not
    counted as protected.

    ## The safety gate is the ROLE'S, not this handler's

    A `durable` sweep with neither `closed_before` nor `pattern` is refused with `400` /
    `invalid_argument`. That refusal comes from the same library surface `bd prune` calls, not from a
    check written here, which is what makes this endpoint incapable of erasing every closed bead in a
    workspace by omission. A caller that really means everything closed sends `pattern: "*"`.

    ## One transaction

    The selection and the deletion share one transaction, so the set the response describes IS the set
    that was deleted. The cost is that a sweep is all-or-nothing: one large enough to exceed the
    backend's write timeout fails whole and deletes nothing. Narrow the request rather than expecting
    progress.

    `dry_run: true` answers the same question and changes nothing — including history. Ask it first.

    Args:
        body (SweepRequest): Which closed beads to clear. The predicate is FIXED at "closed beads
            of one tier" and the two narrowing members only narrow it: there is no status, no
            assignee, no label and no free-text query here, because every one of those would be
            another way to spell a destructive selection that a caller could get subtly wrong.

            `additionalProperties: false`, so an unknown member is a `400` naming the member — the
            same posture the query-parameter rule takes, and for the same reason: on this operation a
            silently ignored narrowing term widens what is erased.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | SweepResult]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    body: SweepRequest,
) -> Problem | SweepResult | None:
    """Delete closed beads in bulk

     Bulk clearance of CLOSED beads from ONE tier: the operation behind `bd purge` (`tier: ephemeral`)
    and `bd prune` (`tier: durable`). It is one of the two DESTRUCTIVE operations on this surface — the
    other is `issues:delete` — and nothing it deletes comes back.

    A collection-level custom method rather than `DELETE /v0/beads/issues?...`: this describes a SET and
    acts on it, and a `DELETE` with a filtering query string is the shape where a dropped parameter
    widens what is erased. Sending a body makes every narrowing term a member the server refuses by name
    if it does not know it.

    ## What it selects, in order

    The closed rows of `tier`, then `pattern`, then the two protections — pinned beads are never swept
    and `protect_referenced` holds back beads another live bead cites. The ORDER is part of the answer:
    `skipped` counts what the request actually reached, so a pinned bead the pattern excluded is not
    counted as protected.

    ## The safety gate is the ROLE'S, not this handler's

    A `durable` sweep with neither `closed_before` nor `pattern` is refused with `400` /
    `invalid_argument`. That refusal comes from the same library surface `bd prune` calls, not from a
    check written here, which is what makes this endpoint incapable of erasing every closed bead in a
    workspace by omission. A caller that really means everything closed sends `pattern: "*"`.

    ## One transaction

    The selection and the deletion share one transaction, so the set the response describes IS the set
    that was deleted. The cost is that a sweep is all-or-nothing: one large enough to exceed the
    backend's write timeout fails whole and deletes nothing. Narrow the request rather than expecting
    progress.

    `dry_run: true` answers the same question and changes nothing — including history. Ask it first.

    Args:
        body (SweepRequest): Which closed beads to clear. The predicate is FIXED at "closed beads
            of one tier" and the two narrowing members only narrow it: there is no status, no
            assignee, no label and no free-text query here, because every one of those would be
            another way to spell a destructive selection that a caller could get subtly wrong.

            `additionalProperties: false`, so an unknown member is a `400` naming the member — the
            same posture the query-parameter rule takes, and for the same reason: on this operation a
            silently ignored narrowing term widens what is erased.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | SweepResult
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
