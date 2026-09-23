"""The normalized, agent-facing alert surface.

Alert sources are deliberately small functions returning :class:`Alert` records.  The
first source adapts the warnings that ``bh doctor`` already calculates; later sources
can register a rule here without teaching every harness integration about it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass

from . import config, doctor, safety


@dataclass(frozen=True)
class Alert:
    """One active condition an agent or operator should be steered toward."""

    severity: str
    code: str
    message: str
    remediation: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


AlertSource = Callable[[], list[Alert]]
_SOURCES: list[AlertSource] = []


def register(source: AlertSource) -> AlertSource:
    """Register an alert source and return it, so sources can use decorator syntax."""
    _SOURCES.append(source)
    return source


@register
def doctor_warnings() -> list[Alert]:
    """Adapt existing doctor warnings without creating a second warning rule set."""
    return [
        Alert(
            severity="warning",
            code="doctor.warning",
            message=message,
            remediation=(
                "Run `bh doctor` for the full diagnostic context, then address the condition "
                "named in this alert."
            ),
        )
        for message in doctor.warning_messages()
    ]


@register
def disk_pressure() -> list[Alert]:
    """Surface worktree-filesystem and host-root pressure as separate actionable alerts."""
    cfg = config.load()
    measurements = doctor._data_worktree_disk_usage(cfg)
    cap_bytes = config.alerts_worktree_cap_mb(cfg) * 1024 * 1024
    worktree_floor_mb = config.alerts_worktree_filesystem_free_floor_mb(cfg)
    worktree_floor_bytes = worktree_floor_mb * 1024 * 1024
    host_root_floor_mb = config.alerts_disk_free_floor_mb(cfg)
    host_root_floor_bytes = host_root_floor_mb * 1024 * 1024
    rows: list[Alert] = []

    if cap_bytes:
        for hive in measurements["hives"]:
            if hive["worktree_bytes"] > cap_bytes:
                rows.append(
                    Alert(
                        severity="warning",
                        code="disk.worktree-footprint",
                        message=(
                            f"hive '{hive['prefix']}' uses "
                            f"{safety.format_bytes(hive['worktree_bytes'])} in managed "
                            f"worktrees, above its {config.alerts_worktree_cap_mb(cfg)} MB cap"
                        ),
                        remediation=(
                            "Dispatch a custodian to inspect and safely prune merged or "
                            "abandoned worktrees with `bh worktree prune`."
                        ),
                    )
                )

    worktree_filesystem = measurements.get("worktree_filesystem", {})
    worktree_free_bytes = worktree_filesystem.get("free_bytes")
    if (
        worktree_floor_bytes
        and worktree_free_bytes is not None
        and worktree_free_bytes < worktree_floor_bytes
    ):
        mount = worktree_filesystem.get("mount_point") or "unknown mount"
        filesystem = worktree_filesystem.get("filesystem_type") or "unknown filesystem"
        device = worktree_filesystem.get("device") or "unknown device"
        root = worktree_filesystem.get("root") or "configured worktree root"
        rows.append(
            Alert(
                severity="warning",
                code="disk.worktree-filesystem-free-space",
                message=(
                    f"worktree root '{root}' on {filesystem} device '{device}' mounted at "
                    f"'{mount}' has "
                    f"{safety.format_bytes(worktree_free_bytes)} free, below its "
                    f"{worktree_floor_mb} MB filesystem floor"
                ),
                remediation=(
                    f"Free capacity on '{mount}': dispatch a custodian to prune safe merged or "
                    "abandoned worktrees with `bh worktree prune`, or move `worktrees.path` / "
                    "`BH_WORKTREES` to a filesystem with more capacity."
                ),
            )
        )

    host_root_filesystem = measurements.get("host_root_filesystem", {})
    host_root_free_bytes = host_root_filesystem.get("free_bytes")
    if (
        host_root_floor_bytes
        and host_root_free_bytes is not None
        and host_root_free_bytes < host_root_floor_bytes
    ):
        mount = host_root_filesystem.get("mount_point") or "/"
        filesystem = host_root_filesystem.get("filesystem_type") or "unknown filesystem"
        device = host_root_filesystem.get("device") or "unknown device"
        rows.append(
            Alert(
                severity="warning",
                code="disk.free-space",
                message=(
                    f"host root filesystem {filesystem} device '{device}' mounted at "
                    f"'{mount}' has "
                    f"{safety.format_bytes(host_root_free_bytes)} free, below its "
                    f"{host_root_floor_mb} MB floor"
                ),
                remediation=(
                    f"Reclaim space on the host root filesystem mounted at '{mount}' and "
                    "inspect `df -h /`; prune worktrees only if they share this filesystem."
                ),
            )
        )
    return rows


def active() -> list[dict[str, str]]:
    """Return all currently active alerts in the stable resource/CLI shape."""
    return [alert.as_dict() for source in _SOURCES for alert in source()]
