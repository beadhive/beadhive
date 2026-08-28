"""Versioned machine contract for registered hive identity and affiliation.

This projection is deliberately registry-only.  It does not carry host paths, runtime tokens,
or presentation/layout policy, so a CLI, daemon, or UI can share the same canonical identity
without learning how a particular integration renders it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

from . import jsonout, registry

SCHEMA_VERSION = 1
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

_CONTRIBUTOR_KINDS = frozenset({"fork", "external"})
_MAINTAINER_KINDS = frozenset({"org-native", "personal", "prototype"})
_IDENTITY_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")


class HiveIdentityContractError(ValueError):
    """A stable machine-contract failure suitable for a CLI error response."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def affiliation_for_kind(registration_kind: str) -> str:
    """Return the stable ownership role implied by a registered hive kind."""

    if registration_kind in _CONTRIBUTOR_KINDS:
        return "contributor"
    if registration_kind in _MAINTAINER_KINDS:
        return "maintainer"
    raise ValueError(f"unsupported hive registration kind: {registration_kind or '<empty>'}")


def identity_record(entry: Mapping[str, object]) -> dict[str, str | None]:
    """Project one registry entry without requiring consumers to parse its canonical ID."""

    provider = str(entry["provider"]).strip()
    organization = str(entry["org"]).strip()
    repository = str(entry["repo"]).strip()
    prefix = str(entry["prefix"]).strip()
    registration_kind = str(entry.get("kind") or "").strip() or None
    if not all((provider, organization, repository, prefix)):
        raise ValueError("hive identity fields must be non-empty")
    if any(
        part in {".", ".."} or not _IDENTITY_SEGMENT.fullmatch(part)
        for part in (provider, organization, repository)
    ):
        raise ValueError("hive identity components must use canonical segments")
    try:
        affiliation = (
            affiliation_for_kind(registration_kind) if registration_kind is not None else None
        )
    except ValueError:
        affiliation = None
    return {
        "canonical_id": f"{provider}/{organization}/{repository}",
        "prefix": prefix,
        "provider": provider,
        "organization": organization,
        "repository": repository,
        "display_name": f"{organization}/{repository}",
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
    cfg: Mapping[str, Any],
    *,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    source_state: str | None = None,
    source_reason: str | None = None,
    freshness_state: str = "fresh",
    generated_at: int | None = None,
) -> dict[str, Any]:
    """Build the bounded registered-hive identity page.

    ``source_state`` is normally inferred (``complete`` or ``partial``).  Readers of copied
    registry state may explicitly report ``partial``/``unavailable`` and ``stale`` freshness
    without changing the identity schema.
    """

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
        for index, entry in enumerate(registry.hives(dict(cfg))):
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
                        "code": "invalid_hive_registry_entry",
                        "detail": (
                            f"Registry entry {index} was omitted because its identity is invalid."
                        ),
                    }
                )
        records.sort(key=lambda item: str(item["canonical_id"]))

    coverage_state = source_state or ("partial" if warnings else "complete")
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
    page = records[offset : offset + limit]
    next_offset = offset + len(page)
    truncated = next_offset < len(records)
    now = generated_at if generated_at is not None else time.time_ns() // 1_000_000
    return jsonout.envelope(
        "hive list",
        SCHEMA_VERSION,
        {
            "source_revision": revision,
            "generated_at": now,
            "freshness": {
                "state": freshness_state if coverage_state != "unavailable" else "unknown",
                "as_of": now if coverage_state != "unavailable" else None,
            },
            "coverage": {
                "state": coverage_state,
                "reason": source_reason or ("invalid_registry_entries" if warnings else None),
            },
            "hives": page,
            "returned": len(page),
            "total": len(records) if coverage_state == "complete" else None,
            "limit": limit,
            "truncated": truncated,
            "next_cursor": _encode_cursor(revision or "", next_offset) if truncated else None,
            "warnings": warnings,
        },
    )


def unavailable_payload(*, limit: int, reason: str = "registry_unavailable") -> dict[str, Any]:
    """Return a valid empty page when the registry source itself cannot be read."""

    return list_payload({}, limit=limit, source_state="unavailable", source_reason=reason)
