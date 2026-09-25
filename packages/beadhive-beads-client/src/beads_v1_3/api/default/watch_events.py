from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.problem import Problem
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    since: int,
    last_event_id: int | Unset = UNSET,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}
    if not isinstance(last_event_id, Unset):
        headers["Last-Event-ID"] = str(last_event_id)

    params: dict[str, Any] = {}

    params["since"] = since

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v0/beads/events:watch",
        "params": params,
    }

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Problem | str | None:
    if response.status_code == 200:
        response_200 = response.text
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

    if response.status_code == 410:
        response_410 = Problem.from_dict(response.json())

        return response_410

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
) -> Response[Problem | str]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    since: int,
    last_event_id: int | Unset = UNSET,
) -> Response[Problem | str]:
    """Stream the durable events journal

     The same journal as `listEvents`, PUSHED: a held-open `text/event-stream` response that emits each
    committed mutation as it lands, so a consumer learns about a write when it happens rather than on
    its next interval.

    THE CURSOR IS STILL THE CONTRACT. This is not a subscription — the server keeps no per-consumer
    state, remembers nothing between connections and cannot redeliver. A stream is the reads you would
    have performed yourself, performed on your behalf, and every event carries `id:` set to the record's
    `seq`: the same number `since` takes, the same number `bd events tail --since` takes. Advance your
    own checkpoint only after your write lands, exactly as on the paged read.

    RECONNECTION IS THE NORMAL CASE and it is free. When a stream drops, a client re-requests this
    operation with the standard `Last-Event-ID` header carrying the last `seq` it processed; that header
    WINS over the `since` query parameter, which is what makes a browser's `EventSource` correct without
    any code — it re-sends the original URL, and its original `since` would otherwise replay everything
    since the consumer started. `since` is still required on every connect, because the header is absent
    on the first one.

    WATCH OR POLL is a real choice and the answer is usually poll. A stream costs a connection and a
    goroutine for its whole life, and this server holds a bounded number of them; a poller holds nothing
    between requests and can never be refused for capacity. Stream when the delay between a mutation and
    your reaction is the point — a live mirror, an agent waiting on a gate — and poll for anything that
    can afford its interval. A backlog is drained at read speed either way, so a stream is not a faster
    way to catch up, only a shorter wait once you have.

    THE STREAM ONLY OPENS ON A SERVABLE CURSOR. Every refusal below is an ordinary
    `application/problem+json` response with its documented status, decided BEFORE any stream byte —
    including the 410 for a checkpoint that has been pruned past, which is the same body `listEvents`
    returns for the same condition. There is exactly one failure a client can meet after the status is
    spent: see the `truncated` event.

    EVERYTHING THE PAGED READ SAYS ABOUT THE RECORDS APPLIES UNCHANGED — they are the same `EventRecord`
    objects from the same projection; scope is per replica and per branch, so a checkpoint is meaningful
    only against the server that issued it; merges and `bd sql` are not journaled; and `events.watch` in
    `ContextResponse.capabilities` says this BUILD serves the operation, not that this workspace has a
    journal.

    Args:
        since (int):
        last_event_id (int | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | str]
    """

    kwargs = _get_kwargs(
        since=since,
        last_event_id=last_event_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    since: int,
    last_event_id: int | Unset = UNSET,
) -> Problem | str | None:
    """Stream the durable events journal

     The same journal as `listEvents`, PUSHED: a held-open `text/event-stream` response that emits each
    committed mutation as it lands, so a consumer learns about a write when it happens rather than on
    its next interval.

    THE CURSOR IS STILL THE CONTRACT. This is not a subscription — the server keeps no per-consumer
    state, remembers nothing between connections and cannot redeliver. A stream is the reads you would
    have performed yourself, performed on your behalf, and every event carries `id:` set to the record's
    `seq`: the same number `since` takes, the same number `bd events tail --since` takes. Advance your
    own checkpoint only after your write lands, exactly as on the paged read.

    RECONNECTION IS THE NORMAL CASE and it is free. When a stream drops, a client re-requests this
    operation with the standard `Last-Event-ID` header carrying the last `seq` it processed; that header
    WINS over the `since` query parameter, which is what makes a browser's `EventSource` correct without
    any code — it re-sends the original URL, and its original `since` would otherwise replay everything
    since the consumer started. `since` is still required on every connect, because the header is absent
    on the first one.

    WATCH OR POLL is a real choice and the answer is usually poll. A stream costs a connection and a
    goroutine for its whole life, and this server holds a bounded number of them; a poller holds nothing
    between requests and can never be refused for capacity. Stream when the delay between a mutation and
    your reaction is the point — a live mirror, an agent waiting on a gate — and poll for anything that
    can afford its interval. A backlog is drained at read speed either way, so a stream is not a faster
    way to catch up, only a shorter wait once you have.

    THE STREAM ONLY OPENS ON A SERVABLE CURSOR. Every refusal below is an ordinary
    `application/problem+json` response with its documented status, decided BEFORE any stream byte —
    including the 410 for a checkpoint that has been pruned past, which is the same body `listEvents`
    returns for the same condition. There is exactly one failure a client can meet after the status is
    spent: see the `truncated` event.

    EVERYTHING THE PAGED READ SAYS ABOUT THE RECORDS APPLIES UNCHANGED — they are the same `EventRecord`
    objects from the same projection; scope is per replica and per branch, so a checkpoint is meaningful
    only against the server that issued it; merges and `bd sql` are not journaled; and `events.watch` in
    `ContextResponse.capabilities` says this BUILD serves the operation, not that this workspace has a
    journal.

    Args:
        since (int):
        last_event_id (int | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | str
    """

    return sync_detailed(
        client=client,
        since=since,
        last_event_id=last_event_id,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    since: int,
    last_event_id: int | Unset = UNSET,
) -> Response[Problem | str]:
    """Stream the durable events journal

     The same journal as `listEvents`, PUSHED: a held-open `text/event-stream` response that emits each
    committed mutation as it lands, so a consumer learns about a write when it happens rather than on
    its next interval.

    THE CURSOR IS STILL THE CONTRACT. This is not a subscription — the server keeps no per-consumer
    state, remembers nothing between connections and cannot redeliver. A stream is the reads you would
    have performed yourself, performed on your behalf, and every event carries `id:` set to the record's
    `seq`: the same number `since` takes, the same number `bd events tail --since` takes. Advance your
    own checkpoint only after your write lands, exactly as on the paged read.

    RECONNECTION IS THE NORMAL CASE and it is free. When a stream drops, a client re-requests this
    operation with the standard `Last-Event-ID` header carrying the last `seq` it processed; that header
    WINS over the `since` query parameter, which is what makes a browser's `EventSource` correct without
    any code — it re-sends the original URL, and its original `since` would otherwise replay everything
    since the consumer started. `since` is still required on every connect, because the header is absent
    on the first one.

    WATCH OR POLL is a real choice and the answer is usually poll. A stream costs a connection and a
    goroutine for its whole life, and this server holds a bounded number of them; a poller holds nothing
    between requests and can never be refused for capacity. Stream when the delay between a mutation and
    your reaction is the point — a live mirror, an agent waiting on a gate — and poll for anything that
    can afford its interval. A backlog is drained at read speed either way, so a stream is not a faster
    way to catch up, only a shorter wait once you have.

    THE STREAM ONLY OPENS ON A SERVABLE CURSOR. Every refusal below is an ordinary
    `application/problem+json` response with its documented status, decided BEFORE any stream byte —
    including the 410 for a checkpoint that has been pruned past, which is the same body `listEvents`
    returns for the same condition. There is exactly one failure a client can meet after the status is
    spent: see the `truncated` event.

    EVERYTHING THE PAGED READ SAYS ABOUT THE RECORDS APPLIES UNCHANGED — they are the same `EventRecord`
    objects from the same projection; scope is per replica and per branch, so a checkpoint is meaningful
    only against the server that issued it; merges and `bd sql` are not journaled; and `events.watch` in
    `ContextResponse.capabilities` says this BUILD serves the operation, not that this workspace has a
    journal.

    Args:
        since (int):
        last_event_id (int | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | str]
    """

    kwargs = _get_kwargs(
        since=since,
        last_event_id=last_event_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    since: int,
    last_event_id: int | Unset = UNSET,
) -> Problem | str | None:
    """Stream the durable events journal

     The same journal as `listEvents`, PUSHED: a held-open `text/event-stream` response that emits each
    committed mutation as it lands, so a consumer learns about a write when it happens rather than on
    its next interval.

    THE CURSOR IS STILL THE CONTRACT. This is not a subscription — the server keeps no per-consumer
    state, remembers nothing between connections and cannot redeliver. A stream is the reads you would
    have performed yourself, performed on your behalf, and every event carries `id:` set to the record's
    `seq`: the same number `since` takes, the same number `bd events tail --since` takes. Advance your
    own checkpoint only after your write lands, exactly as on the paged read.

    RECONNECTION IS THE NORMAL CASE and it is free. When a stream drops, a client re-requests this
    operation with the standard `Last-Event-ID` header carrying the last `seq` it processed; that header
    WINS over the `since` query parameter, which is what makes a browser's `EventSource` correct without
    any code — it re-sends the original URL, and its original `since` would otherwise replay everything
    since the consumer started. `since` is still required on every connect, because the header is absent
    on the first one.

    WATCH OR POLL is a real choice and the answer is usually poll. A stream costs a connection and a
    goroutine for its whole life, and this server holds a bounded number of them; a poller holds nothing
    between requests and can never be refused for capacity. Stream when the delay between a mutation and
    your reaction is the point — a live mirror, an agent waiting on a gate — and poll for anything that
    can afford its interval. A backlog is drained at read speed either way, so a stream is not a faster
    way to catch up, only a shorter wait once you have.

    THE STREAM ONLY OPENS ON A SERVABLE CURSOR. Every refusal below is an ordinary
    `application/problem+json` response with its documented status, decided BEFORE any stream byte —
    including the 410 for a checkpoint that has been pruned past, which is the same body `listEvents`
    returns for the same condition. There is exactly one failure a client can meet after the status is
    spent: see the `truncated` event.

    EVERYTHING THE PAGED READ SAYS ABOUT THE RECORDS APPLIES UNCHANGED — they are the same `EventRecord`
    objects from the same projection; scope is per replica and per branch, so a checkpoint is meaningful
    only against the server that issued it; merges and `bd sql` are not journaled; and `events.watch` in
    `ContextResponse.capabilities` says this BUILD serves the operation, not that this workspace has a
    journal.

    Args:
        since (int):
        last_event_id (int | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | str
    """

    return (
        await asyncio_detailed(
            client=client,
            since=since,
            last_event_id=last_event_id,
        )
    ).parsed
