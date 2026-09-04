"""The single-process Beadhive host daemon core.

The daemon is deliberately an additive runtime.  Importing this module does not start it,
ordinary ``bh`` commands never connect to it, and :mod:`beadhive.mcp` keeps owning the stdio
entrypoint.  Network surfaces join the daemon through :func:`build_application`; they do not
create another listener or lifespan.

Lifecycle ordering is explicit.  Startup callbacks run in ``StartupPhase`` order, followed by
component lifespan entry, before readiness is published.  Shutdown first makes readiness and
``accepting`` false, then runs callbacks and component exits in ``ShutdownPhase`` order.  Within
one phase callbacks retain registration order and components exit in reverse startup order.  A
single finite deadline covers the whole drain.
"""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import hmac
import inspect
import ipaddress
import json
import os
import platform
import secrets
import ssl
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import IntEnum
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import config
from . import host as host_identity
from .daemon_contract import CONTRACT_VERSION, WIRE_SCHEMA_VERSION, HealthResponse

if TYPE_CHECKING:
    from .daemon_config import HostDaemonConfig
    from .daemon_network import NetworkAdmissionPolicy

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8737
DEFAULT_SHUTDOWN_BUDGET = 30.0
_CONTROL_KEY_BYTES = 32


def _secure_ssl_context(
    _uvicorn_config: Any,
    default_factory: Callable[[], ssl.SSLContext],
    *,
    minimum_version: str,
) -> ssl.SSLContext:
    """Harden Uvicorn's certificate-loaded context with the configured protocol floor."""

    context = default_factory()
    context.minimum_version = {
        "TLSv1.2": ssl.TLSVersion.TLSv1_2,
        "TLSv1.3": ssl.TLSVersion.TLSv1_3,
    }[minimum_version]
    context.options |= ssl.OP_NO_COMPRESSION
    return context


class DaemonError(RuntimeError):
    """Base class for operator-facing daemon startup failures."""


class AlreadyRunningError(DaemonError):
    """The singleton lock is owned by another process."""


class ListenerConfigurationError(DaemonError):
    """The phase-one listener configuration is unsafe or invalid."""


class StartupPhase(IntEnum):
    """Ordered extension points which run before traffic becomes ready."""

    TELEMETRY = 10
    SECURITY = 20
    RESOURCES = 30


class ShutdownPhase(IntEnum):
    """Ordered daemon drain phases from the accepted host-daemon ADR."""

    REJECT_NEW_WORK = 10
    DRAIN_IN_FLIGHT = 20
    CLOSE_SESSIONS = 30
    CANCEL_PROCESSES = 40
    CLOSE_RESOURCES = 50
    FLUSH_TELEMETRY = 60


AsyncCallback = Callable[[], Awaitable[None]]
LifespanFactory = Callable[[Starlette], AbstractAsyncContextManager[Any]]


@dataclass(frozen=True)
class LifespanComponent:
    """One daemon-owned async context composed into the outer lifespan.

    ``startup_phase`` orders entry relative to other components.  ``shutdown_phase`` selects
    the drain stage in which ``__aexit__`` runs.  Components at the same startup phase enter in
    declaration order and exit in reverse declaration order.
    """

    name: str
    lifespan: LifespanFactory
    startup_phase: StartupPhase = StartupPhase.RESOURCES
    shutdown_phase: ShutdownPhase = ShutdownPhase.CLOSE_RESOURCES


@dataclass(frozen=True)
class CallbackResult:
    name: str
    phase: str
    status: str
    duration_seconds: float
    error: str = ""


@dataclass(frozen=True)
class _RegisteredCallback:
    phase: IntEnum
    order: int
    name: str
    callback: AsyncCallback


