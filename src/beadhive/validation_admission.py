"""Host-wide admission and exact-identity serialization for validation runs."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import platformdirs
import typer

from . import otel, validation_memory


@dataclass(frozen=True)
class Permit:
    slot: int
    queue_seconds: float
    # The MemAvailable admission-floor record (bh-jg7fy); None only for hand-built permits.
    memory: dict | None = None


def _admit_memory(cfg: dict) -> dict:
    """Wait (holding at most the permit already acquired) for the MemAvailable floor.

    Never acquires a slot itself, so it cannot nest admission.  A timeout refuses to start the
    run with the retryable exit 75 rather than launching into memory pressure.
    """
    try:
        return validation_memory.wait_for_floor(validation_memory.settings(cfg), echo=typer.echo)
    except validation_memory.MemoryAdmissionTimeout as exc:
        typer.echo(
            f"✗ validation not started: {exc}. Free memory or tune "
            "work.validation_memory.admission_floor (BH_VALIDATION_MEMORY_FLOOR=0 skips it).",
            err=True,
        )
        raise typer.Exit(75) from None


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


def _priority_bool(value: object, *, source: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{source} must be a boolean")


def _priority_int(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return parsed


def _priority_settings(cfg: dict) -> tuple[bool, int, int, int]:
    work = cfg.get("work") if isinstance(cfg, dict) else None
    raw = work.get("validation_priority", {}) if isinstance(work, dict) else {}
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("work.validation_priority must be a mapping")
    unknown = set(raw) - {"enabled", "nice", "ionice_class", "ionice_priority"}
    if unknown:
        raise ValueError(
            "unknown work.validation_priority setting(s): " + ", ".join(sorted(unknown))
        )
    enabled = _priority_bool(raw.get("enabled", True), source="validation priority enabled")
    override = os.environ.get("BH_VALIDATION_PRIORITY")
    if override is not None:
        enabled = _priority_bool(override, source="BH_VALIDATION_PRIORITY")
    nice_level = _priority_int(raw.get("nice", 10), name="validation nice", minimum=0, maximum=19)
    ionice_class = _priority_int(
        raw.get("ionice_class", 2), name="validation ionice class", minimum=2, maximum=3
    )
    ionice_priority = _priority_int(
        raw.get("ionice_priority", 7),
        name="validation ionice priority",
        minimum=0,
        maximum=7,
    )
    return enabled, nice_level, ionice_class, ionice_priority


def _current_nice() -> int | None:
    """Read inherited process niceness when the host exposes the POSIX priority API."""
    try:
        return os.getpriority(os.PRIO_PROCESS, 0)
    except (AttributeError, OSError):
        return None


def _priority_prefix_available(prefix: list[str]) -> bool:
    """Return whether a scheduling prefix can launch a harmless child on this host."""
    try:
        probe = subprocess.run(
            [*prefix, sys.executable, "-c", "pass"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def _priority_prefix(cfg: dict) -> tuple[list[str], dict, list[str]]:
    """The nice/ionice launcher prefix, the auditable priority policy, and applied layers."""
    enabled, nice_level, ionice_class, ionice_priority = _priority_settings(cfg)
    inherited_nice = _current_nice()
    nice_increment = max(0, nice_level - inherited_nice) if inherited_nice is not None else None
    policy = {
        "enabled": enabled,
        "applied": False,
        "mechanism": "disabled" if not enabled else "unavailable",
        # Keep `nice` as the configured value for manifest compatibility while making the
        # request, inherited state, and expected result explicit.
        "nice": nice_level,
        "requested_nice": nice_level,
        "inherited_nice": inherited_nice,
        "effective_nice": inherited_nice,
        "nice_increment": nice_increment,
        "ionice_class": ionice_class,
        "ionice_priority": ionice_priority,
        "cpu_weight": 20,
        "io_weight": 20,
    }
    if not enabled:
        return [], policy, []

    nice = shutil.which("nice")
    ionice = shutil.which("ionice")
    prefix: list[str] = []
    layers = []
    if nice and nice_increment:
        nice_argv = [nice, "-n", str(nice_increment)]
        if _priority_prefix_available(nice_argv):
            prefix = [*nice_argv, *prefix]
            layers.append("nice")
            policy["effective_nice"] = nice_level
    if ionice:
        ionice_argv = [ionice, f"-c{ionice_class}"]
        if ionice_class == 2:
            ionice_argv.append(f"-n{ionice_priority}")
        if _priority_prefix_available(ionice_argv):
            prefix = [*ionice_argv, *prefix]
            layers.append("ionice")
    return prefix, policy, layers


_PRIORITY_SCOPE_PROPERTIES = ("--property=CPUWeight=20", "--property=IOWeight=20")


def _user_scope_prefix(properties: list[str]) -> list[str] | None:
    """A ``systemd-run --user --scope`` prefix carrying ``properties``, or None if unusable.

    A user manager is often absent in CI and containers even when systemd-run is installed.
    Probe only local session evidence and keep the command path non-blocking.
    """
    systemd_run = shutil.which("systemd-run")
    true_executable = shutil.which("true")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    bus_address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    bus_available = bool(
        (runtime and (Path(runtime) / "bus").exists()) or bus_address.startswith("unix:path=")
    )
    if not (systemd_run and true_executable and bus_available):
        return None
    prefix = [systemd_run, "--user", "--scope", "--quiet", *properties, "--"]
    try:
        probe = subprocess.run(
            [*prefix, true_executable],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return prefix if probe.returncode == 0 else None


def priority_command(cfg: dict, command: list[str]) -> tuple[list[str], dict]:
    """Return the validation launcher and an auditable summary of its scheduling policy.

    The controls are best-effort.  `BH_VALIDATION_PRIORITY` is the host emergency override;
    otherwise the host-level `work.validation_priority` setting defaults to enabled.
    """
    prefix, policy, layers = _priority_prefix(cfg)
    if not policy["enabled"]:
        return command, policy
    launcher = [*prefix, *command]
    scope = _user_scope_prefix(list(_PRIORITY_SCOPE_PROPERTIES))
    if scope is not None:
        launcher = [*scope, *launcher]
        layers.insert(0, "systemd-user-scope")

    if layers:
        policy["applied"] = True
        policy["mechanism"] = "+".join(layers)
    return launcher, policy


def guarded_command(
    cfg: dict,
    command: list[str],
    *,
    report_path: Path | None = None,
    policy: validation_memory.MemorySettings | None = None,
) -> tuple[list[str], dict, dict]:
    """The full validation launcher: one user scope with priority weights and memory bounds.

    Returns ``(argv, priority_policy, memory_record)``.  The scope (when available) carries the
    CPU/IO weights of ``work.validation_priority`` and the MemoryHigh/MemoryMax/MemorySwapMax of
    ``work.validation_memory`` so the kernel OOM-kills *inside the scope*.  With a
    ``report_path`` the innermost layer is :mod:`beadhive.validation_memory_exec`, which records
    the run's memory peak (and scope OOM kills) for the manifest.  Each part degrades
    independently: a host without a usable user manager or memory controller still gets
    nice/ionice and a peak measurement, with the skipped bound named in ``memory_record``.
    """
    prefix, priority_policy, layers = _priority_prefix(cfg)
    memory_policy = validation_memory.settings(cfg) if policy is None else policy
    memory_properties, memory_record = validation_memory.scope_properties(memory_policy)
    properties = [
        *(_PRIORITY_SCOPE_PROPERTIES if priority_policy["enabled"] else ()),
        *memory_properties,
    ]
    scope = _user_scope_prefix(properties) if properties else None
    if scope is None and memory_properties:
        memory_record["note"] = (
            "systemd-run --user scope with memory properties is unavailable; memory bounds skipped"
        )
        if priority_policy["enabled"]:
            # The weights alone may still be honoured where the memory properties are not.
            scope = _user_scope_prefix(list(_PRIORITY_SCOPE_PROPERTIES))
            memory_properties = []
    elif scope is not None and memory_properties:
        memory_record["applied"] = True
        memory_record["mechanism"] = "systemd-user-scope"
    if scope is not None and priority_policy["enabled"]:
        layers.insert(0, "systemd-user-scope")
    if not priority_policy["enabled"]:
        prefix = []
    elif layers:
        priority_policy["applied"] = True
        priority_policy["mechanism"] = "+".join(layers)
    inner = (
        validation_memory.exec_prefix(report_path, scoped=scope is not None)
        if report_path is not None
        else []
    )
    launcher = [*(scope or []), *prefix, *inner, *command]
    return launcher, priority_policy, memory_record


def _attributes(entry, phase: str) -> dict[str, str]:
    """The deliberately bounded admission metric dimensions."""
    return {
        "bh.hive": str((entry or {}).get("prefix") or ""),
        "bh.work.phase": phase,
    }


@contextlib.contextmanager
def host_slot(cfg: dict, entry=None, *, phase: str = "validation", root: Path | None = None):
    """Block until one counting-semaphore permit is held; zero disables admission.

    Once a permit is held (or admission is disabled), admission also waits for host
    ``MemAvailable`` to reach ``work.validation_memory.admission_floor`` and records that wait
    on the permit (bh-jg7fy).  The wait never acquires a second permit.

    The lock files are stable kernel-lock rendezvous points, not ownership records.  Queue and
    admission facts are emitted through the existing telemetry surface and the authoritative
    execution/use records; no parallel scheduler state is introduced here.
    """
    from .hq_authority_expiry import warn_if_expiring

    warn_if_expiring()  # before the gate starts, so a long gate is not fenced mid-run
    slots = configured_slots(cfg, entry)
    if slots == 0:
        permit = Permit(-1, 0.0, _admit_memory(cfg))
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
                queue_seconds = time.monotonic() - started
                permit = Permit(index, queue_seconds, _admit_memory(cfg))
                attrs = _attributes(entry, phase)
                otel.record_validation_queue_wait(permit.queue_seconds, attrs)
                otel.count_validation_admitted(attrs)
                memory_wait = (permit.memory or {}).get("wait_seconds") or 0.0
                waited = f" and {memory_wait:.1f}s for memory" if memory_wait else ""
                typer.echo(
                    f"  → validation admitted to host slot {index + 1}/{slots} "
                    f"after {permit.queue_seconds:.3f}s{waited}; executing"
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
