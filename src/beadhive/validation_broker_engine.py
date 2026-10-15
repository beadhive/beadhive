"""Validation broker engine selection: ``slots`` (default) or ``local`` (bh-qrixp).

``work.validation_broker.engine`` picks the engine, and ``BH_VALIDATION_BROKER_ENGINE``
overrides it for one invocation. ``slots`` returns exactly the
:class:`~beadhive.validation_broker_slots.SlotsEngine` a caller would build by hand, so today's
admission is unchanged; nothing else in this module runs under it, not even validation of the
local engine's settings.

``local`` builds :class:`beadhive_validation_broker.LocalEngine` from the budget, floor and packer
settings, with ``/proc/meminfo`` availability, ``time.monotonic`` and the pid liveness probe, and
probes it once. ``work.validation_broker.roots`` (bh-mf1o4) adds execution roots: each must be
an existing directory, and a root declared ``disk`` that is RAM-backed is refused (it would
undercount memory). A local engine that is misconfigured, unavailable (no ``/proc/meminfo``) or
erroring falls back to the slots engine with a loud warning, at selection time or later through
:class:`beadhive_validation_broker.FailoverBroker`.

A gate's pending attest keys run side by side through the local engine itself
(:func:`local_engine`, ``bh-qhrl6``): there a failing engine sends the keys it has not started
back to today's sequential slots path, loudly, instead of to the slots engine's port.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import typer

from beadhive_validation_broker import (
    BrokerPort,
    Capacity,
    Clock,
    DiskSpace,
    ExecutionRoot,
    FailoverBroker,
    LocalEngine,
    MeminfoAvailability,
    PackerPolicy,
    ProcessLiveness,
    RootKind,
    pid_alive,
    statvfs_free,
)

from . import config_paths, validation_memory
from .modules.config.contracts import ValidationBrokerConfig
from .validation_broker_slots import SlotsEngine

__all__ = [
    "ENGINES",
    "ENGINE_ENV",
    "BrokerResolution",
    "broker_settings",
    "build_local_engine",
    "configured_engine",
    "describe_broker",
    "execution_roots",
    "local_engine",
    "main",
    "resolve_broker",
    "select_engine",
]

ENGINE_ENV = "BH_VALIDATION_BROKER_ENGINE"
ENGINES = ("slots", "local")


def _warn(message: str) -> None:
    typer.echo(message, err=True)


def _section(owner: object) -> dict:
    """``owner['work']['validation_broker']`` as a dict; anything unreadable is empty."""
    work = owner.get("work") if isinstance(owner, dict) else None
    raw = work.get("validation_broker") if isinstance(work, dict) else None
    return raw if isinstance(raw, dict) else {}


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


#: Fields a hive chooses (hive over host/fleet). ``budget`` is requested per hive but bounded by
#: the host; :data:`HOST_OWNED` come from the host layer only.
HIVE_OWNED = ("engine", "setup")
HOST_OWNED = ("floor", "repoll_seconds", "roots")


@dataclass(frozen=True)
class BrokerResolution:
    """The effective ``work.validation_broker`` for one hive and where each field came from.

    ``memory_ceiling`` is the host's own configured ``budget.memory`` (``None``: the host's
    measured RAM), which :func:`build_local_engine` applies once RAM is known.
    ``sources`` maps a dotted field to ``hive``, ``global`` (the merged host/fleet ``work``),
    ``default`` or ``host cap`` (a hive request the host limit lowered).
    """

    settings: ValidationBrokerConfig
    memory_ceiling: str | None
    sources: dict[str, str]


def resolve_broker(cfg: dict, entry=None) -> BrokerResolution:
    """Layer the hive's ``work.validation_broker`` over the merged host/fleet one.

    Hive-owned fields (``engine``, ``setup``, ``packer`` field by field) resolve hive over
    global. ``budget`` fields are requested per hive but never exceed the host's configured
    budget (a nonzero ``cpu``, ``memory``, each shared pool); ``floor``, ``repoll_seconds`` and
    ``roots`` come from the host layer only. Unlike ``configured_slots``, which ignores hive
    overlays because one host-wide capacity is shared by every hive, the broker's engine and
    packer are per-project choices while its budget stays bounded by what the host may grant.
    """
    host, hive = _section(cfg), _section(entry)
    sources: dict[str, str] = {}

    def origin(field: str, in_hive: bool, in_host: bool) -> None:
        sources[field] = "hive" if in_hive else "global" if in_host else "default"

    merged = {k: v for k, v in host.items() if k not in ("budget", "packer")}
    for field in HIVE_OWNED:
        if field in hive:
            merged[field] = hive[field]
        origin(field, field in hive, field in host)
    host_packer, hive_packer = _mapping(host.get("packer")), _mapping(hive.get("packer"))
    merged["packer"] = {**host_packer, **hive_packer}
    origin("packer", bool(hive_packer), bool(host_packer))
    for field in HOST_OWNED:
        origin(field, False, field in host)

    host_budget, hive_budget = _mapping(host.get("budget")), _mapping(hive.get("budget"))
    host_eff = ValidationBrokerConfig.model_validate({"budget": host_budget}).budget
    budget = {**host_budget, **hive_budget}
    for field in ("cpu", "memory", "pools"):
        origin(f"budget.{field}", field in hive_budget, field in host_budget)
    if host_eff.cpu > 0 and "cpu" in hive_budget:
        asked = float(hive_budget["cpu"])
        if asked > host_eff.cpu or asked <= 0:
            budget["cpu"] = host_eff.cpu
            sources["budget.cpu"] = "host cap"
    if isinstance(hive_budget.get("pools"), dict):
        pools = dict(host_eff.pools)
        for name, amount in hive_budget["pools"].items():
            ceiling = host_eff.pools.get(name)
            pools[name] = amount if ceiling is None else min(amount, ceiling)
            if ceiling is not None and amount > ceiling:
                sources["budget.pools"] = "host cap"
        budget["pools"] = pools
    merged["budget"] = budget
    settings = ValidationBrokerConfig.model_validate(merged)
    memory_ceiling = host_eff.memory if "memory" in host_budget else None
    return BrokerResolution(settings, memory_ceiling, sources)


def configured_engine(cfg: dict, environ: Mapping[str, str] | None = None, entry=None) -> str:
    """The engine name: the env override, else the hive's ``work.validation_broker.engine`` over
    the host/fleet one, else slots."""
    env = os.environ if environ is None else environ
    override = env.get(ENGINE_ENV)
    if override is not None and override.strip():
        name, source = override.strip().lower(), ENGINE_ENV
    else:
        value = _section(entry).get("engine", _section(cfg).get("engine", "slots"))
        name, source = str(value).strip().lower(), "work.validation_broker.engine"
    if name not in ENGINES:
        raise ValueError(f"{source} must be one of {', '.join(ENGINES)}, not {name!r}")
    return name


def broker_settings(cfg: dict, entry=None) -> ValidationBrokerConfig:
    """The effective ``work.validation_broker`` for the hive (:func:`resolve_broker`),
    validated against the config contract (defaults filled)."""
    return resolve_broker(cfg, entry).settings


def describe_broker(cfg: dict, entry=None, environ: Mapping[str, str] | None = None) -> dict:
    """Effective broker settings plus a ``sources`` map, for ``bh config broker``."""
    resolution = resolve_broker(cfg, entry)
    env = os.environ if environ is None else environ
    sources = dict(resolution.sources)
    if (env.get(ENGINE_ENV) or "").strip():
        sources["engine"] = ENGINE_ENV
    return {
        "engine": configured_engine(cfg, env, entry),
        "settings": resolution.settings.model_dump(),
        "memory_ceiling": resolution.memory_ceiling,
        "sources": sources,
    }


def _memory_backed(path: Path) -> bool:
    return config_paths.statfs_type(path) in config_paths.MEMORY_BACKED_MAGICS


def execution_roots(
    settings: ValidationBrokerConfig,
    *,
    memory_backed: Callable[[Path], bool] = _memory_backed,
) -> tuple[ExecutionRoot, ...]:
    """``work.validation_broker.roots`` resolved: ``~`` expanded, symlinks resolved, sizes in
    bytes. Raises ``ValueError`` for a missing directory or a ``disk`` root that is RAM-backed."""
    roots = []
    for entry in settings.roots:
        path = Path(entry.path).expanduser()
        if not path.is_dir():
            raise ValueError(f"execution root {path} is not an existing directory")
        path = path.resolve()
        if entry.kind == "disk" and memory_backed(path):
            raise ValueError(
                f"execution root {path} is declared disk but is RAM-backed (tmpfs/ramfs); "
                "declare it kind: tmpfs so its usage counts against memory"
            )
        size = validation_memory.parse_size(entry.max_bytes, None)
        if size is None:
            raise ValueError(f"execution root {path} needs a max_bytes size")
        roots.append(ExecutionRoot(str(path), RootKind(entry.kind), size))
    return tuple(roots)


def build_local_engine(
    settings: ValidationBrokerConfig,
    *,
    probe: MeminfoAvailability,
    clock: Clock,
    owner_alive: ProcessLiveness,
    disk_space: DiskSpace = statvfs_free,
    memory_backed: Callable[[Path], bool] = _memory_backed,
    memory_ceiling: str | None = None,
) -> LocalEngine:
    """A local engine whose budget and floor are resolved against this host's memory and CPUs.

    The budget is ``budget.cpu`` (0: every CPU) and ``budget.memory`` (a percentage of RAM or a
    size; none: all of RAM); the memory floor is the larger of ``floor.memory`` and
    ``floor.memory_percent`` of RAM. Raises when the host cannot be measured or a root is
    invalid (:func:`execution_roots`).
    """
    roots = execution_roots(settings, memory_backed=memory_backed)
    total = probe.total_memory_bytes()
    measured = probe()
    budget_memory = validation_memory.parse_size(settings.budget.memory, total) or total
    floor_memory = max(
        validation_memory.parse_size(settings.floor.memory, total) or 0,
        int(total * settings.floor.memory_percent / 100),
    )
    if memory_ceiling is not None:
        budget_memory = min(
            budget_memory, validation_memory.parse_size(memory_ceiling, total) or total
        )
    budget = Capacity(
        settings.budget.cpu or measured.cpu,
        min(budget_memory, total),
        dict(settings.budget.pools),
    )
    return LocalEngine(
        budget,
        clock,
        availability=probe,
        floor=Capacity(settings.floor.cpu, floor_memory),
        owner_alive=owner_alive,
        policy=PackerPolicy(**settings.packer.model_dump()),
        repoll_seconds=settings.repoll_seconds,
        roots=roots,
        disk_space=disk_space if roots else None,
    )


def select_engine(
    cfg: dict,
    entry=None,
    *,
    phase: str = "validation",
    root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    probe: MeminfoAvailability | None = None,
    clock: Clock = time.monotonic,
    owner_alive: ProcessLiveness = pid_alive,
    warn: Callable[[str], None] = _warn,
    disk_space: DiskSpace = statvfs_free,
    memory_backed: Callable[[Path], bool] = _memory_backed,
) -> BrokerPort:
    """The configured validation broker engine, falling back to slots loudly."""

    def slots() -> SlotsEngine:
        return SlotsEngine(cfg, entry, phase=phase, root=root)

    local = local_engine(
        cfg,
        entry,
        environ=environ,
        probe=probe,
        clock=clock,
        owner_alive=owner_alive,
        warn=warn,
        disk_space=disk_space,
        memory_backed=memory_backed,
    )
    if local is None:
        return slots()
    return FailoverBroker(local, slots, warn=warn)


def local_engine(
    cfg: dict,
    entry=None,
    *,
    environ: Mapping[str, str] | None = None,
    probe: MeminfoAvailability | None = None,
    clock: Clock = time.monotonic,
    owner_alive: ProcessLiveness = pid_alive,
    warn: Callable[[str], None] = _warn,
    disk_space: DiskSpace = statvfs_free,
    memory_backed: Callable[[Path], bool] = _memory_backed,
) -> LocalEngine | None:
    """The probed local engine when it is the configured engine, else ``None``.

    ``None`` without a word under slots; ``None`` with a loud warning when the engine name is
    invalid or the local engine is misconfigured or unavailable (the caller uses slots).
    """
    try:
        name = configured_engine(cfg, environ, entry)
    except ValueError as exc:
        warn(f"⚠ validation broker: {exc}; using the slots engine")
        return None
    if name == "slots":
        return None
    try:
        resolution = resolve_broker(cfg, entry)
        local = build_local_engine(
            resolution.settings,
            memory_ceiling=resolution.memory_ceiling,
            probe=probe or MeminfoAvailability(),
            clock=clock,
            owner_alive=owner_alive,
            disk_space=disk_space,
            memory_backed=memory_backed,
        )
        local.status()  # one live probe before any job depends on it
    except Exception as exc:  # noqa: BLE001 - any local failure means today's slots, loudly
        warn(
            f"⚠ validation broker: the local engine is unavailable ({exc}); using the slots engine"
        )
        return None
    return local


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m beadhive.validation_broker_engine [--hive H] [--json]``: show a hive's
    effective ``work.validation_broker`` and where each field came from (default: the hive
    owning the current directory)."""
    import argparse
    import json

    from . import registry
    from .config_work_settings import load

    parser = argparse.ArgumentParser(prog="python -m beadhive.validation_broker_engine")
    parser.add_argument("--hive", default="", help="hive to resolve (default: owner of cwd)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    cfg = load()
    entry = registry.resolve_hive(cfg, args.hive) if args.hive else registry.current_hive(cfg)
    try:
        report = describe_broker(cfg, entry)
    except ValueError as exc:
        typer.echo(f"✗ {exc}", err=True)
        return 1
    if args.json:
        typer.echo(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0
    who = f"{entry['org']}/{entry['repo']}" if entry else "(no hive: host/fleet only)"
    typer.echo(f"work.validation_broker for {who}")
    sources, settings = report["sources"], report["settings"]
    typer.echo(f"  engine: {report['engine']}  [{sources['engine']}]")
    for name in ("cpu", "memory", "pools"):
        typer.echo(f"  budget.{name}: {settings['budget'][name]}  [{sources[f'budget.{name}']}]")
    for name in ("packer", "setup", "floor", "repoll_seconds", "roots"):
        typer.echo(f"  {name}: {settings[name]}  [{sources[name]}]")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
