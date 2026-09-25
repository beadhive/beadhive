from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.list_ready_work_sort import ListReadyWorkSort
from ...models.problem import Problem
from ...models.ready_page import ReadyPage
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
    sort: ListReadyWorkSort | Unset = ListReadyWorkSort.PRIORITY,
    limit: int | Unset = 100,
    brief: bool | Unset = False,
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

    json_sort: str | Unset = UNSET
    if not isinstance(sort, Unset):
        json_sort = sort.value

    params["sort"] = json_sort

    params["limit"] = limit

    params["brief"] = brief

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v0/beads/ready",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Problem | ReadyPage | None:
    if response.status_code == 200:
        response_200 = ReadyPage.from_dict(response.json())

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
) -> Response[Problem | ReadyPage]:
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
    sort: ListReadyWorkSort | Unset = ListReadyWorkSort.PRIORITY,
    limit: int | Unset = 100,
    brief: bool | Unset = False,
) -> Response[Problem | ReadyPage]:
    """List ready work

     Unblocked, open work, ordered by the requested sort policy. Items are `IssueWithCounts` — the same
    element type `bd ready --json` emits — so dependency, dependent and comment counts are present from
    v0 onward.

    The result set is always restricted to `status=open`; there is no status parameter. There is no
    cursor either: the sort policies admit no keyset predicate, and the intended usage is snapshot-and-
    requery.

    SOME TYPES ARE EXCLUDED BY DEFAULT, exactly as `bd ready` excludes them: `merge-request`, `gate`,
    `molecule`, `rig` and the workspace's configured infrastructure types, plus anything named in
    `exclude_type`. Setting `type` DROPS THAT ENTIRE EXCLUSION SET (also matching the CLI), so
    `type=molecule` returns records the default view never shows. A client scanning the active set with
    `limit=0` and no `type` therefore never sees those classes at all.

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
        sort (ListReadyWorkSort | Unset):  Default: ListReadyWorkSort.PRIORITY.
        limit (int | Unset):  Default: 100.
        brief (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | ReadyPage]
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
        sort=sort,
        limit=limit,
        brief=brief,
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
    sort: ListReadyWorkSort | Unset = ListReadyWorkSort.PRIORITY,
    limit: int | Unset = 100,
    brief: bool | Unset = False,
) -> Problem | ReadyPage | None:
    """List ready work

     Unblocked, open work, ordered by the requested sort policy. Items are `IssueWithCounts` — the same
    element type `bd ready --json` emits — so dependency, dependent and comment counts are present from
    v0 onward.

    The result set is always restricted to `status=open`; there is no status parameter. There is no
    cursor either: the sort policies admit no keyset predicate, and the intended usage is snapshot-and-
    requery.

    SOME TYPES ARE EXCLUDED BY DEFAULT, exactly as `bd ready` excludes them: `merge-request`, `gate`,
    `molecule`, `rig` and the workspace's configured infrastructure types, plus anything named in
    `exclude_type`. Setting `type` DROPS THAT ENTIRE EXCLUSION SET (also matching the CLI), so
    `type=molecule` returns records the default view never shows. A client scanning the active set with
    `limit=0` and no `type` therefore never sees those classes at all.

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
        sort (ListReadyWorkSort | Unset):  Default: ListReadyWorkSort.PRIORITY.
        limit (int | Unset):  Default: 100.
        brief (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | ReadyPage
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
        sort=sort,
        limit=limit,
        brief=brief,
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
    sort: ListReadyWorkSort | Unset = ListReadyWorkSort.PRIORITY,
    limit: int | Unset = 100,
    brief: bool | Unset = False,
) -> Response[Problem | ReadyPage]:
    """List ready work

     Unblocked, open work, ordered by the requested sort policy. Items are `IssueWithCounts` — the same
    element type `bd ready --json` emits — so dependency, dependent and comment counts are present from
    v0 onward.

    The result set is always restricted to `status=open`; there is no status parameter. There is no
    cursor either: the sort policies admit no keyset predicate, and the intended usage is snapshot-and-
    requery.

    SOME TYPES ARE EXCLUDED BY DEFAULT, exactly as `bd ready` excludes them: `merge-request`, `gate`,
    `molecule`, `rig` and the workspace's configured infrastructure types, plus anything named in
    `exclude_type`. Setting `type` DROPS THAT ENTIRE EXCLUSION SET (also matching the CLI), so
    `type=molecule` returns records the default view never shows. A client scanning the active set with
    `limit=0` and no `type` therefore never sees those classes at all.

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
        sort (ListReadyWorkSort | Unset):  Default: ListReadyWorkSort.PRIORITY.
        limit (int | Unset):  Default: 100.
        brief (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | ReadyPage]
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
        sort=sort,
        limit=limit,
        brief=brief,
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
    sort: ListReadyWorkSort | Unset = ListReadyWorkSort.PRIORITY,
    limit: int | Unset = 100,
    brief: bool | Unset = False,
) -> Problem | ReadyPage | None:
    """List ready work

     Unblocked, open work, ordered by the requested sort policy. Items are `IssueWithCounts` — the same
    element type `bd ready --json` emits — so dependency, dependent and comment counts are present from
    v0 onward.

    The result set is always restricted to `status=open`; there is no status parameter. There is no
    cursor either: the sort policies admit no keyset predicate, and the intended usage is snapshot-and-
    requery.

    SOME TYPES ARE EXCLUDED BY DEFAULT, exactly as `bd ready` excludes them: `merge-request`, `gate`,
    `molecule`, `rig` and the workspace's configured infrastructure types, plus anything named in
    `exclude_type`. Setting `type` DROPS THAT ENTIRE EXCLUSION SET (also matching the CLI), so
    `type=molecule` returns records the default view never shows. A client scanning the active set with
    `limit=0` and no `type` therefore never sees those classes at all.

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
        sort (ListReadyWorkSort | Unset):  Default: ListReadyWorkSort.PRIORITY.
        limit (int | Unset):  Default: 100.
        brief (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | ReadyPage
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
            sort=sort,
            limit=limit,
            brief=brief,
        )
    ).parsed
