"""Closing live-contract handoff for the authenticated unified host daemon."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport

from beadhive import config, daemon_auth, daemon_state_broker, host_daemon, run_journal
from beadhive.daemon_config import HostDaemonConfig
from beadhive.daemon_contract import AuthScope

_raw_build_product_application = host_daemon.build_product_application


def _build_product_application(**kwargs):
    return _raw_build_product_application(
        state_broker_factory=daemon_state_broker.DaemonStateBroker.for_host,
        **kwargs,
    )


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "host_daemon" / "v1" / "live-contract.json"
DOC = ROOT / "docs" / "HOST-DAEMON.md"
HIVE = "github/beadhive/live-contract"
EVIDENCE_CELLS = {
    "liveConcurrentComposition",
    "schemaResultErrorParity",
    "httpSessionModesAndExhaustion",
    "exactIdentityAttacks",
    "scopesExpiryRevocationRotation",
    "tlsCorsHostOriginProxyTrust",
    "concurrentGuardedMutations",
    "snapshotAtomicity",
    "sseReplayResetBackpressure",
    "activityAfterDurableAck",
    "activityDedupeRetryAndSources",
    "daemonRestartAndSessionInvalidation",
    "landedSupervisorContracts",
    "landedContainerContract",
    "boundedTelemetry",
    "redaction",
    "directCliAndStdioDuringOutage",
    "openapiAndTypedTerminalUnavailable",
}


def _run(
    *argv: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=True,
    )


def _seed_real_hive(workspace: Path) -> tuple[Path, str]:
    hive = workspace / "github" / "beadhive" / "live-contract"
    hive.mkdir(parents=True)
    _run("git", "init", "-q", cwd=hive)
    _run("git", "config", "user.name", "Live Contract", cwd=hive)
    _run("git", "config", "user.email", "live-contract@example.invalid", cwd=hive)
    _run(
        "bd",
        "init",
        "--non-interactive",
        "--prefix",
        "lc",
        "--skip-agents",
        "--skip-hooks",
        cwd=hive,
    )
    created = _run(
        "bd",
        "create",
        "Live contract seed",
        "--type",
        "task",
        "--priority",
        "1",
        "--json",
        cwd=hive,
    )
    return hive, json.loads(created.stdout)["id"]


def _control_record(home: Path, port: int) -> host_daemon.ControlRecord:
    return host_daemon.ControlRecord(
        contract=host_daemon.CONTRACT_VERSION,
        account_id="uid:live-contract",
        bh_home=str(home),
        host_id="host-live-contract",
        instance_id="instance-live-contract",
        pid=os.getpid(),
        process_start="test:1",
        listener_host="127.0.0.1",
        listener_port=port,
        started_at="2026-09-04T00:00:00+00:00",
    )


def test_checked_handoff_pins_the_exact_ui_endpoint_recipe_and_release_boundary() -> None:
    contract = json.loads(FIXTURE.read_text())

    assert contract["schemaVersion"] == 1
    assert contract["contractVersion"] == "beadhive.host-daemon/v1"
    assert contract["consumerGate"] == "bhui-unkw"
    assert contract["consumerMatrix"] == "bhui-61s9.11"
    assert contract["profile"] == "authenticated-opt-in"
    assert contract["baseUrl"] == "http://127.0.0.1:8420"
    assert contract["mcp"] == {
        "route": "/mcp",
        "defaultMode": "sessionful",
        "supportedModes": ["sessionful", "stateless"],
        "fallback": "stdio",
    }
    assert contract["operator"] == {
        "factory": "/api/v1/factory",
        "factoryHives": "/api/v1/factory/hives",
        "snapshot": "/api/v1/hives/{encodedHiveId}/snapshot",
        "events": "/api/v1/hives/{encodedHiveId}/events",
        "activity": "/api/v1/runs/{encodedRunId}/activity",
        "openapi": "/openapi.json",
        "health": "/health",
    }
    assert contract["cursor"] == {
        "snapshotField": "cursor",
        "resumeHeader": "Last-Event-ID",
        "resumeQuery": "after",
        "eventId": "{producerEpoch}:{sequence}",
        "onConflictOrReset": "discard-live-state-and-resnapshot",
    }
    assert contract["sources"] == ["baml", "hitch", "beadhive"]
    assert contract["terminal"] == {
        "available": False,
        "type": "terminal.unavailable",
        "code": "pty_verdict_pending",
        "verdictBead": "bh-lx6e.3",
    }
    assert contract["remoteHq"] == {
        "available": False,
        "limitationBead": "bh-pc2a.30",
        "reason": "hq-remote-is-ssh-only",
    }
    assert contract["nodeRelay"] == {
        "role": "test-oracle-and-rollback",
        "retireAfter": "bhui-61s9",
    }
    assert contract["liveProof"] == {
        "transport": "uvicorn-tcp",
        "source": "real-bd",
        "overlapBarrier": "durable-append-held",
        "deliveryBoundary": "durable-ack-before-sse",
        "outageSequence": "refusal-cli-stdio-refusal",
        "teardown": [
            "fastmcp",
            "credential-sessions",
            "operator-sse",
            "state-broker",
            "telemetry",
            "uvicorn",
        ],
        "evidenceNode": (
            "tests/test_daemon_live_contract.py::"
            "test_real_product_app_composes_concurrent_cli_stdio_http_operator_and_activity"
        ),
    }
    assert contract["finalPlatformEvidenceOwner"] == "bh-q0lol"
    assert contract["finalPlatformEvidence"] == {
        "owner": "bh-q0lol",
        "stage": "integration",
        "sourceRevision": "exact-final-tip",
        "containerCells": "7/7",
    }
    assert contract["releaseCertification"] == {
        "owner": "bh-hxbln",
        "matrix": "bh-hxbln.1",
        "stage": "release",
        "status": "deferred",
        "platforms": [
            "darwin-launchagent",
            "persistent-linux-systemd-user",
        ],
        "requiredContainerMatrix": {
            "cells": "all-7",
            "sourceRevision": "same-exact-release-candidate",
            "freshnessWindowDays": 7,
            "purpose": "release-matrix-coherence",
        },
    }
    assert "containerMatrixMayRerun" not in contract["releaseCertification"]
    assert contract["finalPlatformEvidence"]["owner"] != contract["releaseCertification"]["owner"]


def test_every_live_contract_cell_links_to_an_exact_collected_test() -> None:
    contract = json.loads(FIXTURE.read_text())
    evidence = contract["executableEvidence"]

    assert set(evidence) == EVIDENCE_CELLS
    nodes = sorted({node for cell in evidence.values() for node in cell})
    assert all(evidence.values())
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *nodes],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    collected_nodes = tuple(
        line for line in collected.stdout.splitlines() if line.startswith("tests/")
    )
    for node in nodes:
        assert any(actual == node or actual.startswith(f"{node}[") for actual in collected_nodes)


def test_operator_runbook_checks_install_credentials_migration_outage_and_rollback() -> None:
    text = DOC.read_text()

    for required in (
        "# Authenticated host daemon",
        "## Install and credentials",
        "## Opt-in migration",
        "## Exact UI endpoint recipe",
        "## Daemon-down behavior",
        "## Rollback",
        "terminal.unavailable",
        "bh-lx6e.3",
        "bh-pc2a.30",
        "bhui-unkw",
        "bhui-61s9.11",
        "Node relay",
        "test oracle and rollback",
        "durable-append-held",
        "durable-ack-before-sse",
        "refusal-cli-stdio-refusal",
        "MCP stdio",
        "Last-Event-ID",
        "producerEpoch",
        "activity:publish",
        "operator:read",
        "mcp:control",
        "exact-final-tip",
        "container 7/7",
        "bh-hxbln",
        "bh-hxbln.1",
        "must rerun all seven",
        "same exact release-candidate revision",
        "seven-day freshness window",
        "release-matrix coherence",
    ):
        assert required in text


def test_real_product_app_composes_concurrent_cli_stdio_http_operator_and_activity(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    monkeypatch.setenv("BH_HOME", str(home))
    monkeypatch.setenv("GIT_WORKSPACE", str(workspace))
    hive, bead_id = _seed_real_hive(workspace)
    cfg = {
        "schema_version": 1,
        "providers": ["github"],
        "managed_repos": [
            {
                "provider": "github",
                "org": "beadhive",
                "repo": "live-contract",
                "prefix": "lc",
                "kind": "org-native",
            }
        ],
        "exclude": {"orgs": [], "repos": []},
        "otel": {"enabled": False, "protocol": "grpc"},
    }
    (home / "config.yaml").write_text(json.dumps(cfg))

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])

    credential_path = (home / "daemon-credentials.json").absolute()
    credential = daemon_auth.provision_credential_file(
        credential_path,
        credential_id="live-contract",
        audience="beadhive-host",
        principal="service:baml:developer",
        scopes=(
            AuthScope.MCP_CONTROL,
            AuthScope.OPERATOR_READ,
            AuthScope.ACTIVITY_PUBLISH,
        ),
        expires_at=int(time.time()) + 3_600,
    )
    bearer = credential.bearer.reveal_for_authority()
    settings = HostDaemonConfig(
        enabled=True,
        port=port,
        auth={"credential_file": credential_path},
        http={"allowed_hosts": ["127.0.0.1"]},
        cors={"allowed_origins": ["http://127.0.0.1:3000"]},
        mcp={"mode": "sessionful", "max_sessions": 4},
        shutdown={
            "graceful_seconds": 3,
            "request_drain_seconds": 2,
            "telemetry_flush_seconds": 1,
        },
    )
    record = _control_record(home, port)
    identity = run_journal.RunIdentity(
        hive=HIVE,
        bead=bead_id,
        driver="baml",
        provider="codex",
        manifest_digest="sha256:" + "a" * 64,
    )
    journal = run_journal.RunJournal.create(
        identity,
        run_id="run-live-contract",
        writer=run_journal.WRITER_BAML,
    )
    runtime = host_daemon.DaemonRuntime(shutdown_budget=3)
    app = _build_product_application(
        runtime=runtime,
        control_record=record,
        cfg=cfg,
        settings=settings,
    )
    append_entered = threading.Event()
    allow_append = threading.Event()
    durable_ack = threading.Event()
    proof_order: list[str] = []
    actual_append = app.state.activity_store.append

    def gated_append(*args, **kwargs):
        append_entered.set()
        if not allow_append.wait(timeout=5):
            raise AssertionError("live proof did not release the durable append barrier")
        result = actual_append(*args, **kwargs)
        proof_order.append("durable-ack")
        durable_ack.set()
        return result

    monkeypatch.setattr(app.state.activity_store, "append", gated_append)

    async def exercise() -> None:
        auth = {"Authorization": f"Bearer {bearer}"}
        base_url = f"http://127.0.0.1:{port}"
        http_mcp = StreamableHttpTransport(
            f"{base_url}/mcp",
            headers=auth,
        )

        def stdio_transport(log_name: str) -> StdioTransport:
            return StdioTransport(
                command=sys.executable,
                args=["-m", "beadhive.mcp"],
                cwd=str(hive),
                env={
                    **os.environ,
                    "BH_HOME": str(home),
                    "GIT_WORKSPACE": str(workspace),
                    "OTEL_SDK_DISABLED": "true",
                },
                keep_alive=False,
                log_file=tmp_path / log_name,
            )

        stdio_mcp = stdio_transport("stdio-concurrent.log")
        activity = {
            "schemaVersion": 1,
            "runId": journal.run_id,
            "idempotencyKey": "baml:live-contract-1",
            "source": "baml",
            "kind": "provider.progress",
            "occurredAt": int(time.time() * 1_000),
            "expiresAt": int(time.time() * 1_000) + 60_000,
            "payload": {"phase": "running", "seat": "developer", "writer": "baml.provider"},
        }
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                lifespan="on",
                timeout_graceful_shutdown=3,
            )
        )
        server_thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [listener]},
            name="live-contract-uvicorn",
        )
        server_thread.start()
        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=5) as client:
                for _attempt in range(100):
                    try:
                        health = await client.get("/health")
                        if health.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("real product daemon did not accept TCP connections")

                snapshot = await client.get(
                    "/api/v1/hives/github%2Fbeadhive%2Flive-contract/snapshot",
                    headers=auth,
                )
                cursor = snapshot.json()["cursor"]
                events_path = "/api/v1/hives/github%2Fbeadhive%2Flive-contract/events"
                async with (
                    Client(stdio_mcp, timeout=20) as stdio_client,
                    Client(http_mcp, timeout=20) as http_client,
                    httpx.AsyncClient(
                        base_url=base_url,
                        timeout=httpx.Timeout(5, read=None),
                    ) as sse_client,
                ):
                    stdio_names = sorted(tool.name for tool in await stdio_client.list_tools())
                    http_names = sorted(tool.name for tool in await http_client.list_tools())
                    async with sse_client.stream(
                        "GET",
                        events_path,
                        headers=auth,
                        params={
                            "subscription": cursor["subscriptionId"],
                            "after": f"{cursor['producerEpoch']}:{cursor['sequence']}",
                        },
                    ) as stream:
                        assert stream.status_code == 200
                        sse_delivery = asyncio.create_task(
                            _read_sse_events(
                                stream,
                                idempotency_key=activity["idempotencyKey"],
                                durable_ack=durable_ack,
                            )
                        )
                        publication = asyncio.create_task(
                            client.post(
                                f"/api/v1/runs/{journal.run_id}/activity",
                                headers=auth,
                                json=activity,
                            )
                        )
                        assert await asyncio.to_thread(append_entered.wait, 5)
                        assert not durable_ack.is_set()
                        assert not publication.done()
                        assert not sse_delivery.done()
                        try:
                            (
                                mutation,
                                factory,
                                overlap_snapshot,
                                direct,
                                overlap_stdio,
                            ) = await asyncio.gather(
                                http_client.call_tool(
                                    "config_set",
                                    {
                                        "key": "otel.protocol",
                                        "value": "http/protobuf",
                                    },
                                ),
                                client.get("/api/v1/factory", headers=auth),
                                client.get(
                                    "/api/v1/hives/github%2Fbeadhive%2Flive-contract/snapshot",
                                    headers=auth,
                                ),
                                asyncio.to_thread(
                                    _run,
                                    "bh",
                                    "work",
                                    "list",
                                    "--json",
                                    cwd=hive,
                                ),
                                stdio_client.list_tools(),
                            )
                            assert not publication.done()
                            assert not sse_delivery.done()
                        finally:
                            allow_append.set()
                        published = await asyncio.wait_for(publication, timeout=5)
                        sse_events = await asyncio.wait_for(sse_delivery, timeout=5)
                        proof_order.append("sse-delivery")
                        assert durable_ack.is_set()
                    overlap_stdio_names = sorted(tool.name for tool in overlap_stdio)
                durable = await client.get(f"/api/v1/runs/{journal.run_id}/activity", headers=auth)
        finally:
            allow_append.set()
            server.should_exit = True
            await asyncio.wait_for(asyncio.to_thread(server_thread.join, 5), timeout=6)
            assert not server_thread.is_alive()
            listener.close()

        # The real TCP listener is now gone. Neither fresh direct CLI nor fresh stdio MCP
        # consults it, auto-starts it, or silently substitutes an embedded HTTP application.
        async with httpx.AsyncClient(base_url=base_url, timeout=1) as stopped_client:
            with pytest.raises(httpx.ConnectError):
                await stopped_client.get("/health")
        async with Client(stdio_transport("stdio-outage.log"), timeout=20) as mcp:
            outage_stdio_names = sorted(tool.name for tool in await mcp.list_tools())
        outage_direct = await asyncio.to_thread(_run, "bh", "work", "list", "--json", cwd=hive)
        async with httpx.AsyncClient(base_url=base_url, timeout=1) as stopped_again:
            with pytest.raises(httpx.ConnectError):
                await stopped_again.get("/health")

        assert factory.status_code == 200
        assert factory.json()["hives"][0]["hiveId"] == HIVE
        assert overlap_snapshot.status_code == 200
        assert overlap_snapshot.json()["cursor"]["producerEpoch"] == cursor["producerEpoch"]
        assert snapshot.status_code == 200
        assert any(item["record"]["id"] == bead_id for item in snapshot.json()["workItems"])
        assert published.status_code == 201
        assert published.json()["status"] == "created"
        assert mutation.data["ok"] is True
        assert mutation.data["new"] == "http/protobuf"
        assert config.get_value("otel.protocol") == {
            "ok": True,
            "value": "http/protobuf",
            "problems": [],
        }
        assert durable.status_code == 200
        durable_activity = durable.json()["activities"][-1]
        durable_id = durable_activity["payload"]["detail"]["activity"]["activityId"]
        assert durable_id == published.json()["activityId"]
        assert proof_order == ["durable-ack", "sse-delivery"]
        assert sse_events
        assert all(event[1]["producerEpoch"] == cursor["producerEpoch"] for event in sse_events)
        sequences = [event[1]["sequence"] for event in sse_events]
        assert sequences == list(range(cursor["sequence"] + 1, sequences[-1] + 1))
        sse_id, sse_event = sse_events[-1]
        assert sse_event["sequence"] > cursor["sequence"]
        assert sse_event["source"] == "runtime"
        assert sse_event["payload"]["kind"] == "activity"
        assert sse_event["payload"]["runId"] == journal.run_id
        sse_activity = sse_event["payload"]["activity"]
        assert sse_event["payload"] == {
            "kind": "activity",
            "runId": journal.run_id,
            "activity": sse_activity,
        }
        assert sse_activity["hiveId"] == HIVE
        assert sse_activity["runId"] == journal.run_id
        assert sse_activity["beadId"] == bead_id
        assert sse_activity["driver"] == "baml"
        assert sse_activity["provider"] == "codex"
        assert sse_activity["payload"]["name"] == activity["kind"]
        assert sse_activity["payload"]["detail"]["activity"] == {
            "kind": activity["kind"],
            "source": activity["source"],
            "seat": "developer",
            "activityId": durable_id,
            "idempotencyKey": activity["idempotencyKey"],
            "payload": activity["payload"],
        }
        assert sse_id == f"{sse_event['producerEpoch']}:{sse_event['sequence']}"
        assert http_names == stdio_names
        assert overlap_stdio_names == stdio_names
        assert any(item["id"] == bead_id for item in json.loads(direct.stdout))
        assert outage_stdio_names == stdio_names
        assert any(item["id"] == bead_id for item in json.loads(outage_direct.stdout))
        assert runtime.shutdown_started and not runtime.ready and not runtime.accepting
        shutdown = {(result.name, result.status) for result in runtime.shutdown_results}
        assert {
            ("credential-sessions", "completed"),
            ("fastmcp-http", "completed"),
            ("daemon-state-broker", "completed"),
            ("daemon-telemetry", "completed"),
        } <= shutdown
        assert app.state.mcp_sessions.active_session_count == 0
        assert app.state.mcp_sessions._stopping is True
        assert app.state.credential_sessions.active_session_count == 0
        assert app.state.credential_sessions._stopping is True
        assert not app.state.credential_sessions._callback_supervisors
        assert app.state.network_admission.active_mcp_session_count == 0
        manager = app.state.mcp_sessions._manager
        assert manager._task_group is None
        assert not manager._server_instances and not manager._session_owners
        broker = app.state.state_broker.retained_state()
        assert broker["brokerClosing"] is broker["brokerClosed"] is True
        assert broker["closing"] is True
        assert broker["clients"] == broker["events"] == broker["inFlightFeedCalls"] == 0
        assert broker["ownedFeedCalls"] == 0
        assert broker["hives"] == {}
        assert broker["registryReconciler"]["running"] is False
        assert app.state.operator_sse._closed is True
        assert not app.state.operator_sse._pumps
        telemetry = app.state.daemon_telemetry
        assert telemetry._started is telemetry._stopped is True
        assert not telemetry._active_requests and not telemetry._active_connections
        assert all(depth == 0 for depth in telemetry._queue_depths.values())
        assert server.started and server.should_exit and not server.force_exit
        assert not server_thread.is_alive() and listener.fileno() == -1

    try:
        asyncio.run(exercise())
    finally:
        app.state.operator_sources.close()


async def _read_sse_events(
    response: httpx.Response,
    *,
    idempotency_key: str,
    durable_ack: threading.Event,
) -> tuple[tuple[str, dict], ...]:
    events: list[tuple[str, dict]] = []
    event_id = ""
    data = ""
    async for line in response.aiter_lines():
        if line.startswith("id: "):
            event_id = line.removeprefix("id: ")
        elif line.startswith("data: "):
            data = line.removeprefix("data: ")
        elif not line and event_id and data:
            event = json.loads(data)
            detail = (
                event.get("payload", {})
                .get("activity", {})
                .get("payload", {})
                .get("detail", {})
                .get("activity", {})
            )
            events.append((event_id, event))
            if detail.get("idempotencyKey") == idempotency_key:
                assert durable_ack.is_set()
                return tuple(events)
            event_id = ""
            data = ""
    raise AssertionError("authenticated SSE closed before an operator event arrived")
