from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.events_page import EventsPage
from ...models.problem import Problem
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    since: int,
    limit: int | Unset = 1000,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["since"] = since

    params["limit"] = limit

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v0/beads/events",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> EventsPage | Problem | None:
    if response.status_code == 200:
        response_200 = EventsPage.from_dict(response.json())

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
) -> Response[EventsPage | Problem]:
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
    limit: int | Unset = 1000,
) -> Response[EventsPage | Problem]:
    """Read the durable events journal

     The workspace's append-only record of every committed issue mutation, paged from a caller-held
    checkpoint. It is the HTTP form of `bd events tail --since`, and it exists so a hosted consumer can
    mirror or replay a workspace without shelling out to the CLI.

    THE RECORDS ARE THE CLI'S RECORDS, byte for byte. `records[]` elements are the same `EventRecord`
    objects `bd events tail` and `bd events export` print one per line, produced by the same projection
    and covered by the same committed golden fixture. A consumer may reconcile an HTTP mirror against a
    CLI export without a translation layer.

    `since` IS THE CURSOR, and it is not one this server minted. It is a sequence number the journal
    itself assigned, gapless and strictly increasing in commit order, so a consumer's position is
    durable across restarts on both sides and means the same thing to the CLI. Read with `since` set to
    the highest `seq` you have DURABLY PROCESSED — not the highest you have received — and advance it
    only after your own write lands, because this server keeps no per-consumer state and cannot
    redeliver.

    THIS OPERATION POLLS; there is no long poll and no `follow` parameter. `head` is what makes polling
    cheap to pace: when the last record's `seq` equals `head` you are caught up and can back off until
    your next interval. A caught-up read is a 200 with an EMPTY `records` array, never a 404 — "nothing
    new yet" is an ordinary answer about a log.

    FOR A PUSH FEED USE `watchEvents`, the sibling operation at `GET /v0/beads/events:watch`, which
    streams the same records from the same `since` as `text/event-stream`. It is a separate operation
    rather than a mode of this one because the two differ in media type, lifetime and capacity; a client
    that streams still needs this operation, because this is the one that is never refused for capacity
    and the one a stream falls back to.

    SCOPE IS PER REPLICA AND PER BRANCH, and this is the part that most often surprises. The journal
    records what THIS clone mutated on the branch its writer commits to. Rows arrive by direct write,
    never by merge, so `bd dolt pull` and the changes a merge settles into this workspace are NOT
    journaled — they arrived as data, and nothing here wrote them through the mutation seam. Each
    replica also has its OWN seq space, counted from its own first mutation: a checkpoint taken against
    one server is meaningless against another, where the same number names a different record and a
    number above that replica's head reads as "caught up" and stalls forever. Track a checkpoint per
    server URL, and re-baseline (a fresh export or full re-read) after a sync rather than carrying one
    across.

    NOT EVERY MUTATION IS COVERED. Raw DML through `bd sql` bypasses the mutation seam and is not
    journaled; nor are the schema migrations and version reconciliation that run while a store is being
    opened, which touch no bead. Dependency records are not symmetric either: `dep_add` is emitted for
    an idempotent same-type re-add that only refreshes edge metadata, so treat it as an upsert of the
    edge rather than proof the edge is new, and a `dep_remove` naming an edge that is already gone emits
    nothing at all.

    THIS OPERATION NEVER DELETES. Retention is the workspace's decision, made by `bd events prune` and
    by the automatic bounding that keeps an enabled journal inside its floors; no prune is reachable
    over HTTP, and reading a record does not acknowledge or release it.

    IT ALSO DEPENDS ON WORKSPACE STATE, alone among the operations here. `events.list` in
    `ContextResponse.capabilities` says this BUILD serves the operation; it does not say this workspace
    has a journal, because the journal is a per-workspace setting that is off by default. A server that
    advertises the capability and answers 409 `events_journal_disabled` to every request is behaving
    correctly. Handle that 409 as "not on this workspace" rather than as a fault, and do not read the
    capability as a promise that records will arrive.

    A workspace that HAS enabled the journal on a storage backend with no journal seam does not reach
    this operation at all: `bd serve` refuses to start, matching the refusal that opening such a
    workspace already produces. Either the server is running and this operation can answer, or the
    operator saw the failure at startup.

    Args:
        since (int):
        limit (int | Unset):  Default: 1000.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EventsPage | Problem]
    """

    kwargs = _get_kwargs(
        since=since,
        limit=limit,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    since: int,
    limit: int | Unset = 1000,
) -> EventsPage | Problem | None:
    """Read the durable events journal

     The workspace's append-only record of every committed issue mutation, paged from a caller-held
    checkpoint. It is the HTTP form of `bd events tail --since`, and it exists so a hosted consumer can
    mirror or replay a workspace without shelling out to the CLI.

    THE RECORDS ARE THE CLI'S RECORDS, byte for byte. `records[]` elements are the same `EventRecord`
    objects `bd events tail` and `bd events export` print one per line, produced by the same projection
    and covered by the same committed golden fixture. A consumer may reconcile an HTTP mirror against a
    CLI export without a translation layer.

    `since` IS THE CURSOR, and it is not one this server minted. It is a sequence number the journal
    itself assigned, gapless and strictly increasing in commit order, so a consumer's position is
    durable across restarts on both sides and means the same thing to the CLI. Read with `since` set to
    the highest `seq` you have DURABLY PROCESSED — not the highest you have received — and advance it
    only after your own write lands, because this server keeps no per-consumer state and cannot
    redeliver.

    THIS OPERATION POLLS; there is no long poll and no `follow` parameter. `head` is what makes polling
    cheap to pace: when the last record's `seq` equals `head` you are caught up and can back off until
    your next interval. A caught-up read is a 200 with an EMPTY `records` array, never a 404 — "nothing
    new yet" is an ordinary answer about a log.

    FOR A PUSH FEED USE `watchEvents`, the sibling operation at `GET /v0/beads/events:watch`, which
    streams the same records from the same `since` as `text/event-stream`. It is a separate operation
    rather than a mode of this one because the two differ in media type, lifetime and capacity; a client
    that streams still needs this operation, because this is the one that is never refused for capacity
    and the one a stream falls back to.

    SCOPE IS PER REPLICA AND PER BRANCH, and this is the part that most often surprises. The journal
    records what THIS clone mutated on the branch its writer commits to. Rows arrive by direct write,
    never by merge, so `bd dolt pull` and the changes a merge settles into this workspace are NOT
    journaled — they arrived as data, and nothing here wrote them through the mutation seam. Each
    replica also has its OWN seq space, counted from its own first mutation: a checkpoint taken against
    one server is meaningless against another, where the same number names a different record and a
    number above that replica's head reads as "caught up" and stalls forever. Track a checkpoint per
    server URL, and re-baseline (a fresh export or full re-read) after a sync rather than carrying one
    across.

    NOT EVERY MUTATION IS COVERED. Raw DML through `bd sql` bypasses the mutation seam and is not
    journaled; nor are the schema migrations and version reconciliation that run while a store is being
    opened, which touch no bead. Dependency records are not symmetric either: `dep_add` is emitted for
    an idempotent same-type re-add that only refreshes edge metadata, so treat it as an upsert of the
    edge rather than proof the edge is new, and a `dep_remove` naming an edge that is already gone emits
    nothing at all.

    THIS OPERATION NEVER DELETES. Retention is the workspace's decision, made by `bd events prune` and
    by the automatic bounding that keeps an enabled journal inside its floors; no prune is reachable
    over HTTP, and reading a record does not acknowledge or release it.

    IT ALSO DEPENDS ON WORKSPACE STATE, alone among the operations here. `events.list` in
    `ContextResponse.capabilities` says this BUILD serves the operation; it does not say this workspace
    has a journal, because the journal is a per-workspace setting that is off by default. A server that
    advertises the capability and answers 409 `events_journal_disabled` to every request is behaving
    correctly. Handle that 409 as "not on this workspace" rather than as a fault, and do not read the
    capability as a promise that records will arrive.

    A workspace that HAS enabled the journal on a storage backend with no journal seam does not reach
    this operation at all: `bd serve` refuses to start, matching the refusal that opening such a
    workspace already produces. Either the server is running and this operation can answer, or the
    operator saw the failure at startup.

    Args:
        since (int):
        limit (int | Unset):  Default: 1000.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EventsPage | Problem
    """

    return sync_detailed(
        client=client,
        since=since,
        limit=limit,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    since: int,
    limit: int | Unset = 1000,
) -> Response[EventsPage | Problem]:
    """Read the durable events journal

     The workspace's append-only record of every committed issue mutation, paged from a caller-held
    checkpoint. It is the HTTP form of `bd events tail --since`, and it exists so a hosted consumer can
    mirror or replay a workspace without shelling out to the CLI.

    THE RECORDS ARE THE CLI'S RECORDS, byte for byte. `records[]` elements are the same `EventRecord`
    objects `bd events tail` and `bd events export` print one per line, produced by the same projection
    and covered by the same committed golden fixture. A consumer may reconcile an HTTP mirror against a
    CLI export without a translation layer.

    `since` IS THE CURSOR, and it is not one this server minted. It is a sequence number the journal
    itself assigned, gapless and strictly increasing in commit order, so a consumer's position is
    durable across restarts on both sides and means the same thing to the CLI. Read with `since` set to
    the highest `seq` you have DURABLY PROCESSED — not the highest you have received — and advance it
    only after your own write lands, because this server keeps no per-consumer state and cannot
    redeliver.

    THIS OPERATION POLLS; there is no long poll and no `follow` parameter. `head` is what makes polling
    cheap to pace: when the last record's `seq` equals `head` you are caught up and can back off until
    your next interval. A caught-up read is a 200 with an EMPTY `records` array, never a 404 — "nothing
    new yet" is an ordinary answer about a log.

    FOR A PUSH FEED USE `watchEvents`, the sibling operation at `GET /v0/beads/events:watch`, which
    streams the same records from the same `since` as `text/event-stream`. It is a separate operation
    rather than a mode of this one because the two differ in media type, lifetime and capacity; a client
    that streams still needs this operation, because this is the one that is never refused for capacity
    and the one a stream falls back to.

    SCOPE IS PER REPLICA AND PER BRANCH, and this is the part that most often surprises. The journal
    records what THIS clone mutated on the branch its writer commits to. Rows arrive by direct write,
    never by merge, so `bd dolt pull` and the changes a merge settles into this workspace are NOT
    journaled — they arrived as data, and nothing here wrote them through the mutation seam. Each
    replica also has its OWN seq space, counted from its own first mutation: a checkpoint taken against
    one server is meaningless against another, where the same number names a different record and a
    number above that replica's head reads as "caught up" and stalls forever. Track a checkpoint per
    server URL, and re-baseline (a fresh export or full re-read) after a sync rather than carrying one
    across.

    NOT EVERY MUTATION IS COVERED. Raw DML through `bd sql` bypasses the mutation seam and is not
    journaled; nor are the schema migrations and version reconciliation that run while a store is being
    opened, which touch no bead. Dependency records are not symmetric either: `dep_add` is emitted for
    an idempotent same-type re-add that only refreshes edge metadata, so treat it as an upsert of the
    edge rather than proof the edge is new, and a `dep_remove` naming an edge that is already gone emits
    nothing at all.

    THIS OPERATION NEVER DELETES. Retention is the workspace's decision, made by `bd events prune` and
    by the automatic bounding that keeps an enabled journal inside its floors; no prune is reachable
    over HTTP, and reading a record does not acknowledge or release it.

    IT ALSO DEPENDS ON WORKSPACE STATE, alone among the operations here. `events.list` in
    `ContextResponse.capabilities` says this BUILD serves the operation; it does not say this workspace
    has a journal, because the journal is a per-workspace setting that is off by default. A server that
    advertises the capability and answers 409 `events_journal_disabled` to every request is behaving
    correctly. Handle that 409 as "not on this workspace" rather than as a fault, and do not read the
    capability as a promise that records will arrive.

    A workspace that HAS enabled the journal on a storage backend with no journal seam does not reach
    this operation at all: `bd serve` refuses to start, matching the refusal that opening such a
    workspace already produces. Either the server is running and this operation can answer, or the
    operator saw the failure at startup.

    Args:
        since (int):
        limit (int | Unset):  Default: 1000.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EventsPage | Problem]
    """

    kwargs = _get_kwargs(
        since=since,
        limit=limit,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    since: int,
    limit: int | Unset = 1000,
) -> EventsPage | Problem | None:
    """Read the durable events journal

     The workspace's append-only record of every committed issue mutation, paged from a caller-held
    checkpoint. It is the HTTP form of `bd events tail --since`, and it exists so a hosted consumer can
    mirror or replay a workspace without shelling out to the CLI.

    THE RECORDS ARE THE CLI'S RECORDS, byte for byte. `records[]` elements are the same `EventRecord`
    objects `bd events tail` and `bd events export` print one per line, produced by the same projection
    and covered by the same committed golden fixture. A consumer may reconcile an HTTP mirror against a
    CLI export without a translation layer.

    `since` IS THE CURSOR, and it is not one this server minted. It is a sequence number the journal
    itself assigned, gapless and strictly increasing in commit order, so a consumer's position is
    durable across restarts on both sides and means the same thing to the CLI. Read with `since` set to
    the highest `seq` you have DURABLY PROCESSED — not the highest you have received — and advance it
    only after your own write lands, because this server keeps no per-consumer state and cannot
    redeliver.

    THIS OPERATION POLLS; there is no long poll and no `follow` parameter. `head` is what makes polling
    cheap to pace: when the last record's `seq` equals `head` you are caught up and can back off until
    your next interval. A caught-up read is a 200 with an EMPTY `records` array, never a 404 — "nothing
    new yet" is an ordinary answer about a log.

    FOR A PUSH FEED USE `watchEvents`, the sibling operation at `GET /v0/beads/events:watch`, which
    streams the same records from the same `since` as `text/event-stream`. It is a separate operation
    rather than a mode of this one because the two differ in media type, lifetime and capacity; a client
    that streams still needs this operation, because this is the one that is never refused for capacity
    and the one a stream falls back to.

    SCOPE IS PER REPLICA AND PER BRANCH, and this is the part that most often surprises. The journal
    records what THIS clone mutated on the branch its writer commits to. Rows arrive by direct write,
    never by merge, so `bd dolt pull` and the changes a merge settles into this workspace are NOT
    journaled — they arrived as data, and nothing here wrote them through the mutation seam. Each
    replica also has its OWN seq space, counted from its own first mutation: a checkpoint taken against
    one server is meaningless against another, where the same number names a different record and a
    number above that replica's head reads as "caught up" and stalls forever. Track a checkpoint per
    server URL, and re-baseline (a fresh export or full re-read) after a sync rather than carrying one
    across.

    NOT EVERY MUTATION IS COVERED. Raw DML through `bd sql` bypasses the mutation seam and is not
    journaled; nor are the schema migrations and version reconciliation that run while a store is being
    opened, which touch no bead. Dependency records are not symmetric either: `dep_add` is emitted for
    an idempotent same-type re-add that only refreshes edge metadata, so treat it as an upsert of the
    edge rather than proof the edge is new, and a `dep_remove` naming an edge that is already gone emits
    nothing at all.

    THIS OPERATION NEVER DELETES. Retention is the workspace's decision, made by `bd events prune` and
    by the automatic bounding that keeps an enabled journal inside its floors; no prune is reachable
    over HTTP, and reading a record does not acknowledge or release it.

    IT ALSO DEPENDS ON WORKSPACE STATE, alone among the operations here. `events.list` in
    `ContextResponse.capabilities` says this BUILD serves the operation; it does not say this workspace
    has a journal, because the journal is a per-workspace setting that is off by default. A server that
    advertises the capability and answers 409 `events_journal_disabled` to every request is behaving
    correctly. Handle that 409 as "not on this workspace" rather than as a fault, and do not read the
    capability as a promise that records will arrive.

    A workspace that HAS enabled the journal on a storage backend with no journal seam does not reach
    this operation at all: `bd serve` refuses to start, matching the refusal that opening such a
    workspace already produces. Either the server is running and this operation can answer, or the
    operator saw the failure at startup.

    Args:
        since (int):
        limit (int | Unset):  Default: 1000.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EventsPage | Problem
    """

    return (
        await asyncio_detailed(
            client=client,
            since=since,
            limit=limit,
        )
    ).parsed
