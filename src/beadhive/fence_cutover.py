"""Per-hive cutover to the in-data epoch fence, its status, and its rollback (bh-oarxp, P-M4).

ADR ``docs/design/hive-writer-partitioning-adr.md`` Decision 3 and condition 12; spike
``docs/spikes/bh-32379-writer-partitioning-migration.md`` §1 (C1–C6) and §3 (R1–R5). Driven by
the HIDDEN, TEMPORARY ``bh hive fence cutover|status|rollback`` verb, documented only in
``docs/design/hive-writer-cutover-runbook.md`` and removed (E1) once every hive is cut over.
Nothing here runs on its own: an operator invokes it on the current holder, one hive at a time.

**Cutover** (:func:`cutover`):

* **C1** — read ``refs/bh/epoch`` and HQ placement; both must name THIS host at the same epoch.
  The operator attests that no other host holds unpublished commits for the hive (the one case
  the in-data fence cannot see); this node itself must have none either (a dirty working set or
  local commits not on the remote head), because the cutover resets it to the remote head.
  Any disagreement refuses, before anything is written, with a pointer to a legacy adopt.
* **C2** — ``E = max(fence, placement)``: same holder, no bump, so in-flight claim tokens minted
  under ``E`` stay valid.
* **C3** — ONE commit from the remote head (the node is reset to it first: M1's install seeds
  whenever ``bh_writer`` is absent locally, so a lagging node would fork the fence): the fence
  tables, ignore row, ``bh_writer``/``bh_epoch_live`` at ``E``, the ``cutover-E`` sentinel and
  the guard, 44 ``bh_*`` triggers (:meth:`beadhive.fence_data.FenceNode.install`). The commit
  message carries a ``bh-cutover:`` trailer recording ``{hive, E, holder, ref sha}``.
* **C4** — the holder's ``bh_local_ident``, after the commit (its ignore row is on ``HEAD``)
  and before the push.
* **C5** — the coexistence managed path: reserve ``refs/bh/epoch`` (``seq + 1``, a CAS on the
  sha C1 read), push (never forced), verify the reservation is still the remote ref. A
  non-fast-forward means the holder wrote meanwhile: reset to the remote and redo C3.
* **C6** — verify: :func:`beadhive.fence_audit.fence_audit` clean, 44 triggers, the holder's
  guard passes (``CALL bh_guard_check()``, which writes nothing), and return the record.

Re-running a completed cutover is idempotent: it finds the remote head already cut over with
this host as writer, provisions a missing identity, and re-verifies.

**Rollback** (:func:`rollback`), on the current ``bh_writer`` holder:

* **R1** — the remote ``bh_writer`` must name this host; new adopts are stopped by the operator
  (runbook).
* **R2** — ``floor = max(dolt_history_bh_writer.epoch, bh_writer, refs/bh/epoch, placement)``
  is read BEFORE anything is dropped (the history goes with the table). The legacy carriers must
  both carry the floor at this host — otherwise the next legacy adopt could mint an epoch the
  history already saw — so a below-floor state refuses with a pointer to a coexistence adopt.
* **R3** — one commit drops the 44 triggers, the guard procedure and the three tables, keeping
  the ``dolt_ignore`` row.
* **R4** — the same managed path (reserve, push, verify). Never ``--force``, ``reset-data`` or
  a ref rewind: the ref keeps its epoch (only ``seq`` moves) and the push is a fast-forward.
* **R5** — the node drops its ``bh_local_ident``. Run on a replica after the rollback has been
  published, :func:`rollback` does only this step.

Typer-free; the hive data is a :class:`~beadhive.fence_data.FenceNode` and the two legacy
carriers are ports (:class:`LegacyRef`, :class:`PlacementReader`), so the same code runs against
the scratch clusters in tests and against a real hive's git remote and HQ.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import fence_audit as audit_mod
from . import fence_schema
from .fence_data import FenceError, FenceNode, GuardReport, SqlFailed
from .fence_schema import LOCAL_IDENT_TABLE, TRIGGER_COUNT, quote
from .host_lease_contracts import EpochFence
from .writer_adopt import DataUnreachable, PlacementView, WriterRow

__all__ = [
    "CUTOVER_COMMIT_PREFIX",
    "DEFAULT_ATTEMPTS",
    "ROLLBACK_COMMIT_PREFIX",
    "TRAILER",
    "CutoverError",
    "CutoverFailed",
    "CutoverOutcome",
    "CutoverRecord",
    "CutoverRefused",
    "FenceStatus",
    "GitLegacyRef",
    "LegacyRef",
    "LeasePlacementReader",
    "PlacementReader",
    "RefLost",
    "RollbackOutcome",
    "cutover",
    "legacy_adopt_hint",
    "parse_trailer",
    "render_status",
    "rollback",
    "status",
    "unpublished_local",
]

CUTOVER_COMMIT_PREFIX = "bh: cut over to the in-data epoch fence"
ROLLBACK_COMMIT_PREFIX = "bh: roll back the in-data epoch fence"
#: The machine-readable line both commits carry: ``bh-cutover: hive=… epoch=… holder=… ref=…``.
TRAILER = "bh-cutover:"
#: Push rounds before giving up on a holder that keeps writing during the cutover.
DEFAULT_ATTEMPTS = 3
#: ``metadata`` is bd's clone-local, unguarded table (``bh-sieai`` E6): bd rewrites it on every
#: join, so a dirty row there is not unpublished work and the reset to the remote head may drop it.
_CLONE_LOCAL_TABLES = frozenset(fence_schema.UNGUARDED_BD_TABLES) - {"child_counters"}
_TRAILER_RE = re.compile(r"^bh-cutover:\s*(?P<body>.*)$", re.MULTILINE)


# =============================================================================================
# Errors
# =============================================================================================


class CutoverError(RuntimeError):
    """The cutover or rollback could not complete. Typer-free; the CLI maps it to exit 1."""


class CutoverRefused(CutoverError):
    """A precondition failed. NOTHING was published (the node may have been reset to the
    remote head and its identity dropped, which is the pre-push rollback)."""


class CutoverFailed(CutoverError):
    """The change was published but a postcondition failed (or its state is uncertain): stop
    writes on the hive and read ``bh hive fence status`` before anything else."""


class RefLost(CutoverError):
    """The ``refs/bh/epoch`` reservation CAS lost: the ref moved since it was read."""


# =============================================================================================
# Ports
# =============================================================================================


class LegacyRef(Protocol):
    """``refs/bh/epoch`` on the hive remote (the legacy fence, maintained during coexistence)."""

    def read(self) -> tuple[str, EpochFence | None]:
        """``(sha, fence)`` on the remote; ``("", None)`` when the ref does not exist."""

    def stage(self, fence: EpochFence) -> str:
        """The sha ``fence`` will have once reserved (writes the object locally; no remote
        change), so the cutover commit can record it."""

    def reserve(self, fence: EpochFence, *, expected: str) -> str:
        """CAS the remote ref to ``fence`` iff it is still at ``expected``; the new sha.
        Raises :class:`RefLost` on a lost CAS."""


class PlacementReader(Protocol):
    """HQ placement for the hive (git: ``refs/bh/lease/<prefix>``; SQL: the lease row)."""

    def read(self) -> PlacementView | None: ...


class GitLegacyRef:
    """:class:`LegacyRef` over :mod:`beadhive.host_fence` / :mod:`beadhive.gitref`."""

    def __init__(self, *, remote: str, cwd: Path):
        self._remote, self._cwd = remote, Path(cwd)

    def read(self) -> tuple[str, EpochFence | None]:
        from . import host_fence

        return host_fence.read_fence(self._remote, cwd=self._cwd)

    def stage(self, fence: EpochFence) -> str:
        from . import gitref

        return gitref.write_object(fence.to_record(), cwd=self._cwd)

    def reserve(self, fence: EpochFence, *, expected: str) -> str:
        from . import gitref
        from .host_lease_contracts import EPOCH_REF

        result = gitref.cas(
            self._remote, EPOCH_REF, fence.to_record(), expected=expected, cwd=self._cwd
        )
        if not result.ok:
            raise RefLost(f"refs/bh/epoch reservation CAS lost: {result.detail}")
        gitref.set_local(EPOCH_REF, result.sha, cwd=self._cwd)
        return result.sha


class LeasePlacementReader:
    """:class:`PlacementReader` over :func:`beadhive.host_lease.read_record` (git or SQL HQ)."""

    def __init__(self, *, remote: str, prefix: str, cwd: Path):
        self._remote, self._prefix, self._cwd = remote, prefix, Path(cwd)

    def read(self) -> PlacementView | None:
        from . import host_lease

        sha, lease = host_lease.read_record(self._remote, self._prefix, cwd=self._cwd)
        if lease is None or lease.is_tombstone:
            return None
        return PlacementView(frame=lease.host_id, epoch=lease.epoch, token=sha)


# =============================================================================================
# Records
# =============================================================================================


@dataclass(frozen=True)
class CutoverRecord:
    """``{hive, E, cutover commit, ref sha}`` (C6), plus the holder it was seeded for."""

    hive: str
    epoch: int
    holder: str
    commit: str
    ref_sha: str

    def as_dict(self) -> dict:
        return {
            "hive": self.hive,
            "epoch": self.epoch,
            "holder": self.holder,
            "commit": self.commit,
            "ref_sha": self.ref_sha,
        }


@dataclass(frozen=True)
class CutoverOutcome:
    record: CutoverRecord
    audit: audit_mod.FenceAudit
    guard: GuardReport
    #: True when the remote head was already cut over for this host (an idempotent re-run).
    already: bool = False
    #: True when this run provisioned the holder's ``bh_local_ident``.
    provisioned: bool = False
    attempts: int = 1


@dataclass(frozen=True)
class RollbackOutcome:
    hive: str
    #: ``max(history, bh_writer, refs/bh/epoch, placement)`` read before the drop (R2); 0 when
    #: this run only did R5.
    floor: int = 0
    commit: str = ""
    ref_sha: str = ""
    #: True when the remote head carried no ``bh_writer``: only R5 (identity drop) ran.
    already: bool = False
    dropped_ident: bool = False
    attempts: int = 0

    def as_dict(self) -> dict:
        return {
            "hive": self.hive,
            "floor": self.floor,
            "commit": self.commit,
            "ref_sha": self.ref_sha,
            "already": self.already,
            "dropped_ident": self.dropped_ident,
        }


@dataclass(frozen=True)
class FenceStatus:
    """What ``bh hive fence status`` (and ``bh doctor``) report for one hive."""

    hive: str
    cut_over: bool
    writer: WriterRow | None = None
    record: CutoverRecord | None = None
    ref_sha: str | None = None
    ref: EpochFence | None = None
    audit: audit_mod.FenceAudit | None = None
    guard: GuardReport | None = None
    ident: tuple[str, str] | None = None
    errors: tuple[str, ...] = field(default=())

    @property
    def trigger_count(self) -> int | None:
        return None if self.guard is None else self.guard.count

    def findings(self) -> list[str]:
        """Doctor-grade problems: audit findings, a short guard, unreadable parts."""
        out = list(self.errors)
        if not self.cut_over:
            return out
        if self.audit is not None:
            out += self.audit.findings()
        if self.guard is not None and not self.guard.complete:
            out.append(f"guard: {self.guard.describe()} on this node")
        return out

    def as_dict(self) -> dict:
        return {
            "hive": self.hive,
            "cut_over": self.cut_over,
            "epoch": None if self.writer is None else self.writer.epoch,
            "writer": None if self.writer is None else self.writer.frame,
            "cutover": None if self.record is None else self.record.as_dict(),
            "ref_sha": self.ref_sha,
            "ref": None if self.ref is None else self.ref.to_record(),
            "fence_audit": None if self.audit is None else self.audit.as_dict(),
            "trigger_count": self.trigger_count,
            "trigger_expected": TRIGGER_COUNT,
            "ident": None
            if self.ident is None
            else {"frame": self.ident[0], "role": self.ident[1]},
            "findings": self.findings(),
        }


# =============================================================================================
# Pure helpers
# =============================================================================================


def legacy_adopt_hint(prefix: str) -> str:
    return (
        f"converge the legacy carriers with a normal adopt on the intended holder "
        f"(`bh host lease adopt {prefix}`), then re-run the cutover there"
    )


def _trailer(*, hive: str, epoch: int, holder: str, ref_sha: str) -> str:
    return f"{TRAILER} hive={hive} epoch={int(epoch)} holder={holder} ref={ref_sha}"


def parse_trailer(message: str) -> dict[str, str]:
    """The ``key=value`` pairs of a commit message's ``bh-cutover:`` line (``{}`` if none)."""
    match = _TRAILER_RE.search(message or "")
    if match is None:
        return {}
    out: dict[str, str] = {}
    for token in match.group("body").split():
        key, sep, value = token.partition("=")
        if sep:
            out[key] = value
    return out


