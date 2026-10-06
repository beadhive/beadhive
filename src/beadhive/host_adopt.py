"""Adopt: legacy fence-first (bh-ytbb.8), or placement-first on a cut-over hive (bh-4c7p4).

A hive whose data carries ``bh_writer`` (cut over to the in-data epoch fence) is adopted
placement first, then by an idempotent data step 2, keeping ``refs/bh/epoch`` in lockstep while
it exists — :mod:`beadhive.writer_adopt`, ADR ``hive-writer-partitioning-adr.md`` §2. Every other
hive (Φ1, the default) takes the legacy two-phase path below UNCHANGED. Which one applies is
decided by the hive's data alone ("data is the switch", condition 12), read through the
:class:`~beadhive.writer_adopt.FenceData` port; no product adapter for that port is registered
until M1 (``bh-uz46l``) lands, so the coexistence path is dormant by construction.

Legacy: two-phase, fail-closed adopt — fence first, lease second (bh-ytbb.8).

Becoming a hive's primary touches **two remotes**: the hive's own (where the epoch fence
lives, :mod:`beadhive.host_fence`) and Factory HQ (where the host lease lives,
:mod:`beadhive.host_lease`). No git operation is atomic across two remotes, so adopt cannot
be made atomic. What *can* be chosen is the order — and the order is the safety property.

See ``docs/design/multi-host-model-adr.md``, Amendment 1 §2 ("Adopt now touches two remotes
and cannot be atomic across them, so it is ordered fence first, lease second").

Naming note (Amendment 1 §5): "lease" here is always the **host lease** (host ↔ hive), never
``bd``'s *worker* lease (worker ↔ issue).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import failover_reclaim, gitref, host_fence, host_lease, log, writer_adopt
from .host_fence import EpochFence
from .host_lease import HostLease, HostLeaseRejected
from .writer_adopt import (  # re-exported: callers catch these from here
    AdoptError,
    AdoptIncomplete,
    AdoptLost,
    EpochRefLost,
    FenceData,
    PlacementLost,
)

__all__ = [
    "AdoptError",
    "AdoptHalfDone",
    "AdoptIncomplete",
    "AdoptLost",
    "AdoptOutcome",
    "EpochRefLost",
    "HiveNotCloned",
    "PlacementLost",
    "adopt",
    "fence_data_for",
    "set_fence_data_resolver",
]


class HiveNotCloned(AdoptError):
    """This host does not carry the hive it was asked to adopt (bh-1atj).

    A PRECONDITION, refused before either remote is touched. Adopting a hive you do not have is
    never right: the lease is fleet-visible, other hosts then defer to a host that cannot serve
    it, and recovering means a forced takeover somebody has to notice is needed. Measured on
    beadhive-factory 2026-08-05, where it surfaced instead as ``[Errno 2] No such file or
    directory`` out of a ``git ls-remote`` deep in phase 0 — a benign accident of ordering,
    not the guard working."""


class AdoptHalfDone(AdoptError):
    """The fence was installed but the lease was NOT recorded.

    This is the *designed* failure state, not a corruption: with the fence held by this host
    and no lease naming it, **nobody** may write — this host's own ``guard_primary`` refuses
    it (no lease), and every other host's fence CAS now fails against the new epoch. Recover
    by re-adopting; never by hand-editing refs."""


@dataclass(frozen=True)
class AdoptOutcome:
    """A completed two-phase adopt."""

    epoch: int
    fence_sha: str  # the `<held>` value subsequent fenced pushes must present
    lease: HostLease
    #: Set when the hive is cut over and the placement-first path ran (bh-4c7p4).
    coexistence: writer_adopt.CoexistenceOutcome | None = None


# ---- the data switch (M1 seam) ---------------------------------------------------------------

#: ``(prefix, hive_dir) -> FenceData | None``. ``None`` means "no in-data fence adapter for this
#: hive", and the legacy adopt runs. The product adapter is M1's (bh-uz46l); until it registers
#: one here, every hive answers ``None`` — the coexistence path is dormant.
FenceDataResolver = Callable[[str, Path], "FenceData | None"]


def _no_fence_data(_prefix: str, _hive_dir: Path) -> FenceData | None:
    return None


_fence_data_resolver: FenceDataResolver = _no_fence_data


def set_fence_data_resolver(resolver: FenceDataResolver | None) -> None:
    """Register the in-data fence adapter (M1); ``None`` restores the dormant default."""
    global _fence_data_resolver
    _fence_data_resolver = resolver or _no_fence_data


def fence_data_for(prefix: str, hive_dir: Path) -> FenceData | None:
    """The hive's :class:`~beadhive.writer_adopt.FenceData`, or ``None`` (legacy / dormant)."""
    return _fence_data_resolver(prefix, Path(hive_dir))


