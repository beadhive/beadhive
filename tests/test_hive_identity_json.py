"""Machine-readable registered-hive identity and affiliation contract."""

from __future__ import annotations

import json

import pytest

from beadhive import hive, hive_identity


def _entry(
    organization: str,
    repository: str,
    *,
    prefix: str,
    kind: str,
) -> dict[str, str]:
    return {
        "provider": "github",
        "org": organization,
        "repo": repository,
        "prefix": prefix,
        "kind": kind,
    }


def _cfg(*entries: dict[str, str]) -> dict[str, list[dict[str, str]]]:
    return {"managed_repos": list(entries)}


def test_identity_records_are_exact_and_affiliation_is_not_registration_kind() -> None:
    payload = hive_identity.list_payload(
        _cfg(
            _entry("acme", "core", prefix="ac", kind="org-native"),
            _entry("alice", "core", prefix="alice-core", kind="fork"),
            _entry("bob", "widget", prefix="widget", kind="external"),
        ),
        generated_at=1000,
    )

    assert payload["schema_version"] == hive_identity.SCHEMA_VERSION == 1
    assert payload["command"] == "hive list"
    assert payload["coverage"] == {"state": "complete", "reason": None}
    assert payload["freshness"] == {"state": "fresh", "as_of": 1000}
    assert payload["hives"] == [
        {
            "canonical_id": "github/acme/core",
            "prefix": "ac",
            "provider": "github",
            "organization": "acme",
            "repository": "core",
            "display_name": "acme/core",
            "registration_kind": "org-native",
            "affiliation": "maintainer",
        },
        {
            "canonical_id": "github/alice/core",
            "prefix": "alice-core",
            "provider": "github",
            "organization": "alice",
            "repository": "core",
            "display_name": "alice/core",
            "registration_kind": "fork",
            "affiliation": "contributor",
        },
        {
            "canonical_id": "github/bob/widget",
            "prefix": "widget",
            "provider": "github",
            "organization": "bob",
            "repository": "widget",
            "display_name": "bob/widget",
            "registration_kind": "external",
            "affiliation": "contributor",
        },
    ]
    assert "herdr" not in json.dumps(payload).lower()


def test_list_is_bounded_and_cursor_is_bound_to_the_source_revision() -> None:
    cfg = _cfg(
        _entry("acme", "z", prefix="z", kind="personal"),
        _entry("acme", "a", prefix="a", kind="prototype"),
        _entry("acme", "m", prefix="m", kind="org-native"),
    )
    first = hive_identity.list_payload(cfg, limit=2, generated_at=1)
    second = hive_identity.list_payload(cfg, limit=2, cursor=first["next_cursor"], generated_at=2)

    assert [item["repository"] for item in first["hives"]] == ["a", "m"]
    assert (first["returned"], first["total"], first["truncated"]) == (2, 3, True)
    assert [item["repository"] for item in second["hives"]] == ["z"]
    assert second["next_cursor"] is None
    assert second["source_revision"] == first["source_revision"]

    changed = _cfg(*cfg["managed_repos"], _entry("acme", "new", prefix="n", kind="personal"))
    with pytest.raises(hive_identity.HiveIdentityContractError) as error:
        hive_identity.list_payload(changed, limit=2, cursor=first["next_cursor"])
    assert error.value.code == "hive_identity_cursor_revision_mismatch"


def test_partial_stale_and_unavailable_sources_never_claim_complete_totals() -> None:
    partial = hive_identity.list_payload(
        _cfg(
            _entry("acme", "core", prefix="ac", kind="org-native"),
            {"provider": "github", "org": "broken"},
        ),
        freshness_state="stale",
        generated_at=10,
    )
    assert partial["coverage"] == {
        "state": "partial",
        "reason": "invalid_registry_entries",
    }
    assert partial["freshness"]["state"] == "stale"
    assert partial["total"] is None
    assert partial["warnings"][0]["code"] == "invalid_hive_registry_entry"

    unavailable = hive_identity.unavailable_payload(limit=20)
    assert unavailable["source_revision"] is None
    assert unavailable["coverage"] == {
        "state": "unavailable",
        "reason": "registry_unavailable",
    }
    assert unavailable["freshness"] == {"state": "unknown", "as_of": None}
    assert unavailable["hives"] == []
    assert unavailable["total"] is None


def test_partial_identity_is_retained_when_only_affiliation_source_is_missing() -> None:
    entry = _entry("acme", "legacy", prefix="legacy", kind="")
    partial = hive_identity.list_payload(_cfg(entry), generated_at=10)

    assert partial["coverage"]["state"] == "partial"
    assert partial["hives"] == [
        {
            "canonical_id": "github/acme/legacy",
            "prefix": "legacy",
            "provider": "github",
            "organization": "acme",
            "repository": "legacy",
            "display_name": "acme/legacy",
            "registration_kind": None,
            "affiliation": None,
        }
    ]
    assert partial["warnings"][0]["code"] == "missing_hive_registration_kind"

    entry["kind"] = "future-kind"
    future = hive_identity.list_payload(_cfg(entry), generated_at=10)
    assert future["hives"][0]["registration_kind"] == "future-kind"
    assert future["hives"][0]["affiliation"] is None
    assert future["warnings"][0]["code"] == "unsupported_hive_registration_kind"


def test_hive_list_json_emits_the_contract_and_human_output_is_unchanged(
    monkeypatch, capsys
) -> None:
    cfg = _cfg(_entry("acme", "core", prefix="ac", kind="org-native"))
    monkeypatch.setattr(hive.config, "load", lambda: cfg)

    hive.ls(as_json=True)
    machine = json.loads(capsys.readouterr().out)
    assert machine["hives"][0]["canonical_id"] == "github/acme/core"

    monkeypatch.setattr(
        hive,
        "available",
        lambda: {"candidates": [], "registered": ["github/acme/core"]},
    )
    hive.ls()
    assert capsys.readouterr().out == "# Registered hives (1)\n  github/acme/core\n"


def test_hive_list_json_reports_an_unavailable_registry(monkeypatch, capsys) -> None:
    def fail() -> dict:
        raise OSError("synthetic unavailable registry")

    monkeypatch.setattr(hive.config, "load", fail)
    hive.ls(as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["coverage"]["state"] == "unavailable"
    assert payload["hives"] == []