def _agree(
    prefix: str, host_id: str, fence: EpochFence | None, placed: PlacementView | None
) -> int:
    """C1's carrier check and C2's seed: both legacy carriers name THIS host at one epoch."""
    if fence is None:
        raise CutoverRefused(
            f"{prefix}: C1 refused — no refs/bh/epoch on the hive remote, so the hive has never "
            f"been adopted; {legacy_adopt_hint(prefix)}"
        )
    if placed is None:
        raise CutoverRefused(
            f"{prefix}: C1 refused — HQ has no live placement for the hive; "
            f"{legacy_adopt_hint(prefix)}"
        )
    if (fence.host_id, fence.epoch) != (placed.frame, placed.epoch):
        ahead = " (placement_ahead)" if placed.epoch > fence.epoch else ""
        raise CutoverRefused(
            f"{prefix}: C1 refused — the legacy carriers disagree{ahead}: refs/bh/epoch names "
            f"{fence.host_id}@{fence.epoch}, placement names {placed.frame}@{placed.epoch}; "
            f"{legacy_adopt_hint(prefix)}"
        )
    if placed.frame != host_id:
        raise CutoverRefused(
            f"{prefix}: C1 refused — only the current holder cuts a hive over: placement and "
            f"refs/bh/epoch name {placed.frame}@{placed.epoch}, this host is {host_id}. Run it "
            f"on {placed.frame}, or {legacy_adopt_hint(prefix)}"
        )
    return max(fence.epoch, placed.epoch)  # C2: equal here; never below either carrier


