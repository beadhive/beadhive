from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ref import Ref


T = TypeVar("T", bound="ApplyDepAddItem")


@_attrs_define
class ApplyDepAddItem:
    """Asserts ONE dependency edge, under `POST /v0/beads/dependencies:add`'s rules. An edge from a row to itself is a
    `400`.

    A TARGET NEED NOT BE A ROW THIS DATABASE HOLDS: an `external:` reference and an id belonging to another repository
    are legitimate targets, so only an absence this database can SEE is refused. A SOURCE has no such latitude — an edge
    follows its source, so a source this database holds no row for has no plane to land in.

    `metadata` is the edge's type-specific JSON blob, and an OBJECT where it is present at all. Most edge types carry
    none.

    A WAITS-FOR EDGE IS NORMALIZED RATHER THAN STORED AS ASKED. An absent, empty or `{}` `metadata` on a `waits-for`
    edge is STORED as `{"gate":"all-children"}`, because a stored waits-for row must be self-describing: readers
    predating the gate's introduction do not default a missing one, so an empty gate is a row those readers get wrong. A
    metadata that names a gate keeps it, along with the spawner and also-blocks members a caller may carry, and a gate
    that is neither `all-children` nor `any-children` is a `400`. Nothing else about that member is interpreted.

    THERE IS NO TYPED `waits_for` MEMBER, and that is the shape rather than an omission: every measured caller already
    carries the gate as metadata, a typed spelling lowers to these same bytes, and the blob carries members a two-field
    typed member could not express. One spelling, and it is this one.

        Attributes:
            source (Ref): Names ONE issue, either by an id that already exists or by the `key` a create item earlier in the
                same request gave itself.

                EXACTLY ONE OF THE TWO IS SET, and both cases the schema cannot express are a `400`: both members set is a
                caller that cannot say which it meant, and neither set is a reference to nothing. (Spelling that as a schema
                alternation would need `oneOf`, which this document does not use — see `ApplyItem`.)

                A KEY REACHES BACKWARD ONLY where the ref ADDRESSES a row — an `update.target`, a `close.target`, either
                endpoint of a `dep_add`. The one exception is `create.metadata_refs`, whose values may reach forward or name
                their own item's key; the operation's description says why.
            target (Ref): Names ONE issue, either by an id that already exists or by the `key` a create item earlier in the
                same request gave itself.

                EXACTLY ONE OF THE TWO IS SET, and both cases the schema cannot express are a `400`: both members set is a
                caller that cannot say which it meant, and neither set is a reference to nothing. (Spelling that as a schema
                alternation would need `oneOf`, which this document does not use — see `ApplyItem`.)

                A KEY REACHES BACKWARD ONLY where the ref ADDRESSES a row — an `update.target`, a `close.target`, either
                endpoint of a `dep_add`. The one exception is `create.metadata_refs`, whose values may reach forward or name
                their own item's key; the operation's description says why.
            type_ (str): The edge type, from the same OPEN vocabulary `Dependency.type` carries: checked for BEING a
                storable value, never for membership of a known-types list, so a workspace's own type passes.
            metadata (Any | Unset): One metadata value: ANY JSON value — string, number, boolean, null, array or object —
                because typed values enter through the explicit JSON metadata path and persist in older rows. It is not a
                string, and a client must not decode it as one.

                Where a member of this type is OMITTED, the key is absent; where it is present holding `null`, the key exists
                and holds null. Those are different states and this surface reports both.
    """

    source: Ref
    target: Ref
    type_: str
    metadata: Any | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        source = self.source.to_dict()

        target = self.target.to_dict()

        type_ = self.type_

        metadata = self.metadata

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "source": source,
                "target": target,
                "type": type_,
            }
        )
        if metadata is not UNSET:
            field_dict["metadata"] = metadata

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ref import Ref  # noqa: PLC0415

        d = dict(src_dict)
        source = Ref.from_dict(d.pop("source"))

        target = Ref.from_dict(d.pop("target"))

        type_ = d.pop("type")

        metadata = d.pop("metadata", UNSET)

        apply_dep_add_item = cls(
            source=source,
            target=target,
            type_=type_,
            metadata=metadata,
        )

        return apply_dep_add_item
