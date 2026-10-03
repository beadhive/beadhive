"""One backend-neutral HQ instance identity carried as an immutable raw document.

``beadyard_id`` names the HQ instance across Git and Dolt storage. It is not an
authentication credential, a storage backend identity, or a frame identifier.
Only explicit HQ creation/adoption may call :func:`new_document`; reads and
ordinary publications must parse an existing document instead.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable

DOCUMENT_PATH = "beadyard.json"


class BeadyardIdentityError(ValueError):
    """The committed identity is missing, malformed, duplicated, or foreign."""


def parse_id(value: object) -> str:
    """Require the canonical lowercase RFC 4122 UUID4 text form."""
    if not isinstance(value, str):
        raise BeadyardIdentityError("beadyard_id must be a canonical UUID4")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise BeadyardIdentityError("beadyard_id must be a canonical UUID4") from None
    if parsed.version != 4 or str(parsed) != value:
        raise BeadyardIdentityError("beadyard_id must be a canonical UUID4")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BeadyardIdentityError("duplicate beadyard identity field")
        result[key] = value
    return result


def parse_document(content: str | bytes) -> str:
    """Read the exact portable identity document without normalizing its raw bytes."""
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError:
            raise BeadyardIdentityError("invalid beadyard identity document") from None
    if not isinstance(content, str):
        raise BeadyardIdentityError("invalid beadyard identity document")
    try:
        document = json.loads(content, object_pairs_hook=_unique_object)
    except (ValueError, TypeError):
        raise BeadyardIdentityError("invalid beadyard identity document") from None
    if not isinstance(document, dict) or set(document) != {"beadyard_id"}:
        raise BeadyardIdentityError("invalid beadyard identity document")
    return parse_id(document["beadyard_id"])


def new_document() -> str:
    """Generate once for an explicit new-HQ/adoption operation, never on a read."""
    return json.dumps({"beadyard_id": str(uuid.uuid4())}, separators=(",", ":")) + "\n"


def identity_in_documents(documents: Iterable[object], *, required: bool = True) -> str | None:
    """Return the one identity in ordered raw documents; distinguish legacy absence."""
    found: str | None = None
    for document in documents:
        if getattr(document, "path", None) != DOCUMENT_PATH:
            continue
        if found is not None:
            raise BeadyardIdentityError("duplicate beadyard identity document")
        found = parse_document(getattr(document, "content", None))
    if found is None and required:
        raise BeadyardIdentityError("beadyard identity missing; explicit adoption required")
    return found


def require_same_identity(source: str, destination: str | None) -> str:
    """Refuse a foreign or unidentified destination before importing/publishing."""
    source = parse_id(source)
    if destination is None:
        raise BeadyardIdentityError("destination beadyard identity missing")
    if source != parse_id(destination):
        raise BeadyardIdentityError("destination belongs to another beadyard")
    return source


def validate_publication_identity(
    previous: Iterable[object],
    proposed: Iterable[object],
    *,
    local_id: str | None = None,
    explicit_adoption: bool = False,
) -> str | None:
    """Keep a bound HQ immutable across a revision's original-parent CAS.

    Legacy revisions may remain unbound. Adding their first identity requires
    an explicit adoption operation, while a first publication on a newly
    created HQ may carry its already durable local identity.
    """
    previous = tuple(previous)
    proposed = tuple(proposed)
    old_id = identity_in_documents(previous, required=False)
    new_id = identity_in_documents(proposed, required=False)
    if old_id is not None and new_id != old_id:
        raise BeadyardIdentityError("published beadyard identity cannot change or disappear")
    if old_id is None and new_id is not None and previous and not explicit_adoption:
        raise BeadyardIdentityError("legacy beadyard identity requires explicit adoption")
    if local_id is not None and new_id != parse_id(local_id):
        raise BeadyardIdentityError("published beadyard identity conflicts with local HQ")
    return new_id