class DaemonRuntime:
    """Readiness, admission, and ordered lifecycle state shared by every route."""

    def __init__(self, *, shutdown_budget: float = DEFAULT_SHUTDOWN_BUDGET) -> None:
        if not 0 < shutdown_budget < float("inf"):
            raise ValueError("shutdown budget must be finite and greater than zero")
        self.shutdown_budget = float(shutdown_budget)
        self.ready = False
        self.accepting = False
        self.shutdown_started = False
        self.shutdown_results: tuple[CallbackResult, ...] = ()
        self._startup: list[_RegisteredCallback] = []
        self._drain: list[_RegisteredCallback] = []
        self._order = 0

    def _next_order(self) -> int:
        self._order += 1
        return self._order

    @staticmethod
    def _require_async(callback: AsyncCallback) -> None:
        if not inspect.iscoroutinefunction(callback):
            raise TypeError("daemon lifecycle callbacks must be async functions")

    def register_startup(self, phase: StartupPhase, name: str, callback: AsyncCallback) -> None:
        """Register an async startup callback; equal-phase callbacks keep registration order."""
        self._require_async(callback)
        self._startup.append(_RegisteredCallback(phase, self._next_order(), name, callback))

    def register_drain(self, phase: ShutdownPhase, name: str, callback: AsyncCallback) -> None:
        """Register an async drain callback under the daemon's one shutdown deadline."""
        self._require_async(callback)
        self._drain.append(_RegisteredCallback(phase, self._next_order(), name, callback))

    async def start(self) -> None:
        """Run ordered startup callbacks and leave admission closed until the caller is ready."""
        for item in sorted(self._startup, key=lambda value: (value.phase, value.order)):
            await item.callback()

    def mark_ready(self) -> None:
        if self.shutdown_started:
            raise RuntimeError("a draining daemon cannot become ready")
        self.accepting = True
        self.ready = True

    def begin_shutdown(self) -> None:
        """Close admission synchronously, before any potentially-blocking drain work."""
        self.ready = False
        self.accepting = False
        self.shutdown_started = True

    async def shutdown(
        self, extra_callbacks: Sequence[_RegisteredCallback] = ()
    ) -> tuple[CallbackResult, ...]:
        """Drain under one deadline, then propagate the first observed task cancellation.

        ``CancelledError`` is a ``BaseException``.  Treating it like an ordinary callback error
        would be wrong, but propagating it immediately would strand the remaining component
        contexts and leave their async generators for event-loop finalization.  Record it,
        continue bounded cleanup in this task/context, publish every deterministic outcome, and
        only then preserve cancellation semantics by re-raising the first cancellation.
        """
        self.begin_shutdown()
        deadline = time.monotonic() + self.shutdown_budget
        callbacks = sorted(
            (*self._drain, *extra_callbacks), key=lambda item: (item.phase, item.order)
        )
        results: list[CallbackResult] = []
        pending_cancellation: asyncio.CancelledError | None = None

        for index, item in enumerate(callbacks):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                results.extend(
                    CallbackResult(
                        name=pending.name,
                        phase=pending.phase.name.lower(),
                        status="skipped_budget_exhausted",
                        duration_seconds=0.0,
                    )
                    for pending in callbacks[index:]
                )
                break
            started = time.monotonic()
            try:
                # ``asyncio.timeout`` keeps the callback in this Task/Context.  Lifespan
                # managers (including FastMCP's) set ContextVar tokens on entry and must reset
                # them from that same context on exit; ``wait_for`` would spawn a child Task and
                # make an otherwise-correct composed lifespan fail during token reset.
                async with asyncio.timeout(remaining):
                    await item.callback()
            except asyncio.CancelledError as exc:
                if pending_cancellation is None:
                    pending_cancellation = exc
                results.append(
                    CallbackResult(
                        name=item.name,
                        phase=item.phase.name.lower(),
                        status="cancelled",
                        duration_seconds=time.monotonic() - started,
                        error="CancelledError",
                    )
                )
            except TimeoutError:
                results.append(
                    CallbackResult(
                        name=item.name,
                        phase=item.phase.name.lower(),
                        status="timed_out",
                        duration_seconds=time.monotonic() - started,
                    )
                )
            except Exception as exc:  # cleanup is best-effort; later owners still get their turn
                results.append(
                    CallbackResult(
                        name=item.name,
                        phase=item.phase.name.lower(),
                        status="error",
                        duration_seconds=time.monotonic() - started,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
            else:
                results.append(
                    CallbackResult(
                        name=item.name,
                        phase=item.phase.name.lower(),
                        status="completed",
                        duration_seconds=time.monotonic() - started,
                    )
                )

        self.shutdown_results = tuple(results)
        if pending_cancellation is not None:
            raise pending_cancellation
        return self.shutdown_results


@dataclass(frozen=True)
class DaemonKey:
    """The v1 singleton scope: account, canonical ``BH_HOME``, and stable host id."""

    account_id: str
    bh_home: str
    host_id: str

    @classmethod
    def current(cls) -> DaemonKey:
        home = str(config.home().expanduser().resolve(strict=False))
        account = f"uid:{os.getuid()}" if hasattr(os, "getuid") else f"user:{getpass.getuser()}"
        return cls(account_id=account, bh_home=home, host_id=host_identity.host_id())

    @property
    def digest(self) -> str:
        raw = "\0".join((self.account_id, self.bh_home, self.host_id)).encode()
        return hashlib.sha256(raw).hexdigest()[:20]


@dataclass(frozen=True)
class DaemonPaths:
    directory: Path
    lock: Path
    control: Path

    @classmethod
    def for_key(cls, key: DaemonKey) -> DaemonPaths:
        directory = Path(key.bh_home) / "run" / "host-daemon"
        stem = f"daemon-{key.digest}"
        return cls(
            directory=directory,
            lock=directory / f"{stem}.lock",
            control=directory / f"{stem}.json",
        )


@dataclass(frozen=True)
class ControlRecord:
    contract: str
    account_id: str
    bh_home: str
    host_id: str
    instance_id: str
    pid: int
    process_start: str
    listener_host: str
    listener_port: int
    started_at: str
    authentication: str = ""

    @classmethod
    def create(
        cls,
        key: DaemonKey,
        *,
        listener_host: str,
        listener_port: int,
        verification_key: bytes,
    ) -> ControlRecord:
        record = cls(
            contract=CONTRACT_VERSION,
            account_id=key.account_id,
            bh_home=key.bh_home,
            host_id=key.host_id,
            instance_id=str(uuid.uuid4()),
            pid=os.getpid(),
            process_start=_process_start_token(os.getpid()),
            listener_host=listener_host,
            listener_port=listener_port,
            started_at=datetime.now(UTC).isoformat(),
            authentication="",
        )
        return record.authenticated(verification_key)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ControlRecord:
        record = cls(
            contract=str(value["contract"]),
            account_id=str(value["account_id"]),
            bh_home=str(value["bh_home"]),
            host_id=str(value["host_id"]),
            instance_id=str(value["instance_id"]),
            pid=int(value["pid"]),
            process_start=str(value["process_start"]),
            listener_host=str(value["listener_host"]),
            listener_port=int(value["listener_port"]),
            started_at=str(value["started_at"]),
            authentication=str(value["authentication"]),
        )
        uuid.UUID(record.instance_id)
        if len(record.authentication) != hashlib.sha256().digest_size * 2:
            raise ValueError("control record authentication has the wrong size")
        return record

    def authenticated(self, verification_key: bytes) -> ControlRecord:
        """Return a record carrying an HMAC over its canonical public fields."""
        if len(verification_key) != _CONTROL_KEY_BYTES:
            raise ValueError("daemon control verification key has the wrong size")
        return replace(
            self,
            authentication=hmac.new(
                verification_key, self._authentication_payload(), hashlib.sha256
            ).hexdigest(),
        )

    def verify(self, verification_key: bytes) -> bool:
        expected = hmac.new(
            verification_key, self._authentication_payload(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(self.authentication, expected)

    def _authentication_payload(self) -> bytes:
        values = asdict(self)
        values.pop("authentication")
        return json.dumps(values, sort_keys=True, separators=(",", ":")).encode()

    def matches(self, key: DaemonKey) -> bool:
        return (
            self.contract == CONTRACT_VERSION
            and self.account_id == key.account_id
            and self.bh_home == key.bh_home
            and self.host_id == key.host_id
        )


@dataclass(frozen=True)
class DaemonStatus:
    state: str
    running: bool
    verified: bool
    detail: str
    key: DaemonKey
    record: ControlRecord | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "running": self.running,
            "verified": self.verified,
            "detail": self.detail,
            "key": asdict(self.key),
            "record": asdict(self.record) if self.record else None,
        }


def _process_start_token(pid: int) -> str:
    """A read-only PID-reuse fence, not a liveness signal and never a kill/adoption action."""
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        raw = proc_stat.read_text()
        # ``comm`` is parenthesized and may contain spaces or ``)``; fields after its final
        # parenthesis start at field 3.  Start time is field 22, hence offset 19 here.
        fields = raw[raw.rfind(")") + 2 :].split()
        return f"linux:{fields[19]}"
    except (OSError, IndexError):
        pass

    if platform.system() in {"Darwin", "FreeBSD"}:
        try:
            result = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                check=False,
                capture_output=True,
                text=True,
                timeout=1.0,
            )
            if result.returncode == 0 and result.stdout.strip():
                return f"ps:{result.stdout.strip()}"
        except (OSError, subprocess.SubprocessError):
            pass
    return "unavailable"


def _read_control(
    path: Path, verification_key: bytes | None = None
) -> tuple[ControlRecord | None, str]:
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("control record root is not an object")
        record = ControlRecord.from_dict(value)
        if verification_key is not None and not record.verify(verification_key):
            raise ValueError("control record authentication failed")
        return record, ""
    except FileNotFoundError:
        return None, "missing control record"
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        return None, f"invalid control record: {exc}"


def _lock_held(path: Path) -> bool:
    """Probe the named flock only; never inspect, signal, kill, or adopt its owning PID."""
    if not path.exists():
        return False
    import fcntl

    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


class DaemonSingleton:
    """An owned flock plus incarnation-scoped control record."""

    def __init__(
        self,
        *,
        key: DaemonKey,
        paths: DaemonPaths,
        fd: int,
        record: ControlRecord,
        verification_key: bytes,
    ):
        self.key = key
        self.paths = paths
        self.fd = fd
        self.record = record
        self.verification_key = verification_key
        self._released = False

    @classmethod
    def acquire(cls, key: DaemonKey, *, listener_host: str, listener_port: int) -> DaemonSingleton:
        import fcntl

        paths = DaemonPaths.for_key(key)
        paths.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            paths.directory.chmod(0o700)
        except OSError:
            pass
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(paths.lock, flags, 0o600)
        os.fchmod(fd, 0o600)
        os.set_inheritable(fd, False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            record, problem = _read_verified_control(paths)
            owner = f"pid {record.pid}, instance {record.instance_id}" if record else problem
            raise AlreadyRunningError(
                f"host daemon singleton is already held for this account/BH_HOME/host_id ({owner})"
            ) from exc

        try:
            verification_key = secrets.token_bytes(_CONTROL_KEY_BYTES)
            _write_verification_key(fd, verification_key)
            record = ControlRecord.create(
                key,
                listener_host=listener_host,
                listener_port=listener_port,
                verification_key=verification_key,
            )
            _write_control(paths.control, record)
        except Exception:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise
        return cls(
            key=key,
            paths=paths,
            fd=fd,
            record=record,
            verification_key=verification_key,
        )

    def release(self) -> None:
        """Remove only this incarnation's record, then release its lock; idempotent."""
        if self._released:
            return
        import fcntl

        try:
            current, _problem = _read_control(self.paths.control, self.verification_key)
            if current is not None and current.instance_id == self.record.instance_id:
                self.paths.control.unlink(missing_ok=True)
        finally:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self._released = True

    def __enter__(self) -> DaemonSingleton:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _write_control(path: Path, record: ControlRecord) -> None:
    temporary = path.with_name(f".{path.name}.{record.instance_id}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(temporary, flags, 0o600)
    try:
        try:
            payload = (json.dumps(asdict(record), sort_keys=True, indent=2) + "\n").encode()
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(fd)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        # ENOSPC can surface while writing/fsyncing, before os.replace is reached.  Never
        # strand an incarnation-named partial record for status/restart recovery to trip over.
        temporary.unlink(missing_ok=True)


def _write_verification_key(fd: int, verification_key: bytes) -> None:
    if len(verification_key) != _CONTROL_KEY_BYTES:
        raise ValueError("daemon control verification key has the wrong size")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    written = os.write(fd, verification_key)
    if written != len(verification_key):  # pragma: no cover - defensive short-write guard
        raise OSError("short write while installing daemon control verification key")
    os.fsync(fd)


def _read_verification_key(path: Path) -> bytes | None:
    try:
        verification_key = path.read_bytes()
    except OSError:
        return None
    return verification_key if len(verification_key) == _CONTROL_KEY_BYTES else None


def _read_verified_control(paths: DaemonPaths) -> tuple[ControlRecord | None, str]:
    verification_key = _read_verification_key(paths.lock)
    if verification_key is not None:
        return _read_control(paths.control, verification_key)
    record, problem = _read_control(paths.control)
    if record is None:
        return None, problem
    return None, "invalid control record: verification key is missing or invalid"


def daemon_status(key: DaemonKey | None = None) -> DaemonStatus:
    """Verify local singleton ownership and process incarnation without probing the port."""
    expected = key or DaemonKey.current()
    paths = DaemonPaths.for_key(expected)
    held = _lock_held(paths.lock)
    record, problem = _read_verified_control(paths)

    if not held:
        if record is None and problem == "missing control record":
            return DaemonStatus("stopped", False, False, "no daemon owns the singleton", expected)
        detail = problem if record is None else "a stale control record remains"
        return DaemonStatus(
            "stale",
            False,
            False,
            f"singleton is free but {detail}",
            expected,
            record,
        )
    if record is None:
        return DaemonStatus("unverified", True, False, problem, expected)
    if not record.matches(expected):
        return DaemonStatus(
            "unverified",
            True,
            False,
            "control record identity does not match this host",
            expected,
            record,
        )
    current_start = _process_start_token(record.pid)
    if current_start == "unavailable" or current_start != record.process_start:
        return DaemonStatus(
            "unverified",
            True,
            False,
            "control record PID incarnation could not be verified",
            expected,
            record,
        )
    return DaemonStatus(
        "running",
        True,
        True,
        "singleton, host identity, and process incarnation verified",
        expected,
        record,
    )


class _DrainGateMiddleware:
    def __init__(self, app: ASGIApp, *, runtime: DaemonRuntime) -> None:
        self.app = app
        self.runtime = runtime

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] in {"http", "websocket"}
            and scope.get("path") != "/health"
            and not self.runtime.accepting
        ):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013})
            else:
                await JSONResponse(
                    {
                        "schemaVersion": WIRE_SCHEMA_VERSION,
                        "error": {
                            "code": "daemon_draining",
                            "message": "The daemon is draining and cannot accept new work.",
                            "retryable": True,
                            "action": "retry",
                            "requestId": None,
                        },
                    },
                    status_code=503,
                )(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _McpMethodPassthroughMiddleware:
    """Keep the operator method policy while admitting FastMCP's own HTTP verbs."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        fallback_middleware: Callable[[ASGIApp], ASGIApp],
    ) -> None:
        self.app = app
        self.fallback = fallback_middleware(app)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        if scope["type"] == "http" and (path == "/mcp" or path.startswith("/mcp/")):
            await self.app(scope, receive, send)
            return
        await self.fallback(scope, receive, send)


@dataclass
class _McpLiveSession:
    session_id: str = field(repr=False)
    credential_session: Any = field(repr=False)
    created_at: float
    last_seen_at: float
    telemetry_connection: Any = field(default=None, repr=False)


class _McpSessionLifecycle:
    """Join FastMCP transports to daemon credential and capacity ownership."""

    def __init__(
        self,
        *,
        credential_sessions: Any,
        network_policy: Any,
        idle_seconds: float,
        absolute_seconds: float,
        telemetry: Any | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._credential_sessions = credential_sessions
        self._network_policy = network_policy
        self._idle_seconds = idle_seconds
        self._absolute_seconds = absolute_seconds
        self._telemetry = telemetry
        self._monotonic = monotonic
        self._manager: Any = None
        self._sessions: dict[str, _McpLiveSession] = {}
        self._lock = asyncio.Lock()
        self._creation_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._stopping = False

    @property
    def active_session_count(self) -> int:
        return len(self._sessions)

    def attach_manager(self, manager: Any) -> None:
        self._manager = manager
        original_handle_request = manager.handle_request

        async def handle_with_creation_ownership(
            scope: Scope,
            receive: Receive,
            send: Send,
        ) -> None:
            # The upstream SDK exposes no transport-created callback.  Serialize only new
            # session requests so the manager-map delta is request-local; established sessions
            # remain concurrent.  Exact ASGI ownership then decides whether a newly-created
            # transport was actually exposed to this authenticated request.
            if manager.stateless or _mcp_session_header(scope.get("headers", ())) is not None:
                await original_handle_request(scope, receive, send)
                return

            async with self._creation_lock:
                from mcp.server.auth.middleware.bearer_auth import (
                    AuthenticatedUser,
                    authorization_context,
                )

                before = frozenset(manager._server_instances)
                exposed_session_id: str | None = None
                user = scope.get("user")
                expected_owner = (
                    authorization_context(user) if isinstance(user, AuthenticatedUser) else None
                )

                async def observe(message: dict[str, Any]) -> None:
                    nonlocal exposed_session_id
                    if message["type"] == "http.response.start":
                        candidate = _mcp_session_header(message.get("headers", ()))
                        if (
                            candidate is not None
                            and candidate not in before
                            and candidate in manager._server_instances
                            and manager._session_owners.get(candidate) == expected_owner
                        ):
                            exposed_session_id = candidate
                    await send(message)

                try:
                    await original_handle_request(scope, receive, observe)
                finally:
                    created = tuple(
                        session_id
                        for session_id in manager._server_instances
                        if session_id not in before
                    )
                    for session_id in created:
                        if session_id != exposed_session_id:
                            await self.terminate(session_id)

        manager.handle_request = handle_with_creation_ownership

    async def register(self, session_id: str, *, bearer: Any, principal: Any) -> None:
        from .daemon_contract import AuthScope

        async with self._lock:
            if session_id in self._sessions:
                return

            async def close_invalidated(reason: Any) -> None:
                await self.terminate(
                    session_id,
                    unregister=False,
                    reason=str(getattr(reason, "value", "cancelled")),
                )

            credential_session = self._credential_sessions.open(
                bearer,
                required_scope=AuthScope.MCP_CONTROL,
                expected_principal=principal.principal,
                close=close_invalidated,
            )
            now = self._monotonic()
            self._sessions[session_id] = _McpLiveSession(
                session_id=session_id,
                credential_session=credential_session,
                created_at=now,
                last_seen_at=now,
                telemetry_connection=(
                    self._telemetry.open_connection("mcp") if self._telemetry is not None else None
                ),
            )
            self._wake.set()

    async def touch(self, session_id: str, *, principal: Any) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            owner = session.credential_session.principal if session is not None else None
            if owner is not None and (
                owner.credential_id,
                owner.principal,
                owner.audience,
            ) == (
                principal.credential_id,
                principal.principal,
                principal.audience,
            ):
                session.last_seen_at = self._monotonic()
                self._wake.set()

    async def terminate(
        self,
        session_id: str,
        *,
        unregister: bool = True,
        reason: str = "client_closed",
    ) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            manager = self._manager
            transport = (
                manager._server_instances.pop(session_id, None) if manager is not None else None
            )
            if manager is not None:
                manager._session_owners.pop(session_id, None)
        if unregister and session is not None:
            self._credential_sessions.unregister(session.credential_session)
        if (
            session is not None
            and session.telemetry_connection is not None
            and self._telemetry is not None
        ):
            self._telemetry.close_connection(session.telemetry_connection, reason=reason)
        try:
            if transport is not None:
                await transport.terminate()
        finally:
            await self._network_policy.forget_mcp_session(session_id)

    async def expire_due(self) -> None:
        now = self._monotonic()
        async with self._lock:
            candidates = tuple(
                session_id
                for session_id, session in self._sessions.items()
                if now - session.last_seen_at >= self._idle_seconds
                or now - session.created_at >= self._absolute_seconds
            )
        for session_id in candidates:
            async with self._lock:
                session = self._sessions.get(session_id)
                current = self._monotonic()
                still_due = session is not None and (
                    current - session.last_seen_at >= self._idle_seconds
                    or current - session.created_at >= self._absolute_seconds
                )
            if still_due:
                await self.terminate(session_id, reason="timeout")

    async def _run_reaper(self) -> None:
        while not self._stopping:
            self._wake.clear()
            await self.expire_due()
            async with self._lock:
                now = self._monotonic()
                timeout = (
                    min(
                        min(
                            session.last_seen_at + self._idle_seconds,
                            session.created_at + self._absolute_seconds,
                        )
                        for session in self._sessions.values()
                    )
                    - now
                    if self._sessions
                    else max(self._idle_seconds, self._absolute_seconds)
                )
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(0.001, timeout))
            except TimeoutError:
                pass

    @asynccontextmanager
    async def lifespan(self):
        self._stopping = False
        reaper = asyncio.create_task(self._run_reaper(), name="daemon-mcp-session-reaper")
        try:
            yield self
        finally:
            self._stopping = True
            self._wake.set()
            await reaper
            async with self._lock:
                session_ids = tuple(self._sessions)
            for session_id in session_ids:
                await self.terminate(session_id, reason="daemon_shutdown")


class _McpSessionLifecycleMiddleware:
    """Observe successful MCP responses and keep all session owners in lockstep."""

    def __init__(self, app: ASGIApp, *, lifecycle: _McpSessionLifecycle) -> None:
        self.app = app
        self.lifecycle = lifecycle

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        if scope.get("type") != "http" or not (path == "/mcp" or path.startswith("/mcp/")):
            await self.app(scope, receive, send)
            return

        await self.lifecycle.expire_due()
        request_id = _mcp_session_header(scope.get("headers", ()))
        method = str(scope.get("method", ""))
        if request_id is not None:
            await self.lifecycle.touch(
                request_id,
                principal=scope.get("state", {})["auth_principal"],
            )
        registered_id: str | None = None
        successful = False
        complete = False

        async def observe(message: dict[str, Any]) -> None:
            nonlocal registered_id, successful, complete
            if message["type"] == "http.response.start":
                successful = 200 <= int(message.get("status", 500)) < 300
                if successful and request_id is None:
                    response_id = _mcp_session_header(message.get("headers", ()))
                    if response_id is not None:
                        registered_id = response_id
                        state = scope.get("state", {})
                        await self.lifecycle.register(
                            registered_id,
                            bearer=state["auth_bearer"],
                            principal=state["auth_principal"],
                        )
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                complete = True

        try:
            await self.app(scope, receive, observe)
        finally:
            if registered_id is not None and not complete:
                await self.lifecycle.terminate(registered_id, reason="cancelled")
            if successful and request_id is not None and method == "DELETE":
                await self.lifecycle.terminate(request_id)


def _mcp_session_header(headers: Sequence[tuple[bytes, bytes]]) -> str | None:
    values = [value for name, value in headers if name.lower() == b"mcp-session-id"]
    if len(values) != 1 or not 1 <= len(values[0]) <= 256:
        return None
    if not all(0x21 <= value <= 0x7E for value in values[0]):
        return None
    return values[0].decode("ascii")


def _mcp_component(
    *,
    stateless_http: bool,
    server_factory: Callable[[], Any],
    session_lifecycle: _McpSessionLifecycle | None = None,
) -> tuple[LifespanComponent, list[BaseRoute]]:
    server = server_factory()
    mcp_app = server.http_app(
        path="/mcp",
        transport="streamable-http",
        stateless_http=stateless_http,
        json_response=False,
    )
    routes = list(mcp_app.routes)
    if session_lifecycle is not None:
        route = next(route for route in routes if getattr(route, "path", None) == "/mcp")

        @asynccontextmanager
        async def mcp_lifespan(app: Starlette):
            async with mcp_app.lifespan(app):
                # FastMCP binds the running manager while entering its lifespan.
                session_lifecycle.attach_manager(route.endpoint.session_manager)
                async with session_lifecycle.lifespan():
                    yield

        component_lifespan = mcp_lifespan
    else:
        component_lifespan = mcp_app.lifespan
    return (
        LifespanComponent(
            name="fastmcp-http",
            lifespan=component_lifespan,
            startup_phase=StartupPhase.RESOURCES,
            shutdown_phase=ShutdownPhase.CLOSE_SESSIONS,
        ),
        routes,
    )


def build_application(
    *,
    runtime: DaemonRuntime | None = None,
    routes: Sequence[BaseRoute] = (),
    components: Sequence[LifespanComponent] = (),
    enable_mcp_http: bool = False,
    stateless_mcp_http: bool = False,
    mcp_server_factory: Callable[[], Any] | None = None,
    mcp_session_lifecycle: _McpSessionLifecycle | None = None,
    middleware: Sequence[Middleware] = (),
    network_policy: NetworkAdmissionPolicy | None = None,
) -> Starlette:
    """Build the one host application and its one outer lifespan.

    Phase one intentionally leaves ``enable_mcp_http`` false.  The explicit switch proves the
    composition needed by the later MCP-HTTP bead without accidentally exposing control tools in
    the unauthenticated first-UI daemon.
    """
    daemon_runtime = runtime or DaemonRuntime()
    owned_components = list(components)
    owned_routes = list(routes)
    if enable_mcp_http:
        if mcp_server_factory is None:
            from .mcp import build_server

            mcp_server_factory = build_server
        mcp_component, mcp_routes = _mcp_component(
            stateless_http=stateless_mcp_http,
            server_factory=mcp_server_factory,
            session_lifecycle=mcp_session_lifecycle,
        )
        owned_components.append(mcp_component)
        owned_routes.extend(mcp_routes)

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            HealthResponse(
                status="stopping" if daemon_runtime.shutdown_started else "live",
                ready=daemon_runtime.ready,
            ).to_wire()
        )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        entered: list[tuple[LifespanComponent, AbstractAsyncContextManager[Any]]] = []
        app.state.daemon_runtime = daemon_runtime
        try:
            await daemon_runtime.start()
            for component in sorted(owned_components, key=lambda value: value.startup_phase):
                context = component.lifespan(app)
                await context.__aenter__()
                entered.append((component, context))
            daemon_runtime.mark_ready()
            yield
        finally:
            daemon_runtime.begin_shutdown()
            exits: list[_RegisteredCallback] = []
            # Large positive orders put component exits after ordinary callbacks in the same
            # phase; enumerating ``reversed(entered)`` preserves reverse startup order.
            for index, (component, context) in enumerate(reversed(entered)):

                async def close_component(
                    context: AbstractAsyncContextManager[Any] = context,
                ) -> None:
                    await context.__aexit__(None, None, None)

                exits.append(
                    _RegisteredCallback(
                        component.shutdown_phase,
                        1_000_000 + index,
                        component.name,
                        close_component,
                    )
                )
            await daemon_runtime.shutdown(exits)

    network_middleware: list[Middleware] = []
    if network_policy is not None:
        from .daemon_network import SecureNetworkBoundaryMiddleware

        network_middleware.append(
            Middleware(SecureNetworkBoundaryMiddleware, policy=network_policy)
        )

    app = Starlette(
        routes=[Route("/health", health, methods=["GET"]), *owned_routes],
        middleware=[
            *network_middleware,
            *middleware,
            Middleware(_DrainGateMiddleware, runtime=daemon_runtime),
        ],
        lifespan=lifespan,
    )
    app.state.daemon_runtime = daemon_runtime
    return app


def build_product_application(
    *,
    runtime: DaemonRuntime,
    control_record: ControlRecord | None = None,
    listener_host: str = DEFAULT_HOST,
    listener_port: int = DEFAULT_PORT,
    allowed_origin: str | None = None,
    cfg: dict | None = None,
    settings: HostDaemonConfig | None = None,
) -> Starlette:
    """Assemble the installed daemon's current routes on the shared composition seam.

    Operator sources extend this one factory with injected routes and middleware; they do not
    replace :func:`serve` or create a listener. MCP HTTP is mounted only for the typed,
    authenticated daemon profile; the historical unauthenticated compatibility profile remains
    read-only and does not expose control tools.
    """
    from .daemon_activity import DurableActivityStore
    from .daemon_activity_api import ActivityPublicationService
    from .daemon_state_broker import DaemonStateBroker
    from .daemon_telemetry import DaemonTelemetry, DaemonTelemetryMiddleware
    from .operator_api import LocalReadPolicyMiddleware, OperatorAPI, ReadOnlyMethodMiddleware

    host_id = control_record.host_id if control_record is not None else host_identity.host_id()
    instance_id = control_record.instance_id if control_record is not None else uuid.uuid4().hex
    telemetry = DaemonTelemetry(
        cfg=cfg or {},
        host_id=host_id,
        instance_id=instance_id,
        flush_budget_seconds=(
            settings.shutdown.telemetry_flush_seconds
            if settings is not None
            else config.otel_flush_timeout(cfg or {})
        ),
    )
    state_broker = DaemonStateBroker.for_host(
        runtime=runtime,
        host_id=host_id,
        cfg=cfg,
        settings=settings,
    )
    sources = state_broker.sources
    feed = state_broker.feed
    relay = state_broker.relay
    relay.telemetry = telemetry
    activity_store = None
    activity_publication = None
    if settings is not None:
        key = (
            DaemonKey(
                account_id=control_record.account_id,
                bh_home=control_record.bh_home,
                host_id=control_record.host_id,
            )
            if control_record is not None
            else DaemonKey.current()
        )
        activity_store = DurableActivityStore(
            DaemonPaths.for_key(key).directory / f"activity-{key.digest}.sqlite3",
            max_record_bytes=settings.activity.max_body_bytes,
            max_records_per_read=settings.activity.max_records_per_read,
            idempotency_retention_seconds=settings.activity.idempotency_retention_seconds,
        )
        activity_publication = ActivityPublicationService(
            sources=sources,
            store=activity_store,
        )
        feed.configure_durable_activity_reader(activity_publication.durable_records)

    async def publish_activity(run_id, body, principal):
        if activity_publication is None:
            raise RuntimeError("activity publication is not configured")
        result = await activity_publication.publish(run_id, body, principal)
        await state_broker.project_latest_activity(run_id)
        return result

    operator = OperatorAPI(
        sources=sources,
        feed=feed,
        host_id=host_id,
        instance_id=instance_id,
        ready=lambda: runtime.ready,
        accepting=lambda: runtime.accepting,
        started_at=(
            max(
                0,
                int(
                    datetime.fromisoformat(
                        control_record.started_at.replace("Z", "+00:00")
                    ).timestamp()
                    * 1_000
                ),
            )
            if control_record is not None
            else time.time_ns() // 1_000_000
        ),
        journal_stale_after_seconds=(
            settings.status.run_journal_stale_after_seconds if settings is not None else 900.0
        ),
        dolt_probe_timeout_seconds=(
            settings.status.dependency_probe_timeout_seconds if settings is not None else 2.0
        ),
        events=state_broker.events,
        snapshot_reader=state_broker.read_snapshot,
        activity_reader=state_broker.read_activity,
        activity_publisher=(publish_activity if activity_publication is not None else None),
        activity_max_body_bytes=(
            settings.activity.max_body_bytes if settings is not None else None
        ),
    )
    operator.factory_directory.telemetry = telemetry
    product_middleware: list[Middleware]

    @asynccontextmanager
    async def telemetry_lifespan(_app: Starlette):
        await telemetry.start()
        try:
            yield
        finally:
            await telemetry.stop()

    product_components = [
        LifespanComponent(
            name="daemon-telemetry",
            lifespan=telemetry_lifespan,
            startup_phase=StartupPhase.TELEMETRY,
            shutdown_phase=ShutdownPhase.FLUSH_TELEMETRY,
        ),
        state_broker.component(),
    ]
    network_policy = None
    credential_sessions = None
    mcp_sessions = None
    if settings is None:
        # Compatibility for callers constructing the historical phase-one application without
        # the typed daemon settings.  Installed ``serve`` always supplies settings and therefore
        # uses the shared authenticated network boundary below.
        product_middleware = [
            Middleware(DaemonTelemetryMiddleware, telemetry=telemetry),
            Middleware(
                LocalReadPolicyMiddleware,
                listener_host=listener_host,
                listener_port=listener_port,
                allowed_origin=allowed_origin,
            ),
        ]
    else:
        from .daemon_auth import (
            BearerAuthMiddleware,
            CredentialAuthority,
            CredentialSessionRegistry,
        )
        from .daemon_network import SecureNetworkAdmissionPolicy

        credential_file = settings.auth.credential_file
        if credential_file is None:  # also guarded structurally at configured startup
            raise ListenerConfigurationError("authenticated daemon requires a credential file")
        authority = CredentialAuthority(
            credential_file,
            audience=settings.auth.audience,
            session_revalidation_seconds=settings.auth.session_revalidation_seconds,
        )
        credential_sessions = CredentialSessionRegistry(authority)

        @asynccontextmanager
        async def credential_session_lifespan(_app: Starlette):
            async with credential_sessions.lifespan():
                yield

        product_components.insert(
            0,
            LifespanComponent(
                name="credential-sessions",
                lifespan=credential_session_lifespan,
                startup_phase=StartupPhase.SECURITY,
                shutdown_phase=ShutdownPhase.CLOSE_SESSIONS,
            ),
        )
        network_policy = SecureNetworkAdmissionPolicy(settings)
        if settings.mcp.mode == "sessionful":
            mcp_sessions = _McpSessionLifecycle(
                credential_sessions=credential_sessions,
                network_policy=network_policy,
                idle_seconds=settings.mcp.session_idle_seconds,
                absolute_seconds=settings.mcp.session_absolute_seconds,
                telemetry=telemetry,
            )
        product_middleware = [
            Middleware(DaemonTelemetryMiddleware, telemetry=telemetry),
            Middleware(BearerAuthMiddleware, authority=authority),
            *(
                [Middleware(_McpSessionLifecycleMiddleware, lifecycle=mcp_sessions)]
                if mcp_sessions is not None
                else []
            ),
            Middleware(
                _McpMethodPassthroughMiddleware,
                fallback_middleware=partial(
                    ReadOnlyMethodMiddleware,
                    allow_activity_publish=True,
                    allow_terminal_unavailable=True,
                ),
            ),
        ]

    app = build_application(
        runtime=runtime,
        routes=operator.routes(),
        components=product_components,
        enable_mcp_http=settings is not None,
        stateless_mcp_http=settings is not None and settings.mcp.mode == "stateless",
        mcp_session_lifecycle=mcp_sessions,
        middleware=product_middleware,
        network_policy=network_policy,
    )
    app.state.operator_sources = sources
    app.state.operator_feed = feed
    app.state.operator_api = operator
    app.state.operator_sse = relay
    app.state.state_broker = state_broker
    app.state.daemon_telemetry = telemetry
    if activity_store is not None and activity_publication is not None:
        app.state.activity_store = activity_store
        app.state.activity_publication = activity_publication
    if network_policy is not None:
        app.state.network_admission = network_policy
        app.state.auth_authority = authority
        app.state.credential_sessions = credential_sessions
        if mcp_sessions is not None:
            app.state.mcp_sessions = mcp_sessions
    return app


def validate_listener(listener_host: str, listener_port: int) -> None:
    """Phase one permits only literal loopback addresses and a concrete TCP port."""
    try:
        address = ipaddress.ip_address(listener_host)
    except ValueError as exc:
        raise ListenerConfigurationError(
            "phase-one host daemon requires a literal loopback address"
        ) from exc
    if not address.is_loopback:
        raise ListenerConfigurationError(
            "phase-one host daemon refuses non-loopback listeners; authentication is not enabled"
        )
    if not 1 <= listener_port <= 65535:
        raise ListenerConfigurationError("listener port must be between 1 and 65535")


def serve(
    *,
    settings: HostDaemonConfig | None = None,
    listener_host: str | None = None,
    listener_port: int | None = None,
    shutdown_budget: float | None = None,
) -> None:
    """Run the configured daemon, acquiring its singleton before Uvicorn can bind."""
    from .daemon_config import HostDaemonConfig, validate_for_listener_startup

    raw_config: dict[str, Any] | None = None
    if settings is None:
        from .config_schema import BeadhiveConfig

        raw_config = config.load()
        settings = BeadhiveConfig.model_validate(raw_config).host.daemon
    if not isinstance(settings, HostDaemonConfig):
        raise TypeError("settings must be a HostDaemonConfig")

    if listener_host is not None or listener_port is not None:
        effective = settings.model_dump()
        if listener_host is not None:
            effective["bind"] = listener_host
        if listener_port is not None:
            effective["port"] = listener_port
        settings = HostDaemonConfig.model_validate(effective)
    validate_for_listener_startup(settings)

    listener_host = settings.bind
    listener_port = settings.port
    shutdown_budget = (
        settings.shutdown.graceful_seconds if shutdown_budget is None else shutdown_budget
    )
    if not 0 < shutdown_budget < float("inf"):
        raise ListenerConfigurationError("shutdown budget must be finite and greater than zero")
    key = DaemonKey.current()
    singleton = DaemonSingleton.acquire(
        key, listener_host=listener_host, listener_port=listener_port
    )
    try:
        # Lazy so importing/running ordinary CLI and stdio paths remains daemon-independent.
        import uvicorn

        runtime = DaemonRuntime(shutdown_budget=shutdown_budget)
        application = build_product_application(
            runtime=runtime,
            control_record=singleton.record,
            listener_host=listener_host,
            listener_port=listener_port,
            allowed_origin=os.environ.get("BH_OPERATOR_UI_ORIGIN") or None,
            cfg=raw_config,
            settings=settings,
        )
        uvicorn_options: dict[str, Any] = {
            "host": listener_host,
            "port": listener_port,
            "log_level": "info",
            "limit_concurrency": settings.http.max_connections,
            "timeout_graceful_shutdown": shutdown_budget,
            # The shared ASGI boundary must see the socket peer before any forwarded identity
            # rewrite.  It validates trusted proxy source + scheme itself.
            "proxy_headers": False,
            "timeout_keep_alive": min(5.0, settings.http.request_timeout_seconds),
            "backlog": min(settings.http.max_connections, 2_048),
            "ws_max_size": settings.terminal.client_queue_bytes,
            "ws_max_queue": min(settings.terminal.max_sessions, 32),
        }
        if settings.tls.enabled:
            uvicorn_options["ssl_certfile"] = str(settings.tls.certificate_file)
            uvicorn_options["ssl_keyfile"] = str(settings.tls.private_key_file)
            uvicorn_options["ssl_context_factory"] = partial(
                _secure_ssl_context, minimum_version=settings.tls.minimum_version
            )
        uvicorn.run(
            application,
            **uvicorn_options,
        )
    finally:
        # Outer lifespan normally completes first.  This idempotent fallback also covers bind,
        # import, startup, and signal failures before/around lifespan entry.
        singleton.release()


def main() -> None:
    """Installed ``bh-host-daemon`` entry point, intentionally independent of Typer/stdio MCP."""
    try:
        serve()
    except (DaemonError, FileNotFoundError, KeyError, ValueError) as exc:
        print(f"\u2717 {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
