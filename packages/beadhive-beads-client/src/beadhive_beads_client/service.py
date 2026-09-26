"""One supervised loopback ``bd serve`` per workspace: owner, endpoint record, and resolver.

This is the "optional supervision of a local ``bd serve`` process" the API-first ADR leaves to
the session layer, split so that *either* lifecycle owner can satisfy one contract:

* an **owner** (:func:`start` for a detached service, :class:`ServiceSupervisor` for a
  supervised one) spawns exactly one ``bd serve`` bound to ``127.0.0.1`` and one workspace,
  proves readiness through the same context negotiation every client performs, and publishes an
  :class:`EndpointRecord` atomically;
* a **consumer** calls :func:`resolve`, which reads that record, re-verifies the live service's
  project and database, and returns a loopback :class:`~beadhive_beads_client.RemoteEndpoint`.
  It never spawns ``bd serve``; absent, stale, or mismatched services fail closed with
  :class:`ServiceUnavailable`, whose message names the command that starts one.

The bearer token lives only in a least-privilege token file (``0600`` inside a ``0700``
directory, owned by the service account). The endpoint record carries the token file's *path*,
never the token, and nothing here logs the token.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import secrets
import signal
import socket
import stat
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx

from beads_v1_3.models import ContextResponse

from .session import (
    _BASE_CAPABILITIES,
    BeadsSession,
    ExpectedContext,
    IncompatibleService,
    RemoteEndpoint,
    ServiceProblem,
)

RECORD_SCHEMA = 1
LOOPBACK = "127.0.0.1"

#: Who keeps the service alive. ``none`` is a detached one-shot start; ``foreground`` is a
#: supervisor process in the foreground (a developer terminal, or a service manager running it
#: as its main process); ``host-daemon`` is a supervisor inside the Beadhive host daemon.
SupervisorKind = Literal["none", "foreground", "host-daemon"]
ServiceState = Literal["absent", "stale", "mismatch", "unready", "running"]

_SPAWN_ATTEMPTS = 3


class ServiceError(RuntimeError):
    """Base class for supervised-service failures."""


class ServiceUnavailable(ServiceError):
    """No verified service answers for this workspace. The message names the start command."""

    def __init__(self, reason: str, *, state: ServiceState, start_command: str) -> None:
        self.reason = reason
        self.state = state
        self.start_command = start_command
        super().__init__(f"{reason}; start it with `{start_command}`")


class ServiceConflict(ServiceError):
    """A live process occupies this workspace's slot but cannot be proven to be its service."""


class ServiceOwned(ServiceError):
    """The service belongs to a supervisor that must be asked to stop it."""

    def __init__(self, record: EndpointRecord) -> None:
        self.record = record
        super().__init__(
            f"bd serve pid {record.pid} is supervised by {record.supervisor} "
            f"(pid {record.supervisor_pid})"
        )


class ServiceStartFailed(ServiceError):
    """``bd serve`` exited or never became ready within its startup deadline."""


@dataclass(frozen=True)
class ServicePaths:
    """The per-workspace runtime directory an owner publishes into."""

    directory: Path

    @property
    def record(self) -> Path:
        return self.directory / "endpoint.json"

    @property
    def token(self) -> Path:
        return self.directory / "token"

    @property
    def lock(self) -> Path:
        return self.directory / "serve.lock"

    @property
    def log(self) -> Path:
        return self.directory / "serve.log"


@dataclass(frozen=True)
class ServiceSpec:
    """Everything an owner needs to run, and a consumer needs to verify, one workspace service.

    ``workspace`` is the stable identity the service is bound to (the hive key); a record for a
    different workspace, project, or database is a mismatch, never a fallback.
    """

    workspace: str
    repo_root: Path
    project_id: str
    database: str
    bd_executable: Path
    paths: ServicePaths
    start_command: str
    startup_seconds: float = 30.0
    stop_seconds: float = 22.0
    probe_seconds: float = 5.0

    def expected(self, capabilities: frozenset[str] | None = None) -> ExpectedContext:
        return ExpectedContext(
            self.project_id,
            self.database,
            required_capabilities=capabilities or _BASE_CAPABILITIES,
        )


