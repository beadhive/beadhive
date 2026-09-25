from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.list_related_issues_direction import ListRelatedIssuesDirection
from ...models.problem import Problem
from ...models.related_issues import RelatedIssues
from ...types import UNSET, Response, Unset


def _get_kwargs(
    id: str,
    *,
    direction: ListRelatedIssuesDirection,
    type_: list[str] | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_direction = direction.value
    params["direction"] = json_direction

    json_type_: list[str] | Unset = UNSET
    if not isinstance(type_, Unset):
        json_type_ = type_

    params["type"] = json_type_

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v0/beads/issues/{id}/related".format(
            id=quote(str(id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Problem | RelatedIssues | None:
    if response.status_code == 200:
        response_200 = RelatedIssues.from_dict(response.json())

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
) -> Response[Problem | RelatedIssues]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    id: str,
    *,
    client: AuthenticatedClient,
    direction: ListRelatedIssuesDirection,
    type_: list[str] | Unset = UNSET,
) -> Response[Problem | RelatedIssues]:
    """List one issue's neighbors in a named direction

     THE ISSUES ON THE FAR END of this issue's edges, in the direction the request names, each carrying
    the type of the edge that led to it. It is the read behind `bd dep list`'s neighbor view, and its
    elements are the same `IssueWithDependencyMetadata` the `dependencies` and `dependents` members of
    `GET /v0/beads/issues/{id}` already carry.

    IT IS A SUB-RESOURCE OF THE ISSUE, not a member of the dependency collection, and the two answer
    different things about the same edges. `GET /v0/beads/dependencies` returns the stored edge ROWS —
    targets spelled exactly as stored, nothing looked up — for many anchors at once; this one is
    anchored on ONE issue and answers with HYDRATED ISSUES, so an edge whose far end this database holds
    no row for is not a neighbor at all. `issueops.Relations` and `issueops.EdgeReader` state that split
    once; nothing is restated here.

    THE CONSEQUENCE IS WORTH READING BEFORE COUNTING ANYTHING. A dependency target may be an `external:`
    reference or an id belonging to another repository, and this database holds no issue for either.
    Such an edge is left out with no placeholder row and no error, so the length of `items` is this
    issue's NEIGHBOR count and not its EDGE count, and the two differ by however many of its edges point
    outside this database. A caller that needs the edges themselves reads `GET /v0/beads/dependencies`;
    a caller that needs the number reads `GET /v0/beads/dependencies:count`, which counts edges and not
    neighbors.

    AN ID THAT NAMES NEITHER AN ISSUE NOR A WISP IS A 404, and that is the difference from every other
    graph read on this surface. Those are batched and report a miss per anchor, because failing the call
    would throw away the answers for the ids that were found; here there is one anchor and no other
    answer to preserve. It matters because an empty `items` is the COMMON case — most issues have
    neighbors in only one direction — so a typo answered with an empty list would never surface.

    BOTH PLANES ARE READ, on the anchor and on its neighbors. The anchor is resolved against the durable
    and ephemeral planes together, so a wisp id is a legal anchor; and the neighbors are collected from
    BOTH dependency tables and hydrated from both issue tables, so a durable issue's `in` neighbors
    include the wisps that depend on it and a `direction=out` answer includes the wisp targets it
    depends on. Those are the ROLE's rules, stated at `issueops.Relations`, and this document cites them
    rather than restating them.

    THE ORDER IS PINNED: ascending by the neighbor's id, with the edge type breaking a tie. It is pinned
    rather than left to the query because the rows come from two dependency tables read in sequence, so
    their natural order is an artifact of which plane a neighbor happens to live on — stable enough to
    look deliberate and not stable enough to rely on.

    THE ROWS CARRY NO `revision`, on `GET /v0/beads/issues`'s terms. The element here is the pinned Go
    struct `GET /v0/beads/issues/{id}` carries under `dependencies` and `dependents`, so it publishes an
    issue's own serialized fields and nothing this operation invents; the optimistic-concurrency token
    stays on the detail read, which is where a guarded write composes its `expected_version` from. A
    client holding a neighbor list and wanting a guard reads the rows it actually intends to write, one
    detail read each.

    THERE IS NO `limit` AND NO CURSOR. One issue's neighbors are unbounded here exactly as the
    `dependencies` member of `GET /v0/beads/issues/{id}` already is, and this operation names ONE
    anchor, so there is no question to bound instead — which is what the dependency-collection reads
    bound at 100 `issue_id` values apiece. A limit with no cursor behind it would truncate with no way
    to fetch the rest.

    THERE IS NO `both` DIRECTION, for the reason `GET /v0/beads/dependencies:count` has none: a caller
    that wants the pair asks twice, and one call answering both would have to say which direction each
    row came from — which is a second member on an element this document deliberately shares with the
    detail read.

    Args:
        id (str):
        direction (ListRelatedIssuesDirection):
        type_ (list[str] | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | RelatedIssues]
    """

    kwargs = _get_kwargs(
        id=id,
        direction=direction,
        type_=type_,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    id: str,
    *,
    client: AuthenticatedClient,
    direction: ListRelatedIssuesDirection,
    type_: list[str] | Unset = UNSET,
) -> Problem | RelatedIssues | None:
    """List one issue's neighbors in a named direction

     THE ISSUES ON THE FAR END of this issue's edges, in the direction the request names, each carrying
    the type of the edge that led to it. It is the read behind `bd dep list`'s neighbor view, and its
    elements are the same `IssueWithDependencyMetadata` the `dependencies` and `dependents` members of
    `GET /v0/beads/issues/{id}` already carry.

    IT IS A SUB-RESOURCE OF THE ISSUE, not a member of the dependency collection, and the two answer
    different things about the same edges. `GET /v0/beads/dependencies` returns the stored edge ROWS —
    targets spelled exactly as stored, nothing looked up — for many anchors at once; this one is
    anchored on ONE issue and answers with HYDRATED ISSUES, so an edge whose far end this database holds
    no row for is not a neighbor at all. `issueops.Relations` and `issueops.EdgeReader` state that split
    once; nothing is restated here.

    THE CONSEQUENCE IS WORTH READING BEFORE COUNTING ANYTHING. A dependency target may be an `external:`
    reference or an id belonging to another repository, and this database holds no issue for either.
    Such an edge is left out with no placeholder row and no error, so the length of `items` is this
    issue's NEIGHBOR count and not its EDGE count, and the two differ by however many of its edges point
    outside this database. A caller that needs the edges themselves reads `GET /v0/beads/dependencies`;
    a caller that needs the number reads `GET /v0/beads/dependencies:count`, which counts edges and not
    neighbors.

    AN ID THAT NAMES NEITHER AN ISSUE NOR A WISP IS A 404, and that is the difference from every other
    graph read on this surface. Those are batched and report a miss per anchor, because failing the call
    would throw away the answers for the ids that were found; here there is one anchor and no other
    answer to preserve. It matters because an empty `items` is the COMMON case — most issues have
    neighbors in only one direction — so a typo answered with an empty list would never surface.

    BOTH PLANES ARE READ, on the anchor and on its neighbors. The anchor is resolved against the durable
    and ephemeral planes together, so a wisp id is a legal anchor; and the neighbors are collected from
    BOTH dependency tables and hydrated from both issue tables, so a durable issue's `in` neighbors
    include the wisps that depend on it and a `direction=out` answer includes the wisp targets it
    depends on. Those are the ROLE's rules, stated at `issueops.Relations`, and this document cites them
    rather than restating them.

    THE ORDER IS PINNED: ascending by the neighbor's id, with the edge type breaking a tie. It is pinned
    rather than left to the query because the rows come from two dependency tables read in sequence, so
    their natural order is an artifact of which plane a neighbor happens to live on — stable enough to
    look deliberate and not stable enough to rely on.

    THE ROWS CARRY NO `revision`, on `GET /v0/beads/issues`'s terms. The element here is the pinned Go
    struct `GET /v0/beads/issues/{id}` carries under `dependencies` and `dependents`, so it publishes an
    issue's own serialized fields and nothing this operation invents; the optimistic-concurrency token
    stays on the detail read, which is where a guarded write composes its `expected_version` from. A
    client holding a neighbor list and wanting a guard reads the rows it actually intends to write, one
    detail read each.

    THERE IS NO `limit` AND NO CURSOR. One issue's neighbors are unbounded here exactly as the
    `dependencies` member of `GET /v0/beads/issues/{id}` already is, and this operation names ONE
    anchor, so there is no question to bound instead — which is what the dependency-collection reads
    bound at 100 `issue_id` values apiece. A limit with no cursor behind it would truncate with no way
    to fetch the rest.

    THERE IS NO `both` DIRECTION, for the reason `GET /v0/beads/dependencies:count` has none: a caller
    that wants the pair asks twice, and one call answering both would have to say which direction each
    row came from — which is a second member on an element this document deliberately shares with the
    detail read.

    Args:
        id (str):
        direction (ListRelatedIssuesDirection):
        type_ (list[str] | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | RelatedIssues
    """

    return sync_detailed(
        id=id,
        client=client,
        direction=direction,
        type_=type_,
    ).parsed


async def asyncio_detailed(
    id: str,
    *,
    client: AuthenticatedClient,
    direction: ListRelatedIssuesDirection,
    type_: list[str] | Unset = UNSET,
) -> Response[Problem | RelatedIssues]:
    """List one issue's neighbors in a named direction

     THE ISSUES ON THE FAR END of this issue's edges, in the direction the request names, each carrying
    the type of the edge that led to it. It is the read behind `bd dep list`'s neighbor view, and its
    elements are the same `IssueWithDependencyMetadata` the `dependencies` and `dependents` members of
    `GET /v0/beads/issues/{id}` already carry.

    IT IS A SUB-RESOURCE OF THE ISSUE, not a member of the dependency collection, and the two answer
    different things about the same edges. `GET /v0/beads/dependencies` returns the stored edge ROWS —
    targets spelled exactly as stored, nothing looked up — for many anchors at once; this one is
    anchored on ONE issue and answers with HYDRATED ISSUES, so an edge whose far end this database holds
    no row for is not a neighbor at all. `issueops.Relations` and `issueops.EdgeReader` state that split
    once; nothing is restated here.

    THE CONSEQUENCE IS WORTH READING BEFORE COUNTING ANYTHING. A dependency target may be an `external:`
    reference or an id belonging to another repository, and this database holds no issue for either.
    Such an edge is left out with no placeholder row and no error, so the length of `items` is this
    issue's NEIGHBOR count and not its EDGE count, and the two differ by however many of its edges point
    outside this database. A caller that needs the edges themselves reads `GET /v0/beads/dependencies`;
    a caller that needs the number reads `GET /v0/beads/dependencies:count`, which counts edges and not
    neighbors.

    AN ID THAT NAMES NEITHER AN ISSUE NOR A WISP IS A 404, and that is the difference from every other
    graph read on this surface. Those are batched and report a miss per anchor, because failing the call
    would throw away the answers for the ids that were found; here there is one anchor and no other
    answer to preserve. It matters because an empty `items` is the COMMON case — most issues have
    neighbors in only one direction — so a typo answered with an empty list would never surface.

    BOTH PLANES ARE READ, on the anchor and on its neighbors. The anchor is resolved against the durable
    and ephemeral planes together, so a wisp id is a legal anchor; and the neighbors are collected from
    BOTH dependency tables and hydrated from both issue tables, so a durable issue's `in` neighbors
    include the wisps that depend on it and a `direction=out` answer includes the wisp targets it
    depends on. Those are the ROLE's rules, stated at `issueops.Relations`, and this document cites them
    rather than restating them.

    THE ORDER IS PINNED: ascending by the neighbor's id, with the edge type breaking a tie. It is pinned
    rather than left to the query because the rows come from two dependency tables read in sequence, so
    their natural order is an artifact of which plane a neighbor happens to live on — stable enough to
    look deliberate and not stable enough to rely on.

    THE ROWS CARRY NO `revision`, on `GET /v0/beads/issues`'s terms. The element here is the pinned Go
    struct `GET /v0/beads/issues/{id}` carries under `dependencies` and `dependents`, so it publishes an
    issue's own serialized fields and nothing this operation invents; the optimistic-concurrency token
    stays on the detail read, which is where a guarded write composes its `expected_version` from. A
    client holding a neighbor list and wanting a guard reads the rows it actually intends to write, one
    detail read each.

    THERE IS NO `limit` AND NO CURSOR. One issue's neighbors are unbounded here exactly as the
    `dependencies` member of `GET /v0/beads/issues/{id}` already is, and this operation names ONE
    anchor, so there is no question to bound instead — which is what the dependency-collection reads
    bound at 100 `issue_id` values apiece. A limit with no cursor behind it would truncate with no way
    to fetch the rest.

    THERE IS NO `both` DIRECTION, for the reason `GET /v0/beads/dependencies:count` has none: a caller
    that wants the pair asks twice, and one call answering both would have to say which direction each
    row came from — which is a second member on an element this document deliberately shares with the
    detail read.

    Args:
        id (str):
        direction (ListRelatedIssuesDirection):
        type_ (list[str] | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | RelatedIssues]
    """

    kwargs = _get_kwargs(
        id=id,
        direction=direction,
        type_=type_,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    id: str,
    *,
    client: AuthenticatedClient,
    direction: ListRelatedIssuesDirection,
    type_: list[str] | Unset = UNSET,
) -> Problem | RelatedIssues | None:
    """List one issue's neighbors in a named direction

     THE ISSUES ON THE FAR END of this issue's edges, in the direction the request names, each carrying
    the type of the edge that led to it. It is the read behind `bd dep list`'s neighbor view, and its
    elements are the same `IssueWithDependencyMetadata` the `dependencies` and `dependents` members of
    `GET /v0/beads/issues/{id}` already carry.

    IT IS A SUB-RESOURCE OF THE ISSUE, not a member of the dependency collection, and the two answer
    different things about the same edges. `GET /v0/beads/dependencies` returns the stored edge ROWS —
    targets spelled exactly as stored, nothing looked up — for many anchors at once; this one is
    anchored on ONE issue and answers with HYDRATED ISSUES, so an edge whose far end this database holds
    no row for is not a neighbor at all. `issueops.Relations` and `issueops.EdgeReader` state that split
    once; nothing is restated here.

    THE CONSEQUENCE IS WORTH READING BEFORE COUNTING ANYTHING. A dependency target may be an `external:`
    reference or an id belonging to another repository, and this database holds no issue for either.
    Such an edge is left out with no placeholder row and no error, so the length of `items` is this
    issue's NEIGHBOR count and not its EDGE count, and the two differ by however many of its edges point
    outside this database. A caller that needs the edges themselves reads `GET /v0/beads/dependencies`;
    a caller that needs the number reads `GET /v0/beads/dependencies:count`, which counts edges and not
    neighbors.

    AN ID THAT NAMES NEITHER AN ISSUE NOR A WISP IS A 404, and that is the difference from every other
    graph read on this surface. Those are batched and report a miss per anchor, because failing the call
    would throw away the answers for the ids that were found; here there is one anchor and no other
    answer to preserve. It matters because an empty `items` is the COMMON case — most issues have
    neighbors in only one direction — so a typo answered with an empty list would never surface.

    BOTH PLANES ARE READ, on the anchor and on its neighbors. The anchor is resolved against the durable
    and ephemeral planes together, so a wisp id is a legal anchor; and the neighbors are collected from
    BOTH dependency tables and hydrated from both issue tables, so a durable issue's `in` neighbors
    include the wisps that depend on it and a `direction=out` answer includes the wisp targets it
    depends on. Those are the ROLE's rules, stated at `issueops.Relations`, and this document cites them
    rather than restating them.

    THE ORDER IS PINNED: ascending by the neighbor's id, with the edge type breaking a tie. It is pinned
    rather than left to the query because the rows come from two dependency tables read in sequence, so
    their natural order is an artifact of which plane a neighbor happens to live on — stable enough to
    look deliberate and not stable enough to rely on.

    THE ROWS CARRY NO `revision`, on `GET /v0/beads/issues`'s terms. The element here is the pinned Go
    struct `GET /v0/beads/issues/{id}` carries under `dependencies` and `dependents`, so it publishes an
    issue's own serialized fields and nothing this operation invents; the optimistic-concurrency token
    stays on the detail read, which is where a guarded write composes its `expected_version` from. A
    client holding a neighbor list and wanting a guard reads the rows it actually intends to write, one
    detail read each.

    THERE IS NO `limit` AND NO CURSOR. One issue's neighbors are unbounded here exactly as the
    `dependencies` member of `GET /v0/beads/issues/{id}` already is, and this operation names ONE
    anchor, so there is no question to bound instead — which is what the dependency-collection reads
    bound at 100 `issue_id` values apiece. A limit with no cursor behind it would truncate with no way
    to fetch the rest.

    THERE IS NO `both` DIRECTION, for the reason `GET /v0/beads/dependencies:count` has none: a caller
    that wants the pair asks twice, and one call answering both would have to say which direction each
    row came from — which is a second member on an element this document deliberately shares with the
    detail read.

    Args:
        id (str):
        direction (ListRelatedIssuesDirection):
        type_ (list[str] | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | RelatedIssues
    """

    return (
        await asyncio_detailed(
            id=id,
            client=client,
            direction=direction,
            type_=type_,
        )
    ).parsed
