"""Host-wide admission and exact-identity serialization for validation runs."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import platformdirs
import typer

from . import otel


@dataclass(frozen=True)
class Permit:
    slot: int
    queue_seconds: float


def slot_root() -> Path:
    """A user/host-scoped root, deliberately unrelated to a hive or TMPDIR."""
    override = os.environ.get("BH_VALIDATION_SLOT_ROOT")
    if override:
        return Path(override)
    return Path(platformdirs.user_cache_dir("beadhive")) / "validation-slots"


def configured_slots(cfg: dict, entry=None) -> int:
    """Resolve one host capacity; hive-local entry overlays are intentionally ignored."""
    raw = os.environ.get("BH_VALIDATION_SLOTS")
    if raw is None:
        work = cfg.get("work") if isinstance(cfg, dict) else None
        raw = work.get("validation_slots", 1) if isinstance(work, dict) else 1
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("validation slots must be a non-negative integer") from exc
    if value < 0:
        raise ValueError("validation slots must be a non-negative integer")
    return value


def priority_command(cfg: dict, command: list[str]) -> tuple[list[str], dict]:
    """Return the validation launcher and an auditable summary of its scheduling policy.

    The controls are best-effort.  `BH_VALIDATION_PRIORITY` is the host emergency override;
    otherwise the host-level `work.validation_priority` setting defaults to enabled.
    """
    work = cfg.get("work") if isinstance(cfg, dict) else None
    enabled: object = work.get("validation_priority", True) if isinstance(work, dict) else True
    override = os.environ.get("BH_VALIDATION_PRIORITY")
    if override is not None:
        enabled = override.strip().lower() not in {"0", "false", "no", "off"}
    enabled = enabled is not False and str(enabled).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
        "none",
        "",
    }
    policy = {
        "enabled": enabled,
        "applied": False,
        "mechanism": "disabled" if not enabled else "unavailable",
        "nice": 10,
        "ionice_class": 2,
        "ionice_priority": 7,
        "cpu_weight": 20,
        "io_weight": 20,
    }
    if not enabled:
        return command, policy

    nice = shutil.which("nice")
    ionice = shutil.which("ionice")
    launcher = list(command)
    layers = []
    if nice:
        launcher = [nice, "-n", "10", *launcher]
        layers.append("nice")
    if ionice:
        launcher = [ionice, "-c2", "-n7", *launcher]
        layers.append("ionice")

    # A user manager is often absent in CI and containers even when systemd-run is installed.
    # Probe only local session evidence and keep the command path non-blocking.
    systemd_run = shutil.which("systemd-run")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    bus_address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    bus_available = bool(
        (runtime and (Path(runtime) / "bus").exists()) or bus_address.startswith("unix:path=")
    )
    scope_available = False
    if systemd_run and bus_available:
        try:
            probe = subprocess.run(
                [
                    systemd_run,
                    "--user",
                    "--scope",
                    "--quiet",
                    "--property=CPUWeight=20",
                    "--property=IOWeight=20",
                    "--",
                    "/usr/bin/true",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=2,
            )
            scope_available = probe.returncode == 0
        except (OSError, subprocess.SubprocessError):
            scope_available = False
    if scope_available:
        launcher = [
            systemd_run,
            "--user",
            "--scope",
            "--quiet",
            "--property=CPUWeight=20",
            "--property=IOWeight=20",
            "--",
            *launcher,
        ]
        layers.insert(0, "systemd-user-scope")

    if layers:
        policy["applied"] = True
        policy["mechanism"] = "+".join(layers)
    return launcher, policy


def _attributes(entry, phase: str) -> dict[str, str]:
    """The deliberately bounded admission metric dimensions."""
    return {
        "bh.hive": str((entry or {}).get("prefix") or ""),
        "bh.work.phase": phase,
    }


@contextlib.contextmanager
def host_slot(cfg: dict, entry=None, *, phase: str = "validation", root: Path | None = None):
    """Block until one counting-semaphore permit is held; zero disables admission.

    The lock files are stable kernel-lock rendezvous points, not ownership records.  Queue and
    admission facts are emitted through the existing telemetry surface and the authoritative
    execution/use records; no parallel scheduler state is introduced here.
    """
    slots = configured_slots(cfg, entry)
    if slots == 0:
        permit = Permit(-1, 0.0)
        otel.record_validation_queue_wait(0.0, _attributes(entry, phase))
        otel.count_validation_admitted(_attributes(entry, phase))
        typer.echo("  → validation admission disabled; executing without a host slot")
        yield permit
        return
    root = slot_root() if root is None else Path(root)
    root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    announced = False
    while True:
        for index in range(slots):
            handle = (root / f"slot-{index}").open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                handle.close()
                continue
            try:
                permit = Permit(index, time.monotonic() - started)
                attrs = _attributes(entry, phase)
                otel.record_validation_queue_wait(permit.queue_seconds, attrs)
                otel.count_validation_admitted(attrs)
                typer.echo(
                    f"  → validation admitted to host slot {index + 1}/{slots} "
                    f"after {permit.queue_seconds:.3f}s; executing"
                )
                yield permit
                return
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
        if not announced:
            typer.echo(f"  → queued for validation slot (host capacity {slots})")
            announced = True
        time.sleep(0.05)


@contextlib.contextmanager
def identity_lock(hive: str | Path, tree: str, command_hash: str, *, root: Path | None = None):
    """Serialize one hive/tree/command identity without consuming a host permit."""
    lock_root = (slot_root() / "identities") if root is None else Path(root)
    lock_root.mkdir(parents=True, exist_ok=True)
    hive_key = str(Path(hive).resolve())
    key = hashlib.sha256(f"{hive_key}\0{tree}\0{command_hash}".encode()).hexdigest()
    handle = (lock_root / key).open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
