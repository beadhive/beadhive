from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.delete_issues_request import DeleteIssuesRequest
from ...models.delete_issues_result import DeleteIssuesResult
from ...models.problem import Problem
from ...types import Response


def _get_kwargs(
    *,
    body: DeleteIssuesRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v0/beads/issues:delete",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> DeleteIssuesResult | Problem | None:
    if response.status_code == 200:
        response_200 = DeleteIssuesResult.from_dict(response.json())

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
) -> Response[DeleteIssuesResult | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: DeleteIssuesRequest,
) -> Response[DeleteIssuesResult | Problem]:
    """Delete named beads

     Erases the beads this request NAMES, and — under `cascade` — everything that depends on them. It is
    the operation behind `bd delete`, the other DESTRUCTIVE operation on this surface alongside
    `issues:sweep`, and nothing it deletes comes back.

    It is a different operation from `POST /v0/beads/issues:sweep`, not a narrower spelling of it. A
    sweep DESCRIBES a set and lets the server resolve it; this one is handed ids. That difference is
    what decides where each one's safety lives: a description can be accidentally too wide, so the sweep
    carries a require-a-filter refusal, while a list of ids cannot be, so this one's guard is about the
    GRAPH instead.

    A collection-level custom method rather than `DELETE /v0/beads/issues/{id}`: this acts on a SET, its
    behavior turns on three flags, and a `DELETE` carrying those in a query string is the shape where a
    dropped parameter changes what is erased. Sending a body makes every one of them a member the server
    refuses by name if it does not know it.

    ## The three modes

    With neither `cascade` nor `force`, a named bead that some bead OUTSIDE the request depends on is
    REFUSED with `400` / `invalid_argument`, and nothing is deleted. `force` deletes the named beads and
    leaves those dependents ORPHANED — they keep their rows, lose their edges, and come back in
    `orphaned`. `cascade` deletes the transitive closure instead, so nothing is left outside it to
    orphan; a request carrying both behaves as `cascade`.

    The refusal is the LIBRARY's, not this handler's — the same surface `bd delete` calls — which is
    what makes this endpoint incapable of orphaning a workspace's graph by omission.

    ## Every id must resolve

    An id naming no stored bead is a `404` and NOTHING is deleted, not even the ids beside it that did
    resolve. There is no prefix matching here: ids are exact, because resolving an ambiguous prefix to a
    bead and then deleting it is the one place that convenience is not one.

    ## The guard, and why it takes one id

    `expected_version` is a compare-and-set precondition on the row this request NAMES: the deletion
    proceeds only if that bead's revision still equals it, and otherwise the request is `409
    precondition_failed` and nothing is deleted. It is the guard that matters most on this operation,
    because being wrong about which bead you are looking at is the one mistake here that cannot be
    undone.

    IT REQUIRES A SINGLE-ID REQUEST, and that is a `400` naming `expected_version` rather than a fudge:
    one token cannot describe two rows. Duplicates collapse first, so repeating one id is still one
    bead.

    THE ORDER THE REFUSALS HAPPEN IN IS PART OF THE ANSWER. Request shape first, then the existence
    probe (`404`), then this guard (`409`), then the dependents refusal (`400`). A request that is both
    a typo and a graph problem reports the typo, which is the one a caller can fix without deciding
    anything; and a stale guard outranks the dependents refusal because a caller whose view has moved
    should not be asked to choose `cascade` or `force` over information that has already changed.

    NEITHER `cascade` NOR `force` BYPASSES IT. They bypass POLICY. Under `cascade` the guard covers the
    named bead alone — the closure is resolved inside the deleting transaction, so it may have grown
    since the caller's read.

    ## One transaction

    The guard, the deletion and the rewrite of surviving beads' text references all share one
    transaction, so the set the response describes IS the set that was deleted and no bead is left
    citing an id that no longer exists. The cost is that a delete is all-or-nothing: one whose
    neighbourhood is large enough to exceed the backend's write timeout fails whole. Split the request
    rather than expecting progress.

    `dry_run: true` answers the same question and changes nothing — including history, and including the
    two refusals, which a preview reports exactly where the real request would. Ask it first.

    Args:
        body (DeleteIssuesRequest): Which beads to erase, and what to do about the beads that
            point at them. There is no predicate here — no status, no cutoff, no glob — and that
            absence is the reason this operation needs no require-a-filter gate: a caller cannot spell
            "everything" without typing every id.

            `additionalProperties: false`, so an unknown member is a `400` naming the member. On this
            operation a silently ignored member is the difference between orphaning a dependent and
            deleting it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DeleteIssuesResult | Problem]
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
    body: DeleteIssuesRequest,
) -> DeleteIssuesResult | Problem | None:
    """Delete named beads

     Erases the beads this request NAMES, and — under `cascade` — everything that depends on them. It is
    the operation behind `bd delete`, the other DESTRUCTIVE operation on this surface alongside
    `issues:sweep`, and nothing it deletes comes back.

    It is a different operation from `POST /v0/beads/issues:sweep`, not a narrower spelling of it. A
    sweep DESCRIBES a set and lets the server resolve it; this one is handed ids. That difference is
    what decides where each one's safety lives: a description can be accidentally too wide, so the sweep
    carries a require-a-filter refusal, while a list of ids cannot be, so this one's guard is about the
    GRAPH instead.

    A collection-level custom method rather than `DELETE /v0/beads/issues/{id}`: this acts on a SET, its
    behavior turns on three flags, and a `DELETE` carrying those in a query string is the shape where a
    dropped parameter changes what is erased. Sending a body makes every one of them a member the server
    refuses by name if it does not know it.

    ## The three modes

    With neither `cascade` nor `force`, a named bead that some bead OUTSIDE the request depends on is
    REFUSED with `400` / `invalid_argument`, and nothing is deleted. `force` deletes the named beads and
    leaves those dependents ORPHANED — they keep their rows, lose their edges, and come back in
    `orphaned`. `cascade` deletes the transitive closure instead, so nothing is left outside it to
    orphan; a request carrying both behaves as `cascade`.

    The refusal is the LIBRARY's, not this handler's — the same surface `bd delete` calls — which is
    what makes this endpoint incapable of orphaning a workspace's graph by omission.

    ## Every id must resolve

    An id naming no stored bead is a `404` and NOTHING is deleted, not even the ids beside it that did
    resolve. There is no prefix matching here: ids are exact, because resolving an ambiguous prefix to a
    bead and then deleting it is the one place that convenience is not one.

    ## The guard, and why it takes one id

    `expected_version` is a compare-and-set precondition on the row this request NAMES: the deletion
    proceeds only if that bead's revision still equals it, and otherwise the request is `409
    precondition_failed` and nothing is deleted. It is the guard that matters most on this operation,
    because being wrong about which bead you are looking at is the one mistake here that cannot be
    undone.

    IT REQUIRES A SINGLE-ID REQUEST, and that is a `400` naming `expected_version` rather than a fudge:
    one token cannot describe two rows. Duplicates collapse first, so repeating one id is still one
    bead.

    THE ORDER THE REFUSALS HAPPEN IN IS PART OF THE ANSWER. Request shape first, then the existence
    probe (`404`), then this guard (`409`), then the dependents refusal (`400`). A request that is both
    a typo and a graph problem reports the typo, which is the one a caller can fix without deciding
    anything; and a stale guard outranks the dependents refusal because a caller whose view has moved
    should not be asked to choose `cascade` or `force` over information that has already changed.

    NEITHER `cascade` NOR `force` BYPASSES IT. They bypass POLICY. Under `cascade` the guard covers the
    named bead alone — the closure is resolved inside the deleting transaction, so it may have grown
    since the caller's read.

    ## One transaction

    The guard, the deletion and the rewrite of surviving beads' text references all share one
    transaction, so the set the response describes IS the set that was deleted and no bead is left
    citing an id that no longer exists. The cost is that a delete is all-or-nothing: one whose
    neighbourhood is large enough to exceed the backend's write timeout fails whole. Split the request
    rather than expecting progress.

    `dry_run: true` answers the same question and changes nothing — including history, and including the
    two refusals, which a preview reports exactly where the real request would. Ask it first.

    Args:
        body (DeleteIssuesRequest): Which beads to erase, and what to do about the beads that
            point at them. There is no predicate here — no status, no cutoff, no glob — and that
            absence is the reason this operation needs no require-a-filter gate: a caller cannot spell
            "everything" without typing every id.

            `additionalProperties: false`, so an unknown member is a `400` naming the member. On this
            operation a silently ignored member is the difference between orphaning a dependent and
            deleting it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DeleteIssuesResult | Problem
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: DeleteIssuesRequest,
) -> Response[DeleteIssuesResult | Problem]:
    """Delete named beads

     Erases the beads this request NAMES, and — under `cascade` — everything that depends on them. It is
    the operation behind `bd delete`, the other DESTRUCTIVE operation on this surface alongside
    `issues:sweep`, and nothing it deletes comes back.

    It is a different operation from `POST /v0/beads/issues:sweep`, not a narrower spelling of it. A
    sweep DESCRIBES a set and lets the server resolve it; this one is handed ids. That difference is
    what decides where each one's safety lives: a description can be accidentally too wide, so the sweep
    carries a require-a-filter refusal, while a list of ids cannot be, so this one's guard is about the
    GRAPH instead.

    A collection-level custom method rather than `DELETE /v0/beads/issues/{id}`: this acts on a SET, its
    behavior turns on three flags, and a `DELETE` carrying those in a query string is the shape where a
    dropped parameter changes what is erased. Sending a body makes every one of them a member the server
    refuses by name if it does not know it.

    ## The three modes

    With neither `cascade` nor `force`, a named bead that some bead OUTSIDE the request depends on is
    REFUSED with `400` / `invalid_argument`, and nothing is deleted. `force` deletes the named beads and
    leaves those dependents ORPHANED — they keep their rows, lose their edges, and come back in
    `orphaned`. `cascade` deletes the transitive closure instead, so nothing is left outside it to
    orphan; a request carrying both behaves as `cascade`.

    The refusal is the LIBRARY's, not this handler's — the same surface `bd delete` calls — which is
    what makes this endpoint incapable of orphaning a workspace's graph by omission.

    ## Every id must resolve

    An id naming no stored bead is a `404` and NOTHING is deleted, not even the ids beside it that did
    resolve. There is no prefix matching here: ids are exact, because resolving an ambiguous prefix to a
    bead and then deleting it is the one place that convenience is not one.

    ## The guard, and why it takes one id

    `expected_version` is a compare-and-set precondition on the row this request NAMES: the deletion
    proceeds only if that bead's revision still equals it, and otherwise the request is `409
    precondition_failed` and nothing is deleted. It is the guard that matters most on this operation,
    because being wrong about which bead you are looking at is the one mistake here that cannot be
    undone.

    IT REQUIRES A SINGLE-ID REQUEST, and that is a `400` naming `expected_version` rather than a fudge:
    one token cannot describe two rows. Duplicates collapse first, so repeating one id is still one
    bead.

    THE ORDER THE REFUSALS HAPPEN IN IS PART OF THE ANSWER. Request shape first, then the existence
    probe (`404`), then this guard (`409`), then the dependents refusal (`400`). A request that is both
    a typo and a graph problem reports the typo, which is the one a caller can fix without deciding
    anything; and a stale guard outranks the dependents refusal because a caller whose view has moved
    should not be asked to choose `cascade` or `force` over information that has already changed.

    NEITHER `cascade` NOR `force` BYPASSES IT. They bypass POLICY. Under `cascade` the guard covers the
    named bead alone — the closure is resolved inside the deleting transaction, so it may have grown
    since the caller's read.

    ## One transaction

    The guard, the deletion and the rewrite of surviving beads' text references all share one
    transaction, so the set the response describes IS the set that was deleted and no bead is left
    citing an id that no longer exists. The cost is that a delete is all-or-nothing: one whose
    neighbourhood is large enough to exceed the backend's write timeout fails whole. Split the request
    rather than expecting progress.

    `dry_run: true` answers the same question and changes nothing — including history, and including the
    two refusals, which a preview reports exactly where the real request would. Ask it first.

    Args:
        body (DeleteIssuesRequest): Which beads to erase, and what to do about the beads that
            point at them. There is no predicate here — no status, no cutoff, no glob — and that
            absence is the reason this operation needs no require-a-filter gate: a caller cannot spell
            "everything" without typing every id.

            `additionalProperties: false`, so an unknown member is a `400` naming the member. On this
            operation a silently ignored member is the difference between orphaning a dependent and
            deleting it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DeleteIssuesResult | Problem]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    body: DeleteIssuesRequest,
) -> DeleteIssuesResult | Problem | None:
    """Delete named beads

     Erases the beads this request NAMES, and — under `cascade` — everything that depends on them. It is
    the operation behind `bd delete`, the other DESTRUCTIVE operation on this surface alongside
    `issues:sweep`, and nothing it deletes comes back.

    It is a different operation from `POST /v0/beads/issues:sweep`, not a narrower spelling of it. A
    sweep DESCRIBES a set and lets the server resolve it; this one is handed ids. That difference is
    what decides where each one's safety lives: a description can be accidentally too wide, so the sweep
    carries a require-a-filter refusal, while a list of ids cannot be, so this one's guard is about the
    GRAPH instead.

    A collection-level custom method rather than `DELETE /v0/beads/issues/{id}`: this acts on a SET, its
    behavior turns on three flags, and a `DELETE` carrying those in a query string is the shape where a
    dropped parameter changes what is erased. Sending a body makes every one of them a member the server
    refuses by name if it does not know it.

    ## The three modes

    With neither `cascade` nor `force`, a named bead that some bead OUTSIDE the request depends on is
    REFUSED with `400` / `invalid_argument`, and nothing is deleted. `force` deletes the named beads and
    leaves those dependents ORPHANED — they keep their rows, lose their edges, and come back in
    `orphaned`. `cascade` deletes the transitive closure instead, so nothing is left outside it to
    orphan; a request carrying both behaves as `cascade`.

    The refusal is the LIBRARY's, not this handler's — the same surface `bd delete` calls — which is
    what makes this endpoint incapable of orphaning a workspace's graph by omission.

    ## Every id must resolve

    An id naming no stored bead is a `404` and NOTHING is deleted, not even the ids beside it that did
    resolve. There is no prefix matching here: ids are exact, because resolving an ambiguous prefix to a
    bead and then deleting it is the one place that convenience is not one.

    ## The guard, and why it takes one id

    `expected_version` is a compare-and-set precondition on the row this request NAMES: the deletion
    proceeds only if that bead's revision still equals it, and otherwise the request is `409
    precondition_failed` and nothing is deleted. It is the guard that matters most on this operation,
    because being wrong about which bead you are looking at is the one mistake here that cannot be
    undone.

    IT REQUIRES A SINGLE-ID REQUEST, and that is a `400` naming `expected_version` rather than a fudge:
    one token cannot describe two rows. Duplicates collapse first, so repeating one id is still one
    bead.

    THE ORDER THE REFUSALS HAPPEN IN IS PART OF THE ANSWER. Request shape first, then the existence
    probe (`404`), then this guard (`409`), then the dependents refusal (`400`). A request that is both
    a typo and a graph problem reports the typo, which is the one a caller can fix without deciding
    anything; and a stale guard outranks the dependents refusal because a caller whose view has moved
    should not be asked to choose `cascade` or `force` over information that has already changed.

    NEITHER `cascade` NOR `force` BYPASSES IT. They bypass POLICY. Under `cascade` the guard covers the
    named bead alone — the closure is resolved inside the deleting transaction, so it may have grown
    since the caller's read.

    ## One transaction

    The guard, the deletion and the rewrite of surviving beads' text references all share one
    transaction, so the set the response describes IS the set that was deleted and no bead is left
    citing an id that no longer exists. The cost is that a delete is all-or-nothing: one whose
    neighbourhood is large enough to exceed the backend's write timeout fails whole. Split the request
    rather than expecting progress.

    `dry_run: true` answers the same question and changes nothing — including history, and including the
    two refusals, which a preview reports exactly where the real request would. Ask it first.

    Args:
        body (DeleteIssuesRequest): Which beads to erase, and what to do about the beads that
            point at them. There is no predicate here — no status, no cutoff, no glob — and that
            absence is the reason this operation needs no require-a-filter gate: a caller cannot spell
            "everything" without typing every id.

            `additionalProperties: false`, so an unknown member is a `400` naming the member. On this
            operation a silently ignored member is the difference between orphaning a dependent and
            deleting it.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DeleteIssuesResult | Problem
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
