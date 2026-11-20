"""One seam for the session renew/evidence e2e test: a fake or a staged hq-service (bh-zephr).

``SessionStack`` hands the test body ONE thing: the real beadhive client code (the sender, the
heartbeat report module, the hq-service adapter and the client-owned renew seq) wired to an
``httpx`` transport. In ``fake`` mode the transport answers from the in-process
:class:`~harness.fake_hq_service.FakeHqService` on a mocked clock. In ``staged`` mode it is a real
HTTPS transport to a staged hq-service, configured only from environment variable NAMES (see
``STAGED_ENV``; values, key paths included, are never printed) on the wall clock. The test body
cannot tell them apart; everything below the transport is production code.

Only this frame's grant reads are replaced: the anchor (``read_frame_anchor``, which a staged run
could only verify with trust pins its environment does not carry) and a beat generator that builds
the frame's beat from the identity under test. The same stubs apply in both modes, and no SQL
frame runtime exists in either: the host is ``hq.route: api`` (bh-65s71). The real grant reads
are driven end to end against the fake in ``tests/test_hq_api_route_liveness.py``.

The recording transport logs every request the CLIENT made (kind, status, code or outcome), and can
answer a request with a scripted response before it reaches the service (``fault``): a refusal
that the service would give but a staged stack cannot be made to give on demand.
"""

from __future__ import annotations

import json
import os
import time
import types
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from beadhive import heartbeat_report as report
from beadhive import heartbeat_sender, hq_service_adapter
from beadhive.hq_framelease_contracts import HeartbeatLease
from beadhive.hq_port import FrameAnchor
from beadhive.hq_service_adapter import PREFIX, adapter_from_config
from beadhive.hq_sql_signatures import fingerprint

from .clock import FakeClock
from .fake_hq_service import FakeHqService, error_response

CORPUS = Path(__file__).parent.parent / "fixtures/hq_service_contract/session_v1"

#: Environment variable NAMES the staged run reads (values are never echoed). Required unless
#: marked optional. Paths are read from the named variable; the test never prints them.
STAGED_ENV = {
    "BH_STAGED_HQ_SERVICE": "opt-in switch: must be `1` for the staged run to be attempted",
    "BH_STAGED_HQ_URL": "base URL of the staged hq-service (https://...)",
    "BH_STAGED_HQ_SIGNING_KEY": "absolute path of the frame signing key the grant names",
    "BH_STAGED_HQ_PRINCIPAL": "frame principal under test (path segment of the session routes)",
    "BH_STAGED_HQ_FRAME": "frame id of that incarnation",
    "BH_STAGED_HQ_HOLDER": "holder identity of that incarnation",
    "BH_STAGED_HQ_INSTANCE": "instance_ref of that incarnation",
    "BH_STAGED_HQ_EPOCH": "committed epoch of that incarnation (integer)",
    "BH_STAGED_HQ_CLIENT_CERT": "(optional) mTLS client certificate path",
    "BH_STAGED_HQ_CLIENT_KEY": "(optional) mTLS client key path",
    "BH_STAGED_HQ_CA_CERT": "(optional) CA bundle path for the staged server certificate",
    "BH_STAGED_HQ_AUDIENCE": "(optional) beat audience, default `hq`",
    "BH_STAGED_HQ_CONFIG_REVISION": "(optional) beat config_revision, default `cfg-1`",
}
_REQUIRED = (
    "BH_STAGED_HQ_URL",
    "BH_STAGED_HQ_SIGNING_KEY",
    "BH_STAGED_HQ_PRINCIPAL",
    "BH_STAGED_HQ_FRAME",
    "BH_STAGED_HQ_HOLDER",
    "BH_STAGED_HQ_INSTANCE",
    "BH_STAGED_HQ_EPOCH",
)


def staged_skip_reason() -> str | None:
    """Why the staged run cannot be attempted, or ``None`` when it is opted in and configured."""
    if os.environ.get("BH_STAGED_HQ_SERVICE") != "1":
        return "staged hq-service run is opt-in: set BH_STAGED_HQ_SERVICE=1"
    missing = [name for name in _REQUIRED if not os.environ.get(name)]
    if missing:
        return "staged hq-service run opted in but unset: " + ", ".join(missing)
    return None


@dataclass(frozen=True)
class Identity:
    principal: str = "frame_principal_a"
    frame: str = "frame-a"
    holder: str = "host-a"
    instance: str = "inst-host-a"
    epoch: int = 3


