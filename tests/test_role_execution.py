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


def test_explain_report_discloses_complete_redacted_baml_plan(monkeypatch, tmp_path: Path):
    binary, manifest_path, document = _artifact(tmp_path, "codex")
    document["authority"]["config_references"] = [
        "config-reference-secret-one",
        "config-reference-secret-two",
    ]
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    artifact = role_execution.validate_qualified_artifact(
        binary, manifest_path, seat="developer", provider="codex"
    )
    plan = role_execution.RoleLaunchPlan(
        backend="baml",
        provider="codex",
        artifact=artifact,
        detail="validated provider-qualified fixture",
    )
    monkeypatch.setattr(role_execution, "resolve_headless_plan", lambda *_a, **_kw: plan)

    report = role_execution.explain_report(
        seat="developer",
        harness="codex",
        cfg={},
        entry={"provider": "github", "org": "acme", "repo": "core"},
        hive="github/acme/core",
        workspace="/work/core",
        bead="bh-example.1",
        detached=True,
        task_provided=True,
        explicit_harness=True,
        baml_required=True,
        no_hitch=False,
    )

    assert report["schema_version"] == 1
    assert report["command"] == "role explain"
    assert report["decision"] == "runnable"
    assert report["refusal_reasons"] == []
    assert report["request"]["task"] == {
        "provided": True,
        "content": "<redacted:instructions>",
    }
    assert report["request"]["bamlRequired"] is True
    assert report["execution"] == {
        "driver": "baml-harness",
        "provider": "codex",
        "entry_point": str(binary),
        "mode": "detached",
        "baml_driven": True,
        "provider_qualified": True,
        "detail": "validated provider-qualified fixture",
    }
    assert report["artifact"]["binary_path"] == str(binary)
    assert report["artifact"]["manifest_path"] == str(manifest_path)
    assert report["artifact"]["artifact_digest"] == _digest(binary.read_bytes())
    assert report["artifact"]["manifest_digest"] == _digest(manifest_path.read_bytes())
    assert report["authority"]["ambient_inheritance"] == {
        "user_config": False,
        "capability_user": False,
    }
    assert report["authority"]["sandbox"] == "read-only"
    assert report["authority"]["approval"] == "untrusted"
    assert report["authority"]["config_references"] == {"declared": True, "count": 2}
    outer = report["correlation"]["proposed_outer_run_id"]
    continuation = report["correlation"]["proposed_provider_session_id"]
    assert outer.startswith("run-")
    assert continuation.startswith("provider-")
    assert outer != continuation
    assert report["correlation"]["assignment"]["work_item_id"] == "bh-example.1"
    assert report["correlation"]["state_channel"]["source"] == "bh stream --scope hive"
    assert report["correlation"]["runtime_summary_source"] == "AgentRunSummary"
    assert report["correlation"]["activity_channel"] == {
        "version": "beadhive.run-journal/v1",
        "locator_environment": "BH_RUN_JOURNAL_PATH",
    }
    assert report["observability"] == {
        "pre_exit_possible": True,
        "mechanism": "provider-json",
        "source_mechanism": "codex-jsonl",
        "meets_live_evidence_bar": True,
    }
    assert report["argv"]["environment_names"] == [
        "BH_RUN_JOURNAL_VERSION",
        "BH_RUN_JOURNAL_PATH",
        "BH_RUN_ID",
        "BH_RUN_HIVE",
        "BH_RUN_BEAD",
        "BH_RUN_DRIVER",
        "BH_RUN_PROVIDER",
        "BH_RUN_MANIFEST_DIGEST",
    ]
    serialized = json.dumps(report)
    assert "Bash(git push*)" not in serialized
    assert "config-reference-secret-one" not in serialized
    assert "config-reference-secret-two" not in serialized
    assert "<redacted:instructions>" in serialized
    assert "<redacted:run-context>" in serialized


