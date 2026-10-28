"""Pure classification of the operator's *input* string. Standard library only.

An operator may paste a URL, a bead id or an id from a public store. This module only decides
which of those shapes the text has; it knows no tracker URL grammar (the adapter canonicalises a
URL) and reads nothing. External-intake-module-adr.md section 5 (R1, R2).
"""

from __future__ import annotations

import re
from collections.abc import Collection
from enum import StrEnum

_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://\S+$")
_BEAD_ID = re.compile(r"^([a-z][a-z0-9]*(?:-[a-z0-9]+)*?)-([a-z0-9]{2,}(?:\.[0-9]+)*)$")


class ReferenceShape(StrEnum):
    URL = "url"
    PUBLIC_STORE_ID = "public_store_id"  # a bead id under a public store's prefix (R1)
    BEAD_ID = "bead_id"
    OTHER = "other"  # neither a URL nor a bead id (R2)


def classify_reference(text: str, *, public_prefixes: Collection[str] = ()) -> ReferenceShape:
    """The shape of ``text``. A bead id is public when its prefix names a public store."""
    value = text.strip()
    if _URL.match(value):
        return ReferenceShape.URL
    match = _BEAD_ID.match(value)
    if match is None:
        return ReferenceShape.OTHER
    if any(value.startswith(f"{prefix}-") for prefix in public_prefixes):
        return ReferenceShape.PUBLIC_STORE_ID
    return ReferenceShape.BEAD_ID


__all__ = ["ReferenceShape", "classify_reference"]
