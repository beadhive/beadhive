from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.problem import Problem
from ...models.release_issue_request import ReleaseIssueRequest
from ...models.release_issue_response import ReleaseIssueResponse
from ...types import Response


def _get_kwargs(
    id: str,
    *,
    body: ReleaseIssueRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v0/beads/issues/{id}:release".format(
            id=quote(str(id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Problem | ReleaseIssueResponse | None:
    if response.status_code == 200:
        response_200 = ReleaseIssueResponse.from_dict(response.json())

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
) -> Response[Problem | ReleaseIssueResponse]:
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
    body: ReleaseIssueRequest,
) -> Response[Problem | ReleaseIssueResponse]:
    """Give back the claim on an issue

     The claim's inverse, and what `bd unclaim` spells. It ends the OWNERSHIP and leaves the work open
    for the next taker: assignee cleared, status the literal `open`, `started_at` cleared, the lease
    dropped, and `revision` reminted so a concurrent reclaim or close conflicts rather than silently
    merging.

    It is a named lifecycle action rather than a `patch`, for `POST /v0/beads/issues/{id}:close`'s
    reason: an update spells a release as three fields at a time, which puts the transition's definition
    in the CALLER, and the lease it drops is the part a patch cannot express at all.

    The caller always names the actor, for `POST /v0/beads/issues/{id}:claim`'s reason. HERE IT IS ALSO
    THE OWNERSHIP FENCE'S SUBJECT: a release carrying neither `expected_assignee` nor `force` succeeds
    only while `actor` is the current holder. That is not authentication — `actor` is caller-asserted
    provenance exactly as it is on the claim, and it is NOT the authenticated principal even where a
    bearer is required: the token a deployment configures admits a client to the whole surface and names
    nobody, so it can neither confirm nor contradict the name in `actor`. The fence is the same anti-
    yank guard the claim gets from refusing a foreign holder, pointed the other way.

    ## NOT IDEMPOTENT, which is the one thing to read before adopting it

    A release over a row that holds NO claim is `409` / `not_releasable`, never a 200. There is no
    `already_released` member here and there must not be one, because the post-state is ANONYMOUS: "I
    released this twice", "a reaper beat me to it" and "nothing ever claimed it" leave the identical row
    — assignee cleared, status open, `started_at` gone — so one 200 would report one answer for three
    situations that want different things from a caller. `claimIssue` can afford `already_claimed: true`
    because a claim's post-state NAMES the claimant, and this operation has nothing left on the row to
    name.

    ON `not_releasable`, READ THE ROW. Do NOT treat the code as "already released", however ordinary
    that case is for you — it is the one shortcut this operation's taxonomy makes unsafe, and it is
    unsafe in the direction that strands work.

    The code covers TWO conditions and publishes no member telling them apart (see the `409` below). One
    of them is "nothing holds this". The other is "the status will not accept a release", which is true
    of a row that is CLOSED and equally true of a row parked in a status this workspace configured — and
    such a row can still be ASSIGNED. A reaper that read this code as success would book a claim as
    dropped while it is still held, by an agent that is already gone, with nothing left to free it. That
    is exactly the stranding this operation resolves ids across both planes to prevent, reintroduced
    through the refusal vocabulary instead of through the id.

    So the recovery is a read: the row tells you which of the two you got — an assignee, or none. That
    is one extra request on an uncommon path, and it is the honest price of one code rather than two. A
    caller that wants the distinction without the read should say so, and it arrives as an ADDITION,
    which the `Problem.code` rules already tell clients to tolerate.

    ## The two ways to release a claim you do not hold

    `expected_assignee` is a compare-and-set on the holder and REPLACES the ownership fence: a caller
    that can name the current holder has demonstrated the view the fence exists to protect, so `actor`
    need not be that holder. It cannot release a claim that has since moved, which is what makes it the
    safer of the two for a supervisor reaping one named agent's abandoned work.

    `force` ignores the holder entirely, and ignores NOTHING ELSE. It does not make an unheld row
    releasable, it does not make a closed one releasable, and it may not accompany `expected_assignee` —
    the two are answers to the same question and they disagree, so sending both is a 400.

    ## Planes

    The id resolves across BOTH planes, unlike `POST /v0/beads/issues/{id}:claim` and like `POST
    /v0/beads/issues/{id}:close`. The asymmetry is about which direction strands work: a wisp can hold a
    claim, so an operation that refused to release one would leave an ephemeral row owned by an agent
    that is gone with no verb able to free it. A release whose target is a wisp records no durable
    history entry.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes
    inside its own transaction, which is what a proxied CLI write does today.

    Args:
        id (str):
        body (ReleaseIssueRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | ReleaseIssueResponse]
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
    body: ReleaseIssueRequest,
) -> Problem | ReleaseIssueResponse | None:
    """Give back the claim on an issue

     The claim's inverse, and what `bd unclaim` spells. It ends the OWNERSHIP and leaves the work open
    for the next taker: assignee cleared, status the literal `open`, `started_at` cleared, the lease
    dropped, and `revision` reminted so a concurrent reclaim or close conflicts rather than silently
    merging.

    It is a named lifecycle action rather than a `patch`, for `POST /v0/beads/issues/{id}:close`'s
    reason: an update spells a release as three fields at a time, which puts the transition's definition
    in the CALLER, and the lease it drops is the part a patch cannot express at all.

    The caller always names the actor, for `POST /v0/beads/issues/{id}:claim`'s reason. HERE IT IS ALSO
    THE OWNERSHIP FENCE'S SUBJECT: a release carrying neither `expected_assignee` nor `force` succeeds
    only while `actor` is the current holder. That is not authentication — `actor` is caller-asserted
    provenance exactly as it is on the claim, and it is NOT the authenticated principal even where a
    bearer is required: the token a deployment configures admits a client to the whole surface and names
    nobody, so it can neither confirm nor contradict the name in `actor`. The fence is the same anti-
    yank guard the claim gets from refusing a foreign holder, pointed the other way.

    ## NOT IDEMPOTENT, which is the one thing to read before adopting it

    A release over a row that holds NO claim is `409` / `not_releasable`, never a 200. There is no
    `already_released` member here and there must not be one, because the post-state is ANONYMOUS: "I
    released this twice", "a reaper beat me to it" and "nothing ever claimed it" leave the identical row
    — assignee cleared, status open, `started_at` gone — so one 200 would report one answer for three
    situations that want different things from a caller. `claimIssue` can afford `already_claimed: true`
    because a claim's post-state NAMES the claimant, and this operation has nothing left on the row to
    name.

    ON `not_releasable`, READ THE ROW. Do NOT treat the code as "already released", however ordinary
    that case is for you — it is the one shortcut this operation's taxonomy makes unsafe, and it is
    unsafe in the direction that strands work.

    The code covers TWO conditions and publishes no member telling them apart (see the `409` below). One
    of them is "nothing holds this". The other is "the status will not accept a release", which is true
    of a row that is CLOSED and equally true of a row parked in a status this workspace configured — and
    such a row can still be ASSIGNED. A reaper that read this code as success would book a claim as
    dropped while it is still held, by an agent that is already gone, with nothing left to free it. That
    is exactly the stranding this operation resolves ids across both planes to prevent, reintroduced
    through the refusal vocabulary instead of through the id.

    So the recovery is a read: the row tells you which of the two you got — an assignee, or none. That
    is one extra request on an uncommon path, and it is the honest price of one code rather than two. A
    caller that wants the distinction without the read should say so, and it arrives as an ADDITION,
    which the `Problem.code` rules already tell clients to tolerate.

    ## The two ways to release a claim you do not hold

    `expected_assignee` is a compare-and-set on the holder and REPLACES the ownership fence: a caller
    that can name the current holder has demonstrated the view the fence exists to protect, so `actor`
    need not be that holder. It cannot release a claim that has since moved, which is what makes it the
    safer of the two for a supervisor reaping one named agent's abandoned work.

    `force` ignores the holder entirely, and ignores NOTHING ELSE. It does not make an unheld row
    releasable, it does not make a closed one releasable, and it may not accompany `expected_assignee` —
    the two are answers to the same question and they disagree, so sending both is a 400.

    ## Planes

    The id resolves across BOTH planes, unlike `POST /v0/beads/issues/{id}:claim` and like `POST
    /v0/beads/issues/{id}:close`. The asymmetry is about which direction strands work: a wisp can hold a
    claim, so an operation that refused to release one would leave an ephemeral row owned by an agent
    that is gone with no verb able to free it. A release whose target is a wisp records no durable
    history entry.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes
    inside its own transaction, which is what a proxied CLI write does today.

    Args:
        id (str):
        body (ReleaseIssueRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | ReleaseIssueResponse
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
    body: ReleaseIssueRequest,
) -> Response[Problem | ReleaseIssueResponse]:
    """Give back the claim on an issue

     The claim's inverse, and what `bd unclaim` spells. It ends the OWNERSHIP and leaves the work open
    for the next taker: assignee cleared, status the literal `open`, `started_at` cleared, the lease
    dropped, and `revision` reminted so a concurrent reclaim or close conflicts rather than silently
    merging.

    It is a named lifecycle action rather than a `patch`, for `POST /v0/beads/issues/{id}:close`'s
    reason: an update spells a release as three fields at a time, which puts the transition's definition
    in the CALLER, and the lease it drops is the part a patch cannot express at all.

    The caller always names the actor, for `POST /v0/beads/issues/{id}:claim`'s reason. HERE IT IS ALSO
    THE OWNERSHIP FENCE'S SUBJECT: a release carrying neither `expected_assignee` nor `force` succeeds
    only while `actor` is the current holder. That is not authentication — `actor` is caller-asserted
    provenance exactly as it is on the claim, and it is NOT the authenticated principal even where a
    bearer is required: the token a deployment configures admits a client to the whole surface and names
    nobody, so it can neither confirm nor contradict the name in `actor`. The fence is the same anti-
    yank guard the claim gets from refusing a foreign holder, pointed the other way.

    ## NOT IDEMPOTENT, which is the one thing to read before adopting it

    A release over a row that holds NO claim is `409` / `not_releasable`, never a 200. There is no
    `already_released` member here and there must not be one, because the post-state is ANONYMOUS: "I
    released this twice", "a reaper beat me to it" and "nothing ever claimed it" leave the identical row
    — assignee cleared, status open, `started_at` gone — so one 200 would report one answer for three
    situations that want different things from a caller. `claimIssue` can afford `already_claimed: true`
    because a claim's post-state NAMES the claimant, and this operation has nothing left on the row to
    name.

    ON `not_releasable`, READ THE ROW. Do NOT treat the code as "already released", however ordinary
    that case is for you — it is the one shortcut this operation's taxonomy makes unsafe, and it is
    unsafe in the direction that strands work.

    The code covers TWO conditions and publishes no member telling them apart (see the `409` below). One
    of them is "nothing holds this". The other is "the status will not accept a release", which is true
    of a row that is CLOSED and equally true of a row parked in a status this workspace configured — and
    such a row can still be ASSIGNED. A reaper that read this code as success would book a claim as
    dropped while it is still held, by an agent that is already gone, with nothing left to free it. That
    is exactly the stranding this operation resolves ids across both planes to prevent, reintroduced
    through the refusal vocabulary instead of through the id.

    So the recovery is a read: the row tells you which of the two you got — an assignee, or none. That
    is one extra request on an uncommon path, and it is the honest price of one code rather than two. A
    caller that wants the distinction without the read should say so, and it arrives as an ADDITION,
    which the `Problem.code` rules already tell clients to tolerate.

    ## The two ways to release a claim you do not hold

    `expected_assignee` is a compare-and-set on the holder and REPLACES the ownership fence: a caller
    that can name the current holder has demonstrated the view the fence exists to protect, so `actor`
    need not be that holder. It cannot release a claim that has since moved, which is what makes it the
    safer of the two for a supervisor reaping one named agent's abandoned work.

    `force` ignores the holder entirely, and ignores NOTHING ELSE. It does not make an unheld row
    releasable, it does not make a closed one releasable, and it may not accompany `expected_assignee` —
    the two are answers to the same question and they disagree, so sending both is a 400.

    ## Planes

    The id resolves across BOTH planes, unlike `POST /v0/beads/issues/{id}:claim` and like `POST
    /v0/beads/issues/{id}:close`. The asymmetry is about which direction strands work: a wisp can hold a
    claim, so an operation that refused to release one would leave an ephemeral row owned by an agent
    that is gone with no verb able to free it. A release whose target is a wisp records no durable
    history entry.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes
    inside its own transaction, which is what a proxied CLI write does today.

    Args:
        id (str):
        body (ReleaseIssueRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | ReleaseIssueResponse]
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
    body: ReleaseIssueRequest,
) -> Problem | ReleaseIssueResponse | None:
    """Give back the claim on an issue

     The claim's inverse, and what `bd unclaim` spells. It ends the OWNERSHIP and leaves the work open
    for the next taker: assignee cleared, status the literal `open`, `started_at` cleared, the lease
    dropped, and `revision` reminted so a concurrent reclaim or close conflicts rather than silently
    merging.

    It is a named lifecycle action rather than a `patch`, for `POST /v0/beads/issues/{id}:close`'s
    reason: an update spells a release as three fields at a time, which puts the transition's definition
    in the CALLER, and the lease it drops is the part a patch cannot express at all.

    The caller always names the actor, for `POST /v0/beads/issues/{id}:claim`'s reason. HERE IT IS ALSO
    THE OWNERSHIP FENCE'S SUBJECT: a release carrying neither `expected_assignee` nor `force` succeeds
    only while `actor` is the current holder. That is not authentication — `actor` is caller-asserted
    provenance exactly as it is on the claim, and it is NOT the authenticated principal even where a
    bearer is required: the token a deployment configures admits a client to the whole surface and names
    nobody, so it can neither confirm nor contradict the name in `actor`. The fence is the same anti-
    yank guard the claim gets from refusing a foreign holder, pointed the other way.

    ## NOT IDEMPOTENT, which is the one thing to read before adopting it

    A release over a row that holds NO claim is `409` / `not_releasable`, never a 200. There is no
    `already_released` member here and there must not be one, because the post-state is ANONYMOUS: "I
    released this twice", "a reaper beat me to it" and "nothing ever claimed it" leave the identical row
    — assignee cleared, status open, `started_at` gone — so one 200 would report one answer for three
    situations that want different things from a caller. `claimIssue` can afford `already_claimed: true`
    because a claim's post-state NAMES the claimant, and this operation has nothing left on the row to
    name.

    ON `not_releasable`, READ THE ROW. Do NOT treat the code as "already released", however ordinary
    that case is for you — it is the one shortcut this operation's taxonomy makes unsafe, and it is
    unsafe in the direction that strands work.

    The code covers TWO conditions and publishes no member telling them apart (see the `409` below). One
    of them is "nothing holds this". The other is "the status will not accept a release", which is true
    of a row that is CLOSED and equally true of a row parked in a status this workspace configured — and
    such a row can still be ASSIGNED. A reaper that read this code as success would book a claim as
    dropped while it is still held, by an agent that is already gone, with nothing left to free it. That
    is exactly the stranding this operation resolves ids across both planes to prevent, reintroduced
    through the refusal vocabulary instead of through the id.

    So the recovery is a read: the row tells you which of the two you got — an assignee, or none. That
    is one extra request on an uncommon path, and it is the honest price of one code rather than two. A
    caller that wants the distinction without the read should say so, and it arrives as an ADDITION,
    which the `Problem.code` rules already tell clients to tolerate.

    ## The two ways to release a claim you do not hold

    `expected_assignee` is a compare-and-set on the holder and REPLACES the ownership fence: a caller
    that can name the current holder has demonstrated the view the fence exists to protect, so `actor`
    need not be that holder. It cannot release a claim that has since moved, which is what makes it the
    safer of the two for a supervisor reaping one named agent's abandoned work.

    `force` ignores the holder entirely, and ignores NOTHING ELSE. It does not make an unheld row
    releasable, it does not make a closed one releasable, and it may not accompany `expected_assignee` —
    the two are answers to the same question and they disagree, so sending both is a 400.

    ## Planes

    The id resolves across BOTH planes, unlike `POST /v0/beads/issues/{id}:claim` and like `POST
    /v0/beads/issues/{id}:close`. The asymmetry is about which direction strands work: a wisp can hold a
    claim, so an operation that refused to release one would leave an ephemeral row owned by an agent
    that is gone with no verb able to free it. A release whose target is a wisp records no durable
    history entry.

    ## Hooks and auto-commit

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes
    inside its own transaction, which is what a proxied CLI write does today.

    Args:
        id (str):
        body (ReleaseIssueRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | ReleaseIssueResponse
    """

    return (
        await asyncio_detailed(
            id=id,
            client=client,
            body=body,
        )
    ).parsed
