from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

from beadhive.integrations.herdr import (
    Coverage,
    FailureCode,
    HerdrClient,
    IdentityExpectation,
    parse_roster,
    parse_snapshot,
    resolve_session,
    validate_identity,
)
from beadhive.integrations.herdr.transport import decode_protocol


def test_protocol_decoder_distinguishes_malformed_and_refused() -> None:
    malformed = decode_protocol(b"{not-json")
    refused = decode_protocol({"id": "1", "error": {"code": "conflict", "message": "busy"}})

    assert malformed.failure is not None
    assert malformed.failure.code == FailureCode.MALFORMED
    assert refused.failure is not None
    assert refused.failure.code == FailureCode.CONFLICT


def test_session_selection_is_explicit_and_never_follows_focus() -> None:
    assert resolve_session(environment={}).name == "default"
    assert resolve_session(environment={"BH_HERDR_SESSION": "team"}).name == "team"
    assert resolve_session("named", environment={"BH_HERDR_SESSION": "other"}).name == "named"

    try:
        resolve_session("current", environment={})
    except ValueError as exc:
        assert "HERDR_ENV" in str(exc)
    else:
        raise AssertionError("current must require Herdr's injected environment")


def test_snapshot_parser_retains_partial_topology_without_inventing_collections() -> None:
    result = parse_snapshot(
        {
            "session": "default",
            "revision": "r1",
            "spaces": [{"id": "space-1"}],
            "tabs": [{"id": "tab-1"}],
            "agents": [],
        }
    )

    snapshot = result.unwrap()
    assert snapshot.coverage == Coverage.PARTIAL
    assert snapshot.panes == ()

    malformed = parse_snapshot({"session": "default", "revision": "r1", "spaces": "not-a-list"})
    assert malformed.failure is not None
    assert malformed.failure.code == FailureCode.MALFORMED


def test_roster_parser_handles_nested_provider_records() -> None:
    result = parse_roster(
        {
            "agents": [
                {
                    "agent": {"name": "worker-1", "state": "idle", "agent_session": "s1"},
                    "pane_id": "p1",
                    "workspace_id": "w1",
                }
            ]
        }
    )

    record = result.unwrap()[0]
    assert record.target == "worker-1"
    assert record.pane_id == "p1"
    assert record.workspace_id == "w1"


def test_identity_requires_exact_target_and_tokens() -> None:
    expected = IdentityExpectation(
        target="worker-1",
        pane_id="p1",
        workspace_id="w1",
        session="default",
        cwd="/checkout",
        tokens={"bh_generation": "2"},
    )
    record = {
        "name": "worker-1",
        "state": "idle",
        "pane_id": "p1",
        "workspace_id": "w1",
        "session": "default",
        "cwd": "/checkout",
        "tokens": {"bh_owner": "bh.plugin.herdr/v1", "bh_generation": "2"},
    }
    assert validate_identity(record, expected).is_ok
    assert (
        validate_identity({**record, "name": "worker-10"}, expected).failure.code
        == FailureCode.CONFLICT
    )
    assert validate_identity({**record, "tokens": {}}, expected).failure.code == FailureCode.REFUSED


def test_command_client_maps_timeout_and_restart_to_typed_results() -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    timed = HerdrClient(runner=timeout).command("status", timeout=0.01)
    assert timed.failure is not None
    assert timed.failure.code == FailureCode.RETRYABLE
    assert timed.failure.retryable

    def restarted(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, "", "server unavailable after restart")

    lost = HerdrClient(runner=restarted).command("api", "snapshot")
    assert lost.failure is not None
    assert lost.failure.code == FailureCode.UNAVAILABLE


class _Socket:
    def __init__(self, response: bytes = b"") -> None:
        self.response = response
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def settimeout(self, value):
        self.timeout = value

    def connect(self, path):
        self.path = path

    def sendall(self, payload):
        self.sent += payload

    def recv(self, size):
        value, self.response = self.response, b""
        return value


class _MakefileSocket(_Socket):
    def __init__(self, stream):
        super().__init__()
        self.stream = stream(self)

    def makefile(self, _mode):
        return self.stream


class _DelayedStream:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def readline(self, _limit):
        time.sleep(1.05)
        request_id = json.loads(self.owner.sent)["id"]
        return (json.dumps({"id": request_id, "result": {"ok": True}}) + "\n").encode()


class _TimeoutStream(_DelayedStream):
    def readline(self, _limit):
        raise TimeoutError


def test_socket_client_honors_configured_deadline_after_quiet_period() -> None:
    socket_obj = _MakefileSocket(_DelayedStream)
    started = time.monotonic()
    result = HerdrClient(socket_factory=lambda *args: socket_obj).socket_request(
        Path("/tmp/herdr.sock"), "status", timeout=2.0
    )

    assert result.is_ok
    assert time.monotonic() - started >= 1.0
    assert socket_obj.timeout == 2.0


