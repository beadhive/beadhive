from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.problem import Problem
from ...models.set_setting_request import SetSettingRequest
from ...models.setting import Setting
from ...types import Response


def _get_kwargs(
    key: str,
    *,
    body: SetSettingRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "put",
        "url": "/v0/beads/config/{key}".format(
            key=quote(str(key), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Problem | Setting | None:
    if response.status_code == 200:
        response_200 = Setting.from_dict(response.json())

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
) -> Response[Problem | Setting]:
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
    body: SetSettingRequest,
) -> Response[Problem | Setting]:
    """Store one setting

     Stores one setting, REPLACING any value already there — the write `bd config set` spells, on the
    durable settings plane this document's reads already publish.

    `PUT` RATHER THAN A COLLECTION `POST`, and the method is the whole argument: the resource has a
    canonical URI, the caller names it, and the request carries the value that becomes its whole state.
    That is what `PUT` already means, and it is idempotent in the strict sense — the same request sent
    twice leaves the same row. `POST /v0/beads/memories` is the operation this is NOT: that one posts to
    the COLLECTION because its key may be derived from the content, so the caller cannot always name the
    resource it is creating. Here the caller always can.

    THE KEY IS THE PATH'S and appears nowhere in the body: one anchor, one spelling, and no question
    about what to do when two disagree. It is used verbatim — no namespace completion, no case folding,
    no dash/underscore equivalence — and stored UNTRIMMED, because a key differing from another only by
    surrounding space is a key a reader will never match and trimming it would produce a write the
    caller cannot find again.

    ## What this plane refuses, and what it does not

    `issue_prefix` — in either spelling — is a `400` and NOTHING is written. The prefix is owned by `bd
    init --prefix`, `bd bootstrap` and `bd rename-prefix`, each of which does work this plane cannot:
    rewriting existing ids, or seeding a workspace that has none. Storing a new one here would leave the
    beads created before the write and the beads created after it disagreeing about their own namespace
    with nothing to reconcile them.

    A `status.custom` value that does not PARSE is a `400` and nothing is written. That key is not
    merely stored — it is projected into the `custom_statuses` table, which reads consult first, IN THE
    SAME TRANSACTION as the row. `types.custom` is projected into `custom_types` the same way. A row
    without its table is a value that has been stored and has no effect for as long as the table holds
    something else, so the write and the projection are one durable act or neither happens.

    A KEY WHOSE NAME MARKS IT CREDENTIAL-BEARING IS WRITABLE, and that is deliberate rather than an
    oversight in the redaction posture. Redaction is a rule about DISCLOSURE: it withholds a value from
    a reader because a bearer on this surface is shared and surface-wide and cannot decide that one
    caller may read a credential and another may not. A writer supplies the value, so refusing the write
    protects nothing that is not already in the caller's hand — and it would leave a workspace whose
    credentials can be seen to EXIST and never configured. The role refuses no such key either; `bd
    config set`'s own secret guard is about writing a credential into a git-tracked `config.yaml`, which
    is a different plane with a different hazard, and it returns clean for every key this operation
    reaches.

    THE RESPONSE THEREFORE WITHHOLDS IT ANYWAY. The body is a `Setting` projected by the same rule `GET
    /v0/beads/config/{key}` projects with, so a `redacted: true` key comes back with no `value` — the
    response to a write is byte-identical to the read that follows it. It costs the caller nothing: the
    role promises the stored value equals the value sent for every key this plane accepts, so the echo
    carries no information the caller does not already hold, and publishing it would make one schema say
    two different things about `redacted`.

    A key belonging to another SOURCE is not refused here and is worth knowing about: `export.*`,
    `dolt.*`, `federation.*`, `storage-class.*` and the rest of the yaml-only list live in
    `config.yaml`, and `beads.role` lives in git config. Written through this operation they land a row
    no reader consults. The role does not police that routing and neither can this server — three of the
    five sources `bd config show` reads are files on the CLIENT's machine, which is the same reason that
    command has no operation here.

    Hooks do not fire, as for every write on this surface — and on this plane there are none to fire in
    any case: the workspace hook vocabulary is `on_create`/`on_update`/`on_close`, each of which hands a
    script an ISSUE, and a settings write has none to name.

    Args:
        key (str):
        body (SetSettingRequest): What to store under the key the path names. The key is not a
            member here: it has one spelling, and a body carrying it too would give one request two
            anchors and a question about what to do when they disagree.

            THERE IS NO `actor`, unlike every issue mutation on this surface, and no guard member
            either. This plane records no history entry to attribute a write on and holds no row
            version to compare, so both would be members with nothing behind them.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | Setting]
    """

    kwargs = _get_kwargs(
        key=key,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    key: str,
    *,
    client: AuthenticatedClient,
    body: SetSettingRequest,
) -> Problem | Setting | None:
    """Store one setting

     Stores one setting, REPLACING any value already there — the write `bd config set` spells, on the
    durable settings plane this document's reads already publish.

    `PUT` RATHER THAN A COLLECTION `POST`, and the method is the whole argument: the resource has a
    canonical URI, the caller names it, and the request carries the value that becomes its whole state.
    That is what `PUT` already means, and it is idempotent in the strict sense — the same request sent
    twice leaves the same row. `POST /v0/beads/memories` is the operation this is NOT: that one posts to
    the COLLECTION because its key may be derived from the content, so the caller cannot always name the
    resource it is creating. Here the caller always can.

    THE KEY IS THE PATH'S and appears nowhere in the body: one anchor, one spelling, and no question
    about what to do when two disagree. It is used verbatim — no namespace completion, no case folding,
    no dash/underscore equivalence — and stored UNTRIMMED, because a key differing from another only by
    surrounding space is a key a reader will never match and trimming it would produce a write the
    caller cannot find again.

    ## What this plane refuses, and what it does not

    `issue_prefix` — in either spelling — is a `400` and NOTHING is written. The prefix is owned by `bd
    init --prefix`, `bd bootstrap` and `bd rename-prefix`, each of which does work this plane cannot:
    rewriting existing ids, or seeding a workspace that has none. Storing a new one here would leave the
    beads created before the write and the beads created after it disagreeing about their own namespace
    with nothing to reconcile them.

    A `status.custom` value that does not PARSE is a `400` and nothing is written. That key is not
    merely stored — it is projected into the `custom_statuses` table, which reads consult first, IN THE
    SAME TRANSACTION as the row. `types.custom` is projected into `custom_types` the same way. A row
    without its table is a value that has been stored and has no effect for as long as the table holds
    something else, so the write and the projection are one durable act or neither happens.

    A KEY WHOSE NAME MARKS IT CREDENTIAL-BEARING IS WRITABLE, and that is deliberate rather than an
    oversight in the redaction posture. Redaction is a rule about DISCLOSURE: it withholds a value from
    a reader because a bearer on this surface is shared and surface-wide and cannot decide that one
    caller may read a credential and another may not. A writer supplies the value, so refusing the write
    protects nothing that is not already in the caller's hand — and it would leave a workspace whose
    credentials can be seen to EXIST and never configured. The role refuses no such key either; `bd
    config set`'s own secret guard is about writing a credential into a git-tracked `config.yaml`, which
    is a different plane with a different hazard, and it returns clean for every key this operation
    reaches.

    THE RESPONSE THEREFORE WITHHOLDS IT ANYWAY. The body is a `Setting` projected by the same rule `GET
    /v0/beads/config/{key}` projects with, so a `redacted: true` key comes back with no `value` — the
    response to a write is byte-identical to the read that follows it. It costs the caller nothing: the
    role promises the stored value equals the value sent for every key this plane accepts, so the echo
    carries no information the caller does not already hold, and publishing it would make one schema say
    two different things about `redacted`.

    A key belonging to another SOURCE is not refused here and is worth knowing about: `export.*`,
    `dolt.*`, `federation.*`, `storage-class.*` and the rest of the yaml-only list live in
    `config.yaml`, and `beads.role` lives in git config. Written through this operation they land a row
    no reader consults. The role does not police that routing and neither can this server — three of the
    five sources `bd config show` reads are files on the CLIENT's machine, which is the same reason that
    command has no operation here.

    Hooks do not fire, as for every write on this surface — and on this plane there are none to fire in
    any case: the workspace hook vocabulary is `on_create`/`on_update`/`on_close`, each of which hands a
    script an ISSUE, and a settings write has none to name.

    Args:
        key (str):
        body (SetSettingRequest): What to store under the key the path names. The key is not a
            member here: it has one spelling, and a body carrying it too would give one request two
            anchors and a question about what to do when they disagree.

            THERE IS NO `actor`, unlike every issue mutation on this surface, and no guard member
            either. This plane records no history entry to attribute a write on and holds no row
            version to compare, so both would be members with nothing behind them.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | Setting
    """

    return sync_detailed(
        key=key,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    key: str,
    *,
    client: AuthenticatedClient,
    body: SetSettingRequest,
) -> Response[Problem | Setting]:
    """Store one setting

     Stores one setting, REPLACING any value already there — the write `bd config set` spells, on the
    durable settings plane this document's reads already publish.

    `PUT` RATHER THAN A COLLECTION `POST`, and the method is the whole argument: the resource has a
    canonical URI, the caller names it, and the request carries the value that becomes its whole state.
    That is what `PUT` already means, and it is idempotent in the strict sense — the same request sent
    twice leaves the same row. `POST /v0/beads/memories` is the operation this is NOT: that one posts to
    the COLLECTION because its key may be derived from the content, so the caller cannot always name the
    resource it is creating. Here the caller always can.

    THE KEY IS THE PATH'S and appears nowhere in the body: one anchor, one spelling, and no question
    about what to do when two disagree. It is used verbatim — no namespace completion, no case folding,
    no dash/underscore equivalence — and stored UNTRIMMED, because a key differing from another only by
    surrounding space is a key a reader will never match and trimming it would produce a write the
    caller cannot find again.

    ## What this plane refuses, and what it does not

    `issue_prefix` — in either spelling — is a `400` and NOTHING is written. The prefix is owned by `bd
    init --prefix`, `bd bootstrap` and `bd rename-prefix`, each of which does work this plane cannot:
    rewriting existing ids, or seeding a workspace that has none. Storing a new one here would leave the
    beads created before the write and the beads created after it disagreeing about their own namespace
    with nothing to reconcile them.

    A `status.custom` value that does not PARSE is a `400` and nothing is written. That key is not
    merely stored — it is projected into the `custom_statuses` table, which reads consult first, IN THE
    SAME TRANSACTION as the row. `types.custom` is projected into `custom_types` the same way. A row
    without its table is a value that has been stored and has no effect for as long as the table holds
    something else, so the write and the projection are one durable act or neither happens.

    A KEY WHOSE NAME MARKS IT CREDENTIAL-BEARING IS WRITABLE, and that is deliberate rather than an
    oversight in the redaction posture. Redaction is a rule about DISCLOSURE: it withholds a value from
    a reader because a bearer on this surface is shared and surface-wide and cannot decide that one
    caller may read a credential and another may not. A writer supplies the value, so refusing the write
    protects nothing that is not already in the caller's hand — and it would leave a workspace whose
    credentials can be seen to EXIST and never configured. The role refuses no such key either; `bd
    config set`'s own secret guard is about writing a credential into a git-tracked `config.yaml`, which
    is a different plane with a different hazard, and it returns clean for every key this operation
    reaches.

    THE RESPONSE THEREFORE WITHHOLDS IT ANYWAY. The body is a `Setting` projected by the same rule `GET
    /v0/beads/config/{key}` projects with, so a `redacted: true` key comes back with no `value` — the
    response to a write is byte-identical to the read that follows it. It costs the caller nothing: the
    role promises the stored value equals the value sent for every key this plane accepts, so the echo
    carries no information the caller does not already hold, and publishing it would make one schema say
    two different things about `redacted`.

    A key belonging to another SOURCE is not refused here and is worth knowing about: `export.*`,
    `dolt.*`, `federation.*`, `storage-class.*` and the rest of the yaml-only list live in
    `config.yaml`, and `beads.role` lives in git config. Written through this operation they land a row
    no reader consults. The role does not police that routing and neither can this server — three of the
    five sources `bd config show` reads are files on the CLIENT's machine, which is the same reason that
    command has no operation here.

    Hooks do not fire, as for every write on this surface — and on this plane there are none to fire in
    any case: the workspace hook vocabulary is `on_create`/`on_update`/`on_close`, each of which hands a
    script an ISSUE, and a settings write has none to name.

    Args:
        key (str):
        body (SetSettingRequest): What to store under the key the path names. The key is not a
            member here: it has one spelling, and a body carrying it too would give one request two
            anchors and a question about what to do when they disagree.

            THERE IS NO `actor`, unlike every issue mutation on this surface, and no guard member
            either. This plane records no history entry to attribute a write on and holds no row
            version to compare, so both would be members with nothing behind them.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | Setting]
    """

    kwargs = _get_kwargs(
        key=key,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    key: str,
    *,
    client: AuthenticatedClient,
    body: SetSettingRequest,
) -> Problem | Setting | None:
    """Store one setting

     Stores one setting, REPLACING any value already there — the write `bd config set` spells, on the
    durable settings plane this document's reads already publish.

    `PUT` RATHER THAN A COLLECTION `POST`, and the method is the whole argument: the resource has a
    canonical URI, the caller names it, and the request carries the value that becomes its whole state.
    That is what `PUT` already means, and it is idempotent in the strict sense — the same request sent
    twice leaves the same row. `POST /v0/beads/memories` is the operation this is NOT: that one posts to
    the COLLECTION because its key may be derived from the content, so the caller cannot always name the
    resource it is creating. Here the caller always can.

    THE KEY IS THE PATH'S and appears nowhere in the body: one anchor, one spelling, and no question
    about what to do when two disagree. It is used verbatim — no namespace completion, no case folding,
    no dash/underscore equivalence — and stored UNTRIMMED, because a key differing from another only by
    surrounding space is a key a reader will never match and trimming it would produce a write the
    caller cannot find again.

    ## What this plane refuses, and what it does not

    `issue_prefix` — in either spelling — is a `400` and NOTHING is written. The prefix is owned by `bd
    init --prefix`, `bd bootstrap` and `bd rename-prefix`, each of which does work this plane cannot:
    rewriting existing ids, or seeding a workspace that has none. Storing a new one here would leave the
    beads created before the write and the beads created after it disagreeing about their own namespace
    with nothing to reconcile them.

    A `status.custom` value that does not PARSE is a `400` and nothing is written. That key is not
    merely stored — it is projected into the `custom_statuses` table, which reads consult first, IN THE
    SAME TRANSACTION as the row. `types.custom` is projected into `custom_types` the same way. A row
    without its table is a value that has been stored and has no effect for as long as the table holds
    something else, so the write and the projection are one durable act or neither happens.

    A KEY WHOSE NAME MARKS IT CREDENTIAL-BEARING IS WRITABLE, and that is deliberate rather than an
    oversight in the redaction posture. Redaction is a rule about DISCLOSURE: it withholds a value from
    a reader because a bearer on this surface is shared and surface-wide and cannot decide that one
    caller may read a credential and another may not. A writer supplies the value, so refusing the write
    protects nothing that is not already in the caller's hand — and it would leave a workspace whose
    credentials can be seen to EXIST and never configured. The role refuses no such key either; `bd
    config set`'s own secret guard is about writing a credential into a git-tracked `config.yaml`, which
    is a different plane with a different hazard, and it returns clean for every key this operation
    reaches.

    THE RESPONSE THEREFORE WITHHOLDS IT ANYWAY. The body is a `Setting` projected by the same rule `GET
    /v0/beads/config/{key}` projects with, so a `redacted: true` key comes back with no `value` — the
    response to a write is byte-identical to the read that follows it. It costs the caller nothing: the
    role promises the stored value equals the value sent for every key this plane accepts, so the echo
    carries no information the caller does not already hold, and publishing it would make one schema say
    two different things about `redacted`.

    A key belonging to another SOURCE is not refused here and is worth knowing about: `export.*`,
    `dolt.*`, `federation.*`, `storage-class.*` and the rest of the yaml-only list live in
    `config.yaml`, and `beads.role` lives in git config. Written through this operation they land a row
    no reader consults. The role does not police that routing and neither can this server — three of the
    five sources `bd config show` reads are files on the CLIENT's machine, which is the same reason that
    command has no operation here.

    Hooks do not fire, as for every write on this surface — and on this plane there are none to fire in
    any case: the workspace hook vocabulary is `on_create`/`on_update`/`on_close`, each of which hands a
    script an ISSUE, and a settings write has none to name.

    Args:
        key (str):
        body (SetSettingRequest): What to store under the key the path names. The key is not a
            member here: it has one spelling, and a body carrying it too would give one request two
            anchors and a question about what to do when they disagree.

            THERE IS NO `actor`, unlike every issue mutation on this surface, and no guard member
            either. This plane records no history entry to attribute a write on and holds no row
            version to compare, so both would be members with nothing behind them.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | Setting
    """

    return (
        await asyncio_detailed(
            key=key,
            client=client,
            body=body,
        )
    ).parsed
