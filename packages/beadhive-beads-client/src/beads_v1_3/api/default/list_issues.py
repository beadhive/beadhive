import datetime
from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.issues_page import IssuesPage
from ...models.list_issues_sort import ListIssuesSort
from ...models.problem import Problem
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    status: list[str] | Unset = UNSET,
    type_: str | Unset = UNSET,
    assignee: str | Unset = UNSET,
    label: list[str] | Unset = UNSET,
    label_any: list[str] | Unset = UNSET,
    exclude_label: list[str] | Unset = UNSET,
    parent: str | Unset = UNSET,
    all_: bool | Unset = False,
    include_templates: bool | Unset = False,
    include_gates: bool | Unset = False,
    include_infra: bool | Unset = False,
    include_ephemeral: bool | Unset = False,
    created_before: datetime.datetime | Unset = UNSET,
    created_after: datetime.datetime | Unset = UNSET,
    metadata_field: list[str] | Unset = UNSET,
    has_metadata_key: str | Unset = UNSET,
    sort: ListIssuesSort | Unset = ListIssuesSort.LIST_ISSUES_PARAMS_SORT_CREATED,
    cursor: str | Unset = UNSET,
    limit: int | Unset = 50,
    brief: bool | Unset = False,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_status: list[str] | Unset = UNSET
    if not isinstance(status, Unset):
        json_status = status

    params["status"] = json_status

    params["type"] = type_

    params["assignee"] = assignee

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

    params["parent"] = parent

    params["all"] = all_

    params["include_templates"] = include_templates

    params["include_gates"] = include_gates

    params["include_infra"] = include_infra

    params["include_ephemeral"] = include_ephemeral

    json_created_before: str | Unset = UNSET
    if not isinstance(created_before, Unset):
        json_created_before = created_before.isoformat()
    params["created_before"] = json_created_before

    json_created_after: str | Unset = UNSET
    if not isinstance(created_after, Unset):
        json_created_after = created_after.isoformat()
    params["created_after"] = json_created_after

    json_metadata_field: list[str] | Unset = UNSET
    if not isinstance(metadata_field, Unset):
        json_metadata_field = metadata_field

    params["metadata_field"] = json_metadata_field

    params["has_metadata_key"] = has_metadata_key

    json_sort: str | Unset = UNSET
    if not isinstance(sort, Unset):
        json_sort = sort.value

    params["sort"] = json_sort

    params["cursor"] = cursor

    params["limit"] = limit

    params["brief"] = brief

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v0/beads/issues",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> IssuesPage | Problem | None:
    if response.status_code == 200:
        response_200 = IssuesPage.from_dict(response.json())

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
) -> Response[IssuesPage | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    status: list[str] | Unset = UNSET,
    type_: str | Unset = UNSET,
    assignee: str | Unset = UNSET,
    label: list[str] | Unset = UNSET,
    label_any: list[str] | Unset = UNSET,
    exclude_label: list[str] | Unset = UNSET,
    parent: str | Unset = UNSET,
    all_: bool | Unset = False,
    include_templates: bool | Unset = False,
    include_gates: bool | Unset = False,
    include_infra: bool | Unset = False,
    include_ephemeral: bool | Unset = False,
    created_before: datetime.datetime | Unset = UNSET,
    created_after: datetime.datetime | Unset = UNSET,
    metadata_field: list[str] | Unset = UNSET,
    has_metadata_key: str | Unset = UNSET,
    sort: ListIssuesSort | Unset = ListIssuesSort.LIST_ISSUES_PARAMS_SORT_CREATED,
    cursor: str | Unset = UNSET,
    limit: int | Unset = 50,
    brief: bool | Unset = False,
) -> Response[IssuesPage | Problem]:
    """List issues

     Issues under the same default exclusions `bd list` applies (closed and custom done/frozen statuses,
    templates, gates and configured infra types), each carrying dependency, dependent and comment
    counts.

    ORDERING IS NAMED BY `sort`, AND THE VOCABULARY IS CLOSED. Two orders are served: `created` —
    `(created_at DESC, id ASC)`, the default and the only order earlier revisions had — and `priority` —
    `(priority ASC, created_at DESC, id ASC)`, which is `bd list`'s flagless ordering and also what `bd
    list --sort priority` produces. Absent `sort` means `created`, permanently: it is the compatibility
    contract for every client written before this parameter existed.

    THE ORDER AND THE CURSOR ARE ONE DECISION, which is why the vocabulary is closed rather than the
    nine-value display vocabulary `bd list --sort` and `GET /v0/beads/issues:query` take. A `cursor` is
    a keyset POSITION in a total order, so each served order needs its own strictly-after predicate and
    its own proof that its key is total. Both of these have one: `priority` and `created_at` are non-
    null and `id` is unique, so a page boundary landing inside a run of equal keys resolves on `id` with
    no dropped and no repeated row. The other seven orders do not — `id` is a natural-numeric order no
    database expresses, `updated` moves on every write, `closed` is nullable, and
    `status`/`title`/`type`/`assignee` are mutable and unindexed — so they are not offered here at all
    rather than offered behind a cursor that would silently skip and duplicate.

    A CURSOR IS A POSITION IN THE ORDER IT WAS MINTED IN, and re-sending one under a different `sort` is
    refused with 400 `invalid_cursor` (recovery: restart paging). This is the one thing about a cursor
    that is NOT "just repeat your request": mismatched FILTERS are undetectable and silently resume from
    the old position, while a mismatched ORDER is detected and refused, because a position whose order
    is unknown is not interpretable at all.

    `priority` IS A MUTABLE KEY, which `created_at` is not, and a paging client should know what that
    costs. Under `created` only new rows move relative to a walk; under `priority` an `bd update
    --priority` moves an existing row too, so it can be seen twice or missed. That is the already-
    documented consequence of a cursor pinning a position rather than a snapshot, reached by updates as
    well as by creations — not a new class of error. Unchanged data never skips or repeats under either
    order.

    The item set, and each item's JSON, are identical to `bd list --json`. Under `sort=priority` so is
    the order, so a client that used to page this operation to exhaustion and re-sort client-side to
    reproduce `bd list` can now ask for one page.

    EPHEMERAL ROWS ARE OUT OF SCOPE BY DEFAULT and are admitted by `include_ephemeral`, which merges
    that tier IN ADDITION to the durable one. The merged page is ordered by the requested `sort` across
    both tiers as if they were one table, and `cursor` pages across the merge without skipping or
    repeating a row.

    `include_ephemeral` and `include_infra` are INDEPENDENT and compose: the first admits a tier, the
    second takes the infrastructure TYPE exclusions off (and admits the tier those types live in).
    Ephemeral rows of an infrastructure type therefore need `include_infra`.

    The CLI has no `--include-ephemeral` flag for `bd list` yet, so this is the one filter on this
    operation with no `bd list` spelling; `--include-infra` is the nearest one and is wider.

    THE ROWS CARRY NO `revision`, AND THE DETAIL READ DOES. A list-then-guard loop is a real shape, and
    the reason the token stops at `GET /v0/beads/issues/{id}` is not its size — measured against a
    20k-row production export the member is 28 bytes on a 1449-byte median row, under 2%. It is that
    `IssueWithCounts` is ALSO THE INTERCHANGE ROW: it is the record `bd export` writes to JSONL and the
    auto-export flushes into a git-tracked `issues.jsonl`, and the token is re-minted by every write, so
    publishing it on this element would put a per-write-random value into a file whose whole value is
    that it diffs only when something meant something. That is the loss `Issue`'s own storage field is
    withheld from generic serialization to prevent, and a wire member cannot opt out of it — the element
    here and the element there are one pinned Go struct. `IssueDetails` has neither problem: nothing
    interchanges it, and it is assembled in exactly one place, which is also why its token cannot be
    silently 0 on some path that forgot to set it. A caller that has a list and wants a guard reads the
    rows it actually intends to write, one detail read each — the read it needs anyway to decide.

    THE DOOR, IF A LATER REVISION WANTS THE TOKEN HERE: it needs a list element that is NOT the
    interchange element. The exclusion above is a consequence of the two being ONE pinned Go struct, not
    a judgement that a per-row token is unwanted, so the way in is to separate them — and nothing short
    of that will do, because any member added to this element ships in `bd export`'s JSONL by
    construction. Until then a client MUST model the absent member as ABSENT and never as `0`: zero is a
    real token here (a legacy row backfilled and not mutated since), so a client that defaulted a
    missing `revision` to 0 would compose guards that match exactly the rows it is most dangerous to be
    wrong about.

    Args:
        status (list[str] | Unset):
        type_ (str | Unset):
        assignee (str | Unset):
        label (list[str] | Unset):
        label_any (list[str] | Unset):
        exclude_label (list[str] | Unset):
        parent (str | Unset):
        all_ (bool | Unset):  Default: False.
        include_templates (bool | Unset):  Default: False.
        include_gates (bool | Unset):  Default: False.
        include_infra (bool | Unset):  Default: False.
        include_ephemeral (bool | Unset):  Default: False.
        created_before (datetime.datetime | Unset):
        created_after (datetime.datetime | Unset):
        metadata_field (list[str] | Unset):
        has_metadata_key (str | Unset):
        sort (ListIssuesSort | Unset):  Default: ListIssuesSort.LIST_ISSUES_PARAMS_SORT_CREATED.
        cursor (str | Unset):
        limit (int | Unset):  Default: 50.
        brief (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[IssuesPage | Problem]
    """

    kwargs = _get_kwargs(
        status=status,
        type_=type_,
        assignee=assignee,
        label=label,
        label_any=label_any,
        exclude_label=exclude_label,
        parent=parent,
        all_=all_,
        include_templates=include_templates,
        include_gates=include_gates,
        include_infra=include_infra,
        include_ephemeral=include_ephemeral,
        created_before=created_before,
        created_after=created_after,
        metadata_field=metadata_field,
        has_metadata_key=has_metadata_key,
        sort=sort,
        cursor=cursor,
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
    status: list[str] | Unset = UNSET,
    type_: str | Unset = UNSET,
    assignee: str | Unset = UNSET,
    label: list[str] | Unset = UNSET,
    label_any: list[str] | Unset = UNSET,
    exclude_label: list[str] | Unset = UNSET,
    parent: str | Unset = UNSET,
    all_: bool | Unset = False,
    include_templates: bool | Unset = False,
    include_gates: bool | Unset = False,
    include_infra: bool | Unset = False,
    include_ephemeral: bool | Unset = False,
    created_before: datetime.datetime | Unset = UNSET,
    created_after: datetime.datetime | Unset = UNSET,
    metadata_field: list[str] | Unset = UNSET,
    has_metadata_key: str | Unset = UNSET,
    sort: ListIssuesSort | Unset = ListIssuesSort.LIST_ISSUES_PARAMS_SORT_CREATED,
    cursor: str | Unset = UNSET,
    limit: int | Unset = 50,
    brief: bool | Unset = False,
) -> IssuesPage | Problem | None:
    """List issues

     Issues under the same default exclusions `bd list` applies (closed and custom done/frozen statuses,
    templates, gates and configured infra types), each carrying dependency, dependent and comment
    counts.

    ORDERING IS NAMED BY `sort`, AND THE VOCABULARY IS CLOSED. Two orders are served: `created` —
    `(created_at DESC, id ASC)`, the default and the only order earlier revisions had — and `priority` —
    `(priority ASC, created_at DESC, id ASC)`, which is `bd list`'s flagless ordering and also what `bd
    list --sort priority` produces. Absent `sort` means `created`, permanently: it is the compatibility
    contract for every client written before this parameter existed.

    THE ORDER AND THE CURSOR ARE ONE DECISION, which is why the vocabulary is closed rather than the
    nine-value display vocabulary `bd list --sort` and `GET /v0/beads/issues:query` take. A `cursor` is
    a keyset POSITION in a total order, so each served order needs its own strictly-after predicate and
    its own proof that its key is total. Both of these have one: `priority` and `created_at` are non-
    null and `id` is unique, so a page boundary landing inside a run of equal keys resolves on `id` with
    no dropped and no repeated row. The other seven orders do not — `id` is a natural-numeric order no
    database expresses, `updated` moves on every write, `closed` is nullable, and
    `status`/`title`/`type`/`assignee` are mutable and unindexed — so they are not offered here at all
    rather than offered behind a cursor that would silently skip and duplicate.

    A CURSOR IS A POSITION IN THE ORDER IT WAS MINTED IN, and re-sending one under a different `sort` is
    refused with 400 `invalid_cursor` (recovery: restart paging). This is the one thing about a cursor
    that is NOT "just repeat your request": mismatched FILTERS are undetectable and silently resume from
    the old position, while a mismatched ORDER is detected and refused, because a position whose order
    is unknown is not interpretable at all.

    `priority` IS A MUTABLE KEY, which `created_at` is not, and a paging client should know what that
    costs. Under `created` only new rows move relative to a walk; under `priority` an `bd update
    --priority` moves an existing row too, so it can be seen twice or missed. That is the already-
    documented consequence of a cursor pinning a position rather than a snapshot, reached by updates as
    well as by creations — not a new class of error. Unchanged data never skips or repeats under either
    order.

    The item set, and each item's JSON, are identical to `bd list --json`. Under `sort=priority` so is
    the order, so a client that used to page this operation to exhaustion and re-sort client-side to
    reproduce `bd list` can now ask for one page.

    EPHEMERAL ROWS ARE OUT OF SCOPE BY DEFAULT and are admitted by `include_ephemeral`, which merges
    that tier IN ADDITION to the durable one. The merged page is ordered by the requested `sort` across
    both tiers as if they were one table, and `cursor` pages across the merge without skipping or
    repeating a row.

    `include_ephemeral` and `include_infra` are INDEPENDENT and compose: the first admits a tier, the
    second takes the infrastructure TYPE exclusions off (and admits the tier those types live in).
    Ephemeral rows of an infrastructure type therefore need `include_infra`.

    The CLI has no `--include-ephemeral` flag for `bd list` yet, so this is the one filter on this
    operation with no `bd list` spelling; `--include-infra` is the nearest one and is wider.

    THE ROWS CARRY NO `revision`, AND THE DETAIL READ DOES. A list-then-guard loop is a real shape, and
    the reason the token stops at `GET /v0/beads/issues/{id}` is not its size — measured against a
    20k-row production export the member is 28 bytes on a 1449-byte median row, under 2%. It is that
    `IssueWithCounts` is ALSO THE INTERCHANGE ROW: it is the record `bd export` writes to JSONL and the
    auto-export flushes into a git-tracked `issues.jsonl`, and the token is re-minted by every write, so
    publishing it on this element would put a per-write-random value into a file whose whole value is
    that it diffs only when something meant something. That is the loss `Issue`'s own storage field is
    withheld from generic serialization to prevent, and a wire member cannot opt out of it — the element
    here and the element there are one pinned Go struct. `IssueDetails` has neither problem: nothing
    interchanges it, and it is assembled in exactly one place, which is also why its token cannot be
    silently 0 on some path that forgot to set it. A caller that has a list and wants a guard reads the
    rows it actually intends to write, one detail read each — the read it needs anyway to decide.

    THE DOOR, IF A LATER REVISION WANTS THE TOKEN HERE: it needs a list element that is NOT the
    interchange element. The exclusion above is a consequence of the two being ONE pinned Go struct, not
    a judgement that a per-row token is unwanted, so the way in is to separate them — and nothing short
    of that will do, because any member added to this element ships in `bd export`'s JSONL by
    construction. Until then a client MUST model the absent member as ABSENT and never as `0`: zero is a
    real token here (a legacy row backfilled and not mutated since), so a client that defaulted a
    missing `revision` to 0 would compose guards that match exactly the rows it is most dangerous to be
    wrong about.

    Args:
        status (list[str] | Unset):
        type_ (str | Unset):
        assignee (str | Unset):
        label (list[str] | Unset):
        label_any (list[str] | Unset):
        exclude_label (list[str] | Unset):
        parent (str | Unset):
        all_ (bool | Unset):  Default: False.
        include_templates (bool | Unset):  Default: False.
        include_gates (bool | Unset):  Default: False.
        include_infra (bool | Unset):  Default: False.
        include_ephemeral (bool | Unset):  Default: False.
        created_before (datetime.datetime | Unset):
        created_after (datetime.datetime | Unset):
        metadata_field (list[str] | Unset):
        has_metadata_key (str | Unset):
        sort (ListIssuesSort | Unset):  Default: ListIssuesSort.LIST_ISSUES_PARAMS_SORT_CREATED.
        cursor (str | Unset):
        limit (int | Unset):  Default: 50.
        brief (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        IssuesPage | Problem
    """

    return sync_detailed(
        client=client,
        status=status,
        type_=type_,
        assignee=assignee,
        label=label,
        label_any=label_any,
        exclude_label=exclude_label,
        parent=parent,
        all_=all_,
        include_templates=include_templates,
        include_gates=include_gates,
        include_infra=include_infra,
        include_ephemeral=include_ephemeral,
        created_before=created_before,
        created_after=created_after,
        metadata_field=metadata_field,
        has_metadata_key=has_metadata_key,
        sort=sort,
        cursor=cursor,
        limit=limit,
        brief=brief,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    status: list[str] | Unset = UNSET,
    type_: str | Unset = UNSET,
    assignee: str | Unset = UNSET,
    label: list[str] | Unset = UNSET,
    label_any: list[str] | Unset = UNSET,
    exclude_label: list[str] | Unset = UNSET,
    parent: str | Unset = UNSET,
    all_: bool | Unset = False,
    include_templates: bool | Unset = False,
    include_gates: bool | Unset = False,
    include_infra: bool | Unset = False,
    include_ephemeral: bool | Unset = False,
    created_before: datetime.datetime | Unset = UNSET,
    created_after: datetime.datetime | Unset = UNSET,
    metadata_field: list[str] | Unset = UNSET,
    has_metadata_key: str | Unset = UNSET,
    sort: ListIssuesSort | Unset = ListIssuesSort.LIST_ISSUES_PARAMS_SORT_CREATED,
    cursor: str | Unset = UNSET,
    limit: int | Unset = 50,
    brief: bool | Unset = False,
) -> Response[IssuesPage | Problem]:
    """List issues

     Issues under the same default exclusions `bd list` applies (closed and custom done/frozen statuses,
    templates, gates and configured infra types), each carrying dependency, dependent and comment
    counts.

    ORDERING IS NAMED BY `sort`, AND THE VOCABULARY IS CLOSED. Two orders are served: `created` —
    `(created_at DESC, id ASC)`, the default and the only order earlier revisions had — and `priority` —
    `(priority ASC, created_at DESC, id ASC)`, which is `bd list`'s flagless ordering and also what `bd
    list --sort priority` produces. Absent `sort` means `created`, permanently: it is the compatibility
    contract for every client written before this parameter existed.

    THE ORDER AND THE CURSOR ARE ONE DECISION, which is why the vocabulary is closed rather than the
    nine-value display vocabulary `bd list --sort` and `GET /v0/beads/issues:query` take. A `cursor` is
    a keyset POSITION in a total order, so each served order needs its own strictly-after predicate and
    its own proof that its key is total. Both of these have one: `priority` and `created_at` are non-
    null and `id` is unique, so a page boundary landing inside a run of equal keys resolves on `id` with
    no dropped and no repeated row. The other seven orders do not — `id` is a natural-numeric order no
    database expresses, `updated` moves on every write, `closed` is nullable, and
    `status`/`title`/`type`/`assignee` are mutable and unindexed — so they are not offered here at all
    rather than offered behind a cursor that would silently skip and duplicate.

    A CURSOR IS A POSITION IN THE ORDER IT WAS MINTED IN, and re-sending one under a different `sort` is
    refused with 400 `invalid_cursor` (recovery: restart paging). This is the one thing about a cursor
    that is NOT "just repeat your request": mismatched FILTERS are undetectable and silently resume from
    the old position, while a mismatched ORDER is detected and refused, because a position whose order
    is unknown is not interpretable at all.

    `priority` IS A MUTABLE KEY, which `created_at` is not, and a paging client should know what that
    costs. Under `created` only new rows move relative to a walk; under `priority` an `bd update
    --priority` moves an existing row too, so it can be seen twice or missed. That is the already-
    documented consequence of a cursor pinning a position rather than a snapshot, reached by updates as
    well as by creations — not a new class of error. Unchanged data never skips or repeats under either
    order.

    The item set, and each item's JSON, are identical to `bd list --json`. Under `sort=priority` so is
    the order, so a client that used to page this operation to exhaustion and re-sort client-side to
    reproduce `bd list` can now ask for one page.

    EPHEMERAL ROWS ARE OUT OF SCOPE BY DEFAULT and are admitted by `include_ephemeral`, which merges
    that tier IN ADDITION to the durable one. The merged page is ordered by the requested `sort` across
    both tiers as if they were one table, and `cursor` pages across the merge without skipping or
    repeating a row.

    `include_ephemeral` and `include_infra` are INDEPENDENT and compose: the first admits a tier, the
    second takes the infrastructure TYPE exclusions off (and admits the tier those types live in).
    Ephemeral rows of an infrastructure type therefore need `include_infra`.

    The CLI has no `--include-ephemeral` flag for `bd list` yet, so this is the one filter on this
    operation with no `bd list` spelling; `--include-infra` is the nearest one and is wider.

    THE ROWS CARRY NO `revision`, AND THE DETAIL READ DOES. A list-then-guard loop is a real shape, and
    the reason the token stops at `GET /v0/beads/issues/{id}` is not its size — measured against a
    20k-row production export the member is 28 bytes on a 1449-byte median row, under 2%. It is that
    `IssueWithCounts` is ALSO THE INTERCHANGE ROW: it is the record `bd export` writes to JSONL and the
    auto-export flushes into a git-tracked `issues.jsonl`, and the token is re-minted by every write, so
    publishing it on this element would put a per-write-random value into a file whose whole value is
    that it diffs only when something meant something. That is the loss `Issue`'s own storage field is
    withheld from generic serialization to prevent, and a wire member cannot opt out of it — the element
    here and the element there are one pinned Go struct. `IssueDetails` has neither problem: nothing
    interchanges it, and it is assembled in exactly one place, which is also why its token cannot be
    silently 0 on some path that forgot to set it. A caller that has a list and wants a guard reads the
    rows it actually intends to write, one detail read each — the read it needs anyway to decide.

    THE DOOR, IF A LATER REVISION WANTS THE TOKEN HERE: it needs a list element that is NOT the
    interchange element. The exclusion above is a consequence of the two being ONE pinned Go struct, not
    a judgement that a per-row token is unwanted, so the way in is to separate them — and nothing short
    of that will do, because any member added to this element ships in `bd export`'s JSONL by
    construction. Until then a client MUST model the absent member as ABSENT and never as `0`: zero is a
    real token here (a legacy row backfilled and not mutated since), so a client that defaulted a
    missing `revision` to 0 would compose guards that match exactly the rows it is most dangerous to be
    wrong about.

    Args:
        status (list[str] | Unset):
        type_ (str | Unset):
        assignee (str | Unset):
        label (list[str] | Unset):
        label_any (list[str] | Unset):
        exclude_label (list[str] | Unset):
        parent (str | Unset):
        all_ (bool | Unset):  Default: False.
        include_templates (bool | Unset):  Default: False.
        include_gates (bool | Unset):  Default: False.
        include_infra (bool | Unset):  Default: False.
        include_ephemeral (bool | Unset):  Default: False.
        created_before (datetime.datetime | Unset):
        created_after (datetime.datetime | Unset):
        metadata_field (list[str] | Unset):
        has_metadata_key (str | Unset):
        sort (ListIssuesSort | Unset):  Default: ListIssuesSort.LIST_ISSUES_PARAMS_SORT_CREATED.
        cursor (str | Unset):
        limit (int | Unset):  Default: 50.
        brief (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[IssuesPage | Problem]
    """

    kwargs = _get_kwargs(
        status=status,
        type_=type_,
        assignee=assignee,
        label=label,
        label_any=label_any,
        exclude_label=exclude_label,
        parent=parent,
        all_=all_,
        include_templates=include_templates,
        include_gates=include_gates,
        include_infra=include_infra,
        include_ephemeral=include_ephemeral,
        created_before=created_before,
        created_after=created_after,
        metadata_field=metadata_field,
        has_metadata_key=has_metadata_key,
        sort=sort,
        cursor=cursor,
        limit=limit,
        brief=brief,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    status: list[str] | Unset = UNSET,
    type_: str | Unset = UNSET,
    assignee: str | Unset = UNSET,
    label: list[str] | Unset = UNSET,
    label_any: list[str] | Unset = UNSET,
    exclude_label: list[str] | Unset = UNSET,
    parent: str | Unset = UNSET,
    all_: bool | Unset = False,
    include_templates: bool | Unset = False,
    include_gates: bool | Unset = False,
    include_infra: bool | Unset = False,
    include_ephemeral: bool | Unset = False,
    created_before: datetime.datetime | Unset = UNSET,
    created_after: datetime.datetime | Unset = UNSET,
    metadata_field: list[str] | Unset = UNSET,
    has_metadata_key: str | Unset = UNSET,
    sort: ListIssuesSort | Unset = ListIssuesSort.LIST_ISSUES_PARAMS_SORT_CREATED,
    cursor: str | Unset = UNSET,
    limit: int | Unset = 50,
    brief: bool | Unset = False,
) -> IssuesPage | Problem | None:
    """List issues

     Issues under the same default exclusions `bd list` applies (closed and custom done/frozen statuses,
    templates, gates and configured infra types), each carrying dependency, dependent and comment
    counts.

    ORDERING IS NAMED BY `sort`, AND THE VOCABULARY IS CLOSED. Two orders are served: `created` —
    `(created_at DESC, id ASC)`, the default and the only order earlier revisions had — and `priority` —
    `(priority ASC, created_at DESC, id ASC)`, which is `bd list`'s flagless ordering and also what `bd
    list --sort priority` produces. Absent `sort` means `created`, permanently: it is the compatibility
    contract for every client written before this parameter existed.

    THE ORDER AND THE CURSOR ARE ONE DECISION, which is why the vocabulary is closed rather than the
    nine-value display vocabulary `bd list --sort` and `GET /v0/beads/issues:query` take. A `cursor` is
    a keyset POSITION in a total order, so each served order needs its own strictly-after predicate and
    its own proof that its key is total. Both of these have one: `priority` and `created_at` are non-
    null and `id` is unique, so a page boundary landing inside a run of equal keys resolves on `id` with
    no dropped and no repeated row. The other seven orders do not — `id` is a natural-numeric order no
    database expresses, `updated` moves on every write, `closed` is nullable, and
    `status`/`title`/`type`/`assignee` are mutable and unindexed — so they are not offered here at all
    rather than offered behind a cursor that would silently skip and duplicate.

    A CURSOR IS A POSITION IN THE ORDER IT WAS MINTED IN, and re-sending one under a different `sort` is
    refused with 400 `invalid_cursor` (recovery: restart paging). This is the one thing about a cursor
    that is NOT "just repeat your request": mismatched FILTERS are undetectable and silently resume from
    the old position, while a mismatched ORDER is detected and refused, because a position whose order
    is unknown is not interpretable at all.

    `priority` IS A MUTABLE KEY, which `created_at` is not, and a paging client should know what that
    costs. Under `created` only new rows move relative to a walk; under `priority` an `bd update
    --priority` moves an existing row too, so it can be seen twice or missed. That is the already-
    documented consequence of a cursor pinning a position rather than a snapshot, reached by updates as
    well as by creations — not a new class of error. Unchanged data never skips or repeats under either
    order.

    The item set, and each item's JSON, are identical to `bd list --json`. Under `sort=priority` so is
    the order, so a client that used to page this operation to exhaustion and re-sort client-side to
    reproduce `bd list` can now ask for one page.

    EPHEMERAL ROWS ARE OUT OF SCOPE BY DEFAULT and are admitted by `include_ephemeral`, which merges
    that tier IN ADDITION to the durable one. The merged page is ordered by the requested `sort` across
    both tiers as if they were one table, and `cursor` pages across the merge without skipping or
    repeating a row.

    `include_ephemeral` and `include_infra` are INDEPENDENT and compose: the first admits a tier, the
    second takes the infrastructure TYPE exclusions off (and admits the tier those types live in).
    Ephemeral rows of an infrastructure type therefore need `include_infra`.

    The CLI has no `--include-ephemeral` flag for `bd list` yet, so this is the one filter on this
    operation with no `bd list` spelling; `--include-infra` is the nearest one and is wider.

    THE ROWS CARRY NO `revision`, AND THE DETAIL READ DOES. A list-then-guard loop is a real shape, and
    the reason the token stops at `GET /v0/beads/issues/{id}` is not its size — measured against a
    20k-row production export the member is 28 bytes on a 1449-byte median row, under 2%. It is that
    `IssueWithCounts` is ALSO THE INTERCHANGE ROW: it is the record `bd export` writes to JSONL and the
    auto-export flushes into a git-tracked `issues.jsonl`, and the token is re-minted by every write, so
    publishing it on this element would put a per-write-random value into a file whose whole value is
    that it diffs only when something meant something. That is the loss `Issue`'s own storage field is
    withheld from generic serialization to prevent, and a wire member cannot opt out of it — the element
    here and the element there are one pinned Go struct. `IssueDetails` has neither problem: nothing
    interchanges it, and it is assembled in exactly one place, which is also why its token cannot be
    silently 0 on some path that forgot to set it. A caller that has a list and wants a guard reads the
    rows it actually intends to write, one detail read each — the read it needs anyway to decide.

    THE DOOR, IF A LATER REVISION WANTS THE TOKEN HERE: it needs a list element that is NOT the
    interchange element. The exclusion above is a consequence of the two being ONE pinned Go struct, not
    a judgement that a per-row token is unwanted, so the way in is to separate them — and nothing short
    of that will do, because any member added to this element ships in `bd export`'s JSONL by
    construction. Until then a client MUST model the absent member as ABSENT and never as `0`: zero is a
    real token here (a legacy row backfilled and not mutated since), so a client that defaulted a
    missing `revision` to 0 would compose guards that match exactly the rows it is most dangerous to be
    wrong about.

    Args:
        status (list[str] | Unset):
        type_ (str | Unset):
        assignee (str | Unset):
        label (list[str] | Unset):
        label_any (list[str] | Unset):
        exclude_label (list[str] | Unset):
        parent (str | Unset):
        all_ (bool | Unset):  Default: False.
        include_templates (bool | Unset):  Default: False.
        include_gates (bool | Unset):  Default: False.
        include_infra (bool | Unset):  Default: False.
        include_ephemeral (bool | Unset):  Default: False.
        created_before (datetime.datetime | Unset):
        created_after (datetime.datetime | Unset):
        metadata_field (list[str] | Unset):
        has_metadata_key (str | Unset):
        sort (ListIssuesSort | Unset):  Default: ListIssuesSort.LIST_ISSUES_PARAMS_SORT_CREATED.
        cursor (str | Unset):
        limit (int | Unset):  Default: 50.
        brief (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        IssuesPage | Problem
    """

    return (
        await asyncio_detailed(
            client=client,
            status=status,
            type_=type_,
            assignee=assignee,
            label=label,
            label_any=label_any,
            exclude_label=exclude_label,
            parent=parent,
            all_=all_,
            include_templates=include_templates,
            include_gates=include_gates,
            include_infra=include_infra,
            include_ephemeral=include_ephemeral,
            created_before=created_before,
            created_after=created_after,
            metadata_field=metadata_field,
            has_metadata_key=has_metadata_key,
            sort=sort,
            cursor=cursor,
            limit=limit,
            brief=brief,
        )
    ).parsed
