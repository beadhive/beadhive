from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.add_comment_request import AddCommentRequest
from ...models.comment import Comment
from ...models.problem import Problem
from ...types import Response


def _get_kwargs(
    id: str,
    *,
    body: AddCommentRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v0/beads/issues/{id}/comments".format(
            id=quote(str(id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Comment | Problem | None:
    if response.status_code == 200:
        response_200 = Comment.from_dict(response.json())

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
) -> Response[Comment | Problem]:
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
    body: AddCommentRequest,
) -> Response[Comment | Problem]:
    """Append one comment to an issue's thread

     Appends ONE comment to the thread the issue owns, as one atomic mutation — the write behind `bd
    comment`. It leaves every field of the issue untouched: a comment is not a patch, which is why it is
    not a member of `PATCH /v0/beads/issues/{id}` and why `issueops.Commenter` is its own role rather
    than a lifecycle verb.

    IT IS A SUB-RESOURCE COLLECTION OF THE ISSUE, and the argument is the one `GET
    /v0/beads/issues/{id}/related` makes, applied to a write: the row this creates is the SAME pinned
    `Comment` `GET /v0/beads/issues/{id}?include_comments=true` already carries under `comments`, so the
    operation that appends one belongs on the resource whose members it publishes. A PLAIN collection
    `POST` rather than a custom method, for `POST /v0/beads/issues`' reason: creating one member of the
    collection a path names is what `POST` already means, and a `{id}:addComment` spelling would in
    addition need the claim route's wildcard contortion for no gain. It collides with nothing — the
    custom-method dispatcher's pattern is one segment shorter, and this path's literal final segment is
    what ServeMux matches whole.

    THIS COLLECTION PUBLISHES NO `GET`, and the absence is a statement rather than a gap. No role
    answers a comment PAGE: reading the thread is `GET /v0/beads/issues/{id}?include_comments=true`,
    which returns the bodies in full, and a paged walk is a second question with a cursor of its own —
    `issueops.Commenter` says exactly that, and a `GET` here would be this surface inventing the role
    that does not exist. A `GET` on this path is answered `404`, which is what every method mismatch on
    this surface gets; `405` is not in the v0 status vocabulary.

    ## `author` is caller-asserted, and it is not `actor`

    The caller always names the author and the server never infers one, for `POST
    /v0/beads/issues/{id}:claim`'s reason. IT IS NOT THE AUTHENTICATED PRINCIPAL even where a bearer is
    required: the token a deployment configures admits a client to the whole surface and names nobody,
    so it can neither confirm nor contradict the name in `author`. A reader of the thread is reading a
    claim the writer made about itself.

    It is spelled `author` rather than the `actor` every issue mutation here carries because it is a
    different thing. An `actor` is the principal a mutation is attributed to and is not part of what the
    mutation wrote; this value IS part of the row, echoed back by every read of the thread, and it is
    spelled the way the `Comment` element that carries it back spells it.
    `issueops.AddCommentRequest.Author` states the distinction; this member is that field.

    ## Planes

    The id resolves across BOTH planes, as `POST /v0/beads/issues/{id}:close` does and unlike `POST
    /v0/beads/issues/{id}:claim`: A WISP IS A LEGAL TARGET. The comment lands on the ephemeral thread
    and reads back from it, and only the DURABLE trace is missing — a comment on an ephemeral row
    records NO history entry, none rather than one, because the wisp tables are dolt-ignored precisely
    so ephemeral work never ships. A caller reconstructing threads from durable history alone will not
    see it. An id that names neither plane is a `404` and nothing is written.

    ## What this operation does not have

    NO CONFLICT CODE AND NO `expected_version`. A thread is append-only and this write touches no field
    of the issue, so there is no row state for a guard to be stale about and no concurrent comment for
    this one to collide with. Two callers commenting at once both succeed, in whatever order the
    database commits them.

    NO IDEMPOTENCY KEY EITHER: a retried request appends a SECOND comment, because two identical
    comments are a legitimate thread and nothing here can tell that pair from a retry. A client that
    must not double-post reads the thread.

    Hooks do not fire and the per-command auto-commit machinery does not run, as for every write on this
    surface. The only durable effect is the single storage commit the role makes inside its own
    transaction.

    Args:
        id (str):
        body (AddCommentRequest): One comment to append. The issue is named by the path, so it is
            not a member here: a body carrying it too would give one request two spellings of one
            anchor and a question about what to do when they disagree.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Comment | Problem]
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
    body: AddCommentRequest,
) -> Comment | Problem | None:
    """Append one comment to an issue's thread

     Appends ONE comment to the thread the issue owns, as one atomic mutation — the write behind `bd
    comment`. It leaves every field of the issue untouched: a comment is not a patch, which is why it is
    not a member of `PATCH /v0/beads/issues/{id}` and why `issueops.Commenter` is its own role rather
    than a lifecycle verb.

    IT IS A SUB-RESOURCE COLLECTION OF THE ISSUE, and the argument is the one `GET
    /v0/beads/issues/{id}/related` makes, applied to a write: the row this creates is the SAME pinned
    `Comment` `GET /v0/beads/issues/{id}?include_comments=true` already carries under `comments`, so the
    operation that appends one belongs on the resource whose members it publishes. A PLAIN collection
    `POST` rather than a custom method, for `POST /v0/beads/issues`' reason: creating one member of the
    collection a path names is what `POST` already means, and a `{id}:addComment` spelling would in
    addition need the claim route's wildcard contortion for no gain. It collides with nothing — the
    custom-method dispatcher's pattern is one segment shorter, and this path's literal final segment is
    what ServeMux matches whole.

    THIS COLLECTION PUBLISHES NO `GET`, and the absence is a statement rather than a gap. No role
    answers a comment PAGE: reading the thread is `GET /v0/beads/issues/{id}?include_comments=true`,
    which returns the bodies in full, and a paged walk is a second question with a cursor of its own —
    `issueops.Commenter` says exactly that, and a `GET` here would be this surface inventing the role
    that does not exist. A `GET` on this path is answered `404`, which is what every method mismatch on
    this surface gets; `405` is not in the v0 status vocabulary.

    ## `author` is caller-asserted, and it is not `actor`

    The caller always names the author and the server never infers one, for `POST
    /v0/beads/issues/{id}:claim`'s reason. IT IS NOT THE AUTHENTICATED PRINCIPAL even where a bearer is
    required: the token a deployment configures admits a client to the whole surface and names nobody,
    so it can neither confirm nor contradict the name in `author`. A reader of the thread is reading a
    claim the writer made about itself.

    It is spelled `author` rather than the `actor` every issue mutation here carries because it is a
    different thing. An `actor` is the principal a mutation is attributed to and is not part of what the
    mutation wrote; this value IS part of the row, echoed back by every read of the thread, and it is
    spelled the way the `Comment` element that carries it back spells it.
    `issueops.AddCommentRequest.Author` states the distinction; this member is that field.

    ## Planes

    The id resolves across BOTH planes, as `POST /v0/beads/issues/{id}:close` does and unlike `POST
    /v0/beads/issues/{id}:claim`: A WISP IS A LEGAL TARGET. The comment lands on the ephemeral thread
    and reads back from it, and only the DURABLE trace is missing — a comment on an ephemeral row
    records NO history entry, none rather than one, because the wisp tables are dolt-ignored precisely
    so ephemeral work never ships. A caller reconstructing threads from durable history alone will not
    see it. An id that names neither plane is a `404` and nothing is written.

    ## What this operation does not have

    NO CONFLICT CODE AND NO `expected_version`. A thread is append-only and this write touches no field
    of the issue, so there is no row state for a guard to be stale about and no concurrent comment for
    this one to collide with. Two callers commenting at once both succeed, in whatever order the
    database commits them.

    NO IDEMPOTENCY KEY EITHER: a retried request appends a SECOND comment, because two identical
    comments are a legitimate thread and nothing here can tell that pair from a retry. A client that
    must not double-post reads the thread.

    Hooks do not fire and the per-command auto-commit machinery does not run, as for every write on this
    surface. The only durable effect is the single storage commit the role makes inside its own
    transaction.

    Args:
        id (str):
        body (AddCommentRequest): One comment to append. The issue is named by the path, so it is
            not a member here: a body carrying it too would give one request two spellings of one
            anchor and a question about what to do when they disagree.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Comment | Problem
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
    body: AddCommentRequest,
) -> Response[Comment | Problem]:
    """Append one comment to an issue's thread

     Appends ONE comment to the thread the issue owns, as one atomic mutation — the write behind `bd
    comment`. It leaves every field of the issue untouched: a comment is not a patch, which is why it is
    not a member of `PATCH /v0/beads/issues/{id}` and why `issueops.Commenter` is its own role rather
    than a lifecycle verb.

    IT IS A SUB-RESOURCE COLLECTION OF THE ISSUE, and the argument is the one `GET
    /v0/beads/issues/{id}/related` makes, applied to a write: the row this creates is the SAME pinned
    `Comment` `GET /v0/beads/issues/{id}?include_comments=true` already carries under `comments`, so the
    operation that appends one belongs on the resource whose members it publishes. A PLAIN collection
    `POST` rather than a custom method, for `POST /v0/beads/issues`' reason: creating one member of the
    collection a path names is what `POST` already means, and a `{id}:addComment` spelling would in
    addition need the claim route's wildcard contortion for no gain. It collides with nothing — the
    custom-method dispatcher's pattern is one segment shorter, and this path's literal final segment is
    what ServeMux matches whole.

    THIS COLLECTION PUBLISHES NO `GET`, and the absence is a statement rather than a gap. No role
    answers a comment PAGE: reading the thread is `GET /v0/beads/issues/{id}?include_comments=true`,
    which returns the bodies in full, and a paged walk is a second question with a cursor of its own —
    `issueops.Commenter` says exactly that, and a `GET` here would be this surface inventing the role
    that does not exist. A `GET` on this path is answered `404`, which is what every method mismatch on
    this surface gets; `405` is not in the v0 status vocabulary.

    ## `author` is caller-asserted, and it is not `actor`

    The caller always names the author and the server never infers one, for `POST
    /v0/beads/issues/{id}:claim`'s reason. IT IS NOT THE AUTHENTICATED PRINCIPAL even where a bearer is
    required: the token a deployment configures admits a client to the whole surface and names nobody,
    so it can neither confirm nor contradict the name in `author`. A reader of the thread is reading a
    claim the writer made about itself.

    It is spelled `author` rather than the `actor` every issue mutation here carries because it is a
    different thing. An `actor` is the principal a mutation is attributed to and is not part of what the
    mutation wrote; this value IS part of the row, echoed back by every read of the thread, and it is
    spelled the way the `Comment` element that carries it back spells it.
    `issueops.AddCommentRequest.Author` states the distinction; this member is that field.

    ## Planes

    The id resolves across BOTH planes, as `POST /v0/beads/issues/{id}:close` does and unlike `POST
    /v0/beads/issues/{id}:claim`: A WISP IS A LEGAL TARGET. The comment lands on the ephemeral thread
    and reads back from it, and only the DURABLE trace is missing — a comment on an ephemeral row
    records NO history entry, none rather than one, because the wisp tables are dolt-ignored precisely
    so ephemeral work never ships. A caller reconstructing threads from durable history alone will not
    see it. An id that names neither plane is a `404` and nothing is written.

    ## What this operation does not have

    NO CONFLICT CODE AND NO `expected_version`. A thread is append-only and this write touches no field
    of the issue, so there is no row state for a guard to be stale about and no concurrent comment for
    this one to collide with. Two callers commenting at once both succeed, in whatever order the
    database commits them.

    NO IDEMPOTENCY KEY EITHER: a retried request appends a SECOND comment, because two identical
    comments are a legitimate thread and nothing here can tell that pair from a retry. A client that
    must not double-post reads the thread.

    Hooks do not fire and the per-command auto-commit machinery does not run, as for every write on this
    surface. The only durable effect is the single storage commit the role makes inside its own
    transaction.

    Args:
        id (str):
        body (AddCommentRequest): One comment to append. The issue is named by the path, so it is
            not a member here: a body carrying it too would give one request two spellings of one
            anchor and a question about what to do when they disagree.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Comment | Problem]
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
    body: AddCommentRequest,
) -> Comment | Problem | None:
    """Append one comment to an issue's thread

     Appends ONE comment to the thread the issue owns, as one atomic mutation — the write behind `bd
    comment`. It leaves every field of the issue untouched: a comment is not a patch, which is why it is
    not a member of `PATCH /v0/beads/issues/{id}` and why `issueops.Commenter` is its own role rather
    than a lifecycle verb.

    IT IS A SUB-RESOURCE COLLECTION OF THE ISSUE, and the argument is the one `GET
    /v0/beads/issues/{id}/related` makes, applied to a write: the row this creates is the SAME pinned
    `Comment` `GET /v0/beads/issues/{id}?include_comments=true` already carries under `comments`, so the
    operation that appends one belongs on the resource whose members it publishes. A PLAIN collection
    `POST` rather than a custom method, for `POST /v0/beads/issues`' reason: creating one member of the
    collection a path names is what `POST` already means, and a `{id}:addComment` spelling would in
    addition need the claim route's wildcard contortion for no gain. It collides with nothing — the
    custom-method dispatcher's pattern is one segment shorter, and this path's literal final segment is
    what ServeMux matches whole.

    THIS COLLECTION PUBLISHES NO `GET`, and the absence is a statement rather than a gap. No role
    answers a comment PAGE: reading the thread is `GET /v0/beads/issues/{id}?include_comments=true`,
    which returns the bodies in full, and a paged walk is a second question with a cursor of its own —
    `issueops.Commenter` says exactly that, and a `GET` here would be this surface inventing the role
    that does not exist. A `GET` on this path is answered `404`, which is what every method mismatch on
    this surface gets; `405` is not in the v0 status vocabulary.

    ## `author` is caller-asserted, and it is not `actor`

    The caller always names the author and the server never infers one, for `POST
    /v0/beads/issues/{id}:claim`'s reason. IT IS NOT THE AUTHENTICATED PRINCIPAL even where a bearer is
    required: the token a deployment configures admits a client to the whole surface and names nobody,
    so it can neither confirm nor contradict the name in `author`. A reader of the thread is reading a
    claim the writer made about itself.

    It is spelled `author` rather than the `actor` every issue mutation here carries because it is a
    different thing. An `actor` is the principal a mutation is attributed to and is not part of what the
    mutation wrote; this value IS part of the row, echoed back by every read of the thread, and it is
    spelled the way the `Comment` element that carries it back spells it.
    `issueops.AddCommentRequest.Author` states the distinction; this member is that field.

    ## Planes

    The id resolves across BOTH planes, as `POST /v0/beads/issues/{id}:close` does and unlike `POST
    /v0/beads/issues/{id}:claim`: A WISP IS A LEGAL TARGET. The comment lands on the ephemeral thread
    and reads back from it, and only the DURABLE trace is missing — a comment on an ephemeral row
    records NO history entry, none rather than one, because the wisp tables are dolt-ignored precisely
    so ephemeral work never ships. A caller reconstructing threads from durable history alone will not
    see it. An id that names neither plane is a `404` and nothing is written.

    ## What this operation does not have

    NO CONFLICT CODE AND NO `expected_version`. A thread is append-only and this write touches no field
    of the issue, so there is no row state for a guard to be stale about and no concurrent comment for
    this one to collide with. Two callers commenting at once both succeed, in whatever order the
    database commits them.

    NO IDEMPOTENCY KEY EITHER: a retried request appends a SECOND comment, because two identical
    comments are a legitimate thread and nothing here can tell that pair from a retry. A client that
    must not double-post reads the thread.

    Hooks do not fire and the per-command auto-commit machinery does not run, as for every write on this
    surface. The only durable effect is the single storage commit the role makes inside its own
    transaction.

    Args:
        id (str):
        body (AddCommentRequest): One comment to append. The issue is named by the path, so it is
            not a member here: a body carrying it too would give one request two spellings of one
            anchor and a question about what to do when they disagree.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Comment | Problem
    """

    return (
        await asyncio_detailed(
            id=id,
            client=client,
            body=body,
        )
    ).parsed