def test_explain_refusal_reports_reason_without_proposing_identity(monkeypatch):
    refusal = role_execution.RoleLaunchRefused("artifact_missing", "exact Codex artifact missing")
    monkeypatch.setattr(
        role_execution,
        "resolve_headless_plan",
        lambda *_a, **_kw: (_ for _ in ()).throw(refusal),
    )
    monkeypatch.setattr(role_execution.shutil, "which", lambda _name: None)

    report = role_execution.explain_report(
        seat="developer",
        harness="codex",
        cfg={},
        entry=None,
        hive=None,
        workspace="/work/core",
        bead="",
        detached=False,
        task_provided=False,
        explicit_harness=True,
        baml_required=True,
        no_hitch=False,
    )

    assert report["decision"] == "refused"
    assert report["refusal_reasons"] == [
        {"code": "artifact_missing", "detail": "exact Codex artifact missing"}
    ]
    assert report["artifact"]["requested_name"] == "bh-developer-codex"
    assert report["artifact"]["validated"] is False
    assert report["correlation"]["proposed_outer_run_id"] is None
    assert report["correlation"]["proposed_provider_session_id"] is None
    assert report["argv"]["values"] == []


def test_explain_digest_refusal_keeps_evidence_but_not_untrusted_values(
    monkeypatch, tmp_path: Path
):
    binary, manifest_path, document = _artifact(tmp_path, "codex")
    document["credential"] = "manifest-secret-value"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    binary.write_bytes(b"tampered binary")
    monkeypatch.setattr(
        role_execution.shutil,
        "which",
        lambda name: str(binary) if name == "bh-developer-codex" else None,
    )

    report = role_execution.explain_report(
        seat="developer",
        harness="codex",
        cfg={},
        entry=None,
        hive="github/acme/core",
        workspace="/work/core",
        bead="bh-example.1",
        detached=False,
        task_provided=False,
        explicit_harness=True,
        baml_required=True,
        no_hitch=False,
    )

    assert report["decision"] == "refused"
    assert report["refusal_reasons"][0]["code"] == "digest_mismatch"
    assert report["artifact"]["binary_path"] == str(binary)
    assert report["artifact"]["manifest_path"] == str(manifest_path)
    assert report["artifact"]["artifact_digest"] == _digest(b"tampered binary")
    assert report["artifact"]["manifest_digest"] == _digest(manifest_path.read_bytes())
    assert report["artifact"]["validated"] is False
    assert report["authority"] is None
    assert "manifest-secret-value" not in json.dumps(report)


def test_explain_refusal_never_copies_unvalidated_identity_scalars(monkeypatch, tmp_path: Path):
    binary, manifest_path, document = _artifact(tmp_path, "codex")
    markers = {
        "profile": "invalid-profile-secret-marker",
        "seat": "invalid-seat-secret-marker",
        "provider": "invalid-provider-secret-marker",
    }
    document.update(markers)
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(
        role_execution.shutil,
        "which",
        lambda name: str(binary) if name == "bh-developer-codex" else None,
    )

    report = role_execution.explain_report(
        seat="developer",
        harness="codex",
        cfg={},
        entry=None,
        hive="github/acme/core",
        workspace="/work/core",
        bead="bh-example.1",
        detached=False,
        task_provided=False,
        explicit_harness=True,
        baml_required=True,
        no_hitch=False,
    )

    assert report["decision"] == "refused"
    assert report["refusal_reasons"][0]["code"] == "artifact_mismatch"
    assert report["artifact"]["requested_name"] == "bh-developer-codex"
    assert report["artifact"]["binary_path"] == str(binary)
    assert report["artifact"]["manifest_path"] == str(manifest_path)
    assert report["artifact"]["manifest_digest"] == _digest(manifest_path.read_bytes())
    assert report["artifact"]["profile"] is None
    assert report["artifact"]["seat"] is None
    assert report["artifact"]["provider"] is None
    serialized = json.dumps(report)
    for marker in markers.values():
        assert marker not in serialized


