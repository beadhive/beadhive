"""Per-key current/carried/absent ledger semantics (bh-1j3ei.6)."""

from __future__ import annotations

import subprocess

import pytest

from beadhive import host, validation_ledger
from beadhive.modules.work.domain.impact import AttestKey, ImpactReceipt, KeyEvidence


@pytest.fixture(autouse=True)
def _host():
    host.mint_if_needed()


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    repo = tmp_path / "ws" / "github" / "org" / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "ws"))
    entry = {"provider": "github", "org": "org", "repo": "repo"}
    key = AttestKey("unit", "just unit", selectors={"pants": "attest:unit"})
    return entry, key


def receipt(key, **changes):
    values = {
        "backend": "pants",
        "backend_version": "2.29",
        "base_tree": "base",
        "head_tree": "head",
        "changed_paths": ("docs/readme.md",),
        "unowned_paths": (),
        "global_inputs_hit": (),
        "invalidated_keys": (),
        "unaffected_keys": (key.name,),
        "evidence": {key.name: KeyEvidence("unaffected", ("tests::",))},
    }
    values.update(changes)
    return ImpactReceipt(**values)


def test_carries_green_current_verdict_with_receipt_provenance(ledger):
    entry, key = ledger
    validation_ledger.record(entry, "base", key.cmd, 0)

    proof = receipt(key)
    assert validation_ledger.carry_key_verdict(entry, key, proof)
    found = validation_ledger.key_verdict(entry, "head", key)

    assert found.state == validation_ledger.KeyVerdictState.CARRIED
    assert found.record["source_tree"] == "base"
    assert found.record["receipt_digest"] == proof.digest
    assert found.record["backend"] == "pants"
    assert found.record["backend_version"] == "2.29"


@pytest.mark.parametrize(
    "setup,changes",
    [
        (None, {}),  # no current source verdict
        (1, {}),  # source is red
        (0, {"unaffected_keys": (), "invalidated_keys": ("unit",)}),
        (
            0,
            {
                "fallback_reason": "resolver failed",
                "unaffected_keys": (),
                "invalidated_keys": ("unit",),
            },
        ),
        (0, {"base_tree": ""}),
        (0, {"head_tree": "base"}),
    ],
)
def test_refuses_each_missing_carry_precondition(ledger, setup, changes):
    entry, key = ledger
    if setup is not None:
        validation_ledger.record(entry, "base", key.cmd, setup)
    assert not validation_ledger.carry_key_verdict(entry, key, receipt(key, **changes))


def test_never_chains_from_a_carried_hop(ledger):
    entry, key = ledger
    validation_ledger.record(entry, "base", key.cmd, 0)
    assert validation_ledger.carry_key_verdict(entry, key, receipt(key))
    onward = receipt(key, base_tree="head", head_tree="next")

    assert not validation_ledger.carry_key_verdict(entry, key, onward)
    assert validation_ledger.key_verdict(entry, "next", key).state == "absent"


def test_carry_obeys_source_ttl(ledger):
    entry, key = ledger
    validation_ledger.record(entry, "base", key.cmd, 0)

    assert not validation_ledger.carry_key_verdict(entry, key, receipt(key), ttl=-1)


def test_three_states_are_distinguishable_and_unknown_is_absent(ledger):
    entry, key = ledger
    assert validation_ledger.key_verdict(entry, "none", key).state == "absent"
    validation_ledger.record(entry, "base", key.cmd, 0)
    assert validation_ledger.key_verdict(entry, "base", key).state == "current"
    assert validation_ledger.carry_key_verdict(entry, key, receipt(key))
    assert validation_ledger.key_verdict(entry, "head", key).state == "carried"
    validation_ledger.record(entry, "unknown", key.cmd, 75)
    unknown = validation_ledger.key_verdict(entry, "unknown", key)
    assert unknown.state == "absent" and unknown.reason == "unknown"


def test_absent_attest_catalog_preserves_legacy_lookup(ledger):
    entry, key = ledger
    validation_ledger.record(entry, "base", key.cmd, 0)

    assert validation_ledger.key_verdicts(entry, "base", ()) == {}
    assert validation_ledger.green_verdict(entry, "base", key.cmd) is not None


def test_disabled_key_ignores_existing_green_and_cannot_write_carry(ledger):
    entry, key = ledger
    disabled = AttestKey(
        key.name,
        key.cmd,
        selectors=key.selectors,
        enabled=False,
        disabled_reason="bounded maintenance",
    )
    validation_ledger.record(entry, "base", key.cmd, 0)

    found = validation_ledger.key_verdict(entry, "base", disabled)
    assert found.state == validation_ledger.KeyVerdictState.ABSENT
    assert found.reason == "disabled"
    assert not validation_ledger.carry_key_verdict(entry, disabled, receipt(disabled))
    assert validation_ledger.key_verdict(entry, "head", disabled).state == "absent"
