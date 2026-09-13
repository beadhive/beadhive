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

The older :func:`fenced_push` primitive remains for callers that genuinely own a stable local
data ref. Production ``BdEngine`` cannot use it; it uses :func:`reserve_managed_push` and
:func:`verify_managed_push` around the opaque ``bd`` operation instead.

Typer-free; every remote interaction goes through :mod:`beadhive.gitref`'s one subprocess
seam, so tests drive scratch bare repos in a tmp dir.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import dolt_health, gitref, log, store_locator
from .gitref import GIT_TIMEOUT, RemoteUnreachable
from .run import run

# The fence, deliberately a SIBLING of the data ref rather than anything under `refs/dolt/`.
EPOCH_REF = "refs/bh/epoch"

# bd/Dolt's state channel. Mirrors `engine.BdEngine.state_channel()`; callers holding a live
# Engine should pass `engine.get_engine(cfg).state_channel(cwd)` instead of relying on this
# default, so a future backend with a different channel needs no change here.
DATA_REF = "refs/dolt/data"

# The ref a capability probe pushes to under --dry-run. Never actually created (dry-run
# transfers nothing) — verified in tests/test_host_fence.py.
_PROBE_REF = "refs/bh/atomic-probe"

# Where Dolt stages its git transport, relative to ONE DATABASE directory (see
# `transport_lookup` — measured, not assumed). Mode moves the parent above it; the layout from
# the database down is identical, which is the whole reason this is one glob and not two.
_TRANSPORT_UNDER_DB = ".dolt/git-remote-cache/*/repo.git"
# ...and the same thing relative to a parent holding SEVERAL databases (embedded's `.beads/
# embeddeddolt/`, which is private to one hive — the server's parent is not, see the lookup).
_DOLT_TRANSPORT_GLOB = f"*/{_TRANSPORT_UNDER_DB}"

# What `transport_lookup` found, when it found no repos. Three genuinely different answers that
# an empty list used to collapse into one — and collapsing them is how a safety mechanism reads
# "disarmed" as "not needed" (bh-areg.6).
FOUND = "found"  # repos is non-empty
NONE = "none"  # this hive has no dolt transport at all (nodb/JSONL): nothing to fence
NOT_FOUND = "not-found"  # dolt-backed, locally reachable, but bd has not created it yet
UNREACHABLE = "unreachable"  # server mode against a NON-LOCAL server: on another machine

# Hosts that mean "the store is on this machine". Anything else is (c)-remote (`bh-3mik`): the
# transport repo lives in the server's data dir, on the server's disk, so no local hook and no
# local push can reach it (bh-ukit.2's verdict table).
_LOCAL_SERVER_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})

# git's own words when the server's receive-pack never advertised the `atomic` capability:
#   fatal: the receiving end does not support --atomic push
# Matched as a substring so a translated/reworded prefix still classifies correctly, and
# pinned by a test that drives a REAL non-advertising server (receive.advertiseAtomic=false).
ATOMIC_UNSUPPORTED_MARKER = "does not support --atomic"

# Recorded per-forge knowledge, used when a live probe cannot run (offline, or a caller that
# will not pay a round trip). A probe ALWAYS wins over this table when one is available —
# this is a documented default, not a substitute for measuring.
#
# github / gitlab  — advertise `atomic`; both run modern git server-side.
# gitea            — DETERMINED (bh-ytbb.7, see bh-aa5b.1): Gitea does NOT implement its own
#                    receive-pack. It shells out to the real `git receive-pack` binary for
#                    both transports (`routers/web/repo/githttp.go` builds
#                    `gitcmd.NewCommand("receive-pack", "--stateless-rpc", ...)`; SSH goes
#                    through `cmd/serv.go` -> the same verb), and `modules/git/git.go` pins
#                    `RequiredVersion = "2.13.0"`. `--atomic` receive-pack landed in git 2.4,
#                    and `receive.advertiseAtomic` defaults to true — so atomic push is
#                    available on EVERY supported Gitea deployment, and the only way to lose
#                    it is an admin explicitly setting `receive.advertiseAtomic=false`.
#                    Caveat, recorded because it is easy to trip over later: Gitea's AGit
#                    flow uses `proc-receive` for `refs/for/*` refspecs only — bh never
#                    pushes those, so it does not interact with the fence.
# local            — a `file://`/path remote runs the local git binary; supported.
FORGE_ATOMIC_SUPPORT: dict[str, bool] = {
    "github": True,
    "gitlab": True,
    "gitea": True,
    "local": True,
}


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
class PushOutcome:
    """Result of one fenced data push."""

    ok: bool
    atomic: bool  # whether the fence rode the SAME push as the data
    held: str  # the fence sha to pass as `<held>` next time (unchanged on failure)
    detail: str = ""


