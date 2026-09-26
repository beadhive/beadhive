"""Explicit, audited validation bypass decisions.

This module owns the one non-green result shared by emergency config mode and the later one-shot
operator override.  A bypass is deliberately neither a successful process result nor a reusable
receipt: it is a durable decision to continue without executing the configured command.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Literal

import typer

from . import identity, otel, registry, validation_records, work_guards
from .config_consumer_ports import work_settings as config

VALIDATION_PHASES = frozenset(
    {"validation", "check", "submit", "merge", "molecule", "union", "postland", "push-main"}
)


class BypassAuditError(RuntimeError):
    """The emergency bypass was requested but its mandatory audit record could not be stored."""


class OverrideRefused(ValueError):
    """A one-shot override is unauthorized, incomplete, or stale."""


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
    reason: str = ""

    def as_record(self) -> dict:
        value = asdict(self)
        value.pop("record_id", None)
        return value

    @property
    def message(self) -> str:
        candidate = self.sha[:12] if self.sha else "candidate unavailable"
        tree = f", tree {self.tree[:12]}" if self.tree else ""
        bead = f", bead {self.bead}" if self.bead else ""
        reason = f"; reason: {self.reason}" if self.reason else ""
        return (
            f"⚠ BYPASSED validation [{self.phase}] for hive {self.hive}{bead} "
            f"({candidate}{tree}) — {self.source}{reason}; configured command preserved: "
            f"{self.command!r}"
        )


@dataclass(frozen=True, slots=True)
class OneShotOverride:
    """An authorized one-use decision bound to one exact validation candidate."""

    actor: str
    bead: str
    phase: str
    reason: str
    sha: str
    tree: str


def bind_override(
    *, actor: str, bead: str, phase: str, reason: str, sha: str, tree: str
) -> OneShotOverride:
    """Authorize and bind an operator override to an immutable candidate identity."""
    actor, bead, phase, reason = authorize_override(
        actor=actor, bead=bead, phase=phase, reason=reason
    )
    sha = sha.strip()
    tree = tree.strip()
    if not sha or not tree:
        raise OverrideRefused(
            f"validation override [{phase}] requires an exact candidate SHA and tree"
        )
    return OneShotOverride(actor, bead, phase, reason, sha, tree)


def authorize_override(
    *, actor: str, bead: str, phase: str, reason: str
) -> tuple[str, str, str, str]:
    """Validate the human authority and immutable non-candidate scope of an override."""
    actor = actor.strip()
    bead = bead.strip()
    reason = reason.strip()
    if not reason:
        raise OverrideRefused("--override-validation requires a non-empty reason")
    if not actor or work_guards.names_a_seat(actor):
        raise OverrideRefused(
            "validation override is operator-only; use a supervised human identity, not "
            f"agent seat {actor!r}"
        )
    if not bead:
        raise OverrideRefused("validation override requires one exact bead")
    if phase not in {"submit", "merge", "molecule"}:
        raise OverrideRefused(f"validation override does not support phase {phase!r}")
    return actor, bead, phase, reason


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
    reason: str = "",
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
        reason=reason,
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
    otel.record_validation_bypass_event(
        {
            "bh.hive": hive,
            "bh.bead": bead or "",
            "bh.actor": resolved_actor,
            "bh.work.phase": phase,
            "bh.validation.bypass.source": source,
            "bh.validation.bypass.reason": reason,
            "bh.validation.sha": sha,
            "bh.validation.tree": tree,
            "bh.validation.command": command,
        }
    )
    if emit:
        typer.echo(result.message)
    return result


def record_override(
    cfg,
    entry,
    override: OneShotOverride,
    *,
    sha: str,
    tree: str,
    command: str,
    branch: str | None = None,
    audit: Callable[[dict], bool] | None = None,
) -> BypassedValidation:
    """Consume one exact override after refusing candidate drift and persisting bead audit."""
    if sha != override.sha or tree != override.tree:
        raise OverrideRefused(
            f"stale validation override [{override.phase}] for {override.bead}: expected "
            f"{override.sha[:12]}/{override.tree[:12]}, found {sha[:12]}/{tree[:12]}"
        )
    event = {
        "event": "validation_override",
        "status": "BYPASSED",
        "actor": override.actor,
        "reason": override.reason,
        "bead": override.bead,
        "phase": override.phase,
        "sha": sha,
        "tree": tree,
        "command": command,
    }
    if audit is not None and not audit(event):
        raise BypassAuditError(
            f"validation override [{override.phase}] for {override.bead} was not applied: "
            "bead audit write failed"
        )
    return record(
        cfg,
        entry,
        phase=override.phase,
        command=command,
        bead=override.bead,
        sha=sha,
        tree=tree,
        branch=branch,
        source="one-shot-operator-override",
        actor=override.actor,
        reason=override.reason,
    )


def write_bead_event(bd, main, event: dict) -> bool:
    """Append the structured override decision to the bead's durable comment stream."""
    payload = json.dumps(event, sort_keys=True, separators=(",", ":"))
    result = bd.run(
        ["comments", "add", event["bead"], f"bh:validation-override {payload}"],
        main,
        actor=event["actor"],
    )
    return result.returncode == 0


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
