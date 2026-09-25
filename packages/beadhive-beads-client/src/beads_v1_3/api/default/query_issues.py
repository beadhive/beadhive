from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.problem import Problem
from ...models.query_issues_sort import QueryIssuesSort
from ...models.query_page import QueryPage
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    q: str,
    all_: bool | Unset = False,
    sort: QueryIssuesSort | Unset = UNSET,
    reverse: bool | Unset = False,
    limit: int | Unset = 50,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["q"] = q

    params["all"] = all_

    json_sort: str | Unset = UNSET
    if not isinstance(sort, Unset):
        json_sort = sort.value

    params["sort"] = json_sort

    params["reverse"] = reverse

    params["limit"] = limit

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v0/beads/issues:query",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Problem | QueryPage | None:
    if response.status_code == 200:
        response_200 = QueryPage.from_dict(response.json())

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
) -> Response[Problem | QueryPage]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    q: str,
    all_: bool | Unset = False,
    sort: QueryIssuesSort | Unset = UNSET,
    reverse: bool | Unset = False,
    limit: int | Unset = 50,
) -> Response[Problem | QueryPage]:
    """Query issues with a boolean expression

     The `bd query` expression language over HTTP: field comparisons combined with `AND`, `OR`, `NOT` and
    parentheses. It is the one operation on this surface that takes a DISJUNCTION — every filter
    parameter of `GET /v0/beads/issues` narrows the answer, and no combination of them expresses
    `type=bug OR label=urgent`.

    IT IS NOT A SQL PASSTHROUGH, and the shape of the language is what makes that true rather than a
    promise about validation: there are no table names, no joins and no way to name a column the
    vocabulary does not publish. The vocabulary is the one `bd query --help` documents, and an
    expression outside it is a 400 rather than an empty page.

    EVERY MATCH IS IN SCOPE, which has not always been true and is the reason this operation exists in
    the shape it does. An expression the storage filter cannot express is answered by evaluating the
    predicate over every candidate row and then cutting the page, so `has_more` means what it says. The
    CLI used to bound that scan at a few hundred rows and filter what came back, which silently dropped
    matches from an `OR` query and reported the result as complete. The cost is stated rather than
    hidden: a broad expression over a large workspace is a large read, and `limit` bounds the RESPONSE
    rather than the scan.

    THERE IS NO CURSOR AND NO `offset`. A cursor is a keyset position in a database order, and the
    matching set of a predicate query is assembled outside the database, so there is no position to
    encode; `offset` is absent because the two database sources this server can be built on disagree
    about whether they can honor one, and a parameter that works under one deployment topology and
    refuses under another is worse on a wire than an absent one. Raise `limit`, or narrow the
    expression.

    Args:
        q (str):
        all_ (bool | Unset):  Default: False.
        sort (QueryIssuesSort | Unset):
        reverse (bool | Unset):  Default: False.
        limit (int | Unset):  Default: 50.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | QueryPage]
    """

    kwargs = _get_kwargs(
        q=q,
        all_=all_,
        sort=sort,
        reverse=reverse,
        limit=limit,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    q: str,
    all_: bool | Unset = False,
    sort: QueryIssuesSort | Unset = UNSET,
    reverse: bool | Unset = False,
    limit: int | Unset = 50,
) -> Problem | QueryPage | None:
    """Query issues with a boolean expression

     The `bd query` expression language over HTTP: field comparisons combined with `AND`, `OR`, `NOT` and
    parentheses. It is the one operation on this surface that takes a DISJUNCTION — every filter
    parameter of `GET /v0/beads/issues` narrows the answer, and no combination of them expresses
    `type=bug OR label=urgent`.

    IT IS NOT A SQL PASSTHROUGH, and the shape of the language is what makes that true rather than a
    promise about validation: there are no table names, no joins and no way to name a column the
    vocabulary does not publish. The vocabulary is the one `bd query --help` documents, and an
    expression outside it is a 400 rather than an empty page.

    EVERY MATCH IS IN SCOPE, which has not always been true and is the reason this operation exists in
    the shape it does. An expression the storage filter cannot express is answered by evaluating the
    predicate over every candidate row and then cutting the page, so `has_more` means what it says. The
    CLI used to bound that scan at a few hundred rows and filter what came back, which silently dropped
    matches from an `OR` query and reported the result as complete. The cost is stated rather than
    hidden: a broad expression over a large workspace is a large read, and `limit` bounds the RESPONSE
    rather than the scan.

    THERE IS NO CURSOR AND NO `offset`. A cursor is a keyset position in a database order, and the
    matching set of a predicate query is assembled outside the database, so there is no position to
    encode; `offset` is absent because the two database sources this server can be built on disagree
    about whether they can honor one, and a parameter that works under one deployment topology and
    refuses under another is worse on a wire than an absent one. Raise `limit`, or narrow the
    expression.

    Args:
        q (str):
        all_ (bool | Unset):  Default: False.
        sort (QueryIssuesSort | Unset):
        reverse (bool | Unset):  Default: False.
        limit (int | Unset):  Default: 50.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | QueryPage
    """

    return sync_detailed(
        client=client,
        q=q,
        all_=all_,
        sort=sort,
        reverse=reverse,
        limit=limit,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    q: str,
    all_: bool | Unset = False,
    sort: QueryIssuesSort | Unset = UNSET,
    reverse: bool | Unset = False,
    limit: int | Unset = 50,
) -> Response[Problem | QueryPage]:
    """Query issues with a boolean expression

     The `bd query` expression language over HTTP: field comparisons combined with `AND`, `OR`, `NOT` and
    parentheses. It is the one operation on this surface that takes a DISJUNCTION — every filter
    parameter of `GET /v0/beads/issues` narrows the answer, and no combination of them expresses
    `type=bug OR label=urgent`.

    IT IS NOT A SQL PASSTHROUGH, and the shape of the language is what makes that true rather than a
    promise about validation: there are no table names, no joins and no way to name a column the
    vocabulary does not publish. The vocabulary is the one `bd query --help` documents, and an
    expression outside it is a 400 rather than an empty page.

    EVERY MATCH IS IN SCOPE, which has not always been true and is the reason this operation exists in
    the shape it does. An expression the storage filter cannot express is answered by evaluating the
    predicate over every candidate row and then cutting the page, so `has_more` means what it says. The
    CLI used to bound that scan at a few hundred rows and filter what came back, which silently dropped
    matches from an `OR` query and reported the result as complete. The cost is stated rather than
    hidden: a broad expression over a large workspace is a large read, and `limit` bounds the RESPONSE
    rather than the scan.

    THERE IS NO CURSOR AND NO `offset`. A cursor is a keyset position in a database order, and the
    matching set of a predicate query is assembled outside the database, so there is no position to
    encode; `offset` is absent because the two database sources this server can be built on disagree
    about whether they can honor one, and a parameter that works under one deployment topology and
    refuses under another is worse on a wire than an absent one. Raise `limit`, or narrow the
    expression.

    Args:
        q (str):
        all_ (bool | Unset):  Default: False.
        sort (QueryIssuesSort | Unset):
        reverse (bool | Unset):  Default: False.
        limit (int | Unset):  Default: 50.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | QueryPage]
    """

    kwargs = _get_kwargs(
        q=q,
        all_=all_,
        sort=sort,
        reverse=reverse,
        limit=limit,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    q: str,
    all_: bool | Unset = False,
    sort: QueryIssuesSort | Unset = UNSET,
    reverse: bool | Unset = False,
    limit: int | Unset = 50,
) -> Problem | QueryPage | None:
    """Query issues with a boolean expression

     The `bd query` expression language over HTTP: field comparisons combined with `AND`, `OR`, `NOT` and
    parentheses. It is the one operation on this surface that takes a DISJUNCTION — every filter
    parameter of `GET /v0/beads/issues` narrows the answer, and no combination of them expresses
    `type=bug OR label=urgent`.

    IT IS NOT A SQL PASSTHROUGH, and the shape of the language is what makes that true rather than a
    promise about validation: there are no table names, no joins and no way to name a column the
    vocabulary does not publish. The vocabulary is the one `bd query --help` documents, and an
    expression outside it is a 400 rather than an empty page.

    EVERY MATCH IS IN SCOPE, which has not always been true and is the reason this operation exists in
    the shape it does. An expression the storage filter cannot express is answered by evaluating the
    predicate over every candidate row and then cutting the page, so `has_more` means what it says. The
    CLI used to bound that scan at a few hundred rows and filter what came back, which silently dropped
    matches from an `OR` query and reported the result as complete. The cost is stated rather than
    hidden: a broad expression over a large workspace is a large read, and `limit` bounds the RESPONSE
    rather than the scan.

    THERE IS NO CURSOR AND NO `offset`. A cursor is a keyset position in a database order, and the
    matching set of a predicate query is assembled outside the database, so there is no position to
    encode; `offset` is absent because the two database sources this server can be built on disagree
    about whether they can honor one, and a parameter that works under one deployment topology and
    refuses under another is worse on a wire than an absent one. Raise `limit`, or narrow the
    expression.

    Args:
        q (str):
        all_ (bool | Unset):  Default: False.
        sort (QueryIssuesSort | Unset):
        reverse (bool | Unset):  Default: False.
        limit (int | Unset):  Default: 50.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | QueryPage
    """

    return (
        await asyncio_detailed(
            client=client,
            q=q,
            all_=all_,
            sort=sort,
            reverse=reverse,
            limit=limit,
        )
    ).parsed
