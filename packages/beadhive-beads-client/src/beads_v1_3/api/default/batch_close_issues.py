from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.batch_close_request import BatchCloseRequest
from ...models.batch_close_response import BatchCloseResponse
from ...models.problem import Problem
from ...types import Response


def _get_kwargs(
    *,
    body: BatchCloseRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v0/beads/issues:batchClose",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> BatchCloseResponse | Problem | None:
    if response.status_code == 200:
        response_200 = BatchCloseResponse.from_dict(response.json())

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
) -> Response[BatchCloseResponse | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: BatchCloseRequest,
) -> Response[BatchCloseResponse | Problem]:
    """Close many issues as one act

     Closes every item it can and commits them together. It is the write side of `bd close a b c`, and it
    is the operation `POST /v0/beads/issues:batchCreate` already names as its own opposite.

    THE REQUEST IS THE TRANSACTION BOUNDARY, which is the whole reason this is one operation rather than
    a loop over `POST /v0/beads/issues/{id}:close`: closing N issues one call at a time is N
    transactions and N history entries, and a caller that wants them to land together has no way to say
    so.

    ## It is NOT all-or-nothing, and that is the point

    An id this batch refuses is SKIPPED and the survivors commit. An agent that finishes four of five
    steps and mistypes the fifth keeps the four; making the batch atomic in the other sense would turn a
    typo into a rollback of finished work.

    SO THE ANSWER IS A 200 CARRYING PER-ITEM OUTCOMES, even when items refused. `outcomes` has exactly
    one entry per requested item, in REQUEST ORDER, so a client walks it against its own argument list
    without matching ids back up. A NON-2xx from this operation means the batch NEVER RAN — a refused
    body, or an infrastructure failure — and never that some items landed.

    That divides the refusal vocabulary in two, and the division is the contract: a refusal OF THE
    REQUEST is a problem document, and a refusal OF AN ITEM is a member of that item's outcome. `code`
    on an outcome is the same vocabulary `Problem.code` publishes, restricted to `not_found` and
    `not_closable`, so a client classifies an item exactly as it classifies a request.

    WHAT IS ATOMIC IS EVERYTHING THAT LANDS: one transaction, at most one history entry, and none at all
    when nothing landed. LANDED MEANS CHANGED — an idempotent re-close is a per-item success that
    persisted nothing, so a batch of them lands nothing and records no history entry, exactly as a batch
    of typos does.

    ## Duplicates and planes

    A DUPLICATED id is not a request error: `bd close a b a` is a plausible typo, not a failure. Items
    are closed in the order given, so the second occurrence finds what the first one did and reports an
    idempotent re-close at ITS OWN index — a success with `already_closed: true`. The reason on the row
    is the first occurrence's, because the second mutated nothing and so wrote nothing.

    A WISP ID IS AN ADMISSIBLE ITEM, resolved across both planes exactly as `POST
    /v0/beads/issues/{id}:close` resolves one. What it does not do is reach the durable history entry:
    the entry a mixed batch records is composed from its DURABLE landings alone.

    ## What this operation does not publish

    NO COMPOSED CLAIM. The role can claim the next ready issue in the same transaction once the closes
    land, and that member is deliberately not published here — expressing it would require a second,
    BODY-shaped spelling of the ready-filter vocabulary that `GET /v0/beads/ready` and `POST
    /v0/beads/issues:claimNext` both express as query parameters, and two spellings of one predicate
    eventually disagree. A client that wants both sends the two requests; what it loses is the single
    transaction, which is a real loss and is named here rather than papered over. It is additive later,
    once there is one shape for a ready filter in a body.

    NO PER-ITEM PRECONDITION. There is no counterpart here to a compare-and-set guard, matching the
    role, so `force` is a question with only two answers: it bypasses blocker and open-child close
    policy for every item, and it never bypasses validation or existence.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:close`.

    Args:
        body (BatchCloseRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BatchCloseResponse | Problem]
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
    body: BatchCloseRequest,
) -> BatchCloseResponse | Problem | None:
    """Close many issues as one act

     Closes every item it can and commits them together. It is the write side of `bd close a b c`, and it
    is the operation `POST /v0/beads/issues:batchCreate` already names as its own opposite.

    THE REQUEST IS THE TRANSACTION BOUNDARY, which is the whole reason this is one operation rather than
    a loop over `POST /v0/beads/issues/{id}:close`: closing N issues one call at a time is N
    transactions and N history entries, and a caller that wants them to land together has no way to say
    so.

    ## It is NOT all-or-nothing, and that is the point

    An id this batch refuses is SKIPPED and the survivors commit. An agent that finishes four of five
    steps and mistypes the fifth keeps the four; making the batch atomic in the other sense would turn a
    typo into a rollback of finished work.

    SO THE ANSWER IS A 200 CARRYING PER-ITEM OUTCOMES, even when items refused. `outcomes` has exactly
    one entry per requested item, in REQUEST ORDER, so a client walks it against its own argument list
    without matching ids back up. A NON-2xx from this operation means the batch NEVER RAN — a refused
    body, or an infrastructure failure — and never that some items landed.

    That divides the refusal vocabulary in two, and the division is the contract: a refusal OF THE
    REQUEST is a problem document, and a refusal OF AN ITEM is a member of that item's outcome. `code`
    on an outcome is the same vocabulary `Problem.code` publishes, restricted to `not_found` and
    `not_closable`, so a client classifies an item exactly as it classifies a request.

    WHAT IS ATOMIC IS EVERYTHING THAT LANDS: one transaction, at most one history entry, and none at all
    when nothing landed. LANDED MEANS CHANGED — an idempotent re-close is a per-item success that
    persisted nothing, so a batch of them lands nothing and records no history entry, exactly as a batch
    of typos does.

    ## Duplicates and planes

    A DUPLICATED id is not a request error: `bd close a b a` is a plausible typo, not a failure. Items
    are closed in the order given, so the second occurrence finds what the first one did and reports an
    idempotent re-close at ITS OWN index — a success with `already_closed: true`. The reason on the row
    is the first occurrence's, because the second mutated nothing and so wrote nothing.

    A WISP ID IS AN ADMISSIBLE ITEM, resolved across both planes exactly as `POST
    /v0/beads/issues/{id}:close` resolves one. What it does not do is reach the durable history entry:
    the entry a mixed batch records is composed from its DURABLE landings alone.

    ## What this operation does not publish

    NO COMPOSED CLAIM. The role can claim the next ready issue in the same transaction once the closes
    land, and that member is deliberately not published here — expressing it would require a second,
    BODY-shaped spelling of the ready-filter vocabulary that `GET /v0/beads/ready` and `POST
    /v0/beads/issues:claimNext` both express as query parameters, and two spellings of one predicate
    eventually disagree. A client that wants both sends the two requests; what it loses is the single
    transaction, which is a real loss and is named here rather than papered over. It is additive later,
    once there is one shape for a ready filter in a body.

    NO PER-ITEM PRECONDITION. There is no counterpart here to a compare-and-set guard, matching the
    role, so `force` is a question with only two answers: it bypasses blocker and open-child close
    policy for every item, and it never bypasses validation or existence.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:close`.

    Args:
        body (BatchCloseRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BatchCloseResponse | Problem
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: BatchCloseRequest,
) -> Response[BatchCloseResponse | Problem]:
    """Close many issues as one act

     Closes every item it can and commits them together. It is the write side of `bd close a b c`, and it
    is the operation `POST /v0/beads/issues:batchCreate` already names as its own opposite.

    THE REQUEST IS THE TRANSACTION BOUNDARY, which is the whole reason this is one operation rather than
    a loop over `POST /v0/beads/issues/{id}:close`: closing N issues one call at a time is N
    transactions and N history entries, and a caller that wants them to land together has no way to say
    so.

    ## It is NOT all-or-nothing, and that is the point

    An id this batch refuses is SKIPPED and the survivors commit. An agent that finishes four of five
    steps and mistypes the fifth keeps the four; making the batch atomic in the other sense would turn a
    typo into a rollback of finished work.

    SO THE ANSWER IS A 200 CARRYING PER-ITEM OUTCOMES, even when items refused. `outcomes` has exactly
    one entry per requested item, in REQUEST ORDER, so a client walks it against its own argument list
    without matching ids back up. A NON-2xx from this operation means the batch NEVER RAN — a refused
    body, or an infrastructure failure — and never that some items landed.

    That divides the refusal vocabulary in two, and the division is the contract: a refusal OF THE
    REQUEST is a problem document, and a refusal OF AN ITEM is a member of that item's outcome. `code`
    on an outcome is the same vocabulary `Problem.code` publishes, restricted to `not_found` and
    `not_closable`, so a client classifies an item exactly as it classifies a request.

    WHAT IS ATOMIC IS EVERYTHING THAT LANDS: one transaction, at most one history entry, and none at all
    when nothing landed. LANDED MEANS CHANGED — an idempotent re-close is a per-item success that
    persisted nothing, so a batch of them lands nothing and records no history entry, exactly as a batch
    of typos does.

    ## Duplicates and planes

    A DUPLICATED id is not a request error: `bd close a b a` is a plausible typo, not a failure. Items
    are closed in the order given, so the second occurrence finds what the first one did and reports an
    idempotent re-close at ITS OWN index — a success with `already_closed: true`. The reason on the row
    is the first occurrence's, because the second mutated nothing and so wrote nothing.

    A WISP ID IS AN ADMISSIBLE ITEM, resolved across both planes exactly as `POST
    /v0/beads/issues/{id}:close` resolves one. What it does not do is reach the durable history entry:
    the entry a mixed batch records is composed from its DURABLE landings alone.

    ## What this operation does not publish

    NO COMPOSED CLAIM. The role can claim the next ready issue in the same transaction once the closes
    land, and that member is deliberately not published here — expressing it would require a second,
    BODY-shaped spelling of the ready-filter vocabulary that `GET /v0/beads/ready` and `POST
    /v0/beads/issues:claimNext` both express as query parameters, and two spellings of one predicate
    eventually disagree. A client that wants both sends the two requests; what it loses is the single
    transaction, which is a real loss and is named here rather than papered over. It is additive later,
    once there is one shape for a ready filter in a body.

    NO PER-ITEM PRECONDITION. There is no counterpart here to a compare-and-set guard, matching the
    role, so `force` is a question with only two answers: it bypasses blocker and open-child close
    policy for every item, and it never bypasses validation or existence.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:close`.

    Args:
        body (BatchCloseRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BatchCloseResponse | Problem]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    body: BatchCloseRequest,
) -> BatchCloseResponse | Problem | None:
    """Close many issues as one act

     Closes every item it can and commits them together. It is the write side of `bd close a b c`, and it
    is the operation `POST /v0/beads/issues:batchCreate` already names as its own opposite.

    THE REQUEST IS THE TRANSACTION BOUNDARY, which is the whole reason this is one operation rather than
    a loop over `POST /v0/beads/issues/{id}:close`: closing N issues one call at a time is N
    transactions and N history entries, and a caller that wants them to land together has no way to say
    so.

    ## It is NOT all-or-nothing, and that is the point

    An id this batch refuses is SKIPPED and the survivors commit. An agent that finishes four of five
    steps and mistypes the fifth keeps the four; making the batch atomic in the other sense would turn a
    typo into a rollback of finished work.

    SO THE ANSWER IS A 200 CARRYING PER-ITEM OUTCOMES, even when items refused. `outcomes` has exactly
    one entry per requested item, in REQUEST ORDER, so a client walks it against its own argument list
    without matching ids back up. A NON-2xx from this operation means the batch NEVER RAN — a refused
    body, or an infrastructure failure — and never that some items landed.

    That divides the refusal vocabulary in two, and the division is the contract: a refusal OF THE
    REQUEST is a problem document, and a refusal OF AN ITEM is a member of that item's outcome. `code`
    on an outcome is the same vocabulary `Problem.code` publishes, restricted to `not_found` and
    `not_closable`, so a client classifies an item exactly as it classifies a request.

    WHAT IS ATOMIC IS EVERYTHING THAT LANDS: one transaction, at most one history entry, and none at all
    when nothing landed. LANDED MEANS CHANGED — an idempotent re-close is a per-item success that
    persisted nothing, so a batch of them lands nothing and records no history entry, exactly as a batch
    of typos does.

    ## Duplicates and planes

    A DUPLICATED id is not a request error: `bd close a b a` is a plausible typo, not a failure. Items
    are closed in the order given, so the second occurrence finds what the first one did and reports an
    idempotent re-close at ITS OWN index — a success with `already_closed: true`. The reason on the row
    is the first occurrence's, because the second mutated nothing and so wrote nothing.

    A WISP ID IS AN ADMISSIBLE ITEM, resolved across both planes exactly as `POST
    /v0/beads/issues/{id}:close` resolves one. What it does not do is reach the durable history entry:
    the entry a mixed batch records is composed from its DURABLE landings alone.

    ## What this operation does not publish

    NO COMPOSED CLAIM. The role can claim the next ready issue in the same transaction once the closes
    land, and that member is deliberately not published here — expressing it would require a second,
    BODY-shaped spelling of the ready-filter vocabulary that `GET /v0/beads/ready` and `POST
    /v0/beads/issues:claimNext` both express as query parameters, and two spellings of one predicate
    eventually disagree. A client that wants both sends the two requests; what it loses is the single
    transaction, which is a real loss and is named here rather than papered over. It is additive later,
    once there is one shape for a ready filter in a body.

    NO PER-ITEM PRECONDITION. There is no counterpart here to a compare-and-set guard, matching the
    role, so `force` is a question with only two answers: it bypasses blocker and open-child close
    policy for every item, and it never bypasses validation or existence.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:close`.

    Args:
        body (BatchCloseRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BatchCloseResponse | Problem
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
