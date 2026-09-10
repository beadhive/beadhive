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
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, TypeVar

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport

from beadhive import config, daemon_auth, daemon_state_broker, host_daemon, run_journal
from beadhive.daemon_config import HostDaemonConfig
from beadhive.daemon_contract import AuthScope
from harness.processes import process_context

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
LIVE_HARNESS_DEADLINE_SECONDS = 20.0
LIVE_SCENARIO_HEARTBEAT_SECONDS = 2.0
LIVE_SCENARIO_LIVENESS_SECONDS = 30.0
LIVE_SCENARIO_WATCHDOG_SECONDS = 90.0
assert LIVE_SCENARIO_LIVENESS_SECONDS < LIVE_SCENARIO_WATCHDOG_SECONDS
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

ResultT = TypeVar("ResultT")


async def _wait_for_rendezvous(event: threading.Event, *, label: str) -> None:
    """Poll an in-process rendezvous without occupying the scenario event loop."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + LIVE_HARNESS_DEADLINE_SECONDS
    while not event.is_set():
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise AssertionError(f"timed out waiting for live-contract rendezvous: {label}")
        await asyncio.sleep(min(0.02, remaining))


async def _await_harness(
    awaitable: Awaitable[ResultT],
    *,
    label: str,
    timeout_seconds: float = LIVE_HARNESS_DEADLINE_SECONDS,
) -> ResultT:
    """Apply one finite contention-tolerant deadline to an async harness operation."""

    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except TimeoutError as exc:
        raise AssertionError(f"live-contract harness deadline expired: {label}") from exc


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


def _live_server_cleanup_report(
    *,
    runtime: host_daemon.DaemonRuntime,
    app: Any,
    settings: HostDaemonConfig,
    server: uvicorn.Server,
    listener: socket.socket,
    proof_order: list[str],
) -> dict[str, Any]:
    """Return only serializable shutdown evidence to the parent pytest worker."""

    manager = app.state.mcp_sessions._manager
    broker = app.state.state_broker.retained_state()
    telemetry = app.state.daemon_telemetry
    return {
        "proof_order": proof_order,
        "runtime": {
            "shutdown_started": runtime.shutdown_started,
            "ready": runtime.ready,
            "accepting": runtime.accepting,
            "shutdown_budget": runtime.shutdown_budget,
            "shutdown_results": sorted(
                (result.name, result.status) for result in runtime.shutdown_results
            ),
        },
        "settings": {
            "graceful_seconds": settings.shutdown.graceful_seconds,
            "request_drain_seconds": settings.shutdown.request_drain_seconds,
            "telemetry_flush_seconds": settings.shutdown.telemetry_flush_seconds,
        },
        "mcp_sessions": {
            "active": app.state.mcp_sessions.active_session_count,
            "stopping": app.state.mcp_sessions._stopping,
            "task_group_none": manager._task_group is None,
            "server_instances": len(manager._server_instances),
            "session_owners": len(manager._session_owners),
        },
        "credential_sessions": {
            "active": app.state.credential_sessions.active_session_count,
            "stopping": app.state.credential_sessions._stopping,
            "callback_supervisors": len(app.state.credential_sessions._callback_supervisors),
        },
        "network_admission": {
            "active_mcp_sessions": app.state.network_admission.active_mcp_session_count,
        },
        "broker": {
            "brokerClosing": broker["brokerClosing"],
            "brokerClosed": broker["brokerClosed"],
            "closing": broker["closing"],
            "clients": broker["clients"],
            "events": broker["events"],
            "inFlightFeedCalls": broker["inFlightFeedCalls"],
            "ownedFeedCalls": broker["ownedFeedCalls"],
            "hives": broker["hives"],
            "registryReconcilerRunning": broker["registryReconciler"]["running"],
        },
        "operator_sse": {
            "closed": app.state.operator_sse._closed,
            "pumps": len(app.state.operator_sse._pumps),
        },
        "telemetry": {
            "started": telemetry._started,
            "stopped": telemetry._stopped,
            "active_requests": len(telemetry._active_requests),
            "active_connections": len(telemetry._active_connections),
            "queue_depths": tuple(telemetry._queue_depths.values()),
        },
        "server": {
            "started": server.started,
            "should_exit": server.should_exit,
            "force_exit": server.force_exit,
            "listener_closed": listener.fileno() == -1,
        },
        "non_main_threads": sorted(
            thread.name
            for thread in threading.enumerate()
            if thread is not threading.main_thread() and thread.is_alive()
        ),
    }


def _set_scenario_phase(diagnostics: dict[str, Any], phase: str) -> None:
    diagnostics["phase"] = phase
    diagnostics.setdefault("phase_history", []).append(phase)


def _task_states(tasks: dict[str, asyncio.Task[Any]]) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for name, task in tasks.items():
        state: dict[str, Any] = {
            "done": task.done(),
            "cancelled": task.cancelled(),
        }
        if task.done() and not task.cancelled():
            exception = task.exception()
            state["exception"] = None if exception is None else repr(exception)
        states[name] = state
    return states


def _diagnostic_snapshot(
    diagnostics: dict[str, Any], tasks: dict[str, asyncio.Task[Any]] | None = None
) -> dict[str, Any]:
    snapshot = dict(diagnostics)
    if tasks is not None:
        snapshot["task_states"] = _task_states(tasks)
    return snapshot


def _publish_child_diagnostics(
    control: Any,
    diagnostics: dict[str, Any],
    *,
    kind: str,
    tasks: dict[str, asyncio.Task[Any]] | None = None,
) -> None:
    try:
        control.send(
            {
                "kind": kind,
                "diagnostics": _diagnostic_snapshot(diagnostics, tasks),
            }
        )
    except (BrokenPipeError, EOFError, OSError):
        # The parent owns the watchdog and may already be reaping this process.
        pass


async def _publish_live_scenario_heartbeats(
    control: Any,
    diagnostics: dict[str, Any],
    tasks: dict[str, asyncio.Task[Any]],
) -> None:
    while True:
        await asyncio.sleep(LIVE_SCENARIO_HEARTBEAT_SECONDS)
        _publish_child_diagnostics(
            control,
            diagnostics,
            kind="heartbeat",
            tasks=tasks,
        )


def _live_contract_scenario_process(tmp_path_text: str, control: Any) -> None:
    """Own the complete async live scenario and publish one terminal envelope."""

    import traceback

    diagnostics: dict[str, Any] = {
        "phase": "child-bootstrap",
        "phase_history": ["child-bootstrap"],
        "task_states": {},
    }
    _publish_child_diagnostics(control, diagnostics, kind="progress")
    try:
        state = asyncio.run(
            _run_live_contract_scenario(
                Path(tmp_path_text),
                diagnostics,
                control=control,
            )
        )
        report = _live_server_cleanup_report(**state)
        _assert_live_server_cleanup(report)
        control.send(
            {
                "kind": "passed",
                "diagnostics": _diagnostic_snapshot(diagnostics),
                "report": report,
            }
        )
    except BaseException:
        diagnostics.setdefault("failure_phase", diagnostics["phase"])
        try:
            control.send(
                {
                    "kind": "failed",
                    "diagnostics": _diagnostic_snapshot(diagnostics),
                    "traceback": traceback.format_exc(),
                }
            )
        finally:
            control.close()
        raise SystemExit(1) from None
    else:
        control.close()


def _wait_for_live_scenario_process(
    process: Any,
    *,
    control: Any,
    watchdog_seconds: float = LIVE_SCENARIO_WATCHDOG_SECONDS,
    liveness_seconds: float = LIVE_SCENARIO_LIVENESS_SECONDS,
    monotonic: Callable[[], float] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Drain child progress until one terminal result, then reap with bounded fallbacks."""

    forced = False
    clock = monotonic or time.monotonic
    started_at = clock()
    absolute_deadline = started_at + watchdog_seconds
    liveness_deadline = started_at + liveness_seconds
    latest_diagnostics: dict[str, Any] = {
        "phase": "child-bootstrap",
        "phase_history": ["child-bootstrap"],
        "task_states": {},
    }
    message = None
    expiry_kind = None
    try:
        while message is None:
            now = clock()
            if now >= absolute_deadline:
                expiry_kind = "absolute"
                break
            if now >= liveness_deadline:
                expiry_kind = "liveness"
                break
            poll_seconds = min(1.0, absolute_deadline - now, liveness_deadline - now)
            if not control.poll(poll_seconds):
                continue
            candidate = control.recv()
            if candidate.get("kind") in {"progress", "heartbeat"}:
                latest_diagnostics = dict(candidate["diagnostics"])
                liveness_deadline = clock() + liveness_seconds
                continue
            message = candidate
    except (EOFError, OSError) as exc:
        message = {
            "kind": "failed",
            "diagnostics": {
                "phase": "parent-terminal-receive",
                "phase_history": ["parent-terminal-receive"],
                "task_states": {},
            },
            "traceback": f"live-contract child terminal pipe failed: {exc!r}",
        }
    if message is None:
        latest_diagnostics["parent_watchdog"] = {
            "kind": expiry_kind,
            "seconds": watchdog_seconds if expiry_kind == "absolute" else liveness_seconds,
        }
        message = {
            "kind": "failed",
            "diagnostics": latest_diagnostics,
            "traceback": (
                "live-contract child did not publish one terminal result before the "
                f"{expiry_kind} watchdog expired"
            ),
        }
    process.join(5)
    if process.is_alive():
        forced = True
        process.terminate()
        process.join(5)
    if process.is_alive():
        process.kill()
        process.join(5)
    return message, forced


