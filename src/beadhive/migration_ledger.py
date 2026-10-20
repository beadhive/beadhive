"""Append-only, hash-chained phase ledger for a remote-relocation transaction.

Schema ``bh.remote-migration-ledger/1`` (docs/design/remote-migration-transaction-adr.md,
section 3). One JSON-lines file per transaction. Every record carries ``seq``, ``txn``,
``phase``, ``state``, ``ts``, ``actor``, ``host`` (the lease holder; another host appending is a
fault), an ``inputs`` digest, ``evidence`` (paths and digests,
never secrets) and ``prev`` -- the sha256 of the previous record's canonical form, so truncation
and edits are detectable. Each append is a single ``O_APPEND`` write followed by ``fsync``.

State is DERIVED by :func:`replay`; there is no mutable status field. This module is a primitive:
it knows phases, states and the ADR invariants, and nothing about how a phase is executed. It is
not wired into any verb (phase B composes it).

Invariants enforced here (ADR section 3):
  3. ``done`` requires recorded evidence.
  5. A ``running`` phase found on resume becomes ``failed`` (:meth:`PhaseLedger.plan_resume`).
  6. Resume never skips forward; every ``done`` phase is re-verified before it is skipped.
  7. ``cutover`` / ``retire`` are never auto-retried: they surface as ``needs-operator``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "bh.remote-migration-ledger/1"
GENESIS = "0" * 64

PHASES = (
    "assess",
    "quiesce",
    "snapshot",
    "attest-backup",
    "stage",
    "hydrate",
    "verify",
    "cutover",
    "prove",
    "hold",
    "retire",
)
#: Never auto-retried after a partial success (invariant 7).
IRREVERSIBLE = frozenset({"cutover", "retire"})
#: Out-of-order pseudo-phase: the operator/executor gave up on the transaction.
ABORT = "abort"

PENDING, RUNNING, DONE, FAILED, ROLLED_BACK, SKIPPED = (
    "pending",
    "running",
    "done",
    "failed",
    "rolled-back",
    "skipped",
)
STATES = (PENDING, RUNNING, DONE, FAILED, ROLLED_BACK, SKIPPED)

_ALLOWED = {
    PENDING: {RUNNING, SKIPPED},
    RUNNING: {DONE, FAILED},
    FAILED: {RUNNING, ROLLED_BACK},
    DONE: {ROLLED_BACK, FAILED},  # FAILED from DONE = a resumed run invalidated it
    ROLLED_BACK: {RUNNING},
    SKIPPED: set(),
}

#: Derived transaction state = the furthest ``done`` phase.
_TXN_STATE = {
    "assess": "planned",
    "quiesce": "quiesced",
    "snapshot": "snapshotted",
    "attest-backup": "backup-attested",
    "stage": "staged",
    "hydrate": "staged",
    "verify": "verified",
    "cutover": "cut-over",
    "prove": "proven",
    "hold": "held",
    "retire": "retired",
}

_CRED_URL = re.compile(r"://[^/\s@:]+:[^/\s@]+@")


class LedgerError(Exception):
    """Base: the ledger refuses; the message carries recovery guidance."""


class LedgerCorrupt(LedgerError):
    """The file is damaged, edited, truncated or out of order."""


class LedgerIncompatible(LedgerError):
    """The file was written by a different schema version."""


class LedgerFault(LedgerError):
    """An append would violate an ADR invariant (non-holder writer, no evidence, skip-forward,
    an illegal state transition, or a secret in evidence)."""


_RECOVERY = (
    "Do not edit or delete the ledger. Preserve it with the transaction's snapshot and backup "
    "set, then either restore the ledger from the copy kept beside the migrate backup set or "
    "start a new transaction (the source remote is untouched before cutover). If the failure "
    "is after cutover, take the registry snapshot and go to the operator ('needs-operator')."
)


def canonical(obj) -> str:
    """Key-sorted, compact, UTF-8-safe JSON: the one serialization every digest is taken over."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_of(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Record:
    seq: int
    txn: str
    phase: str
    state: str
    ts: str
    actor: str
    host: str
    inputs: str
    evidence: Mapping
    prev: str
    schema: str = SCHEMA

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "seq": self.seq,
            "txn": self.txn,
            "phase": self.phase,
            "state": self.state,
            "ts": self.ts,
            "actor": self.actor,
            "host": self.host,
            "inputs": self.inputs,
            "evidence": dict(self.evidence),
            "prev": self.prev,
        }

    def digest(self) -> str:
        return digest_of(self.to_dict())


