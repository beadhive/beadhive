from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.issue_details import IssueDetails
from ...models.problem import Problem
from ...types import UNSET, Response, Unset


def _get_kwargs(
    id: str,
    *,
    include_comments: bool | Unset = False,
    include_dependents: bool | Unset = False,
    brief_deps: bool | Unset = False,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["include_comments"] = include_comments

    params["include_dependents"] = include_dependents

    params["brief_deps"] = brief_deps

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v0/beads/issues/{id}".format(
            id=quote(str(id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> IssueDetails | Problem | None:
    if response.status_code == 200:
        response_200 = IssueDetails.from_dict(response.json())

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
) -> Response[IssueDetails | Problem]:
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
    include_comments: bool | Unset = False,
    include_dependents: bool | Unset = False,
    brief_deps: bool | Unset = False,
) -> Response[IssueDetails | Problem]:
    """Get one issue with its labels, dependencies and counts

     Returns a single object, never an array. The id must be the exact canonical issue id: there is no
    fuzzy, prefix or substring resolution on this surface, so a lookup can never resolve to a different
    issue than the caller named. Wisp (ephemeral) records are looked up as a fallback when no issue
    matches.

    COMMENT BODIES AND DEPENDENTS ARE ASKED FOR, NEVER VOLUNTEERED. They are the two expensive row lists
    on this read, so each is absent unless the request sets the parameter that populates it, and a
    caller that wants only the cardinalities reads `comment_count`, `dependent_count` and
    `comments_omitted` and pays for neither. A request that sets neither parameter is answered exactly
    as this operation answered it before the parameters existed.

    IT IS THE TOKEN SOURCE FOR A GUARDED WRITE. The response carries `revision`, the row's optimistic-
    concurrency token, so a read-modify-write loop starts HERE: read the row, decide from what it says,
    and send the token back as the next request's `expected_version`. Before this member every token on
    the surface was minted by a WRITE, so the first guarded write of a loop had to be preceded by a
    write the caller did not want to make — or guarded on nothing at all, which is the lost-update this
    whole family exists to refuse. The token is carried on every 200, including for an ephemeral wisp
    resolved by the fallback lookup.

    Args:
        id (str):
        include_comments (bool | Unset):  Default: False.
        include_dependents (bool | Unset):  Default: False.
        brief_deps (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[IssueDetails | Problem]
    """

    kwargs = _get_kwargs(
        id=id,
        include_comments=include_comments,
        include_dependents=include_dependents,
        brief_deps=brief_deps,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    id: str,
    *,
    client: AuthenticatedClient,
    include_comments: bool | Unset = False,
    include_dependents: bool | Unset = False,
    brief_deps: bool | Unset = False,
) -> IssueDetails | Problem | None:
    """Get one issue with its labels, dependencies and counts

     Returns a single object, never an array. The id must be the exact canonical issue id: there is no
    fuzzy, prefix or substring resolution on this surface, so a lookup can never resolve to a different
    issue than the caller named. Wisp (ephemeral) records are looked up as a fallback when no issue
    matches.

    COMMENT BODIES AND DEPENDENTS ARE ASKED FOR, NEVER VOLUNTEERED. They are the two expensive row lists
    on this read, so each is absent unless the request sets the parameter that populates it, and a
    caller that wants only the cardinalities reads `comment_count`, `dependent_count` and
    `comments_omitted` and pays for neither. A request that sets neither parameter is answered exactly
    as this operation answered it before the parameters existed.

    IT IS THE TOKEN SOURCE FOR A GUARDED WRITE. The response carries `revision`, the row's optimistic-
    concurrency token, so a read-modify-write loop starts HERE: read the row, decide from what it says,
    and send the token back as the next request's `expected_version`. Before this member every token on
    the surface was minted by a WRITE, so the first guarded write of a loop had to be preceded by a
    write the caller did not want to make — or guarded on nothing at all, which is the lost-update this
    whole family exists to refuse. The token is carried on every 200, including for an ephemeral wisp
    resolved by the fallback lookup.

    Args:
        id (str):
        include_comments (bool | Unset):  Default: False.
        include_dependents (bool | Unset):  Default: False.
        brief_deps (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        IssueDetails | Problem
    """

    return sync_detailed(
        id=id,
        client=client,
        include_comments=include_comments,
        include_dependents=include_dependents,
        brief_deps=brief_deps,
    ).parsed


async def asyncio_detailed(
    id: str,
    *,
    client: AuthenticatedClient,
    include_comments: bool | Unset = False,
    include_dependents: bool | Unset = False,
    brief_deps: bool | Unset = False,
) -> Response[IssueDetails | Problem]:
    """Get one issue with its labels, dependencies and counts

     Returns a single object, never an array. The id must be the exact canonical issue id: there is no
    fuzzy, prefix or substring resolution on this surface, so a lookup can never resolve to a different
    issue than the caller named. Wisp (ephemeral) records are looked up as a fallback when no issue
    matches.

    COMMENT BODIES AND DEPENDENTS ARE ASKED FOR, NEVER VOLUNTEERED. They are the two expensive row lists
    on this read, so each is absent unless the request sets the parameter that populates it, and a
    caller that wants only the cardinalities reads `comment_count`, `dependent_count` and
    `comments_omitted` and pays for neither. A request that sets neither parameter is answered exactly
    as this operation answered it before the parameters existed.

    IT IS THE TOKEN SOURCE FOR A GUARDED WRITE. The response carries `revision`, the row's optimistic-
    concurrency token, so a read-modify-write loop starts HERE: read the row, decide from what it says,
    and send the token back as the next request's `expected_version`. Before this member every token on
    the surface was minted by a WRITE, so the first guarded write of a loop had to be preceded by a
    write the caller did not want to make — or guarded on nothing at all, which is the lost-update this
    whole family exists to refuse. The token is carried on every 200, including for an ephemeral wisp
    resolved by the fallback lookup.

    Args:
        id (str):
        include_comments (bool | Unset):  Default: False.
        include_dependents (bool | Unset):  Default: False.
        brief_deps (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[IssueDetails | Problem]
    """

    kwargs = _get_kwargs(
        id=id,
        include_comments=include_comments,
        include_dependents=include_dependents,
        brief_deps=brief_deps,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    id: str,
    *,
    client: AuthenticatedClient,
    include_comments: bool | Unset = False,
    include_dependents: bool | Unset = False,
    brief_deps: bool | Unset = False,
) -> IssueDetails | Problem | None:
    """Get one issue with its labels, dependencies and counts

     Returns a single object, never an array. The id must be the exact canonical issue id: there is no
    fuzzy, prefix or substring resolution on this surface, so a lookup can never resolve to a different
    issue than the caller named. Wisp (ephemeral) records are looked up as a fallback when no issue
    matches.

    COMMENT BODIES AND DEPENDENTS ARE ASKED FOR, NEVER VOLUNTEERED. They are the two expensive row lists
    on this read, so each is absent unless the request sets the parameter that populates it, and a
    caller that wants only the cardinalities reads `comment_count`, `dependent_count` and
    `comments_omitted` and pays for neither. A request that sets neither parameter is answered exactly
    as this operation answered it before the parameters existed.

    IT IS THE TOKEN SOURCE FOR A GUARDED WRITE. The response carries `revision`, the row's optimistic-
    concurrency token, so a read-modify-write loop starts HERE: read the row, decide from what it says,
    and send the token back as the next request's `expected_version`. Before this member every token on
    the surface was minted by a WRITE, so the first guarded write of a loop had to be preceded by a
    write the caller did not want to make — or guarded on nothing at all, which is the lost-update this
    whole family exists to refuse. The token is carried on every 200, including for an ephemeral wisp
    resolved by the fallback lookup.

    Args:
        id (str):
        include_comments (bool | Unset):  Default: False.
        include_dependents (bool | Unset):  Default: False.
        brief_deps (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        IssueDetails | Problem
    """

    return (
        await asyncio_detailed(
            id=id,
            client=client,
            include_comments=include_comments,
            include_dependents=include_dependents,
            brief_deps=brief_deps,
        )
    ).parsed