# =============================================================================================
# Node helpers
# =============================================================================================


def unpublished_local(node: FenceNode) -> list[str]:
    """Why resetting ``node`` to the remote head would lose work (empty: nothing would).

    Fetches first. Lists dirty working-set tables (bd's clone-local ``metadata`` excepted) and
    local ``main`` commits the remote head does not have."""
    node.fetch()
    out: list[str] = []
    dirty = sorted(
        {
            str(r.get("table_name", ""))
            for r in node.query("SELECT table_name FROM dolt_status")
            if str(r.get("table_name", "")) not in _CLONE_LOCAL_TABLES
            and not str(r.get("table_name", "")).startswith("bh_local_")
        }
    )
    if dirty:
        out.append(f"uncommitted changes in {', '.join(dirty)}")
    remote = quote(f"{node.remote}/{node.branch}")
    rows = node.query(
        f"SELECT COUNT(*) AS n FROM dolt_log({quote(node.branch)}, '--not', {remote})"
    )
    ahead = int(next(iter(rows[0].values())) or 0) if rows else 0
    if ahead:
        out.append(f"{ahead} local commit(s) on {node.branch} not on {node.remote}/{node.branch}")
    return out


def _require_published(node: FenceNode, prefix: str, step: str) -> None:
    pending = unpublished_local(node)
    if pending:
        raise CutoverRefused(
            f"{prefix}: {step} refused — this node holds unpublished work ({'; '.join(pending)}). "
            "Publish it on the managed path first (`bh hive sync --push`), then re-run; the "
            "fence step resets this node to the remote head and would discard it."
        )


