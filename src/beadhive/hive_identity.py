"""Compatibility facade for the module-owned registered-hive identity contract.

Consumers retain this import and its registry monkeypatch seam.  Removal is gated by the
explicit hives compatibility ledger; new code imports :mod:`beadhive.modules.hives`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

from . import registry
from .modules.hives.domain.identity import (
    HiveIdentityContractError,
    affiliation_for_kind,
    identity_record,
)
from .modules.hives.domain.models import HiveDiagnostic, HiveIdentityPage

SCHEMA_VERSION = 1
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


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


def identity_page(
    cfg: Mapping[str, Any],
    *,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    source_state: str | None = None,
    source_reason: str | None = None,
    freshness_state: str = "fresh",
    generated_at: int | None = None,
) -> HiveIdentityPage:
    """Project the patched legacy registry into the module's semantic page DTO."""

    entries = () if source_state == "unavailable" else tuple(registry.hives(dict(cfg)))
    return _identity_page_from_entries(
        entries,
        limit=limit,
        cursor=cursor,
        source_state=source_state,
        source_reason=source_reason,
        freshness_state=freshness_state,
        generated_at=generated_at,
    )


def _identity_page_from_entries(
    entries: Sequence[Mapping[str, object]],
    *,
    limit: int,
    cursor: str | None,
    source_state: str | None,
    source_reason: str | None,
    freshness_state: str,
    generated_at: int | None,
) -> HiveIdentityPage:
    if not 1 <= limit <= MAX_LIMIT:
        raise HiveIdentityContractError(
            "invalid_hive_identity_limit", f"limit must be from 1 through {MAX_LIMIT}."
        )
    if source_state not in {None, "complete", "partial", "unavailable"}:
        raise ValueError("source_state must be complete, partial, or unavailable")
    if freshness_state not in {"fresh", "stale", "unknown"}:
        raise ValueError("freshness_state must be fresh, stale, or unknown")

    diagnostics: list[HiveDiagnostic] = []
    records: list[dict[str, str | None]] = []
    if source_state != "unavailable":
        for index, entry in enumerate(entries):
            try:
                record = identity_record(entry)
                records.append(record)
                if record["registration_kind"] is None:
                    diagnostics.append(
                        HiveDiagnostic(
                            "missing_hive_registration_kind",
                            f"Registry entry {index} has identity but no registration kind; "
                            "its affiliation is unavailable.",
                        )
                    )
                elif record["affiliation"] is None:
                    diagnostics.append(
                        HiveDiagnostic(
                            "unsupported_hive_registration_kind",
                            f"Registry entry {index} has an unsupported registration kind; "
                            "its affiliation is unavailable.",
                        )
                    )
            except (KeyError, TypeError, ValueError):
                diagnostics.append(
                    HiveDiagnostic(
                        "invalid_hive_registry_entry",
                        f"Registry entry {index} was omitted because its identity is invalid.",
                    )
                )
        records.sort(key=lambda item: str(item["canonical_id"]))

    coverage_state = source_state or ("partial" if diagnostics else "complete")
    revision = None if coverage_state == "unavailable" else _revision(records)
    if coverage_state == "unavailable":
        if cursor is not None:
            raise HiveIdentityContractError(
                "hive_identity_source_unavailable",
                "The hive registry is unavailable; pagination cannot continue.",
            )
        offset = 0
    else:
        offset = _cursor_offset(cursor, revision=revision or "", size=len(records))
    records_page = records[offset : offset + limit]
    next_offset = offset + len(records_page)
    truncated = next_offset < len(records)
    now = generated_at if generated_at is not None else time.time_ns() // 1_000_000
    return HiveIdentityPage(
        source_revision=revision,
        generated_at=now,
        freshness_state=freshness_state if coverage_state != "unavailable" else "unknown",
        freshness_as_of=now if coverage_state != "unavailable" else None,
        coverage_state=coverage_state,
        coverage_reason=source_reason or ("invalid_registry_entries" if diagnostics else None),
        hives=tuple(records_page),
        total=len(records) if coverage_state == "complete" else None,
        limit=limit,
        truncated=truncated,
        next_cursor=_encode_cursor(revision or "", next_offset) if truncated else None,
        diagnostics=tuple(diagnostics),
    )


def unavailable_page(
    *, limit: int, reason: str = "registry_unavailable", generated_at: int | None = None
) -> HiveIdentityPage:
    return identity_page(
        {},
        limit=limit,
        source_state="unavailable",
        source_reason=reason,
        generated_at=generated_at,
    )


def _page_payload(page: HiveIdentityPage) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "hive list",
        "source_revision": page.source_revision,
        "generated_at": page.generated_at,
        "freshness": {"state": page.freshness_state, "as_of": page.freshness_as_of},
        "coverage": {"state": page.coverage_state, "reason": page.coverage_reason},
        "hives": list(page.hives),
        "returned": len(page.hives),
        "total": page.total,
        "limit": page.limit,
        "truncated": page.truncated,
        "next_cursor": page.next_cursor,
        "warnings": [
            {"code": diagnostic.code, "detail": diagnostic.detail}
            for diagnostic in page.diagnostics
        ],
    }


def list_payload(
    cfg: Mapping[str, Any],
    *,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    source_state: str | None = None,
    source_reason: str | None = None,
    freshness_state: str = "fresh",
    generated_at: int | None = None,
) -> dict[str, Any]:
    """Compatibility wire projection over the typed legacy registry adapter."""

    entries = () if source_state == "unavailable" else tuple(registry.hives(dict(cfg)))
    return _page_payload(
        _identity_page_from_entries(
            entries,
            limit=limit,
            cursor=cursor,
            source_state=source_state,
            source_reason=source_reason,
            freshness_state=freshness_state,
            generated_at=generated_at,
        )
    )


def unavailable_payload(*, limit: int, reason: str = "registry_unavailable") -> dict[str, Any]:
    return _page_payload(unavailable_page(limit=limit, reason=reason))


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "SCHEMA_VERSION",
    "HiveIdentityContractError",
    "affiliation_for_kind",
    "identity_record",
    "identity_page",
    "list_payload",
    "unavailable_page",
    "unavailable_payload",
]
