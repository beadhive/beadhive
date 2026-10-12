"""Request validation, canonical bodies and revision minting (refused before any I/O).

The rules mirror the port's in-memory fake exactly (``beadhive_hq_storage`` keeps its helpers
private, so they are restated here; the contract suite proves the two agree). The encodings are
the receiver-compatible ones root uses today: canonical JSON is root's
``hq_sql_signatures.canonical`` and a revision is ``sha256(canonical(record) || uuid4)`` in the
64-hex format of ``hq_sql_placement.fresh_revision``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from beadhive_hq_storage import InvalidRequest, OperationId, Revision

__all__ = [
    "DEFAULT_MAX_BODY_BYTES",
    "MAX_KEY_LEN",
    "aware",
    "canonical",
    "check_key",
    "check_operation",
    "decode_body",
    "fresh_revision",
    "quote",
]

DEFAULT_MAX_BODY_BYTES: Final = 64 * 1024
"""Body cap: the receiver's ``_MAX_ROW_BYTES`` (root ``hq_sql_placement``), equal to the fake's."""
MAX_KEY_LEN: Final = 255


def check_key(key: object, what: str = "key") -> str:
    if not isinstance(key, str) or not key or len(key) > MAX_KEY_LEN:
        raise InvalidRequest(f"{what} must be a non-empty string of at most {MAX_KEY_LEN} chars")
    return key


def check_operation(operation: object) -> OperationId:
    if not isinstance(operation, str):
        raise InvalidRequest("operation id must be canonical UUID text")
    try:
        text = str(uuid.UUID(operation))
    except ValueError:
        raise InvalidRequest("operation id must be canonical UUID text") from None
    if text != operation:
        raise InvalidRequest("operation id must be canonical (lower-case, hyphenated) UUID text")
    return OperationId(operation)


def canonical(body: object, *, cap: int = DEFAULT_MAX_BODY_BYTES) -> bytes:
    """``body`` as canonical JSON bytes (root's ``canonical``), refusing non-JSON and oversize."""
    if not isinstance(body, Mapping):
        raise InvalidRequest("body must be a JSON object")
    try:
        text = json.dumps(dict(body), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InvalidRequest(f"body is not canonical JSON: {type(exc).__name__}") from None
    raw = text.encode()
    if len(raw) > cap:
        raise InvalidRequest(f"body exceeds the {cap}-byte cap")
    return raw


def decode_body(raw: object) -> dict[str, Any]:
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    if isinstance(raw, str):
        raw = raw.encode()
    if not isinstance(raw, bytes | bytearray):
        raise InvalidRequest("stored body is not bytes")
    value = json.loads(bytes(raw))
    if not isinstance(value, dict):
        raise InvalidRequest("stored body is not a JSON object")
    return value


def fresh_revision(record: Mapping[str, Any]) -> Revision:
    """``sha256(canonical(record) || uuid4)``: fresh on every write, never a repeatable content
    hash (condition 5), in the receiver's 64-hex format."""
    material = json.dumps(dict(record), sort_keys=True, separators=(",", ":")).encode()
    return Revision(hashlib.sha256(material + uuid.uuid4().hex.encode()).hexdigest())


def aware(value: object) -> datetime:
    """A server ``DATETIME(6)`` (naive UTC from ``UTC_TIMESTAMP(6)``) as an aware UTC datetime."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise InvalidRequest("store returned a non-datetime stamp")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def quote(value: object) -> str:
    """A SQL string literal (backslash and quote escaped), for engines that take no parameters.
    Root's ``fence_schema.quote``."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"
