import datetime
from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.count_issues_group_by import CountIssuesGroupBy
from ...models.issue_count import IssueCount
from ...models.problem import Problem
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    status: str | Unset = UNSET,
    type_: str | Unset = UNSET,
    assignee: str | Unset = UNSET,
    priority: int | Unset = UNSET,
    priority_min: int | Unset = UNSET,
    priority_max: int | Unset = UNSET,
    label: list[str] | Unset = UNSET,
    label_any: list[str] | Unset = UNSET,
    title: str | Unset = UNSET,
    id: str | Unset = UNSET,
    title_contains: str | Unset = UNSET,
    desc_contains: str | Unset = UNSET,
    notes_contains: str | Unset = UNSET,
    created_after: datetime.datetime | Unset = UNSET,
    created_before: datetime.datetime | Unset = UNSET,
    updated_after: datetime.datetime | Unset = UNSET,
    updated_before: datetime.datetime | Unset = UNSET,
    closed_after: datetime.datetime | Unset = UNSET,
    closed_before: datetime.datetime | Unset = UNSET,
    empty_description: bool | Unset = False,
    no_assignee: bool | Unset = False,
    no_labels: bool | Unset = False,
    include_infra: bool | Unset = False,
    group_by: CountIssuesGroupBy | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["status"] = status

    params["type"] = type_

    params["assignee"] = assignee

    params["priority"] = priority

    params["priority_min"] = priority_min

    params["priority_max"] = priority_max

    json_label: list[str] | Unset = UNSET
    if not isinstance(label, Unset):
        json_label = label

    params["label"] = json_label

    json_label_any: list[str] | Unset = UNSET
    if not isinstance(label_any, Unset):
        json_label_any = label_any

    params["label_any"] = json_label_any

    params["title"] = title

    params["id"] = id

    params["title_contains"] = title_contains

    params["desc_contains"] = desc_contains

    params["notes_contains"] = notes_contains

    json_created_after: str | Unset = UNSET
    if not isinstance(created_after, Unset):
        json_created_after = created_after.isoformat()
    params["created_after"] = json_created_after

    json_created_before: str | Unset = UNSET
    if not isinstance(created_before, Unset):
        json_created_before = created_before.isoformat()
    params["created_before"] = json_created_before

    json_updated_after: str | Unset = UNSET
    if not isinstance(updated_after, Unset):
        json_updated_after = updated_after.isoformat()
    params["updated_after"] = json_updated_after

    json_updated_before: str | Unset = UNSET
    if not isinstance(updated_before, Unset):
        json_updated_before = updated_before.isoformat()
    params["updated_before"] = json_updated_before

    json_closed_after: str | Unset = UNSET
    if not isinstance(closed_after, Unset):
        json_closed_after = closed_after.isoformat()
    params["closed_after"] = json_closed_after

    json_closed_before: str | Unset = UNSET
    if not isinstance(closed_before, Unset):
        json_closed_before = closed_before.isoformat()
    params["closed_before"] = json_closed_before

    params["empty_description"] = empty_description

    params["no_assignee"] = no_assignee

    params["no_labels"] = no_labels

    params["include_infra"] = include_infra

    json_group_by: str | Unset = UNSET
    if not isinstance(group_by, Unset):
        json_group_by = group_by.value

    params["group_by"] = json_group_by

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v0/beads/issues:count",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> IssueCount | Problem | None:
    if response.status_code == 200:
        response_200 = IssueCount.from_dict(response.json())

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
) -> Response[IssueCount | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    status: str | Unset = UNSET,
    type_: str | Unset = UNSET,
    assignee: str | Unset = UNSET,
    priority: int | Unset = UNSET,
    priority_min: int | Unset = UNSET,
    priority_max: int | Unset = UNSET,
    label: list[str] | Unset = UNSET,
    label_any: list[str] | Unset = UNSET,
    title: str | Unset = UNSET,
    id: str | Unset = UNSET,
    title_contains: str | Unset = UNSET,
    desc_contains: str | Unset = UNSET,
    notes_contains: str | Unset = UNSET,
    created_after: datetime.datetime | Unset = UNSET,
    created_before: datetime.datetime | Unset = UNSET,
    updated_after: datetime.datetime | Unset = UNSET,
    updated_before: datetime.datetime | Unset = UNSET,
    closed_after: datetime.datetime | Unset = UNSET,
    closed_before: datetime.datetime | Unset = UNSET,
    empty_description: bool | Unset = False,
    no_assignee: bool | Unset = False,
    no_labels: bool | Unset = False,
    include_infra: bool | Unset = False,
    group_by: CountIssuesGroupBy | Unset = UNSET,
) -> Response[IssueCount | Problem]:
    """Count issues matching a predicate

     How many issues match, and — with `group_by` — how many in each bucket. It is the operation behind
    `bd count`, and it answers from `issueops.Counter` over the same predicate that command builds.

    ## It is not `GET /v0/beads/issues` with the page taken off

    That is the difference to internalize before using it, because the two answer about DIFFERENT SETS
    by default. A listing hides closed, pinned, template and gate rows; a count hides NONE of them. An
    empty request here counts every durable row this workspace holds, closed rows included, and it is
    the ROLE that decides that — the same decision `bd count` has always made — not a default this
    operation applies.

    Nor is it a listing with paging removed at the type level: there is no `limit`, no `offset` and no
    `cursor`, and the role refuses the first two rather than accepting them and dropping them. A
    cardinality is a number about a set; bounding the scan would answer "how many of the first N", which
    is the shape that makes a caller believe `limit=10` bounded the answer.

    There is no free-text `q` either. The count seam takes one and both of this role's front doors have
    always passed the empty string, so it is left off rather than published untested. `title`,
    `title_contains`, `desc_contains` and `notes_contains` are the substring matches that ARE reachable.

    ## One operation, two shapes of one answer

    `group_by` selects the bucketed form. It is a parameter rather than a second operationId because the
    role is one role born with two methods, for a reason this document inherits rather than re-decides:
    the two ask the SAME predicate of the SAME set and differ only in whether the answer is one number
    or a number per bucket. The grouped response is the scalar response PLUS `groups` — the same schema,
    with one member that appears when you ask for it — so there is no second contract here to hide under
    one id.

    That is the opposite of `GET /v0/beads/events:watch`, which is a sibling operation rather than a
    mode of the paged read: those two differ in media type, lifetime, limits and capacity, and one
    operation carrying both would have documented two of everything. Here nothing differs but one
    optional response member.

    ## `total` is not the sum of `groups`

    For four of the five dimensions it happens to be. For `label` it is not, and that is why the role
    computes it rather than leaving a client to add the buckets up: LABEL BUCKETS OVERLAP. An issue
    carrying three labels is one row in `total` and one row in each of three buckets, so a client that
    summed them would report a workspace three times its size.

    The two numbers are NOT promised to describe one snapshot. The store-backed implementation runs the
    scalar and the grouped query separately, so a concurrent write between them can leave them
    disagreeing by that write. Nothing here is transactional across the two.

    ## Planes

    A count is DURABLE-PLANE ONLY unless `include_infra` is set. The wisps tier — ephemeral wisps and
    the `no_history` beads that are durable work stored in that tier — is not counted by default, and
    `include_infra` changes FOUR things at once rather than one. See that parameter; it is the one place
    on this operation where a single flag moves the set in more than one direction.

    Args:
        status (str | Unset):
        type_ (str | Unset):
        assignee (str | Unset):
        priority (int | Unset):
        priority_min (int | Unset):
        priority_max (int | Unset):
        label (list[str] | Unset):
        label_any (list[str] | Unset):
        title (str | Unset):
        id (str | Unset):
        title_contains (str | Unset):
        desc_contains (str | Unset):
        notes_contains (str | Unset):
        created_after (datetime.datetime | Unset):
        created_before (datetime.datetime | Unset):
        updated_after (datetime.datetime | Unset):
        updated_before (datetime.datetime | Unset):
        closed_after (datetime.datetime | Unset):
        closed_before (datetime.datetime | Unset):
        empty_description (bool | Unset):  Default: False.
        no_assignee (bool | Unset):  Default: False.
        no_labels (bool | Unset):  Default: False.
        include_infra (bool | Unset):  Default: False.
        group_by (CountIssuesGroupBy | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[IssueCount | Problem]
    """

    kwargs = _get_kwargs(
        status=status,
        type_=type_,
        assignee=assignee,
        priority=priority,
        priority_min=priority_min,
        priority_max=priority_max,
        label=label,
        label_any=label_any,
        title=title,
        id=id,
        title_contains=title_contains,
        desc_contains=desc_contains,
        notes_contains=notes_contains,
        created_after=created_after,
        created_before=created_before,
        updated_after=updated_after,
        updated_before=updated_before,
        closed_after=closed_after,
        closed_before=closed_before,
        empty_description=empty_description,
        no_assignee=no_assignee,
        no_labels=no_labels,
        include_infra=include_infra,
        group_by=group_by,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    status: str | Unset = UNSET,
    type_: str | Unset = UNSET,
    assignee: str | Unset = UNSET,
    priority: int | Unset = UNSET,
    priority_min: int | Unset = UNSET,
    priority_max: int | Unset = UNSET,
    label: list[str] | Unset = UNSET,
    label_any: list[str] | Unset = UNSET,
    title: str | Unset = UNSET,
    id: str | Unset = UNSET,
    title_contains: str | Unset = UNSET,
    desc_contains: str | Unset = UNSET,
    notes_contains: str | Unset = UNSET,
    created_after: datetime.datetime | Unset = UNSET,
    created_before: datetime.datetime | Unset = UNSET,
    updated_after: datetime.datetime | Unset = UNSET,
    updated_before: datetime.datetime | Unset = UNSET,
    closed_after: datetime.datetime | Unset = UNSET,
    closed_before: datetime.datetime | Unset = UNSET,
    empty_description: bool | Unset = False,
    no_assignee: bool | Unset = False,
    no_labels: bool | Unset = False,
    include_infra: bool | Unset = False,
    group_by: CountIssuesGroupBy | Unset = UNSET,
) -> IssueCount | Problem | None:
    """Count issues matching a predicate

     How many issues match, and — with `group_by` — how many in each bucket. It is the operation behind
    `bd count`, and it answers from `issueops.Counter` over the same predicate that command builds.

    ## It is not `GET /v0/beads/issues` with the page taken off

    That is the difference to internalize before using it, because the two answer about DIFFERENT SETS
    by default. A listing hides closed, pinned, template and gate rows; a count hides NONE of them. An
    empty request here counts every durable row this workspace holds, closed rows included, and it is
    the ROLE that decides that — the same decision `bd count` has always made — not a default this
    operation applies.

    Nor is it a listing with paging removed at the type level: there is no `limit`, no `offset` and no
    `cursor`, and the role refuses the first two rather than accepting them and dropping them. A
    cardinality is a number about a set; bounding the scan would answer "how many of the first N", which
    is the shape that makes a caller believe `limit=10` bounded the answer.

    There is no free-text `q` either. The count seam takes one and both of this role's front doors have
    always passed the empty string, so it is left off rather than published untested. `title`,
    `title_contains`, `desc_contains` and `notes_contains` are the substring matches that ARE reachable.

    ## One operation, two shapes of one answer

    `group_by` selects the bucketed form. It is a parameter rather than a second operationId because the
    role is one role born with two methods, for a reason this document inherits rather than re-decides:
    the two ask the SAME predicate of the SAME set and differ only in whether the answer is one number
    or a number per bucket. The grouped response is the scalar response PLUS `groups` — the same schema,
    with one member that appears when you ask for it — so there is no second contract here to hide under
    one id.

    That is the opposite of `GET /v0/beads/events:watch`, which is a sibling operation rather than a
    mode of the paged read: those two differ in media type, lifetime, limits and capacity, and one
    operation carrying both would have documented two of everything. Here nothing differs but one
    optional response member.

    ## `total` is not the sum of `groups`

    For four of the five dimensions it happens to be. For `label` it is not, and that is why the role
    computes it rather than leaving a client to add the buckets up: LABEL BUCKETS OVERLAP. An issue
    carrying three labels is one row in `total` and one row in each of three buckets, so a client that
    summed them would report a workspace three times its size.

    The two numbers are NOT promised to describe one snapshot. The store-backed implementation runs the
    scalar and the grouped query separately, so a concurrent write between them can leave them
    disagreeing by that write. Nothing here is transactional across the two.

    ## Planes

    A count is DURABLE-PLANE ONLY unless `include_infra` is set. The wisps tier — ephemeral wisps and
    the `no_history` beads that are durable work stored in that tier — is not counted by default, and
    `include_infra` changes FOUR things at once rather than one. See that parameter; it is the one place
    on this operation where a single flag moves the set in more than one direction.

    Args:
        status (str | Unset):
        type_ (str | Unset):
        assignee (str | Unset):
        priority (int | Unset):
        priority_min (int | Unset):
        priority_max (int | Unset):
        label (list[str] | Unset):
        label_any (list[str] | Unset):
        title (str | Unset):
        id (str | Unset):
        title_contains (str | Unset):
        desc_contains (str | Unset):
        notes_contains (str | Unset):
        created_after (datetime.datetime | Unset):
        created_before (datetime.datetime | Unset):
        updated_after (datetime.datetime | Unset):
        updated_before (datetime.datetime | Unset):
        closed_after (datetime.datetime | Unset):
        closed_before (datetime.datetime | Unset):
        empty_description (bool | Unset):  Default: False.
        no_assignee (bool | Unset):  Default: False.
        no_labels (bool | Unset):  Default: False.
        include_infra (bool | Unset):  Default: False.
        group_by (CountIssuesGroupBy | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        IssueCount | Problem
    """

    return sync_detailed(
        client=client,
        status=status,
        type_=type_,
        assignee=assignee,
        priority=priority,
        priority_min=priority_min,
        priority_max=priority_max,
        label=label,
        label_any=label_any,
        title=title,
        id=id,
        title_contains=title_contains,
        desc_contains=desc_contains,
        notes_contains=notes_contains,
        created_after=created_after,
        created_before=created_before,
        updated_after=updated_after,
        updated_before=updated_before,
        closed_after=closed_after,
        closed_before=closed_before,
        empty_description=empty_description,
        no_assignee=no_assignee,
        no_labels=no_labels,
        include_infra=include_infra,
        group_by=group_by,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    status: str | Unset = UNSET,
    type_: str | Unset = UNSET,
    assignee: str | Unset = UNSET,
    priority: int | Unset = UNSET,
    priority_min: int | Unset = UNSET,
    priority_max: int | Unset = UNSET,
    label: list[str] | Unset = UNSET,
    label_any: list[str] | Unset = UNSET,
    title: str | Unset = UNSET,
    id: str | Unset = UNSET,
    title_contains: str | Unset = UNSET,
    desc_contains: str | Unset = UNSET,
    notes_contains: str | Unset = UNSET,
    created_after: datetime.datetime | Unset = UNSET,
    created_before: datetime.datetime | Unset = UNSET,
    updated_after: datetime.datetime | Unset = UNSET,
    updated_before: datetime.datetime | Unset = UNSET,
    closed_after: datetime.datetime | Unset = UNSET,
    closed_before: datetime.datetime | Unset = UNSET,
    empty_description: bool | Unset = False,
    no_assignee: bool | Unset = False,
    no_labels: bool | Unset = False,
    include_infra: bool | Unset = False,
    group_by: CountIssuesGroupBy | Unset = UNSET,
) -> Response[IssueCount | Problem]:
    """Count issues matching a predicate

     How many issues match, and — with `group_by` — how many in each bucket. It is the operation behind
    `bd count`, and it answers from `issueops.Counter` over the same predicate that command builds.

    ## It is not `GET /v0/beads/issues` with the page taken off

    That is the difference to internalize before using it, because the two answer about DIFFERENT SETS
    by default. A listing hides closed, pinned, template and gate rows; a count hides NONE of them. An
    empty request here counts every durable row this workspace holds, closed rows included, and it is
    the ROLE that decides that — the same decision `bd count` has always made — not a default this
    operation applies.

    Nor is it a listing with paging removed at the type level: there is no `limit`, no `offset` and no
    `cursor`, and the role refuses the first two rather than accepting them and dropping them. A
    cardinality is a number about a set; bounding the scan would answer "how many of the first N", which
    is the shape that makes a caller believe `limit=10` bounded the answer.

    There is no free-text `q` either. The count seam takes one and both of this role's front doors have
    always passed the empty string, so it is left off rather than published untested. `title`,
    `title_contains`, `desc_contains` and `notes_contains` are the substring matches that ARE reachable.

    ## One operation, two shapes of one answer

    `group_by` selects the bucketed form. It is a parameter rather than a second operationId because the
    role is one role born with two methods, for a reason this document inherits rather than re-decides:
    the two ask the SAME predicate of the SAME set and differ only in whether the answer is one number
    or a number per bucket. The grouped response is the scalar response PLUS `groups` — the same schema,
    with one member that appears when you ask for it — so there is no second contract here to hide under
    one id.

    That is the opposite of `GET /v0/beads/events:watch`, which is a sibling operation rather than a
    mode of the paged read: those two differ in media type, lifetime, limits and capacity, and one
    operation carrying both would have documented two of everything. Here nothing differs but one
    optional response member.

    ## `total` is not the sum of `groups`

    For four of the five dimensions it happens to be. For `label` it is not, and that is why the role
    computes it rather than leaving a client to add the buckets up: LABEL BUCKETS OVERLAP. An issue
    carrying three labels is one row in `total` and one row in each of three buckets, so a client that
    summed them would report a workspace three times its size.

    The two numbers are NOT promised to describe one snapshot. The store-backed implementation runs the
    scalar and the grouped query separately, so a concurrent write between them can leave them
    disagreeing by that write. Nothing here is transactional across the two.

    ## Planes

    A count is DURABLE-PLANE ONLY unless `include_infra` is set. The wisps tier — ephemeral wisps and
    the `no_history` beads that are durable work stored in that tier — is not counted by default, and
    `include_infra` changes FOUR things at once rather than one. See that parameter; it is the one place
    on this operation where a single flag moves the set in more than one direction.

    Args:
        status (str | Unset):
        type_ (str | Unset):
        assignee (str | Unset):
        priority (int | Unset):
        priority_min (int | Unset):
        priority_max (int | Unset):
        label (list[str] | Unset):
        label_any (list[str] | Unset):
        title (str | Unset):
        id (str | Unset):
        title_contains (str | Unset):
        desc_contains (str | Unset):
        notes_contains (str | Unset):
        created_after (datetime.datetime | Unset):
        created_before (datetime.datetime | Unset):
        updated_after (datetime.datetime | Unset):
        updated_before (datetime.datetime | Unset):
        closed_after (datetime.datetime | Unset):
        closed_before (datetime.datetime | Unset):
        empty_description (bool | Unset):  Default: False.
        no_assignee (bool | Unset):  Default: False.
        no_labels (bool | Unset):  Default: False.
        include_infra (bool | Unset):  Default: False.
        group_by (CountIssuesGroupBy | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[IssueCount | Problem]
    """

    kwargs = _get_kwargs(
        status=status,
        type_=type_,
        assignee=assignee,
        priority=priority,
        priority_min=priority_min,
        priority_max=priority_max,
        label=label,
        label_any=label_any,
        title=title,
        id=id,
        title_contains=title_contains,
        desc_contains=desc_contains,
        notes_contains=notes_contains,
        created_after=created_after,
        created_before=created_before,
        updated_after=updated_after,
        updated_before=updated_before,
        closed_after=closed_after,
        closed_before=closed_before,
        empty_description=empty_description,
        no_assignee=no_assignee,
        no_labels=no_labels,
        include_infra=include_infra,
        group_by=group_by,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    status: str | Unset = UNSET,
    type_: str | Unset = UNSET,
    assignee: str | Unset = UNSET,
    priority: int | Unset = UNSET,
    priority_min: int | Unset = UNSET,
    priority_max: int | Unset = UNSET,
    label: list[str] | Unset = UNSET,
    label_any: list[str] | Unset = UNSET,
    title: str | Unset = UNSET,
    id: str | Unset = UNSET,
    title_contains: str | Unset = UNSET,
    desc_contains: str | Unset = UNSET,
    notes_contains: str | Unset = UNSET,
    created_after: datetime.datetime | Unset = UNSET,
    created_before: datetime.datetime | Unset = UNSET,
    updated_after: datetime.datetime | Unset = UNSET,
    updated_before: datetime.datetime | Unset = UNSET,
    closed_after: datetime.datetime | Unset = UNSET,
    closed_before: datetime.datetime | Unset = UNSET,
    empty_description: bool | Unset = False,
    no_assignee: bool | Unset = False,
    no_labels: bool | Unset = False,
    include_infra: bool | Unset = False,
    group_by: CountIssuesGroupBy | Unset = UNSET,
) -> IssueCount | Problem | None:
    """Count issues matching a predicate

     How many issues match, and — with `group_by` — how many in each bucket. It is the operation behind
    `bd count`, and it answers from `issueops.Counter` over the same predicate that command builds.

    ## It is not `GET /v0/beads/issues` with the page taken off

    That is the difference to internalize before using it, because the two answer about DIFFERENT SETS
    by default. A listing hides closed, pinned, template and gate rows; a count hides NONE of them. An
    empty request here counts every durable row this workspace holds, closed rows included, and it is
    the ROLE that decides that — the same decision `bd count` has always made — not a default this
    operation applies.

    Nor is it a listing with paging removed at the type level: there is no `limit`, no `offset` and no
    `cursor`, and the role refuses the first two rather than accepting them and dropping them. A
    cardinality is a number about a set; bounding the scan would answer "how many of the first N", which
    is the shape that makes a caller believe `limit=10` bounded the answer.

    There is no free-text `q` either. The count seam takes one and both of this role's front doors have
    always passed the empty string, so it is left off rather than published untested. `title`,
    `title_contains`, `desc_contains` and `notes_contains` are the substring matches that ARE reachable.

    ## One operation, two shapes of one answer

    `group_by` selects the bucketed form. It is a parameter rather than a second operationId because the
    role is one role born with two methods, for a reason this document inherits rather than re-decides:
    the two ask the SAME predicate of the SAME set and differ only in whether the answer is one number
    or a number per bucket. The grouped response is the scalar response PLUS `groups` — the same schema,
    with one member that appears when you ask for it — so there is no second contract here to hide under
    one id.

    That is the opposite of `GET /v0/beads/events:watch`, which is a sibling operation rather than a
    mode of the paged read: those two differ in media type, lifetime, limits and capacity, and one
    operation carrying both would have documented two of everything. Here nothing differs but one
    optional response member.

    ## `total` is not the sum of `groups`

    For four of the five dimensions it happens to be. For `label` it is not, and that is why the role
    computes it rather than leaving a client to add the buckets up: LABEL BUCKETS OVERLAP. An issue
    carrying three labels is one row in `total` and one row in each of three buckets, so a client that
    summed them would report a workspace three times its size.

    The two numbers are NOT promised to describe one snapshot. The store-backed implementation runs the
    scalar and the grouped query separately, so a concurrent write between them can leave them
    disagreeing by that write. Nothing here is transactional across the two.

    ## Planes

    A count is DURABLE-PLANE ONLY unless `include_infra` is set. The wisps tier — ephemeral wisps and
    the `no_history` beads that are durable work stored in that tier — is not counted by default, and
    `include_infra` changes FOUR things at once rather than one. See that parameter; it is the one place
    on this operation where a single flag moves the set in more than one direction.

    Args:
        status (str | Unset):
        type_ (str | Unset):
        assignee (str | Unset):
        priority (int | Unset):
        priority_min (int | Unset):
        priority_max (int | Unset):
        label (list[str] | Unset):
        label_any (list[str] | Unset):
        title (str | Unset):
        id (str | Unset):
        title_contains (str | Unset):
        desc_contains (str | Unset):
        notes_contains (str | Unset):
        created_after (datetime.datetime | Unset):
        created_before (datetime.datetime | Unset):
        updated_after (datetime.datetime | Unset):
        updated_before (datetime.datetime | Unset):
        closed_after (datetime.datetime | Unset):
        closed_before (datetime.datetime | Unset):
        empty_description (bool | Unset):  Default: False.
        no_assignee (bool | Unset):  Default: False.
        no_labels (bool | Unset):  Default: False.
        include_infra (bool | Unset):  Default: False.
        group_by (CountIssuesGroupBy | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        IssueCount | Problem
    """

    return (
        await asyncio_detailed(
            client=client,
            status=status,
            type_=type_,
            assignee=assignee,
            priority=priority,
            priority_min=priority_min,
            priority_max=priority_max,
            label=label,
            label_any=label_any,
            title=title,
            id=id,
            title_contains=title_contains,
            desc_contains=desc_contains,
            notes_contains=notes_contains,
            created_after=created_after,
            created_before=created_before,
            updated_after=updated_after,
            updated_before=updated_before,
            closed_after=closed_after,
            closed_before=closed_before,
            empty_description=empty_description,
            no_assignee=no_assignee,
            no_labels=no_labels,
            include_infra=include_infra,
            group_by=group_by,
        )
    ).parsed