def test_explain_direct_hitch_is_never_labeled_baml(monkeypatch, tmp_path: Path):
    plan = role_execution.RoleLaunchPlan(
        backend="hitch",
        provider="claude-code",
        detail="direct Hitch fixture",
        hitch_target="claude-code",
        hitch_profile="developer",
    )
    monkeypatch.setattr(role_execution, "resolve_headless_plan", lambda *_a, **_kw: plan)
    monkeypatch.setattr(
        role_execution.shutil,
        "which",
        lambda name: "/usr/bin/hitch" if name == "hitch" else None,
    )
    cfg = {"hitch": {"repo": str(tmp_path), "command": "hitch"}}

    report = role_execution.explain_report(
        seat="developer",
        harness="claude",
        cfg=cfg,
        entry=None,
        hive="github/acme/core",
        workspace="/work/core",
        bead="bh-example.1",
        detached=False,
        task_provided=True,
        explicit_harness=True,
        baml_required=False,
        no_hitch=False,
    )

    assert report["execution"]["driver"] == "hitch-direct"
    assert report["execution"]["baml_driven"] is False
    assert report["execution"]["provider_qualified"] is False
    assert report["artifact"]["validated"] is False
    assert report["observability"]["pre_exit_possible"] is False
    argv = report["argv"]["values"]
    assert argv[:4] == ["hitch", "up", "claude-code", "developer"]
    assert argv[argv.index("--task") + 1] == "<redacted:instructions>"


def test_explain_refuses_hitch_plan_when_executable_is_unavailable(monkeypatch):
    plan = role_execution.RoleLaunchPlan(
        backend="hitch",
        provider="claude-code",
        detail="configured Hitch profile",
        hitch_target="claude-code",
        hitch_profile="developer",
    )
    monkeypatch.setattr(role_execution, "resolve_headless_plan", lambda *_a, **_kw: plan)
    monkeypatch.setattr(role_execution.shutil, "which", lambda _name: None)

    report = role_execution.explain_report(
        seat="developer",
        harness="claude",
        cfg={"hitch": {"command": "missing-hitch"}},
        entry=None,
        hive=None,
        workspace="/work/core",
        bead="",
        detached=False,
        task_provided=False,
        explicit_harness=True,
        baml_required=False,
        no_hitch=False,
    )

    assert report["decision"] == "refused"
    assert report["refusal_reasons"][0]["code"] == "hitch_unavailable"
    assert report["execution"]["driver"] is None
    assert report["correlation"]["proposed_outer_run_id"] is None


def test_explain_redacts_unqualified_configured_command_arguments(monkeypatch):
    plan = role_execution.RoleLaunchPlan(
        backend="baml", provider=None, detail="compatibility alias fixture"
    )
    monkeypatch.setattr(role_execution, "resolve_headless_plan", lambda *_a, **_kw: plan)
    monkeypatch.setattr(role_execution.shutil, "which", lambda _name: None)
    from beadhive import config

    monkeypatch.setattr(
        config,
        "dispatch_seat_command",
        lambda *_a: "seat-wrapper --api-key credential-value --role {role}",
    )
    monkeypatch.setattr(config, "dispatch_seat_bundle", lambda *_a: "")

    report = role_execution.explain_report(
        seat="developer",
        harness="claude",
        cfg={},
        entry=None,
        hive=None,
        workspace="/work/core",
        bead="",
        detached=False,
        task_provided=False,
        explicit_harness=False,
        baml_required=False,
        no_hitch=False,
    )

    serialized = json.dumps(report)
    assert "credential-value" not in serialized
    assert report["execution"]["entry_point"] == "seat-wrapper"
    assert report["argv"]["values"][:5] == [
        "seat-wrapper",
        "<redacted:configured-argument>",
        "<redacted:configured-argument>",
        "<redacted:configured-argument>",
        "<redacted:configured-argument>",
    ]