class _ControlledClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class _ScriptedControl:
    def __init__(self, clock: _ControlledClock, messages: list[dict[str, Any]]) -> None:
        self.clock = clock
        self.messages = list(messages)

    def poll(self, timeout: float) -> bool:
        self.clock.value += min(1.0, timeout)
        return bool(self.messages)

    def recv(self) -> dict[str, Any]:
        return self.messages.pop(0)


class _ExitedProcess:
    def join(self, _timeout: float) -> None:
        pass

    def is_alive(self) -> bool:
        return False

    def terminate(self) -> None:
        raise AssertionError("an exited diagnostic probe must not be terminated")

    def kill(self) -> None:
        raise AssertionError("an exited diagnostic probe must not be killed")


def test_parent_watchdog_preserves_latest_child_progress_on_liveness_expiry() -> None:
    clock = _ControlledClock()
    control = _ScriptedControl(
        clock,
        [
            {
                "kind": "progress",
                "diagnostics": {
                    "phase": "activity-stream",
                    "phase_history": ["child-bootstrap", "activity-stream"],
                    "task_states": {},
                },
            },
            {
                "kind": "heartbeat",
                "diagnostics": {
                    "phase": "overlap-operations",
                    "phase_history": [
                        "child-bootstrap",
                        "activity-stream",
                        "overlap-operations",
                    ],
                    "task_states": {
                        "overlap-direct-cli": {"done": False, "cancelled": False},
                    },
                },
            },
        ],
    )

    terminal, forced = _wait_for_live_scenario_process(
        _ExitedProcess(),
        control=control,
        watchdog_seconds=10.0,
        liveness_seconds=2.0,
        monotonic=clock,
    )

    assert forced is False
    assert terminal["kind"] == "failed"
    assert terminal["diagnostics"]["phase"] == "overlap-operations"
    assert terminal["diagnostics"]["task_states"] == {
        "overlap-direct-cli": {"done": False, "cancelled": False}
    }
    assert terminal["diagnostics"]["parent_watchdog"] == {
        "kind": "liveness",
        "seconds": 2.0,
    }


