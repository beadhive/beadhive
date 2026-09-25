from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.problem import Problem
from ...models.ready_count import ReadyCount
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
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
) -> dict[str, Any]:

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

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v0/beads/ready:count",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Problem | ReadyCount | None:
    if response.status_code == 200:
        response_200 = ReadyCount.from_dict(response.json())

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
) -> Response[Problem | ReadyCount]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
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
) -> Response[Problem | ReadyCount]:
    """Count ready work

     How many items `GET /v0/beads/ready` would return for the same filters with `limit=0`. That is an
    identity, not an estimate: the two operations answer from one role over one predicate
    (`issueops.ReadyCounter`), so a client may page the listing and print this number beside it — which
    is exactly what `bd ready` does when it reports "showing 100 of 412".

    THERE IS NO `limit` AND NO `sort` PARAMETER, and both absences are deliberate. A cardinality has no
    page: a limit here would answer "how many of the first N", which breaks the identity above, so the
    role refuses one rather than accepting it and dropping it. A cardinality has no order either — this
    operation counts the same set the listing returns under ANY sort policy — so there is nothing for a
    `sort` parameter to change and it is not published. Every other parameter of the listing is here and
    means exactly what it means there, including the default type exclusions and the way `type` drops
    them.

    It costs a query of its own. A client that only needs to know whether MORE work exists should read
    `has_more` on the listing instead; this operation is for the client that needs the number.

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

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | ReadyCount]
    """

    kwargs = _get_kwargs(
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
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
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
) -> Problem | ReadyCount | None:
    """Count ready work

     How many items `GET /v0/beads/ready` would return for the same filters with `limit=0`. That is an
    identity, not an estimate: the two operations answer from one role over one predicate
    (`issueops.ReadyCounter`), so a client may page the listing and print this number beside it — which
    is exactly what `bd ready` does when it reports "showing 100 of 412".

    THERE IS NO `limit` AND NO `sort` PARAMETER, and both absences are deliberate. A cardinality has no
    page: a limit here would answer "how many of the first N", which breaks the identity above, so the
    role refuses one rather than accepting it and dropping it. A cardinality has no order either — this
    operation counts the same set the listing returns under ANY sort policy — so there is nothing for a
    `sort` parameter to change and it is not published. Every other parameter of the listing is here and
    means exactly what it means there, including the default type exclusions and the way `type` drops
    them.

    It costs a query of its own. A client that only needs to know whether MORE work exists should read
    `has_more` on the listing instead; this operation is for the client that needs the number.

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

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | ReadyCount
    """

    return sync_detailed(
        client=client,
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
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
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
) -> Response[Problem | ReadyCount]:
    """Count ready work

     How many items `GET /v0/beads/ready` would return for the same filters with `limit=0`. That is an
    identity, not an estimate: the two operations answer from one role over one predicate
    (`issueops.ReadyCounter`), so a client may page the listing and print this number beside it — which
    is exactly what `bd ready` does when it reports "showing 100 of 412".

    THERE IS NO `limit` AND NO `sort` PARAMETER, and both absences are deliberate. A cardinality has no
    page: a limit here would answer "how many of the first N", which breaks the identity above, so the
    role refuses one rather than accepting it and dropping it. A cardinality has no order either — this
    operation counts the same set the listing returns under ANY sort policy — so there is nothing for a
    `sort` parameter to change and it is not published. Every other parameter of the listing is here and
    means exactly what it means there, including the default type exclusions and the way `type` drops
    them.

    It costs a query of its own. A client that only needs to know whether MORE work exists should read
    `has_more` on the listing instead; this operation is for the client that needs the number.

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

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | ReadyCount]
    """

    kwargs = _get_kwargs(
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
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
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
) -> Problem | ReadyCount | None:
    """Count ready work

     How many items `GET /v0/beads/ready` would return for the same filters with `limit=0`. That is an
    identity, not an estimate: the two operations answer from one role over one predicate
    (`issueops.ReadyCounter`), so a client may page the listing and print this number beside it — which
    is exactly what `bd ready` does when it reports "showing 100 of 412".

    THERE IS NO `limit` AND NO `sort` PARAMETER, and both absences are deliberate. A cardinality has no
    page: a limit here would answer "how many of the first N", which breaks the identity above, so the
    role refuses one rather than accepting it and dropping it. A cardinality has no order either — this
    operation counts the same set the listing returns under ANY sort policy — so there is nothing for a
    `sort` parameter to change and it is not published. Every other parameter of the listing is here and
    means exactly what it means there, including the default type exclusions and the way `type` drops
    them.

    It costs a query of its own. A client that only needs to know whether MORE work exists should read
    `has_more` on the listing instead; this operation is for the client that needs the number.

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

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | ReadyCount
    """

    return (
        await asyncio_detailed(
            client=client,
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
        )
    ).parsed
