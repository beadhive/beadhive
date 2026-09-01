"""Pure hive identity contract tests."""

from __future__ import annotations

import pytest

from beadhive.modules.hives import (
    HiveIdentity,
    HiveIdentityContractError,
    affiliation_for_kind,
    identity_record,
)


def test_identity_rejects_paths_and_affiliation_is_policy_owned() -> None:
    assert HiveIdentity.parse("github/acme/api").canonical_id == "github/acme/api"
    assert affiliation_for_kind("fork") == "contributor"
    assert affiliation_for_kind("org-native") == "maintainer"
    with pytest.raises(ValueError, match="canonical"):
        HiveIdentity("github", "..", "api")


def test_identity_record_is_semantic_and_transport_neutral() -> None:
    record = identity_record(
        {
            "provider": "github",
            "org": "acme",
            "repo": "api",
            "prefix": "acme-api",
            "kind": "org-native",
        }
    )

    assert record["canonical_id"] == "github/acme/api"
    assert record["affiliation"] == "maintainer"
    assert "schema_version" not in record
    assert "command" not in record


def test_identity_contract_error_retains_semantic_diagnostic() -> None:
    error = HiveIdentityContractError("invalid_identity", "identity is invalid")

    assert error.code == "invalid_identity"
    assert str(error) == "identity is invalid"