class _LeasePlacement:
    """The HQ host lease as the placement authority (git HQ: ``refs/bh/lease/<prefix>``; SQL
    HQ: the protected lease row through the frame plane). Every CAS writes a new record, so the
    CAS token is always rewritten (condition 5)."""

    def __init__(self, *, remote, prefix, cwd, label, ttl, at, force):
        self._remote, self._prefix, self._cwd = remote, prefix, Path(cwd)
        self._label, self._ttl, self._at, self._force = label, ttl, at, force
        self.lease: HostLease | None = None
        self.outcome: host_lease.LeaseOutcome | None = None

    def read(self) -> writer_adopt.PlacementView | None:
        sha, lease = host_lease.read_record(self._remote, self._prefix, cwd=self._cwd)
        self.lease = lease
        if lease is None:
            return None
        return writer_adopt.PlacementView(frame=lease.host_id, epoch=lease.epoch, token=sha)

    def cas(self, frame, epoch, *, expected):
        try:
            outcome = host_lease.adopt(
                self._remote,
                self._prefix,
                host_id=frame,
                label=self._label,
                cwd=self._cwd,
                ttl=self._ttl,
                at=self._at,
                force=self._force,
                epoch=epoch,
                expected=expected.token if expected is not None else gitref.ABSENT,
            )
        except HostLeaseRejected as exc:
            raise PlacementLost(str(exc)) from exc
        self.outcome, self.lease = outcome, outcome.lease
        return writer_adopt.PlacementView(frame=frame, epoch=epoch, token=outcome.sha)


class _GitEpochRef:
    """``refs/bh/epoch`` on the hive remote, CASed by ``host_fence.install_fence``."""

    def __init__(self, *, remote, cwd):
        self._remote, self._cwd = remote, Path(cwd)

    def read(self) -> writer_adopt.RefView | None:
        sha, fence = host_fence.read_fence(self._remote, cwd=self._cwd)
        if fence is None:
            return None
        return writer_adopt.RefView(frame=fence.host_id, epoch=fence.epoch, sha=sha)

    def cas(self, frame, epoch, *, expected):
        sha = host_fence.install_fence(
            self._remote,
            EpochFence(epoch=epoch, host_id=frame),
            expected=expected.sha,
            cwd=self._cwd,
        )
        return writer_adopt.RefView(frame=frame, epoch=epoch, sha=sha)


def _next_epoch(fence: EpochFence | None, lease: HostLease | None) -> int:
    """One past the highest generation EITHER object has seen.

    Taking the max of the two — rather than ``lease.epoch + 1`` — is what makes the half-state
    recoverable. After a crash between the phases the fence is at N+1 while the lease is still
    at N; deriving from the lease alone would mint N+1 again, colliding with the orphaned
    fence's own generation and making a stale token indistinguishable from a fresh one. The
    sequence must be monotonic across BOTH objects, so recovery lands on N+2."""
    highest = max(
        fence.epoch if fence is not None else 0,
        lease.epoch if lease is not None else 0,
    )
    return highest + 1


