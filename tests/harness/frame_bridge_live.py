"""Child-owned real-process proof for the authenticated Frame Bridge path."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from joserfc import jwt
from joserfc.jwk import RSAKey
from joserfc.jws import JWSRegistry

from beadhive import daemon_auth, host
from beadhive.daemon_contract import AuthScope

DAEMON_ORIGIN = "http://127.0.0.1:8420"
BRIDGE_ORIGIN = "http://127.0.0.1:8787"
GATEWAY_HOST = "gateway-dev.beadhive.cloud"
APP_ORIGIN = "https://app-dev.beadhive.cloud"
ISSUER = "https://rapid-snail-6758.clerk.accounts.dev"
AUDIENCE = "beadhive-gateway-dev"
SUBJECT = "user_dev_demo"
HIVE_ID = "github/beadhive/beadhive"
TIMEOUT = 20.0


def _run(*argv: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=TIMEOUT,
    )


def _stop(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def _wait_for_http(url: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
    deadline = time.monotonic() + TIMEOUT
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, headers=headers, timeout=0.5, trust_env=False)
            if response.status_code < 500:
                return response
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
        time.sleep(0.05)
    raise RuntimeError("process did not expose its bounded health route") from last_error


def _wait_for_daemon_ready() -> httpx.Response:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        response = _wait_for_http(f"{DAEMON_ORIGIN}/health")
        if response.json().get("ready") is True:
            return response
        time.sleep(0.05)
    raise RuntimeError("host daemon did not become ready")


def _new_caller_credentials(credentials: Path) -> str:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key = RSAKey.import_key(private)
    public_key = RSAKey.import_key(private.public_key())
    jwk = public_key.as_dict()
    jwk.update({"kid": "development-test", "use": "sig", "alg": "RS256"})
    jwks = credentials / "clerk-jwks.json"
    subjects = credentials / "authorized-subjects.json"
    jwks.write_text(json.dumps({"keys": [jwk]}), encoding="utf-8")
    subjects.write_text(json.dumps([SUBJECT]), encoding="utf-8")
    jwks.chmod(0o600)
    subjects.chmod(0o600)
    token = jwt.encode(
        {"alg": "RS256", "kid": "development-test"},
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": SUBJECT,
            "exp": int(time.time()) + 300,
        },
        private_key,
        registry=JWSRegistry(algorithms=["RS256"], strict_check_header=False),
    )
    return token


def _seed_hive(workspace: Path, env: dict[str, str]) -> Path:
    hive = workspace / HIVE_ID
    hive.mkdir(parents=True)
    _run("git", "init", "-q", cwd=hive, env=env)
    _run("git", "config", "user.name", "Frame Bridge Test", cwd=hive, env=env)
    _run(
        "git",
        "config",
        "user.email",
        "frame-bridge-test@example.invalid",
        cwd=hive,
        env=env,
    )
    _run(
        "bd",
        "init",
        "--non-interactive",
        "--prefix",
        "bh",
        "--skip-agents",
        "--skip-hooks",
        cwd=hive,
        env=env,
    )
    _run("bd", "create", "Frame Bridge seed", "--type", "task", cwd=hive, env=env)
    return hive


def _write_daemon_config(home: Path, verifier: Path) -> None:
    config = {
        "schema_version": 1,
        "providers": ["github"],
        "managed_repos": [
            {
                "provider": "github",
                "org": "beadhive",
                "repo": "beadhive",
                "prefix": "bh",
                "kind": "org-native",
            }
        ],
        "exclude": {"orgs": [], "repos": []},
        "otel": {"enabled": False, "protocol": "grpc"},
        "host": {
            "daemon": {
                "enabled": True,
                "bind": "127.0.0.1",
                "port": 8420,
                "auth": {"credential_file": str(verifier)},
                "http": {"allowed_hosts": ["127.0.0.1"]},
                "shutdown": {
                    "graceful_seconds": 3,
                    "request_drain_seconds": 2,
                    "telemetry_flush_seconds": 1,
                },
            }
        },
    }
    (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")


def _start(
    executable: Path,
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
) -> tuple[subprocess.Popen[bytes], Any]:
    log = log_path.open("wb")
    process = subprocess.Popen(
        [str(executable)],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return process, log


def _read_one_sse_event(
    client: httpx.Client,
    *,
    cursor: str,
    headers: dict[str, str],
    mutate: Callable[[], object],
) -> str:
    event_id: str | None = None
    with client.stream(
        "GET",
        "/v1/instances/dev/demo/events",
        params={"cursor": cursor},
        headers=headers,
    ) as response:
        if response.status_code != 200:
            response.read()
            raise AssertionError(f"unexpected SSE status {response.status_code}: {response.text}")
        mutate()
        for line in response.iter_lines():
            if line.startswith("id: "):
                event_id = line.removeprefix("id: ")
                break
    if event_id is None:
        raise AssertionError("Frame Bridge SSE ended without an invalidation")
    # The client context has sent its disconnect, but Uvicorn's ASGI disconnect task releases
    # the per-subject stream slot asynchronously.  Model an ordinary client's reconnect backoff
    # so the next request still gets exactly one attempt: a 503 remains a hard failure below.
    time.sleep(0.2)
    return event_id


def _assert_port_released(port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()


def main() -> None:
    root = Path(sys.argv[1]).absolute()
    executable_dir = Path(sys.argv[2]).absolute()
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True, timeout=5)

    home = root / "home"
    workspace = root / "workspace"
    credentials = root / "credentials"
    logs = root / "logs"
    for path in (home, workspace, credentials, logs):
        path.mkdir(mode=0o700, parents=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("BEADS_")
        and key
        not in {
            "BEADHIVE_FRAME_BRIDGE_DAEMON_CREDENTIAL_FILE",
            "BEADHIVE_FRAME_BRIDGE_JWKS_FILE",
            "BEADHIVE_FRAME_BRIDGE_SUBJECTS_FILE",
            "CREDENTIALS_DIRECTORY",
        }
    }
    env.update(
        {
            "BH_HOME": str(home),
            "GIT_WORKSPACE": str(workspace),
            "OTEL_SDK_DISABLED": "true",
            "NO_COLOR": "1",
        }
    )
    os.environ["BH_HOME"] = str(home)
    host.mint_if_needed()
    hive = _seed_hive(workspace, env)
    verifier = (home / "daemon-credentials.json").absolute()
    provisioned = daemon_auth.provision_credential_file(
        verifier,
        credential_id="frame-bridge",
        audience="beadhive-host",
        principal="service:frame-bridge",
        scopes=(AuthScope.OPERATOR_READ,),
        expires_at=int(time.time()) + 3_600,
    )
    bearer = provisioned.bearer.reveal_for_authority()
    bearer_file = (credentials / "daemon-bearer").absolute()
    bearer_file.write_text(f"{bearer}\n", encoding="ascii")
    bearer_file.chmod(0o600)
    _write_daemon_config(home, verifier)
    caller_token = _new_caller_credentials(credentials)

    daemon: subprocess.Popen[bytes] | None = None
    bridge: subprocess.Popen[bytes] | None = None
    handles: list[Any] = []
    child_pids: list[int] = []
    observed_payloads: list[str] = []
    coverage: set[str] = set()
    try:
        daemon, daemon_log = _start(
            executable_dir / "bh-host-daemon",
            cwd=hive,
            env=env,
            log_path=logs / "daemon.log",
        )
        child_pids.append(daemon.pid)
        handles.append(daemon_log)
        health = _wait_for_daemon_ready()
        assert health.status_code == 200

        bridge_env = {
            **env,
            "CREDENTIALS_DIRECTORY": str(credentials),
            "BEADHIVE_FRAME_BRIDGE_SOURCE_MODE": "live",
        }
        bridge, bridge_log = _start(
            executable_dir / "beadhive-frame-bridge",
            cwd=hive,
            env=bridge_env,
            log_path=logs / "bridge.log",
        )
        child_pids.append(bridge.pid)
        handles.append(bridge_log)
        bridge_health = _wait_for_http(f"{BRIDGE_ORIGIN}/healthz", headers={"Host": GATEWAY_HOST})
        assert bridge_health.json() == {"live": True, "contractVersion": "gateway.v1"}
        observed_payloads.append(bridge_health.text)
        coverage.add("health")

        caller_headers = {
            "Authorization": f"Bearer {caller_token}",
            "Origin": APP_ORIGIN,
            "Host": GATEWAY_HOST,
        }
        with httpx.Client(base_url=BRIDGE_ORIGIN, timeout=TIMEOUT, trust_env=False) as client:
            discovery = client.get("/v1/instances", params={"limit": "50"}, headers=caller_headers)
            assert discovery.status_code == 200, discovery.text
            assert discovery.json()["items"][0]["availability"] == "online"
            coverage.add("authenticated discovery")
            snapshot = client.get("/v1/instances/dev/demo/snapshot", headers=caller_headers)
            assert snapshot.status_code == 200, snapshot.text
            snapshot_body = snapshot.json()
            revision = snapshot_body["snapshot"]["revision"]
            cursor = snapshot_body["snapshot"]["eventCursor"]
            assert snapshot_body["contractVersion"] == "gateway.v1"
            assert snapshot_body["snapshot"]["workItems"]
            refresh = client.post(
                "/v1/instances/dev/demo/commands/refresh",
                headers={**caller_headers, "Content-Type": "application/json"},
                json={
                    "schemaVersion": 1,
                    "correlationId": "123e4567-e89b-42d3-a456-426614174000",
                    "expectedRevision": revision,
                },
            )
            assert refresh.status_code == 200, refresh.text
            assert refresh.json()["result"] == {"status": "completed", "revision": revision}
            stale_refresh = client.post(
                "/v1/instances/dev/demo/commands/refresh",
                headers={**caller_headers, "Content-Type": "application/json"},
                json={
                    "schemaVersion": 1,
                    "correlationId": "223e4567-e89b-42d3-a456-426614174000",
                    "expectedRevision": "sha256:" + "f" * 64,
                },
            )
            assert stale_refresh.status_code == 409
            assert stale_refresh.json()["error"]["code"] == "scope_conflict"
            coverage.add("revision-checked refresh")
            first_cursor = _read_one_sse_event(
                client,
                cursor=cursor,
                headers=caller_headers,
                mutate=lambda: _run(
                    "bd", "create", "Frame Bridge event one", "--type", "task", cwd=hive, env=env
                ),
            )
            second_cursor = _read_one_sse_event(
                client,
                cursor=first_cursor,
                headers=caller_headers,
                mutate=lambda: _run(
                    "bd", "create", "Frame Bridge event two", "--type", "task", cwd=hive, env=env
                ),
            )
            assert second_cursor.rsplit(":", 1)[0] == first_cursor.rsplit(":", 1)[0]
            assert int(second_cursor.rsplit(":", 1)[1]) == int(first_cursor.rsplit(":", 1)[1]) + 1
            coverage.add("SSE delivery and reconnect")
            observed_payloads.extend(
                [
                    discovery.text,
                    snapshot.text,
                    refresh.text,
                    stale_refresh.text,
                    first_cursor,
                    second_cursor,
                ]
            )

        for process in (daemon, bridge):
            assert process is not None
            assert bearer.encode() not in Path(f"/proc/{process.pid}/cmdline").read_bytes()
            assert bearer.encode() not in Path(f"/proc/{process.pid}/environ").read_bytes()

        _stop(bridge)
        bridge = None
        bearer_file.write_text("bh1.wrong." + "w" * 43, encoding="ascii")
        bearer_file.chmod(0o600)
        wrong, wrong_log = _start(
            executable_dir / "beadhive-frame-bridge",
            cwd=hive,
            env=bridge_env,
            log_path=logs / "bridge-wrong.log",
        )
        bridge = wrong
        child_pids.append(wrong.pid)
        handles.append(wrong_log)
        _wait_for_http(f"{BRIDGE_ORIGIN}/healthz", headers={"Host": GATEWAY_HOST})
        wrong_snapshot = httpx.get(
            f"{BRIDGE_ORIGIN}/v1/instances/dev/demo/snapshot",
            headers=caller_headers,
            timeout=TIMEOUT,
            trust_env=False,
        )
        assert wrong_snapshot.status_code == 503
        assert wrong_snapshot.json()["error"]["code"] == "runtime_unavailable"
        observed_payloads.append(wrong_snapshot.text)
        _stop(bridge)
        bridge = None

        bearer_file.write_text("malformed", encoding="ascii")
        bearer_file.chmod(0o600)
        malformed, malformed_log = _start(
            executable_dir / "beadhive-frame-bridge",
            cwd=hive,
            env=bridge_env,
            log_path=logs / "bridge-malformed.log",
        )
        bridge = malformed
        child_pids.append(malformed.pid)
        handles.append(malformed_log)
        assert malformed.wait(timeout=8) != 0
        bridge = None
        bearer_file.unlink()
        missing, missing_log = _start(
            executable_dir / "beadhive-frame-bridge",
            cwd=hive,
            env=bridge_env,
            log_path=logs / "bridge-missing.log",
        )
        bridge = missing
        child_pids.append(missing.pid)
        handles.append(missing_log)
        assert missing.wait(timeout=8) != 0
        bridge = None
        coverage.add("credential failure")
    finally:
        _stop(bridge)
        _stop(daemon)
        for handle in handles:
            handle.close()

    assert bearer not in snapshot.text
    coverage.add("redacted snapshot")
    assert bearer not in "".join(observed_payloads)
    for log_path in logs.iterdir():
        assert bearer.encode() not in log_path.read_bytes()
    for path in root.rglob("*"):
        if path.is_file() and path != bearer_file:
            assert bearer.encode() not in path.read_bytes()
    coverage.add("secret redaction")
    assert not [path for path in root.rglob("*.sock")]
    assert not [pid for pid in child_pids if Path(f"/proc/{pid}").exists()]
    _assert_port_released(8420)
    _assert_port_released(8787)
    shutil.rmtree(root)
    assert not root.exists()
    coverage.add("process/socket/credential/temp-state cleanup")
    print(
        json.dumps(
            {
                "status": "ok",
                "contractVersion": "gateway.v1",
                "coverage": sorted(coverage),
            }
        )
    )


if __name__ == "__main__":
    main()