def _head(node: FenceNode) -> str:
    rows = node.query("SELECT hashof('HEAD') AS h")
    return str(next(iter(rows[0].values()))) if rows else ""


def _drop_ident(node: FenceNode) -> bool:
    had = node.ident() is not None
    node.engine.execute(fence_schema.drop_ident_statements())
    return had


def _guard_passes(node: FenceNode) -> str | None:
    """``None`` when this node's guard accepts it as the writer (``CALL bh_guard_check()``,
    which reads and writes nothing else), else the refusal."""
    try:
        node.engine.execute([f"CALL {fence_schema.GUARD_PROCEDURE}()"])
    except SqlFailed as exc:
        return str(exc)
    return None


def _undo_unpublished(node: FenceNode) -> None:
    """Rollback "Φ2, before C5's push": reset to the remote head and drop the identity."""
    node.sync_to_remote()
    _drop_ident(node)


def _latest_cutover(node: FenceNode, ref: str) -> tuple[str, dict[str, str]] | None:
    """The newest ``bh: cut over …`` commit reachable from ``ref`` and its trailer."""
    rows = node.query(
        f"SELECT commit_hash, message FROM dolt_log({quote(ref)}) "
        f"WHERE message LIKE {quote(CUTOVER_COMMIT_PREFIX + '%')} LIMIT 1"
    )
    if not rows:
        return None
    return str(rows[0]["commit_hash"]), parse_trailer(str(rows[0].get("message") or ""))


def _record_from_history(node: FenceNode, prefix: str, ref: str) -> CutoverRecord | None:
    found = _latest_cutover(node, ref)
    if found is None:
        return None
    commit, trailer = found
    try:
        epoch = int(trailer.get("epoch", "0"))
    except ValueError:
        epoch = 0
    return CutoverRecord(
        hive=trailer.get("hive", prefix),
        epoch=epoch,
        holder=trailer.get("holder", ""),
        commit=commit,
        ref_sha=trailer.get("ref", ""),
    )


# =============================================================================================
# C6
# =============================================================================================


def _verify(
    node: FenceNode, placement: PlacementView | None, *, prefix: str, host_id: str
) -> tuple[audit_mod.FenceAudit, GuardReport]:
    try:
        audit = audit_mod.fence_audit(node, placement=placement)
        guard = node.guard_report()
    except Exception as exc:  # noqa: BLE001 — published: an unreadable check is a failure
        raise CutoverFailed(
            f"{prefix}: C6 verification could not read the fence ({exc}). Stop writes on the "
            "hive and read `bh hive fence status`."
        ) from exc
    problems = list(audit.findings())
    if not audit.cut_over:
        problems.append("the remote head carries no bh_writer")
    elif audit.writer_frame != host_id:
        problems.append(f"the remote bh_writer names {audit.writer_frame}, not {host_id}")
    if guard.count != TRIGGER_COUNT or not guard.complete:
        problems.append(f"guard: {guard.describe()}")
    refusal = _guard_passes(node)
    if refusal is not None:
        problems.append(f"the holder's guard refuses it: {refusal[:200]}")
    if problems:
        raise CutoverFailed(
            f"{prefix}: C6 verification failed — " + "; ".join(problems) + ". Stop writes on "
            "the hive and read `bh hive fence status` (the runbook's recovery section)."
        )
    return audit, guard


