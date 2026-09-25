from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.create_issue_request import CreateIssueRequest
from ...models.issue import Issue
from ...models.problem import Problem
from ...types import Response


def _get_kwargs(
    *,
    body: CreateIssueRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v0/beads/issues",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Issue | Problem | None:
    if response.status_code == 200:
        response_200 = Issue.from_dict(response.json())

        return response_200

    if response.status_code == 400:
        response_400 = Problem.from_dict(response.json())

        return response_400

    if response.status_code == 401:
        response_401 = Problem.from_dict(response.json())

        return response_401

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
) -> Response[Issue | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: CreateIssueRequest,
) -> Response[Issue | Problem]:
    """Create one issue

     Creates one issue, with its parent, its explicit edges and its waits-for gate, as ONE transaction. A
    plain collection `POST` rather than a custom method, because creating one member of the collection
    the path names is what `POST` already means — the same argument `memories.remember` makes on its own
    collection.

    ## It publishes the whole create vocabulary

    Every member the role accepts and this surface publishes anywhere is here, and that is deliberate:
    `POST /v0/beads/issues:batchCreate` spells nine of them, which is what makes it unusable for a
    caller composing a real row — no `status`, no `sender`, no `metadata`, no `ephemeral`, no
    `no_history`, no `id`. This operation is the single-issue create with `ApplyCreateItem`'s
    vocabulary, and the two agree member for member except where a difference is stated below.

    THE EDGES ARE HERE, unlike on `issues:batchApply`. That operation splits edges into their own
    `dep_add` items so a plan's edge order is total; one create has no ordering to express, so
    `parent_id`, `dependencies` and `waits_for` ride on the request the way `bd create --parent`,
    `--deps` and `--waits-for` do. Every edge lands in the same transaction as the row: an edge this
    request could not write is a `400` and the issue is not created either.

    ## The explicit id

    `id` is CREATE-ONLY. An id that already names a stored row — on either plane — is a `409
    already_exists` and nothing is written. It is never an adoption and never an overwrite: `PATCH
    /v0/beads/issues/{id}` acts on a row that already exists, and `bd import` is the upsert surface,
    which this API does not publish. Absent is the ordinary case and the server mints one.

    ## What this operation deliberately cannot set

    `created_at` and `created_by` — a create whose stored creation time and author come from the caller
    makes the row's own timestamp disagree with the journal entry that records it, and re-dating history
    is what an import is for. `issues:batchApply`'s create item publishes neither either.

    `spec_id`, `await_*`, `mol_type`, `wisp_type`, `work_type`, `storage_class`, `source_*`, `pinned`,
    `is_template` and the event quartet (`event_kind`, `actor`, `target`, `payload`) — workflow and
    classification plumbing this surface publishes on no operation, read or write. `PATCH
    /v0/beads/issues/{id}` says the same of `spec_id` and `await_id`.

    The per-type `storage-class.<type>` keys in `config.yaml` are CLI-door policy that `bd create`
    resolves before it calls the role, so they do not reach any create on this surface — this operation,
    `issues:batchCreate` and `issues:batchApply` all mint the unset (versioned) class no matter what
    those keys say on the server's machine.

    `comments` and an inline `dependencies` list on the issue itself — the role refuses both, because
    edges belong to the request's own `dependencies` member where their direction can be stated.

    ## Planes

    `ephemeral` and `no_history` create the row on the EPHEMERAL plane rather than the durable one,
    exactly as they do for `issues:batchApply`. They are mutually exclusive; sending both is a `400`. An
    edge between rows on opposite planes is refused with everything else the request asked for.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (CreateIssueRequest): One issue, its parent, its explicit edges and its waits-for
            gate, created as one act.

            It is FLAT rather than nesting the issue's fields under an `issue` member, unlike
            `UpdateIssueRequest`'s `patch`: a patch has to distinguish a member that is absent from
            one set to its zero value, and a create has no such distinction to make — an absent member
            is the workspace default, which is the same answer a nested object would have given.

            The issue members mirror `ApplyCreateItem` exactly, minus that schema's two plan-only
            members (`key` and `metadata_refs`, which name items of a request this operation has only
            one of). What this adds is the edge vocabulary that operation moves into `dep_add` items:
            `parent_id`, `inherit_labels_from_parent`, `dependencies` and `waits_for`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Issue | Problem]
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
    body: CreateIssueRequest,
) -> Issue | Problem | None:
    """Create one issue

     Creates one issue, with its parent, its explicit edges and its waits-for gate, as ONE transaction. A
    plain collection `POST` rather than a custom method, because creating one member of the collection
    the path names is what `POST` already means — the same argument `memories.remember` makes on its own
    collection.

    ## It publishes the whole create vocabulary

    Every member the role accepts and this surface publishes anywhere is here, and that is deliberate:
    `POST /v0/beads/issues:batchCreate` spells nine of them, which is what makes it unusable for a
    caller composing a real row — no `status`, no `sender`, no `metadata`, no `ephemeral`, no
    `no_history`, no `id`. This operation is the single-issue create with `ApplyCreateItem`'s
    vocabulary, and the two agree member for member except where a difference is stated below.

    THE EDGES ARE HERE, unlike on `issues:batchApply`. That operation splits edges into their own
    `dep_add` items so a plan's edge order is total; one create has no ordering to express, so
    `parent_id`, `dependencies` and `waits_for` ride on the request the way `bd create --parent`,
    `--deps` and `--waits-for` do. Every edge lands in the same transaction as the row: an edge this
    request could not write is a `400` and the issue is not created either.

    ## The explicit id

    `id` is CREATE-ONLY. An id that already names a stored row — on either plane — is a `409
    already_exists` and nothing is written. It is never an adoption and never an overwrite: `PATCH
    /v0/beads/issues/{id}` acts on a row that already exists, and `bd import` is the upsert surface,
    which this API does not publish. Absent is the ordinary case and the server mints one.

    ## What this operation deliberately cannot set

    `created_at` and `created_by` — a create whose stored creation time and author come from the caller
    makes the row's own timestamp disagree with the journal entry that records it, and re-dating history
    is what an import is for. `issues:batchApply`'s create item publishes neither either.

    `spec_id`, `await_*`, `mol_type`, `wisp_type`, `work_type`, `storage_class`, `source_*`, `pinned`,
    `is_template` and the event quartet (`event_kind`, `actor`, `target`, `payload`) — workflow and
    classification plumbing this surface publishes on no operation, read or write. `PATCH
    /v0/beads/issues/{id}` says the same of `spec_id` and `await_id`.

    The per-type `storage-class.<type>` keys in `config.yaml` are CLI-door policy that `bd create`
    resolves before it calls the role, so they do not reach any create on this surface — this operation,
    `issues:batchCreate` and `issues:batchApply` all mint the unset (versioned) class no matter what
    those keys say on the server's machine.

    `comments` and an inline `dependencies` list on the issue itself — the role refuses both, because
    edges belong to the request's own `dependencies` member where their direction can be stated.

    ## Planes

    `ephemeral` and `no_history` create the row on the EPHEMERAL plane rather than the durable one,
    exactly as they do for `issues:batchApply`. They are mutually exclusive; sending both is a `400`. An
    edge between rows on opposite planes is refused with everything else the request asked for.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (CreateIssueRequest): One issue, its parent, its explicit edges and its waits-for
            gate, created as one act.

            It is FLAT rather than nesting the issue's fields under an `issue` member, unlike
            `UpdateIssueRequest`'s `patch`: a patch has to distinguish a member that is absent from
            one set to its zero value, and a create has no such distinction to make — an absent member
            is the workspace default, which is the same answer a nested object would have given.

            The issue members mirror `ApplyCreateItem` exactly, minus that schema's two plan-only
            members (`key` and `metadata_refs`, which name items of a request this operation has only
            one of). What this adds is the edge vocabulary that operation moves into `dep_add` items:
            `parent_id`, `inherit_labels_from_parent`, `dependencies` and `waits_for`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Issue | Problem
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: CreateIssueRequest,
) -> Response[Issue | Problem]:
    """Create one issue

     Creates one issue, with its parent, its explicit edges and its waits-for gate, as ONE transaction. A
    plain collection `POST` rather than a custom method, because creating one member of the collection
    the path names is what `POST` already means — the same argument `memories.remember` makes on its own
    collection.

    ## It publishes the whole create vocabulary

    Every member the role accepts and this surface publishes anywhere is here, and that is deliberate:
    `POST /v0/beads/issues:batchCreate` spells nine of them, which is what makes it unusable for a
    caller composing a real row — no `status`, no `sender`, no `metadata`, no `ephemeral`, no
    `no_history`, no `id`. This operation is the single-issue create with `ApplyCreateItem`'s
    vocabulary, and the two agree member for member except where a difference is stated below.

    THE EDGES ARE HERE, unlike on `issues:batchApply`. That operation splits edges into their own
    `dep_add` items so a plan's edge order is total; one create has no ordering to express, so
    `parent_id`, `dependencies` and `waits_for` ride on the request the way `bd create --parent`,
    `--deps` and `--waits-for` do. Every edge lands in the same transaction as the row: an edge this
    request could not write is a `400` and the issue is not created either.

    ## The explicit id

    `id` is CREATE-ONLY. An id that already names a stored row — on either plane — is a `409
    already_exists` and nothing is written. It is never an adoption and never an overwrite: `PATCH
    /v0/beads/issues/{id}` acts on a row that already exists, and `bd import` is the upsert surface,
    which this API does not publish. Absent is the ordinary case and the server mints one.

    ## What this operation deliberately cannot set

    `created_at` and `created_by` — a create whose stored creation time and author come from the caller
    makes the row's own timestamp disagree with the journal entry that records it, and re-dating history
    is what an import is for. `issues:batchApply`'s create item publishes neither either.

    `spec_id`, `await_*`, `mol_type`, `wisp_type`, `work_type`, `storage_class`, `source_*`, `pinned`,
    `is_template` and the event quartet (`event_kind`, `actor`, `target`, `payload`) — workflow and
    classification plumbing this surface publishes on no operation, read or write. `PATCH
    /v0/beads/issues/{id}` says the same of `spec_id` and `await_id`.

    The per-type `storage-class.<type>` keys in `config.yaml` are CLI-door policy that `bd create`
    resolves before it calls the role, so they do not reach any create on this surface — this operation,
    `issues:batchCreate` and `issues:batchApply` all mint the unset (versioned) class no matter what
    those keys say on the server's machine.

    `comments` and an inline `dependencies` list on the issue itself — the role refuses both, because
    edges belong to the request's own `dependencies` member where their direction can be stated.

    ## Planes

    `ephemeral` and `no_history` create the row on the EPHEMERAL plane rather than the durable one,
    exactly as they do for `issues:batchApply`. They are mutually exclusive; sending both is a `400`. An
    edge between rows on opposite planes is refused with everything else the request asked for.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (CreateIssueRequest): One issue, its parent, its explicit edges and its waits-for
            gate, created as one act.

            It is FLAT rather than nesting the issue's fields under an `issue` member, unlike
            `UpdateIssueRequest`'s `patch`: a patch has to distinguish a member that is absent from
            one set to its zero value, and a create has no such distinction to make — an absent member
            is the workspace default, which is the same answer a nested object would have given.

            The issue members mirror `ApplyCreateItem` exactly, minus that schema's two plan-only
            members (`key` and `metadata_refs`, which name items of a request this operation has only
            one of). What this adds is the edge vocabulary that operation moves into `dep_add` items:
            `parent_id`, `inherit_labels_from_parent`, `dependencies` and `waits_for`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Issue | Problem]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    body: CreateIssueRequest,
) -> Issue | Problem | None:
    """Create one issue

     Creates one issue, with its parent, its explicit edges and its waits-for gate, as ONE transaction. A
    plain collection `POST` rather than a custom method, because creating one member of the collection
    the path names is what `POST` already means — the same argument `memories.remember` makes on its own
    collection.

    ## It publishes the whole create vocabulary

    Every member the role accepts and this surface publishes anywhere is here, and that is deliberate:
    `POST /v0/beads/issues:batchCreate` spells nine of them, which is what makes it unusable for a
    caller composing a real row — no `status`, no `sender`, no `metadata`, no `ephemeral`, no
    `no_history`, no `id`. This operation is the single-issue create with `ApplyCreateItem`'s
    vocabulary, and the two agree member for member except where a difference is stated below.

    THE EDGES ARE HERE, unlike on `issues:batchApply`. That operation splits edges into their own
    `dep_add` items so a plan's edge order is total; one create has no ordering to express, so
    `parent_id`, `dependencies` and `waits_for` ride on the request the way `bd create --parent`,
    `--deps` and `--waits-for` do. Every edge lands in the same transaction as the row: an edge this
    request could not write is a `400` and the issue is not created either.

    ## The explicit id

    `id` is CREATE-ONLY. An id that already names a stored row — on either plane — is a `409
    already_exists` and nothing is written. It is never an adoption and never an overwrite: `PATCH
    /v0/beads/issues/{id}` acts on a row that already exists, and `bd import` is the upsert surface,
    which this API does not publish. Absent is the ordinary case and the server mints one.

    ## What this operation deliberately cannot set

    `created_at` and `created_by` — a create whose stored creation time and author come from the caller
    makes the row's own timestamp disagree with the journal entry that records it, and re-dating history
    is what an import is for. `issues:batchApply`'s create item publishes neither either.

    `spec_id`, `await_*`, `mol_type`, `wisp_type`, `work_type`, `storage_class`, `source_*`, `pinned`,
    `is_template` and the event quartet (`event_kind`, `actor`, `target`, `payload`) — workflow and
    classification plumbing this surface publishes on no operation, read or write. `PATCH
    /v0/beads/issues/{id}` says the same of `spec_id` and `await_id`.

    The per-type `storage-class.<type>` keys in `config.yaml` are CLI-door policy that `bd create`
    resolves before it calls the role, so they do not reach any create on this surface — this operation,
    `issues:batchCreate` and `issues:batchApply` all mint the unset (versioned) class no matter what
    those keys say on the server's machine.

    `comments` and an inline `dependencies` list on the issue itself — the role refuses both, because
    edges belong to the request's own `dependencies` member where their direction can be stated.

    ## Planes

    `ephemeral` and `no_history` create the row on the EPHEMERAL plane rather than the durable one,
    exactly as they do for `issues:batchApply`. They are mutually exclusive; sending both is a `400`. An
    edge between rows on opposite planes is refused with everything else the request asked for.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (CreateIssueRequest): One issue, its parent, its explicit edges and its waits-for
            gate, created as one act.

            It is FLAT rather than nesting the issue's fields under an `issue` member, unlike
            `UpdateIssueRequest`'s `patch`: a patch has to distinguish a member that is absent from
            one set to its zero value, and a create has no such distinction to make — an absent member
            is the workspace default, which is the same answer a nested object would have given.

            The issue members mirror `ApplyCreateItem` exactly, minus that schema's two plan-only
            members (`key` and `metadata_refs`, which name items of a request this operation has only
            one of). What this adds is the edge vocabulary that operation moves into `dep_add` items:
            `parent_id`, `inherit_labels_from_parent`, `dependencies` and `waits_for`.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Issue | Problem
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
