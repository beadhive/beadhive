from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..models.apply_item_kind import ApplyItemKind
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.apply_close_item import ApplyCloseItem
    from ..models.apply_create_item import ApplyCreateItem
    from ..models.apply_dep_add_item import ApplyDepAddItem
    from ..models.apply_update_item import ApplyUpdateItem


T = TypeVar("T", bound="ApplyItem")


@_attrs_define
class ApplyItem:
    """One item of a plan: a `kind` naming what it does, plus exactly one payload member matching it.

    IT IS A TAGGED SINGLE-SHAPE OBJECT rather than a polymorphic one, and the spelling is deliberate. This document uses
    no `oneOf`, `anyOf` or `allOf` anywhere: a component carrying a composition keyword alongside the `x-go-type` pins
    the response schemas depend on silently loses the pin, and the generated result is a second wire struct that drifts
    from the canonical one. So the union is carried as four OPTIONAL members with a required tag rather than as a schema
    alternation.

    WHAT A CLIENT MUST DO, since no validator can enforce it from this schema alone: send `kind`, send the ONE member
    `kind` names, and send no other. An item carrying no payload does nothing; an item carrying a payload its `kind`
    does not name has two halves that disagree; an item carrying two payloads cannot say which it meant. All three are a
    `400` and nothing in the request is written. A generated client's type will make all four members constructible at
    once — that is the cost of the spelling, and checking it is the client's.

    READING one is the same rule from the other side: dispatch on `kind` and read only that member. The other three are
    absent.

        Attributes:
            kind (ApplyItemKind): Which member below is read. A CLOSED set, unlike a dependency `type`: every value here is
                a verb this operation implements, and an unknown one is a request the server cannot execute rather than a
                workspace's own vocabulary.
            create (ApplyCreateItem | Unset): Creates one issue and optionally NAMES it, so later items can reach the row
                without knowing an id the request has not minted yet.

                It publishes the whole create vocabulary rather than `POST /v0/beads/issues:batchCreate`'s narrow one, and the
                additions are the point: `status`, `sender`, `metadata`, `ephemeral` and `no_history` are the members whose
                absence there makes that operation unusable for a caller composing a real plan.

                THE EDGES ARE NOT HERE. An issue's dependencies and its parent are `dep_add` ITEMS, so the order of every edge
                in the request is total and there is exactly one spelling for an edge. An item carrying comments or dependencies
                on the issue is a `400`.

                `metadata` is the issue's own metadata document and must be a JSON OBJECT where it is present at all. It is
                stored as sent; the resolved ids `metadata_refs` splices are written over its top-level keys after every id in
                the request exists.
            update (ApplyUpdateItem | Unset): Patches one existing issue, under `PATCH /v0/beads/issues/{id}`'s rules.

                The two carry the same preconditions and the same force flags; what is this operation's alone is that its guards
                evaluate AS-MODIFIED — against the row as earlier items of this same request have already changed it — and that
                a miss takes the whole plan down rather than one write.
            close (ApplyCloseItem | Unset): Closes one existing issue, under `POST /v0/beads/issues/{id}:close`'s rules
                including first-close-wins.
            dep_add (ApplyDepAddItem | Unset): Asserts ONE dependency edge, under `POST /v0/beads/dependencies:add`'s rules.
                An edge from a row to itself is a `400`.

                A TARGET NEED NOT BE A ROW THIS DATABASE HOLDS: an `external:` reference and an id belonging to another
                repository are legitimate targets, so only an absence this database can SEE is refused. A SOURCE has no such
                latitude — an edge follows its source, so a source this database holds no row for has no plane to land in.

                `metadata` is the edge's type-specific JSON blob, and an OBJECT where it is present at all. Most edge types
                carry none.

                A WAITS-FOR EDGE IS NORMALIZED RATHER THAN STORED AS ASKED. An absent, empty or `{}` `metadata` on a `waits-for`
                edge is STORED as `{"gate":"all-children"}`, because a stored waits-for row must be self-describing: readers
                predating the gate's introduction do not default a missing one, so an empty gate is a row those readers get
                wrong. A metadata that names a gate keeps it, along with the spawner and also-blocks members a caller may carry,
                and a gate that is neither `all-children` nor `any-children` is a `400`. Nothing else about that member is
                interpreted.

                THERE IS NO TYPED `waits_for` MEMBER, and that is the shape rather than an omission: every measured caller
                already carries the gate as metadata, a typed spelling lowers to these same bytes, and the blob carries members
                a two-field typed member could not express. One spelling, and it is this one.
    """

    kind: ApplyItemKind
    create: ApplyCreateItem | Unset = UNSET
    update: ApplyUpdateItem | Unset = UNSET
    close: ApplyCloseItem | Unset = UNSET
    dep_add: ApplyDepAddItem | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind.value

        create: dict[str, Any] | Unset = UNSET
        if not isinstance(self.create, Unset):
            create = self.create.to_dict()

        update: dict[str, Any] | Unset = UNSET
        if not isinstance(self.update, Unset):
            update = self.update.to_dict()

        close: dict[str, Any] | Unset = UNSET
        if not isinstance(self.close, Unset):
            close = self.close.to_dict()

        dep_add: dict[str, Any] | Unset = UNSET
        if not isinstance(self.dep_add, Unset):
            dep_add = self.dep_add.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "kind": kind,
            }
        )
        if create is not UNSET:
            field_dict["create"] = create
        if update is not UNSET:
            field_dict["update"] = update
        if close is not UNSET:
            field_dict["close"] = close
        if dep_add is not UNSET:
            field_dict["dep_add"] = dep_add

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.apply_close_item import ApplyCloseItem  # noqa: PLC0415
        from ..models.apply_create_item import ApplyCreateItem  # noqa: PLC0415
        from ..models.apply_dep_add_item import ApplyDepAddItem  # noqa: PLC0415
        from ..models.apply_update_item import ApplyUpdateItem  # noqa: PLC0415

        d = dict(src_dict)
        kind = ApplyItemKind(d.pop("kind"))

        _create = d.pop("create", UNSET)
        create: ApplyCreateItem | Unset
        if isinstance(_create, Unset):
            create = UNSET
        else:
            create = ApplyCreateItem.from_dict(_create)

        _update = d.pop("update", UNSET)
        update: ApplyUpdateItem | Unset
        if isinstance(_update, Unset):
            update = UNSET
        else:
            update = ApplyUpdateItem.from_dict(_update)

        _close = d.pop("close", UNSET)
        close: ApplyCloseItem | Unset
        if isinstance(_close, Unset):
            close = UNSET
        else:
            close = ApplyCloseItem.from_dict(_close)

        _dep_add = d.pop("dep_add", UNSET)
        dep_add: ApplyDepAddItem | Unset
        if isinstance(_dep_add, Unset):
            dep_add = UNSET
        else:
            dep_add = ApplyDepAddItem.from_dict(_dep_add)

        apply_item = cls(
            kind=kind,
            create=create,
            update=update,
            close=close,
            dep_add=dep_add,
        )

        return apply_item