@dataclass(frozen=True)
class EndpointRecord:
    """The published, token-free description of one running service."""

    schema: int
    workspace: str
    address: str
    port: int
    token_file: str
    project_id: str
    database: str
    repo_root: str
    bd_executable: str
    bd_version: str
    pid: int
    process_start: str
    supervisor: str
    supervisor_pid: int | None
    started_at: str
    state: str = "running"
    stale_reason: str = ""

    @property
    def url(self) -> str:
        return f"http://{self.address}:{self.port}"

    def payload(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: object) -> EndpointRecord:
        if not isinstance(value, dict):
            raise ValueError("endpoint record root is not an object")
        if value.get("schema") != RECORD_SCHEMA:
            raise ValueError(f"unsupported endpoint record schema {value.get('schema')!r}")
        supervisor_pid = value.get("supervisor_pid")
        record = cls(
            schema=RECORD_SCHEMA,
            workspace=str(value["workspace"]),
            address=str(value["address"]),
            port=int(value["port"]),
            token_file=str(value["token_file"]),
            project_id=str(value["project_id"]),
            database=str(value["database"]),
            repo_root=str(value["repo_root"]),
            bd_executable=str(value["bd_executable"]),
            bd_version=str(value["bd_version"]),
            pid=int(value["pid"]),
            process_start=str(value["process_start"]),
            supervisor=str(value["supervisor"]),
            supervisor_pid=None if supervisor_pid is None else int(supervisor_pid),
            started_at=str(value["started_at"]),
            state=str(value.get("state", "running")),
            stale_reason=str(value.get("stale_reason", "")),
        )
        if record.address != LOOPBACK:
            raise ValueError("endpoint record is not bound to loopback")
        if not 1 <= record.port <= 65535:
            raise ValueError("endpoint record port is out of range")
        return record


@dataclass(frozen=True)
class ServiceStatus:
    state: ServiceState
    detail: str
    record: EndpointRecord | None = None

    def payload(self) -> dict[str, object]:
        return {
            "state": self.state,
            "detail": self.detail,
            "record": self.record.payload() if self.record is not None else None,
        }


@dataclass
class StartOutcome:
    """``started`` is false for the idempotent case; ``process`` is set only for own spawns."""

    record: EndpointRecord
    started: bool
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    readiness_seconds: float = 0.0


@dataclass(frozen=True)
class StopOutcome:
    state_before: ServiceState
    pid: int | None
    signalled: bool
    detail: str


# ---- process identity ------------------------------------------------------------------------


def process_start_token(pid: int) -> str:
    """A PID-reuse fence: the kernel's start time for ``pid``, or ``"unavailable"``."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return f"linux:{raw[raw.rfind(')') + 2 :].split()[19]}"
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
        except (OSError, subprocess.SubprocessError):
            return "unavailable"
        if result.returncode == 0 and result.stdout.strip():
            return f"ps:{result.stdout.strip()}"
    return "unavailable"


def _process_state(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return ""
    return raw[raw.rfind(")") + 2 :].split()[0]


def process_alive(pid: int | None, start_token: str = "") -> bool:
    """Whether ``pid`` is a live (non-zombie) process of the recorded incarnation."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    if _process_state(pid) == "Z":
        return False
    return not start_token or process_start_token(pid) == start_token


# ---- runtime files ---------------------------------------------------------------------------


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(0o700)