@dataclass(frozen=True)
class Replay:
    """The state derived from a ledger by replaying it."""

    txn: str
    records: tuple[Record, ...]
    phases: Mapping[str, str]  # phase -> latest state (every PHASES key present)
    last_digest: str  # digest of the final record (GENESIS when empty)

    @property
    def aborted(self) -> bool:
        return any(r.phase == ABORT and r.state == DONE for r in self.records)

    def evidence(self, phase: str) -> Mapping:
        for rec in reversed(self.records):
            if rec.phase == phase and rec.state == DONE:
                return rec.evidence
        return {}

    @property
    def needs_operator(self) -> bool:
        """An irreversible phase that is not cleanly ``done``/``pending``/``rolled-back`` was
        interrupted or failed -- it must not be auto-retried (invariant 7)."""
        return any(self.phases[p] in (RUNNING, FAILED) and self._ever_ran(p) for p in IRREVERSIBLE)

    def _ever_ran(self, phase: str) -> bool:
        return any(r.phase == phase and r.state == RUNNING for r in self.records)

    @property
    def txn_state(self) -> str:
        if self.needs_operator:
            return "needs-operator"
        if self.aborted:
            return "aborted"
        state = "planned"
        for phase in PHASES:
            if self.phases[phase] == DONE:
                state = _TXN_STATE[phase]
            else:
                break
        return state

    @property
    def next_phase(self) -> str | None:
        """First phase that is not ``done``/``skipped`` (resume never skips forward)."""
        for phase in PHASES:
            if self.phases[phase] not in (DONE, SKIPPED):
                return phase
        return None


