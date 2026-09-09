"""Shared daemon-supervision vocabulary owned inside the daemon kernel."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol

PLATFORM_LIFECYCLE_HANDOFF = (
    "platform lifecycle management is owned by bh-q0lol.14; this backend is detect-only"
)


class DaemonKeyPort(Protocol):
    """The stable portion of a daemon key needed by supervisor backends."""

    @property
    def digest(self) -> str: ...


class SupervisorError(RuntimeError):
    """An operator-facing daemon supervisor failure."""


class SupervisorIdentityError(SupervisorError):
    """A backend answered for a different daemon singleton key."""


class SupervisorUnavailableError(SupervisorError):
    """The current environment has no installed lifecycle implementation."""


@dataclass(frozen=True)
class SupervisorState:
    key: Any
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


def service_name(key: DaemonKeyPort) -> str:
    """Opaque per-key service name; raw homes and host ids never enter manager identifiers."""
    return f"bh-host-daemon-{key.digest}"


def _guidance(key: DaemonKeyPort, backend: str, *, supported: bool) -> dict[str, str]:
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


def build_supervisor_state(
    key: DaemonKeyPort,
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


_state = build_supervisor_state


__all__ = (
    "PLATFORM_LIFECYCLE_HANDOFF",
    "SupervisorError",
    "SupervisorIdentityError",
    "SupervisorState",
    "SupervisorUnavailableError",
    "build_supervisor_state",
    "service_name",
)
