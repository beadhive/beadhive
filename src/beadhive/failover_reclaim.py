"""Failover reclaim in the adopt bump commit (bh-4z2rx, M3; M14 D5a).

Binding conditions 7, 17 and 18 of ``docs/design/hive-writer-partitioning-adr.md`` and the
reclaim table D5a of ``docs/spikes/bh-55vvh-state-work-pairing.md``. bd's worker leases are
node-local (``bh-cvk70`` E19), so after the PRIMARY dies the new primary holds no lease rows and
the dead frame's claims strand. A **failover** adopt — never a planned handoff (D5c) — therefore
reclaims them inside its own bump commit:

* only claims whose ``claim-frame`` names the dead frame (the ``bh_writer`` the bump replaces)
  are in scope; another frame's claim (row 5) and an unattributed claim (row 6) are never
  touched;
* the plan is computed per step-2 round from the synced head plus ONE fetch of the dead frame's
  backup refs, and its statements ride in the bump commit. A non-fast-forward retry recomputes
  from the new head and a new fetch; a re-run after the bump landed stops at step 2's data check
  before anything is computed. That is the "exactly once, idempotent, keyed to the bump" rule
  (``bh-jbb6r`` E5);
* the per-row outcome follows M14 D5a (see :func:`decide`).

Everything is gated by the hive's own policy (``bh.reclaim.failover.mode``, D8): ``off``
computes and writes nothing, ``report`` computes the plan and writes nothing, ``apply`` writes it
— and ``apply`` degrades to ``report`` unless ``bh.pairing.enabled`` is on
(:meth:`~beadhive.work_pairing_policy.PairingPolicy.reclaim_mode`).

Typer-free, and talks to the outside only through two ports so the table is provable with fakes
and against the composed Dolt prototype alike:

* :class:`ReclaimData` — the hive's data on the adopter's synced ``main`` (the bh.* policy rows
  and the ``in_progress`` claims). The product adapter is M1's (``bh-uz46l``); a
  :class:`~beadhive.writer_adopt.FenceData` that also implements this port switches reclaim on
  for the coexistence adopt (:func:`for_fence_data`), so until M1 lands it is dormant.
* :class:`BackupProbe` — the D3 recoverability check over ``refs/bh/backup/<bead>/<frame>``
  (:class:`GitBackupProbe` is the product one, built on :mod:`beadhive.work_backup`).
"""

from __future__ import annotations

import datetime as _dt
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import log, work_backup, work_pairing_policy

__all__ = [
    "AUTHOR_PREFIX",
    "MODE_APPLY",
    "MODE_OFF",
    "MODE_REPORT",
    "Backup",
    "BackupProbe",
    "Claim",
    "FailoverReclaim",
    "GitBackupProbe",
    "Outcome",
    "ReclaimData",
    "ReclaimPlan",
    "Row",
    "audit_author",
    "decide",
    "for_fence_data",
    "reclaim_statements",
]

MODE_OFF = "off"
MODE_REPORT = "report"
MODE_APPLY = "apply"
#: The audit identity of every reclaim write: ``ops/adopt@<new frame>`` (D5a).
AUTHOR_PREFIX = "ops/adopt@"
#: Review states that mean "submitted": a handoff, not work in progress (D5a row 1).
SUBMITTED_REVIEW = frozenset({"pending", "approved"})


class Outcome(StrEnum):
    """One claim's D5a row."""

    SUBMITTED = "submitted"  # row 1: untouched
    VIOLATION = "pairing-violation"  # row 1, backup missing: untouched, flagged
    RESUMABLE = "resumable"  # row 2: open, marked recovery=resumable
    REWOUND = "rewound"  # row 3: exactly what `bd unclaim --force` leaves
    SUSPECT = "suspect"  # D3 strict signatures: retained for the operator, untouched
    PENDING = "reclaim-pending"  # row 4: backup check failed, untouched this run
    OTHER_FRAME = "other-frame"  # row 5: never touched by this adopt
    UNATTRIBUTED = "unattributed"  # row 6: no claim-frame, untouched

    @property
    def writes(self) -> bool:
        return self in (Outcome.RESUMABLE, Outcome.REWOUND)


# =============================================================================================
# Records and ports
# =============================================================================================


