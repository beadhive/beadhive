"""Pure registered-hive identity and affiliation projection."""

from __future__ import annotations

from collections.abc import Mapping

from .models import HiveIdentity

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
    provider = str(entry["provider"]).strip()
    organization = str(entry["org"]).strip()
    repository = str(entry["repo"]).strip()
    prefix = str(entry["prefix"]).strip()
    if not all((provider, organization, repository, prefix)):
        raise ValueError("hive identity fields must be non-empty")
    try:
        identity = HiveIdentity(provider, organization, repository)
    except ValueError as exc:
        raise ValueError("hive identity components must use canonical segments") from exc
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