def _parse(line: str, lineno: int) -> Record:
    try:
        raw = json.loads(line)
    except ValueError as exc:
        raise LedgerCorrupt(
            f"ledger line {lineno} is not valid JSON ({exc}); a torn or edited write. {_RECOVERY}"
        ) from None
    if not isinstance(raw, dict):
        raise LedgerCorrupt(f"ledger line {lineno} is not an object. {_RECOVERY}")
    schema = raw.get("schema")
    if schema != SCHEMA:
        raise LedgerIncompatible(
            f"ledger line {lineno} has schema {schema!r}, this bh reads {SCHEMA!r}. Use the bh "
            "release that wrote it to inspect it; do not rewrite it."
        )
    try:
        return Record(
            seq=int(raw["seq"]),
            txn=str(raw["txn"]),
            phase=str(raw["phase"]),
            state=str(raw["state"]),
            ts=str(raw["ts"]),
            actor=str(raw["actor"]),
            host=str(raw["host"]),
            inputs=str(raw["inputs"]),
            evidence=dict(raw["evidence"]),
            prev=str(raw["prev"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LedgerCorrupt(
            f"ledger line {lineno} is missing/invalid {exc!r}. {_RECOVERY}"
        ) from None


def replay(path: Path) -> Replay:
    """Read and verify *path* end to end; derive state. Absent file = empty ledger. Raises
    :class:`LedgerCorrupt` / :class:`LedgerIncompatible` (never repairs)."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Replay("", (), {p: PENDING for p in PHASES}, GENESIS)
    except OSError as exc:
        raise LedgerCorrupt(f"cannot read ledger {path}: {exc}. {_RECOVERY}") from None
    if text and not text.endswith("\n"):
        raise LedgerCorrupt(f"ledger {path} ends in a torn (unterminated) record. {_RECOVERY}")
    records: list[Record] = []
    prev = GENESIS
    phases = {p: PENDING for p in PHASES}
    txn = ""
    for lineno, line in enumerate(text.splitlines(), 1):
        rec = _parse(line, lineno)
        if rec.seq != len(records) + 1:
            raise LedgerCorrupt(
                f"ledger line {lineno}: seq {rec.seq}, expected {len(records) + 1} "
                f"(a record was removed or reordered). {_RECOVERY}"
            )
        if rec.prev != prev:
            raise LedgerCorrupt(
                f"ledger line {lineno}: hash chain broken (a record before it was edited, "
                f"truncated or replaced). {_RECOVERY}"
            )
        if txn and rec.txn != txn:
            raise LedgerCorrupt(f"ledger line {lineno}: mixed transactions. {_RECOVERY}")
        if rec.state not in STATES or (rec.phase not in PHASES and rec.phase != ABORT):
            raise LedgerCorrupt(
                f"ledger line {lineno}: unknown phase/state {rec.phase!r}/{rec.state!r}. "
                f"{_RECOVERY}"
            )
        txn = rec.txn
        if rec.phase in phases:
            phases[rec.phase] = rec.state
        records.append(rec)
        prev = rec.digest()
    return Replay(txn, tuple(records), phases, prev)


def _check_no_secret(value, where="evidence") -> None:
    if isinstance(value, str):
        if _CRED_URL.search(value):
            raise LedgerFault(f"{where} contains a URL with embedded credentials; redact it first")
    elif isinstance(value, Mapping):
        for k, v in value.items():
            _check_no_secret(v, f"{where}.{k}")
    elif isinstance(value, list | tuple):
        for item in value:
            _check_no_secret(item, where)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ResumeVerdict:
    ok: bool
    detail: str = ""


#: Re-verifies a ``done`` phase against reality before a resumed run may skip it.
Verifier = Callable[[Record], ResumeVerdict]


@dataclass
class ResumePlan:
    next_phase: str | None  # None: nothing left to run, or needs_operator
    needs_operator: bool = False
    invalidated: list[str] = field(default_factory=list)  # done phases that failed re-check
    recovered: list[str] = field(default_factory=list)  # running phases turned failed
    detail: str = ""


class PhaseLedger:
    """Writer/reader for one transaction's ledger file.

    ``holder`` is the identity that owns the per-hive lock; when given, an append by any other
    ``actor`` is a fault (invariant 1). ``clock`` is injectable so tests never read real time.
    """

    def __init__(
        self,
        path: Path,
        txn: str,
        *,
        actor: str,
        holder: str | None = None,
        host: str = "",
        clock: Callable[[], datetime] = _now,
    ):
        self.path = Path(path)
        self.txn = txn
        self.actor = actor
        self.holder = holder
        self.host = host
        self._clock = clock

    @classmethod
    def path_for(cls, home: Path, hive: str, txn: str) -> Path:
        return Path(home) / "migrations" / hive / txn / "ledger.jsonl"

    def state(self) -> Replay:
        rep = replay(self.path)
        if rep.records and rep.txn != self.txn:
            raise LedgerCorrupt(f"{self.path} belongs to transaction {rep.txn!r}. {_RECOVERY}")
        return rep

    def append(
        self, phase: str, state: str, *, evidence: Mapping | None = None, inputs=None
    ) -> Record:
        if self.holder is not None and self.actor != self.holder:
            raise LedgerFault(
                f"{self.actor!r} is not the lock holder {self.holder!r}; a record by a "
                "non-holder is a fault (single writer)"
            )
        rep = self.state()
        if rep.records and rep.records[-1].host != self.host:
            raise LedgerFault(
                f"ledger is owned by host {rep.records[-1].host!r}, not {self.host!r}; a "
                "cross-host append is a fault (the hive lease holder drives the transaction)"
            )
        if state not in STATES:
            raise LedgerFault(f"unknown state {state!r}")
        evidence = dict(evidence or {})
        _check_no_secret(evidence)
        if phase == ABORT:
            if state != DONE:
                raise LedgerFault("abort is recorded as state 'done'")
            if rep.aborted:
                raise LedgerFault("transaction already aborted")
        else:
            if phase not in PHASES:
                raise LedgerFault(f"unknown phase {phase!r}")
            if rep.aborted:
                raise LedgerFault("transaction is aborted; start a new one")
            cur = rep.phases[phase]
            if state not in _ALLOWED[cur]:
                raise LedgerFault(f"phase {phase}: {cur} -> {state} is not a legal transition")
            if state in (RUNNING, SKIPPED):
                for earlier in PHASES[: PHASES.index(phase)]:
                    if rep.phases[earlier] not in (DONE, SKIPPED):
                        raise LedgerFault(
                            f"phase {phase} cannot start: {earlier} is {rep.phases[earlier]}; "
                            "resume never skips forward"
                        )
            if state == DONE and not evidence:
                raise LedgerFault(f"phase {phase} cannot be done without gate evidence")
        rec = Record(
            seq=len(rep.records) + 1,
            txn=self.txn,
            phase=phase,
            state=state,
            ts=self._clock().astimezone(UTC).isoformat(),
            actor=self.actor,
            host=self.host,
            inputs=digest_of(inputs) if inputs is not None else "",
            evidence=evidence,
            prev=rep.last_digest,
        )
        self._write(rec)
        return rec

    def _write(self, rec: Record) -> None:
        line = (canonical(rec.to_dict()) + "\n").encode("utf-8")
        new_file = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            written = os.write(fd, line)
            if written != len(line):  # pragma: no cover - local fs short write
                raise LedgerCorrupt(f"short ledger write to {self.path}. {_RECOVERY}")
            os.fsync(fd)
        finally:
            os.close(fd)
        if new_file:
            dfd = os.open(self.path.parent, os.O_RDONLY)
            try:
                with contextlib.suppress(OSError):
                    os.fsync(dfd)
            finally:
                os.close(dfd)

    def plan_resume(self, verifiers: Mapping[str, Verifier]) -> ResumePlan:
        """Turn ``running`` into ``failed`` (invariant 5), re-verify every ``done`` phase with its
        verifier before it may be skipped (a missing verifier refuses), and return the phase to
        continue at. Irreversible phases found interrupted yield ``needs_operator``."""
        rep = self.state()
        plan = ResumePlan(next_phase=None)
        if rep.aborted:
            plan.detail = "transaction aborted"
            return plan
        for phase in PHASES:
            if rep.phases[phase] == RUNNING:
                self.append(phase, FAILED, evidence={"reason": "found running on resume"})
                plan.recovered.append(phase)
        rep = self.state()
        if rep.needs_operator:
            plan.needs_operator = True
            plan.detail = "an irreversible phase was interrupted; never auto-retried"
            return plan
        for phase in PHASES:
            if rep.phases[phase] == SKIPPED:
                continue
            if rep.phases[phase] != DONE:
                plan.next_phase = phase
                return plan
            verifier = verifiers.get(phase)
            if verifier is None:
                raise LedgerFault(
                    f"phase {phase} is done but no verifier was supplied; a completed phase is "
                    "verified before a resumed run skips it"
                )
            record = next(r for r in reversed(rep.records) if r.phase == phase and r.state == DONE)
            verdict = verifier(record)
            if not verdict.ok:
                self.append(
                    phase, FAILED, evidence={"reason": verdict.detail or "re-verify failed"}
                )
                plan.invalidated.append(phase)
                for later in PHASES[PHASES.index(phase) + 1 :]:
                    if rep.phases[later] == DONE:  # built on a phase that no longer holds
                        self.append(later, FAILED, evidence={"reason": f"{phase} invalidated"})
                        plan.invalidated.append(later)
                plan.next_phase = phase
                plan.detail = verdict.detail
                if any(p in IRREVERSIBLE for p in plan.invalidated):
                    plan.needs_operator = True
                    plan.next_phase = None
                return plan
        return plan