@dataclass(frozen=True)
class Claim:
    """One ``in_progress`` bead as the synced head carries it."""

    bead: str
    assignee: str = ""
    labels: frozenset[str] = frozenset()
    #: The sha recorded at submit (``review`` reason ``submitted <sha>``), when the adapter can
    #: read it; D3's stronger test for submitted beads checks the backup contains it.
    submitted_sha: str = ""

    def state(self, dimension: str) -> str:
        prefix = f"{dimension}:"
        for label in sorted(self.labels):
            if label.startswith(prefix):
                return label[len(prefix) :]
        return ""

    @property
    def claim_frame(self) -> str:
        return self.state(work_backup.CLAIM_FRAME_DIMENSION)

    @property
    def submitted(self) -> bool:
        return self.state("review") in SUBMITTED_REVIEW


@dataclass(frozen=True)
class Backup:
    """D3 for one (bead, dead frame): ``status`` is ``recoverable`` / ``unbacked`` / ``suspect``.

    ``sha`` is empty when the ref is absent. ``covers_submitted`` is ``None`` when no submitted
    sha was known, else whether the tip contains it."""

    status: str
    ref: str = ""
    sha: str = ""
    detail: str = ""
    covers_submitted: bool | None = None


@runtime_checkable
class ReclaimData(Protocol):
    """The hive's data on the adopter's local ``main``, right after step 2's sync."""

    def config_rows(self) -> Mapping[str, str]:
        """Every ``bh.``-prefixed config row (the pairing / reclaim policy lives there)."""

    def claims(self) -> Sequence[Claim]:
        """Every ``in_progress`` bead, with its labels."""


class BackupProbe(Protocol):
    """One fetch of the dead frame's backup refs (D3)."""

    def probe(
        self,
        claims: Sequence[Claim],
        dead_frame: str,
        policy: work_pairing_policy.PairingPolicy,
    ) -> Mapping[str, Backup] | None:
        """``{bead: Backup}`` for ``claims``; ``None`` when the fetch failed (unknown)."""

    def delete(self, ref: str, sha: str, policy: work_pairing_policy.PairingPolicy) -> bool:
        """CAS-delete an empty backup ref (row 3). ``False`` when it did not happen."""


# =============================================================================================
# The table (pure)
# =============================================================================================


@dataclass(frozen=True)
class Row:
    bead: str
    outcome: Outcome
    assignee: str = ""
    claim_frame: str = ""
    ref: str = ""
    sha: str = ""
    detail: str = ""

    def describe(self) -> str:
        where = f" {self.ref}@{self.sha[:12]}" if self.ref and self.sha else ""
        why = f" ({self.detail})" if self.detail else ""
        return f"{self.bead}: {self.outcome}{where}{why}"


