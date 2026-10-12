"""The receiver-compatible placement wire format (bh-sqfvq; hq-storage-port-adr section 2).

``hq_live_hive_leases`` keeps the trusted receiver's row exactly: ``lease_json`` is the canonical
``{"authority", "lease"}`` carrier the receiver writes and parses, ``request_sha256`` is the
director witness and ``request_id`` is ``CHAR(36)``. The port body is richer, so this module
encodes it onto those columns and decodes it back.

**The carrier body.** A ``PLACEMENT`` body whose keys are ``authority`` and ``lease`` plus an
optional ``cause`` and ``director`` is a carrier:

* ``lease_json`` is ``canonical({"authority", "lease"})`` -- never the cause, which the receiver,
  0.22.x readers and the 0.22.8 bridge would refuse;
* a ``cause`` (``failover`` / ``planned``) rides in ``request_id`` as root's ``cause_token``,
  bound to the prefix and the fresh revision, so a 0.23.x frame still reads it;
* a write without a cause puts the ``OperationId`` in ``request_id``;
* ``director`` may only be ``True`` on a write: the port writer is the director, and the witness
  says so.

Reading a canonical carrier row returns ``{"authority", "lease", "director"}`` plus ``cause`` when
the row is director-written and its ``request_id`` is a cause token. ``director`` is whether the
witness verifies, so a receiver-written row reads ``False``. Any other row decodes as plain JSON
(``{}`` when it is not a JSON object), so a non-canonical carrier never reads as a valid one.

**The witness of a caused write.** ``request_id`` then holds the cause token, so the write's
``OperationId`` is bound into its revision instead: :func:`operation_revision` is
``sha256(prefix, lease_json, operation)``, still the receiver's 64-hex shape and still fresh for
every write, because an ``OperationId`` is minted once per intent and never reused (ADR section
2, rule 3). ``lookup`` confirms a row whose ``request_id`` is the operation or whose revision
is that operation's revision under a verifying witness.

Every encoding here restates a root original in ``beadhive.hq_sql_placement``, which still
parses raw rows on the frame side; ``tests/contracts/test_hq_storage_composition.py`` proves
the two agree.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from beadhive_hq_storage import InvalidRequest, OperationId, Revision

from . import _schema as schema
from ._values import DEFAULT_MAX_BODY_BYTES, canonical, fresh_revision

__all__ = [
    "CAUSE_DOMAIN",
    "OPERATION_DOMAIN",
    "PLACEMENT_CAUSES",
    "PLACEMENT_DOMAIN",
    "PlacementRow",
    "cause_token",
    "decode_placement",
    "encode_placement",
    "operation_revision",
    "placement_cause",
    "placement_seed_statement",
    "placement_witness",
    "placement_witnessed",
]

PLACEMENT_DOMAIN: Final = "beadhive/sql-placement/v1"
"""Root ``hq_sql_placement.PLACEMENT_DOMAIN``: the ``request_sha256`` of a director row."""
CAUSE_DOMAIN: Final = "beadhive/sql-placement-cause/v1"
"""Root ``hq_sql_placement.CAUSE_DOMAIN``: the cause token in a director row's ``request_id``."""
OPERATION_DOMAIN: Final = "beadhive/sql-placement-operation/v1"
"""The revision of a caused write, bound to its ``OperationId`` (its lookup witness)."""
PLACEMENT_CAUSES: Final = ("failover", "planned")
"""Root ``hq_sql_placement.PLACEMENT_CAUSES``."""

_CARRIER: Final = frozenset({"authority", "lease"})
_OPTIONAL: Final = frozenset({"cause", "director"})