@dataclass(frozen=True)
class PushReservation:
    """The exact remote ticket held by one Beadhive-managed ``bd dolt push``."""

    prefix: str
    held: str
    fence: EpochFence


def _git(args: list[str], cwd: Path):
    return run(["git", *args], cwd=str(cwd), check=False, capture=True, timeout=GIT_TIMEOUT)


@dataclass(frozen=True)
class TransportLookup:
    """Where this hive's git transport is, or *why it isn't here* — see :data:`FOUND`,
    :data:`NONE`, :data:`NOT_FOUND`, :data:`UNREACHABLE`.

    The state is the point. ``repos == []`` used to be the whole answer and it meant three
    incompatible things at once, one of which ("the fence has nowhere to run") is a disarmed
    safety mechanism and two of which are fine. A caller that cannot tell them apart cannot
    tell a nodb hive from a broken fence."""

    repos: list[Path]
    state: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        """Whether the absence of repos (if any) is BENIGN — nothing here needs fencing, or
        bd simply hasn't staged it yet. ``UNREACHABLE`` is the one that is not."""
        return self.state in (FOUND, NONE, NOT_FOUND)


def transport_lookup(hive_dir: Path) -> TransportLookup:
    """Every bare repo Dolt uses to stage `hive_dir`'s git transport — the repos that actually
    perform the ``git push`` — plus WHY there are none when there are none.

    **Measured, not assumed.** bh-ytbb.7 measured the embedded layout against bd 1.1.0;
    bh-ukit.2 measured both modes against bd HEAD-af076b6 and bd 1.1.0. bh-tfapu then pinned
    current shipped bd with Git trace2 rather than relying on hook execution. What that
    established:

      * The push fires from a hidden bare repo at ``<db>/.dolt/git-remote-cache/<hash>/
        repo.git``, in BOTH modes — **never** from the hive's own checkout, whose hook does not
        fire for a data push at all.
      * Only the PARENT differs: ``<hive>/.beads/embeddeddolt/`` embedded, versus
        ``~/.beads/shared-server/dolt/`` under bd's shared server, where the push runs inside
        the server process. That is why the parent comes from
        :func:`store_locator.store_dir` and the glob below is parent-relative.
      * ``refs/dolt/data`` is **not** a local ref in that repo — in either mode, on either bd
        version. The local side of the push is a transient
        ``refs/dolt/blobstore/origin/dolt/data/<uuid>``. This function's previous docstring
        claimed the opposite ("the repos that actually hold a LOCAL ``refs/dolt/data``"); that
        was wrong when written, and it is why the ADR's
        ``git push --atomic … origin refs/dolt/data refs/bh/epoch`` cannot be issued by bh from
        here. See ``docs/spikes/bh-ukit.2-fence-under-a-dolt-server.md``.
      * Current bd supplies ``core.hooksPath=/dev/null`` to that exact real Git process. A hook
        installed in the located repo therefore does not fire and confers no authority.

    **Scope differs by mode, deliberately.** Embedded's parent is private to the hive, so every
    database under it belongs to this hive (this repo carries ``bh`` and a legacy ``beads``) and
    all of them are returned — the caller picks by matching a candidate's ``origin`` against the
    hive's remote, never by directory name. The server's parent is shared by EVERY hive on the
    host, so returning its whole glob would hand a caller other hives' transport repos — and
    ``prepush`` would bake this hive's id into another hive's hook. Under a server the lookup is
    therefore scoped to this hive's own database (``store_locator.database_dir``)."""
    if not (Path(hive_dir) / ".beads").is_dir():
        return TransportLookup([], NONE, f"{hive_dir} has no .beads/ — not a bd-backed hive")

    mode = store_locator.dolt_mode(hive_dir)
    if mode == "server":
        host, _port = dolt_health.server_endpoint()
        if host not in _LOCAL_SERVER_HOSTS:
            return TransportLookup(
                [],
                UNREACHABLE,
                f"this hive's store is served by {host}, not this machine — its transport repo "
                f"is on that host's disk, so no local hook or push can reach it (bh-3mik)",
            )
        root, glob = store_locator.database_dir(hive_dir), _TRANSPORT_UNDER_DB
    elif mode == "embedded" or store_locator.has_embedded_store(hive_dir):
        root, glob = store_locator.embedded_store_dir(hive_dir), _DOLT_TRANSPORT_GLOB
    else:
        return TransportLookup(
            [], NONE, f"{hive_dir} records no dolt store (nodb/JSONL) — nothing to fence"
        )

    repos = sorted(p for p in root.glob(glob) if p.is_dir())
    if repos:
        return TransportLookup(repos, FOUND)
    return TransportLookup(
        [],
        NOT_FOUND,
        f"no transport repo under {root} yet — bd creates it lazily on the first "
        f"`bd dolt push` for this hive",
    )


