"""Typed per-hive result shared by engine-mode migration and remote relocation (bh-ksmt0.2).

One shape for "what happened to this hive": the hive, the transaction, the furthest phase
reached, the (derived) state, and the evidence paths. A relocation builds it from a ledger
replay; engine-mode migration wraps its existing :class:`~beadhive.storage_migrate.
HiveMigrationResult` without changing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .migration_ledger import DONE, PHASES, Replay

#: HiveMigrationResult.status values that mean the hive ended in a good state.
_STORAGE_OK = frozenset({"migrated", "already-migrated", "would-migrate", "no-store", "skipped"})


@dataclass(frozen=True)
class HiveMigrationOutcome:
    hive: str
    txn: str
    phase_reached: str  # furthest phase that is done ("" = none; "engine-mode" for storage)
    state: str  # the ledger-derived transaction state, or the storage status
    ok: bool
    evidence_paths: tuple[str, ...] = field(default=())
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "hive": self.hive,
            "txn": self.txn,
            "phase_reached": self.phase_reached,
            "state": self.state,
            "ok": self.ok,
            "evidence_paths": list(self.evidence_paths),
            "detail": self.detail,
        }


def from_replay(hive: str, rep: Replay, *, detail: str = "") -> HiveMigrationOutcome:
    reached = ""
    for phase in PHASES:
        if rep.phases[phase] == DONE:
            reached = phase
        else:
            break
    paths: list[str] = []
    for rec in rep.records:
        for key, value in rec.evidence.items():
            if key.endswith(("path", "paths")):
                paths.extend([value] if isinstance(value, str) else [str(v) for v in value])
    state = rep.txn_state
    return HiveMigrationOutcome(
        hive=hive,
        txn=rep.txn,
        phase_reached=reached,
        state=state,
        ok=state not in ("needs-operator", "aborted"),
        evidence_paths=tuple(dict.fromkeys(paths)),
        detail=detail,
    )


def from_storage_result(result, *, txn: str = "") -> HiveMigrationOutcome:
    """Wrap an engine-mode :class:`HiveMigrationResult` (unchanged) in the shared shape."""
    paths = tuple(p for p in (getattr(result, "backup_dir", ""), result.target_path) if p)
    return HiveMigrationOutcome(
        hive=result.hive_id,
        txn=txn,
        phase_reached="engine-mode",
        state=result.status,
        ok=result.status in _STORAGE_OK,
        evidence_paths=paths,
        detail=result.detail,
    )
