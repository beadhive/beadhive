from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.problem import Problem
from ...models.settings_page import SettingsPage
from ...types import Response


def _get_kwargs() -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v0/beads/config",
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Problem | SettingsPage | None:
    if response.status_code == 200:
        response_200 = SettingsPage.from_dict(response.json())

        return response_200

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
) -> Response[Problem | SettingsPage]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
) -> Response[Problem | SettingsPage]:
    """List the workspace's stored settings

     The SETTINGS stored in the workspace database, which is the plane `bd config list` reads. It is not
    the effective configuration: values that reach a running `bd` from `config.yaml`, from environment
    variables or from git config are absent and cannot be here, because they are files and variables on
    the CLIENT's machine and this server answers for the database. `bd config show` is the multi-source
    view and has no HTTP operation for the same reason.

    IT IS NOT EVERY ROW OF THAT TABLE. Keys under `kv.` are omitted — the generic `bd kv` namespace and
    the `bd remember` memories nested under it. Those rows are USER DATA that rides in the settings
    table because there is one table, not because they are settings, and enumerating them here published
    a workspace's memories, key and value, to anything that could reach this port. `bd config list`
    omits them too: the exclusion is in the shared role both doors call, so the two cannot drift.
    `getSetting` still answers a `kv.` key NAMED EXACTLY, and this operation is where it stopped being
    discoverable.

    SETTINGS WHOSE KEY MARKS THEM AS CREDENTIAL-BEARING ARE WITHHELD. Their entry is present with
    `redacted: true` and no `value`, so a client can see that the key is configured without the surface
    handing a secret to every process that can reach the port. A CONFIGURED BEARER DOES NOT NARROW IT:
    authentication on this surface is a deployment posture and the credential, where there is one, is a
    single shared token that names nobody — it decides WHO may call, never what a caller who is let in
    may read — and there is no TLS either. The rule is the KEY's and is the same for every caller. See
    `Setting`, and `PUT` on this key's own path for the write half, which withholds the value in its
    response for the same reason and permits the write.

    The envelope is the paginated one and `has_more` is always false today: settings are a keyed
    namespace a workspace holds tens of, not a collection to scan, so the whole plane is returned in one
    page. The envelope is used anyway because entries are ordered by key, which makes a keyset cursor
    expressible later without a breaking change.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | SettingsPage]
    """

    kwargs = _get_kwargs()

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
) -> Problem | SettingsPage | None:
    """List the workspace's stored settings

     The SETTINGS stored in the workspace database, which is the plane `bd config list` reads. It is not
    the effective configuration: values that reach a running `bd` from `config.yaml`, from environment
    variables or from git config are absent and cannot be here, because they are files and variables on
    the CLIENT's machine and this server answers for the database. `bd config show` is the multi-source
    view and has no HTTP operation for the same reason.

    IT IS NOT EVERY ROW OF THAT TABLE. Keys under `kv.` are omitted — the generic `bd kv` namespace and
    the `bd remember` memories nested under it. Those rows are USER DATA that rides in the settings
    table because there is one table, not because they are settings, and enumerating them here published
    a workspace's memories, key and value, to anything that could reach this port. `bd config list`
    omits them too: the exclusion is in the shared role both doors call, so the two cannot drift.
    `getSetting` still answers a `kv.` key NAMED EXACTLY, and this operation is where it stopped being
    discoverable.

    SETTINGS WHOSE KEY MARKS THEM AS CREDENTIAL-BEARING ARE WITHHELD. Their entry is present with
    `redacted: true` and no `value`, so a client can see that the key is configured without the surface
    handing a secret to every process that can reach the port. A CONFIGURED BEARER DOES NOT NARROW IT:
    authentication on this surface is a deployment posture and the credential, where there is one, is a
    single shared token that names nobody — it decides WHO may call, never what a caller who is let in
    may read — and there is no TLS either. The rule is the KEY's and is the same for every caller. See
    `Setting`, and `PUT` on this key's own path for the write half, which withholds the value in its
    response for the same reason and permits the write.

    The envelope is the paginated one and `has_more` is always false today: settings are a keyed
    namespace a workspace holds tens of, not a collection to scan, so the whole plane is returned in one
    page. The envelope is used anyway because entries are ordered by key, which makes a keyset cursor
    expressible later without a breaking change.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | SettingsPage
    """

    return sync_detailed(
        client=client,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
) -> Response[Problem | SettingsPage]:
    """List the workspace's stored settings

     The SETTINGS stored in the workspace database, which is the plane `bd config list` reads. It is not
    the effective configuration: values that reach a running `bd` from `config.yaml`, from environment
    variables or from git config are absent and cannot be here, because they are files and variables on
    the CLIENT's machine and this server answers for the database. `bd config show` is the multi-source
    view and has no HTTP operation for the same reason.

    IT IS NOT EVERY ROW OF THAT TABLE. Keys under `kv.` are omitted — the generic `bd kv` namespace and
    the `bd remember` memories nested under it. Those rows are USER DATA that rides in the settings
    table because there is one table, not because they are settings, and enumerating them here published
    a workspace's memories, key and value, to anything that could reach this port. `bd config list`
    omits them too: the exclusion is in the shared role both doors call, so the two cannot drift.
    `getSetting` still answers a `kv.` key NAMED EXACTLY, and this operation is where it stopped being
    discoverable.

    SETTINGS WHOSE KEY MARKS THEM AS CREDENTIAL-BEARING ARE WITHHELD. Their entry is present with
    `redacted: true` and no `value`, so a client can see that the key is configured without the surface
    handing a secret to every process that can reach the port. A CONFIGURED BEARER DOES NOT NARROW IT:
    authentication on this surface is a deployment posture and the credential, where there is one, is a
    single shared token that names nobody — it decides WHO may call, never what a caller who is let in
    may read — and there is no TLS either. The rule is the KEY's and is the same for every caller. See
    `Setting`, and `PUT` on this key's own path for the write half, which withholds the value in its
    response for the same reason and permits the write.

    The envelope is the paginated one and `has_more` is always false today: settings are a keyed
    namespace a workspace holds tens of, not a collection to scan, so the whole plane is returned in one
    page. The envelope is used anyway because entries are ordered by key, which makes a keyset cursor
    expressible later without a breaking change.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | SettingsPage]
    """

    kwargs = _get_kwargs()

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
) -> Problem | SettingsPage | None:
    """List the workspace's stored settings

     The SETTINGS stored in the workspace database, which is the plane `bd config list` reads. It is not
    the effective configuration: values that reach a running `bd` from `config.yaml`, from environment
    variables or from git config are absent and cannot be here, because they are files and variables on
    the CLIENT's machine and this server answers for the database. `bd config show` is the multi-source
    view and has no HTTP operation for the same reason.

    IT IS NOT EVERY ROW OF THAT TABLE. Keys under `kv.` are omitted — the generic `bd kv` namespace and
    the `bd remember` memories nested under it. Those rows are USER DATA that rides in the settings
    table because there is one table, not because they are settings, and enumerating them here published
    a workspace's memories, key and value, to anything that could reach this port. `bd config list`
    omits them too: the exclusion is in the shared role both doors call, so the two cannot drift.
    `getSetting` still answers a `kv.` key NAMED EXACTLY, and this operation is where it stopped being
    discoverable.

    SETTINGS WHOSE KEY MARKS THEM AS CREDENTIAL-BEARING ARE WITHHELD. Their entry is present with
    `redacted: true` and no `value`, so a client can see that the key is configured without the surface
    handing a secret to every process that can reach the port. A CONFIGURED BEARER DOES NOT NARROW IT:
    authentication on this surface is a deployment posture and the credential, where there is one, is a
    single shared token that names nobody — it decides WHO may call, never what a caller who is let in
    may read — and there is no TLS either. The rule is the KEY's and is the same for every caller. See
    `Setting`, and `PUT` on this key's own path for the write half, which withholds the value in its
    response for the same reason and permits the write.

    The envelope is the paginated one and `has_more` is always false today: settings are a keyed
    namespace a workspace holds tens of, not a collection to scan, so the whole plane is returned in one
    page. The envelope is used anyway because entries are ordered by key, which makes a keyset cursor
    expressible later without a breaking change.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | SettingsPage
    """

    return (
        await asyncio_detailed(
            client=client,
        )
    ).parsed