def transport_repos(hive_dir: Path) -> list[Path]:
    """Just the repos from :func:`transport_lookup` — the list-only form every existing caller
    already expects. Prefer the lookup itself anywhere the *reason* for an empty list matters."""
    return transport_lookup(hive_dir).repos


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


def probe_atomic(remote: str, *, cwd: Path) -> bool:
    """Whether `remote`'s receive-pack advertises the ``atomic`` capability.

    A ``--dry-run`` push: it negotiates capabilities for real but transfers nothing and
    creates nothing (asserted by test), so probing is safe against a live remote. Raises
    :class:`beadhive.gitref.RemoteUnreachable` when the probe failed for any reason OTHER
    than missing atomic support — an unreachable remote teaches us nothing, and quietly
    reading that as "no atomic" would drop the fence for the wrong reason."""
    probe_sha = gitref.write_object({"probe": "atomic"}, cwd=cwd)
    res = _git(["push", "--atomic", "--dry-run", remote, f"{probe_sha}:{_PROBE_REF}"], cwd)
    if res.returncode == 0:
        return True
    output = (res.stderr or "") + (res.stdout or "")
    if ATOMIC_UNSUPPORTED_MARKER in output:
        return False
    raise RemoteUnreachable(
        f"could not probe --atomic support on {remote}: {output.strip() or res.returncode}"
    )


def atomic_default(provider: str) -> bool:
    """The RECORDED per-forge answer for `provider` (see :data:`FORGE_ATOMIC_SUPPORT`), used
    only when a live probe is unavailable. Unknown forges default to ``False`` — the
    conservative direction, since the fallback still fences (just more weakly) whereas
    assuming atomic on a forge that lacks it would make the push die outright."""
    return FORGE_ATOMIC_SUPPORT.get(provider, False)


def fenced_push(
    remote: str,
    *,
    held: str,
    cwd: Path,
    data_ref: str = DATA_REF,
    epoch_ref: str = EPOCH_REF,
    atomic: bool | None = None,
) -> PushOutcome:
    """Legacy stable-local-ref push primitive; production ``BdEngine`` does not use it.

    `held` is the fence sha this host believes is current — the ``<held>`` in
    ``--force-with-lease=refs/bh/epoch:<held>``. It comes from the adopt that installed the
    fence, or from the previous successful push's :attr:`PushOutcome.held`.

    `atomic=None` probes the remote once (:func:`probe_atomic`); pass an explicit bool to
    reuse a cached answer and skip the round trip.

    Raises :class:`FenceRejected` when the fence is stale — with ``--atomic`` that is
    guaranteed to mean **no data landed**. That guarantee applies only to this primitive's
    stable refs, not bd's opaque transient-ref push.
    """
    if atomic is None:
        atomic = probe_atomic(remote, cwd=cwd)
    if atomic:
        return _atomic_push(remote, held=held, cwd=cwd, data_ref=data_ref, epoch_ref=epoch_ref)
    return _fallback_push(remote, held=held, cwd=cwd, data_ref=data_ref, epoch_ref=epoch_ref)