def _digest(record: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(record), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def placement_witness(prefix: str, revision: str, body: bytes) -> str:
    """Root ``hq_sql_placement.placement_witness``: the ``request_sha256`` that marks a
    director-written placement row (a receiver row carries a signed-request digest instead)."""
    return _digest(
        {
            "domain": PLACEMENT_DOMAIN,
            "prefix": prefix,
            "revision": revision,
            "lease_sha256": hashlib.sha256(body).hexdigest(),
        }
    )


def cause_token(prefix: str, revision: str, cause: str) -> str:
    """Root ``hq_sql_placement.cause_token``: a UUID-shaped (``CHAR(36)``) digest over the row's
    prefix, its fresh revision and the cause, spelled as an RFC 9562 version 8 UUID."""
    if cause not in PLACEMENT_CAUSES:
        raise InvalidRequest(f"placement cause must be one of {', '.join(PLACEMENT_CAUSES)}")
    digest = _digest(
        {"domain": CAUSE_DOMAIN, "prefix": prefix, "revision": revision, "cause": cause}
    )[:32]
    h = digest[:12] + "8" + digest[13:16] + "89ab"[int(digest[16], 16) & 3] + digest[17:]
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def placement_cause(prefix: str, revision: str, request_id: object) -> str | None:
    """Root ``hq_sql_placement.placement_cause``: the cause a ``request_id`` carries, or
    ``None``. Never raises."""
    if not isinstance(request_id, str) or not isinstance(revision, str):
        return None
    for cause in PLACEMENT_CAUSES:
        if request_id == cause_token(prefix, revision, cause):
            return cause
    return None


def operation_revision(prefix: str, lease_json: bytes, operation: str) -> Revision:
    """The revision of a caused write: bound to its ``OperationId``, which is its witness."""
    return Revision(
        _digest(
            {
                "domain": OPERATION_DOMAIN,
                "prefix": prefix,
                "lease_sha256": hashlib.sha256(lease_json).hexdigest(),
                "operation": operation,
            }
        )
    )


@dataclass(frozen=True)
class PlacementRow:
    """The four columns one placement write sets."""

    revision: Revision
    lease_json: bytes
    request_id: str
    request_sha256: str


def _is_carrier(body: Mapping[str, Any]) -> bool:
    keys = set(body)
    return _CARRIER <= keys and keys <= _CARRIER | _OPTIONAL


def encode_placement(
    prefix: str,
    body: Mapping[str, Any],
    *,
    operation: OperationId,
    cap: int = DEFAULT_MAX_BODY_BYTES,
) -> PlacementRow:
    """The row a ``PLACEMENT`` write stores for ``body`` (refused before any I/O)."""
    if not _is_carrier(body):
        raw = canonical(body, cap=cap)
        revision = fresh_revision({"kind": "placement", "key": prefix, "body": json.loads(raw)})
        return PlacementRow(revision, raw, operation, placement_witness(prefix, revision, raw))
    if body.get("director", True) is not True:
        raise InvalidRequest("a placement written through the port is director-written")
    cause = body.get("cause")
    if "cause" in body and cause not in PLACEMENT_CAUSES:
        raise InvalidRequest(f"placement cause must be one of {', '.join(PLACEMENT_CAUSES)}")
    raw = canonical({"authority": body["authority"], "lease": body["lease"]}, cap=cap)
    if isinstance(cause, str):
        revision = operation_revision(prefix, raw, operation)
        request_id = cause_token(prefix, revision, cause)
    else:
        revision = fresh_revision(
            {"prefix": prefix, "lease_sha256": hashlib.sha256(raw).hexdigest()}
        )
        request_id = operation
    return PlacementRow(revision, raw, request_id, placement_witness(prefix, revision, raw))


def decode_placement(
    prefix: str, revision: str, lease_json: bytes, request_id: object, request_sha256: object
) -> dict[str, Any]:
    """The port body of one stored placement row. Never raises."""
    try:
        value = json.loads(lease_json)
    except (ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    exact = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if set(value) != _CARRIER or exact.encode() != lease_json:
        return value
    director = request_sha256 == placement_witness(prefix, revision, lease_json)
    out: dict[str, Any] = {**value, "director": director}
    cause = placement_cause(prefix, revision, request_id) if director else None
    if cause is not None:
        out["cause"] = cause
    return out


def placement_witnessed(
    prefix: str,
    revision: str,
    lease_json: bytes,
    request_id: object,
    request_sha256: object,
    operation: str,
) -> bool:
    """Whether the stored row is ``operation``'s write (the ``lookup`` witness)."""
    if request_id == operation:
        return True
    return request_sha256 == placement_witness(
        prefix, revision, lease_json
    ) and revision == operation_revision(prefix, lease_json, operation)


def placement_seed_statement(
    prefix: str, revision: str, lease_json: bytes, request_id: str
) -> tuple[str, tuple[Any, ...]]:
    """The operator's one-time provisioning ``INSERT`` for a never-placed hive, with its director
    witness (root renders it for the provisioning account and never executes it)."""
    return (
        f"INSERT INTO {schema.PLACEMENT_TABLE} (prefix,revision,lease_json,request_id,"
        "request_sha256) VALUES (%s,%s,%s,%s,%s)",
        (prefix, revision, lease_json, request_id, placement_witness(prefix, revision, lease_json)),
    )