# =============================================================================================
# Cutover
# =============================================================================================


def cutover(
    node: FenceNode,
    placement: PlacementReader,
    ref: LegacyRef,
    *,
    prefix: str,
    host_id: str,
    others_published: bool,
    attempts: int = DEFAULT_ATTEMPTS,
    before_push: Callable[[], None] | None = None,
) -> CutoverOutcome:
    """C1–C6 on the current holder. Idempotent on re-run. ``before_push`` is a test seam
    (one-shot, between the reservation and the push)."""
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1 (got {attempts})")
    if not others_published:
        raise CutoverRefused(
            f"{prefix}: C1 refused — confirm that no OTHER host holds unpublished commits for "
            "this hive (commits made before the cutover carry no marks, so the in-data fence "
            "cannot see them). Check every replica, then pass --others-published."
        )
    try:
        remote = node.remote_writer()
    except (FenceError, DataUnreachable) as exc:
        raise CutoverRefused(f"{prefix}: C1 refused — cannot read the remote head: {exc}") from exc
    if remote is not None:
        return _already_cut_over(node, placement, remote, prefix=prefix, host_id=host_id)

    try:
        ref_sha, fence = ref.read()  # C1: both legacy carriers
        placed = placement.read()
    except CutoverError:
        raise
    except Exception as exc:  # noqa: BLE001 — an unreadable carrier is a C1 refusal
        raise CutoverRefused(f"{prefix}: C1 refused — cannot read a legacy carrier: {exc}") from exc
    epoch = _agree(prefix, host_id, fence, placed)  # C1 + C2
    assert fence is not None
    try:
        _require_published(node, prefix, "C1")
    except (FenceError, DataUnreachable) as exc:
        raise CutoverRefused(f"{prefix}: C1 refused — {exc}") from exc

    for attempt in range(1, attempts + 1):
        try:
            outcome = _cutover_round(
                node,
                ref,
                prefix=prefix,
                host_id=host_id,
                epoch=epoch,
                fence=fence,
                ref_sha=ref_sha,
                before_push=before_push,
            )
        except (FenceError, DataUnreachable) as exc:
            _undo_quietly(node)
            raise CutoverRefused(f"{prefix}: {exc}; nothing was published") from exc
        before_push = None
        if isinstance(outcome, _Raced):  # lost the push race: redo C3 from the new head
            ref_sha, fence = outcome.ref_sha, outcome.fence
            continue
        held, commit = outcome
        audit, guard = _verify(node, placed, prefix=prefix, host_id=host_id)  # C6
        return CutoverOutcome(
            record=CutoverRecord(prefix, epoch, host_id, commit, held),
            audit=audit,
            guard=guard,
            provisioned=True,
            attempts=attempt,
        )
    raise CutoverRefused(
        f"{prefix}: C5 lost {attempts} push rounds to concurrent writes; nothing was "
        "published. Quiesce the hive's writers and re-run."
    )


@dataclass(frozen=True)
class _Raced:
    """A C5 push that lost a fast-forward race; the ref as re-read afterwards."""

    ref_sha: str
    fence: EpochFence


