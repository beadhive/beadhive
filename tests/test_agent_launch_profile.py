import hashlib
import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from beadhive.agent_launch_profile import (
    AgentLaunchProfile,
    AgentLaunchReceipt,
    BeadPolicy,
    agent_launch_receipt_from_env,
    bead_policy_for_seat,
    parse_agent_launch_receipt,
    resolve_agent_launch_profile,
)


@pytest.mark.parametrize(
    ("seat", "policy"),
    [
        ("developer", BeadPolicy.REQUIRED),
        ("planner", BeadPolicy.OPTIONAL),
        ("director", BeadPolicy.FORBIDDEN),
    ],
)
def test_every_bead_policy_class(seat, policy):
    assert bead_policy_for_seat(seat) is policy


def test_managed_profile_defaults_seats_and_round_trips_without_host_fields():
    profile = AgentLaunchProfile(
        managed_bead=True, bead="bh-123", initial_seat="developer", harness="codex"
    )
    assert profile.available_seats == frozenset({"developer"})
    payload = profile.model_dump_json()
    assert "herdr" not in payload and "space" not in payload and "pane" not in payload
    assert AgentLaunchProfile.model_validate_json(payload) == profile


@pytest.mark.parametrize("bead", ["bh-123", "bh-wi2os.1", "ag-run-a1b2.10.child"])
def test_exact_bead_identities_are_accepted(bead):
    profile = AgentLaunchProfile(
        managed_bead=True, bead=bead, initial_seat="developer", harness="codex"
    )
    assert profile.bead == bead


@pytest.mark.parametrize(
    "bead",
    [
        "bh",
        "bh-",
        "-123",
        "BH-123",
        "bh-UPPER",
        "bh-123.",
        "bh-123..2",
        "bh-123/2",
        " bh-123",
        "bh-123 ",
        "bh-123\n",
    ],
)
def test_malformed_or_inexact_bead_identities_are_refused(bead):
    with pytest.raises(ValidationError):
        AgentLaunchProfile(managed_bead=True, bead=bead, initial_seat="developer", harness="codex")


def test_unmanaged_optional_profile_is_valid_but_unmanaged_is_not_a_value():
    profile = AgentLaunchProfile(managed_bead=False, initial_seat="analyst", harness="claude")
    assert profile.bead is None
    with pytest.raises(ValidationError):
        AgentLaunchProfile.model_validate({**profile.model_dump(), "managed_bead": "unmanaged"})


@pytest.mark.parametrize(
    "values",
    [
        dict(managed_bead=False, initial_seat="developer", harness="codex"),
        dict(managed_bead=True, bead="bh-1", initial_seat="supervisor", harness="codex"),
        dict(managed_bead=True, initial_seat="planner", harness="codex"),
        dict(managed_bead=False, bead="bh-1", initial_seat="planner", harness="codex"),
    ],
)
def test_bead_policy_is_validated_at_construction(values):
    with pytest.raises(ValidationError):
        AgentLaunchProfile(**values)


def test_explicit_seats_allow_and_refuse_switches():
    profile = AgentLaunchProfile(
        managed_bead=True,
        bead="bh-123",
        initial_seat="developer",
        available_seats={"developer", "reviewer"},
        harness="codex",
    )
    assert resolve_agent_launch_profile(profile, current_seat="reviewer").current_seat == "reviewer"
    with pytest.raises(ValueError, match="not authorized"):
        resolve_agent_launch_profile(profile, current_seat="merger")


def test_incompatible_available_seat_is_refused_before_resolution():
    with pytest.raises(ValidationError, match="forbids a managed bead"):
        AgentLaunchProfile(
            managed_bead=True,
            bead="bh-123",
            initial_seat="developer",
            available_seats={"developer", "controller"},
            harness="codex",
        )


def test_codex_adapter_normalizes_only_allowlisted_switches():
    profile = AgentLaunchProfile(
        managed_bead=True,
        bead="bh-123",
        initial_seat="developer",
        harness="codex",
        model="  gpt-5.6  ",
        effort=" HIGH ",
    )
    resolved = resolve_agent_launch_profile(profile)
    assert resolved.model == "gpt-5.6"
    assert resolved.effort == "high"
    assert resolved.argv == (
        "codex",
        "--model",
        "gpt-5.6",
        "--config",
        'model_reasoning_effort="high"',
        "--config",
        "developer_instructions="
        '"You are the Beadhive developer seat. Implement only the assigned bead in its managed '
        "worktree, validate it, and submit it for review. Never approve or merge your own work.\"",
    )
    assert resolved.seat_contract_version == "1"
    assert resolved.seat_contract_digest.startswith("sha256:")


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("seat", ["developer", "dispatcher", "planner"])
def test_managed_seat_matrix_has_exact_provider_authority(harness, seat):
    profile = AgentLaunchProfile(
        managed_bead=seat != "planner",
        bead="bh-123" if seat != "planner" else None,
        initial_seat=seat,
        harness=harness,
        model="model-1",
        effort="low",
    )
    resolved = resolve_agent_launch_profile(profile)
    assert resolved.seat_contract_version == "1"
    assert len(resolved.seat_contract_digest) == len("sha256:") + 64
    if harness == "claude":
        assert resolved.argv[:3] == ("claude", "--agent", f"bh:{seat}")
    else:
        instruction = resolved.argv[resolved.argv.index("--config", 5) + 1]
        assert instruction.startswith('developer_instructions="You are the Beadhive ')
        assert f"{seat} seat" in instruction


