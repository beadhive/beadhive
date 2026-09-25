from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.apply_metadata_patch_set import ApplyMetadataPatchSet


T = TypeVar("T", bound="ApplyMetadataPatch")


@_attrs_define
class ApplyMetadataPatch:
    """A metadata edit. `replace` is mutually exclusive with the other three; without it the edits apply as `merge`, then
    `set` in key order, then `unset`, so UNSETTING A KEY WINS over setting or merging it. Sending `replace` beside any
    of the others is a `400`.

    `replace` replaces the whole document. Present holding `null`, `{}` or an empty value CLEARS metadata — and clearing
    STORES THE EMPTY JSON DOCUMENT rather than SQL null, so "created with no metadata" and "given metadata and then
    cleared" are the same stored value; a reader must treat absent, empty and `{}` as one value on the way out. `merge`
    must be a nonempty JSON OBJECT and is merged into the current document.

        Attributes:
            replace (Any | Unset): One metadata value: ANY JSON value — string, number, boolean, null, array or object —
                because typed values enter through the explicit JSON metadata path and persist in older rows. It is not a
                string, and a client must not decode it as one.

                Where a member of this type is OMITTED, the key is absent; where it is present holding `null`, the key exists
                and holds null. Those are different states and this surface reports both.
            merge (Any | Unset): One metadata value: ANY JSON value — string, number, boolean, null, array or object —
                because typed values enter through the explicit JSON metadata path and persist in older rows. It is not a
                string, and a client must not decode it as one.

                Where a member of this type is OMITTED, the key is absent; where it is present holding `null`, the key exists
                and holds null. Those are different states and this surface reports both.
            set_ (ApplyMetadataPatchSet | Unset): Individual top-level keys to write, in deterministic key order. A value
                present holding `null` writes JSON null; a key is removed with `unset`, never by sending a null here.
            unset (list[str] | Unset): Top-level keys to remove, applied after every other edit.
    """

    replace: Any | Unset = UNSET
    merge: Any | Unset = UNSET
    set_: ApplyMetadataPatchSet | Unset = UNSET
    unset: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        replace = self.replace

        merge = self.merge

        set_: dict[str, Any] | Unset = UNSET
        if not isinstance(self.set_, Unset):
            set_ = self.set_.to_dict()

        unset: list[str] | Unset = UNSET
        if not isinstance(self.unset, Unset):
            unset = self.unset

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if replace is not UNSET:
            field_dict["replace"] = replace
        if merge is not UNSET:
            field_dict["merge"] = merge
        if set_ is not UNSET:
            field_dict["set"] = set_
        if unset is not UNSET:
            field_dict["unset"] = unset

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.apply_metadata_patch_set import ApplyMetadataPatchSet  # noqa: PLC0415

        d = dict(src_dict)
        replace = d.pop("replace", UNSET)

        merge = d.pop("merge", UNSET)

        _set_ = d.pop("set", UNSET)
        set_: ApplyMetadataPatchSet | Unset
        if isinstance(_set_, Unset):
            set_ = UNSET
        else:
            set_ = ApplyMetadataPatchSet.from_dict(_set_)

        unset = cast(list[str], d.pop("unset", UNSET))

        apply_metadata_patch = cls(
            replace=replace,
            merge=merge,
            set_=set_,
            unset=unset,
        )

        return apply_metadata_patch
