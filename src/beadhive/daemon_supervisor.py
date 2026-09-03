"""Per-host-daemon lifecycle seam and its single structured status computation.

The host daemon is keyed once by OS account, canonical ``BH_HOME``, and ``host_id``.  This
module is intentionally separate from :mod:`beadhive.dispatch_supervisor`, whose instances are
keyed per hive.  Platform installation backends plug into :class:`SupervisorBackend`; this
slice owns the identity fence, lifecycle vocabulary, diagnostics, and read-only detection.

Status never starts the daemon, Dolt, or an alternate store.  It first verifies the local
singleton/control record and only then accepts an authenticated ``/api/v1/factory`` response
whose stable host and changing service-instance identities both match that record.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from . import config

if TYPE_CHECKING:
    from .host_daemon import DaemonKey, DaemonStatus

LifecycleAction = Literal["install", "start", "stop", "remove"]
Request = Callable[[str, dict[str, str], float], tuple[int, dict[str, Any]]]
PLATFORM_LIFECYCLE_HANDOFF = (
    "platform lifecycle management is owned by bh-q0lol.14; this backend is detect-only"
)
REQUIRED_FACTORY_DEPENDENCIES = frozenset({"hq", "dolt", "bead-state", "run-journals"})
_PRODUCT_ERROR_STATES = {
    "daemon_draining": ("draining", "authenticated daemon is draining"),
    "factory_source_unavailable": (
        "factory-source-unavailable",
        "authenticated factory source is unavailable",
    ),
}


class SupervisorError(RuntimeError):
    """An operator-facing daemon supervisor failure."""


class SupervisorIdentityError(SupervisorError):
    """A backend answered for a different daemon singleton key."""


class SupervisorUnavailableError(SupervisorError):
    """The current environment has no installed lifecycle implementation."""


@dataclass(frozen=True)
class SupervisorState:
    key: DaemonKey
    backend: str
    detectable: bool
    supported: bool
    capability: Literal["manage", "detect-only"]
    handoff: str | None
    installed: bool | None
    running: bool | None
    persisted: bool | None
    detail: str
    install_command: str
    start_command: str
    stop_command: str
    status_command: str
    logs_command: str
    remove_command: str

    def payload(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("key")
        return value


class SupervisorBackend(Protocol):
    """One lifecycle implementation, always addressed by the complete daemon key."""

    name: str

    def install(self, key: DaemonKey) -> SupervisorState: ...

    def start(self, key: DaemonKey) -> SupervisorState: ...

    def stop(self, key: DaemonKey) -> SupervisorState: ...

    def status(self, key: DaemonKey) -> SupervisorState: ...

    def remove(self, key: DaemonKey) -> SupervisorState: ...


def service_name(key: DaemonKey) -> str:
    """Opaque per-key service name; raw homes and host ids never enter manager identifiers."""
    return f"bh-host-daemon-{key.digest}"


def _guidance(key: DaemonKey, backend: str, *, supported: bool) -> dict[str, str]:
    name = service_name(key)
    if backend == "systemd-user":
        unit = f"{name}.service"
        lifecycle = (
            {
                "install": f"bh host daemon install  # installs {unit}",
                "start": f"systemctl --user start {unit}",
                "stop": f"systemctl --user stop {unit}",
                "remove": f"bh host daemon rm  # removes {unit}",
            }
            if supported
            else dict.fromkeys(("install", "start", "stop", "remove"), PLATFORM_LIFECYCLE_HANDOFF)
        )
        return {
            **lifecycle,
            "status": f"systemctl --user status {unit}",
            "logs": f"journalctl --user -u {unit}",
        }
    if backend == "launchagent":
        label = f"dev.beadhive.{name}"
        domain = f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui/<uid>"
        lifecycle = (
            {
                "install": f"bh host daemon install  # installs LaunchAgent {label}",
                "start": f"launchctl kickstart {domain}/{label}",
                "stop": f"launchctl kill SIGTERM {domain}/{label}",
                "remove": f"bh host daemon rm  # removes LaunchAgent {label}",
            }
            if supported
            else dict.fromkeys(("install", "start", "stop", "remove"), PLATFORM_LIFECYCLE_HANDOFF)
        )
        return {
            **lifecycle,
            "status": f"launchctl print {domain}/{label}",
            "logs": "log show --predicate 'process == \"bh-host-daemon\"'",
        }
    if not supported:
        return {
            **dict.fromkeys(("install", "start", "stop", "remove"), PLATFORM_LIFECYCLE_HANDOFF),
            "status": "inspect the configured container/orchestrator service and /health",
            "logs": "read logs from the configured container/orchestrator service",
        }
    return {
        "install": "install the daemon as the container/orchestrator main workload",
        "start": "start the configured container/orchestrator service",
        "stop": "stop the configured container/orchestrator service",
        "status": "inspect the configured container/orchestrator service and /health",
        "logs": "read logs from the configured container/orchestrator service",
        "remove": "remove the configured container/orchestrator service",
    }


def _state(
    key: DaemonKey,
    *,
    backend: str,
    detectable: bool,
    supported: bool,
    installed: bool | None,
    running: bool | None,
    persisted: bool | None,
    detail: str,
) -> SupervisorState:
    commands = _guidance(key, backend, supported=supported)
    return SupervisorState(
        key=key,
        backend=backend,
        detectable=detectable,
        supported=supported,
        capability="manage" if supported else "detect-only",
        handoff=None if supported else PLATFORM_LIFECYCLE_HANDOFF,
        installed=installed,
        running=running,
        persisted=persisted,
        detail=detail,
        install_command=commands["install"],
        start_command=commands["start"],
        stop_command=commands["stop"],
        status_command=commands["status"],
        logs_command=commands["logs"],
        remove_command=commands["remove"],
    )


class DetectOnlySupervisorBackend:
    """Read platform-manager state without claiming to ship its lifecycle implementation.

    The platform backends and their real crash/login/reboot/container evidence are a separate
    release slice.  Detection here makes status and Doctor useful before that work lands while
    refusing to manufacture a process-local fallback.
    """

    def __init__(self, name: str | None = None) -> None:
        self.name = name or _platform_backend_name()

    def _unavailable(self, key: DaemonKey, action: str) -> SupervisorState:
        del action
        state = self.status(key)
        raise SupervisorUnavailableError(
            f"{self.name} supervisor capability is detect-only; {state.handoff}"
        )

    def install(self, key: DaemonKey) -> SupervisorState:
        return self._unavailable(key, "install")

    def start(self, key: DaemonKey) -> SupervisorState:
        return self._unavailable(key, "start")

    def stop(self, key: DaemonKey) -> SupervisorState:
        return self._unavailable(key, "stop")

    def remove(self, key: DaemonKey) -> SupervisorState:
        return self._unavailable(key, "remove")

    def status(self, key: DaemonKey) -> SupervisorState:
        if self.name == "systemd-user" and shutil.which("systemctl"):
            unit = f"{service_name(key)}.service"
            try:
                active = subprocess.run(
                    ["systemctl", "--user", "is-active", unit],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=1.0,
                )
                enabled = subprocess.run(
                    ["systemctl", "--user", "is-enabled", unit],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=1.0,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return _state(
                    key,
                    backend=self.name,
                    detectable=False,
                    supported=False,
                    installed=None,
                    running=None,
                    persisted=None,
                    detail=f"systemd user-manager unavailable: {exc}",
                )
            active_text = active.stdout.strip()
            enabled_text = enabled.stdout.strip()
            if not active_text and not enabled_text and (active.stderr or enabled.stderr):
                detail = (active.stderr or enabled.stderr).strip()
                return _state(
                    key,
                    backend=self.name,
                    detectable=False,
                    supported=False,
                    installed=None,
                    running=None,
                    persisted=None,
                    detail=f"systemd user-manager unavailable: {detail}",
                )
            running = active_text == "active"
            persisted = enabled_text in {"enabled", "enabled-runtime", "static"}
            installed = running or persisted or enabled_text not in {"", "disabled", "not-found"}
            detail = f"is-active={active_text or 'unknown'} is-enabled={enabled_text or 'unknown'}"
            return _state(
                key,
                backend=self.name,
                detectable=True,
                supported=False,
                installed=installed,
                running=running,
                persisted=persisted,
                detail=detail,
            )
        return _state(
            key,
            backend=self.name,
            detectable=False,
            supported=False,
            installed=None,
            running=None,
            persisted=None,
            detail="platform supervisor state is not detectable in this environment",
        )


class RecordingSupervisorBackend:
    """In-memory backend proving the lifecycle seam and identity fence in tests."""

    name = "recording"

    def __init__(self) -> None:
        self._states: dict[str, tuple[DaemonKey, bool, bool, bool]] = {}
        self.calls: list[tuple[str, str]] = []

    def _record(self, action: str, key: DaemonKey) -> tuple[DaemonKey, bool, bool, bool]:
        self.calls.append((action, key.digest))
        return self._states.get(key.digest, (key, False, False, False))

    def _answer(self, key: DaemonKey) -> SupervisorState:
        owned_key, installed, running, persisted = self._states.get(
            key.digest, (key, False, False, False)
        )
        return _state(
            owned_key,
            backend=self.name,
            detectable=True,
            supported=True,
            installed=installed,
            running=running,
            persisted=persisted,
            detail="recording backend",
        )

    def install(self, key: DaemonKey) -> SupervisorState:
        _owned, _installed, running, persisted = self._record("install", key)
        self._states[key.digest] = (key, True, running, persisted)
        return self._answer(key)

    def start(self, key: DaemonKey) -> SupervisorState:
        _owned, installed, _running, persisted = self._record("start", key)
        self._states[key.digest] = (key, installed, installed, persisted)
        return self._answer(key)

    def stop(self, key: DaemonKey) -> SupervisorState:
        _owned, installed, _running, persisted = self._record("stop", key)
        self._states[key.digest] = (key, installed, False, persisted)
        return self._answer(key)

    def status(self, key: DaemonKey) -> SupervisorState:
        self._record("status", key)
        return self._answer(key)

    def remove(self, key: DaemonKey) -> SupervisorState:
        self._record("remove", key)
        self._states.pop(key.digest, None)
        return self._answer(key)


def _platform_backend_name() -> str:
    system = platform.system()
    if system == "Darwin":
        return "launchagent"
    if system == "Linux" and not Path("/.dockerenv").exists():
        return "systemd-user"
    return "container-external"


def get_supervisor_backend() -> SupervisorBackend:
    return DetectOnlySupervisorBackend()


def _require_identity(expected: DaemonKey, state: SupervisorState) -> SupervisorState:
    if state.key != expected:
        raise SupervisorIdentityError(
            "supervisor answered for another BH_HOME or host identity; refusing the target"
        )
    return state


def run_lifecycle(
    action: LifecycleAction,
    *,
    key: DaemonKey | None = None,
    backend: SupervisorBackend | None = None,
) -> SupervisorState:
    """Run one explicit lifecycle verb and fence the backend result to the requested key."""
    from .host_daemon import DaemonKey

    expected = key or DaemonKey.current()
    manager = backend or get_supervisor_backend()
    method = getattr(manager, action)
    return _require_identity(expected, method(expected))


@dataclass(frozen=True)
class ListenerStatus:
    host: str | None
    port: int | None
    reachable: bool | None


@dataclass(frozen=True)
class IdentityStatus:
    expected_host_id: str
    reported_host_id: str | None
    expected_instance_id: str | None
    reported_instance_id: str | None
    verified: bool


@dataclass(frozen=True)
class DependencyStatus:
    name: str
    status: str
    reason_code: str | None


@dataclass(frozen=True)
class ReadinessStatus:
    state: str
    authenticated: bool
    accepting_work: bool | None
    dependencies: tuple[DependencyStatus, ...]
    detail: str
    reason_code: str | None = None
    retryable: bool | None = None


@dataclass(frozen=True)
class DaemonServiceStatus:
    state: str
    key: DaemonKey
    local: DaemonStatus
    supervisor: SupervisorState
    listener: ListenerStatus
    identity: IdentityStatus
    readiness: ReadinessStatus

    @property
    def healthy(self) -> bool:
        dependency_names = {item.name for item in self.readiness.dependencies}
        return (
            self.local.verified
            and self.identity.verified
            and self.readiness.authenticated
            and self.readiness.state == "ready"
            and self.readiness.accepting_work is True
            and len(self.readiness.dependencies) == len(REQUIRED_FACTORY_DEPENDENCIES)
            and dependency_names == REQUIRED_FACTORY_DEPENDENCIES
            and all(item.status == "ready" for item in self.readiness.dependencies)
        )

    def payload(self) -> dict[str, Any]:
        from .host_daemon import DaemonPaths

        paths = DaemonPaths.for_key(self.key)
        record = self.local.record
        return {
            "state": self.state,
            "healthy": self.healthy,
            "key": asdict(self.key),
            "lock": {"path": str(paths.lock), "held": self.local.running},
            "control_record": {
                "path": str(paths.control),
                "present": record is not None,
                "verified": self.local.verified,
                "detail": self.local.detail,
            },
            "identity": asdict(self.identity),
            "listener": asdict(self.listener),
            "supervisor": self.supervisor.payload(),
            "readiness": {
                **asdict(self.readiness),
                "dependencies": [asdict(item) for item in self.readiness.dependencies],
            },
            "guidance": {
                "install": self.supervisor.install_command,
                "start": self.supervisor.start_command,
                "stop": self.supervisor.stop_command,
                "status": f"bh host daemon status; {self.supervisor.status_command}",
                "logs": self.supervisor.logs_command,
                "remove": self.supervisor.remove_command,
                "control": f"verified control record: {paths.control}",
            },
        }


def _http_request(url: str, headers: dict[str, str], timeout: float) -> tuple[int, dict[str, Any]]:
    import httpx

    try:
        with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False) as client:
            response = client.get(url, headers=headers)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return response.status_code, payload if isinstance(payload, dict) else {}
    except httpx.HTTPError as exc:
        raise ConnectionError(str(exc)) from exc


def _product_error(payload: dict[str, Any]) -> tuple[str, bool] | None:
    """Parse product error codes absent from the narrower shared enum without reflecting data."""
    error = payload.get("error")
    if payload.get("schemaVersion") != 1 or not isinstance(error, dict):
        return None
    if set(error) != {"code", "message", "retryable", "action", "requestId"}:
        return None
    code = error.get("code")
    retryable = error.get("retryable")
    action = error.get("action")
    request_id = error.get("requestId")
    if (
        not isinstance(code, str)
        or code not in _PRODUCT_ERROR_STATES
        or not isinstance(error.get("message"), str)
        or not isinstance(retryable, bool)
        or action not in {None, "retry", "resnapshot", "reauthenticate"}
        or (request_id is not None and not isinstance(request_id, str))
    ):
        return None
    return code, retryable


def _configured_bearer() -> str | None:
    path = os.environ.get("BH_HOST_DAEMON_BEARER_FILE")
    if path:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None
    return os.environ.get("BH_HOST_DAEMON_BEARER") or None


def _listener_scheme() -> str:
    """Use direct TLS when configured; proxy TLS still terminates outside this listener."""
    try:
        raw = config.load()
        host = raw.get("host", {})
        daemon = host.get("daemon", {}) if isinstance(host, dict) else {}
        tls = daemon.get("tls", {}) if isinstance(daemon, dict) else {}
    except (AttributeError, FileNotFoundError, TypeError):
        return "http"
    return "https" if isinstance(tls, dict) and tls.get("enabled") is True else "http"


def daemon_service_status(
    *,
    key: DaemonKey | None = None,
    backend: SupervisorBackend | None = None,
    bearer: str | None = None,
    timeout: float = 2.0,
    request: Request = _http_request,
) -> DaemonServiceStatus:
    """Return the one status object shared by CLI, Doctor, setup, and hive readiness."""
    from . import host_daemon
    from .daemon_contract import (
        CONTRACT_VERSION,
        WIRE_SCHEMA_VERSION,
        ErrorResponse,
        FactoryResponse,
    )

    expected = key or host_daemon.DaemonKey.current()
    manager = backend or get_supervisor_backend()
    supervisor = _require_identity(expected, manager.status(expected))
    local = host_daemon.daemon_status(expected)
    record = local.record
    listener = ListenerStatus(
        record.listener_host if record else None,
        record.listener_port if record else None,
        False if not local.running else None,
    )
    identity = IdentityStatus(
        expected.host_id,
        None,
        record.instance_id if record else None,
        None,
        False,
    )

    if not local.verified or record is None:
        readiness = ReadinessStatus("unavailable", False, None, (), local.detail)
        return DaemonServiceStatus(
            local.state, expected, local, supervisor, listener, identity, readiness
        )

    credential = bearer if bearer is not None else _configured_bearer()
    if not credential:
        readiness = ReadinessStatus(
            "unknown",
            False,
            None,
            (),
            "authenticated readiness unavailable: set BH_HOST_DAEMON_BEARER_FILE to an "
            "operator:read credential",
        )
        return DaemonServiceStatus(
            "running-unverified", expected, local, supervisor, listener, identity, readiness
        )

    url_host = f"[{record.listener_host}]" if ":" in record.listener_host else record.listener_host
    url = f"{_listener_scheme()}://{url_host}:{record.listener_port}/api/v1/factory"
    try:
        status_code, payload = request(
            url,
            {"Authorization": f"Bearer {credential}"},
            timeout,
        )
    except (ConnectionError, OSError, TimeoutError) as exc:
        readiness = ReadinessStatus("unavailable", False, None, (), f"listener unreachable: {exc}")
        return DaemonServiceStatus(
            "listener-unreachable", expected, local, supervisor, listener, identity, readiness
        )

    listener = ListenerStatus(record.listener_host, record.listener_port, True)
    if status_code != 200:
        reason_code = f"http_{status_code}"
        retryable = status_code == 429 or status_code >= 500
        try:
            if payload.get("schemaVersion") == WIRE_SCHEMA_VERSION:
                error = ErrorResponse.model_validate_json(json.dumps(payload), strict=True)
                reason_code = error.error.code.value
                retryable = error.error.retryable
        except (TypeError, ValueError):
            pass
        product_error = _product_error(payload)
        if product_error is not None:
            reason_code, retryable = product_error
        if status_code in {401, 403}:
            state = "authentication-failed"
            authenticated = False
            detail = f"daemon readiness credential was refused with HTTP {status_code}"
        elif status_code == 503 and reason_code in _PRODUCT_ERROR_STATES:
            state, detail = _PRODUCT_ERROR_STATES[reason_code]
            authenticated = True
        elif status_code == 503:
            state = "readiness-unavailable"
            authenticated = True
            detail = "authenticated daemon readiness is temporarily unavailable"
        elif status_code == 429:
            state = "readiness-rate-limited"
            authenticated = True
            detail = "authenticated daemon readiness probe was rate limited"
        elif status_code >= 500:
            state = "readiness-service-error"
            authenticated = True
            detail = f"authenticated daemon readiness failed with HTTP {status_code}"
        else:
            state = "readiness-refused"
            authenticated = True
            detail = f"authenticated daemon readiness was refused with HTTP {status_code}"
        readiness = ReadinessStatus(
            "unavailable",
            authenticated,
            None,
            (),
            detail,
            reason_code,
            retryable,
        )
        return DaemonServiceStatus(
            state, expected, local, supervisor, listener, identity, readiness
        )

    if payload.get("schemaVersion") != WIRE_SCHEMA_VERSION:
        readiness = ReadinessStatus(
            "unavailable",
            False,
            None,
            (),
            "authenticated factory response failed contract validation",
            "factory_contract_invalid",
            False,
        )
        return DaemonServiceStatus(
            "contract-invalid", expected, local, supervisor, listener, identity, readiness
        )
    if payload.get("contractVersion") != CONTRACT_VERSION:
        readiness = ReadinessStatus(
            "unavailable",
            False,
            None,
            (),
            "authenticated factory contract version is unsupported",
            "factory_contract_mismatch",
            False,
        )
        return DaemonServiceStatus(
            "contract-mismatch", expected, local, supervisor, listener, identity, readiness
        )
    try:
        factory = FactoryResponse.model_validate_json(json.dumps(payload), strict=True)
    except (TypeError, ValueError):
        readiness = ReadinessStatus(
            "unavailable",
            False,
            None,
            (),
            "authenticated factory response failed contract validation",
            "factory_contract_invalid",
            False,
        )
        return DaemonServiceStatus(
            "contract-invalid", expected, local, supervisor, listener, identity, readiness
        )

    reported_host = factory.host.host_id
    reported_instance = factory.host.service_instance_id
    identity = IdentityStatus(
        expected.host_id,
        reported_host,
        record.instance_id,
        reported_instance,
        reported_host == expected.host_id and reported_instance == record.instance_id,
    )
    if not identity.verified:
        readiness = ReadinessStatus(
            "unavailable",
            False,
            None,
            (),
            "authenticated factory identity does not match the control record",
        )
        return DaemonServiceStatus(
            "wrong-identity", expected, local, supervisor, listener, identity, readiness
        )

    dependencies = tuple(
        DependencyStatus(
            name=item.name,
            status=item.status.value,
            reason_code=item.reason_code,
        )
        for item in factory.status.dependencies
    )
    dependency_names = {item.name for item in dependencies}
    if (
        len(dependencies) != len(REQUIRED_FACTORY_DEPENDENCIES)
        or dependency_names != REQUIRED_FACTORY_DEPENDENCIES
    ):
        readiness = ReadinessStatus(
            factory.status.readiness.value,
            True,
            factory.status.accepting_work,
            dependencies,
            "authenticated factory dependency set failed contract validation",
            "factory_dependency_set_invalid",
            False,
        )
        return DaemonServiceStatus(
            "contract-invalid", expected, local, supervisor, listener, identity, readiness
        )
    readiness_state = factory.status.readiness.value
    accepting = factory.status.accepting_work
    if readiness_state == "ready" and any(item.status != "ready" for item in dependencies):
        readiness = ReadinessStatus(
            readiness_state,
            True,
            accepting,
            dependencies,
            "authenticated factory readiness contradicts dependency state",
            "factory_readiness_inconsistent",
            False,
        )
        return DaemonServiceStatus(
            "contract-invalid", expected, local, supervisor, listener, identity, readiness
        )
    readiness = ReadinessStatus(
        readiness_state,
        True,
        accepting,
        dependencies,
        "authenticated factory identity and dependency readiness verified",
        None,
        False,
    )
    if readiness_state == "ready":
        state = "healthy" if accepting else "draining"
    else:
        state = readiness_state
    return DaemonServiceStatus(
        state,
        expected,
        local,
        supervisor,
        listener,
        identity,
        readiness,
    )


def configured(cfg: dict[str, Any] | None = None) -> bool:
    """Whether daemon use was explicitly requested, even when its config is malformed."""
    try:
        raw = config.load() if cfg is None else cfg
        host = raw.get("host", {})
        daemon = host.get("daemon", {}) if isinstance(host, dict) else {}
        return isinstance(daemon, dict) and daemon.get("enabled") is True
    except (AttributeError, FileNotFoundError, TypeError):
        return False


def setup_advisories(cfg: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Structural and moving readiness facts, both advisory to the core setup gate."""
    try:
        raw = config.load() if cfg is None else cfg
    except FileNotFoundError:
        return []
    if not configured(raw):
        return []
    try:
        from .config_schema import BeadhiveConfig
        from .daemon_config import validate_for_listener_startup

        settings = BeadhiveConfig.model_validate(raw).host.daemon
        validate_for_listener_startup(settings)
    except (OSError, TypeError, ValueError) as exc:
        return [
            {
                "id": "host-daemon-structural",
                "category": "structural",
                "message": f"⚠ host daemon structural configuration is incomplete: {exc}",
            }
        ]
    key: DaemonKey
    try:
        from .host_daemon import DaemonKey

        key = DaemonKey.current()
    except (FileNotFoundError, KeyError, ValueError) as exc:
        return [
            {
                "id": "host-daemon-structural",
                "category": "structural",
                "message": f"⚠ host daemon structural configuration is incomplete: {exc}",
            }
        ]
    backend = get_supervisor_backend()
    state = _require_identity(key, backend.status(key))
    binary = shutil.which("bh-host-daemon")
    if binary is None or not state.supported:
        gaps = []
        if binary is None:
            gaps.append("installed bh-host-daemon command not found")
        if not state.supported:
            gaps.append(f"{state.backend} supervisor capability is detect-only")
        return [
            {
                "id": "host-daemon-structural",
                "category": "structural",
                "message": (
                    f"⚠ host daemon structural gap: {', '.join(gaps)}. {state.install_command}"
                ),
            }
        ]
    status = daemon_service_status(key=key, backend=backend)
    if status.healthy:
        return []
    return [
        {
            "id": "host-daemon-readiness",
            "category": "readiness",
            "message": (
                f"⚠ configured host daemon is not ready: {status.readiness.detail}. "
                f"{status.supervisor.start_command}"
            ),
        }
    ]