def _atomic_push(remote, *, held, cwd, data_ref, epoch_ref) -> PushOutcome:
    """The ADR's formulation verbatim: both refs in ONE ``--atomic`` push, gated by a
    ``--force-with-lease`` on the fence. Either both refs move or neither does."""
    res = _git(
        [
            "push",
            "--atomic",
            f"--force-with-lease={epoch_ref}:{held}",
            remote,
            data_ref,
            epoch_ref,
        ],
        cwd,
    )
    if res.returncode == 0:
        return PushOutcome(ok=True, atomic=True, held=held)
    detail = gitref.message(res)
    raise FenceRejected(
        f"fenced push rejected — this host's epoch fence is stale, so it may no longer write "
        f"{data_ref} (another host adopted). The push was --atomic: NO data landed.\n"
        f"  git: {detail}"
    )


def _fallback_push(remote, *, held, cwd, data_ref, epoch_ref) -> PushOutcome:
    """The legacy primitive's per-push ticket-bump fallback when ``--atomic`` is unavailable.

    Two sequenced pushes, fence FIRST:

      1. CAS the fence from `held` to a value with a bumped ``seq`` — a real ref update, so
         the CAS is genuinely exercised (a no-op ref update would be skipped by git) and the
         value we just installed becomes single-use: any other host still holding the old sha
         now loses its own CAS.
      2. Only on success, push the data.

    **Honest limit, stated rather than buried:** this narrows the unfenced window to the
    interval between (1) and (2) — it does not close it, because two remotes' worth of
    atomicity is exactly what ``--atomic`` provides and a sequence cannot synthesize. A host
    that adopts inside that window can still land data concurrently. This is a degradation,
    which is why it is *reported* (``PushOutcome.atomic is False``) and logged, never silent.

    Deviation from the acceptance wording, recorded deliberately: the ADR calls this a
    "per-push epoch-bump", and what is bumped here is the fence record's ``seq``, not its
    ``epoch``. Bumping ``epoch`` per push would invalidate the adopt-generation fencing token
    ``ClaimRecord`` carries (bh-ytbb.10) on the very first write. ``seq`` gives the required
    property — the fence VALUE (and therefore its sha, which is what the CAS compares) changes
    on every push — while leaving ``epoch`` meaning what the lease says it means.
    """
    _sha, current = read_fence(remote, cwd=cwd, epoch_ref=epoch_ref)
    if current is None:
        raise FenceRejected(
            f"no epoch fence installed at {epoch_ref} on {remote} — adopt this hive before "
            f"writing to it (the non-atomic fallback cannot install a fence mid-push)"
        )
    bumped = EpochFence(epoch=current.epoch, host_id=current.host_id, seq=current.seq + 1)
    ticket = gitref.cas(remote, epoch_ref, bumped.to_record(), expected=held, cwd=cwd)
    if not ticket.ok:
        raise FenceRejected(
            f"fenced push rejected at the fence CAS — this host's epoch fence is stale, so it "
            f"may no longer write {data_ref}. The data push was NOT attempted: no data "
            f"landed.\n  git: {ticket.detail}"
        )
    gitref.set_local(epoch_ref, ticket.sha, cwd=cwd)
    log.get_logger(__name__).warning(
        "fence_non_atomic_fallback",
        remote=remote,
        epoch=bumped.epoch,
        seq=bumped.seq,
        reason=(
            "forge does not advertise --atomic receive-pack; the fence and the data push are "
            "sequenced instead of atomic, leaving a narrow window between them"
        ),
    )
    pushed = _git(["push", remote, data_ref], cwd)
    if pushed.returncode:
        return PushOutcome(
            ok=False,
            atomic=False,
            held=ticket.sha,
            detail=(
                f"fence advanced but the data push failed: {gitref.message(pushed)} — this host "
                f"still holds the fence (retry the data push; do not re-adopt)"
            ),
        )
    return PushOutcome(ok=True, atomic=False, held=ticket.sha)
