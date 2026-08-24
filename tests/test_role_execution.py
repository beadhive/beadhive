"""Provider-qualified explicit-role launch contract (bh-e8s3i.3)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from beadhive import hitch_plugin, role_execution


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _manifest(binary: Path, provider: str = "claude-code") -> dict:
    packs = [{"name": "core", "version": "1", "digest": "sha256:" + "c" * 64}]
    permissions = {"allow": ["Read"], "ask": [], "deny": ["Bash(git push*)"]}
    authority = {
        "provider": provider,
        "inherit_user_config": False,
        "mcp": {"enabled": False, "servers": []},
    }
    mechanism = "claude-stream-json"
    if provider == "claude-code":
        authority.update(permission_mode="auto", permissions=permissions)
    else:
        authority.update(sandbox="read-only", approval="untrusted")
        mechanism = "codex-jsonl"
    return {
        "version": 1,
        "artifact": binary.name,
        "artifact_digest": _digest(binary.read_bytes()),
        "seat": "developer",
        "provider": provider,
        "driver": "baml-harness",
        "runnable": True,
        "baked": True,
        "profile": "fixture",
        "profile_digest": "sha256:" + "a" * 64,
        "packs_digest": "sha256:" + "b" * 64,
        "contract_version": 1,
        "packs": packs,
        "authority": authority,
        "framing": {"input": "stream-json", "output": "seat-run-jsonl"},
        "live_event_mechanism": mechanism,
        "capabilities": {
            "name": "developer",
            "permission_mode": "auto",
            "permissions": permissions,
            "inherit_user": False,
            "has_mcp": False,
            "mcp_servers": [],
            "packs": packs,
            "baked": {
                "baked": True,
                "provider": provider,
                "profile": "fixture",
                "contract_version": 1,
            },
        },
    }


def _artifact(tmp_path: Path, provider: str = "claude-code") -> tuple[Path, Path, dict]:
    binary = tmp_path / f"bh-developer-{provider}"
    binary.write_bytes(f"packed {provider}".encode())
    binary.chmod(0o755)
    manifest = _manifest(binary, provider)
    manifest_path = binary.with_name(f"{binary.name}.manifest.json")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return binary, manifest_path, manifest


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
def test_exact_qualified_artifact_validates_all_launch_evidence(tmp_path: Path, provider: str):
    binary, manifest_path, _manifest_doc = _artifact(tmp_path, provider)

    resolved = role_execution.validate_qualified_artifact(
        binary, manifest_path, seat="developer", provider=provider
    )

    assert resolved.binary == binary.resolve()
    assert resolved.provider == provider
    assert resolved.artifact_digest == _digest(binary.read_bytes())
    assert resolved.manifest_digest == _digest(manifest_path.read_bytes())


def test_digest_mismatch_refuses_before_any_fallback(tmp_path: Path):
    binary, manifest_path, _manifest_doc = _artifact(tmp_path)
    binary.write_bytes(b"tampered after manifest")

    with pytest.raises(role_execution.RoleLaunchRefused) as exc_info:
        role_execution.validate_qualified_artifact(
            binary, manifest_path, seat="developer", provider="claude-code"
        )

    assert exc_info.value.code == "digest_mismatch"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda doc: doc.update(version=True), "manifest_version_unsupported"),
        (lambda doc: doc.update(driver="claude"), "driver_mismatch"),
        (lambda doc: doc.update(runnable=False), "provider_unavailable"),
        (lambda doc: doc["authority"].update(inherit_user_config=True), "authority_unsupported"),
        (lambda doc: doc.update(framing="stream-json"), "framing_unsupported"),
        (lambda doc: doc.update(live_event_mechanism="none"), "live_mechanism_unsupported"),
    ],
)
def test_incomplete_or_unsupported_manifest_is_fail_closed(tmp_path: Path, mutation, code: str):
    binary, manifest_path, document = _artifact(tmp_path)
    mutation(document)
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(role_execution.RoleLaunchRefused) as exc_info:
        role_execution.validate_qualified_artifact(
            binary, manifest_path, seat="developer", provider="claude-code"
        )

    assert exc_info.value.code == code


def test_baml_required_refuses_missing_codex_without_consulting_hitch(monkeypatch):
    monkeypatch.setattr(role_execution, "resolve_qualified_artifact", lambda *_args: None)
    monkeypatch.setattr(
        hitch_plugin,
        "headless_hitch_plan",
        lambda *_args: pytest.fail("BAML-required request fell back to Hitch"),
    )

    with pytest.raises(role_execution.RoleLaunchRefused) as exc_info:
        role_execution.resolve_headless_plan(
            "developer",
            "codex",
            {},
            explicit_harness=True,
            baml_required=True,
            no_hitch=False,
        )

    assert exc_info.value.code == "artifact_missing"
    assert "bh-developer-codex" in exc_info.value.detail


def test_explicit_provider_never_consults_unqualified_baml_alias(monkeypatch):
    monkeypatch.setattr(role_execution, "resolve_qualified_artifact", lambda *_args: None)
    monkeypatch.setattr(
        hitch_plugin,
        "headless_plan",
        lambda *_args: pytest.fail("explicit provider consulted the unqualified alias planner"),
    )
    monkeypatch.setattr(
        hitch_plugin,
        "headless_hitch_plan",
        lambda *_args: ("hitch", "direct Hitch profile developer"),
    )

    plan = role_execution.resolve_headless_plan(
        "developer",
        "claude",
        {},
        explicit_harness=True,
        baml_required=False,
        no_hitch=False,
    )

    assert plan.backend == "hitch"
    assert plan.provider == "claude-code"


def test_present_but_invalid_artifact_is_never_bypassed_by_hitch(monkeypatch):
    def invalid(*_args):
        raise role_execution.RoleLaunchRefused("digest_mismatch", "tampered")

    monkeypatch.setattr(role_execution, "resolve_qualified_artifact", invalid)
    monkeypatch.setattr(
        hitch_plugin,
        "headless_hitch_plan",
        lambda *_args: pytest.fail("invalid BAML evidence fell back to Hitch"),
    )

    with pytest.raises(role_execution.RoleLaunchRefused, match="tampered"):
        role_execution.resolve_headless_plan(
            "developer",
            "claude",
            {},
            explicit_harness=True,
            baml_required=False,
            no_hitch=False,
        )


def test_unsuitable_seat_refuses_before_artifact_resolution(monkeypatch):
    monkeypatch.setattr(hitch_plugin, "headless_unsuitable", lambda _seat: "attached-only fixture")
    monkeypatch.setattr(
        role_execution,
        "resolve_qualified_artifact",
        lambda *_args: pytest.fail("unsuitable seat consulted artifacts"),
    )

    with pytest.raises(role_execution.RoleLaunchRefused) as exc_info:
        role_execution.resolve_headless_plan(
            "supervisor",
            "claude",
            {},
            explicit_harness=True,
            baml_required=True,
            no_hitch=False,
        )

    assert exc_info.value.code == "seat_unsuitable"


def test_legacy_headless_no_hitch_still_refuses_hitch_only_plan(monkeypatch):
    monkeypatch.setattr(hitch_plugin, "headless_unsuitable", lambda _seat: "")
    monkeypatch.setattr(
        hitch_plugin, "headless_plan", lambda *_args: ("hitch", "only Hitch is installed")
    )

    with pytest.raises(role_execution.RoleLaunchRefused) as exc_info:
        role_execution.resolve_headless_plan(
            "developer",
            "claude",
            {},
            explicit_harness=False,
            baml_required=False,
            no_hitch=True,
        )

    assert exc_info.value.code == "backend_unavailable"
    assert "--no-hitch" in exc_info.value.detail