class WallClock:
    """The staged run's clock: real time. ``advance`` is a no-op (time moves by itself)."""

    def wall(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def advance(self, seconds: float) -> None:  # noqa: ARG002 - staged time is not ours to move
        return None


class Recorder(httpx.BaseTransport):
    """Wraps the real transport: logs each client request and applies scripted faults."""

    def __init__(self, inner: httpx.BaseTransport, log: list[dict], faults: list) -> None:
        self._inner, self._log, self._faults = inner, log, faults

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        kind = request.url.path.rsplit("/", 1)[-1]
        injected = False
        for index, (want, make) in enumerate(self._faults):
            if want == kind:
                del self._faults[index]
                response, injected = make(), True
                break
        else:
            response = self._inner.handle_request(request)
        response.read()
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else None
        self._log.append(
            {
                "kind": kind,
                "status": response.status_code,
                "code": error.get("code") if isinstance(error, dict) else None,
                "outcome": payload.get("outcome") if isinstance(payload, dict) else None,
                "injected": injected,
                "body": request.content,
            }
        )
        return response


@dataclass
class Process:
    """One simulated OS process: its own adapter/connection and its own in-memory ``_ACCEPTED``.

    Processes share the per-host renew-seq state directory (``BH_HOME``), as real ones do."""

    adapter: Any
    accepted: dict = field(default_factory=dict)
    renewer: Any = None


class SessionStack:
    def __init__(self, mode: str, tmp_path: Path, monkeypatch, identity: Identity | None = None):
        self.mode = mode
        self.fake: FakeHqService | None = None
        self.log: list[dict] = []
        self.faults: list = []
        self.tag = "conformance-1"  # the conformance cache's content, via the stubbed generator
        self.current: Process | None = None
        self._on_generate: Callable[[], None] | None = None
        self._monkeypatch = monkeypatch
        tmp_path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("BH_HOME", str(tmp_path / "bh-home"))  # the renew seq state: never real
        if mode == "fake":
            self.identity = identity or Identity()
            self.clock: Any = FakeClock(datetime(2026, 11, 18, 12, 0, tzinfo=UTC))
            self.signing_key, public = _generate_key(tmp_path)
            self.fake = FakeHqService(self.clock.wall)
            self.fake.provision(
                self.identity.principal,
                frame_id=self.identity.frame,
                holder=self.identity.holder,
                instance=self.identity.instance,
                epoch=self.identity.epoch,
                public_key=public,
            )
            self._inner: httpx.BaseTransport = self.fake.transport()
            self.url = "http://hq"
            # deadlines run on the mocked clock too: nothing sleeps, nothing reads wall time
            frozen = types.SimpleNamespace(monotonic=self.clock.monotonic)
            monkeypatch.setattr(hq_service_adapter, "time", frozen)
            monkeypatch.setattr(report, "_port_deadline", lambda plane: self.clock.monotonic() + 30)
        else:
            env = os.environ
            self.identity = Identity(
                env["BH_STAGED_HQ_PRINCIPAL"],
                env["BH_STAGED_HQ_FRAME"],
                env["BH_STAGED_HQ_HOLDER"],
                env["BH_STAGED_HQ_INSTANCE"],
                int(env["BH_STAGED_HQ_EPOCH"]),
            )
            self.clock = WallClock()
            self.signing_key = env["BH_STAGED_HQ_SIGNING_KEY"]
            self.url = env["BH_STAGED_HQ_URL"]
            cert, key = env.get("BH_STAGED_HQ_CLIENT_CERT"), env.get("BH_STAGED_HQ_CLIENT_KEY")
            self._inner = httpx.HTTPTransport(
                verify=env.get("BH_STAGED_HQ_CA_CERT") or True,
                cert=(cert, key) if cert and key else None,
            )
            public = None
        self.public_key = public
        self.anchor = FrameAnchor(
            principal=self.identity.principal,
            frame=self.identity.frame,
            epoch=self.identity.epoch,
            holder=self.identity.holder,
            instance=self.identity.instance,
            carrier="inbox",
        )
        monkeypatch.setattr(report, "_sql_plane", self._no_sql_plane)
        monkeypatch.setattr(report, "generate", self._generate)
        monkeypatch.setattr(
            report.config,
            "load_host",
            lambda: {"hq": {"route": "api", "api": {"url": self.url}}},
        )
        monkeypatch.setattr(heartbeat_sender, "_select_port", self._select_port)
        monkeypatch.delenv(report.SESSION_VIA_PORT_ENV, raising=False)
        self.process = self.new_process()

    # ---- the stubbed frame authority ---------------------------------------------------

    @staticmethod
    def _no_sql_plane():
        raise AssertionError("an hq.route api host read a SQL frame plane")

    def _generate(self, plane, **_kwargs) -> HeartbeatLease:
        """The beat generator: this frame's beat for the CURRENT conformance cache content."""
        if self._on_generate is not None:
            hook, self._on_generate = self._on_generate, None
            hook()
        return self.lease(self.tag)

    def conformance_report(self, tag: str) -> dict:
        return {
            "method": report.REPORT_METHOD,
            "checks": [{"id": "installed-release", "status": "pass", "evidence": tag}],
            "free_sessions": 0,
        }

    def lease(self, tag: str) -> HeartbeatLease:
        """The unsigned beat for the identity under test, stamped with the stack clock's now."""
        spec = json.loads(
            json.loads((CORPUS / "renew-accepted-first-beat.json").read_text())["request"]["body"]
        )["spec"]
        spec.pop("signature")
        rep = self.conformance_report(tag)
        spec.update(
            frame_id=self.identity.frame,
            holderIdentity=self.identity.holder,
            instance_ref=self.identity.instance,
            epoch=self.identity.epoch,
            key_id=fingerprint(self._public()),
            seq=1,  # the client-owned seq store raises it; the generator's value is a lower bound
            renewTime=datetime.fromtimestamp(int(self.clock.wall()), UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            report_digest=report.report_digest(rep),
            free_sessions=0,
        )
        if os.environ.get("BH_STAGED_HQ_AUDIENCE") and self.mode == "staged":
            spec["audience"] = os.environ["BH_STAGED_HQ_AUDIENCE"]
        if os.environ.get("BH_STAGED_HQ_CONFIG_REVISION") and self.mode == "staged":
            spec["config_revision"] = os.environ["BH_STAGED_HQ_CONFIG_REVISION"]
        spec["conformance"] = {
            "profile": "reviewed",
            "status": "conformant",
            "checks": rep["checks"],
        }
        return HeartbeatLease.model_validate(spec)

    def _public(self) -> str:
        if self.public_key:
            return self.public_key
        private = serialization.load_ssh_private_key(
            Path(self.signing_key).read_bytes(), password=None
        )
        return (
            private.public_key()
            .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
            .decode()
        )

    # ---- processes ---------------------------------------------------------------------

    def new_process(self) -> Process:
        """A fresh process: a cold incarnation (nothing accepted in memory), same seq state dir."""
        adapter = adapter_from_config(
            {
                "hq": {"route": "api", "api": {"url": self.url}},
                "host": {"frame_id": self.identity.frame},
            },
            transport=Recorder(self._inner, self.log, self.faults),
            signing_key=self.signing_key,
            clock=self.clock.wall,
        )
        adapter.read_frame_anchor = lambda **_: self.anchor  # the grant read, stubbed (above)
        return Process(adapter)

    def _select_port(self):
        assert self.current is not None, "port selected outside a process"
        return self.current.adapter

    @contextmanager
    def as_process(self, process: Process) -> Iterator[Process]:
        """Run as *process*: its memory-held acceptance (``_ACCEPTED``) and its adapter."""
        before_current, before_accepted = self.current, report._ACCEPTED
        self.current, report._ACCEPTED = process, process.accepted
        try:
            yield process
        finally:
            self.current, report._ACCEPTED = before_current, before_accepted

    # ---- the two actors ----------------------------------------------------------------

    def timer_tick(self, process: Process) -> Any:
        """One tick of the renew timer (``heartbeat_sender renew``) in *process*."""
        with self.as_process(process):
            if process.renewer is None:
                process.renewer = report.session_renewer(heartbeat_sender._select_port)
            return process.renewer.tick()

    def conformance_run(self, *, tag: str | None = None, process: Process | None = None) -> Any:
        """One conformance-job run: refresh the cache, then publish evidence (renew-first).

        The job is its own process: by default a fresh, cold one."""
        if tag is not None:
            self.tag = tag
        with self.as_process(process or self.new_process()):
            return heartbeat_sender._publish_evidence()

    # ---- bookkeeping -------------------------------------------------------------------

    def fault(self, kind: str, code: str | None = None, *, status: int = 503) -> None:
        """Answer the next *kind* request with a scripted response, before the service sees it:
        the service's refusal *code*, or (no code) a 503 outage."""
        if code is not None:
            self.faults.append((kind, lambda: error_response(code)))
        else:
            self.faults.append(
                (
                    kind,
                    lambda: httpx.Response(
                        status,
                        json={
                            "error": {
                                "code": "request_unavailable",
                                "message": "down",
                                "retryable": True,
                            }
                        },
                    ),
                )
            )

    def kinds(self) -> list[str]:
        return [entry["kind"] for entry in self.log]

    def refusals(self) -> list[str]:
        return [entry["code"] for entry in self.log if entry["code"]]

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)

    def reset_log(self) -> None:
        self.log.clear()


def _generate_key(tmp_path: Path) -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    path = tmp_path / "frame-id_ed25519"
    path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    return str(path), public


__all__ = ["PREFIX", "STAGED_ENV", "Identity", "SessionStack", "staged_skip_reason"]
