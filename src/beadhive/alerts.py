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
    """Surface configured worktree and host-free-space pressure as actionable alerts."""
    cfg = config.load()
    measurements = doctor._data_worktree_disk_usage(cfg)
    cap_bytes = config.alerts_worktree_cap_mb(cfg) * 1024 * 1024
    floor_bytes = config.alerts_disk_free_floor_mb(cfg) * 1024 * 1024
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

    free_bytes = measurements["disk_free_bytes"]
    if floor_bytes and free_bytes is not None and free_bytes < floor_bytes:
        rows.append(
            Alert(
                severity="warning",
                code="disk.free-space",
                message=(
                    f"host has {safety.format_bytes(free_bytes)} free disk space, below its "
                    f"{config.alerts_disk_free_floor_mb(cfg)} MB floor"
                ),
                remediation=(
                    "Dispatch a custodian to reclaim space, starting with `bh worktree prune` "
                    "and the largest managed worktrees."
                ),
            )
        )
    return rows


def active() -> list[dict[str, str]]:
    """Return all currently active alerts in the stable resource/CLI shape."""
    return [alert.as_dict() for source in _SOURCES for alert in source()]
