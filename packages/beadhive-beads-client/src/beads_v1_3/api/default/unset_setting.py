from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.problem import Problem
from ...models.removed_setting import RemovedSetting
from ...types import Response


def _get_kwargs(
    key: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "delete",
        "url": "/v0/beads/config/{key}".format(
            key=quote(str(key), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Problem | RemovedSetting | None:
    if response.status_code == 200:
        response_200 = RemovedSetting.from_dict(response.json())

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
) -> Response[Problem | RemovedSetting]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    key: str,
    *,
    client: AuthenticatedClient,
) -> Response[Problem | RemovedSetting]:
    """Remove one stored setting

     Removes the setting stored under one key — the write `bd config unset` spells. `DELETE` for `DELETE
    /v0/beads/memories/{key}`'s reason: it names ONE resource by path, carries no body and takes no
    flags, which is what the method already means.

    REMOVING A KEY NOTHING SET SUCCEEDS, and this is where the operation parts company with the memory
    delete beside it. That one answers `404` for a key it held nothing under, because its role reports
    whether a row was found and `bd recall` already has an exit-code contract for the miss. THIS role
    reports no such thing: the storage seam discards the affected-row count on all three
    implementations, and an absent key and a key stored as the empty string are one answer on this plane
    — the same conflation `GET /v0/beads/config/{key}` has no `404` for. So this operation states an
    INTENDED END STATE rather than an act performed, and a caller clearing configuration it is not sure
    was ever written does not have to classify an error to learn it was already absent.

    THERE IS CONSEQUENTLY NO `removed` MEMBER, and there must not be one: it would be a value one
    implementation had to invent. Sending the same request twice is `200` and then `200`.

    UNSET DOES NOT UNDO `PUT`'s PROJECTION. Removing `status.custom` or `types.custom` deletes the row
    and LEAVES the normalized table exactly as the last write left it, so the custom statuses and types
    keep applying after the key that configured them is gone. All three implementations agree, so it is
    the plane's behavior rather than a divergence, and it is stated here so no reader infers a symmetry
    with `PUT` that the code does not have.

    THE PROTECTED KEY IS NOT REFUSED HERE. `issue_prefix` cannot be WRITTEN through this plane and can
    be removed through it — an asymmetry that is shipped behavior on all three implementations rather
    than a decision this document makes, recorded as bd-yby99.34. Removing it does not rename anything;
    it leaves the workspace resolving its prefix from `config.yaml` or from nothing.

    A CREDENTIAL-BEARING KEY IS REMOVABLE, on `PUT`'s reasoning: redaction withholds a value from a
    READER, and a removal discloses nothing at all.

    Hooks do not fire, and this plane has none to fire; see `PUT`.

    Args:
        key (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | RemovedSetting]
    """

    kwargs = _get_kwargs(
        key=key,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    key: str,
    *,
    client: AuthenticatedClient,
) -> Problem | RemovedSetting | None:
    """Remove one stored setting

     Removes the setting stored under one key — the write `bd config unset` spells. `DELETE` for `DELETE
    /v0/beads/memories/{key}`'s reason: it names ONE resource by path, carries no body and takes no
    flags, which is what the method already means.

    REMOVING A KEY NOTHING SET SUCCEEDS, and this is where the operation parts company with the memory
    delete beside it. That one answers `404` for a key it held nothing under, because its role reports
    whether a row was found and `bd recall` already has an exit-code contract for the miss. THIS role
    reports no such thing: the storage seam discards the affected-row count on all three
    implementations, and an absent key and a key stored as the empty string are one answer on this plane
    — the same conflation `GET /v0/beads/config/{key}` has no `404` for. So this operation states an
    INTENDED END STATE rather than an act performed, and a caller clearing configuration it is not sure
    was ever written does not have to classify an error to learn it was already absent.

    THERE IS CONSEQUENTLY NO `removed` MEMBER, and there must not be one: it would be a value one
    implementation had to invent. Sending the same request twice is `200` and then `200`.

    UNSET DOES NOT UNDO `PUT`'s PROJECTION. Removing `status.custom` or `types.custom` deletes the row
    and LEAVES the normalized table exactly as the last write left it, so the custom statuses and types
    keep applying after the key that configured them is gone. All three implementations agree, so it is
    the plane's behavior rather than a divergence, and it is stated here so no reader infers a symmetry
    with `PUT` that the code does not have.

    THE PROTECTED KEY IS NOT REFUSED HERE. `issue_prefix` cannot be WRITTEN through this plane and can
    be removed through it — an asymmetry that is shipped behavior on all three implementations rather
    than a decision this document makes, recorded as bd-yby99.34. Removing it does not rename anything;
    it leaves the workspace resolving its prefix from `config.yaml` or from nothing.

    A CREDENTIAL-BEARING KEY IS REMOVABLE, on `PUT`'s reasoning: redaction withholds a value from a
    READER, and a removal discloses nothing at all.

    Hooks do not fire, and this plane has none to fire; see `PUT`.

    Args:
        key (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | RemovedSetting
    """

    return sync_detailed(
        key=key,
        client=client,
    ).parsed


async def asyncio_detailed(
    key: str,
    *,
    client: AuthenticatedClient,
) -> Response[Problem | RemovedSetting]:
    """Remove one stored setting

     Removes the setting stored under one key — the write `bd config unset` spells. `DELETE` for `DELETE
    /v0/beads/memories/{key}`'s reason: it names ONE resource by path, carries no body and takes no
    flags, which is what the method already means.

    REMOVING A KEY NOTHING SET SUCCEEDS, and this is where the operation parts company with the memory
    delete beside it. That one answers `404` for a key it held nothing under, because its role reports
    whether a row was found and `bd recall` already has an exit-code contract for the miss. THIS role
    reports no such thing: the storage seam discards the affected-row count on all three
    implementations, and an absent key and a key stored as the empty string are one answer on this plane
    — the same conflation `GET /v0/beads/config/{key}` has no `404` for. So this operation states an
    INTENDED END STATE rather than an act performed, and a caller clearing configuration it is not sure
    was ever written does not have to classify an error to learn it was already absent.

    THERE IS CONSEQUENTLY NO `removed` MEMBER, and there must not be one: it would be a value one
    implementation had to invent. Sending the same request twice is `200` and then `200`.

    UNSET DOES NOT UNDO `PUT`'s PROJECTION. Removing `status.custom` or `types.custom` deletes the row
    and LEAVES the normalized table exactly as the last write left it, so the custom statuses and types
    keep applying after the key that configured them is gone. All three implementations agree, so it is
    the plane's behavior rather than a divergence, and it is stated here so no reader infers a symmetry
    with `PUT` that the code does not have.

    THE PROTECTED KEY IS NOT REFUSED HERE. `issue_prefix` cannot be WRITTEN through this plane and can
    be removed through it — an asymmetry that is shipped behavior on all three implementations rather
    than a decision this document makes, recorded as bd-yby99.34. Removing it does not rename anything;
    it leaves the workspace resolving its prefix from `config.yaml` or from nothing.

    A CREDENTIAL-BEARING KEY IS REMOVABLE, on `PUT`'s reasoning: redaction withholds a value from a
    READER, and a removal discloses nothing at all.

    Hooks do not fire, and this plane has none to fire; see `PUT`.

    Args:
        key (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | RemovedSetting]
    """

    kwargs = _get_kwargs(
        key=key,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    key: str,
    *,
    client: AuthenticatedClient,
) -> Problem | RemovedSetting | None:
    """Remove one stored setting

     Removes the setting stored under one key — the write `bd config unset` spells. `DELETE` for `DELETE
    /v0/beads/memories/{key}`'s reason: it names ONE resource by path, carries no body and takes no
    flags, which is what the method already means.

    REMOVING A KEY NOTHING SET SUCCEEDS, and this is where the operation parts company with the memory
    delete beside it. That one answers `404` for a key it held nothing under, because its role reports
    whether a row was found and `bd recall` already has an exit-code contract for the miss. THIS role
    reports no such thing: the storage seam discards the affected-row count on all three
    implementations, and an absent key and a key stored as the empty string are one answer on this plane
    — the same conflation `GET /v0/beads/config/{key}` has no `404` for. So this operation states an
    INTENDED END STATE rather than an act performed, and a caller clearing configuration it is not sure
    was ever written does not have to classify an error to learn it was already absent.

    THERE IS CONSEQUENTLY NO `removed` MEMBER, and there must not be one: it would be a value one
    implementation had to invent. Sending the same request twice is `200` and then `200`.

    UNSET DOES NOT UNDO `PUT`'s PROJECTION. Removing `status.custom` or `types.custom` deletes the row
    and LEAVES the normalized table exactly as the last write left it, so the custom statuses and types
    keep applying after the key that configured them is gone. All three implementations agree, so it is
    the plane's behavior rather than a divergence, and it is stated here so no reader infers a symmetry
    with `PUT` that the code does not have.

    THE PROTECTED KEY IS NOT REFUSED HERE. `issue_prefix` cannot be WRITTEN through this plane and can
    be removed through it — an asymmetry that is shipped behavior on all three implementations rather
    than a decision this document makes, recorded as bd-yby99.34. Removing it does not rename anything;
    it leaves the workspace resolving its prefix from `config.yaml` or from nothing.

    A CREDENTIAL-BEARING KEY IS REMOVABLE, on `PUT`'s reasoning: redaction withholds a value from a
    READER, and a removal discloses nothing at all.

    Hooks do not fire, and this plane has none to fire; see `PUT`.

    Args:
        key (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | RemovedSetting
    """

    return (
        await asyncio_detailed(
            key=key,
            client=client,
        )
    ).parsed