def _same_frame(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        return work_backup.segment(a) == work_backup.segment(b)
    except ValueError:
        return False


def decide(
    claims: Sequence[Claim], dead_frame: str, backups: Mapping[str, Backup] | None
) -> list[Row]:
    """M14 D5a for every ``in_progress`` claim. ``backups is None`` is a failed fetch.

    | claim-frame | state / backup                         | outcome                        |
    |-------------|----------------------------------------|--------------------------------|
    | dead frame  | submitted, backup holds the work       | ``submitted`` (untouched)      |
    | dead frame  | submitted, backup missing              | ``pairing-violation`` (flag)   |
    | dead frame  | not submitted, recoverable             | ``resumable``                  |
    | dead frame  | not submitted, absent or tip in base   | ``rewound``                    |
    | dead frame  | not submitted, strict & unsigned       | ``suspect`` (untouched)        |
    | dead frame  | fetch failed                           | ``reclaim-pending`` (untouched)|
    | other frame | —                                      | ``other-frame`` (untouched)    |
    | none        | —                                      | ``unattributed`` (untouched)   |
    """
    rows: list[Row] = []
    for claim in sorted(claims, key=lambda c: c.bead):
        frame = claim.claim_frame
        base = {"bead": claim.bead, "assignee": claim.assignee, "claim_frame": frame}
        if not frame:
            rows.append(Row(outcome=Outcome.UNATTRIBUTED, **base))
            continue
        if not _same_frame(frame, dead_frame):
            rows.append(Row(outcome=Outcome.OTHER_FRAME, **base))
            continue
        backup = None if backups is None else backups.get(claim.bead)
        where = {"ref": backup.ref, "sha": backup.sha} if backup is not None else {}
        if claim.submitted:
            if backups is None:
                rows.append(
                    Row(outcome=Outcome.SUBMITTED, detail="backup unverified: fetch failed", **base)
                )
            elif backup is None or not backup.sha:
                rows.append(
                    Row(outcome=Outcome.VIOLATION, detail="submitted with no backup ref", **base)
                )
            elif backup.covers_submitted is False:
                rows.append(
                    Row(
                        outcome=Outcome.VIOLATION,
                        detail=f"backup does not contain submitted {claim.submitted_sha[:12]}",
                        **base,
                        **where,
                    )
                )
            else:
                rows.append(Row(outcome=Outcome.SUBMITTED, **base, **where))
            continue
        if backups is None:
            rows.append(Row(outcome=Outcome.PENDING, detail="backup fetch failed", **base))
            continue
        status = backup.status if backup is not None else work_backup.UNBACKED
        detail = backup.detail if backup is not None else "no backup ref"
        if status == work_backup.RECOVERABLE:
            rows.append(Row(outcome=Outcome.RESUMABLE, **base, **where))
        elif status == work_backup.SUSPECT:
            rows.append(Row(outcome=Outcome.SUSPECT, detail=detail, **base, **where))
        elif status == work_backup.UNBACKED:
            rows.append(Row(outcome=Outcome.REWOUND, detail=detail, **base, **where))
        else:  # anything unrecognised never licenses a rewind
            rows.append(Row(outcome=Outcome.PENDING, detail=f"backup {status}", **base))
    return rows


# =============================================================================================
# The writes (pure)
# =============================================================================================


def _sql_text(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def audit_author(frame: str) -> str:
    return f"{AUTHOR_PREFIX}{frame}"


def _audit_text(row: Row, *, dead_frame: str, frame: str, epoch: int, at: str) -> str:
    where = f"; work {row.ref}@{row.sha}" if row.outcome is Outcome.RESUMABLE else ""
    if row.outcome is Outcome.REWOUND and row.ref and row.sha:
        where = f"; empty backup {row.ref}@{row.sha} (already in base) deleted"
    held = f" held by {row.assignee}" if row.assignee else ""
    why = (
        "its work is recoverable from the remote backup and is left resumable"
        if row.outcome is Outcome.RESUMABLE
        else "no recoverable work on the remote: rewound to the pre-claim state"
    )
    return (
        f"bh reclaim ({row.outcome}): failover adopt at epoch {epoch} by frame {frame} "
        f"reclaimed this claim{held} from dead frame {dead_frame} — {why}{where}. "
        f"At {at} UTC (M14 D5a)."
    )


def reclaim_statements(
    rows: Sequence[Row],
    *,
    dead_frame: str,
    frame: str,
    epoch: int,
    at: str,
    new_id: Callable[[], str] | None = None,
) -> list[str]:
    """The bump commit's reclaim writes for the ``rewound`` / ``resumable`` rows.

    Both rows leave the lifecycle fields exactly as ``bd unclaim --force`` does (``status =
    'open'``, ``assignee = ''``, ``started_at = NULL``) and clear ``claim-frame``. A resumable
    row also sets ``recovery:resumable`` (replacing any older ``recovery`` value). Each row gets
    one audit comment authored ``ops/adopt@<frame>`` (who, when, why, and the ``<ref>@<sha>`` of
    a resumable row's work). bd's ``events`` table is node-local (not in the versioned data a
    new primary pushes), so the audit lives only in the comment and the bump commit's history,
    as D5a says. Every UPDATE is guarded on
    ``status = 'in_progress'`` so a plan can never reopen a bead that moved on."""
    new_id = new_id or (lambda: str(uuid.uuid4()))
    author = _sql_text(audit_author(frame))
    when = _sql_text(at)
    out: list[str] = []
    for row in rows:
        if not row.outcome.writes:
            continue
        bead = _sql_text(row.bead)
        out += [
            "UPDATE issues SET status = 'open', assignee = '', started_at = NULL "
            f"WHERE id = {bead} AND status = 'in_progress'",
            f"DELETE FROM labels WHERE issue_id = {bead} AND label LIKE "
            f"{_sql_text(work_backup.CLAIM_FRAME_DIMENSION + ':%')}",
        ]
        if row.outcome is Outcome.RESUMABLE:
            label = f"{work_backup.RECOVERY_DIMENSION}:{work_backup.RESUMABLE}"
            out += [
                f"DELETE FROM labels WHERE issue_id = {bead} AND label LIKE "
                f"{_sql_text(work_backup.RECOVERY_DIMENSION + ':%')}",
                f"INSERT INTO labels (issue_id, label) VALUES ({bead}, {_sql_text(label)})",
            ]
        text = _audit_text(row, dead_frame=dead_frame, frame=frame, epoch=epoch, at=at)
        out.append(
            "INSERT INTO comments (id, issue_id, author, text, created_at) VALUES "
            f"({_sql_text(new_id())}, {bead}, {author}, {_sql_text(text)}, {when})"
        )
    return out


# =============================================================================================
# The plan one step-2 round carries
# =============================================================================================


@dataclass(frozen=True)
class ReclaimPlan:
    """What one round decided. ``statements`` is empty unless ``mode == apply``."""

    mode: str
    dead_frame: str
    frame: str
    epoch: int
    at: str = ""
    rows: tuple[Row, ...] = ()
    statements: tuple[str, ...] = ()
    #: Why nothing was computed (mode off, planned handoff, self re-adopt, a read error).
    skipped: str = ""
    error: str = ""

    @property
    def applied(self) -> bool:
        return bool(self.statements)

    def by_outcome(self, outcome: Outcome) -> list[Row]:
        return [r for r in self.rows if r.outcome is outcome]

    def describe(self) -> list[str]:
        """Operator lines for the adopt output (``report`` and ``apply`` alike)."""
        head = f"failover reclaim [{self.mode}] of dead frame {self.dead_frame or '-'}"
        if self.skipped:
            return [f"{head}: skipped — {self.skipped}"]
        if self.error:
            return [f"{head}: not computed — {self.error} (re-run the adopt or the D5b sweep)"]
        verb = "applied in the bump commit" if self.applied else "report only, nothing written"
        lines = [f"{head}: {len(self.rows)} in_progress claim(s), {verb}"]
        lines += [
            f"  {row.describe()}" for row in self.rows if row.outcome is not Outcome.OTHER_FRAME
        ]
        others = len(self.by_outcome(Outcome.OTHER_FRAME))
        if others:
            lines.append(f"  {others} claim(s) of other frames left untouched")
        return lines

    def to_json(self) -> dict:
        return {
            "mode": self.mode,
            "dead_frame": self.dead_frame,
            "frame": self.frame,
            "epoch": self.epoch,
            "at": self.at,
            "applied": self.applied,
            "skipped": self.skipped,
            "error": self.error,
            "rows": [
                {
                    "bead": r.bead,
                    "outcome": str(r.outcome),
                    "assignee": r.assignee,
                    "claim_frame": r.claim_frame,
                    "ref": r.ref,
                    "sha": r.sha,
                    "detail": r.detail,
                }
                for r in self.rows
            ],
        }


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


@dataclass
class FailoverReclaim:
    """The reclaim hook :func:`beadhive.writer_adopt.run_step2` calls once per round.

    ``failover`` is the adopt's kind: ``True`` for a failover (reclaim per policy), ``False``
    for a planned handoff (D5c: applies nothing), ``None`` when the kind is unknown (a resumed
    adopt whose displaced placement is gone): nothing is written and the plan says so."""

    data: ReclaimData
    probe: BackupProbe
    failover: bool | None = True
    clock: Callable[[], _dt.datetime] = _utc_now
    new_id: Callable[[], str] | None = None
    deleted: list[str] = field(default_factory=list)

    def with_kind(self, failover: bool | None) -> FailoverReclaim:
        return FailoverReclaim(self.data, self.probe, failover, self.clock, self.new_id)

    def plan(self, *, dead_frame: str, frame: str, epoch: int) -> ReclaimPlan:
        """Compute this round's plan from the synced head and one backup fetch. Never raises:
        a failure reads as "not computed", which writes nothing (unknown never rewinds)."""
        try:
            policy = work_pairing_policy.parse(self.data.config_rows())
        except Exception as exc:  # noqa: BLE001 — an unreadable policy is the off state
            return ReclaimPlan(MODE_OFF, dead_frame, frame, epoch, error=f"policy: {exc}")
        mode = policy.reclaim_mode("failover")
        at = self.clock().astimezone(_dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
        plan = ReclaimPlan(mode, dead_frame, frame, epoch, at)
        if mode == MODE_OFF:
            return _replace(plan, skipped="bh.reclaim.failover.mode is off")
        if self.failover is False:
            return _replace(plan, skipped="planned handoff (D5c applies nothing)")
        if self.failover is None:
            return _replace(
                plan,
                skipped="adopt kind unknown on a resumed adopt; the D5b sweep or the manual "
                "runbook (D10) reclaims instead",
            )
        if not dead_frame or _same_frame(dead_frame, frame):
            return _replace(plan, skipped="no other frame is displaced by this bump")
        try:
            claims = list(self.data.claims())
            scoped = [c for c in claims if _same_frame(c.claim_frame, dead_frame)]
            backups = self.probe.probe(scoped, dead_frame, policy) if scoped else {}
        except Exception as exc:  # noqa: BLE001 — unknown never rewinds (D3)
            return _replace(plan, error=str(exc))
        rows = tuple(decide(claims, dead_frame, backups))
        statements: tuple[str, ...] = ()
        if mode == MODE_APPLY:
            statements = tuple(
                reclaim_statements(
                    rows, dead_frame=dead_frame, frame=frame, epoch=epoch, at=at, new_id=self.new_id
                )
            )
        return _replace(plan, rows=rows, statements=statements)

    def after_landed(self, plan: ReclaimPlan) -> None:
        """Row 3's "delete an empty backup ref", once this run's bump has landed. Best-effort:
        a leftover ref is already in base and is reaped by D7's coverage rule anyway."""
        if not plan.applied:
            return
        try:
            policy = work_pairing_policy.parse(self.data.config_rows())
        except Exception:  # noqa: BLE001
            return
        for row in plan.by_outcome(Outcome.REWOUND):
            if not (row.ref and row.sha):
                continue
            try:
                if self.probe.delete(row.ref, row.sha, policy):
                    self.deleted.append(row.ref)
            except Exception as exc:  # noqa: BLE001
                log.get_logger(__name__).warning(
                    "failover_reclaim_delete_failed", ref=row.ref, error=str(exc)
                )


def _replace(plan: ReclaimPlan, **changes) -> ReclaimPlan:
    return replace(plan, **changes)


# =============================================================================================
# The product backup probe and the data switch
# =============================================================================================


@dataclass
class GitBackupProbe:
    """D3 over the hive clone at ``cwd``: one pruning fetch of the scoped beads' backup refs plus
    the integration base, then :func:`beadhive.work_backup.classify` per tip.

    ``remote`` defaults to ``pairing.remote`` and then ``default_remote``."""

    cwd: Path
    default_remote: str = "origin"
    integration: str = "main"

    def _remote(self, policy) -> str:
        return policy.remote or self.default_remote

    def probe(self, claims, dead_frame, policy):
        remote = self._remote(policy)
        beads = sorted({c.bead for c in claims})
        if not beads:
            return {}
        ok, _why = work_backup.fetch_backups(self.cwd, remote, beads, self.integration)
        if not ok:
            return None
        base = work_backup._local_sha(
            self.cwd, f"{work_backup.BASE_NS}/{work_backup.segment(remote)}/{self.integration}"
        )
        mine = work_backup.segment(dead_frame)
        out: dict[str, Backup] = {}
        for claim in claims:
            ref = work_backup.backup_ref(claim.bead, dead_frame)
            tip = work_backup.fetched_refs(self.cwd, claim.bead).get(mine, "")
            if not tip:
                out[claim.bead] = Backup(work_backup.UNBACKED, ref, "", "no backup ref")
                continue
            status, detail = work_backup.classify(self.cwd, tip, base, policy.signature_policy)
            covers = None
            if claim.submitted_sha:
                covers = work_backup.is_ancestor(self.cwd, claim.submitted_sha, tip)
            out[claim.bead] = Backup(status, ref, tip, detail, covers)
        return out

    def delete(self, ref, sha, policy) -> bool:
        return work_backup.delete_backup(self.cwd, self._remote(policy), ref, sha)


def for_fence_data(
    data: object, *, cwd: Path, failover: bool | None = True
) -> FailoverReclaim | None:
    """The reclaim hook for a coexistence adopt, or ``None`` (dormant) when the hive's fence
    adapter does not also read claims and policy (:class:`ReclaimData`)."""
    if not isinstance(data, ReclaimData):
        return None
    return FailoverReclaim(data, GitBackupProbe(Path(cwd)), failover)