def _cutover_round(
    node: FenceNode,
    ref: LegacyRef,
    *,
    prefix: str,
    host_id: str,
    epoch: int,
    fence: EpochFence,
    ref_sha: str,
    before_push: Callable[[], None] | None,
) -> tuple[str, str] | _Raced:
    """One C3–C5 round. ``(held_sha, commit)`` when published; :class:`_Raced` (the re-read
    ref) when the push lost a fast-forward race and the round must be redone."""
    node.sync_to_remote()  # C3 starts from the remote head (never a lagging node)
    if node.writer() is not None:
        raise CutoverRefused(
            f"{prefix}: the remote head became cut over during this run — read "
            "`bh hive fence status`, then re-run the cutover to converge"
        )
    reserved = EpochFence(epoch=fence.epoch, host_id=fence.host_id, seq=fence.seq + 1)
    reserved_sha = ref.stage(reserved)
    message = f"{CUTOVER_COMMIT_PREFIX} at {epoch}\n\n" + _trailer(
        hive=prefix, epoch=epoch, holder=host_id, ref_sha=reserved_sha
    )
    report = node.install(  # C3: one commit
        host_id,
        epoch,
        message=message,
        seed_extra=fence_schema.cutover_sentinel_statements(epoch),
    )
    if not report.seeded or (report.writer.frame, report.writer.epoch) != (host_id, epoch):
        _undo_unpublished(node)
        raise CutoverRefused(
            f"{prefix}: C3 did not seed bh_writer at {host_id}@{epoch} (found "
            f"{report.writer.frame}@{report.writer.epoch}); nothing was published"
        )
    commit = _head(node)
    node.provision_ident(host_id)  # C4: after the commit, before the push

    try:  # C5: reserve, push, verify
        held = ref.reserve(reserved, expected=ref_sha)
    except RefLost as exc:
        _undo_unpublished(node)
        raise CutoverRefused(
            f"{prefix}: C5 refused — {exc}. refs/bh/epoch moved since C1 (an adopt or another "
            "managed push); nothing was published. Re-run the cutover."
        ) from exc
    if before_push is not None:
        before_push()
    try:
        pushed = node.push()
    except DataUnreachable as exc:
        raise _failed_push(node, prefix, exc) from exc
    if not pushed:  # non-fast-forward: the holder wrote meanwhile
        _undo_unpublished(node)
        now_sha, now = ref.read()
        if now is None or (now.host_id, now.epoch) != (host_id, epoch):
            raise CutoverRefused(
                f"{prefix}: C5 push lost a race and refs/bh/epoch now names "
                f"{'nothing' if now is None else now.describe()}; nothing was published. "
                f"{legacy_adopt_hint(prefix)}"
            )
        return _Raced(now_sha, now)
    now_sha, _now = ref.read()
    if now_sha != held:
        raise CutoverFailed(
            f"{prefix}: C5 verify failed — refs/bh/epoch moved during the push (reserved "
            f"{held[:12]}, now {now_sha[:12] or 'missing'}). The cutover commit may have "
            "landed: stop writes and read `bh hive fence status`."
        )
    return held, commit


def _undo_quietly(node: FenceNode) -> None:
    """Best-effort pre-push undo after an unexpected local failure (never masks it)."""
    try:
        if node.remote_writer() is None:
            _undo_unpublished(node)
    except Exception:  # noqa: BLE001 — the original failure is what the operator must see
        pass


def _failed_push(node: FenceNode, prefix: str, exc: Exception) -> CutoverError:
    """A push that failed for a reason other than a lost race: if the remote head is not cut
    over, nothing landed — undo locally and refuse; otherwise it is uncertain — fail."""
    try:
        landed = node.remote_writer() is not None
    except (FenceError, DataUnreachable):
        return CutoverFailed(
            f"{prefix}: C5 push failed ({exc}) and the remote head cannot be read back; the "
            "cutover may or may not have landed. Read `bh hive fence status` before anything "
            "else."
        )
    if landed:
        return CutoverFailed(
            f"{prefix}: C5 push reported a failure ({exc}) but the remote head is cut over. "
            "Re-run the cutover to verify (it is idempotent)."
        )
    _undo_unpublished(node)
    return CutoverRefused(f"{prefix}: C5 push failed ({exc}); nothing was published")


def _already_cut_over(
    node: FenceNode,
    placement: PlacementReader,
    remote: WriterRow,
    *,
    prefix: str,
    host_id: str,
) -> CutoverOutcome:
    if remote.frame != host_id:
        raise CutoverRefused(
            f"{prefix}: already cut over — the remote bh_writer names "
            f"{remote.frame}@{remote.epoch}, not this host. Nothing to do here: read "
            "`bh hive fence status`; moving the writer is an adopt, not a cutover."
        )
    _require_published(node, prefix, "cutover re-run")
    node.sync_to_remote()
    provisioned = False
    if node.ident() is None:
        node.provision_ident(host_id)  # C4 for a re-run that crashed before it
        provisioned = True
    placed = placement.read()
    audit, guard = _verify(node, placed, prefix=prefix, host_id=host_id)
    record = _record_from_history(node, prefix, "HEAD") or CutoverRecord(
        prefix, remote.epoch, host_id, "", ""
    )
    return CutoverOutcome(
        record=record, audit=audit, guard=guard, already=True, provisioned=provisioned
    )


# =============================================================================================
# Status
# =============================================================================================


