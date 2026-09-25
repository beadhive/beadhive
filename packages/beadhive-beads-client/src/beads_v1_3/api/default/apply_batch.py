from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.apply_batch_request import ApplyBatchRequest
from ...models.apply_batch_response import ApplyBatchResponse
from ...models.problem import Problem
from ...types import Response


def _get_kwargs(
    *,
    body: ApplyBatchRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v0/beads/issues:batchApply",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ApplyBatchResponse | Problem | None:
    if response.status_code == 200:
        response_200 = ApplyBatchResponse.from_dict(response.json())

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
) -> Response[ApplyBatchResponse | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: ApplyBatchRequest,
) -> Response[ApplyBatchResponse | Problem]:
    """Apply an ordered, heterogeneous plan as one act

     Applies an ORDERED list of creates, updates, closes and dependency edges as ONE transaction, or
    applies none of them. It is the operation for a PLAN — create these three issues, wire them to each
    other, close the step that spawned them — which every other write here can only approximate as a
    sequence of calls with a window between each pair.

    A collection-level custom method, spelled the way `issues:sweep` and `issues:delete` are: it acts on
    a set the request DESCRIBES rather than on one addressable resource, and every one of its narrowing
    terms is a body member the server refuses by name if it does not know it.

    ## Order is the contract

    Items apply in declaration order and are NEVER reordered. That is the difference from `POST
    /v0/beads/dependencies:add`, which applies parent-child edges first so the planned hierarchy is
    visible before any blocking edge is validated against it. Reordering is not available here because
    the items are not all edges: "clear the old blockers, then set the new ones" is a sequence, and a
    server that reordered it would apply the clear after the set. What that pass buys there, this
    operation buys with an END GATE instead — after every item has landed, every scheduling edge the
    request added is re-validated against the parent-child closure the WHOLE request produced.
    `skip_per_edge_cycle_check` never drops it.

    ## Names, and which way they reach

    A `create` item may give itself a `key`. Later items address the row it minted with a `Ref` carrying
    that key, and the response's `keys` member maps each key to the id it was bound to — the one fact
    the request cannot carry and every caller needs.

    A KEY REACHES BACKWARD ONLY. A ref used to ADDRESS a row — an update's or a close's `target`, either
    endpoint of an edge — may name a key only if the create item declaring it appears EARLIER in
    `items`. That is forced by what the items do rather than chosen: an update has to see the row it
    patches. A key declared LATER is a `400` carrying `declared_later: true`, which is a different
    diagnosis from a key nothing in the request declares at all — one is an ordering mistake and the
    other is a typo, and a client fixes them differently.

    `create.metadata_refs` IS THE ONE EXCEPTION and may reach forward, or name its own item's key. Every
    id is minted before any splice is applied, so direction cannot matter there; the backward-only rule
    exists to make a TARGET ROW exist before an item touches it, which a metadata VALUE does not need.
    The splice is a SECOND WRITE and says so: the row is created with the metadata the item spelled, and
    the resolved ids are written after every id exists, so a consumer of the event stream sees a create
    and then an update rather than one create carrying values nothing could have known yet.

    ## All or nothing, and what that does to a precondition

    A non-2xx means NOTHING WAS WRITTEN — no id was minted, no edge landed, no row was closed — so a
    client fixes its payload and resends it unchanged. There is no per-item status because there is no
    outcome but the request's: an item whose target is a key an earlier item failed to create has no
    outcome that could be reported.

    THAT IS WHY A PRECONDITION MISS IS A `409` HERE and not an answer. `POST
    /v0/beads/issues/{id}:casMetadata` reports a lost compare-and-set as a 200, because a retry loop is
    its designed caller and a miss is the ordinary path. Here the guarded item is one step of a graph
    the caller meant to land as a unit, so committing the rest would leave a shape nobody asked for.
    `update.expected_version`, `update.expected_status`, `update.expected_assignee` and
    `close.expected_version` therefore refuse the WHOLE request with `precondition_failed`, and the
    problem names the offending item.

    Those guards evaluate AS-MODIFIED: against the row as this request has already changed it at that
    item's position, not against the row as it was when the request began. An item guarding on what an
    earlier item of the same request just wrote is asking a coherent question and gets a coherent
    answer. `expected_version` is the exception and it is a `400`: the token is server-minted and
    rewritten by every write, so mid-request there is no value a caller COULD send, and guarding on a
    row an earlier item already touched is refused before anything is written rather than answered with
    a mismatch the caller would go looking for a concurrent writer to explain.

    ## What lands, and what it costs

    `items` accepts at most 100 entries. The cap bounds how long one request may hold a write
    transaction — it is not a statement about batch semantics, and it is the bound the sibling batch
    operations already run under. Split a larger plan; each request is atomic on its own, but note that
    splitting it changes what the end gate can see, since the gate runs over one request at a time.

    ONE HISTORY ENTRY IS RECORDED FOR THE WHOLE REQUEST, attributed to `actor`, and none at all when
    nothing durable landed — a request made entirely of ephemeral items writes only to unversioned
    tables. `provenance` labels that entry: it changes how the entry READS, never whether one is
    recorded, and an empty one composes a default naming how many items of each kind landed rather than
    every id, since an entry listing a hundred is the diff written twice.

    EPHEMERALITY IS PER ITEM, exactly as it is for `POST /v0/beads/issues:batchCreate`: one request may
    create durable issues and ephemeral ones together. The two planes hold their edges in different
    tables, so a `dep_add` BETWEEN two rows this request creates on opposite planes is refused with
    everything else the request asked for.

    IT IS NOT IDEMPOTENT AND CARRIES NO IDEMPOTENCY KEY. Replaying a request applies it again — the
    creates mint new ids, the edges are idempotent, the closes are no-ops. A caller that needs a replay
    record makes it an ITEM of the batch, so the record lands or rolls back with the work it describes;
    a key on the request would be a second, weaker mechanism for the same thing.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (ApplyBatchRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApplyBatchResponse | Problem]
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
    body: ApplyBatchRequest,
) -> ApplyBatchResponse | Problem | None:
    """Apply an ordered, heterogeneous plan as one act

     Applies an ORDERED list of creates, updates, closes and dependency edges as ONE transaction, or
    applies none of them. It is the operation for a PLAN — create these three issues, wire them to each
    other, close the step that spawned them — which every other write here can only approximate as a
    sequence of calls with a window between each pair.

    A collection-level custom method, spelled the way `issues:sweep` and `issues:delete` are: it acts on
    a set the request DESCRIBES rather than on one addressable resource, and every one of its narrowing
    terms is a body member the server refuses by name if it does not know it.

    ## Order is the contract

    Items apply in declaration order and are NEVER reordered. That is the difference from `POST
    /v0/beads/dependencies:add`, which applies parent-child edges first so the planned hierarchy is
    visible before any blocking edge is validated against it. Reordering is not available here because
    the items are not all edges: "clear the old blockers, then set the new ones" is a sequence, and a
    server that reordered it would apply the clear after the set. What that pass buys there, this
    operation buys with an END GATE instead — after every item has landed, every scheduling edge the
    request added is re-validated against the parent-child closure the WHOLE request produced.
    `skip_per_edge_cycle_check` never drops it.

    ## Names, and which way they reach

    A `create` item may give itself a `key`. Later items address the row it minted with a `Ref` carrying
    that key, and the response's `keys` member maps each key to the id it was bound to — the one fact
    the request cannot carry and every caller needs.

    A KEY REACHES BACKWARD ONLY. A ref used to ADDRESS a row — an update's or a close's `target`, either
    endpoint of an edge — may name a key only if the create item declaring it appears EARLIER in
    `items`. That is forced by what the items do rather than chosen: an update has to see the row it
    patches. A key declared LATER is a `400` carrying `declared_later: true`, which is a different
    diagnosis from a key nothing in the request declares at all — one is an ordering mistake and the
    other is a typo, and a client fixes them differently.

    `create.metadata_refs` IS THE ONE EXCEPTION and may reach forward, or name its own item's key. Every
    id is minted before any splice is applied, so direction cannot matter there; the backward-only rule
    exists to make a TARGET ROW exist before an item touches it, which a metadata VALUE does not need.
    The splice is a SECOND WRITE and says so: the row is created with the metadata the item spelled, and
    the resolved ids are written after every id exists, so a consumer of the event stream sees a create
    and then an update rather than one create carrying values nothing could have known yet.

    ## All or nothing, and what that does to a precondition

    A non-2xx means NOTHING WAS WRITTEN — no id was minted, no edge landed, no row was closed — so a
    client fixes its payload and resends it unchanged. There is no per-item status because there is no
    outcome but the request's: an item whose target is a key an earlier item failed to create has no
    outcome that could be reported.

    THAT IS WHY A PRECONDITION MISS IS A `409` HERE and not an answer. `POST
    /v0/beads/issues/{id}:casMetadata` reports a lost compare-and-set as a 200, because a retry loop is
    its designed caller and a miss is the ordinary path. Here the guarded item is one step of a graph
    the caller meant to land as a unit, so committing the rest would leave a shape nobody asked for.
    `update.expected_version`, `update.expected_status`, `update.expected_assignee` and
    `close.expected_version` therefore refuse the WHOLE request with `precondition_failed`, and the
    problem names the offending item.

    Those guards evaluate AS-MODIFIED: against the row as this request has already changed it at that
    item's position, not against the row as it was when the request began. An item guarding on what an
    earlier item of the same request just wrote is asking a coherent question and gets a coherent
    answer. `expected_version` is the exception and it is a `400`: the token is server-minted and
    rewritten by every write, so mid-request there is no value a caller COULD send, and guarding on a
    row an earlier item already touched is refused before anything is written rather than answered with
    a mismatch the caller would go looking for a concurrent writer to explain.

    ## What lands, and what it costs

    `items` accepts at most 100 entries. The cap bounds how long one request may hold a write
    transaction — it is not a statement about batch semantics, and it is the bound the sibling batch
    operations already run under. Split a larger plan; each request is atomic on its own, but note that
    splitting it changes what the end gate can see, since the gate runs over one request at a time.

    ONE HISTORY ENTRY IS RECORDED FOR THE WHOLE REQUEST, attributed to `actor`, and none at all when
    nothing durable landed — a request made entirely of ephemeral items writes only to unversioned
    tables. `provenance` labels that entry: it changes how the entry READS, never whether one is
    recorded, and an empty one composes a default naming how many items of each kind landed rather than
    every id, since an entry listing a hundred is the diff written twice.

    EPHEMERALITY IS PER ITEM, exactly as it is for `POST /v0/beads/issues:batchCreate`: one request may
    create durable issues and ephemeral ones together. The two planes hold their edges in different
    tables, so a `dep_add` BETWEEN two rows this request creates on opposite planes is refused with
    everything else the request asked for.

    IT IS NOT IDEMPOTENT AND CARRIES NO IDEMPOTENCY KEY. Replaying a request applies it again — the
    creates mint new ids, the edges are idempotent, the closes are no-ops. A caller that needs a replay
    record makes it an ITEM of the batch, so the record lands or rolls back with the work it describes;
    a key on the request would be a second, weaker mechanism for the same thing.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (ApplyBatchRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApplyBatchResponse | Problem
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: ApplyBatchRequest,
) -> Response[ApplyBatchResponse | Problem]:
    """Apply an ordered, heterogeneous plan as one act

     Applies an ORDERED list of creates, updates, closes and dependency edges as ONE transaction, or
    applies none of them. It is the operation for a PLAN — create these three issues, wire them to each
    other, close the step that spawned them — which every other write here can only approximate as a
    sequence of calls with a window between each pair.

    A collection-level custom method, spelled the way `issues:sweep` and `issues:delete` are: it acts on
    a set the request DESCRIBES rather than on one addressable resource, and every one of its narrowing
    terms is a body member the server refuses by name if it does not know it.

    ## Order is the contract

    Items apply in declaration order and are NEVER reordered. That is the difference from `POST
    /v0/beads/dependencies:add`, which applies parent-child edges first so the planned hierarchy is
    visible before any blocking edge is validated against it. Reordering is not available here because
    the items are not all edges: "clear the old blockers, then set the new ones" is a sequence, and a
    server that reordered it would apply the clear after the set. What that pass buys there, this
    operation buys with an END GATE instead — after every item has landed, every scheduling edge the
    request added is re-validated against the parent-child closure the WHOLE request produced.
    `skip_per_edge_cycle_check` never drops it.

    ## Names, and which way they reach

    A `create` item may give itself a `key`. Later items address the row it minted with a `Ref` carrying
    that key, and the response's `keys` member maps each key to the id it was bound to — the one fact
    the request cannot carry and every caller needs.

    A KEY REACHES BACKWARD ONLY. A ref used to ADDRESS a row — an update's or a close's `target`, either
    endpoint of an edge — may name a key only if the create item declaring it appears EARLIER in
    `items`. That is forced by what the items do rather than chosen: an update has to see the row it
    patches. A key declared LATER is a `400` carrying `declared_later: true`, which is a different
    diagnosis from a key nothing in the request declares at all — one is an ordering mistake and the
    other is a typo, and a client fixes them differently.

    `create.metadata_refs` IS THE ONE EXCEPTION and may reach forward, or name its own item's key. Every
    id is minted before any splice is applied, so direction cannot matter there; the backward-only rule
    exists to make a TARGET ROW exist before an item touches it, which a metadata VALUE does not need.
    The splice is a SECOND WRITE and says so: the row is created with the metadata the item spelled, and
    the resolved ids are written after every id exists, so a consumer of the event stream sees a create
    and then an update rather than one create carrying values nothing could have known yet.

    ## All or nothing, and what that does to a precondition

    A non-2xx means NOTHING WAS WRITTEN — no id was minted, no edge landed, no row was closed — so a
    client fixes its payload and resends it unchanged. There is no per-item status because there is no
    outcome but the request's: an item whose target is a key an earlier item failed to create has no
    outcome that could be reported.

    THAT IS WHY A PRECONDITION MISS IS A `409` HERE and not an answer. `POST
    /v0/beads/issues/{id}:casMetadata` reports a lost compare-and-set as a 200, because a retry loop is
    its designed caller and a miss is the ordinary path. Here the guarded item is one step of a graph
    the caller meant to land as a unit, so committing the rest would leave a shape nobody asked for.
    `update.expected_version`, `update.expected_status`, `update.expected_assignee` and
    `close.expected_version` therefore refuse the WHOLE request with `precondition_failed`, and the
    problem names the offending item.

    Those guards evaluate AS-MODIFIED: against the row as this request has already changed it at that
    item's position, not against the row as it was when the request began. An item guarding on what an
    earlier item of the same request just wrote is asking a coherent question and gets a coherent
    answer. `expected_version` is the exception and it is a `400`: the token is server-minted and
    rewritten by every write, so mid-request there is no value a caller COULD send, and guarding on a
    row an earlier item already touched is refused before anything is written rather than answered with
    a mismatch the caller would go looking for a concurrent writer to explain.

    ## What lands, and what it costs

    `items` accepts at most 100 entries. The cap bounds how long one request may hold a write
    transaction — it is not a statement about batch semantics, and it is the bound the sibling batch
    operations already run under. Split a larger plan; each request is atomic on its own, but note that
    splitting it changes what the end gate can see, since the gate runs over one request at a time.

    ONE HISTORY ENTRY IS RECORDED FOR THE WHOLE REQUEST, attributed to `actor`, and none at all when
    nothing durable landed — a request made entirely of ephemeral items writes only to unversioned
    tables. `provenance` labels that entry: it changes how the entry READS, never whether one is
    recorded, and an empty one composes a default naming how many items of each kind landed rather than
    every id, since an entry listing a hundred is the diff written twice.

    EPHEMERALITY IS PER ITEM, exactly as it is for `POST /v0/beads/issues:batchCreate`: one request may
    create durable issues and ephemeral ones together. The two planes hold their edges in different
    tables, so a `dep_add` BETWEEN two rows this request creates on opposite planes is refused with
    everything else the request asked for.

    IT IS NOT IDEMPOTENT AND CARRIES NO IDEMPOTENCY KEY. Replaying a request applies it again — the
    creates mint new ids, the edges are idempotent, the closes are no-ops. A caller that needs a replay
    record makes it an ITEM of the batch, so the record lands or rolls back with the work it describes;
    a key on the request would be a second, weaker mechanism for the same thing.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (ApplyBatchRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApplyBatchResponse | Problem]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    body: ApplyBatchRequest,
) -> ApplyBatchResponse | Problem | None:
    """Apply an ordered, heterogeneous plan as one act

     Applies an ORDERED list of creates, updates, closes and dependency edges as ONE transaction, or
    applies none of them. It is the operation for a PLAN — create these three issues, wire them to each
    other, close the step that spawned them — which every other write here can only approximate as a
    sequence of calls with a window between each pair.

    A collection-level custom method, spelled the way `issues:sweep` and `issues:delete` are: it acts on
    a set the request DESCRIBES rather than on one addressable resource, and every one of its narrowing
    terms is a body member the server refuses by name if it does not know it.

    ## Order is the contract

    Items apply in declaration order and are NEVER reordered. That is the difference from `POST
    /v0/beads/dependencies:add`, which applies parent-child edges first so the planned hierarchy is
    visible before any blocking edge is validated against it. Reordering is not available here because
    the items are not all edges: "clear the old blockers, then set the new ones" is a sequence, and a
    server that reordered it would apply the clear after the set. What that pass buys there, this
    operation buys with an END GATE instead — after every item has landed, every scheduling edge the
    request added is re-validated against the parent-child closure the WHOLE request produced.
    `skip_per_edge_cycle_check` never drops it.

    ## Names, and which way they reach

    A `create` item may give itself a `key`. Later items address the row it minted with a `Ref` carrying
    that key, and the response's `keys` member maps each key to the id it was bound to — the one fact
    the request cannot carry and every caller needs.

    A KEY REACHES BACKWARD ONLY. A ref used to ADDRESS a row — an update's or a close's `target`, either
    endpoint of an edge — may name a key only if the create item declaring it appears EARLIER in
    `items`. That is forced by what the items do rather than chosen: an update has to see the row it
    patches. A key declared LATER is a `400` carrying `declared_later: true`, which is a different
    diagnosis from a key nothing in the request declares at all — one is an ordering mistake and the
    other is a typo, and a client fixes them differently.

    `create.metadata_refs` IS THE ONE EXCEPTION and may reach forward, or name its own item's key. Every
    id is minted before any splice is applied, so direction cannot matter there; the backward-only rule
    exists to make a TARGET ROW exist before an item touches it, which a metadata VALUE does not need.
    The splice is a SECOND WRITE and says so: the row is created with the metadata the item spelled, and
    the resolved ids are written after every id exists, so a consumer of the event stream sees a create
    and then an update rather than one create carrying values nothing could have known yet.

    ## All or nothing, and what that does to a precondition

    A non-2xx means NOTHING WAS WRITTEN — no id was minted, no edge landed, no row was closed — so a
    client fixes its payload and resends it unchanged. There is no per-item status because there is no
    outcome but the request's: an item whose target is a key an earlier item failed to create has no
    outcome that could be reported.

    THAT IS WHY A PRECONDITION MISS IS A `409` HERE and not an answer. `POST
    /v0/beads/issues/{id}:casMetadata` reports a lost compare-and-set as a 200, because a retry loop is
    its designed caller and a miss is the ordinary path. Here the guarded item is one step of a graph
    the caller meant to land as a unit, so committing the rest would leave a shape nobody asked for.
    `update.expected_version`, `update.expected_status`, `update.expected_assignee` and
    `close.expected_version` therefore refuse the WHOLE request with `precondition_failed`, and the
    problem names the offending item.

    Those guards evaluate AS-MODIFIED: against the row as this request has already changed it at that
    item's position, not against the row as it was when the request began. An item guarding on what an
    earlier item of the same request just wrote is asking a coherent question and gets a coherent
    answer. `expected_version` is the exception and it is a `400`: the token is server-minted and
    rewritten by every write, so mid-request there is no value a caller COULD send, and guarding on a
    row an earlier item already touched is refused before anything is written rather than answered with
    a mismatch the caller would go looking for a concurrent writer to explain.

    ## What lands, and what it costs

    `items` accepts at most 100 entries. The cap bounds how long one request may hold a write
    transaction — it is not a statement about batch semantics, and it is the bound the sibling batch
    operations already run under. Split a larger plan; each request is atomic on its own, but note that
    splitting it changes what the end gate can see, since the gate runs over one request at a time.

    ONE HISTORY ENTRY IS RECORDED FOR THE WHOLE REQUEST, attributed to `actor`, and none at all when
    nothing durable landed — a request made entirely of ephemeral items writes only to unversioned
    tables. `provenance` labels that entry: it changes how the entry READS, never whether one is
    recorded, and an empty one composes a default naming how many items of each kind landed rather than
    every id, since an entry listing a hundred is the diff written twice.

    EPHEMERALITY IS PER ITEM, exactly as it is for `POST /v0/beads/issues:batchCreate`: one request may
    create durable issues and ephemeral ones together. The two planes hold their edges in different
    tables, so a `dep_add` BETWEEN two rows this request creates on opposite planes is refused with
    everything else the request asked for.

    IT IS NOT IDEMPOTENT AND CARRIES NO IDEMPOTENCY KEY. Replaying a request applies it again — the
    creates mint new ids, the edges are idempotent, the closes are no-ops. A caller that needs a replay
    record makes it an ITEM of the batch, so the record lands or rolls back with the work it describes;
    a key on the request would be a second, weaker mechanism for the same thing.

    Hooks do not fire and the per-command auto-commit machinery does not run, exactly as for `POST
    /v0/beads/issues/{id}:claim`. The only durable effect is the single storage commit the role makes in
    its own transaction.

    Args:
        body (ApplyBatchRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApplyBatchResponse | Problem
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
