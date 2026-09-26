from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.compare_and_set_metadata_request import CompareAndSetMetadataRequest
from ...models.compare_and_set_metadata_response import CompareAndSetMetadataResponse
from ...models.problem import Problem
from ...types import Response


def _get_kwargs(
    id: str,
    *,
    body: CompareAndSetMetadataRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v0/beads/issues/{id}:casMetadata".format(
            id=quote(str(id), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> CompareAndSetMetadataResponse | Problem | None:
    if response.status_code == 200:
        response_200 = CompareAndSetMetadataResponse.from_dict(response.json())

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
) -> Response[CompareAndSetMetadataResponse | Problem]:
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
    body: CompareAndSetMetadataRequest,
) -> Response[CompareAndSetMetadataResponse | Problem]:
    """Conditionally set one metadata key on an issue

     Sets `metadata[key]` if and only if it currently holds `expected`, and reports what it found either
    way. It is the conditional write every coordination protocol over the metadata plane is built from,
    and the one operation on this surface a client is expected to call in a LOOP.

    THE TRANSITION IS A PAIR. `expected` is the key's value before, `value` is its value after, and
    OMITTING either member means the key is ABSENT there. So a first-writer-wins acquire omits
    `expected`, a release omits `value`, and an ordinary hand-off carries both. A member present with
    the JSON value `null` is a real value and does NOT mean absent: a key stored holding null exists,
    and the server can tell the two apart.

    EQUALITY IS CANONICAL. Two JSON values match when their canonical encodings match, so insignificant
    whitespace is ignored and object keys compare as a set rather than in the order they were written —
    a client cannot lose a swap to its own serializer.

    NUMBERS COMPARE AS THEIR SOURCE LITERAL, so `1` and `1.0` do not match, and that is a CONSTRAINT ON
    CLIENTS rather than a precision guarantee. The metadata store decodes JSON numbers through a float
    and re-emits them, so a number is not always stored as it was sent: `1.0` is stored as `1`, an
    integer past 2^53 is rounded, and `1e300` is stored as three hundred and one digits. COMPOSE
    `expected` FROM A PREVIOUS `current`, never from your own spelling of a number — `current` is the
    value the row HOLDS, so a loop that feeds it back converges while a loop that re-sends a
    renormalized literal is refused forever. For a coordination token, prefer a STRING: it round-trips
    byte for byte.

    SERIALIZERS OMIT NULLS BY DEFAULT, AND HERE THAT CHANGES THE REQUEST. Omitting `value` is a DELETE,
    not "leave the value alone" — so a client that builds this body from a struct under Jackson's
    NON_NULL, Pydantic's `exclude_none`, or Go's `omitempty` will send a delete when it meant to write
    null. Send the members explicitly, and check what your serializer does with a null before you rely
    on either meaning.

    A REFUSED SWAP IS A 200, not a 409. `swapped` is false and `current` carries the value that refused
    it, which is what lets a client recompute and retry without a second read that could itself go
    stale. A lost race is the ordinary path here rather than an exceptional one, and a client that
    treats this operation's non-2xx codes as "the swap did not happen" would be wrong in both
    directions. DISPATCH ON `swapped`.

    A SUCCESSFUL SWAP THAT CHANGES NOTHING WRITES NOTHING: when the precondition holds over a value
    already equal to `value`, `swapped` is true, `current` is that value, and no row and no history
    entry is touched. `swapped` answers the PRECONDITION; it does not claim a write happened.

    `current` IS ALWAYS THE VALUE THE ROW HOLDS, read inside the transaction that decided — on a
    refusal, on a swap that landed, and on a swap that changed nothing. It is not an echo of what was
    sent, so where the store renormalized a value on the way in, `current` reports the store's form. A
    member present holding `null` is a value; an ABSENT `current` member means the key is absent.

    Sibling keys survive. The read, the comparison and the write share one transaction, so a concurrent
    write to a DIFFERENT key of the same issue is preserved rather than clobbered.

    The id resolves across both planes, so a swap whose target is a wisp lands on the unversioned plane
    and records no durable history entry.

    An issue whose stored metadata is not a JSON object answers 500 rather than 400 or 404, and that is
    deliberate: the request was well-formed and the issue exists, so both client-error codes would be
    something a caller could act on. The row is corrupt and no retry converges.

    Args:
        id (str):
        body (CompareAndSetMetadataRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CompareAndSetMetadataResponse | Problem]
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
    body: CompareAndSetMetadataRequest,
) -> CompareAndSetMetadataResponse | Problem | None:
    """Conditionally set one metadata key on an issue

     Sets `metadata[key]` if and only if it currently holds `expected`, and reports what it found either
    way. It is the conditional write every coordination protocol over the metadata plane is built from,
    and the one operation on this surface a client is expected to call in a LOOP.

    THE TRANSITION IS A PAIR. `expected` is the key's value before, `value` is its value after, and
    OMITTING either member means the key is ABSENT there. So a first-writer-wins acquire omits
    `expected`, a release omits `value`, and an ordinary hand-off carries both. A member present with
    the JSON value `null` is a real value and does NOT mean absent: a key stored holding null exists,
    and the server can tell the two apart.

    EQUALITY IS CANONICAL. Two JSON values match when their canonical encodings match, so insignificant
    whitespace is ignored and object keys compare as a set rather than in the order they were written —
    a client cannot lose a swap to its own serializer.

    NUMBERS COMPARE AS THEIR SOURCE LITERAL, so `1` and `1.0` do not match, and that is a CONSTRAINT ON
    CLIENTS rather than a precision guarantee. The metadata store decodes JSON numbers through a float
    and re-emits them, so a number is not always stored as it was sent: `1.0` is stored as `1`, an
    integer past 2^53 is rounded, and `1e300` is stored as three hundred and one digits. COMPOSE
    `expected` FROM A PREVIOUS `current`, never from your own spelling of a number — `current` is the
    value the row HOLDS, so a loop that feeds it back converges while a loop that re-sends a
    renormalized literal is refused forever. For a coordination token, prefer a STRING: it round-trips
    byte for byte.

    SERIALIZERS OMIT NULLS BY DEFAULT, AND HERE THAT CHANGES THE REQUEST. Omitting `value` is a DELETE,
    not "leave the value alone" — so a client that builds this body from a struct under Jackson's
    NON_NULL, Pydantic's `exclude_none`, or Go's `omitempty` will send a delete when it meant to write
    null. Send the members explicitly, and check what your serializer does with a null before you rely
    on either meaning.

    A REFUSED SWAP IS A 200, not a 409. `swapped` is false and `current` carries the value that refused
    it, which is what lets a client recompute and retry without a second read that could itself go
    stale. A lost race is the ordinary path here rather than an exceptional one, and a client that
    treats this operation's non-2xx codes as "the swap did not happen" would be wrong in both
    directions. DISPATCH ON `swapped`.

    A SUCCESSFUL SWAP THAT CHANGES NOTHING WRITES NOTHING: when the precondition holds over a value
    already equal to `value`, `swapped` is true, `current` is that value, and no row and no history
    entry is touched. `swapped` answers the PRECONDITION; it does not claim a write happened.

    `current` IS ALWAYS THE VALUE THE ROW HOLDS, read inside the transaction that decided — on a
    refusal, on a swap that landed, and on a swap that changed nothing. It is not an echo of what was
    sent, so where the store renormalized a value on the way in, `current` reports the store's form. A
    member present holding `null` is a value; an ABSENT `current` member means the key is absent.

    Sibling keys survive. The read, the comparison and the write share one transaction, so a concurrent
    write to a DIFFERENT key of the same issue is preserved rather than clobbered.

    The id resolves across both planes, so a swap whose target is a wisp lands on the unversioned plane
    and records no durable history entry.

    An issue whose stored metadata is not a JSON object answers 500 rather than 400 or 404, and that is
    deliberate: the request was well-formed and the issue exists, so both client-error codes would be
    something a caller could act on. The row is corrupt and no retry converges.

    Args:
        id (str):
        body (CompareAndSetMetadataRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CompareAndSetMetadataResponse | Problem
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
    body: CompareAndSetMetadataRequest,
) -> Response[CompareAndSetMetadataResponse | Problem]:
    """Conditionally set one metadata key on an issue

     Sets `metadata[key]` if and only if it currently holds `expected`, and reports what it found either
    way. It is the conditional write every coordination protocol over the metadata plane is built from,
    and the one operation on this surface a client is expected to call in a LOOP.

    THE TRANSITION IS A PAIR. `expected` is the key's value before, `value` is its value after, and
    OMITTING either member means the key is ABSENT there. So a first-writer-wins acquire omits
    `expected`, a release omits `value`, and an ordinary hand-off carries both. A member present with
    the JSON value `null` is a real value and does NOT mean absent: a key stored holding null exists,
    and the server can tell the two apart.

    EQUALITY IS CANONICAL. Two JSON values match when their canonical encodings match, so insignificant
    whitespace is ignored and object keys compare as a set rather than in the order they were written —
    a client cannot lose a swap to its own serializer.

    NUMBERS COMPARE AS THEIR SOURCE LITERAL, so `1` and `1.0` do not match, and that is a CONSTRAINT ON
    CLIENTS rather than a precision guarantee. The metadata store decodes JSON numbers through a float
    and re-emits them, so a number is not always stored as it was sent: `1.0` is stored as `1`, an
    integer past 2^53 is rounded, and `1e300` is stored as three hundred and one digits. COMPOSE
    `expected` FROM A PREVIOUS `current`, never from your own spelling of a number — `current` is the
    value the row HOLDS, so a loop that feeds it back converges while a loop that re-sends a
    renormalized literal is refused forever. For a coordination token, prefer a STRING: it round-trips
    byte for byte.

    SERIALIZERS OMIT NULLS BY DEFAULT, AND HERE THAT CHANGES THE REQUEST. Omitting `value` is a DELETE,
    not "leave the value alone" — so a client that builds this body from a struct under Jackson's
    NON_NULL, Pydantic's `exclude_none`, or Go's `omitempty` will send a delete when it meant to write
    null. Send the members explicitly, and check what your serializer does with a null before you rely
    on either meaning.

    A REFUSED SWAP IS A 200, not a 409. `swapped` is false and `current` carries the value that refused
    it, which is what lets a client recompute and retry without a second read that could itself go
    stale. A lost race is the ordinary path here rather than an exceptional one, and a client that
    treats this operation's non-2xx codes as "the swap did not happen" would be wrong in both
    directions. DISPATCH ON `swapped`.

    A SUCCESSFUL SWAP THAT CHANGES NOTHING WRITES NOTHING: when the precondition holds over a value
    already equal to `value`, `swapped` is true, `current` is that value, and no row and no history
    entry is touched. `swapped` answers the PRECONDITION; it does not claim a write happened.

    `current` IS ALWAYS THE VALUE THE ROW HOLDS, read inside the transaction that decided — on a
    refusal, on a swap that landed, and on a swap that changed nothing. It is not an echo of what was
    sent, so where the store renormalized a value on the way in, `current` reports the store's form. A
    member present holding `null` is a value; an ABSENT `current` member means the key is absent.

    Sibling keys survive. The read, the comparison and the write share one transaction, so a concurrent
    write to a DIFFERENT key of the same issue is preserved rather than clobbered.

    The id resolves across both planes, so a swap whose target is a wisp lands on the unversioned plane
    and records no durable history entry.

    An issue whose stored metadata is not a JSON object answers 500 rather than 400 or 404, and that is
    deliberate: the request was well-formed and the issue exists, so both client-error codes would be
    something a caller could act on. The row is corrupt and no retry converges.

    Args:
        id (str):
        body (CompareAndSetMetadataRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CompareAndSetMetadataResponse | Problem]
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
    body: CompareAndSetMetadataRequest,
) -> CompareAndSetMetadataResponse | Problem | None:
    """Conditionally set one metadata key on an issue

     Sets `metadata[key]` if and only if it currently holds `expected`, and reports what it found either
    way. It is the conditional write every coordination protocol over the metadata plane is built from,
    and the one operation on this surface a client is expected to call in a LOOP.

    THE TRANSITION IS A PAIR. `expected` is the key's value before, `value` is its value after, and
    OMITTING either member means the key is ABSENT there. So a first-writer-wins acquire omits
    `expected`, a release omits `value`, and an ordinary hand-off carries both. A member present with
    the JSON value `null` is a real value and does NOT mean absent: a key stored holding null exists,
    and the server can tell the two apart.

    EQUALITY IS CANONICAL. Two JSON values match when their canonical encodings match, so insignificant
    whitespace is ignored and object keys compare as a set rather than in the order they were written —
    a client cannot lose a swap to its own serializer.

    NUMBERS COMPARE AS THEIR SOURCE LITERAL, so `1` and `1.0` do not match, and that is a CONSTRAINT ON
    CLIENTS rather than a precision guarantee. The metadata store decodes JSON numbers through a float
    and re-emits them, so a number is not always stored as it was sent: `1.0` is stored as `1`, an
    integer past 2^53 is rounded, and `1e300` is stored as three hundred and one digits. COMPOSE
    `expected` FROM A PREVIOUS `current`, never from your own spelling of a number — `current` is the
    value the row HOLDS, so a loop that feeds it back converges while a loop that re-sends a
    renormalized literal is refused forever. For a coordination token, prefer a STRING: it round-trips
    byte for byte.

    SERIALIZERS OMIT NULLS BY DEFAULT, AND HERE THAT CHANGES THE REQUEST. Omitting `value` is a DELETE,
    not "leave the value alone" — so a client that builds this body from a struct under Jackson's
    NON_NULL, Pydantic's `exclude_none`, or Go's `omitempty` will send a delete when it meant to write
    null. Send the members explicitly, and check what your serializer does with a null before you rely
    on either meaning.

    A REFUSED SWAP IS A 200, not a 409. `swapped` is false and `current` carries the value that refused
    it, which is what lets a client recompute and retry without a second read that could itself go
    stale. A lost race is the ordinary path here rather than an exceptional one, and a client that
    treats this operation's non-2xx codes as "the swap did not happen" would be wrong in both
    directions. DISPATCH ON `swapped`.

    A SUCCESSFUL SWAP THAT CHANGES NOTHING WRITES NOTHING: when the precondition holds over a value
    already equal to `value`, `swapped` is true, `current` is that value, and no row and no history
    entry is touched. `swapped` answers the PRECONDITION; it does not claim a write happened.

    `current` IS ALWAYS THE VALUE THE ROW HOLDS, read inside the transaction that decided — on a
    refusal, on a swap that landed, and on a swap that changed nothing. It is not an echo of what was
    sent, so where the store renormalized a value on the way in, `current` reports the store's form. A
    member present holding `null` is a value; an ABSENT `current` member means the key is absent.

    Sibling keys survive. The read, the comparison and the write share one transaction, so a concurrent
    write to a DIFFERENT key of the same issue is preserved rather than clobbered.

    The id resolves across both planes, so a swap whose target is a wisp lands on the unversioned plane
    and records no durable history entry.

    An issue whose stored metadata is not a JSON object answers 500 rather than 400 or 404, and that is
    deliberate: the request was well-formed and the issue exists, so both client-error codes would be
    something a caller could act on. The row is corrupt and no retry converges.

    Args:
        id (str):
        body (CompareAndSetMetadataRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CompareAndSetMetadataResponse | Problem
    """

    return (
        await asyncio_detailed(
            id=id,
            client=client,
            body=body,
        )
    ).parsed
