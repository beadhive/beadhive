from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.ref import Ref


T = TypeVar("T", bound="ApplyCreateItemMetadataRefs")


@_attrs_define
class ApplyCreateItemMetadataRefs:
    """Splices resolved ids into this issue's metadata: each entry writes the id its `Ref` resolves to as the WHOLE VALUE
    of one top-level metadata key.

    IT IS THE ONE PLACE A KEY MAY REACH FORWARD, or name this item's own `key` — see the operation's description. A ref
    here that names a key NO item declares is still a `400`.

    IT IS A TYPED MAP, NOT TEMPLATING. A `${key}` placeholder inside a JSON string would have no escape for a literal
    dollar-brace, would collide with every other templating language a caller's own values might carry, and could not be
    type-checked at all. This is one key, one whole value, one level deep.

    The splice is applied AFTER the row is created, so a consumer of the event stream sees a create and then an update
    on the spliced row.

    """

    additional_properties: dict[str, Ref] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:

        field_dict: dict[str, Any] = {}
        for prop_name, prop in self.additional_properties.items():
            field_dict[prop_name] = prop.to_dict()

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ref import Ref  # noqa: PLC0415

        d = dict(src_dict)
        apply_create_item_metadata_refs = cls()

        additional_properties = {}
        for prop_name, prop_dict in d.items():
            additional_property = Ref.from_dict(prop_dict)

            additional_properties[prop_name] = additional_property

        apply_create_item_metadata_refs.additional_properties = additional_properties
        return apply_create_item_metadata_refs

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Ref:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Ref) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
