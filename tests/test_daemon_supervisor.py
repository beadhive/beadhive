"""Host-daemon lifecycle/status contract (bh-q0lol.7)."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from beadhive import daemon_contract, daemon_supervisor, host_daemon
from beadhive.cli import app as cli_app

runner = CliRunner()
EXPECTED_DEPENDENCIES = ("hq", "dolt", "bead-state", "run-journals")


def _key(tmp_path, *, host_id: str = "host-1") -> host_daemon.DaemonKey:
    return host_daemon.DaemonKey("uid:1234", str(tmp_path.resolve()), host_id)


def _local_status(key: host_daemon.DaemonKey) -> host_daemon.DaemonStatus:
    return host_daemon.DaemonStatus(
        "running",
        True,
        True,
        "singleton, host identity, and process incarnation verified",
        key,
        host_daemon.ControlRecord(
            contract="beadhive.host-daemon/v1",
            account_id=key.account_id,
            bh_home=key.bh_home,
            host_id=key.host_id,
            instance_id="9d499c00-4d22-4df4-b7bd-68df66572765",
            pid=123,
            process_start="linux:10",
            listener_host="127.0.0.1",
            listener_port=8737,
            started_at="2026-09-03T00:00:00+00:00",
            authentication="a" * 64,
        ),
    )


def _factory_payload(
    key: host_daemon.DaemonKey,
    local: host_daemon.DaemonStatus,
    *,
    readiness: str = "ready",
    accepting_work: bool = True,
    dependencies: tuple[daemon_contract.DependencyStatus, ...] | None = None,
) -> dict:
    assert local.record is not None
    dependencies = dependencies or tuple(
        daemon_contract.DependencyStatus(name=name, status="ready")
        for name in EXPECTED_DEPENDENCIES
    )
    return daemon_contract.FactoryResponse(
        generated_at=1_000,
        host=daemon_contract.FactoryHost(
            host_id=key.host_id,
            service_instance_id=local.record.instance_id,
        ),
        status=daemon_contract.DaemonStatus(
            readiness=readiness,
            accepting_work=accepting_work,
            started_at=900,
            dependencies=dependencies,
        ),
        hives=(),
        capabilities=(),
        coverage=daemon_contract.FactoryCoverage(
            state="complete",
            generated_at=1_000,
            sources={},
        ),
    ).to_wire()


def test_recording_supervisor_lifecycle_is_idempotent_and_keyed(tmp_path):
    backend = daemon_supervisor.RecordingSupervisorBackend()
    first = _key(tmp_path, host_id="host-a")
    second = _key(tmp_path, host_id="host-b")

    assert not backend.status(first).installed
    assert backend.install(first).installed
    assert backend.install(first).installed
    assert backend.start(first).running
    assert backend.start(first).running
    assert not backend.status(second).installed
    assert not backend.stop(first).running
    assert not backend.stop(first).running
    assert not backend.remove(first).installed
    assert not backend.remove(first).installed


def test_lifecycle_refuses_a_selected_backend_result_for_another_identity(tmp_path, monkeypatch):
    expected = _key(tmp_path, host_id="host-a")
    wrong = _key(tmp_path, host_id="host-b")

    class WrongBackend(daemon_supervisor.RecordingSupervisorBackend):
        def start(self, key):
            return replace(super().start(key), key=wrong)

    monkeypatch.setattr(daemon_supervisor, "get_supervisor_backend", WrongBackend)

    with pytest.raises(daemon_supervisor.SupervisorIdentityError, match="another BH_HOME"):
        daemon_supervisor.run_lifecycle("start", key=expected)


def test_status_reports_paths_listener_supervisor_and_authenticated_dependencies(
    tmp_path, monkeypatch
):
    key = _key(tmp_path)
    backend = daemon_supervisor.RecordingSupervisorBackend()
    backend.install(key)
    backend.start(key)
    local = _local_status(key)
    monkeypatch.setattr(host_daemon, "daemon_status", lambda expected: local)

    def request(url, headers, timeout):  # noqa: ARG001
        assert headers == {"Authorization": "Bearer secret"}
        return 200, _factory_payload(
            key,
            local,
            readiness="degraded",
            dependencies=(
                daemon_contract.DependencyStatus(name="hq", status="ready"),
                daemon_contract.DependencyStatus(
                    name="dolt", status="unavailable", reason_code="down"
                ),
                daemon_contract.DependencyStatus(name="bead-state", status="ready"),
                daemon_contract.DependencyStatus(name="run-journals", status="ready"),
            ),
        )

    status = daemon_supervisor.daemon_service_status(
        key=key,
        backend=backend,
        bearer="secret",
        request=request,
    )
    payload = status.payload()

    assert payload["lock"]["path"].endswith(".lock")
    assert payload["lock"]["held"] is True
    assert payload["control_record"]["path"].endswith(".json")
    assert payload["control_record"]["verified"] is True
    assert payload["identity"] == {
        "expected_host_id": key.host_id,
        "reported_host_id": key.host_id,
        "expected_instance_id": local.record.instance_id,
        "reported_instance_id": local.record.instance_id,
        "verified": True,
    }
    assert payload["listener"] == {
        "host": "127.0.0.1",
        "port": 8737,
        "reachable": True,
    }
    assert payload["supervisor"]["running"] is True
    assert payload["readiness"]["authenticated"] is True
    assert payload["readiness"]["state"] == "degraded"
    assert payload["readiness"]["dependencies"][1]["reason_code"] == "down"
    assert "bh host daemon status" in payload["guidance"]["status"]
    assert str(host_daemon.DaemonPaths.for_key(key).control) in payload["guidance"]["control"]


def test_status_rejects_an_authenticated_factory_identity_mismatch(tmp_path, monkeypatch):
    key = _key(tmp_path)
    local = _local_status(key)
    monkeypatch.setattr(host_daemon, "daemon_status", lambda expected: local)

    def request(url, headers, timeout):  # noqa: ARG001
        payload = _factory_payload(key, local)
        payload["host"]["hostId"] = "another-host"
        return 200, payload

    status = daemon_supervisor.daemon_service_status(
        key=key,
        backend=daemon_supervisor.RecordingSupervisorBackend(),
        bearer="secret",
        request=request,
    )

    assert status.listener.reachable
    assert not status.identity.verified
    assert not status.readiness.authenticated
    assert status.readiness.state == "unavailable"
    assert "identity" in status.readiness.detail


@pytest.mark.parametrize(
    "case",
    [
        "missing-schema-version",
        "missing-contract-version",
        "skewed-contract-version",
        "missing-host-identity",
        "missing-readiness",
        "missing-dependencies",
        "missing-accepting-work",
        "malformed-dependencies",
    ],
)
def test_status_fails_closed_on_an_invalid_authenticated_factory_contract(
    tmp_path, monkeypatch, case
):
    key = _key(tmp_path)
    local = _local_status(key)
    monkeypatch.setattr(host_daemon, "daemon_status", lambda expected: local)
    payload = copy.deepcopy(_factory_payload(key, local))
    if case == "missing-schema-version":
        payload.pop("schemaVersion")
    elif case == "missing-contract-version":
        payload.pop("contractVersion")
    elif case == "skewed-contract-version":
        payload["contractVersion"] = "attacker-controlled-version"
    elif case == "missing-host-identity":
        payload["host"].pop("hostId")
    elif case == "missing-readiness":
        payload["status"].pop("readiness")
    elif case == "missing-dependencies":
        payload["status"].pop("dependencies")
    elif case == "missing-accepting-work":
        payload["status"].pop("acceptingWork")
    else:
        payload["status"]["dependencies"] = [
            {
                "name": "attacker-controlled-dependency",
                "status": "attacker-controlled-status",
                "reasonCode": "/secret/path",
            }
        ]

    status = daemon_supervisor.daemon_service_status(
        key=key,
        backend=daemon_supervisor.RecordingSupervisorBackend(),
        bearer="secret",
        request=lambda *_args: (200, payload),
    )

    assert status.state in {"contract-invalid", "contract-mismatch"}
    assert not status.healthy
    assert not status.readiness.authenticated
    assert status.readiness.state == "unavailable"
    assert "attacker" not in status.readiness.detail
    assert "/secret/path" not in status.readiness.detail


@pytest.mark.parametrize(
    ("status_code", "error_code", "expected_state", "authenticated", "retryable"),
    [
        (401, daemon_contract.ErrorCode.UNAUTHORIZED, "authentication-failed", False, False),
        (403, daemon_contract.ErrorCode.FORBIDDEN, "authentication-failed", False, False),
        (503, daemon_contract.ErrorCode.UNAVAILABLE, "readiness-unavailable", True, True),
        (429, daemon_contract.ErrorCode.RATE_LIMITED, "readiness-rate-limited", True, True),
        (409, daemon_contract.ErrorCode.CONFLICT, "readiness-refused", True, True),
    ],
)
def test_status_classifies_only_auth_responses_as_authentication_failure(
    tmp_path,
    monkeypatch,
    status_code,
    error_code,
    expected_state,
    authenticated,
    retryable,
):
    key = _key(tmp_path)
    local = _local_status(key)
    monkeypatch.setattr(host_daemon, "daemon_status", lambda expected: local)
    error = daemon_contract.redacted_error(
        error_code,
        action="reauthenticate" if status_code in {401, 403} else "retry",
    ).to_wire()

    status = daemon_supervisor.daemon_service_status(
        key=key,
        backend=daemon_supervisor.RecordingSupervisorBackend(),
        bearer="secret",
        request=lambda *_args: (status_code, error),
    )

    assert status.state == expected_state
    assert status.readiness.authenticated is authenticated
    assert status.readiness.reason_code == error_code.value
    assert status.readiness.retryable is retryable


def test_ready_but_draining_factory_never_claims_healthy(tmp_path, monkeypatch):
    key = _key(tmp_path)
    local = _local_status(key)
    monkeypatch.setattr(host_daemon, "daemon_status", lambda expected: local)

    status = daemon_supervisor.daemon_service_status(
        key=key,
        backend=daemon_supervisor.RecordingSupervisorBackend(),
        bearer="secret",
        request=lambda *_args: (
            200,
            _factory_payload(key, local, readiness="ready", accepting_work=False),
        ),
    )

    assert status.state == "draining"
    assert status.readiness.accepting_work is False
    assert not status.healthy


@pytest.mark.parametrize(
    ("dependency_payload", "expected_reason"),
    [
        ([], "factory_dependency_set_invalid"),
        (
            [
                {"name": "hq", "status": "ready", "reasonCode": None},
                {"name": "dolt", "status": "ready", "reasonCode": None},
                {"name": "bead-state", "status": "ready", "reasonCode": None},
            ],
            "factory_dependency_set_invalid",
        ),
        (
            [
                {"name": "hq", "status": "ready", "reasonCode": None},
                {"name": "dolt", "status": "ready", "reasonCode": None},
                {"name": "bead-state", "status": "ready", "reasonCode": None},
                {"name": "hq", "status": "ready", "reasonCode": None},
            ],
            "factory_dependency_set_invalid",
        ),
        (
            [
                {"name": "hq", "status": "ready", "reasonCode": None},
                {
                    "name": "dolt",
                    "status": "unavailable",
                    "reasonCode": "dolt_unreachable",
                },
                {"name": "bead-state", "status": "ready", "reasonCode": None},
                {"name": "run-journals", "status": "ready", "reasonCode": None},
            ],
            "factory_readiness_inconsistent",
        ),
    ],
)
def test_ready_factory_requires_a_complete_unique_ready_dependency_set(
    tmp_path, monkeypatch, dependency_payload, expected_reason
):
    key = _key(tmp_path)
    local = _local_status(key)
    monkeypatch.setattr(host_daemon, "daemon_status", lambda expected: local)
    payload = _factory_payload(key, local)
    payload["status"]["dependencies"] = dependency_payload

    status = daemon_supervisor.daemon_service_status(
        key=key,
        backend=daemon_supervisor.RecordingSupervisorBackend(),
        bearer="secret",
        request=lambda *_args: (200, payload),
    )

    assert status.state == "contract-invalid"
    assert status.readiness.reason_code == expected_reason
    assert status.readiness.state == "ready"
    assert status.readiness.accepting_work is True
    assert status.readiness.authenticated is True
    assert len(status.readiness.dependencies) == len(dependency_payload)
    assert not status.healthy


@pytest.mark.parametrize(
    ("error_code", "expected_state", "expected_detail"),
    [
        ("daemon_draining", "draining", "authenticated daemon is draining"),
        (
            "factory_source_unavailable",
            "factory-source-unavailable",
            "authenticated factory source is unavailable",
        ),
    ],
)
def test_status_parses_real_daemon_503_codes_without_reflecting_the_message(
    tmp_path, monkeypatch, error_code, expected_state, expected_detail
):
    key = _key(tmp_path)
    local = _local_status(key)
    monkeypatch.setattr(host_daemon, "daemon_status", lambda expected: local)
    error_payload = {
        "schemaVersion": daemon_contract.WIRE_SCHEMA_VERSION,
        "error": {
            "code": error_code,
            "message": "attacker-controlled /secret/path",
            "retryable": True,
            "action": "retry" if error_code == "daemon_draining" else None,
            "requestId": None,
        },
    }

    status = daemon_supervisor.daemon_service_status(
        key=key,
        backend=daemon_supervisor.RecordingSupervisorBackend(),
        bearer="secret",
        request=lambda *_args: (503, error_payload),
    )

    assert status.state == expected_state
    assert status.readiness.authenticated is True
    assert status.readiness.reason_code == error_code
    assert status.readiness.retryable is True
    assert status.readiness.detail == expected_detail
    assert "attacker" not in json.dumps(status.payload())
    assert "/secret/path" not in json.dumps(status.payload())

    monkeypatch.setattr(daemon_supervisor, "daemon_service_status", lambda: status)
    human = runner.invoke(cli_app, ["host", "daemon", "status"])
    assert human.exit_code == 0, human.output
    assert f"reason={error_code}" in human.stdout
    assert expected_detail in human.stdout
    assert "attacker" not in human.stdout
    assert "/secret/path" not in human.stdout


def test_status_without_bearer_never_starts_or_falls_back(tmp_path, monkeypatch):
    key = _key(tmp_path)
    local = _local_status(key)
    monkeypatch.setattr(host_daemon, "daemon_status", lambda expected: local)
    backend = daemon_supervisor.RecordingSupervisorBackend()

    status = daemon_supervisor.daemon_service_status(key=key, backend=backend, bearer=None)

    assert status.readiness.state == "unknown"
    assert not status.readiness.authenticated
    assert status.listener.reachable is None
    assert backend.calls == [("status", key.digest)]
    assert "credential" in status.readiness.detail


def test_authenticated_probe_brackets_an_ipv6_listener(tmp_path, monkeypatch):
    key = _key(tmp_path)
    local = _local_status(key)
    assert local.record is not None
    local = replace(local, record=replace(local.record, listener_host="::1"))
    monkeypatch.setattr(host_daemon, "daemon_status", lambda expected: local)
    urls = []

    def request(url, headers, timeout):  # noqa: ARG001
        urls.append(url)
        return 200, _factory_payload(key, local)

    daemon_supervisor.daemon_service_status(
        key=key,
        backend=daemon_supervisor.RecordingSupervisorBackend(),
        bearer="secret",
        request=request,
    )
    assert urls == ["http://[::1]:8737/api/v1/factory"]


def test_authenticated_probe_uses_configured_direct_tls(tmp_path, monkeypatch):
    key = _key(tmp_path)
    local = _local_status(key)
    monkeypatch.setattr(host_daemon, "daemon_status", lambda expected: local)
    monkeypatch.setattr(
        daemon_supervisor.config,
        "load",
        lambda: {"host": {"daemon": {"tls": {"enabled": True}}}},
    )
    urls = []

    def request(url, headers, timeout):  # noqa: ARG001
        urls.append(url)
        return 200, _factory_payload(key, local)

    daemon_supervisor.daemon_service_status(
        key=key,
        backend=daemon_supervisor.RecordingSupervisorBackend(),
        bearer="secret",
        request=request,
    )
    assert urls == ["https://127.0.0.1:8737/api/v1/factory"]


def test_enabled_but_invalid_configuration_is_a_structural_setup_advisory():
    advisories = daemon_supervisor.setup_advisories({"host": {"daemon": {"enabled": True}}})
    assert daemon_supervisor.configured({"host": {"daemon": {"enabled": True}}})
    assert advisories[0]["id"] == "host-daemon-structural"
    assert "credential" in advisories[0]["message"]


def test_setup_reports_detect_only_handoff_without_claiming_install(tmp_path, monkeypatch):
    credential = tmp_path / "credentials.json"
    credential.write_text("{}", encoding="utf-8")
    credential.chmod(0o600)
    key = _key(tmp_path)
    monkeypatch.setattr(host_daemon.DaemonKey, "current", classmethod(lambda _cls: key))
    monkeypatch.setattr(
        daemon_supervisor,
        "get_supervisor_backend",
        lambda: daemon_supervisor.DetectOnlySupervisorBackend("systemd-user"),
    )
    monkeypatch.setattr(daemon_supervisor.shutil, "which", lambda _binary: "/bin/present")

    advisories = daemon_supervisor.setup_advisories(
        {
            "host": {
                "daemon": {
                    "enabled": True,
                    "auth": {"credential_file": str(credential.resolve())},
                }
            }
        }
    )

    assert advisories[0]["id"] == "host-daemon-structural"
    assert "detect-only" in advisories[0]["message"]
    assert "bh-q0lol.14" in advisories[0]["message"]
    assert "bh host daemon install" not in advisories[0]["message"]


def test_stopped_status_does_not_probe_the_listener(tmp_path, monkeypatch):
    key = _key(tmp_path)
    monkeypatch.setattr(
        host_daemon,
        "daemon_status",
        lambda expected: host_daemon.DaemonStatus(
            "stopped", False, False, "no daemon owns the singleton", key
        ),
    )

    def unexpected(*_args, **_kwargs):
        pytest.fail("a stopped daemon must not trigger a network probe")

    status = daemon_supervisor.daemon_service_status(
        key=key,
        backend=daemon_supervisor.RecordingSupervisorBackend(),
        bearer="secret",
        request=unexpected,
    )
    assert status.state == "stopped"
    assert status.listener.reachable is False
    assert status.readiness.state == "unavailable"


def test_cli_exposes_explicit_lifecycle_verbs_without_an_ordinary_autostart(tmp_path, monkeypatch):
    key = _key(tmp_path)
    backend = daemon_supervisor.RecordingSupervisorBackend()
    monkeypatch.setattr(daemon_supervisor, "get_supervisor_backend", lambda: backend)
    monkeypatch.setattr(host_daemon.DaemonKey, "current", classmethod(lambda _cls: key))

    for command in ("install", "start", "stop", "rm"):
        result = runner.invoke(cli_app, ["host", "daemon", command, "--json"])
        assert result.exit_code == 0, result.output
        payload = __import__("json").loads(result.stdout)
        assert payload["backend"] == "recording"

    assert [action for action, _digest in backend.calls] == [
        "install",
        "start",
        "stop",
        "remove",
    ]

    # The brief's long-form vocabulary remains accepted but hidden from canonical help.
    result = runner.invoke(cli_app, ["host", "daemon", "remove", "--json"])
    assert result.exit_code == 0, result.output


def test_cli_lifecycle_help_and_failure_report_detect_only_handoff(tmp_path, monkeypatch):
    key = _key(tmp_path)
    monkeypatch.setattr(host_daemon.DaemonKey, "current", classmethod(lambda _cls: key))
    monkeypatch.setattr(
        daemon_supervisor,
        "get_supervisor_backend",
        lambda: daemon_supervisor.DetectOnlySupervisorBackend("systemd-user"),
    )
    monkeypatch.setattr(daemon_supervisor.shutil, "which", lambda _binary: None)

    help_result = runner.invoke(cli_app, ["host", "daemon", "--help"])
    install_result = runner.invoke(cli_app, ["host", "daemon", "install"])

    assert help_result.exit_code == 0, help_result.output
    assert "detect-only" in help_result.stdout
    assert install_result.exit_code == 1
    assert "detect-only" in install_result.output
    assert "bh-q0lol.14" in install_result.output


def test_cli_status_uses_the_shared_structured_computation(tmp_path, monkeypatch):
    key = _key(tmp_path)
    monkeypatch.setattr(host_daemon.DaemonKey, "current", classmethod(lambda _cls: key))
    monkeypatch.setattr(
        host_daemon,
        "daemon_status",
        lambda expected: host_daemon.DaemonStatus(
            "stopped", False, False, "no daemon owns the singleton", expected
        ),
    )
    monkeypatch.setattr(
        daemon_supervisor,
        "get_supervisor_backend",
        lambda: daemon_supervisor.RecordingSupervisorBackend(),
    )

    result = runner.invoke(cli_app, ["host", "daemon", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = __import__("json").loads(result.stdout)
    assert payload["state"] == "stopped"
    assert payload["lock"]["held"] is False
    assert payload["readiness"]["state"] == "unavailable"


def test_human_status_renders_the_same_identity_and_dependencies_as_json_without_paths(
    tmp_path, monkeypatch
):
    key = _key(tmp_path)
    local = _local_status(key)
    monkeypatch.setattr(host_daemon, "daemon_status", lambda expected: local)
    status = daemon_supervisor.daemon_service_status(
        key=key,
        backend=daemon_supervisor.RecordingSupervisorBackend(),
        bearer="secret",
        request=lambda *_args: (
            200,
            _factory_payload(
                key,
                local,
                readiness="degraded",
                dependencies=(
                    daemon_contract.DependencyStatus(name="hq", status="ready"),
                    daemon_contract.DependencyStatus(
                        name="dolt", status="unavailable", reason_code="dolt_unreachable"
                    ),
                    daemon_contract.DependencyStatus(name="bead-state", status="ready"),
                    daemon_contract.DependencyStatus(name="run-journals", status="ready"),
                ),
            ),
        ),
    )
    monkeypatch.setattr(daemon_supervisor, "daemon_service_status", lambda: status)

    human = runner.invoke(cli_app, ["host", "daemon", "status"])
    machine = runner.invoke(cli_app, ["host", "daemon", "status", "--json"])

    assert human.exit_code == machine.exit_code == 0
    payload = json.loads(machine.stdout)
    assert f"expected host: {payload['identity']['expected_host_id']}" in human.stdout
    assert f"reported host: {payload['identity']['reported_host_id']}" in human.stdout
    assert f"expected instance: {payload['identity']['expected_instance_id']}" in human.stdout
    assert f"reported instance: {payload['identity']['reported_instance_id']}" in human.stdout
    assert "hq: ready (reason: none)" in human.stdout
    assert "dolt: unavailable (reason: dolt_unreachable)" in human.stdout
    assert str(tmp_path) not in human.stdout
    assert "secret" not in human.stdout


def test_product_selection_seam_preserves_idempotency_and_exact_key_targeting(
    tmp_path, monkeypatch
):
    key = _key(tmp_path)
    backend = daemon_supervisor.RecordingSupervisorBackend()
    monkeypatch.setattr(daemon_supervisor, "get_supervisor_backend", lambda: backend)

    assert daemon_supervisor.run_lifecycle("install", key=key).installed is True
    assert daemon_supervisor.run_lifecycle("install", key=key).installed is True
    assert daemon_supervisor.run_lifecycle("start", key=key).running is True
    assert daemon_supervisor.run_lifecycle("start", key=key).running is True
    assert {digest for _action, digest in backend.calls} == {key.digest}


def test_product_selection_seam_exposes_typed_detect_only_platform_handoff(tmp_path, monkeypatch):
    key = _key(tmp_path)
    backend = daemon_supervisor.DetectOnlySupervisorBackend("systemd-user")
    monkeypatch.setattr(daemon_supervisor, "get_supervisor_backend", lambda: backend)
    monkeypatch.setattr(daemon_supervisor.shutil, "which", lambda _binary: None)

    state = daemon_supervisor.get_supervisor_backend().status(key)

    assert state.supported is False
    assert state.capability == "detect-only"
    assert "bh-q0lol.14" in state.handoff
    with pytest.raises(daemon_supervisor.SupervisorUnavailableError, match="detect-only"):
        daemon_supervisor.run_lifecycle("install", key=key)