def ensure_token(paths: ServicePaths) -> Path:
    """Create (once) or validate the service-owned bearer token file; return its path.

    The token is minted with :mod:`secrets` and written ``O_EXCL`` at ``0600``. An existing file
    must be a regular file owned by this account and closed to group/other, or it is refused.
    """
    _private_directory(paths.directory)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(paths.token, flags, 0o600)
    except FileExistsError:
        info = paths.token.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError(f"{paths.token} is not a regular file") from None
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise PermissionError(f"{paths.token} is not owned by this account") from None
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError(
                f"{paths.token} is readable by group or others; chmod 600 it"
            ) from None
        return paths.token
    try:
        os.write(fd, (secrets.token_urlsafe(32) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return paths.token


def read_token(path: Path) -> str:
    token = next((line.strip() for line in path.read_text().splitlines() if line.strip()), "")
    if not token:
        raise ValueError(f"{path} holds no token")
    return token


def load_record(paths: ServicePaths) -> tuple[EndpointRecord | None, str]:
    """``(record, "")``, ``(None, "missing")``, or ``(None, <why it is unreadable>)``."""
    try:
        return EndpointRecord.from_payload(json.loads(paths.record.read_text())), ""
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return None, f"unreadable endpoint record ({exc})"


def write_record(paths: ServicePaths, record: EndpointRecord) -> None:
    """Publish ``record`` atomically: private temp file, fsync, rename, directory fsync."""
    _private_directory(paths.directory)
    temporary = paths.directory / f".{paths.record.name}.{os.getpid()}.{secrets.token_hex(4)}"
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        try:
            os.write(fd, (json.dumps(record.payload(), sort_keys=True, indent=2) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, paths.record)
    finally:
        temporary.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        directory = os.open(paths.directory, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def mark_stale(paths: ServicePaths, *, pid: int, reason: str) -> None:
    """Rewrite the record as stale, but only while it still describes ``pid``."""
    record, _problem = load_record(paths)
    if record is not None and record.pid == pid and record.state != "stale":
        write_record(paths, replace(record, state="stale", stale_reason=reason))


def remove_record(paths: ServicePaths, *, pid: int) -> None:
    """Remove the record, but only while it still describes ``pid``."""
    record, _problem = load_record(paths)
    if record is not None and record.pid == pid:
        paths.record.unlink(missing_ok=True)


@contextlib.contextmanager
def _slot_lock(paths: ServicePaths, deadline_seconds: float) -> Iterator[None]:
    """Serialize owners of one workspace slot; bounded, never an indefinite wait."""
    import fcntl

    _private_directory(paths.directory)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(paths.lock, flags, 0o600)
    deadline = time.monotonic() + deadline_seconds
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ServiceConflict(
                        f"another owner holds {paths.lock}; retry once it finishes"
                    ) from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


# ---- verification ----------------------------------------------------------------------------


def _loopback_endpoint(record: EndpointRecord, timeout: float) -> RemoteEndpoint:
    if record.address != LOOPBACK:
        raise IncompatibleService("endpoint record is not bound to loopback")
    return RemoteEndpoint(record.url, read_token(Path(record.token_file)), timeout)


def probe(
    record: EndpointRecord,
    spec: ServiceSpec,
    capabilities: frozenset[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ContextResponse:
    """Negotiate once against the recorded endpoint: health, auth, context identity, readiness."""
    endpoint = _loopback_endpoint(record, spec.probe_seconds)
    with BeadsSession(endpoint, spec.expected(capabilities), transport=transport) as session:
        assert session.context is not None
        return session.context


def _retryable(exc: IncompatibleService) -> bool:
    """A startup probe failure that more waiting can cure (not a contract/identity mismatch)."""
    return isinstance(exc.__cause__, httpx.TransportError | ServiceProblem)


def _identity_mismatch(record: EndpointRecord, spec: ServiceSpec) -> str:
    for label, recorded, expected in (
        ("workspace", record.workspace, spec.workspace),
        ("project id", record.project_id, spec.project_id),
        ("database", record.database, spec.database),
        ("repository", record.repo_root, str(spec.repo_root)),
    ):
        if recorded != expected:
            return f"recorded {label} {recorded!r} differs from this hive's {expected!r}"
    return ""


def status(
    spec: ServiceSpec,
    capabilities: frozenset[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ServiceStatus:
    """Classify the workspace slot: absent / stale / mismatch / unready / running (verified)."""
    record, problem = load_record(spec.paths)
    if record is None:
        if problem == "missing":
            return ServiceStatus("absent", "no endpoint record is published")
        return ServiceStatus("stale", problem)
    if record.state == "stale":
        return ServiceStatus("stale", record.stale_reason or "record is marked stale", record)
    if not process_alive(record.pid, record.process_start):
        return ServiceStatus("stale", f"recorded bd serve pid {record.pid} is gone", record)
    mismatch = _identity_mismatch(record, spec)
    if mismatch:
        return ServiceStatus("mismatch", mismatch, record)
    try:
        probe(record, spec, capabilities, transport=transport)
    except IncompatibleService as exc:
        if _retryable(exc):
            return ServiceStatus("unready", str(exc), record)
        return ServiceStatus("mismatch", str(exc), record)
    except (OSError, ValueError) as exc:
        return ServiceStatus("unready", f"token file unusable: {exc}", record)
    return ServiceStatus("running", f"bd serve pid {record.pid} verified at {record.url}", record)


def resolve(
    spec: ServiceSpec,
    capabilities: frozenset[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> RemoteEndpoint:
    """Return a context-verified loopback endpoint for ``spec``'s workspace, or fail closed.

    Never spawns ``bd serve``. The caller opens its own
    :class:`~beadhive_beads_client.BeadsSession` over the returned endpoint (which re-verifies
    its own required capabilities).
    """
    current = status(spec, capabilities, transport=transport)
    if current.state != "running" or current.record is None:
        raise ServiceUnavailable(
            f"no verified bd serve for {spec.workspace} ({current.state}: {current.detail})",
            state=current.state,
            start_command=spec.start_command,
        )
    return _loopback_endpoint(current.record, spec.probe_seconds)


# ---- ownership -------------------------------------------------------------------------------


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind((LOOPBACK, 0))
        return int(candidate.getsockname()[1])


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _spawn(spec: ServiceSpec, token_file: Path, port: int) -> subprocess.Popen[bytes]:
    argv = [
        str(spec.bd_executable),
        "-C",
        str(spec.repo_root),
        "serve",
        "--addr",
        f"{LOOPBACK}:{port}",
        "--auth-token-file",
        str(token_file),
    ]
    log_fd = os.open(spec.paths.log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            close_fds=True,
            start_new_session=True,
        )
    finally:
        os.close(log_fd)


def _terminate(process: subprocess.Popen[bytes], seconds: float) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _await_ready(
    spec: ServiceSpec,
    record: EndpointRecord,
    process: subprocess.Popen[bytes],
    deadline: float,
    transport: httpx.BaseTransport | None,
) -> str:
    """The negotiated Beads version once ready; ``""`` if the process exited (lost a port race)."""
    while True:
        if process.poll() is not None:
            return ""
        try:
            return probe(record, spec, transport=transport).bd_version
        except IncompatibleService as exc:
            if not _retryable(exc):
                raise
            if time.monotonic() >= deadline:
                raise ServiceStartFailed(
                    f"bd serve pid {process.pid} did not become ready within "
                    f"{spec.startup_seconds:g}s (see {spec.paths.log})"
                ) from exc
        time.sleep(0.05)


def start(
    spec: ServiceSpec,
    *,
    supervisor: SupervisorKind = "none",
    supervisor_pid: int | None = None,
    transport: httpx.BaseTransport | None = None,
) -> StartOutcome:
    """Idempotently ensure one verified service for ``spec`` and publish its record.

    A verified running service is returned untouched (``started=False``). A dead or stale record
    is replaced. A live process that cannot be verified as this workspace's service is never
    duplicated: that raises :class:`ServiceConflict`.
    """
    with _slot_lock(spec.paths, spec.startup_seconds + spec.stop_seconds):
        current = status(spec, transport=transport)
        if current.state == "running" and current.record is not None:
            return StartOutcome(current.record, started=False)
        if current.record is not None and process_alive(
            current.record.pid, current.record.process_start
        ):
            raise ServiceConflict(
                f"bd serve pid {current.record.pid} is alive but {current.state}: {current.detail}"
            )
        spec.paths.record.unlink(missing_ok=True)
        token_file = ensure_token(spec.paths)
        began = time.monotonic()
        deadline = began + spec.startup_seconds
        for _attempt in range(_SPAWN_ATTEMPTS):
            port = _free_loopback_port()
            process = _spawn(spec, token_file, port)
            record = EndpointRecord(
                schema=RECORD_SCHEMA,
                workspace=spec.workspace,
                address=LOOPBACK,
                port=port,
                token_file=str(token_file),
                project_id=spec.project_id,
                database=spec.database,
                repo_root=str(spec.repo_root),
                bd_executable=str(spec.bd_executable),
                bd_version="",
                pid=process.pid,
                process_start=process_start_token(process.pid),
                supervisor=supervisor,
                supervisor_pid=supervisor_pid,
                started_at=_now(),
            )
            try:
                version = _await_ready(spec, record, process, deadline, transport)
            except BaseException:
                _terminate(process, spec.stop_seconds)
                raise
            if version:
                record = replace(record, bd_version=version)
                write_record(spec.paths, record)
                return StartOutcome(
                    record,
                    started=True,
                    process=process,
                    readiness_seconds=time.monotonic() - began,
                )
            if time.monotonic() >= deadline:
                break
        raise ServiceStartFailed(
            f"bd serve for {spec.workspace} exited during startup "
            f"(status {process.returncode}; see {spec.paths.log})"
        )


def _wait_gone(pid: int, start_token: str, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while process_alive(pid, start_token):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def stop(spec: ServiceSpec) -> StopOutcome:
    """Stop the workspace's service with ``SIGTERM`` (``SIGKILL`` after its budget); idempotent.

    A foreground supervisor is signalled instead of its child so it does not restart it. A
    host-daemon-supervised service raises :class:`ServiceOwned`; its owner must release it.
    """
    with _slot_lock(spec.paths, spec.stop_seconds * 2 + spec.startup_seconds):
        record, problem = load_record(spec.paths)
        if record is None:
            if problem != "missing":
                spec.paths.record.unlink(missing_ok=True)
                return StopOutcome("stale", None, False, f"removed {problem}")
            return StopOutcome("absent", None, False, "no bd serve is published")
        if not process_alive(record.pid, record.process_start):
            spec.paths.record.unlink(missing_ok=True)
            return StopOutcome("stale", record.pid, False, "removed a stale endpoint record")
        before: ServiceState = "running" if record.state == "running" else "stale"
        target = record.pid
        if record.supervisor != "none" and process_alive(record.supervisor_pid):
            if record.supervisor != "foreground":
                raise ServiceOwned(record)
            target = int(record.supervisor_pid or 0)
        os.kill(target, signal.SIGTERM)
        if not _wait_gone(record.pid, record.process_start, spec.stop_seconds):
            with contextlib.suppress(ProcessLookupError):
                os.kill(record.pid, signal.SIGKILL)
            _wait_gone(record.pid, record.process_start, 5.0)
        remove_record(spec.paths, pid=record.pid)
        return StopOutcome(before, record.pid, True, f"stopped bd serve pid {record.pid}")


# ---- supervision -----------------------------------------------------------------------------


@dataclass(frozen=True)
class SupervisorSnapshot:
    state: Literal["starting", "running", "backoff", "failed", "stopped"]
    failures: int
    detail: str
    record: EndpointRecord | None = None


class ServiceSupervisor:
    """Keep one workspace service alive with bounded exponential backoff.

    Call :meth:`tick` periodically (it blocks only while a start is in progress) and
    :meth:`shutdown` once. After ``max_failures`` consecutive failed starts or crashes the
    supervisor gives up and leaves the record stale; a service that stays up for
    ``healthy_reset_seconds`` clears the failure count. A verified service some other owner
    already published is adopted for monitoring, never duplicated.
    """

    def __init__(
        self,
        spec: ServiceSpec,
        *,
        kind: SupervisorKind,
        backoff_initial: float = 0.5,
        backoff_max: float = 30.0,
        max_failures: int = 5,
        healthy_reset_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.spec = spec
        self.kind = kind
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.max_failures = max_failures
        self.healthy_reset_seconds = healthy_reset_seconds
        self._clock = clock
        self._transport = transport
        self._process: subprocess.Popen[bytes] | None = None
        self._record: EndpointRecord | None = None
        self._since = 0.0
        self._retry_at = 0.0
        self.failures = 0
        self.snapshot = SupervisorSnapshot("starting", 0, "not started")

    def _fail(self, detail: str) -> SupervisorSnapshot:
        self.failures += 1
        if self.failures >= self.max_failures:
            if self._record is not None:
                mark_stale(self.spec.paths, pid=self._record.pid, reason="restart budget exhausted")
            self.snapshot = SupervisorSnapshot(
                "failed", self.failures, f"gave up after {self.failures} failures: {detail}"
            )
            return self.snapshot
        delay = min(self.backoff_max, self.backoff_initial * 2 ** (self.failures - 1))
        self._retry_at = self._clock() + delay
        self.snapshot = SupervisorSnapshot(
            "backoff", self.failures, f"{detail}; retrying in {delay:g}s"
        )
        return self.snapshot

    def _alive(self) -> bool:
        if self._process is not None:
            return self._process.poll() is None
        return self._record is not None and process_alive(
            self._record.pid, self._record.process_start
        )

    def tick(self) -> SupervisorSnapshot:
        if self.snapshot.state in {"failed", "stopped"}:
            return self.snapshot
        if self._record is not None:
            if self._alive():
                if self.failures and self._clock() - self._since >= self.healthy_reset_seconds:
                    self.failures = 0
                self.snapshot = SupervisorSnapshot(
                    "running", self.failures, f"bd serve pid {self._record.pid}", self._record
                )
                return self.snapshot
            crashed = self._record
            code = self._process.returncode if self._process is not None else None
            self._process, self._record = None, None
            mark_stale(self.spec.paths, pid=crashed.pid, reason=f"bd serve exited ({code})")
            return self._fail(f"bd serve pid {crashed.pid} exited ({code})")
        if self._clock() < self._retry_at:
            return self.snapshot
        try:
            outcome = start(
                self.spec,
                supervisor=self.kind,
                supervisor_pid=os.getpid(),
                transport=self._transport,
            )
        except ServiceError as exc:
            return self._fail(str(exc))
        self._process, self._record = outcome.process, outcome.record
        self._since = self._clock()
        self.snapshot = SupervisorSnapshot(
            "running", self.failures, f"bd serve pid {outcome.record.pid}", outcome.record
        )
        return self.snapshot

    def shutdown(self) -> None:
        """SIGTERM an own child and retract its record. An adopted service is left running."""
        process, record = self._process, self._record
        self._process, self._record = None, None
        self.snapshot = SupervisorSnapshot("stopped", self.failures, "supervisor stopped")
        if process is None or record is None:
            return
        _terminate(process, self.spec.stop_seconds)
        remove_record(self.spec.paths, pid=record.pid)


__all__ = [
    "LOOPBACK",
    "RECORD_SCHEMA",
    "EndpointRecord",
    "ServiceConflict",
    "ServiceError",
    "ServiceOwned",
    "ServicePaths",
    "ServiceSpec",
    "ServiceStartFailed",
    "ServiceState",
    "ServiceStatus",
    "ServiceSupervisor",
    "ServiceUnavailable",
    "StartOutcome",
    "StopOutcome",
    "SupervisorKind",
    "SupervisorSnapshot",
    "ensure_token",
    "load_record",
    "mark_stale",
    "probe",
    "process_alive",
    "process_start_token",
    "read_token",
    "remove_record",
    "resolve",
    "start",
    "status",
    "stop",
    "write_record",
]