def test_receipt_rejects_conflicting_contract_evidence():
    receipt = AgentLaunchReceipt.from_resolved(
        resolve_agent_launch_profile(
            AgentLaunchProfile(
                managed_bead=True,
                bead="bh-123",
                initial_seat="developer",
                harness="codex",
            )
        )
    )
    with pytest.raises(ValidationError, match="contract digest"):
        AgentLaunchReceipt.model_validate(
            {**receipt.model_dump(), "seat_contract_digest": "sha256:" + "0" * 64}
        )


def test_another_harness_and_refused_capability():
    profile = AgentLaunchProfile(
        managed_bead=False, initial_seat="analyst", harness="claude", model="sonnet", effort="low"
    )
    assert resolve_agent_launch_profile(profile).argv == (
        "claude",
        "--agent",
        "bh:analyst",
        "--model",
        "sonnet",
        "--effort",
        "low",
    )
    unsupported = AgentLaunchProfile(
        managed_bead=False, initial_seat="analyst", harness="opencode", effort="high"
    )
    with pytest.raises(ValueError, match="not supported"):
        resolve_agent_launch_profile(unsupported)


def test_no_arbitrary_argv_or_unknown_fields():
    with pytest.raises(ValidationError):
        AgentLaunchProfile(
            managed_bead=False,
            initial_seat="analyst",
            harness="claude",
            argv=["sh", "-c", "oops"],
        )
    profile = AgentLaunchProfile(
        managed_bead=False, initial_seat="analyst", harness="claude", model="--danger"
    )
    with pytest.raises(ValueError, match="invalid model"):
        resolve_agent_launch_profile(profile)


def test_schema_is_stable_and_marks_unique_seats():
    schema = AgentLaunchProfile.model_json_schema()
    assert schema["title"] == "AgentLaunchProfile"
    assert schema["properties"]["version"]["const"] == "1"
    seats = schema["properties"]["available_seats"]["anyOf"][0]
    assert seats["type"] == "array" and seats["uniqueItems"] is True
    assert schema["required"] == ["managed_bead", "initial_seat", "harness"]
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical).hexdigest() == (
        "b56a3bb4ddbd0c7fcb3099cb5b83f404170319fc7442b41b4f5978f083431013"
    )


def test_core_fresh_import_does_not_require_herdr():
    probe = """
import importlib.abc
import sys

class RefuseHerdr(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("beadhive.herdr"):
            raise AssertionError(f"core attempted Herdr import: {fullname}")
        return None

sys.meta_path.insert(0, RefuseHerdr())
from beadhive.agent_launch_profile import AgentLaunchProfile, resolve_agent_launch_profile
profile = AgentLaunchProfile(managed_bead=False, initial_seat="planner", harness="opencode")
assert resolve_agent_launch_profile(profile).argv == ("opencode", "--agent", "planner")
assert not any(name.startswith("beadhive.herdr") for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_versioned_core_receipt_is_redacted_and_round_trips():
    resolved = resolve_agent_launch_profile(
        AgentLaunchProfile(
            managed_bead=True,
            bead="bh-wi2os.4",
            initial_seat="developer",
            available_seats={"developer", "reviewer"},
            harness="codex",
            model="gpt-5.6",
            effort="high",
        ),
        current_seat="reviewer",
    )
    receipt = AgentLaunchReceipt.from_resolved(resolved)
    payload = receipt.model_dump_json()
    assert parse_agent_launch_receipt(payload) == receipt
    assert receipt.version == "1" and receipt.current_seat == "reviewer"
    assert "argv" not in payload and "herdr" not in payload.lower()
    assert "instructions" not in payload
    assert receipt.seat_contract_digest == resolved.seat_contract_digest


def test_base_receipt_consumers_fail_closed_on_extensions_and_additive_fields():
    receipt = AgentLaunchReceipt.from_resolved(
        resolve_agent_launch_profile(
            AgentLaunchProfile(managed_bead=False, initial_seat="planner", harness="claude")
        )
    )
    with pytest.raises(ValidationError):
        parse_agent_launch_receipt({**receipt.model_dump(), "future": True})
    with pytest.raises(ValidationError):
        parse_agent_launch_receipt(
            {**receipt.model_dump(), "receipt_type": "beadhive.herdr-agent-launch"}
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"managed_bead": False},
        {"bead": "not exact"},
        {"current_seat": "controller"},
        {"available_seats": ["developer"]},
    ],
)
def test_malformed_or_policy_incompatible_core_receipts_fail_closed(changes):
    receipt = AgentLaunchReceipt.from_resolved(
        resolve_agent_launch_profile(
            AgentLaunchProfile(
                managed_bead=True,
                bead="bh-wi2os.4",
                initial_seat="developer",
                available_seats={"developer", "reviewer"},
                harness="codex",
            ),
            current_seat="reviewer",
        )
    )
    with pytest.raises(ValidationError):
        parse_agent_launch_receipt({**receipt.model_dump(), **changes})


def test_external_harness_without_receipt_is_unmanaged_but_invalid_evidence_is_refused():
    assert agent_launch_receipt_from_env({}) is None
    with pytest.raises(ValidationError):
        agent_launch_receipt_from_env({"BH_AGENT_LAUNCH_RECEIPT": '{"managed_bead":true}'})
