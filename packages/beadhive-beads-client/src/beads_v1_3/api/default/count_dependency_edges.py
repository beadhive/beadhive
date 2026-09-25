from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.count_dependency_edges_direction import CountDependencyEdgesDirection
from ...models.edge_counts import EdgeCounts
from ...models.problem import Problem
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    issue_id: list[str],
    direction: CountDependencyEdgesDirection,
    type_: list[str] | Unset = UNSET,
    status: str | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_issue_id = issue_id

    params["issue_id"] = json_issue_id

    json_direction = direction.value
    params["direction"] = json_direction

    json_type_: list[str] | Unset = UNSET
    if not isinstance(type_, Unset):
        json_type_ = type_

    params["type"] = json_type_

    params["status"] = status

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v0/beads/dependencies:count",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> EdgeCounts | Problem | None:
    if response.status_code == 200:
        response_200 = EdgeCounts.from_dict(response.json())

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
) -> Response[EdgeCounts | Problem]:
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
    direction: CountDependencyEdgesDirection,
    type_: list[str] | Unset = UNSET,
    status: str | Unset = UNSET,
) -> Response[EdgeCounts | Problem]:
    """Count the dependency edges around several issues

     HOW MANY EDGES each named issue has in ONE named direction, after the type and status filters — the
    cardinality behind `GET /v0/beads/dependencies`, plus the direction that read does not take. Nothing
    is materialized: this is the operation for a caller that wants the numbers for issues whose edges it
    will never print.

    IT IS NOT THE LISTING COUNTED, and the operationId says so rather than pretending otherwise. The
    listing is OUTGOING ONLY and takes no direction; this one REQUIRES a direction and answers about
    either end. Naming it `countDependencies` would have promised that it sizes the set
    `listDependencies` returns, which it does only at `direction=out`.

    THE ANSWER IS PER ANCHOR, which is the other difference from the listing. That one flattens every
    issue's edges onto one array because the rows carry their own `issue_id`; a number does not, so
    folding these together would produce a total no caller asked for and lose the per-issue answer every
    caller wants. `anchors` carries one entry per DISTINCT requested id, in the order the request first
    named it — an array rather than an object, because the request's order is part of the answer and a
    keyed object would have made that ordering this surface's own invention.

    A NAMED ISSUE THAT DOES NOT EXIST IS NOT A 404, on `GET /v0/beads/dependencies`'s terms and one
    sharper: its entry reports `missing: true` with `count: 0`, and 0 is the COMMON answer here — most
    issues have no edges in at least one direction — so without that flag a typo would be
    indistinguishable from a real zero and would never surface. There is no `not_found` on this
    operation at all.

    THE COUNT SPANS BOTH DEPENDENCY PLANES and is a SUM rather than a distinct count of edge rows; a
    status-narrowed count reads the dependent's status from its own plane; and `status` is legal only
    with `direction=in` because an outbound edge's far end may be a row this database does not hold.
    Those are the ROLE's rules, stated once at `issueops.GraphCounter` and its `EdgeCountRequest`, and
    this document cites them rather than restating them — a second telling is a second thing to keep
    true.

    THERE IS NO `limit` AND NO CURSOR, for the reason `GET /v0/beads/dependencies` has none and `GET
    /v0/beads/issues:count` gives: a cardinality has no page, and bounding the scan would answer "how
    many of the first N". The QUESTION is bounded instead, at 100 `issue_id` values per call — the same
    bound, the same number and the same constant as the listing on this collection, because the two
    bound the same thing and a client holding both must not have to learn two numbers.

    THERE IS NO `both` DIRECTION. A caller that wants the pair asks twice, which is what every front
    door in the tree already does. One call answering both would mean two numbers per anchor, and
    `status` — which narrows by a row only the inbound direction has — would then govern one of them and
    silently not the other.

    Args:
        issue_id (list[str]):
        direction (CountDependencyEdgesDirection):
        type_ (list[str] | Unset):
        status (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EdgeCounts | Problem]
    """

    kwargs = _get_kwargs(
        issue_id=issue_id,
        direction=direction,
        type_=type_,
        status=status,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    issue_id: list[str],
    direction: CountDependencyEdgesDirection,
    type_: list[str] | Unset = UNSET,
    status: str | Unset = UNSET,
) -> EdgeCounts | Problem | None:
    """Count the dependency edges around several issues

     HOW MANY EDGES each named issue has in ONE named direction, after the type and status filters — the
    cardinality behind `GET /v0/beads/dependencies`, plus the direction that read does not take. Nothing
    is materialized: this is the operation for a caller that wants the numbers for issues whose edges it
    will never print.

    IT IS NOT THE LISTING COUNTED, and the operationId says so rather than pretending otherwise. The
    listing is OUTGOING ONLY and takes no direction; this one REQUIRES a direction and answers about
    either end. Naming it `countDependencies` would have promised that it sizes the set
    `listDependencies` returns, which it does only at `direction=out`.

    THE ANSWER IS PER ANCHOR, which is the other difference from the listing. That one flattens every
    issue's edges onto one array because the rows carry their own `issue_id`; a number does not, so
    folding these together would produce a total no caller asked for and lose the per-issue answer every
    caller wants. `anchors` carries one entry per DISTINCT requested id, in the order the request first
    named it — an array rather than an object, because the request's order is part of the answer and a
    keyed object would have made that ordering this surface's own invention.

    A NAMED ISSUE THAT DOES NOT EXIST IS NOT A 404, on `GET /v0/beads/dependencies`'s terms and one
    sharper: its entry reports `missing: true` with `count: 0`, and 0 is the COMMON answer here — most
    issues have no edges in at least one direction — so without that flag a typo would be
    indistinguishable from a real zero and would never surface. There is no `not_found` on this
    operation at all.

    THE COUNT SPANS BOTH DEPENDENCY PLANES and is a SUM rather than a distinct count of edge rows; a
    status-narrowed count reads the dependent's status from its own plane; and `status` is legal only
    with `direction=in` because an outbound edge's far end may be a row this database does not hold.
    Those are the ROLE's rules, stated once at `issueops.GraphCounter` and its `EdgeCountRequest`, and
    this document cites them rather than restating them — a second telling is a second thing to keep
    true.

    THERE IS NO `limit` AND NO CURSOR, for the reason `GET /v0/beads/dependencies` has none and `GET
    /v0/beads/issues:count` gives: a cardinality has no page, and bounding the scan would answer "how
    many of the first N". The QUESTION is bounded instead, at 100 `issue_id` values per call — the same
    bound, the same number and the same constant as the listing on this collection, because the two
    bound the same thing and a client holding both must not have to learn two numbers.

    THERE IS NO `both` DIRECTION. A caller that wants the pair asks twice, which is what every front
    door in the tree already does. One call answering both would mean two numbers per anchor, and
    `status` — which narrows by a row only the inbound direction has — would then govern one of them and
    silently not the other.

    Args:
        issue_id (list[str]):
        direction (CountDependencyEdgesDirection):
        type_ (list[str] | Unset):
        status (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EdgeCounts | Problem
    """

    return sync_detailed(
        client=client,
        issue_id=issue_id,
        direction=direction,
        type_=type_,
        status=status,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    issue_id: list[str],
    direction: CountDependencyEdgesDirection,
    type_: list[str] | Unset = UNSET,
    status: str | Unset = UNSET,
) -> Response[EdgeCounts | Problem]:
    """Count the dependency edges around several issues

     HOW MANY EDGES each named issue has in ONE named direction, after the type and status filters — the
    cardinality behind `GET /v0/beads/dependencies`, plus the direction that read does not take. Nothing
    is materialized: this is the operation for a caller that wants the numbers for issues whose edges it
    will never print.

    IT IS NOT THE LISTING COUNTED, and the operationId says so rather than pretending otherwise. The
    listing is OUTGOING ONLY and takes no direction; this one REQUIRES a direction and answers about
    either end. Naming it `countDependencies` would have promised that it sizes the set
    `listDependencies` returns, which it does only at `direction=out`.

    THE ANSWER IS PER ANCHOR, which is the other difference from the listing. That one flattens every
    issue's edges onto one array because the rows carry their own `issue_id`; a number does not, so
    folding these together would produce a total no caller asked for and lose the per-issue answer every
    caller wants. `anchors` carries one entry per DISTINCT requested id, in the order the request first
    named it — an array rather than an object, because the request's order is part of the answer and a
    keyed object would have made that ordering this surface's own invention.

    A NAMED ISSUE THAT DOES NOT EXIST IS NOT A 404, on `GET /v0/beads/dependencies`'s terms and one
    sharper: its entry reports `missing: true` with `count: 0`, and 0 is the COMMON answer here — most
    issues have no edges in at least one direction — so without that flag a typo would be
    indistinguishable from a real zero and would never surface. There is no `not_found` on this
    operation at all.

    THE COUNT SPANS BOTH DEPENDENCY PLANES and is a SUM rather than a distinct count of edge rows; a
    status-narrowed count reads the dependent's status from its own plane; and `status` is legal only
    with `direction=in` because an outbound edge's far end may be a row this database does not hold.
    Those are the ROLE's rules, stated once at `issueops.GraphCounter` and its `EdgeCountRequest`, and
    this document cites them rather than restating them — a second telling is a second thing to keep
    true.

    THERE IS NO `limit` AND NO CURSOR, for the reason `GET /v0/beads/dependencies` has none and `GET
    /v0/beads/issues:count` gives: a cardinality has no page, and bounding the scan would answer "how
    many of the first N". The QUESTION is bounded instead, at 100 `issue_id` values per call — the same
    bound, the same number and the same constant as the listing on this collection, because the two
    bound the same thing and a client holding both must not have to learn two numbers.

    THERE IS NO `both` DIRECTION. A caller that wants the pair asks twice, which is what every front
    door in the tree already does. One call answering both would mean two numbers per anchor, and
    `status` — which narrows by a row only the inbound direction has — would then govern one of them and
    silently not the other.

    Args:
        issue_id (list[str]):
        direction (CountDependencyEdgesDirection):
        type_ (list[str] | Unset):
        status (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EdgeCounts | Problem]
    """

    kwargs = _get_kwargs(
        issue_id=issue_id,
        direction=direction,
        type_=type_,
        status=status,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    issue_id: list[str],
    direction: CountDependencyEdgesDirection,
    type_: list[str] | Unset = UNSET,
    status: str | Unset = UNSET,
) -> EdgeCounts | Problem | None:
    """Count the dependency edges around several issues

     HOW MANY EDGES each named issue has in ONE named direction, after the type and status filters — the
    cardinality behind `GET /v0/beads/dependencies`, plus the direction that read does not take. Nothing
    is materialized: this is the operation for a caller that wants the numbers for issues whose edges it
    will never print.

    IT IS NOT THE LISTING COUNTED, and the operationId says so rather than pretending otherwise. The
    listing is OUTGOING ONLY and takes no direction; this one REQUIRES a direction and answers about
    either end. Naming it `countDependencies` would have promised that it sizes the set
    `listDependencies` returns, which it does only at `direction=out`.

    THE ANSWER IS PER ANCHOR, which is the other difference from the listing. That one flattens every
    issue's edges onto one array because the rows carry their own `issue_id`; a number does not, so
    folding these together would produce a total no caller asked for and lose the per-issue answer every
    caller wants. `anchors` carries one entry per DISTINCT requested id, in the order the request first
    named it — an array rather than an object, because the request's order is part of the answer and a
    keyed object would have made that ordering this surface's own invention.

    A NAMED ISSUE THAT DOES NOT EXIST IS NOT A 404, on `GET /v0/beads/dependencies`'s terms and one
    sharper: its entry reports `missing: true` with `count: 0`, and 0 is the COMMON answer here — most
    issues have no edges in at least one direction — so without that flag a typo would be
    indistinguishable from a real zero and would never surface. There is no `not_found` on this
    operation at all.

    THE COUNT SPANS BOTH DEPENDENCY PLANES and is a SUM rather than a distinct count of edge rows; a
    status-narrowed count reads the dependent's status from its own plane; and `status` is legal only
    with `direction=in` because an outbound edge's far end may be a row this database does not hold.
    Those are the ROLE's rules, stated once at `issueops.GraphCounter` and its `EdgeCountRequest`, and
    this document cites them rather than restating them — a second telling is a second thing to keep
    true.

    THERE IS NO `limit` AND NO CURSOR, for the reason `GET /v0/beads/dependencies` has none and `GET
    /v0/beads/issues:count` gives: a cardinality has no page, and bounding the scan would answer "how
    many of the first N". The QUESTION is bounded instead, at 100 `issue_id` values per call — the same
    bound, the same number and the same constant as the listing on this collection, because the two
    bound the same thing and a client holding both must not have to learn two numbers.

    THERE IS NO `both` DIRECTION. A caller that wants the pair asks twice, which is what every front
    door in the tree already does. One call answering both would mean two numbers per anchor, and
    `status` — which narrows by a row only the inbound direction has — would then govern one of them and
    silently not the other.

    Args:
        issue_id (list[str]):
        direction (CountDependencyEdgesDirection):
        type_ (list[str] | Unset):
        status (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EdgeCounts | Problem
    """

    return (
        await asyncio_detailed(
            client=client,
            issue_id=issue_id,
            direction=direction,
            type_=type_,
            status=status,
        )
    ).parsed