def test_parent_watchdog_keeps_absolute_deadline_despite_fresh_heartbeats() -> None:
    clock = _ControlledClock()
    heartbeat = {
        "kind": "heartbeat",
        "diagnostics": {
            "phase": "server-shutdown",
            "phase_history": ["child-bootstrap", "server-shutdown"],
            "task_states": {"uvicorn": {"done": False, "cancelled": False}},
        },
    }
    control = _ScriptedControl(clock, [heartbeat] * 10)

    terminal, forced = _wait_for_live_scenario_process(
        _ExitedProcess(),
        control=control,
        watchdog_seconds=3.0,
        liveness_seconds=10.0,
        monotonic=clock,
    )

    assert forced is False
    assert terminal["kind"] == "failed"
    assert terminal["diagnostics"]["phase"] == "server-shutdown"
    assert terminal["diagnostics"]["parent_watchdog"] == {
        "kind": "absolute",
        "seconds": 3.0,
    }


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


async def _run_live_contract_scenario(
    tmp_path: Path,
    diagnostics: dict[str, Any],
    *,
    control: Any,
) -> dict[str, Any]:
    tasks: dict[str, asyncio.Task[Any]] = {}
    heartbeat = asyncio.create_task(
        _publish_live_scenario_heartbeats(control, diagnostics, tasks),
        name="live-contract-diagnostics-heartbeat",
    )
    try:
        return await _run_live_contract_scenario_body(
            tmp_path,
            diagnostics,
            control=control,
            tasks=tasks,
        )
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat


async def _run_live_contract_scenario_body(
    tmp_path: Path,
    diagnostics: dict[str, Any],
    *,
    control: Any,
    tasks: dict[str, asyncio.Task[Any]],
) -> dict[str, Any]:
    """Run server, clients, overlap proof, outage proof, and shutdown in one event loop."""

    def set_phase(phase: str) -> None:
        _set_scenario_phase(diagnostics, phase)
        _publish_child_diagnostics(
            control,
            diagnostics,
            kind="progress",
            tasks=tasks,
        )

    set_phase("scenario-setup")
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    os.environ["BH_HOME"] = str(home)
    os.environ["GIT_WORKSPACE"] = str(workspace)
    os.environ["OTEL_SDK_DISABLED"] = "true"
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
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
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
        control_record=_control_record(home, port),
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
        if not allow_append.wait(timeout=LIVE_HARNESS_DEADLINE_SECONDS):
            raise AssertionError("live proof did not release the durable append barrier")
        result = actual_append(*args, **kwargs)
        proof_order.append("durable-ack")
        durable_ack.set()
        return result

    app.state.activity_store.append = gated_append
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

    async def exercise_live() -> tuple[str, ...]:
        set_phase("live-clients")
        auth = {"Authorization": f"Bearer {bearer}"}
        base_url = f"http://127.0.0.1:{port}"
        http_mcp = StreamableHttpTransport(
            f"{base_url}/mcp",
            headers=auth,
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
        set_phase("transport-connect")
        async with httpx.AsyncClient(base_url=base_url, timeout=5) as client:
            health = await client.get("/health")
            assert health.status_code == 200
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
                set_phase("activity-stream")
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
                    tasks["sse-delivery"] = asyncio.create_task(
                        _read_sse_events(
                            stream,
                            idempotency_key=activity["idempotencyKey"],
                            durable_ack=durable_ack,
                        ),
                        name="live-contract-sse-delivery",
                    )
                    tasks["activity-publication"] = asyncio.create_task(
                        client.post(
                            f"/api/v1/runs/{journal.run_id}/activity",
                            headers=auth,
                            json=activity,
                            # The append is deliberately held below. The finite test-harness
                            # deadline owns this wait instead of httpx's unrelated 5s default.
                            timeout=None,
                        ),
                        name="live-contract-activity-publication",
                    )
                    publication = tasks["activity-publication"]
                    sse_delivery = tasks["sse-delivery"]
                    await _wait_for_rendezvous(append_entered, label="durable append entered")
                    assert not durable_ack.is_set()
                    assert not publication.done()
                    assert not sse_delivery.done()
                    set_phase("overlap-operations")
                    overlap_tasks = {
                        "overlap-config-mutation": asyncio.create_task(
                            http_client.call_tool(
                                "config_set",
                                {
                                    "key": "otel.protocol",
                                    "value": "http/protobuf",
                                },
                            ),
                            name="live-contract-config-mutation",
                        ),
                        "overlap-factory": asyncio.create_task(
                            client.get("/api/v1/factory", headers=auth),
                            name="live-contract-factory",
                        ),
                        "overlap-snapshot": asyncio.create_task(
                            client.get(
                                "/api/v1/hives/github%2Fbeadhive%2Flive-contract/snapshot",
                                headers=auth,
                            ),
                            name="live-contract-overlap-snapshot",
                        ),
                        "overlap-direct-cli": asyncio.create_task(
                            asyncio.to_thread(
                                _run,
                                "bh",
                                "work",
                                "list",
                                "--json",
                                cwd=hive,
                            ),
                            name="live-contract-direct-cli",
                        ),
                        "overlap-stdio": asyncio.create_task(
                            stdio_client.list_tools(),
                            name="live-contract-overlap-stdio",
                        ),
                    }
                    tasks.update(overlap_tasks)
                    try:
                        (
                            mutation,
                            factory,
                            overlap_snapshot,
                            direct,
                            overlap_stdio,
                        ) = await _await_harness(
                            asyncio.gather(*overlap_tasks.values()),
                            label="concurrent overlap operations",
                        )
                        assert not publication.done()
                        assert not sse_delivery.done()
                    finally:
                        set_phase("activity-release")
                        allow_append.set()
                    published = await _await_harness(
                        publication,
                        label="activity publication after append release",
                    )
                    sse_events = await _await_harness(
                        sse_delivery,
                        label="SSE delivery after durable acknowledgement",
                    )
                    proof_order.append("sse-delivery")
                    assert durable_ack.is_set()
                overlap_stdio_names = sorted(tool.name for tool in overlap_stdio)
            set_phase("durable-read")
            durable = await client.get(f"/api/v1/runs/{journal.run_id}/activity", headers=auth)

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
        return tuple(stdio_names)

    async def exercise_outage(stdio_names: tuple[str, ...]) -> None:
        set_phase("daemon-outage")
        base_url = f"http://127.0.0.1:{port}"
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
        assert outage_stdio_names == list(stdio_names)
        assert any(item["id"] == bead_id for item in json.loads(outage_direct.stdout))

    primary_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    stdio_names: tuple[str, ...] = ()
    tasks["uvicorn"] = asyncio.create_task(
        server.serve(sockets=[listener]), name="live-contract-uvicorn"
    )
    server_task = tasks["uvicorn"]
    try:
        set_phase("server-startup")
        loop = asyncio.get_running_loop()
        startup_deadline = loop.time() + LIVE_HARNESS_DEADLINE_SECONDS
        while not server.started:
            if server_task.done():
                await server_task
                raise AssertionError("real product daemon stopped before becoming ready")
            if loop.time() >= startup_deadline:
                raise AssertionError("real product daemon did not become ready")
            await asyncio.sleep(0.02)
        stdio_names = await exercise_live()
    except BaseException as exc:
        primary_error = exc
        diagnostics["failure_phase"] = diagnostics["phase"]
        diagnostics["task_states_at_failure"] = _task_states(tasks)
    finally:
        allow_append.set()
        set_phase("server-shutdown")
        server.should_exit = True
        try:
            await _await_harness(
                asyncio.shield(server_task),
                label="uvicorn graceful shutdown",
            )
        except BaseException as exc:
            cleanup_error = exc
            diagnostics["cleanup_error"] = repr(exc)
        try:
            app.state.operator_sources.close()
            listener.close()
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
            diagnostics["resource_close_error"] = repr(exc)
        diagnostics["task_states"] = _task_states(tasks)

    # Never let cleanup/exit diagnostics mask the actionable scenario failure.
    if primary_error is not None:
        raise primary_error.with_traceback(primary_error.__traceback__)
    if cleanup_error is not None:
        raise cleanup_error.with_traceback(cleanup_error.__traceback__)

    await exercise_outage(stdio_names)
    set_phase("scenario-complete")
    diagnostics["task_states"] = _task_states(tasks)
    return {
        "runtime": runtime,
        "app": app,
        "settings": settings,
        "server": server,
        "listener": listener,
        "proof_order": proof_order,
    }


def _assert_live_server_cleanup(cleanup: dict[str, Any]) -> None:
    """Assert every live product resource is quiescent before child success."""

    assert cleanup["proof_order"] == ["durable-ack", "sse-delivery"]
    assert cleanup["runtime"]["shutdown_started"] is True
    assert cleanup["runtime"]["ready"] is False
    assert cleanup["runtime"]["accepting"] is False
    assert cleanup["runtime"]["shutdown_budget"] == 3
    shutdown = {tuple(result) for result in cleanup["runtime"]["shutdown_results"]}
    # Product budgets stay strict and independent of the more generous test-harness deadline.
    assert cleanup["settings"] == {
        "graceful_seconds": 3,
        "request_drain_seconds": 2,
        "telemetry_flush_seconds": 1,
    }
    assert cleanup["mcp_sessions"] == {
        "active": 0,
        "stopping": True,
        "task_group_none": True,
        "server_instances": 0,
        "session_owners": 0,
    }
    assert cleanup["credential_sessions"] == {
        "active": 0,
        "stopping": True,
        "callback_supervisors": 0,
    }
    assert cleanup["network_admission"] == {"active_mcp_sessions": 0}
    assert cleanup["broker"] == {
        "brokerClosing": True,
        "brokerClosed": True,
        "closing": True,
        "clients": 0,
        "events": 0,
        "inFlightFeedCalls": 0,
        "ownedFeedCalls": 0,
        "hives": {},
        "registryReconcilerRunning": False,
    }
    assert cleanup["operator_sse"] == {"closed": True, "pumps": 0}
    assert cleanup["telemetry"]["started"] is True
    assert cleanup["telemetry"]["stopped"] is True
    assert cleanup["telemetry"]["active_requests"] == 0
    assert cleanup["telemetry"]["active_connections"] == 0
    assert all(depth == 0 for depth in cleanup["telemetry"]["queue_depths"])
    assert cleanup["server"] == {
        "started": True,
        "should_exit": True,
        "force_exit": False,
        "listener_closed": True,
    }
    assert cleanup["non_main_threads"] == []
    assert shutdown == {
        ("credential-sessions", "completed"),
        ("fastmcp-http", "completed"),
        ("daemon-state-broker", "completed"),
        ("daemon-telemetry", "completed"),
    }


def test_real_product_app_composes_concurrent_cli_stdio_http_operator_and_activity(
    tmp_path: Path,
) -> None:
    ctx = process_context()
    parent_control, child_control = ctx.Pipe(duplex=False)
    scenario_process = ctx.Process(
        target=_live_contract_scenario_process,
        args=(str(tmp_path), child_control),
        name="live-contract-scenario",
    )
    scenario_process.start()
    child_control.close()
    try:
        terminal, forced_cleanup = _wait_for_live_scenario_process(
            scenario_process,
            control=parent_control,
        )
    finally:
        parent_control.close()

    # Surface the scenario's primary phase/task traceback before process cleanup assertions.
    if terminal["kind"] != "passed":
        pytest.fail(
            "live-contract child failed\n"
            f"diagnostics={json.dumps(terminal['diagnostics'], sort_keys=True)}\n"
            f"{terminal['traceback']}",
            pytrace=False,
        )
    assert not forced_cleanup
    assert not scenario_process.is_alive()
    assert scenario_process.exitcode == 0
    _assert_live_server_cleanup(terminal["report"])


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
