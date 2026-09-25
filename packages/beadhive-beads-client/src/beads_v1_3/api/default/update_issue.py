from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.problem import Problem
from ...models.update_issue_request import UpdateIssueRequest
from ...models.update_issue_response import UpdateIssueResponse
from ...types import Response


def _get_kwargs(
    id: str,
    *,
    body: UpdateIssueRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "patch",
        "url": "/v0/beads/issues/{id}".format(
            id=quote(str(id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Problem | UpdateIssueResponse | None:
    if response.status_code == 200:
        response_200 = UpdateIssueResponse.from_dict(response.json())

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
) -> Response[Problem | UpdateIssueResponse]:
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
    body: UpdateIssueRequest,
) -> Response[Problem | UpdateIssueResponse]:
    """Edit the fields of one issue

     Partial update of one named issue. A plain `PATCH` rather than a custom method, because partial
    update of one named resource is what `PATCH` already means — exactly as one named resource with no
    body is what `DELETE` means for `forgetMemory`. The custom-method namespace is left for the
    operations that are not CRUD.

    ## Member presence is the signal

    A member PRESENT in `patch` is written; a member ABSENT is left untouched. That is the whole
    partial-update rule, and it is why an absent member and a member set to its current value are
    different requests with the same outcome.

    Explicit `null` is defined per member. On the four NULLABLE members — `estimated_minutes`,
    `external_ref`, `due_at`, `defer_until` — it CLEARS the value, because `null` is the only wire
    spelling a clear has. On every other member it is a `400` naming the member, so a null is never
    quietly recorded as an empty string.

    An EMPTY `patch` object is a `400`: a write that writes nothing is a client bug, the same judgement
    `issues:batchCreate` makes about an empty `items`.

    `labels` is COMPLETE REPLACEMENT. It is the only shape whose result the client already knows without
    a read-back; an additive `labels_add`/`labels_remove` pair can join the vocabulary later without
    disturbing it.

    `notes` and `append_notes` are mutually exclusive; sending both is a `400`.

    ## Guards, forces and the conflicts they answer

    `expected_version`, `expected_status` and `expected_assignee` are compare-and-set preconditions,
    checked before the patch. A miss refuses the WHOLE request with `409 precondition_failed` and writes
    nothing, so recovery is to re-read and recompose rather than to retry the same body. They are
    `ApplyUpdateItem`'s three members with `ApplyUpdateItem`'s contract; `expected_version`'s token is
    the `revision` this operation answers with.

    `force_close_policy` and `force_assignee_transfer` bypass exactly one refusal each and nothing else.
    Neither bypasses validation, the preconditions above, or the other's guard.

    Three members carry policy, which is where this operation's `409`s come from. `status` crossing into
    the workspace's done category answers to close policy (`not_closable`). `assignee` transferring away
    from a live foreign in-progress owner answers to the assignee fence (`already_claimed`). `parent_id`
    is a graph edit and answers to the graph (`dependency_cycle`, `dependency_exists`). Each is the SAME
    refusal `issues:batchApply` and `dependencies:add` already publish, not a second vocabulary.

    ## What this operation deliberately cannot edit

    `owner` — additive later, and there is no argument against it any more: the one it had was
    `assignee`'s, which this operation now publishes. Nothing has asked for it.

    `persistence` — a plane move is an atomic aggregate migration rather than a field write, and moving
    a row between planes mid-patch is a different act from editing it.

    `closed_by_session` and `close_reason` — written under first-close-wins by `{id}:close`, which a
    patch write would bypass. A `status` that crosses into the done category still answers to close
    POLICY here; it does not acquire the close's semantics, which is why `{id}:close` remains the
    operation to reach for when what you mean is "close this".

    `created_at`, `created_by` — `POST /v0/beads/issues` withholds them for the same reason: a caller-
    chosen creation time makes the row disagree with the journal entry that recorded it, and re-dating
    history is what an import is for.

    `spec_id` and `await_id` — workflow plumbing this surface publishes nowhere yet.

    ## Planes

    The id resolves across BOTH planes, as the close and reopen do. An update whose target is a wisp
    lands on the unversioned plane and records no durable history entry.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        id (str):
        body (UpdateIssueRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | UpdateIssueResponse]
    """

    kwargs = _get_kwargs(
        id=id,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    id: str,
    *,
    client: AuthenticatedClient,
    body: UpdateIssueRequest,
) -> Problem | UpdateIssueResponse | None:
    """Edit the fields of one issue

     Partial update of one named issue. A plain `PATCH` rather than a custom method, because partial
    update of one named resource is what `PATCH` already means — exactly as one named resource with no
    body is what `DELETE` means for `forgetMemory`. The custom-method namespace is left for the
    operations that are not CRUD.

    ## Member presence is the signal

    A member PRESENT in `patch` is written; a member ABSENT is left untouched. That is the whole
    partial-update rule, and it is why an absent member and a member set to its current value are
    different requests with the same outcome.

    Explicit `null` is defined per member. On the four NULLABLE members — `estimated_minutes`,
    `external_ref`, `due_at`, `defer_until` — it CLEARS the value, because `null` is the only wire
    spelling a clear has. On every other member it is a `400` naming the member, so a null is never
    quietly recorded as an empty string.

    An EMPTY `patch` object is a `400`: a write that writes nothing is a client bug, the same judgement
    `issues:batchCreate` makes about an empty `items`.

    `labels` is COMPLETE REPLACEMENT. It is the only shape whose result the client already knows without
    a read-back; an additive `labels_add`/`labels_remove` pair can join the vocabulary later without
    disturbing it.

    `notes` and `append_notes` are mutually exclusive; sending both is a `400`.

    ## Guards, forces and the conflicts they answer

    `expected_version`, `expected_status` and `expected_assignee` are compare-and-set preconditions,
    checked before the patch. A miss refuses the WHOLE request with `409 precondition_failed` and writes
    nothing, so recovery is to re-read and recompose rather than to retry the same body. They are
    `ApplyUpdateItem`'s three members with `ApplyUpdateItem`'s contract; `expected_version`'s token is
    the `revision` this operation answers with.

    `force_close_policy` and `force_assignee_transfer` bypass exactly one refusal each and nothing else.
    Neither bypasses validation, the preconditions above, or the other's guard.

    Three members carry policy, which is where this operation's `409`s come from. `status` crossing into
    the workspace's done category answers to close policy (`not_closable`). `assignee` transferring away
    from a live foreign in-progress owner answers to the assignee fence (`already_claimed`). `parent_id`
    is a graph edit and answers to the graph (`dependency_cycle`, `dependency_exists`). Each is the SAME
    refusal `issues:batchApply` and `dependencies:add` already publish, not a second vocabulary.

    ## What this operation deliberately cannot edit

    `owner` — additive later, and there is no argument against it any more: the one it had was
    `assignee`'s, which this operation now publishes. Nothing has asked for it.

    `persistence` — a plane move is an atomic aggregate migration rather than a field write, and moving
    a row between planes mid-patch is a different act from editing it.

    `closed_by_session` and `close_reason` — written under first-close-wins by `{id}:close`, which a
    patch write would bypass. A `status` that crosses into the done category still answers to close
    POLICY here; it does not acquire the close's semantics, which is why `{id}:close` remains the
    operation to reach for when what you mean is "close this".

    `created_at`, `created_by` — `POST /v0/beads/issues` withholds them for the same reason: a caller-
    chosen creation time makes the row disagree with the journal entry that recorded it, and re-dating
    history is what an import is for.

    `spec_id` and `await_id` — workflow plumbing this surface publishes nowhere yet.

    ## Planes

    The id resolves across BOTH planes, as the close and reopen do. An update whose target is a wisp
    lands on the unversioned plane and records no durable history entry.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        id (str):
        body (UpdateIssueRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | UpdateIssueResponse
    """

    return sync_detailed(
        id=id,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    id: str,
    *,
    client: AuthenticatedClient,
    body: UpdateIssueRequest,
) -> Response[Problem | UpdateIssueResponse]:
    """Edit the fields of one issue

     Partial update of one named issue. A plain `PATCH` rather than a custom method, because partial
    update of one named resource is what `PATCH` already means — exactly as one named resource with no
    body is what `DELETE` means for `forgetMemory`. The custom-method namespace is left for the
    operations that are not CRUD.

    ## Member presence is the signal

    A member PRESENT in `patch` is written; a member ABSENT is left untouched. That is the whole
    partial-update rule, and it is why an absent member and a member set to its current value are
    different requests with the same outcome.

    Explicit `null` is defined per member. On the four NULLABLE members — `estimated_minutes`,
    `external_ref`, `due_at`, `defer_until` — it CLEARS the value, because `null` is the only wire
    spelling a clear has. On every other member it is a `400` naming the member, so a null is never
    quietly recorded as an empty string.

    An EMPTY `patch` object is a `400`: a write that writes nothing is a client bug, the same judgement
    `issues:batchCreate` makes about an empty `items`.

    `labels` is COMPLETE REPLACEMENT. It is the only shape whose result the client already knows without
    a read-back; an additive `labels_add`/`labels_remove` pair can join the vocabulary later without
    disturbing it.

    `notes` and `append_notes` are mutually exclusive; sending both is a `400`.

    ## Guards, forces and the conflicts they answer

    `expected_version`, `expected_status` and `expected_assignee` are compare-and-set preconditions,
    checked before the patch. A miss refuses the WHOLE request with `409 precondition_failed` and writes
    nothing, so recovery is to re-read and recompose rather than to retry the same body. They are
    `ApplyUpdateItem`'s three members with `ApplyUpdateItem`'s contract; `expected_version`'s token is
    the `revision` this operation answers with.

    `force_close_policy` and `force_assignee_transfer` bypass exactly one refusal each and nothing else.
    Neither bypasses validation, the preconditions above, or the other's guard.

    Three members carry policy, which is where this operation's `409`s come from. `status` crossing into
    the workspace's done category answers to close policy (`not_closable`). `assignee` transferring away
    from a live foreign in-progress owner answers to the assignee fence (`already_claimed`). `parent_id`
    is a graph edit and answers to the graph (`dependency_cycle`, `dependency_exists`). Each is the SAME
    refusal `issues:batchApply` and `dependencies:add` already publish, not a second vocabulary.

    ## What this operation deliberately cannot edit

    `owner` — additive later, and there is no argument against it any more: the one it had was
    `assignee`'s, which this operation now publishes. Nothing has asked for it.

    `persistence` — a plane move is an atomic aggregate migration rather than a field write, and moving
    a row between planes mid-patch is a different act from editing it.

    `closed_by_session` and `close_reason` — written under first-close-wins by `{id}:close`, which a
    patch write would bypass. A `status` that crosses into the done category still answers to close
    POLICY here; it does not acquire the close's semantics, which is why `{id}:close` remains the
    operation to reach for when what you mean is "close this".

    `created_at`, `created_by` — `POST /v0/beads/issues` withholds them for the same reason: a caller-
    chosen creation time makes the row disagree with the journal entry that recorded it, and re-dating
    history is what an import is for.

    `spec_id` and `await_id` — workflow plumbing this surface publishes nowhere yet.

    ## Planes

    The id resolves across BOTH planes, as the close and reopen do. An update whose target is a wisp
    lands on the unversioned plane and records no durable history entry.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        id (str):
        body (UpdateIssueRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | UpdateIssueResponse]
    """

    kwargs = _get_kwargs(
        id=id,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    id: str,
    *,
    client: AuthenticatedClient,
    body: UpdateIssueRequest,
) -> Problem | UpdateIssueResponse | None:
    """Edit the fields of one issue

     Partial update of one named issue. A plain `PATCH` rather than a custom method, because partial
    update of one named resource is what `PATCH` already means — exactly as one named resource with no
    body is what `DELETE` means for `forgetMemory`. The custom-method namespace is left for the
    operations that are not CRUD.

    ## Member presence is the signal

    A member PRESENT in `patch` is written; a member ABSENT is left untouched. That is the whole
    partial-update rule, and it is why an absent member and a member set to its current value are
    different requests with the same outcome.

    Explicit `null` is defined per member. On the four NULLABLE members — `estimated_minutes`,
    `external_ref`, `due_at`, `defer_until` — it CLEARS the value, because `null` is the only wire
    spelling a clear has. On every other member it is a `400` naming the member, so a null is never
    quietly recorded as an empty string.

    An EMPTY `patch` object is a `400`: a write that writes nothing is a client bug, the same judgement
    `issues:batchCreate` makes about an empty `items`.

    `labels` is COMPLETE REPLACEMENT. It is the only shape whose result the client already knows without
    a read-back; an additive `labels_add`/`labels_remove` pair can join the vocabulary later without
    disturbing it.

    `notes` and `append_notes` are mutually exclusive; sending both is a `400`.

    ## Guards, forces and the conflicts they answer

    `expected_version`, `expected_status` and `expected_assignee` are compare-and-set preconditions,
    checked before the patch. A miss refuses the WHOLE request with `409 precondition_failed` and writes
    nothing, so recovery is to re-read and recompose rather than to retry the same body. They are
    `ApplyUpdateItem`'s three members with `ApplyUpdateItem`'s contract; `expected_version`'s token is
    the `revision` this operation answers with.

    `force_close_policy` and `force_assignee_transfer` bypass exactly one refusal each and nothing else.
    Neither bypasses validation, the preconditions above, or the other's guard.

    Three members carry policy, which is where this operation's `409`s come from. `status` crossing into
    the workspace's done category answers to close policy (`not_closable`). `assignee` transferring away
    from a live foreign in-progress owner answers to the assignee fence (`already_claimed`). `parent_id`
    is a graph edit and answers to the graph (`dependency_cycle`, `dependency_exists`). Each is the SAME
    refusal `issues:batchApply` and `dependencies:add` already publish, not a second vocabulary.

    ## What this operation deliberately cannot edit

    `owner` — additive later, and there is no argument against it any more: the one it had was
    `assignee`'s, which this operation now publishes. Nothing has asked for it.

    `persistence` — a plane move is an atomic aggregate migration rather than a field write, and moving
    a row between planes mid-patch is a different act from editing it.

    `closed_by_session` and `close_reason` — written under first-close-wins by `{id}:close`, which a
    patch write would bypass. A `status` that crosses into the done category still answers to close
    POLICY here; it does not acquire the close's semantics, which is why `{id}:close` remains the
    operation to reach for when what you mean is "close this".

    `created_at`, `created_by` — `POST /v0/beads/issues` withholds them for the same reason: a caller-
    chosen creation time makes the row disagree with the journal entry that recorded it, and re-dating
    history is what an import is for.

    `spec_id` and `await_id` — workflow plumbing this surface publishes nowhere yet.

    ## Planes

    The id resolves across BOTH planes, as the close and reopen do. An update whose target is a wisp
    lands on the unversioned plane and records no durable history entry.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        id (str):
        body (UpdateIssueRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | UpdateIssueResponse
    """

    return (
        await asyncio_detailed(
            id=id,
            client=client,
            body=body,
        )
    ).parsed