def adopt(
    *,
    prefix: str,
    hive_remote: str,
    hq_remote: str,
    hive_cwd: Path,
    hq_cwd: Path,
    host_id: str,
    label: str,
    ttl: float = host_lease.DEFAULT_TTL,
    force: bool = False,
    at: float | None = None,
    fence_data: FenceData | None = None,
    step2_attempts: int | None = None,
    failover: bool | None = None,
) -> AdoptOutcome:
    """Become primary for `prefix`: CAS the hive-side epoch **fence** first, then record the
    **lease** in HQ — or, on a hive whose data is cut over, placement first then data.

    CUT-OVER HIVES (bh-4c7p4). When `fence_data` (default: :func:`fence_data_for`) reports a
    ``bh_writer`` on the remote head, the order inverts to placement first, ``refs/bh/epoch``
    in lockstep while it exists, then :func:`beadhive.writer_adopt.run_step2`. See
    :func:`beadhive.writer_adopt.coexistence_adopt`; its half-state is "adopt incomplete"
    (:class:`AdoptIncomplete`), recovered by re-running this adopt. When the hive's fence adapter
    also reads claims and policy (:class:`beadhive.failover_reclaim.ReclaimData`), a FAILOVER
    adopt reclaims the dead frame's claims in the bump commit per the hive's
    ``bh.reclaim.failover.mode`` (M3, M14 D5a); ``failover`` overrides the kind read from the
    displaced placement (a released one is a planned handoff). Everything below describes
    the legacy path, which every other hive keeps unchanged.

    ORDERING IS LOAD-BEARING — DO NOT "SIMPLIFY" IT TO LEASE-FIRST.
    ================================================================
    The reverse ordering (record the lease, then set the fence) is **unsafe and is rejected**.
    Both orders have the same crash window; they differ in what the window leaves behind:

      * fence → lease (this code). A crash in between leaves the fence set and the lease
        unrecorded. This host cannot write — ``guard_primary`` consults the LEASE, which does
        not name it. No other host can write either — their fence CAS now expects a superseded
        value. Net: **nobody** may write. Fail-closed, and one re-adopt clears it.
      * lease → fence (rejected). A crash in between leaves the lease naming this host while
        the fence still authorizes the PREVIOUS one. Two hosts each believe they may write:
        the new one because the lease says so, the old one because the fence still says so.
        That is split-brain — precisely the ``beads#4796`` failure this molecule exists to
        prevent (two hosts allocating the same child id, then an unresolvable PK collision on
        the next pull, with sync blocked indefinitely and heavy manual recovery).

    The asymmetry is that the fence is *enforcement* and the lease is *bookkeeping*. Setting
    enforcement before bookkeeping can only ever over-restrict; the reverse can under-restrict,
    and an under-restriction here is unrecoverable data corruption rather than an inconvenience.

    Raises :class:`AdoptHalfDone` when phase 2 fails after phase 1 succeeded (recover by
    re-adopting), :class:`beadhive.host_fence.FenceRejected` when another host won the fence,
    and :class:`beadhive.host_lease.HostLeaseRejected` when the HQ lease is held and `force`
    was not given. Never rolls the fence back on a phase-2 failure: a rollback would hand the
    write right back to a host this adopt has already superseded.

    Raises :class:`HiveNotCloned` when `hive_cwd` is not a clone on this host — see the
    precondition below."""
    from . import frame_eligibility

    frame_eligibility.require_eligible(host_id, {"prefix": prefix}, hq_dir=hq_cwd, at=at)

    # ---- precondition: this host must actually CARRY the hive (bh-1atj) --------------
    # BEFORE phase 0, so a host with nothing on it cannot reach either CAS. A skip chain
    # (`git workspace update` skipped -> `bead sync` skipped) leaves exactly that host, and
    # `_step_adopt`'s fail-closed guard does not catch it: skips are not failures, and that
    # distinction is load-bearing elsewhere. This is the narrower fix — the precondition adopt
    # actually needs, stated as one, rather than promoting every skip to a failure.
    if not (Path(hive_cwd) / ".git").exists():
        raise HiveNotCloned(
            f"{prefix}: no clone at {hive_cwd} — this host does not carry the hive.\n"
            f"  Adopting it would take a fleet-visible lease this host cannot honour. Clone it "
            f"first (`git workspace update`), then adopt."
        )

    # ---- phase 0: read both sides (free — reads are never gated) --------------------
    fence_sha, fence = host_fence.read_fence(hive_remote, cwd=hive_cwd)
    lease = host_lease.read(hq_remote, prefix, cwd=hq_cwd)

    # Refuse a live foreign lease BEFORE touching either remote, so the common "someone else
    # has it" case costs nothing and leaves no half-state at all.
    evict = (
        lease is not None
        and not lease.is_tombstone
        and not lease.is_expired(at)
        and lease.host_id != host_id
        and frame_eligibility.evictable(lease.host_id, hq_dir=hq_cwd, at=at)
    )
    if (
        lease is not None
        and not lease.is_expired(at)
        and lease.host_id != host_id
        and not force
        and not evict
    ):
        raise HostLeaseRejected(
            f"{prefix} is held by another host — host lease: {lease.describe()}.\n"
            f"  Wait for it to expire, have that host release it, or force a takeover "
            f"(dangerous: `--force` is how split-brain happens — ADR Limitation 3)."
        )

    epoch = _next_epoch(fence, lease)

    # Recheck after the remote reads, immediately before the first mutation.
    frame_eligibility.require_eligible(host_id, {"prefix": prefix}, hq_dir=hq_cwd, at=at)
    if evict and not force and not frame_eligibility.evictable(lease.host_id, hq_dir=hq_cwd, at=at):
        raise HostLeaseRejected("incumbent eviction authority changed")

    # ---- the data switch: a cut-over hive is adopted placement first (bh-4c7p4) -------
    data = fence_data if fence_data is not None else fence_data_for(prefix, Path(hive_cwd))
    if data is not None:
        try:
            return _adopt_cut_over(
                data,
                prefix=prefix,
                hive_remote=hive_remote,
                hq_remote=hq_remote,
                hive_cwd=hive_cwd,
                hq_cwd=hq_cwd,
                host_id=host_id,
                label=label,
                ttl=ttl,
                force=force,
                at=at,
                attempts=step2_attempts,
                failover=failover,
            )
        except writer_adopt.NotCutOver:
            pass  # no bh_writer on the remote head (Φ1): legacy adopt, nothing was written
    # ---- phase 1: ENFORCEMENT (hive remote) -----------------------------------------
    held = host_fence.install_fence(
        hive_remote,
        EpochFence(epoch=epoch, host_id=host_id),
        expected=fence_sha,
        cwd=hive_cwd,
    )

    # ---- phase 2: BOOKKEEPING (HQ) ---------------------------------------------------
    try:
        outcome = host_lease.adopt(
            hq_remote,
            prefix,
            host_id=host_id,
            label=label,
            cwd=hq_cwd,
            ttl=ttl,
            at=at,
            force=force,
            epoch=epoch,  # the SAME generation the fence already carries
        )
    except Exception as exc:  # noqa: BLE001 — every phase-2 failure lands in the same state
        log.get_logger(__name__).warning(
            "host_adopt_half_done",
            hive_prefix=prefix,
            host_id=host_id,
            epoch=epoch,
            reason=(
                "fence installed but the HQ host lease was not recorded — fail-closed by "
                "design (nobody may write); recover by re-adopting, never by ref surgery"
            ),
            error=str(exc),
        )
        raise AdoptHalfDone(
            f"adopted the epoch fence for {prefix} (epoch {epoch}) but failed to record the "
            f"host lease in HQ: {exc}\n"
            f"  This is fail-closed: NO host may write {prefix} until an adopt completes. "
            f"Re-run adopt once HQ is reachable — it recovers from exactly this state and "
            f"needs no manual ref surgery."
        ) from exc

    host_lease.cache(prefix, outcome, cwd=hq_cwd)
    return AdoptOutcome(epoch=epoch, fence_sha=held, lease=outcome.lease)


