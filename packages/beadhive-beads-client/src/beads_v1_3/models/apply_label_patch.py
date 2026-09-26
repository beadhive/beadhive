from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="ApplyLabelPatch")


@_attrs_define
class ApplyLabelPatch:
    """An ordered label edit: `replace` first, then `add`, then `remove`, so REMOVAL WINS when the same label appears in
    more than one member.

    It is the full patch rather than `IssuePatchBody.labels`' complete replacement because a plan edits a set it did not
    compose: replacing would mean reading the labels back first, and the read this operation exists to avoid is exactly
    that one.

    Repetition is free in both directions — a label named twice in one member is applied once, and removing a label the
    issue does not carry is a no-op. An EMPTY-STRING entry is dropped rather than refused: a label row holding ""
    renders as nothing and matches nothing, so refusing the whole request for one stray entry would fail an otherwise-
    good edit.

        Attributes:
            replace (list[str] | Unset): The complete starting label set. An empty array CLEARS every label; omitting the
                member leaves the current set as the starting point.
            add (list[str] | Unset): Labels to add after any replacement.
            remove (list[str] | Unset): Labels to remove after replacement and addition.
    """

    replace: list[str] | Unset = UNSET
    add: list[str] | Unset = UNSET
    remove: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        replace: list[str] | Unset = UNSET
        if not isinstance(self.replace, Unset):
            replace = self.replace

        add: list[str] | Unset = UNSET
        if not isinstance(self.add, Unset):
            add = self.add

        remove: list[str] | Unset = UNSET
        if not isinstance(self.remove, Unset):
            remove = self.remove

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if replace is not UNSET:
            field_dict["replace"] = replace
        if add is not UNSET:
            field_dict["add"] = add
        if remove is not UNSET:
            field_dict["remove"] = remove

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        replace = cast(list[str], d.pop("replace", UNSET))

        add = cast(list[str], d.pop("add", UNSET))

        remove = cast(list[str], d.pop("remove", UNSET))

        apply_label_patch = cls(
            replace=replace,
            add=add,
            remove=remove,
        )

        return apply_label_patch
