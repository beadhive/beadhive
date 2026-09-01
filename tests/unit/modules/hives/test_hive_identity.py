"""Pure hive identity contract tests."""

from __future__ import annotations

import pytest

from beadhive.modules.hives import (
    HiveIdentity,
    HiveIdentityContractError,
    affiliation_for_kind,
    identity_record,
    list_payload,
)


def test_identity_rejects_paths_and_affiliation_is_policy_owned() -> None:
    assert HiveIdentity.parse("github/acme/api").canonical_id == "github/acme/api"
    assert affiliation_for_kind("fork") == "contributor"
    assert affiliation_for_kind("org-native") == "maintainer"
    with pytest.raises(ValueError, match="canonical"):
        HiveIdentity("github", "..", "api")


def test_identity_page_is_bounded_sorted_and_cursor_stable() -> None:
    entries = [
        {"provider": "github", "org": "zed", "repo": "web", "prefix": "zed-web", "kind": "fork"},
        {
            "provider": "github",
            "org": "acme",
            "repo": "api",
            "prefix": "acme-api",
            "kind": "org-native",
        },
    ]

    first = list_payload(entries, limit=1, generated_at=7)
    second = list_payload(entries, limit=1, cursor=first["page"]["next_cursor"], generated_at=8)

    assert first["items"][0] == identity_record(entries[1])
    assert first["items"][0]["affiliation"] == "maintainer"
    assert second["items"][0]["canonical_id"] == "github/zed/web"
    assert second["page"]["next_cursor"] is None


def test_identity_cursor_fails_closed_when_registry_revision_changes() -> None:
    first = list_payload(
        [{"provider": "github", "org": "acme", "repo": "api", "prefix": "aa", "kind": "personal"}],
        limit=1,
    )
    cursor = first["page"]["next_cursor"]
    assert cursor is None

    with pytest.raises(HiveIdentityContractError, match="malformed"):
        list_payload([], cursor="not-a-cursor")
