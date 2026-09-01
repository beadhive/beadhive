"""Pure registered-hive identity and affiliation projection."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

from .models import HiveIdentity

SCHEMA_VERSION = 1
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

_CONTRIBUTOR_KINDS = frozenset({"fork", "external"})
_MAINTAINER_KINDS = frozenset({"org-native", "personal", "prototype"})


class HiveIdentityContractError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def affiliation_for_kind(registration_kind: str) -> str:
    if registration_kind in _CONTRIBUTOR_KINDS:
        return "contributor"
    if registration_kind in _MAINTAINER_KINDS:
        return "maintainer"
    raise ValueError(f"unsupported hive registration kind: {registration_kind or '<empty>'}")


def identity_record(entry: Mapping[str, object]) -> dict[str, str | None]:
    identity = HiveIdentity(
        str(entry["provider"]),
        str(entry["org"]),
        str(entry["repo"]),
    )
    prefix = str(entry["prefix"]).strip()
    if not prefix:
        raise ValueError("hive identity fields must be non-empty")
    registration_kind = str(entry.get("kind") or "").strip() or None
    try:
        affiliation = (
            affiliation_for_kind(registration_kind) if registration_kind is not None else None
        )
    except ValueError:
        affiliation = None
    return {
        "canonical_id": identity.canonical_id,
        "prefix": prefix,
        "provider": identity.provider,
        "organization": identity.organization,
        "repository": identity.repository,
        "display_name": f"{identity.organization}/{identity.repository}",
        "registration_kind": registration_kind,
        "affiliation": affiliation,
    }


def _revision(records: Sequence[Mapping[str, object]]) -> str:
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _encode_cursor(revision: str, offset: int) -> str:
    raw = json.dumps(
        {"v": 1, "revision": revision, "offset": offset},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_offset(cursor: str | None, *, revision: str, size: int) -> int:
    if cursor is None:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HiveIdentityContractError(
            "invalid_hive_identity_cursor", "The hive identity cursor is malformed."
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"v", "revision", "offset"}
        or value["v"] != 1
        or not isinstance(value["revision"], str)
        or type(value["offset"]) is not int
        or value["offset"] < 0
        or value["offset"] > size
    ):
        raise HiveIdentityContractError(
            "invalid_hive_identity_cursor", "The hive identity cursor is malformed."
        )
    if value["revision"] != revision:
        raise HiveIdentityContractError(
            "hive_identity_cursor_revision_mismatch",
            "The hive registry changed; restart without a cursor.",
        )
    return value["offset"]


def list_payload(
    entries: Sequence[Mapping[str, object]],
    *,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    source_state: str | None = None,
    source_reason: str | None = None,
    freshness_state: str = "fresh",
    generated_at: int | None = None,
) -> dict[str, Any]:
    if not 1 <= limit <= MAX_LIMIT:
        raise HiveIdentityContractError(
            "invalid_hive_identity_limit", f"limit must be from 1 through {MAX_LIMIT}."
        )
    if source_state not in {None, "complete", "partial", "unavailable"}:
        raise ValueError("source_state must be complete, partial, or unavailable")
    if freshness_state not in {"fresh", "stale", "unknown"}:
        raise ValueError("freshness_state must be fresh, stale, or unknown")

    warnings: list[dict[str, str]] = []
    records: list[dict[str, str | None]] = []
    if source_state != "unavailable":
        for index, entry in enumerate(entries):
            try:
                record = identity_record(entry)
                records.append(record)
                if record["registration_kind"] is None:
                    warnings.append(
                        {
                            "code": "missing_hive_registration_kind",
                            "detail": (
                                f"Registry entry {index} has identity but no registration kind; "
                                "its affiliation is unavailable."
                            ),
                        }
                    )
                elif record["affiliation"] is None:
                    warnings.append(
                        {
                            "code": "unsupported_hive_registration_kind",
                            "detail": (
                                f"Registry entry {index} has an unsupported registration kind; "
                                "its affiliation is unavailable."
                            ),
                        }
                    )
            except (KeyError, TypeError, ValueError):
                warnings.append(
                    {
                        "code": "invalid_hive_identity_entry",
                        "detail": (
                            f"Registry entry {index} was omitted because its identity is invalid."
                        ),
                    }
                )

    records.sort(key=lambda item: str(item["canonical_id"]))
    revision = _revision(records)
    offset = _cursor_offset(cursor, revision=revision, size=len(records))
    page = records[offset : offset + limit]
    next_offset = offset + len(page)
    effective_source_state = source_state or ("partial" if warnings else "complete")
    next_cursor = _encode_cursor(revision, next_offset) if next_offset < len(records) else None
    return {
        "schema": "hive identity",
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at if generated_at is not None else int(time.time()),
        "source": {
            "state": effective_source_state,
            "reason": source_reason,
            "revision": revision,
            "freshness": freshness_state,
        },
        "items": page,
        "page": {
            "limit": limit,
            "count": len(page),
            "total": len(records),
            "next_cursor": next_cursor,
        },
        "warnings": warnings,
    }


def unavailable_payload(*, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    return list_payload(
        (),
        limit=limit,
        source_state="unavailable",
        source_reason="registry_unavailable",
        freshness_state="unknown",
    )
