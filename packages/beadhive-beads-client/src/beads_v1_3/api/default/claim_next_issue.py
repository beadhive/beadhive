from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.claim_next_issue_sort import ClaimNextIssueSort
from ...models.claim_next_request import ClaimNextRequest
from ...models.claim_next_response import ClaimNextResponse
from ...models.problem import Problem
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    body: ClaimNextRequest,
    assignee: str | Unset = UNSET,
    unassigned: bool | Unset = UNSET,
    type_: str | Unset = UNSET,
    exclude_type: list[str] | Unset = UNSET,
    label: list[str] | Unset = UNSET,
    label_any: list[str] | Unset = UNSET,
    exclude_label: list[str] | Unset = UNSET,
    label_pattern: str | Unset = UNSET,
    label_regex: str | Unset = UNSET,
    priority: int | Unset = UNSET,
    parent: str | Unset = UNSET,
    metadata_field: list[str] | Unset = UNSET,
    has_metadata_key: str | Unset = UNSET,
    include_ephemeral: bool | Unset = False,
    include_deferred: bool | Unset = False,
    sort: ClaimNextIssueSort | Unset = ClaimNextIssueSort.PRIORITY,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    params: dict[str, Any] = {}

    params["assignee"] = assignee

    params["unassigned"] = unassigned

    params["type"] = type_

    json_exclude_type: list[str] | Unset = UNSET
    if not isinstance(exclude_type, Unset):
        json_exclude_type = exclude_type

    params["exclude_type"] = json_exclude_type

    json_label: list[str] | Unset = UNSET
    if not isinstance(label, Unset):
        json_label = label

    params["label"] = json_label

    json_label_any: list[str] | Unset = UNSET
    if not isinstance(label_any, Unset):
        json_label_any = label_any

    params["label_any"] = json_label_any

    json_exclude_label: list[str] | Unset = UNSET
    if not isinstance(exclude_label, Unset):
        json_exclude_label = exclude_label

    params["exclude_label"] = json_exclude_label

    params["label_pattern"] = label_pattern

    params["label_regex"] = label_regex

    params["priority"] = priority

    params["parent"] = parent

    json_metadata_field: list[str] | Unset = UNSET
    if not isinstance(metadata_field, Unset):
        json_metadata_field = metadata_field

    params["metadata_field"] = json_metadata_field

    params["has_metadata_key"] = has_metadata_key

    params["include_ephemeral"] = include_ephemeral

    params["include_deferred"] = include_deferred

    json_sort: str | Unset = UNSET
    if not isinstance(sort, Unset):
        json_sort = sort.value

    params["sort"] = json_sort

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v0/beads/issues:claimNext",
        "params": params,
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ClaimNextResponse | Problem | None:
    if response.status_code == 200:
        response_200 = ClaimNextResponse.from_dict(response.json())

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
) -> Response[ClaimNextResponse | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: ClaimNextRequest,
    assignee: str | Unset = UNSET,
    unassigned: bool | Unset = UNSET,
    type_: str | Unset = UNSET,
    exclude_type: list[str] | Unset = UNSET,
    label: list[str] | Unset = UNSET,
    label_any: list[str] | Unset = UNSET,
    exclude_label: list[str] | Unset = UNSET,
    label_pattern: str | Unset = UNSET,
    label_regex: str | Unset = UNSET,
    priority: int | Unset = UNSET,
    parent: str | Unset = UNSET,
    metadata_field: list[str] | Unset = UNSET,
    has_metadata_key: str | Unset = UNSET,
    include_ephemeral: bool | Unset = False,
    include_deferred: bool | Unset = False,
    sort: ClaimNextIssueSort | Unset = ClaimNextIssueSort.PRIORITY,
) -> Response[ClaimNextResponse | Problem]:
    """Claim the next ready issue

     Takes ONE ready issue and hands it back claimed, in a single transaction. It is `bd ready --claim`,
    and it is the operation this surface's polling clients have been composing by hand out of `GET
    /v0/beads/ready` and `POST /v0/beads/issues/{id}:claim`.

    THAT COMPOSITION IS A RACE, and retiring it is why this exists. Between the listing that offered a
    row and the claim that asked for it, another agent claims it — so the second agent gets `409
    already_claimed` for a row it was correctly offered, and a fleet polling one queue spends its
    requests losing races rather than doing work. Here the ready predicate, the compare-and-set that
    wins the row, and the hydration of the row that was won are ONE transaction, so the row cannot move
    between being chosen and being reported.

    THE CALLER NAMES A QUESTION, NOT A ROW, which is what makes this a different operation rather than a
    mode of the claim. There is no id in the path and none in the body: selection is part of the
    contract.

    ## Nothing eligible is a 200

    An empty ready front is the steady state of a drained queue, not a failure, so a request that finds
    nothing answers `200` with `claimed` ABSENT. Its absence is the whole signal and there is no second
    member beside it: a polling agent branches on presence and sleeps. Nothing is written and no history
    entry is recorded.

    This is the one place this operation deliberately differs from `POST /v0/beads/issues/{id}:claim`,
    which 404s an id that names nothing — that operation was asked about a ROW, and this one was asked a
    question whose honest answer can be "none".

    ## The filters are the listing's, exactly

    Every parameter below is `GET /v0/beads/ready`'s, means what it means there, and is decoded by the
    same function — including the default type exclusions and the way `type` drops them. That is not
    tidiness: a claim that answered a different question than the listing shows would hand an agent work
    the listing never offered it, and two predicates that are allowed to differ eventually do.

    THERE IS NO `limit`, and sending one is a 400 rather than a silently dropped parameter. A claim
    delivers exactly the one row it wins no matter how large the pool it scanned, and the scan itself
    must stay UNBOUNDED: the implementation walks the ready order and continues past rows a racing agent
    already took, so a bounded window would report "nothing to claim" whenever that window happened to
    be unclaimable while plenty of other ready work remained.

    `sort` IS published, unlike on `GET /v0/beads/ready:count`, because order decides WHICH row a claim
    wins where it cannot change a cardinality.

    ## Leases, and the one case that has none

    A DURABLE win grants exactly one lease on the row it won — the handle heartbeats extend, and the row
    lease-expiry recovery walks once nothing extends it.

    An EPHEMERAL win carries NO LEASE, and that is the sharper consequence because it has no expiry to
    wait out. Ephemeral rows are outside the ready set by default; `include_ephemeral` pulls them in for
    the claim exactly as it does for the listing, and such a row IS claimable — claiming it moves the
    ephemeral row itself rather than promoting it. But heartbeats refuse an ephemeral row and lease-
    expiry recovery only walks leased durable ones, so nothing reclaims it if its claimant dies: it
    stays in progress under a gone actor until something releases or finishes it. A caller handing
    ephemeral work to agents it does not supervise owns that recovery itself. An ephemeral win records
    no durable history entry either, so a caller reconstructing who took what from history alone will
    not see it — read the row.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`.

    Args:
        assignee (str | Unset):
        unassigned (bool | Unset):
        type_ (str | Unset):
        exclude_type (list[str] | Unset):
        label (list[str] | Unset):
        label_any (list[str] | Unset):
        exclude_label (list[str] | Unset):
        label_pattern (str | Unset):
        label_regex (str | Unset):
        priority (int | Unset):
        parent (str | Unset):
        metadata_field (list[str] | Unset):
        has_metadata_key (str | Unset):
        include_ephemeral (bool | Unset):  Default: False.
        include_deferred (bool | Unset):  Default: False.
        sort (ClaimNextIssueSort | Unset):  Default: ClaimNextIssueSort.PRIORITY.
        body (ClaimNextRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ClaimNextResponse | Problem]
    """

    kwargs = _get_kwargs(
        body=body,
        assignee=assignee,
        unassigned=unassigned,
        type_=type_,
        exclude_type=exclude_type,
        label=label,
        label_any=label_any,
        exclude_label=exclude_label,
        label_pattern=label_pattern,
        label_regex=label_regex,
        priority=priority,
        parent=parent,
        metadata_field=metadata_field,
        has_metadata_key=has_metadata_key,
        include_ephemeral=include_ephemeral,
        include_deferred=include_deferred,
        sort=sort,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    body: ClaimNextRequest,
    assignee: str | Unset = UNSET,
    unassigned: bool | Unset = UNSET,
    type_: str | Unset = UNSET,
    exclude_type: list[str] | Unset = UNSET,
    label: list[str] | Unset = UNSET,
    label_any: list[str] | Unset = UNSET,
    exclude_label: list[str] | Unset = UNSET,
    label_pattern: str | Unset = UNSET,
    label_regex: str | Unset = UNSET,
    priority: int | Unset = UNSET,
    parent: str | Unset = UNSET,
    metadata_field: list[str] | Unset = UNSET,
    has_metadata_key: str | Unset = UNSET,
    include_ephemeral: bool | Unset = False,
    include_deferred: bool | Unset = False,
    sort: ClaimNextIssueSort | Unset = ClaimNextIssueSort.PRIORITY,
) -> ClaimNextResponse | Problem | None:
    """Claim the next ready issue

     Takes ONE ready issue and hands it back claimed, in a single transaction. It is `bd ready --claim`,
    and it is the operation this surface's polling clients have been composing by hand out of `GET
    /v0/beads/ready` and `POST /v0/beads/issues/{id}:claim`.

    THAT COMPOSITION IS A RACE, and retiring it is why this exists. Between the listing that offered a
    row and the claim that asked for it, another agent claims it — so the second agent gets `409
    already_claimed` for a row it was correctly offered, and a fleet polling one queue spends its
    requests losing races rather than doing work. Here the ready predicate, the compare-and-set that
    wins the row, and the hydration of the row that was won are ONE transaction, so the row cannot move
    between being chosen and being reported.

    THE CALLER NAMES A QUESTION, NOT A ROW, which is what makes this a different operation rather than a
    mode of the claim. There is no id in the path and none in the body: selection is part of the
    contract.

    ## Nothing eligible is a 200

    An empty ready front is the steady state of a drained queue, not a failure, so a request that finds
    nothing answers `200` with `claimed` ABSENT. Its absence is the whole signal and there is no second
    member beside it: a polling agent branches on presence and sleeps. Nothing is written and no history
    entry is recorded.

    This is the one place this operation deliberately differs from `POST /v0/beads/issues/{id}:claim`,
    which 404s an id that names nothing — that operation was asked about a ROW, and this one was asked a
    question whose honest answer can be "none".

    ## The filters are the listing's, exactly

    Every parameter below is `GET /v0/beads/ready`'s, means what it means there, and is decoded by the
    same function — including the default type exclusions and the way `type` drops them. That is not
    tidiness: a claim that answered a different question than the listing shows would hand an agent work
    the listing never offered it, and two predicates that are allowed to differ eventually do.

    THERE IS NO `limit`, and sending one is a 400 rather than a silently dropped parameter. A claim
    delivers exactly the one row it wins no matter how large the pool it scanned, and the scan itself
    must stay UNBOUNDED: the implementation walks the ready order and continues past rows a racing agent
    already took, so a bounded window would report "nothing to claim" whenever that window happened to
    be unclaimable while plenty of other ready work remained.

    `sort` IS published, unlike on `GET /v0/beads/ready:count`, because order decides WHICH row a claim
    wins where it cannot change a cardinality.

    ## Leases, and the one case that has none

    A DURABLE win grants exactly one lease on the row it won — the handle heartbeats extend, and the row
    lease-expiry recovery walks once nothing extends it.

    An EPHEMERAL win carries NO LEASE, and that is the sharper consequence because it has no expiry to
    wait out. Ephemeral rows are outside the ready set by default; `include_ephemeral` pulls them in for
    the claim exactly as it does for the listing, and such a row IS claimable — claiming it moves the
    ephemeral row itself rather than promoting it. But heartbeats refuse an ephemeral row and lease-
    expiry recovery only walks leased durable ones, so nothing reclaims it if its claimant dies: it
    stays in progress under a gone actor until something releases or finishes it. A caller handing
    ephemeral work to agents it does not supervise owns that recovery itself. An ephemeral win records
    no durable history entry either, so a caller reconstructing who took what from history alone will
    not see it — read the row.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`.

    Args:
        assignee (str | Unset):
        unassigned (bool | Unset):
        type_ (str | Unset):
        exclude_type (list[str] | Unset):
        label (list[str] | Unset):
        label_any (list[str] | Unset):
        exclude_label (list[str] | Unset):
        label_pattern (str | Unset):
        label_regex (str | Unset):
        priority (int | Unset):
        parent (str | Unset):
        metadata_field (list[str] | Unset):
        has_metadata_key (str | Unset):
        include_ephemeral (bool | Unset):  Default: False.
        include_deferred (bool | Unset):  Default: False.
        sort (ClaimNextIssueSort | Unset):  Default: ClaimNextIssueSort.PRIORITY.
        body (ClaimNextRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ClaimNextResponse | Problem
    """

    return sync_detailed(
        client=client,
        body=body,
        assignee=assignee,
        unassigned=unassigned,
        type_=type_,
        exclude_type=exclude_type,
        label=label,
        label_any=label_any,
        exclude_label=exclude_label,
        label_pattern=label_pattern,
        label_regex=label_regex,
        priority=priority,
        parent=parent,
        metadata_field=metadata_field,
        has_metadata_key=has_metadata_key,
        include_ephemeral=include_ephemeral,
        include_deferred=include_deferred,
        sort=sort,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: ClaimNextRequest,
    assignee: str | Unset = UNSET,
    unassigned: bool | Unset = UNSET,
    type_: str | Unset = UNSET,
    exclude_type: list[str] | Unset = UNSET,
    label: list[str] | Unset = UNSET,
    label_any: list[str] | Unset = UNSET,
    exclude_label: list[str] | Unset = UNSET,
    label_pattern: str | Unset = UNSET,
    label_regex: str | Unset = UNSET,
    priority: int | Unset = UNSET,
    parent: str | Unset = UNSET,
    metadata_field: list[str] | Unset = UNSET,
    has_metadata_key: str | Unset = UNSET,
    include_ephemeral: bool | Unset = False,
    include_deferred: bool | Unset = False,
    sort: ClaimNextIssueSort | Unset = ClaimNextIssueSort.PRIORITY,
) -> Response[ClaimNextResponse | Problem]:
    """Claim the next ready issue

     Takes ONE ready issue and hands it back claimed, in a single transaction. It is `bd ready --claim`,
    and it is the operation this surface's polling clients have been composing by hand out of `GET
    /v0/beads/ready` and `POST /v0/beads/issues/{id}:claim`.

    THAT COMPOSITION IS A RACE, and retiring it is why this exists. Between the listing that offered a
    row and the claim that asked for it, another agent claims it — so the second agent gets `409
    already_claimed` for a row it was correctly offered, and a fleet polling one queue spends its
    requests losing races rather than doing work. Here the ready predicate, the compare-and-set that
    wins the row, and the hydration of the row that was won are ONE transaction, so the row cannot move
    between being chosen and being reported.

    THE CALLER NAMES A QUESTION, NOT A ROW, which is what makes this a different operation rather than a
    mode of the claim. There is no id in the path and none in the body: selection is part of the
    contract.

    ## Nothing eligible is a 200

    An empty ready front is the steady state of a drained queue, not a failure, so a request that finds
    nothing answers `200` with `claimed` ABSENT. Its absence is the whole signal and there is no second
    member beside it: a polling agent branches on presence and sleeps. Nothing is written and no history
    entry is recorded.

    This is the one place this operation deliberately differs from `POST /v0/beads/issues/{id}:claim`,
    which 404s an id that names nothing — that operation was asked about a ROW, and this one was asked a
    question whose honest answer can be "none".

    ## The filters are the listing's, exactly

    Every parameter below is `GET /v0/beads/ready`'s, means what it means there, and is decoded by the
    same function — including the default type exclusions and the way `type` drops them. That is not
    tidiness: a claim that answered a different question than the listing shows would hand an agent work
    the listing never offered it, and two predicates that are allowed to differ eventually do.

    THERE IS NO `limit`, and sending one is a 400 rather than a silently dropped parameter. A claim
    delivers exactly the one row it wins no matter how large the pool it scanned, and the scan itself
    must stay UNBOUNDED: the implementation walks the ready order and continues past rows a racing agent
    already took, so a bounded window would report "nothing to claim" whenever that window happened to
    be unclaimable while plenty of other ready work remained.

    `sort` IS published, unlike on `GET /v0/beads/ready:count`, because order decides WHICH row a claim
    wins where it cannot change a cardinality.

    ## Leases, and the one case that has none

    A DURABLE win grants exactly one lease on the row it won — the handle heartbeats extend, and the row
    lease-expiry recovery walks once nothing extends it.

    An EPHEMERAL win carries NO LEASE, and that is the sharper consequence because it has no expiry to
    wait out. Ephemeral rows are outside the ready set by default; `include_ephemeral` pulls them in for
    the claim exactly as it does for the listing, and such a row IS claimable — claiming it moves the
    ephemeral row itself rather than promoting it. But heartbeats refuse an ephemeral row and lease-
    expiry recovery only walks leased durable ones, so nothing reclaims it if its claimant dies: it
    stays in progress under a gone actor until something releases or finishes it. A caller handing
    ephemeral work to agents it does not supervise owns that recovery itself. An ephemeral win records
    no durable history entry either, so a caller reconstructing who took what from history alone will
    not see it — read the row.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`.

    Args:
        assignee (str | Unset):
        unassigned (bool | Unset):
        type_ (str | Unset):
        exclude_type (list[str] | Unset):
        label (list[str] | Unset):
        label_any (list[str] | Unset):
        exclude_label (list[str] | Unset):
        label_pattern (str | Unset):
        label_regex (str | Unset):
        priority (int | Unset):
        parent (str | Unset):
        metadata_field (list[str] | Unset):
        has_metadata_key (str | Unset):
        include_ephemeral (bool | Unset):  Default: False.
        include_deferred (bool | Unset):  Default: False.
        sort (ClaimNextIssueSort | Unset):  Default: ClaimNextIssueSort.PRIORITY.
        body (ClaimNextRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ClaimNextResponse | Problem]
    """

    kwargs = _get_kwargs(
        body=body,
        assignee=assignee,
        unassigned=unassigned,
        type_=type_,
        exclude_type=exclude_type,
        label=label,
        label_any=label_any,
        exclude_label=exclude_label,
        label_pattern=label_pattern,
        label_regex=label_regex,
        priority=priority,
        parent=parent,
        metadata_field=metadata_field,
        has_metadata_key=has_metadata_key,
        include_ephemeral=include_ephemeral,
        include_deferred=include_deferred,
        sort=sort,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    body: ClaimNextRequest,
    assignee: str | Unset = UNSET,
    unassigned: bool | Unset = UNSET,
    type_: str | Unset = UNSET,
    exclude_type: list[str] | Unset = UNSET,
    label: list[str] | Unset = UNSET,
    label_any: list[str] | Unset = UNSET,
    exclude_label: list[str] | Unset = UNSET,
    label_pattern: str | Unset = UNSET,
    label_regex: str | Unset = UNSET,
    priority: int | Unset = UNSET,
    parent: str | Unset = UNSET,
    metadata_field: list[str] | Unset = UNSET,
    has_metadata_key: str | Unset = UNSET,
    include_ephemeral: bool | Unset = False,
    include_deferred: bool | Unset = False,
    sort: ClaimNextIssueSort | Unset = ClaimNextIssueSort.PRIORITY,
) -> ClaimNextResponse | Problem | None:
    """Claim the next ready issue

     Takes ONE ready issue and hands it back claimed, in a single transaction. It is `bd ready --claim`,
    and it is the operation this surface's polling clients have been composing by hand out of `GET
    /v0/beads/ready` and `POST /v0/beads/issues/{id}:claim`.

    THAT COMPOSITION IS A RACE, and retiring it is why this exists. Between the listing that offered a
    row and the claim that asked for it, another agent claims it — so the second agent gets `409
    already_claimed` for a row it was correctly offered, and a fleet polling one queue spends its
    requests losing races rather than doing work. Here the ready predicate, the compare-and-set that
    wins the row, and the hydration of the row that was won are ONE transaction, so the row cannot move
    between being chosen and being reported.

    THE CALLER NAMES A QUESTION, NOT A ROW, which is what makes this a different operation rather than a
    mode of the claim. There is no id in the path and none in the body: selection is part of the
    contract.

    ## Nothing eligible is a 200

    An empty ready front is the steady state of a drained queue, not a failure, so a request that finds
    nothing answers `200` with `claimed` ABSENT. Its absence is the whole signal and there is no second
    member beside it: a polling agent branches on presence and sleeps. Nothing is written and no history
    entry is recorded.

    This is the one place this operation deliberately differs from `POST /v0/beads/issues/{id}:claim`,
    which 404s an id that names nothing — that operation was asked about a ROW, and this one was asked a
    question whose honest answer can be "none".

    ## The filters are the listing's, exactly

    Every parameter below is `GET /v0/beads/ready`'s, means what it means there, and is decoded by the
    same function — including the default type exclusions and the way `type` drops them. That is not
    tidiness: a claim that answered a different question than the listing shows would hand an agent work
    the listing never offered it, and two predicates that are allowed to differ eventually do.

    THERE IS NO `limit`, and sending one is a 400 rather than a silently dropped parameter. A claim
    delivers exactly the one row it wins no matter how large the pool it scanned, and the scan itself
    must stay UNBOUNDED: the implementation walks the ready order and continues past rows a racing agent
    already took, so a bounded window would report "nothing to claim" whenever that window happened to
    be unclaimable while plenty of other ready work remained.

    `sort` IS published, unlike on `GET /v0/beads/ready:count`, because order decides WHICH row a claim
    wins where it cannot change a cardinality.

    ## Leases, and the one case that has none

    A DURABLE win grants exactly one lease on the row it won — the handle heartbeats extend, and the row
    lease-expiry recovery walks once nothing extends it.

    An EPHEMERAL win carries NO LEASE, and that is the sharper consequence because it has no expiry to
    wait out. Ephemeral rows are outside the ready set by default; `include_ephemeral` pulls them in for
    the claim exactly as it does for the listing, and such a row IS claimable — claiming it moves the
    ephemeral row itself rather than promoting it. But heartbeats refuse an ephemeral row and lease-
    expiry recovery only walks leased durable ones, so nothing reclaims it if its claimant dies: it
    stays in progress under a gone actor until something releases or finishes it. A caller handing
    ephemeral work to agents it does not supervise owns that recovery itself. An ephemeral win records
    no durable history entry either, so a caller reconstructing who took what from history alone will
    not see it — read the row.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`.

    Args:
        assignee (str | Unset):
        unassigned (bool | Unset):
        type_ (str | Unset):
        exclude_type (list[str] | Unset):
        label (list[str] | Unset):
        label_any (list[str] | Unset):
        exclude_label (list[str] | Unset):
        label_pattern (str | Unset):
        label_regex (str | Unset):
        priority (int | Unset):
        parent (str | Unset):
        metadata_field (list[str] | Unset):
        has_metadata_key (str | Unset):
        include_ephemeral (bool | Unset):  Default: False.
        include_deferred (bool | Unset):  Default: False.
        sort (ClaimNextIssueSort | Unset):  Default: ClaimNextIssueSort.PRIORITY.
        body (ClaimNextRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ClaimNextResponse | Problem
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
            assignee=assignee,
            unassigned=unassigned,
            type_=type_,
            exclude_type=exclude_type,
            label=label,
            label_any=label_any,
            exclude_label=exclude_label,
            label_pattern=label_pattern,
            label_regex=label_regex,
            priority=priority,
            parent=parent,
            metadata_field=metadata_field,
            has_metadata_key=has_metadata_key,
            include_ephemeral=include_ephemeral,
            include_deferred=include_deferred,
            sort=sort,
        )
    ).parsed