def _adopt_cut_over(
    data: FenceData,
    *,
    prefix: str,
    hive_remote: str,
    hq_remote: str,
    hive_cwd: Path,
    hq_cwd: Path,
    host_id: str,
    label: str,
    ttl: float,
    force: bool,
    at: float | None,
    attempts: int | None,
    failover: bool | None = None,
) -> AdoptOutcome:
    """Placement-first coexistence adopt on a cut-over hive (bh-4c7p4).

    The placement record is cached locally the moment its CAS is won, so this host's own
    ``bh doctor`` sees an "adopt incomplete" it left behind even with HQ unreachable."""
    placement = _LeasePlacement(
        remote=hq_remote, prefix=prefix, cwd=hq_cwd, label=label, ttl=ttl, at=at, force=force
    )
    ref = _GitEpochRef(remote=hive_remote, cwd=hive_cwd)

    def cache_placed(_view: writer_adopt.PlacementView) -> None:
        if placement.outcome is not None:
            host_lease.cache(prefix, placement.outcome, cwd=hq_cwd)

    result = writer_adopt.coexistence_adopt(
        data,
        placement,
        ref,
        prefix=prefix,
        frame=host_id,
        attempts=attempts,
        on_placed=cache_placed,
        reclaim=failover_reclaim.for_fence_data(data, cwd=Path(hive_cwd)),
        failover=failover,
    )
    if placement.outcome is None:  # resumed: placement already named us; mirror what we read
        host_lease.cache(
            prefix,
            host_lease.LeaseOutcome(
                lease=placement.lease, sha=result.placement.token, previous=None
            ),
            cwd=hq_cwd,
        )
    assert placement.lease is not None
    return AdoptOutcome(
        epoch=result.epoch,
        fence_sha=result.ref.sha if result.ref is not None else "",
        lease=placement.lease,
        coexistence=result,
    )
