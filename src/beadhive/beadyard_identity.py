"""Public compatibility surface for the pure HQ instance identity contract."""

import uuid as uuid

from .modules.config.domain.beadyard_identity import (
    DOCUMENT_PATH,
    BeadyardIdentityError,
    identity_in_documents,
    new_document,
    parse_document,
    parse_id,
    require_same_identity,
    validate_publication_identity,
)

__all__ = [
    "DOCUMENT_PATH",
    "BeadyardIdentityError",
    "identity_in_documents",
    "new_document",
    "parse_document",
    "parse_id",
    "require_same_identity",
    "validate_publication_identity",
]
