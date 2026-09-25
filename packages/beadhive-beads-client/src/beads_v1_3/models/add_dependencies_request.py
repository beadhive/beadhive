from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

if TYPE_CHECKING:
    from ..models.dependency_edge import DependencyEdge


T = TypeVar("T", bound="AddDependenciesRequest")


@_attrs_define
class AddDependenciesRequest:
    """
    Attributes:
        actor (str): Who is asserting the edges, under `ClaimRequest.actor`'s rules and for the same reasons: the server
            trims it, refuses an empty result, anything longer than 256 BYTES, and any control character including newline.
            It is attributed on each `dependency_added` event a genuinely new edge records, and interpolated into the
            storage commit message.
        edges (list[DependencyEdge]): The edges to assert, in the caller's order. An empty array is a `400` rather than
            a successful no-op: a write request that writes nothing is a client bug, and answering it cheerfully is how a
            client whose own list filtered to nothing silently stops wiring anything.

            The 100-edge cap is a bound on how long one request may hold a write transaction, not a statement about batch
            semantics. Split a larger graph; each request is atomic on its own — but note that splitting it changes what the
            cycle gate can see, since the gate runs over one request at a time.

            A per-edge refusal names its offender as `edges[i].member`.
    """

    actor: str
    edges: list[DependencyEdge]

    def to_dict(self) -> dict[str, Any]:
        actor = self.actor

        edges = []
        for edges_item_data in self.edges:
            edges_item = edges_item_data.to_dict()
            edges.append(edges_item)

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "actor": actor,
                "edges": edges,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.dependency_edge import DependencyEdge  # noqa: PLC0415

        d = dict(src_dict)
        actor = d.pop("actor")

        edges = []
        _edges = d.pop("edges")
        for edges_item_data in _edges:
            edges_item = DependencyEdge.from_dict(edges_item_data)

            edges.append(edges_item)

        add_dependencies_request = cls(
            actor=actor,
            edges=edges,
        )

        return add_dependencies_request
