"""Validation broker engine selection: ``slots`` (default) or ``local`` (bh-qrixp).

``work.validation_broker.engine`` picks the engine, and ``BH_VALIDATION_BROKER_ENGINE``
overrides it for one invocation. ``slots`` returns exactly the
:class:`~beadhive.validation_broker_slots.SlotsEngine` a caller would build by hand, so today's
admission is unchanged; nothing else in this module runs under it, not even validation of the
local engine's settings.

``local`` builds :class:`beadhive_validation_broker.LocalEngine` from the budget, floor and packer
settings, with ``/proc/meminfo`` availability, ``time.monotonic`` and the pid liveness probe, and
probes it once. A local engine that is misconfigured, unavailable (no ``/proc/meminfo``) or
erroring falls back to the slots engine with a loud warning, at selection time or later through
:class:`beadhive_validation_broker.FailoverBroker`.

No validation path calls this yet: wiring gates through the broker is ``bh-qhrl6``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import typer

from beadhive_validation_broker import (
    BrokerPort,
    Capacity,
    Clock,
    FailoverBroker,
    LocalEngine,
    MeminfoAvailability,
    PackerPolicy,
    ProcessLiveness,
    pid_alive,
)

from . import validation_memory
from .modules.config.contracts import ValidationBrokerConfig
from .validation_broker_slots import SlotsEngine

__all__ = [
    "ENGINES",
    "ENGINE_ENV",
    "broker_settings",
    "build_local_engine",
    "configured_engine",
    "select_engine",
]

ENGINE_ENV = "BH_VALIDATION_BROKER_ENGINE"
ENGINES = ("slots", "local")


def _warn(message: str) -> None:
    typer.echo(message, err=True)


def _raw_settings(cfg: dict) -> object:
    work = cfg.get("work") if isinstance(cfg, dict) else None
    return work.get("validation_broker") if isinstance(work, dict) else None


def configured_engine(cfg: dict, environ: Mapping[str, str] | None = None) -> str:
    """The engine name: the env override, else ``work.validation_broker.engine``, else slots."""
    env = os.environ if environ is None else environ
    override = env.get(ENGINE_ENV)
    if override is not None and override.strip():
        name, source = override.strip().lower(), ENGINE_ENV
    else:
        raw = _raw_settings(cfg)
        value = raw.get("engine", "slots") if isinstance(raw, dict) else "slots"
        name, source = str(value).strip().lower(), "work.validation_broker.engine"
    if name not in ENGINES:
        raise ValueError(f"{source} must be one of {', '.join(ENGINES)}, not {name!r}")
    return name


def broker_settings(cfg: dict) -> ValidationBrokerConfig:
    """``work.validation_broker`` validated against the config contract (defaults filled)."""
    raw = _raw_settings(cfg)
    return ValidationBrokerConfig.model_validate(raw if raw is not None else {})


def build_local_engine(
    settings: ValidationBrokerConfig,
    *,
    probe: MeminfoAvailability,
    clock: Clock,
    owner_alive: ProcessLiveness,
) -> LocalEngine:
    """A local engine whose budget and floor are resolved against this host's memory and CPUs.

    The budget is ``budget.cpu`` (0: every CPU) and ``budget.memory`` (a percentage of RAM or a
    size; none: all of RAM); the memory floor is the larger of ``floor.memory`` and
    ``floor.memory_percent`` of RAM. Raises when the host cannot be measured.
    """
    total = probe.total_memory_bytes()
    measured = probe()
    budget_memory = validation_memory.parse_size(settings.budget.memory, total) or total
    floor_memory = max(
        validation_memory.parse_size(settings.floor.memory, total) or 0,
        int(total * settings.floor.memory_percent / 100),
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
) -> BrokerPort:
    """The configured validation broker engine, falling back to slots loudly."""

    def slots() -> SlotsEngine:
        return SlotsEngine(cfg, entry, phase=phase, root=root)

    try:
        name = configured_engine(cfg, environ)
    except ValueError as exc:
        warn(f"⚠ validation broker: {exc}; using the slots engine")
        return slots()
    if name == "slots":
        return slots()
    try:
        local = build_local_engine(
            broker_settings(cfg),
            probe=probe or MeminfoAvailability(),
            clock=clock,
            owner_alive=owner_alive,
        )
        local.status()  # one live probe before any job depends on it
    except Exception as exc:  # noqa: BLE001 - any local failure means today's slots, loudly
        warn(
            f"⚠ validation broker: the local engine is unavailable ({exc}); using the slots engine"
        )
        return slots()
    return FailoverBroker(local, slots, warn=warn)
