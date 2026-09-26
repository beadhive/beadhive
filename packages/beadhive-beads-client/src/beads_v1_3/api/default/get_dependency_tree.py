from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.dependency_tree_page import DependencyTreePage
from ...models.get_dependency_tree_direction import GetDependencyTreeDirection
from ...models.problem import Problem
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    root_id: str,
    direction: GetDependencyTreeDirection | Unset = GetDependencyTreeDirection.DOWN,
    max_depth: int | Unset = 50,
    status: str | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["root_id"] = root_id

    json_direction: str | Unset = UNSET
    if not isinstance(direction, Unset):
        json_direction = direction.value

    params["direction"] = json_direction

    params["max_depth"] = max_depth

    params["status"] = status

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v0/beads/dependencies/tree",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> DependencyTreePage | Problem | None:
    if response.status_code == 200:
        response_200 = DependencyTreePage.from_dict(response.json())

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
) -> Response[DependencyTreePage | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    root_id: str,
    direction: GetDependencyTreeDirection | Unset = GetDependencyTreeDirection.DOWN,
    max_depth: int | Unset = 50,
    status: str | Unset = UNSET,
) -> Response[DependencyTreePage | Problem]:
    """Walk the dependency tree of one issue

     The dependency graph walked recursively from ONE root — the answer `bd dep tree` renders, and the
    same `TreeNode` elements that command's `--json` emits.

    THE ANSWER IS FLAT. Each element carries `depth` and `parent_id`, and a client rebuilds the shape
    from those two; the elements are in depth-first pre-order, so a subtree is contiguous.

    EVERY NODE APPEARS AT MOST ONCE PER WALK, at the depth and parent of the first path that reached it.
    That single rule is both the cycle policy and the diamond policy: a cycle TERMINATES rather than
    failing the call or being reported (`GET /v0/beads/dependencies/cycles` is where a cycle is an
    answer), and a shared subtree is shown under one parent only, with no option to show it twice.

    EDGES ARE WIDER THAN THEY ARE ON THE CYCLE REPORT. This walk follows every dependency type except
    `relates-to`, which is symmetric annotation — following it would make the "tree" the connected
    component. The cycle report follows `blocks` and `conditional-blocks` only, because it is about
    scheduling deadlock. Note that `related` and `relates-to` are two different types and only the
    second is excluded.

    Both dependency planes — durable and ephemeral — are one graph here, so an ephemeral step in the
    middle of a chain does not end the picture.

    A NODE THIS DATABASE CANNOT DESCRIBE ENDS THAT BRANCH. A `TreeNode` IS an issue, so there is no
    shape for "on the tree and undescribable": an edge whose target is an `external:` reference or an id
    in another repository's namespace contributes no node, and nothing in the answer says a branch
    stopped for that reason rather than because it ended. That is the one place this operation is less
    honest than the cycle report, whose `CycleMember` can carry a bare id.

    THERE IS NO `limit` AND NO CURSOR. The walk is bounded by `max_depth` instead, which bounds the
    DESCENT rather than truncating the answer; `has_more` is therefore always false in v0. There is also
    no `max_rows`: the CLI's defensive cap is a circuit breaker for a caller that would rather fail than
    wait, and refusing a whole answer is not something this surface offers a remote client.

    Args:
        root_id (str):
        direction (GetDependencyTreeDirection | Unset):  Default: GetDependencyTreeDirection.DOWN.
        max_depth (int | Unset):  Default: 50.
        status (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DependencyTreePage | Problem]
    """

    kwargs = _get_kwargs(
        root_id=root_id,
        direction=direction,
        max_depth=max_depth,
        status=status,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    root_id: str,
    direction: GetDependencyTreeDirection | Unset = GetDependencyTreeDirection.DOWN,
    max_depth: int | Unset = 50,
    status: str | Unset = UNSET,
) -> DependencyTreePage | Problem | None:
    """Walk the dependency tree of one issue

     The dependency graph walked recursively from ONE root — the answer `bd dep tree` renders, and the
    same `TreeNode` elements that command's `--json` emits.

    THE ANSWER IS FLAT. Each element carries `depth` and `parent_id`, and a client rebuilds the shape
    from those two; the elements are in depth-first pre-order, so a subtree is contiguous.

    EVERY NODE APPEARS AT MOST ONCE PER WALK, at the depth and parent of the first path that reached it.
    That single rule is both the cycle policy and the diamond policy: a cycle TERMINATES rather than
    failing the call or being reported (`GET /v0/beads/dependencies/cycles` is where a cycle is an
    answer), and a shared subtree is shown under one parent only, with no option to show it twice.

    EDGES ARE WIDER THAN THEY ARE ON THE CYCLE REPORT. This walk follows every dependency type except
    `relates-to`, which is symmetric annotation — following it would make the "tree" the connected
    component. The cycle report follows `blocks` and `conditional-blocks` only, because it is about
    scheduling deadlock. Note that `related` and `relates-to` are two different types and only the
    second is excluded.

    Both dependency planes — durable and ephemeral — are one graph here, so an ephemeral step in the
    middle of a chain does not end the picture.

    A NODE THIS DATABASE CANNOT DESCRIBE ENDS THAT BRANCH. A `TreeNode` IS an issue, so there is no
    shape for "on the tree and undescribable": an edge whose target is an `external:` reference or an id
    in another repository's namespace contributes no node, and nothing in the answer says a branch
    stopped for that reason rather than because it ended. That is the one place this operation is less
    honest than the cycle report, whose `CycleMember` can carry a bare id.

    THERE IS NO `limit` AND NO CURSOR. The walk is bounded by `max_depth` instead, which bounds the
    DESCENT rather than truncating the answer; `has_more` is therefore always false in v0. There is also
    no `max_rows`: the CLI's defensive cap is a circuit breaker for a caller that would rather fail than
    wait, and refusing a whole answer is not something this surface offers a remote client.

    Args:
        root_id (str):
        direction (GetDependencyTreeDirection | Unset):  Default: GetDependencyTreeDirection.DOWN.
        max_depth (int | Unset):  Default: 50.
        status (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DependencyTreePage | Problem
    """

    return sync_detailed(
        client=client,
        root_id=root_id,
        direction=direction,
        max_depth=max_depth,
        status=status,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    root_id: str,
    direction: GetDependencyTreeDirection | Unset = GetDependencyTreeDirection.DOWN,
    max_depth: int | Unset = 50,
    status: str | Unset = UNSET,
) -> Response[DependencyTreePage | Problem]:
    """Walk the dependency tree of one issue

     The dependency graph walked recursively from ONE root — the answer `bd dep tree` renders, and the
    same `TreeNode` elements that command's `--json` emits.

    THE ANSWER IS FLAT. Each element carries `depth` and `parent_id`, and a client rebuilds the shape
    from those two; the elements are in depth-first pre-order, so a subtree is contiguous.

    EVERY NODE APPEARS AT MOST ONCE PER WALK, at the depth and parent of the first path that reached it.
    That single rule is both the cycle policy and the diamond policy: a cycle TERMINATES rather than
    failing the call or being reported (`GET /v0/beads/dependencies/cycles` is where a cycle is an
    answer), and a shared subtree is shown under one parent only, with no option to show it twice.

    EDGES ARE WIDER THAN THEY ARE ON THE CYCLE REPORT. This walk follows every dependency type except
    `relates-to`, which is symmetric annotation — following it would make the "tree" the connected
    component. The cycle report follows `blocks` and `conditional-blocks` only, because it is about
    scheduling deadlock. Note that `related` and `relates-to` are two different types and only the
    second is excluded.

    Both dependency planes — durable and ephemeral — are one graph here, so an ephemeral step in the
    middle of a chain does not end the picture.

    A NODE THIS DATABASE CANNOT DESCRIBE ENDS THAT BRANCH. A `TreeNode` IS an issue, so there is no
    shape for "on the tree and undescribable": an edge whose target is an `external:` reference or an id
    in another repository's namespace contributes no node, and nothing in the answer says a branch
    stopped for that reason rather than because it ended. That is the one place this operation is less
    honest than the cycle report, whose `CycleMember` can carry a bare id.

    THERE IS NO `limit` AND NO CURSOR. The walk is bounded by `max_depth` instead, which bounds the
    DESCENT rather than truncating the answer; `has_more` is therefore always false in v0. There is also
    no `max_rows`: the CLI's defensive cap is a circuit breaker for a caller that would rather fail than
    wait, and refusing a whole answer is not something this surface offers a remote client.

    Args:
        root_id (str):
        direction (GetDependencyTreeDirection | Unset):  Default: GetDependencyTreeDirection.DOWN.
        max_depth (int | Unset):  Default: 50.
        status (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DependencyTreePage | Problem]
    """

    kwargs = _get_kwargs(
        root_id=root_id,
        direction=direction,
        max_depth=max_depth,
        status=status,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    root_id: str,
    direction: GetDependencyTreeDirection | Unset = GetDependencyTreeDirection.DOWN,
    max_depth: int | Unset = 50,
    status: str | Unset = UNSET,
) -> DependencyTreePage | Problem | None:
    """Walk the dependency tree of one issue

     The dependency graph walked recursively from ONE root — the answer `bd dep tree` renders, and the
    same `TreeNode` elements that command's `--json` emits.

    THE ANSWER IS FLAT. Each element carries `depth` and `parent_id`, and a client rebuilds the shape
    from those two; the elements are in depth-first pre-order, so a subtree is contiguous.

    EVERY NODE APPEARS AT MOST ONCE PER WALK, at the depth and parent of the first path that reached it.
    That single rule is both the cycle policy and the diamond policy: a cycle TERMINATES rather than
    failing the call or being reported (`GET /v0/beads/dependencies/cycles` is where a cycle is an
    answer), and a shared subtree is shown under one parent only, with no option to show it twice.

    EDGES ARE WIDER THAN THEY ARE ON THE CYCLE REPORT. This walk follows every dependency type except
    `relates-to`, which is symmetric annotation — following it would make the "tree" the connected
    component. The cycle report follows `blocks` and `conditional-blocks` only, because it is about
    scheduling deadlock. Note that `related` and `relates-to` are two different types and only the
    second is excluded.

    Both dependency planes — durable and ephemeral — are one graph here, so an ephemeral step in the
    middle of a chain does not end the picture.

    A NODE THIS DATABASE CANNOT DESCRIBE ENDS THAT BRANCH. A `TreeNode` IS an issue, so there is no
    shape for "on the tree and undescribable": an edge whose target is an `external:` reference or an id
    in another repository's namespace contributes no node, and nothing in the answer says a branch
    stopped for that reason rather than because it ended. That is the one place this operation is less
    honest than the cycle report, whose `CycleMember` can carry a bare id.

    THERE IS NO `limit` AND NO CURSOR. The walk is bounded by `max_depth` instead, which bounds the
    DESCENT rather than truncating the answer; `has_more` is therefore always false in v0. There is also
    no `max_rows`: the CLI's defensive cap is a circuit breaker for a caller that would rather fail than
    wait, and refusing a whole answer is not something this surface offers a remote client.

    Args:
        root_id (str):
        direction (GetDependencyTreeDirection | Unset):  Default: GetDependencyTreeDirection.DOWN.
        max_depth (int | Unset):  Default: 50.
        status (str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DependencyTreePage | Problem
    """

    return (
        await asyncio_detailed(
            client=client,
            root_id=root_id,
            direction=direction,
            max_depth=max_depth,
            status=status,
        )
    ).parsed
