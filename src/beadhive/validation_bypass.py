"""Explicit, audited validation bypass decisions.

This module owns the one non-green result shared by emergency config mode and the later one-shot
operator override.  A bypass is deliberately neither a successful process result nor a reusable
receipt: it is a durable decision to continue without executing the configured command.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from typing import Literal

import typer

from . import identity, otel, registry, validation_records
from .config_consumer_ports import work_settings as config

VALIDATION_PHASES = frozenset(
    {"validation", "check", "submit", "merge", "molecule", "union", "postland", "push-main"}
)


class BypassAuditError(RuntimeError):
    """The emergency bypass was requested but its mandatory audit record could not be stored."""


class BypassedExitCode(int):
    """Zero-compatible lifecycle result that remains distinguishable from a passed command."""

    bypassed = True


BYPASSED_EXIT = BypassedExitCode(0)


def is_bypassed(value: object) -> bool:
    return bool(getattr(value, "bypassed", False))


@dataclass(frozen=True, slots=True)
class BypassedValidation:
    """Machine-readable non-green result for one skipped validation boundary."""

    status: Literal["BYPASSED"]
    source: str
    hive: str
    actor: str
    bead: str | None
    sha: str
    tree: str
    phase: str
    command: str
    timestamp: str
    branch: str | None = None
    record_id: str | None = None

    def as_record(self) -> dict:
        value = asdict(self)
        value.pop("record_id", None)
        return value

    @property
    def message(self) -> str:
        candidate = self.sha[:12] if self.sha else "candidate unavailable"
        tree = f", tree {self.tree[:12]}" if self.tree else ""
        bead = f", bead {self.bead}" if self.bead else ""
        return (
            f"⚠ BYPASSED validation [{self.phase}] for hive {self.hive}{bead} "
            f"({candidate}{tree}) — {self.source}; configured command preserved: {self.command!r}"
        )


def enabled(cfg, entry) -> bool:
    return config.validation_bypass_enabled(cfg, entry)


def record(
    cfg,
    entry,
    *,
    phase: str,
    command: str,
    bead: str | None = None,
    sha: str = "",
    tree: str = "",
    branch: str | None = None,
    source: str = "work.validation_bypass=true",
    actor: str = "",
    emit: bool = True,
) -> BypassedValidation:
    """Record and render a bypass; never touch validation runs, ledgers, or attestations."""
    hive = str((entry or {}).get("prefix") or "unknown")
    main = registry.hive_dir(entry)
    profile = config.work_identity(cfg, entry)
    resolved_actor = identity.resolve_actor(actor, profile.get("name", ""), cwd=main)
    result = BypassedValidation(
        status="BYPASSED",
        source=source,
        hive=hive,
        actor=resolved_actor,
        bead=bead,
        sha=sha,
        tree=tree,
        phase=phase,
        command=command,
        timestamp=dt.datetime.now(dt.UTC).isoformat(),
        branch=branch,
    )
    durable = validation_records.record_bypass(main, result.as_record())
    if durable is None:
        raise BypassAuditError(
            f"validation bypass [{phase}] for hive {hive} was not applied: audit write failed"
        )
    result = BypassedValidation(**{**asdict(result), "record_id": durable["bypass_id"]})
    otel.count_validation_bypass(
        {
            "bh.hive": hive,
            "bh.work.phase": phase,
            "bh.validation.bypass.source": source,
        }
    )
    if emit:
        typer.echo(result.message)
    return result


def maybe_record(
    cfg,
    entry,
    *,
    phase: str,
    command: str,
    bead: str | None = None,
    sha: str = "",
    tree: str = "",
    branch: str | None = None,
    actor: str = "",
    emit: bool = True,
) -> BypassedValidation | None:
    """Return an audited bypass only for known validation phases in the enabled hive."""
    if phase not in VALIDATION_PHASES or not enabled(cfg, entry):
        return None
    return record(
        cfg,
        entry,
        phase=phase,
        command=command,
        bead=bead,
        sha=sha,
        tree=tree,
        branch=branch,
        actor=actor,
        emit=emit,
    )
