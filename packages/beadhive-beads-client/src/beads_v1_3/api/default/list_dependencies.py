from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.dependency_edges import DependencyEdges
from ...models.problem import Problem
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    issue_id: list[str],
    type_: list[str] | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_issue_id = issue_id

    params["issue_id"] = json_issue_id

    json_type_: list[str] | Unset = UNSET
    if not isinstance(type_, Unset):
        json_type_ = type_

    params["type"] = json_type_

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v0/beads/dependencies",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> DependencyEdges | Problem | None:
    if response.status_code == 200:
        response_200 = DependencyEdges.from_dict(response.json())

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
) -> Response[DependencyEdges | Problem]:
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
    type_: list[str] | Unset = UNSET,
) -> Response[DependencyEdges | Problem]:
    """List the stored dependency edges of several issues

     The STORED EDGE ROWS whose source is each named issue — the answer `bd dep list a b c` gives, and
    the same `Dependency` elements that command's `--json` emits.

    THESE ARE ROWS, NOT NEIGHBORS, and that is the difference from the `dependencies` member of `GET
    /v0/beads/issues/{id}`'s issue body. A dependency target may be an `external:` reference or an id
    belonging to another repository, and this database holds no issue for either; such an edge is
    returned here with its target spelled exactly as stored, and nothing is looked up on the far end.
    The `id` member of each element is absent: the read behind this operation does not select the row's
    surrogate key, so the `(issue_id, depends_on_id, type)` triple is what identifies an edge on this
    surface.

    THE DIRECTION IS OUTGOING ONLY — what each issue depends on. There is no direction parameter: the
    inbound bulk read is a different read against a different key with its own de-duplication rule
    across the two dependency tables, and adding it later is additive.

    A NAMED ISSUE THAT DOES NOT EXIST IS NOT A 404. It is listed in `missing`, and the edges of the
    issues that do exist are still returned: a batch that failed on one absent id would throw away the
    answers for the ids that were found. An issue that exists and depends on nothing is absent from
    `missing` and contributes no `items`, which is the distinction this operation exists to publish — an
    empty edge list is otherwise indistinguishable from a typo, and the empty list is the common case.

    THERE IS NO `limit` AND NO CURSOR. `bd dep list` has no limit either, so one here would make the two
    surfaces answer differently by default; instead the QUESTION is bounded — at most 100 `issue_id`
    values per call — which bounds the answer without truncating it. Each issue's edges are unbounded,
    exactly as the `dependencies` member of `GET /v0/beads/issues/{id}` already is.

    Args:
        issue_id (list[str]):
        type_ (list[str] | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DependencyEdges | Problem]
    """

    kwargs = _get_kwargs(
        issue_id=issue_id,
        type_=type_,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    issue_id: list[str],
    type_: list[str] | Unset = UNSET,
) -> DependencyEdges | Problem | None:
    """List the stored dependency edges of several issues

     The STORED EDGE ROWS whose source is each named issue — the answer `bd dep list a b c` gives, and
    the same `Dependency` elements that command's `--json` emits.

    THESE ARE ROWS, NOT NEIGHBORS, and that is the difference from the `dependencies` member of `GET
    /v0/beads/issues/{id}`'s issue body. A dependency target may be an `external:` reference or an id
    belonging to another repository, and this database holds no issue for either; such an edge is
    returned here with its target spelled exactly as stored, and nothing is looked up on the far end.
    The `id` member of each element is absent: the read behind this operation does not select the row's
    surrogate key, so the `(issue_id, depends_on_id, type)` triple is what identifies an edge on this
    surface.

    THE DIRECTION IS OUTGOING ONLY — what each issue depends on. There is no direction parameter: the
    inbound bulk read is a different read against a different key with its own de-duplication rule
    across the two dependency tables, and adding it later is additive.

    A NAMED ISSUE THAT DOES NOT EXIST IS NOT A 404. It is listed in `missing`, and the edges of the
    issues that do exist are still returned: a batch that failed on one absent id would throw away the
    answers for the ids that were found. An issue that exists and depends on nothing is absent from
    `missing` and contributes no `items`, which is the distinction this operation exists to publish — an
    empty edge list is otherwise indistinguishable from a typo, and the empty list is the common case.

    THERE IS NO `limit` AND NO CURSOR. `bd dep list` has no limit either, so one here would make the two
    surfaces answer differently by default; instead the QUESTION is bounded — at most 100 `issue_id`
    values per call — which bounds the answer without truncating it. Each issue's edges are unbounded,
    exactly as the `dependencies` member of `GET /v0/beads/issues/{id}` already is.

    Args:
        issue_id (list[str]):
        type_ (list[str] | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DependencyEdges | Problem
    """

    return sync_detailed(
        client=client,
        issue_id=issue_id,
        type_=type_,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    issue_id: list[str],
    type_: list[str] | Unset = UNSET,
) -> Response[DependencyEdges | Problem]:
    """List the stored dependency edges of several issues

     The STORED EDGE ROWS whose source is each named issue — the answer `bd dep list a b c` gives, and
    the same `Dependency` elements that command's `--json` emits.

    THESE ARE ROWS, NOT NEIGHBORS, and that is the difference from the `dependencies` member of `GET
    /v0/beads/issues/{id}`'s issue body. A dependency target may be an `external:` reference or an id
    belonging to another repository, and this database holds no issue for either; such an edge is
    returned here with its target spelled exactly as stored, and nothing is looked up on the far end.
    The `id` member of each element is absent: the read behind this operation does not select the row's
    surrogate key, so the `(issue_id, depends_on_id, type)` triple is what identifies an edge on this
    surface.

    THE DIRECTION IS OUTGOING ONLY — what each issue depends on. There is no direction parameter: the
    inbound bulk read is a different read against a different key with its own de-duplication rule
    across the two dependency tables, and adding it later is additive.

    A NAMED ISSUE THAT DOES NOT EXIST IS NOT A 404. It is listed in `missing`, and the edges of the
    issues that do exist are still returned: a batch that failed on one absent id would throw away the
    answers for the ids that were found. An issue that exists and depends on nothing is absent from
    `missing` and contributes no `items`, which is the distinction this operation exists to publish — an
    empty edge list is otherwise indistinguishable from a typo, and the empty list is the common case.

    THERE IS NO `limit` AND NO CURSOR. `bd dep list` has no limit either, so one here would make the two
    surfaces answer differently by default; instead the QUESTION is bounded — at most 100 `issue_id`
    values per call — which bounds the answer without truncating it. Each issue's edges are unbounded,
    exactly as the `dependencies` member of `GET /v0/beads/issues/{id}` already is.

    Args:
        issue_id (list[str]):
        type_ (list[str] | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DependencyEdges | Problem]
    """

    kwargs = _get_kwargs(
        issue_id=issue_id,
        type_=type_,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    issue_id: list[str],
    type_: list[str] | Unset = UNSET,
) -> DependencyEdges | Problem | None:
    """List the stored dependency edges of several issues

     The STORED EDGE ROWS whose source is each named issue — the answer `bd dep list a b c` gives, and
    the same `Dependency` elements that command's `--json` emits.

    THESE ARE ROWS, NOT NEIGHBORS, and that is the difference from the `dependencies` member of `GET
    /v0/beads/issues/{id}`'s issue body. A dependency target may be an `external:` reference or an id
    belonging to another repository, and this database holds no issue for either; such an edge is
    returned here with its target spelled exactly as stored, and nothing is looked up on the far end.
    The `id` member of each element is absent: the read behind this operation does not select the row's
    surrogate key, so the `(issue_id, depends_on_id, type)` triple is what identifies an edge on this
    surface.

    THE DIRECTION IS OUTGOING ONLY — what each issue depends on. There is no direction parameter: the
    inbound bulk read is a different read against a different key with its own de-duplication rule
    across the two dependency tables, and adding it later is additive.

    A NAMED ISSUE THAT DOES NOT EXIST IS NOT A 404. It is listed in `missing`, and the edges of the
    issues that do exist are still returned: a batch that failed on one absent id would throw away the
    answers for the ids that were found. An issue that exists and depends on nothing is absent from
    `missing` and contributes no `items`, which is the distinction this operation exists to publish — an
    empty edge list is otherwise indistinguishable from a typo, and the empty list is the common case.

    THERE IS NO `limit` AND NO CURSOR. `bd dep list` has no limit either, so one here would make the two
    surfaces answer differently by default; instead the QUESTION is bounded — at most 100 `issue_id`
    values per call — which bounds the answer without truncating it. Each issue's edges are unbounded,
    exactly as the `dependencies` member of `GET /v0/beads/issues/{id}` already is.

    Args:
        issue_id (list[str]):
        type_ (list[str] | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DependencyEdges | Problem
    """

    return (
        await asyncio_detailed(
            client=client,
            issue_id=issue_id,
            type_=type_,
        )
    ).parsed