def status(
    node: FenceNode,
    *,
    prefix: str,
    placement: PlacementView | None = None,
    ref: LegacyRef | None = None,
) -> FenceStatus:
    """Read-only: the remote head's fence, the cutover record, ``refs/bh/epoch`` (when ``ref``
    is given), :func:`~beadhive.fence_audit.fence_audit` against ``placement`` and this node's
    trigger count. Moves nothing but remote-tracking refs; a part that cannot be read becomes
    an error line, never an exception."""
    errors: list[str] = []
    ref_sha: str | None = None
    fence: EpochFence | None = None
    if ref is not None:
        try:
            ref_sha, fence = ref.read()
        except Exception as exc:  # noqa: BLE001 — status reports, it never crashes on a read
            errors.append(f"refs/bh/epoch unreadable: {exc}")
    try:
        audit = audit_mod.fence_audit(node, placement=placement)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"fence_audit failed: {exc}")
        return FenceStatus(prefix, cut_over=False, ref_sha=ref_sha, ref=fence, errors=tuple(errors))
    if not audit.cut_over:
        ident = None
        try:
            ident = node.ident()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"identity unreadable: {exc}")
        if ident is not None:
            errors.append(
                f"{LOCAL_IDENT_TABLE} is still provisioned on a hive that is not cut over — "
                "rollback R5 drops it (`bh hive fence rollback`)"
            )
        return FenceStatus(
            prefix,
            cut_over=False,
            ref_sha=ref_sha,
            ref=fence,
            audit=audit,
            ident=ident,
            errors=tuple(errors),
        )
    writer = WriterRow(audit.writer_frame, audit.writer_epoch)
    record = guard = ident = None
    try:
        record = _record_from_history(node, prefix, audit.head)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"cutover record unreadable: {exc}")
    try:
        guard = node.guard_report()
        ident = node.ident()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"local guard unreadable: {exc}")
    return FenceStatus(
        prefix,
        cut_over=True,
        writer=writer,
        record=record,
        ref_sha=ref_sha,
        ref=fence,
        audit=audit,
        guard=guard,
        ident=ident,
        errors=tuple(errors),
    )


def render_status(d: dict) -> list[str]:
    """Text lines for one :meth:`beadhive.fence_cutover.FenceStatus.as_dict` payload."""
    hive = d["hive"]
    if not d["cut_over"]:
        out = [f"{hive}: not cut over (legacy fence: lease + refs/bh/epoch)"]
    else:
        rec = d.get("cutover") or {}
        audit = d.get("fence_audit") or {}
        out = [
            f"{hive}: cut over — bh_writer {d['writer']}@{d['epoch']}",
            f"  cutover         epoch {rec.get('epoch', '?')}, commit "
            f"{rec.get('commit') or '?'}, recorded ref {rec.get('ref_sha') or '?'}",
            f"  fence_audit     stale_marks={audit.get('stale_marks', '?')} "
            f"epoch_regressed={audit.get('epoch_regressed', '?')} "
            f"placement_ahead={audit.get('placement_ahead', '?')} "
            f"late_writes={len(audit.get('late_writes') or [])}",
            f"  triggers        {d['trigger_count']} of {d['trigger_expected']} on this node",
            f"  identity        {(d.get('ident') or {}).get('frame') or 'unprovisioned'}",
        ]
    if d.get("ref_sha") is not None:
        ref = d.get("ref") or {}
        out.append(
            f"  refs/bh/epoch   {d['ref_sha'] or 'absent'}"
            + (f" ({ref.get('host_id')}@{ref.get('epoch')}, seq {ref.get('seq')})" if ref else "")
        )
    out += [f"  ✗ {f}" for f in d.get("findings") or []]
    return out


# =============================================================================================
# Rollback
# =============================================================================================


def rollback(
    node: FenceNode,
    placement: PlacementReader,
    ref: LegacyRef,
    *,
    prefix: str,
    host_id: str,
    attempts: int = DEFAULT_ATTEMPTS,
) -> RollbackOutcome:
    """R1–R5 on the current ``bh_writer`` holder; on a node whose remote head is already
    rolled back, only R5 (drop this node's identity)."""
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1 (got {attempts})")
    try:
        remote = node.remote_writer()
        if remote is None:  # R5 on a replica (or a re-run on the holder)
            return RollbackOutcome(prefix, already=True, dropped_ident=_drop_ident(node))
        if remote.frame != host_id:  # R1
            raise CutoverRefused(
                f"{prefix}: R1 refused — take the rollback on the current bh_writer holder "
                f"({remote.frame}@{remote.epoch}), not {host_id}"
            )
        _require_published(node, prefix, "R1")
        for attempt in range(1, attempts + 1):
            node.sync_to_remote()
            writer = node.writer()
            if writer is None:
                return RollbackOutcome(prefix, already=True, dropped_ident=_drop_ident(node))
            ref_sha, fence = ref.read()
            placed = placement.read()
            floor = _floor(node, writer, fence, placed, prefix=prefix, host_id=host_id)  # R2
            assert fence is not None
            reserved = EpochFence(epoch=fence.epoch, host_id=fence.host_id, seq=fence.seq + 1)
            reserved_sha = ref.stage(reserved)
            message = f"{ROLLBACK_COMMIT_PREFIX} (floor {floor})\n\n" + _trailer(
                hive=prefix, epoch=floor, holder=host_id, ref_sha=reserved_sha
            )
            node.engine.execute(fence_schema.rollback_statements())  # R3: one commit
            if not node.engine.commit(message):
                raise CutoverFailed(f"{prefix}: R3 dropped nothing — the fence is not installed")
            commit = _head(node)
            try:  # R4: reserve, push (fast-forward only), verify
                held = ref.reserve(reserved, expected=ref_sha)
            except RefLost as exc:
                node.sync_to_remote()
                raise CutoverRefused(
                    f"{prefix}: R4 refused — {exc}. refs/bh/epoch moved since R2; nothing was "
                    "published. Re-run the rollback."
                ) from exc
            try:
                pushed = node.push()
            except DataUnreachable as exc:
                raise CutoverFailed(
                    f"{prefix}: R4 push failed after refs/bh/epoch was reserved ({exc}); re-run "
                    "the rollback (it re-reads the remote head and is idempotent)"
                ) from exc
            if not pushed:
                node.sync_to_remote()
                continue
            return _rollback_published(
                node, ref, prefix=prefix, floor=floor, commit=commit, held=held, attempt=attempt
            )
        raise CutoverRefused(
            f"{prefix}: R4 lost {attempts} push rounds to concurrent writes; nothing was "
            "published. Quiesce the hive's writers and re-run."
        )
    except (FenceError, DataUnreachable) as exc:
        try:
            if node.remote_writer() is not None:
                node.sync_to_remote()  # drop an unpublished R3 commit
        except Exception:  # noqa: BLE001 — the original failure is what the operator must see
            pass
        raise CutoverRefused(f"{prefix}: {exc}; nothing was published") from exc


