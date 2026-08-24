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
import inspect
import ipaddress
import json
import os
import platform
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import config
from . import host as host_identity

CONTRACT_VERSION = "bh.host-daemon/v1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8420
DEFAULT_SHUTDOWN_BUDGET = 10.0


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
        """Drain in documented order, sharing one finite deadline across every callback."""
        self.begin_shutdown()
        deadline = time.monotonic() + self.shutdown_budget
        callbacks = sorted(
            (*self._drain, *extra_callbacks), key=lambda item: (item.phase, item.order)
        )
        results: list[CallbackResult] = []

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

    @classmethod
    def create(cls, key: DaemonKey, *, listener_host: str, listener_port: int) -> ControlRecord:
        return cls(
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
        )

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
        )
        uuid.UUID(record.instance_id)
        return record

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


def _read_control(path: Path) -> tuple[ControlRecord | None, str]:
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("control record root is not an object")
        return ControlRecord.from_dict(value), ""
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

    def __init__(self, *, key: DaemonKey, paths: DaemonPaths, fd: int, record: ControlRecord):
        self.key = key
        self.paths = paths
        self.fd = fd
        self.record = record
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
        os.set_inheritable(fd, False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            record, problem = _read_control(paths.control)
            owner = f"pid {record.pid}, instance {record.instance_id}" if record else problem
            raise AlreadyRunningError(
                f"host daemon singleton is already held for this account/BH_HOME/host_id ({owner})"
            ) from exc

        try:
            record = ControlRecord.create(
                key, listener_host=listener_host, listener_port=listener_port
            )
            _write_control(paths.control, record)
        except Exception:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise
        return cls(key=key, paths=paths, fd=fd, record=record)

    def release(self) -> None:
        """Remove only this incarnation's record, then release its lock; idempotent."""
        if self._released:
            return
        import fcntl

        try:
            current, _problem = _read_control(self.paths.control)
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
        payload = (json.dumps(asdict(record), sort_keys=True, indent=2) + "\n").encode()
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)
    try:
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def daemon_status(key: DaemonKey | None = None) -> DaemonStatus:
    """Verify local singleton ownership and process incarnation without probing the port."""
    expected = key or DaemonKey.current()
    paths = DaemonPaths.for_key(expected)
    record, problem = _read_control(paths.control)
    held = _lock_held(paths.lock)

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
                    {"error": "daemon_draining", "retryable": True}, status_code=503
                )(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _mcp_component(
    *, stateless_http: bool, server_factory: Callable[[], Any]
) -> tuple[LifespanComponent, list[BaseRoute]]:
    server = server_factory()
    mcp_app = server.http_app(
        path="/mcp",
        transport="streamable-http",
        stateless_http=stateless_http,
        json_response=False,
    )
    return (
        LifespanComponent(
            name="fastmcp-http",
            lifespan=mcp_app.lifespan,
            startup_phase=StartupPhase.RESOURCES,
            shutdown_phase=ShutdownPhase.CLOSE_SESSIONS,
        ),
        list(mcp_app.routes),
    )


def build_application(
    *,
    runtime: DaemonRuntime | None = None,
    routes: Sequence[BaseRoute] = (),
    components: Sequence[LifespanComponent] = (),
    enable_mcp_http: bool = False,
    stateless_mcp_http: bool = False,
    mcp_server_factory: Callable[[], Any] | None = None,
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
            stateless_http=stateless_mcp_http, server_factory=mcp_server_factory
        )
        owned_components.append(mcp_component)
        owned_routes.extend(mcp_routes)

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            {"live": True, "ready": daemon_runtime.ready, "contract": CONTRACT_VERSION}
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

    app = Starlette(
        routes=[Route("/health", health, methods=["GET"]), *owned_routes],
        middleware=[Middleware(_DrainGateMiddleware, runtime=daemon_runtime)],
        lifespan=lifespan,
    )
    app.state.daemon_runtime = daemon_runtime
    return app


def build_product_application(*, runtime: DaemonRuntime) -> Starlette:
    """Assemble the installed daemon's current routes on the shared composition seam.

    The first daemon-core slice serves only health.  Operator REST/SSE slices extend this one
    factory with injected routes/components; they do not replace :func:`serve` or create a
    listener.  MCP HTTP remains explicitly disabled here until its authenticated product slice.
    """
    return build_application(runtime=runtime, enable_mcp_http=False)


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
    listener_host: str = DEFAULT_HOST,
    listener_port: int = DEFAULT_PORT,
    shutdown_budget: float = DEFAULT_SHUTDOWN_BUDGET,
) -> None:
    """Run the installed phase-one daemon, acquiring its singleton before Uvicorn can bind."""
    validate_listener(listener_host, listener_port)
    key = DaemonKey.current()
    singleton = DaemonSingleton.acquire(
        key, listener_host=listener_host, listener_port=listener_port
    )
    try:
        # Lazy so importing/running ordinary CLI and stdio paths remains daemon-independent.
        import uvicorn

        runtime = DaemonRuntime(shutdown_budget=shutdown_budget)
        application = build_product_application(runtime=runtime)
        uvicorn.run(
            application,
            host=listener_host,
            port=listener_port,
            log_level="info",
            timeout_graceful_shutdown=shutdown_budget,
        )
    finally:
        # Outer lifespan normally completes first.  This idempotent fallback also covers bind,
        # import, startup, and signal failures before/around lifespan entry.
        singleton.release()
