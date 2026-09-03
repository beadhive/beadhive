"""Unified daemon v1 configuration, wire, identity, and attack contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from beadhive import config_partition, daemon_contract
from beadhive.config_schema import BeadhiveConfig
from beadhive.daemon_config import (
    DaemonConfigurationError,
    HostDaemonConfig,
    validate_for_listener_startup,
)

HIVE = "github/beadhive/beadhive"
ENCODED_HIVE = "github%2Fbeadhive%2Fbeadhive"
RUN = "run-1234:attempt.1"
CFG = {
    "managed_repos": [
        {
            "provider": "github",
            "org": "beadhive",
            "repo": "beadhive",
            "prefix": "bh",
            "kind": "org-native",
        },
        {
            "provider": "github",
            "org": "other",
            "repo": "beadhive",
            "prefix": "other-bh",
            "kind": "org-native",
        },
    ]
}


def _private_file(path: Path, content: str = "fixture-verifier\n") -> Path:
    path.write_text(content)
    path.chmod(0o600)
    return path


def _event(**overrides) -> daemon_contract.OperatorEvent:
    values = {
        "hive_id": HIVE,
        "subscription_id": "subscription-1",
        "producer_epoch": "epoch-1",
        "sequence": 2,
        "base_sequence": 1,
        "observed_at": 1_777_000_000_000,
        "generated_at": 1_777_000_000_001,
        "source": "beads",
        "revision": "opaque:r2",
        "entity": None,
        "payload": {"kind": "heartbeat"},
    }
    values.update(overrides)
    return daemon_contract.OperatorEvent(**values)


def _activity(run_id: str = RUN) -> daemon_contract.ActivityAppendRequest:
    return daemon_contract.ActivityAppendRequest(
        run_id=run_id,
        idempotency_key="publisher-1:event-42",
        source="baml",
        kind="provider.progress",
        occurred_at=100,
        expires_at=200,
        payload={"phase": "running"},
    )


def test_daemon_defaults_are_disabled_loopback_authenticated_and_terminal_unavailable() -> None:
    daemon = BeadhiveConfig().host.daemon

    assert daemon.enabled is False
    assert daemon.bind == "127.0.0.1"
    assert daemon.auth.required is True
    assert daemon.cors.allowed_origins == ()
    assert daemon.http.allowed_hosts == ("localhost", "127.0.0.1", "[::1]")
    assert daemon.mcp.mode == "sessionful"
    assert daemon.terminal.available is False
    assert daemon.status.run_journal_stale_after_seconds == 900.0
    assert config_partition.partition_of("host.daemon.bind") == config_partition.HOST


@pytest.mark.parametrize(
    "value",
    [
        {"bind": "0.0.0.0"},
        {"bind": "daemon.example"},
        {"cors": {"allowed_origins": ["*"]}},
        {"http": {"allowed_hosts": ["*"]}},
        {"proxy": {"tls_terminating": True}},
        {"proxy": {"trusted_addresses": ["127.0.0.1"]}},
        {"tls": {"enabled": True}},
        {"enabled": True},
        {
            "shutdown": {
                "graceful_seconds": 5,
                "request_drain_seconds": 4,
                "telemetry_flush_seconds": 2,
            }
        },
        {"mcp": {"session_idle_seconds": 20, "session_absolute_seconds": 10}},
        {"terminal": {"available": True}},
        {"sse": {"client_queue_events": 0}},
        {"status": {"run_journal_stale_after_seconds": 0}},
        {"activity": {"max_inventory_roots": 0}},
        {"activity": {"max_inventory_entries": 0}},
        {"activity": {"max_inventory_bytes": 0}},
    ],
)
def test_insecure_or_invalid_configuration_is_rejected_structurally(value: dict) -> None:
    with pytest.raises(ValidationError):
        HostDaemonConfig(**value)


def test_non_loopback_requires_and_accepts_an_explicit_tls_boundary() -> None:
    direct = HostDaemonConfig(
        bind="192.0.2.10",
        tls={
            "enabled": True,
            "certificate_file": "/secure/cert.pem",
            "private_key_file": "/secure/key.pem",
        },
    )
    proxied = HostDaemonConfig(
        bind="192.0.2.10",
        proxy={"tls_terminating": True, "trusted_addresses": ["192.0.2.2"]},
    )

    assert direct.tls.minimum_version == "TLSv1.3"
    assert proxied.proxy.trusted_addresses == ("192.0.2.2",)


def test_startup_boundary_checks_private_material_before_listener(tmp_path: Path) -> None:
    credential = _private_file((tmp_path / "tokens.json").absolute())
    daemon = HostDaemonConfig(enabled=True, auth={"credential_file": credential})

    assert validate_for_listener_startup(daemon) is daemon

    credential.chmod(0o644)
    with pytest.raises(DaemonConfigurationError, match="group or others"):
        validate_for_listener_startup(daemon)

    credential.chmod(0o000)
    with pytest.raises(DaemonConfigurationError, match="readable by its owner"):
        validate_for_listener_startup(daemon)

    with pytest.raises(DaemonConfigurationError, match="disabled"):
        validate_for_listener_startup(HostDaemonConfig())


def test_startup_boundary_validates_tls_files_without_reading_secrets(tmp_path: Path) -> None:
    credential = _private_file((tmp_path / "tokens.json").absolute())
    certificate = (tmp_path / "cert.pem").absolute()
    certificate.write_text("certificate\n")
    certificate.chmod(0o644)
    key = _private_file((tmp_path / "key.pem").absolute(), "private-key\n")
    daemon = HostDaemonConfig(
        enabled=True,
        bind="192.0.2.10",
        auth={"credential_file": credential},
        tls={"enabled": True, "certificate_file": certificate, "private_key_file": key},
    )

    assert validate_for_listener_startup(daemon) is daemon
    key.chmod(0o640)
    with pytest.raises(DaemonConfigurationError, match="group or others"):
        validate_for_listener_startup(daemon)


def test_startup_boundary_rejects_material_unreadable_to_effective_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = _private_file((tmp_path / "tokens.json").absolute())
    daemon = HostDaemonConfig(enabled=True, auth={"credential_file": credential})
    monkeypatch.setattr("beadhive.daemon_config.os.access", lambda *_args, **_kwargs: False)

    with pytest.raises(DaemonConfigurationError, match="readable by the daemon process"):
        validate_for_listener_startup(daemon)


def test_route_manifest_covers_every_adr_non_mcp_route_scope_and_status() -> None:
    actual = {(route.method, route.path): route for route in daemon_contract.NON_MCP_ROUTES}
    expected = {
        ("GET", "/health"),
        ("GET", "/api/v1/factory"),
        ("GET", "/api/v1/factory/hives"),
        ("GET", "/api/v1/hives/{hive_id}/snapshot"),
        ("GET", "/api/v1/hives/{hive_id}/events"),
        ("GET", "/api/v1/runs/{run_id}/activity"),
        ("POST", "/api/v1/runs/{run_id}/activity"),
        ("POST", "/api/v1/terminal/attach-token"),
        ("WEBSOCKET", "/ws/terminal"),
        ("GET", "/openapi.json"),
    }

    assert set(actual) == expected
    assert actual[("GET", "/health")].scope is None
    assert all(
        route.statuses and len(route.statuses) == len(set(route.statuses))
        for route in actual.values()
    )
    assert all(
        route.scope is not None for key, route in actual.items() if key != ("GET", "/health")
    )
    assert actual[("POST", "/api/v1/runs/{run_id}/activity")].scope == "activity:publish"
    assert actual[("POST", "/api/v1/terminal/attach-token")].scope == "terminal:attach"
    factory_hives = actual[("GET", "/api/v1/factory/hives")]
    assert factory_hives.statuses == (200, 304, 400, 401, 403, 409, 503)
    assert factory_hives.response_model is daemon_contract.FactoryHivePage
    assert "/mcp" not in {route.path for route in actual.values()}
    assert 409 in actual[("GET", "/api/v1/hives/{hive_id}/events")].statuses


def test_wire_models_generate_strict_reusable_json_schemas() -> None:
    for model in daemon_contract.WIRE_MODELS:
        schema = model.model_json_schema(by_alias=True)
        assert schema["type"] == "object"
        assert schema.get("additionalProperties") is False


def test_public_health_is_minimal_and_factory_identity_is_consistent() -> None:
    health = daemon_contract.HealthResponse(status="live", ready=True).to_wire()
    assert set(health) == {"schemaVersion", "status", "ready", "contract"}
    assert not ({"hostId", "serviceInstanceId", "hives", "path", "runId"} & set(health))

    hive = daemon_contract.HiveDescriptor(
        hive_id=HIVE,
        encoded_hive_id=ENCODED_HIVE,
        provider="github",
        org="beadhive",
        repo="beadhive",
        prefix="bh",
        kind="org-native",
        readiness="ready",
    )
    assert hive.to_wire()["hiveId"] == HIVE
    with pytest.raises(ValidationError, match="disagree"):
        hive.model_copy(update={"encoded_hive_id": "github%2Fwrong%2Frepo"}).model_validate(
            {**hive.to_wire(), "encodedHiveId": "github%2Fwrong%2Frepo"}
        )


def test_redacted_errors_ignore_causes_and_do_not_expose_secrets_or_paths() -> None:
    secret = "Bearer top-secret /home/operator/.beadhive/token"
    response = daemon_contract.redacted_error(
        daemon_contract.ErrorCode.INTERNAL,
        request_id="request-1",
        cause=RuntimeError(secret),
    )
    encoded = json.dumps(response.to_wire())

    assert "top-secret" not in encoded
    assert "/home/operator" not in encoded
    assert response.error.message == "The request could not be completed."


def test_full_encoded_hive_identity_round_trips_and_resolves_exactly() -> None:
    assert daemon_contract.encode_hive_id(HIVE) == ENCODED_HIVE
    assert daemon_contract.decode_hive_id(ENCODED_HIVE) == HIVE
    assert daemon_contract.resolve_exact_hive(CFG, ENCODED_HIVE)["prefix"] == "bh"
    unicode_id = "gitea/acme space/répo"
    assert daemon_contract.decode_hive_id(daemon_contract.encode_hive_id(unicode_id)) == unicode_id


@pytest.mark.parametrize(
    ("candidate", "code"),
    [
        ("bh", daemon_contract.IdentityErrorCode.MALFORMED),
        ("beadhive", daemon_contract.IdentityErrorCode.MALFORMED),
        ("beadhive%2Fbeadhive", daemon_contract.IdentityErrorCode.MALFORMED),
        ("github%2Fbeadhive%2Funknown", daemon_contract.IdentityErrorCode.UNKNOWN),
        ("github%2F.%2Fbeadhive", daemon_contract.IdentityErrorCode.DOT_SEGMENT),
        ("github%2F..%2Fbeadhive", daemon_contract.IdentityErrorCode.DOT_SEGMENT),
        ("github%2Fbeadhive%2Fbeadhive%2Fevil", daemon_contract.IdentityErrorCode.MALFORMED),
        ("github%252Fbeadhive%252Fbeadhive", daemon_contract.IdentityErrorCode.UNSAFE_ENCODING),
        ("github%2fbeadhive%2fbeadhive", daemon_contract.IdentityErrorCode.UNSAFE_ENCODING),
        ("github/beadhive/beadhive", daemon_contract.IdentityErrorCode.MALFORMED),
    ],
)
def test_hive_identity_attacks_fail_closed(candidate: str, code) -> None:
    with pytest.raises(daemon_contract.ExactIdentityError) as caught:
        daemon_contract.resolve_exact_hive(CFG, candidate)
    assert caught.value.code is code
    assert candidate not in str(caught.value)


def test_duplicate_exact_registry_rows_are_ambiguous_and_fail_closed() -> None:
    cfg = {"managed_repos": [CFG["managed_repos"][0], CFG["managed_repos"][0]]}
    with pytest.raises(daemon_contract.ExactIdentityError) as caught:
        daemon_contract.resolve_exact_hive(cfg, ENCODED_HIVE)
    assert caught.value.code is daemon_contract.IdentityErrorCode.AMBIGUOUS


def test_exact_outer_run_round_trips_but_prefix_unknown_and_ambiguous_fail() -> None:
    assert daemon_contract.decode_run_id(daemon_contract.encode_run_id(RUN)) == RUN
    assert daemon_contract.resolve_exact_run_id(RUN, [RUN, "run-999"]) == RUN
    for candidate, known, code in (
        ("run-1234", [RUN], daemon_contract.IdentityErrorCode.UNKNOWN),
        ("unknown", [RUN], daemon_contract.IdentityErrorCode.UNKNOWN),
        (RUN, [RUN, RUN], daemon_contract.IdentityErrorCode.AMBIGUOUS),
    ):
        with pytest.raises(daemon_contract.ExactIdentityError) as caught:
            daemon_contract.resolve_exact_run_id(candidate, known)
        assert caught.value.code is code


@pytest.mark.parametrize("candidate", [".", "..", "run%2Fevil", "run%252Fevil", "run/evil"])
def test_run_path_attacks_fail_closed(candidate: str) -> None:
    with pytest.raises(daemon_contract.ExactIdentityError):
        daemon_contract.resolve_exact_run_id(candidate, [candidate, RUN])


def test_activity_path_and_payload_run_identity_must_be_byte_identical() -> None:
    assert daemon_contract.resolve_activity_run_id(RUN, _activity(), [RUN]) == RUN
    with pytest.raises(daemon_contract.ExactIdentityError) as caught:
        daemon_contract.resolve_activity_run_id(RUN, _activity("run-other"), [RUN, "run-other"])
    assert caught.value.code is daemon_contract.IdentityErrorCode.PAYLOAD_MISMATCH


def test_activity_contract_requires_idempotency_expiry_and_finite_json() -> None:
    assert _activity().to_wire()["idempotencyKey"] == "publisher-1:event-42"
    with pytest.raises(ValidationError):
        _activity().model_copy(update={"expires_at": 99}).model_validate(
            {**_activity().to_wire(), "expiresAt": 99}
        )
    with pytest.raises(ValidationError, match="finite JSON"):
        daemon_contract.ActivityAppendRequest(
            **{**_activity().model_dump(), "payload": {"cost": float("nan")}}
        )


def test_sse_cursor_sequence_reset_and_wire_id_are_tied_together() -> None:
    event = _event()
    wire = daemon_contract.encode_sse_event(event)
    assert wire.startswith(b"id: epoch-1:2\nevent: operator-event\n")
    assert b'"baseSequence":1' in wire

    reset = _event(
        sequence=1, base_sequence=None, payload={"kind": "reset", "reason": "source_reset"}
    )
    assert reset.event_id == "epoch-1:1"
    with pytest.raises(ValidationError, match="immediately preceding"):
        _event(base_sequence=0)
    with pytest.raises(ValidationError, match="first event"):
        _event(payload={"kind": "reset", "reason": "late"})


def test_reconnect_cursor_header_and_after_must_match_byte_for_byte() -> None:
    cursor = daemon_contract.resolve_reconnect_cursor(last_event_id="epoch-1:2", after="epoch-1:2")
    assert cursor is not None and cursor.event_id == "epoch-1:2"
    assert daemon_contract.resolve_reconnect_cursor(last_event_id=None, after=None) is None
    with pytest.raises(daemon_contract.ExactIdentityError) as caught:
        daemon_contract.resolve_reconnect_cursor(last_event_id="epoch-1:2", after="epoch-1:3")
    assert caught.value.code is daemon_contract.IdentityErrorCode.CURSOR_MISMATCH
    for bad in ("epoch-1", "epoch-1:02", "epoch:one:2", "../epoch:2"):
        with pytest.raises(daemon_contract.ExactIdentityError):
            daemon_contract.parse_event_cursor(bad)


def test_terminal_contract_is_explicitly_unavailable_until_pty_verdict() -> None:
    unavailable = daemon_contract.TerminalUnavailable().to_wire()
    assert unavailable == {
        "schemaVersion": 1,
        "type": "terminal.unavailable",
        "code": "pty_verdict_pending",
        "message": "Terminal attachment is not available on this host.",
        "retryable": False,
        "verdictBead": "bh-lx6e.3",
    }
    token = daemon_contract.TerminalAttachTokenResponse(
        attach_token="fixture-single-use-token",
        host_id="host-1",
        principal="operator-1",
        target="hive:bh",
        expires_at=200,
    )
    assert token.protocol == token.audience == "bh-terminal.v1"
    assert token.single_use is True


def test_checked_contract_fixture_round_trips_through_public_models() -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "host_daemon" / "v1" / "contracts.json").read_text()
    )

    assert (
        daemon_contract.FactoryResponse.model_validate(fixture["factory"]).to_wire()
        == fixture["factory"]
    )
    assert (
        daemon_contract.FactoryHivePage.model_validate(fixture["factoryHives"]).to_wire()
        == fixture["factoryHives"]
    )
    assert (
        daemon_contract.HiveSnapshotResponse.model_validate(fixture["snapshot"]).to_wire()
        == fixture["snapshot"]
    )
    assert (
        daemon_contract.OperatorEvent.model_validate(fixture["event"]).to_wire() == fixture["event"]
    )
    assert (
        daemon_contract.ActivityAppendRequest.model_validate(fixture["activityAppend"]).to_wire()
        == fixture["activityAppend"]
    )
    assert (
        daemon_contract.TerminalUnavailable.model_validate(fixture["terminalUnavailable"]).to_wire()
        == fixture["terminalUnavailable"]
    )
