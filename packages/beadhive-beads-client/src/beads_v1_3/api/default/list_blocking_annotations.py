from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.blocking_annotations import BlockingAnnotations
from ...models.problem import Problem
from ...types import UNSET, Response


def _get_kwargs(
    *,
    issue_id: list[str],
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_issue_id = issue_id

    params["issue_id"] = json_issue_id

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v0/beads/dependencies/blocking",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> BlockingAnnotations | Problem | None:
    if response.status_code == 200:
        response_200 = BlockingAnnotations.from_dict(response.json())

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
) -> Response[BlockingAnnotations | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    issue_id: list[str],
) -> Response[BlockingAnnotations | Problem]:
    """Read the blocking decoration of several issues

     The DERIVED blocking summary `bd list` prints beside each row — `(parent: X, blocked by: Y, blocks:
    Z)` — for the issues named here.

    IT IS DERIVED, NOT STORED, and that is the whole difference from `GET /v0/beads/dependencies` next
    door. That operation returns the edge ROWS: every edge type, targets spelled exactly as stored,
    nothing looked up. This one answers a summary over TWO of those types with a rule applied: a
    `blocks` edge counts only while its blocker is open, and a `parent-child` edge becomes `parent`
    rather than a blocker. A client that wants the rows asks the other operation; a client that wants
    the decoration a listing shows asks this one.

    A CLOSED BLOCKER IS NOT A BLOCKER, in either direction. An issue whose every blocker is closed comes
    back with an empty `blocked_by`, and a CLOSED issue blocks nothing — its `blocks` is empty even
    where the edges still exist. `parent` follows the same rule: a closed parent is absent.

    A BLOCKER THIS WORKSPACE HOLDS NO ROW FOR STILL BLOCKS. An `external:` reference, an id in another
    repository's namespace and an id whose issue was deleted out from under its edges are all statuses
    this database cannot read, and an unreadable status is not `closed`. Hiding such a blocker would
    report work as unblocked on the strength of a row that was never found, so it is reported.

    THERE IS NO `missing` MEMBER, unlike `GET /v0/beads/dependencies`. This operation runs no existence
    probe: an id that names nothing and an id with no live blocking edges decorate identically, so the
    probe would be a read whose answer no client could act on. A client that needs to know whether an id
    exists calls `GET /v0/beads/issues/{id}`, which answers `404`. Every requested id gets an entry here
    either way.

    THERE IS NO `limit` AND NO CURSOR, for the reason `GET /v0/beads/dependencies` has none: the
    QUESTION is bounded instead, at 100 `issue_id` values per call, which bounds the answer without
    truncating it.

    Args:
        issue_id (list[str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BlockingAnnotations | Problem]
    """

    kwargs = _get_kwargs(
        issue_id=issue_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    issue_id: list[str],
) -> BlockingAnnotations | Problem | None:
    """Read the blocking decoration of several issues

     The DERIVED blocking summary `bd list` prints beside each row — `(parent: X, blocked by: Y, blocks:
    Z)` — for the issues named here.

    IT IS DERIVED, NOT STORED, and that is the whole difference from `GET /v0/beads/dependencies` next
    door. That operation returns the edge ROWS: every edge type, targets spelled exactly as stored,
    nothing looked up. This one answers a summary over TWO of those types with a rule applied: a
    `blocks` edge counts only while its blocker is open, and a `parent-child` edge becomes `parent`
    rather than a blocker. A client that wants the rows asks the other operation; a client that wants
    the decoration a listing shows asks this one.

    A CLOSED BLOCKER IS NOT A BLOCKER, in either direction. An issue whose every blocker is closed comes
    back with an empty `blocked_by`, and a CLOSED issue blocks nothing — its `blocks` is empty even
    where the edges still exist. `parent` follows the same rule: a closed parent is absent.

    A BLOCKER THIS WORKSPACE HOLDS NO ROW FOR STILL BLOCKS. An `external:` reference, an id in another
    repository's namespace and an id whose issue was deleted out from under its edges are all statuses
    this database cannot read, and an unreadable status is not `closed`. Hiding such a blocker would
    report work as unblocked on the strength of a row that was never found, so it is reported.

    THERE IS NO `missing` MEMBER, unlike `GET /v0/beads/dependencies`. This operation runs no existence
    probe: an id that names nothing and an id with no live blocking edges decorate identically, so the
    probe would be a read whose answer no client could act on. A client that needs to know whether an id
    exists calls `GET /v0/beads/issues/{id}`, which answers `404`. Every requested id gets an entry here
    either way.

    THERE IS NO `limit` AND NO CURSOR, for the reason `GET /v0/beads/dependencies` has none: the
    QUESTION is bounded instead, at 100 `issue_id` values per call, which bounds the answer without
    truncating it.

    Args:
        issue_id (list[str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BlockingAnnotations | Problem
    """

    return sync_detailed(
        client=client,
        issue_id=issue_id,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    issue_id: list[str],
) -> Response[BlockingAnnotations | Problem]:
    """Read the blocking decoration of several issues

     The DERIVED blocking summary `bd list` prints beside each row — `(parent: X, blocked by: Y, blocks:
    Z)` — for the issues named here.

    IT IS DERIVED, NOT STORED, and that is the whole difference from `GET /v0/beads/dependencies` next
    door. That operation returns the edge ROWS: every edge type, targets spelled exactly as stored,
    nothing looked up. This one answers a summary over TWO of those types with a rule applied: a
    `blocks` edge counts only while its blocker is open, and a `parent-child` edge becomes `parent`
    rather than a blocker. A client that wants the rows asks the other operation; a client that wants
    the decoration a listing shows asks this one.

    A CLOSED BLOCKER IS NOT A BLOCKER, in either direction. An issue whose every blocker is closed comes
    back with an empty `blocked_by`, and a CLOSED issue blocks nothing — its `blocks` is empty even
    where the edges still exist. `parent` follows the same rule: a closed parent is absent.

    A BLOCKER THIS WORKSPACE HOLDS NO ROW FOR STILL BLOCKS. An `external:` reference, an id in another
    repository's namespace and an id whose issue was deleted out from under its edges are all statuses
    this database cannot read, and an unreadable status is not `closed`. Hiding such a blocker would
    report work as unblocked on the strength of a row that was never found, so it is reported.

    THERE IS NO `missing` MEMBER, unlike `GET /v0/beads/dependencies`. This operation runs no existence
    probe: an id that names nothing and an id with no live blocking edges decorate identically, so the
    probe would be a read whose answer no client could act on. A client that needs to know whether an id
    exists calls `GET /v0/beads/issues/{id}`, which answers `404`. Every requested id gets an entry here
    either way.

    THERE IS NO `limit` AND NO CURSOR, for the reason `GET /v0/beads/dependencies` has none: the
    QUESTION is bounded instead, at 100 `issue_id` values per call, which bounds the answer without
    truncating it.

    Args:
        issue_id (list[str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BlockingAnnotations | Problem]
    """

    kwargs = _get_kwargs(
        issue_id=issue_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    issue_id: list[str],
) -> BlockingAnnotations | Problem | None:
    """Read the blocking decoration of several issues

     The DERIVED blocking summary `bd list` prints beside each row — `(parent: X, blocked by: Y, blocks:
    Z)` — for the issues named here.

    IT IS DERIVED, NOT STORED, and that is the whole difference from `GET /v0/beads/dependencies` next
    door. That operation returns the edge ROWS: every edge type, targets spelled exactly as stored,
    nothing looked up. This one answers a summary over TWO of those types with a rule applied: a
    `blocks` edge counts only while its blocker is open, and a `parent-child` edge becomes `parent`
    rather than a blocker. A client that wants the rows asks the other operation; a client that wants
    the decoration a listing shows asks this one.

    A CLOSED BLOCKER IS NOT A BLOCKER, in either direction. An issue whose every blocker is closed comes
    back with an empty `blocked_by`, and a CLOSED issue blocks nothing — its `blocks` is empty even
    where the edges still exist. `parent` follows the same rule: a closed parent is absent.

    A BLOCKER THIS WORKSPACE HOLDS NO ROW FOR STILL BLOCKS. An `external:` reference, an id in another
    repository's namespace and an id whose issue was deleted out from under its edges are all statuses
    this database cannot read, and an unreadable status is not `closed`. Hiding such a blocker would
    report work as unblocked on the strength of a row that was never found, so it is reported.

    THERE IS NO `missing` MEMBER, unlike `GET /v0/beads/dependencies`. This operation runs no existence
    probe: an id that names nothing and an id with no live blocking edges decorate identically, so the
    probe would be a read whose answer no client could act on. A client that needs to know whether an id
    exists calls `GET /v0/beads/issues/{id}`, which answers `404`. Every requested id gets an entry here
    either way.

    THERE IS NO `limit` AND NO CURSOR, for the reason `GET /v0/beads/dependencies` has none: the
    QUESTION is bounded instead, at 100 `issue_id` values per call, which bounds the answer without
    truncating it.

    Args:
        issue_id (list[str]):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BlockingAnnotations | Problem
    """

    return (
        await asyncio_detailed(
            client=client,
            issue_id=issue_id,
        )
    ).parsed