def test_socket_client_reports_true_makefile_timeout() -> None:
    socket_obj = _MakefileSocket(_TimeoutStream)
    result = HerdrClient(socket_factory=lambda *args: socket_obj).socket_request(
        Path("/tmp/herdr.sock"), "status", timeout=0.01
    )

    assert result.failure is not None
    assert result.failure.code == FailureCode.RETRYABLE
    assert result.failure.retryable


def test_socket_client_cancellation_is_bounded_and_checks_response_id() -> None:
    cancelled = threading.Event()
    cancelled.set()
    socket_obj = _Socket()
    result = HerdrClient(socket_factory=lambda *args: socket_obj).socket_request(
        Path("/tmp/herdr.sock"), "agent.prompt", {"text": "secret"}, cancelled=cancelled
    )
    assert result.failure is not None
    assert result.failure.code == FailureCode.RETRYABLE

    request = {"id": "wrong", "result": {"type": "agent_prompt"}}
    result = HerdrClient(
        socket_factory=lambda *args: _Socket((json.dumps(request) + "\n").encode())
    ).socket_request(Path("/tmp/herdr.sock"), "agent.prompt", {"text": "secret"})
    assert result.failure is not None
    assert result.failure.code == FailureCode.CONFLICT


def _snapshot(**overrides):
    value = {
        "session": "default",
        "revision": "r1",
        "spaces": [{"id": "space-1"}],
        "tabs": [{"id": "tab-1", "space_id": "space-1"}],
        "panes": [{"id": "pane-1", "tab_id": "tab-1", "space_id": "space-1"}],
        "agents": [
            {
                "name": "worker-1",
                "state": "idle",
                "pane_id": "pane-1",
                "session": "default",
                "tokens": {"bh_generation": "1"},
            }
        ],
    }
    value.update(overrides)
    return value


def test_snapshot_parser_rejects_duplicate_and_dangling_topology_identity() -> None:
    duplicate = parse_snapshot(_snapshot(spaces=[{"id": "space-1"}, {"id": "space-1"}]))
    dangling = parse_snapshot(_snapshot(panes=[{"id": "pane-1", "tab_id": "missing"}]))
    missing_identity = parse_snapshot(_snapshot(spaces=[{}]))

    assert duplicate.failure is not None
    assert duplicate.failure.code == FailureCode.CONFLICT
    assert dangling.failure is not None
    assert dangling.failure.code == FailureCode.CONFLICT
    assert missing_identity.failure is not None
    assert missing_identity.failure.code == FailureCode.CONFLICT


def test_snapshot_parser_rejects_broken_space_tab_pane_agent_ancestry() -> None:
    broken_tab = parse_snapshot(
        _snapshot(
            spaces=[{"id": "space-1"}, {"id": "space-2"}],
            tabs=[{"id": "tab-1", "space_id": "space-2"}],
            panes=[{"id": "pane-1", "tab_id": "tab-1", "space_id": "space-1"}],
        )
    )
    broken_agent = parse_snapshot(
        _snapshot(
            tabs=[
                {"id": "tab-1", "space_id": "space-1"},
                {"id": "tab-2", "space_id": "space-1"},
            ],
            agents=[
                {
                    "name": "worker-1",
                    "state": "idle",
                    "pane_id": "pane-1",
                    "tab_id": "tab-2",
                    "space_id": "space-1",
                }
            ],
        )
    )

    assert broken_tab.failure is not None
    assert broken_tab.failure.code == FailureCode.CONFLICT
    assert broken_agent.failure is not None
    assert broken_agent.failure.code == FailureCode.CONFLICT


def test_versioned_snapshot_rejects_missing_ancestry_ids() -> None:
    cases = (
        {"tabs": [{"id": "tab-1"}]},
        {"panes": [{"id": "pane-1", "tab_id": "tab-1"}]},
        {"agents": [{"name": "worker-1", "state": "idle", "pane_id": "pane-1"}]},
    )

    for change in cases:
        payload = _snapshot()
        payload.update(change)
        result = parse_snapshot(payload)
        assert result.failure is not None
        assert result.failure.code == FailureCode.CONFLICT


def test_snapshot_parser_rejects_cross_session_generation_and_stale_topology() -> None:
    cross_session = parse_snapshot(
        _snapshot(agents=[{"name": "worker-1", "state": "idle", "session": "other"}])
    )
    cross_generation = parse_snapshot(_snapshot(generation=2))
    stale = parse_snapshot(_snapshot(stale=True))

    assert cross_session.failure is not None
    assert cross_session.failure.code == FailureCode.CONFLICT
    assert cross_generation.failure is not None
    assert cross_generation.failure.code == FailureCode.CONFLICT
    assert stale.failure is not None
    assert stale.failure.code == FailureCode.STALE


def test_typed_command_failure_never_exposes_provider_stderr() -> None:
    secret = "provider-secret-should-not-escape"

    def refused(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, "", secret)

    result = HerdrClient(runner=refused).command("api", "snapshot")
    assert result.failure is not None
    assert secret not in result.failure.message
    assert secret not in result.failure.detail
