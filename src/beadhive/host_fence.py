"""The **epoch fence** — ``refs/bh/epoch`` beside a hive's own data (bh-ytbb.7).

The enforcement half of the multi-host write model
(``docs/design/multi-host-model-adr.md``, Amendment 1 §2). The **host lease**
(:mod:`beadhive.host_lease`, in HQ) says who *should* be primary; this ref is the remote
compare-and-swap token every Beadhive-managed data push must reserve first.

The original design coupled ``refs/dolt/data`` and this ref in one atomic Git push. Current
``bd`` does invoke real Git from the located transport repo, but deliberately supplies
``core.hooksPath=/dev/null`` and owns a transient data ref that disappears when the call
returns. Beadhive therefore cannot join the two refs or intercept that push. The strongest
available boundary is deliberately honest and fail-closed: reserve the fence by remote CAS,
run ``bd dolt push``, then verify that the exact reservation is still current. A stale host
loses before data is attempted; a takeover in the CAS-to-push window can still race, and the
postflight detects that data may already have landed. Raw ``bd dolt push`` is outside this
boundary entirely. Doctor and the ADR expose both limitations; neither hooks nor a local ref
are represented as authority.

``refs/bh/epoch`` lives OUTSIDE ``refs/dolt/data``: it is a sibling ref, not a row inside the
database, so it never participates in a Dolt merge and can never be "resolved" by a
cell-level merge policy into something both hosts think they hold. Where a hive's remote
cannot take custom refs at all, its bead data cannot live there either (``refs/dolt/data`` is
itself a custom ref), so fence and data are co-located by necessity, not preference.

**Fence record.** ``{"epoch": int, "host_id": str, "seq": int}``:

  * ``epoch`` — the ADOPT generation, minted by :mod:`beadhive.host_lease`'s ``epoch + 1``.
    This is the fencing token ``ClaimRecord`` carries (bh-ytbb.10), so it must stay stable
    for the whole tenure.
  * ``seq``   — a per-push counter, bumped for each managed push reservation.
  * ``host_id`` — who installed it. The remote object and its sha are authoritative; the
    identity is checked against the live lease, never trusted merely because it is local.

Production ``BdEngine`` uses :func:`reserve_managed_push` and :func:`verify_managed_push`
around the opaque ``bd`` operation. (The legacy ``fenced_push`` stable-local-ref primitive was
deleted in bh-vwbxy: it had no caller.)

Typer-free; every remote interaction goes through :mod:`beadhive.gitref`'s one subprocess
seam, so tests drive scratch bare repos in a tmp dir.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import gitref, log

# The fence, deliberately a SIBLING of the data ref rather than anything under `refs/dolt/`.
EPOCH_REF = "refs/bh/epoch"

# bd/Dolt's state channel. Mirrors `engine.BdEngine.state_channel()`; callers holding a live
# Engine should pass `engine.get_engine(cfg).state_channel(cwd)` instead of relying on this
# default, so a future backend with a different channel needs no change here.
DATA_REF = "refs/dolt/data"


class FenceError(RuntimeError):
    """A fence operation could not be completed. Typer-free; the CLI maps it to exit 1."""


class FenceRejected(FenceError):
    """The remote refused the fenced push: this host's epoch is stale, so it is no longer
    permitted to write. NOT a retryable condition — re-adopt (bh-ytbb.8) or stay read-only."""


class FenceViolation(FenceError):
    """A managed push lost its reservation after data transfer began.

    Rejection happens before the data push. A violation means data may already have landed
    and the two hosts must reconcile before another write.
    """


@dataclass(frozen=True)
class EpochFence:
    """The value at ``refs/bh/epoch``."""

    epoch: int
    host_id: str
    seq: int = 0

    def to_record(self) -> dict:
        return {"epoch": self.epoch, "host_id": self.host_id, "seq": self.seq}

    @classmethod
    def from_record(cls, record: dict) -> EpochFence:
        if "epoch" not in record:
            raise ValueError("epoch-fence record missing field: epoch")
        return cls(
            epoch=int(record["epoch"]),
            host_id=str(record.get("host_id", "")),
            seq=int(record.get("seq", 0)),
        )

    def describe(self) -> str:
        return f"epoch {self.epoch} (seq {self.seq}) held by {self.host_id or '?'}"


@dataclass(frozen=True)
class PushReservation:
    """The exact remote ticket held by one Beadhive-managed ``bd dolt push``."""

    prefix: str
    held: str
    fence: EpochFence


def read_fence(
    remote: str, *, cwd: Path, epoch_ref: str = EPOCH_REF
) -> tuple[str, EpochFence | None]:
    """``(sha, fence)`` currently on `remote`; ``("", None)`` when no fence is installed."""
    sha, record = gitref.read_remote(remote, epoch_ref, cwd=cwd)
    if record is None:
        return "", None
    return sha, EpochFence.from_record(record)


def install_fence(
    remote: str,
    fence: EpochFence,
    *,
    expected: str,
    cwd: Path,
    epoch_ref: str = EPOCH_REF,
) -> str:
    """CAS `fence` onto `remote`, returning the new held sha.

    This is the *enforcement* leg of adopt (bh-ytbb.8), which runs it BEFORE recording the
    lease in HQ. Raises :class:`FenceRejected` when the CAS loses — meaning another host
    already fenced this hive and this host must not proceed to claim it."""
    result = gitref.cas(remote, epoch_ref, fence.to_record(), expected=expected, cwd=cwd)
    if not result.ok:
        raise FenceRejected(
            f"epoch fence CAS on {epoch_ref} was rejected — another host moved it out from "
            f"under this adopt. Re-read the fence and decide again; nothing was written.\n"
            f"  git: {result.detail}"
        )
    gitref.set_local(epoch_ref, result.sha, cwd=cwd)  # local ref is what the push refspec sends
    return result.sha


def reserve_managed_push(remote: str, *, cwd: Path, cfg=None) -> PushReservation | None:
    """Reserve the remote fence immediately before a managed ``bd dolt push``.

    ``None`` means the hive has never entered the multi-host model, so no fence exists to
    reserve. Once a lease exists this is fail-closed: the caller must be its live holder and
    the authoritative REMOTE fence must name the same generation and host. A forged/stale
    local ref grants no authority because it is never read here.

    The CAS bumps ``seq`` and makes the ticket single-use. Losing it raises
    :class:`FenceRejected` before the caller invokes bd, which guarantees that rejection did
    not publish data. This reservation and bd's opaque push are sequenced, not atomic; the
    mandatory postflight is :func:`verify_managed_push`.
    """
    from . import guard  # lazy: guard imports this module on other paths

    cwd = Path(cwd)
    state = guard.primary_state(cfg=cfg, hive_dir=cwd)
    if state is None:
        return None
    prefix, this_host, lease = state
    if not this_host or not lease.held_by(this_host):
        raise FenceRejected(
            f"{prefix}: managed state push refused before data transfer — this host does not "
            "hold the live host lease"
        )

    held, current = read_fence(remote, cwd=cwd)
    if current is None:
        raise FenceRejected(
            f"{prefix}: no epoch fence is installed on {remote}; managed state push refused "
            "before data transfer. Re-adopt this hive to restore the fence."
        )
    if current.epoch != lease.epoch or current.host_id != this_host:
        raise FenceRejected(
            f"{prefix}: remote epoch fence is {current.describe()}, but this host's live lease "
            f"is epoch {lease.epoch} held by {this_host}; managed state push refused before "
            "data transfer. Re-adopt or reconcile the half-state."
        )

    bumped = EpochFence(epoch=current.epoch, host_id=current.host_id, seq=current.seq + 1)
    ticket = gitref.cas(remote, EPOCH_REF, bumped.to_record(), expected=held, cwd=cwd)
    if not ticket.ok:
        raise FenceRejected(
            f"{prefix}: epoch-fence reservation lost its remote CAS; managed state push was "
            f"not attempted and no data landed.\n  git: {ticket.detail}"
        )
    gitref.set_local(EPOCH_REF, ticket.sha, cwd=cwd)
    log.get_logger(__name__).warning(
        "fence_sequenced_reservation",
        hive_prefix=prefix,
        remote=remote,
        epoch=bumped.epoch,
        seq=bumped.seq,
        reason="bd disables transport hooks; fence CAS and data push cannot be atomic",
    )
    return PushReservation(prefix=prefix, held=ticket.sha, fence=bumped)


def verify_managed_push(remote: str, *, cwd: Path, reservation: PushReservation) -> None:
    """Require the exact reservation to remain remote after bd reports push success.

    A mismatch is detected after the opaque data operation, so the exception states the
    material distinction explicitly: data may have landed. It must never be presented as a
    clean preflight refusal.
    """
    observed_sha, observed = read_fence(remote, cwd=Path(cwd))
    if observed_sha == reservation.held and observed == reservation.fence:
        return
    description = observed.describe() if observed is not None else "missing"
    raise FenceViolation(
        f"{reservation.prefix}: epoch fence changed during managed state push "
        f"(reserved {reservation.fence.describe()}, now {description}). DATA MAY HAVE LANDED; "
        "stop writes and reconcile the two hosts before retrying."
    )
