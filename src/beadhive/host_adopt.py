"""Two-phase, fail-closed adopt — fence first, lease second (bh-ytbb.8).

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

from dataclasses import dataclass
from pathlib import Path

from . import host_fence, host_lease, log
from .host_fence import EpochFence
from .host_lease import HostLease, HostLeaseRejected


class AdoptError(RuntimeError):
    """Adopt could not be completed. Typer-free; the CLI maps it to exit 1."""


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
) -> AdoptOutcome:
    """Become primary for `prefix`: CAS the hive-side epoch **fence** first, then record the
    **lease** in HQ.

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
    if lease is not None and not lease.is_expired(at) and lease.host_id != host_id and not force:
        raise HostLeaseRejected(
            f"{prefix} is held by another host — host lease: {lease.describe()}.\n"
            f"  Wait for it to expire, have that host release it, or force a takeover "
            f"(dangerous: `--force` is how split-brain happens — ADR Limitation 3)."
        )

    epoch = _next_epoch(fence, lease)

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