def _rollback_published(
    node: FenceNode,
    ref: LegacyRef,
    *,
    prefix: str,
    floor: int,
    commit: str,
    held: str,
    attempt: int,
) -> RollbackOutcome:
    """R4's verify and R5 on the holder, after the rollback commit was pushed."""
    try:
        now_sha, _now = ref.read()
        still_cut_over = node.remote_writer() is not None
    except Exception as exc:  # noqa: BLE001 — published: an unreadable check is a failure
        raise CutoverFailed(
            f"{prefix}: R4 pushed the rollback but could not verify it ({exc}); read "
            "`bh hive fence status` before the next adopt"
        ) from exc
    if now_sha != held:
        raise CutoverFailed(
            f"{prefix}: R4 verify failed — refs/bh/epoch moved during the push (reserved "
            f"{held[:12]}, now {now_sha[:12] or 'missing'}). Stop writes and reconcile before "
            "the next adopt."
        )
    if still_cut_over:
        raise CutoverFailed(f"{prefix}: R4 pushed but the remote head still has bh_writer")
    try:
        dropped = _drop_ident(node)  # R5 on the holder
    except Exception as exc:  # noqa: BLE001
        raise CutoverFailed(
            f"{prefix}: the rollback is published, but R5 could not drop this node's "
            f"{LOCAL_IDENT_TABLE} ({exc}); re-run `bh hive fence rollback` here"
        ) from exc
    return RollbackOutcome(
        prefix, floor=floor, commit=commit, ref_sha=held, dropped_ident=dropped, attempts=attempt
    )


def _floor(
    node: FenceNode,
    writer: WriterRow,
    fence: EpochFence | None,
    placed: PlacementView | None,
    *,
    prefix: str,
    host_id: str,
) -> int:
    """R2: the epoch floor, read BEFORE the drop; refuse unless both legacy carriers name this
    host AT the floor (so the next legacy adopt, ``max(ref, placement) + 1``, is above it)."""
    if writer.frame != host_id:
        raise CutoverRefused(
            f"{prefix}: R1 refused — bh_writer names {writer.frame}@{writer.epoch}, not {host_id}"
        )
    floor = max(
        node.history_max_epoch(),
        writer.epoch,
        fence.epoch if fence is not None else 0,
        placed.epoch if placed is not None else 0,
    )
    carriers = {
        "refs/bh/epoch": None if fence is None else (fence.host_id, fence.epoch),
        "placement": None if placed is None else (placed.frame, placed.epoch),
        "bh_writer": (writer.frame, writer.epoch),
    }
    below = {
        name: seen for name, seen in carriers.items() if seen is None or seen != (host_id, floor)
    }
    if below:
        shown = ", ".join(
            f"{name} {'missing' if seen is None else f'{seen[0]}@{seen[1]}'}"
            for name, seen in sorted(below.items())
        )
        raise CutoverRefused(
            f"{prefix}: R2 refused — below-floor state: the floor is {floor} but {shown}. The "
            f"legacy carriers must carry the floor at this host before the fence is dropped. "
            f"Run a coexistence adopt (`bh host lease adopt {prefix}`) so placement, "
            "refs/bh/epoch and bh_writer converge above it, then re-run the rollback."
        )
    return floor
