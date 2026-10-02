"""The **host lease** — ``refs/bh/lease/<prefix>`` in the Factory HQ repo (bh-ytbb.6).

The coordination half of the multi-host write model
(``docs/design/multi-host-model-adr.md``, Amendment 1 §§1–2): *who should be primary for a
hive, with a TTL*. It is bookkeeping and scheduling — ``bh host list``, handoff, expiry — and
it is deliberately **not** remote write authority. Managed publication re-reads and
CAS-reserves the epoch fence beside the hive's data (:mod:`beadhive.host_fence`) before bd,
then verifies the exact ticket afterward. Per Amendment 1's refinement of Limitation 6, a
lease read is only a *reading with an as-of*. Current bd makes the fence/data sequence
non-atomic, so doctor exposes the residual window rather than claiming the lease makes a write
safe.

Why HQ and not the hive's own remote: a contrib repo or a fork's upstream is not the
operator's to write, so a per-hive primary ref cannot exist for a whole class of hives ``bh``
supports. HQ (``<owner>/beadhive-hq``) is the operator's by construction, so the lease
centralizes there, keyed by hive prefix (Amendment 1 §1).

**Vocabulary — host lease vs worker lease** (Amendment 1 §5). ``bd`` owns a *worker* lease:
``lease_expires_at`` on an issue, ``bd heartbeat`` / ``bd reclaim``, and the
``Lease: expires in 4 mins`` line ``bh work issue`` prints. This module's lease is a **host**
lease: host ↔ hive, not worker ↔ issue. Nothing here reads or writes bd's. Names in this
module say ``host_lease`` / ``HostLease`` rather than a bare ``lease`` for exactly that
reason, and any user-facing string qualifies it ("host lease") wherever both could appear.

All four operations are compare-and-swap through :mod:`beadhive.gitref`, which is what keeps
the HQ remote a linearization point:

===========  ===========================================================================
adopt        CAS from expired-or-absent(-or-tombstone), ``epoch + 1``. A loser is
             **rejected**, never silently retried.
renew        CAS from its own value, SAME epoch, a new ``expires_at``.
release      CAS from its own value to a tombstone (``host_id: ""``) that PRESERVES the
             epoch — see :func:`release`.
takeover     CAS from an *unexpired* value; requires ``force=True`` and logs loudly.
===========  ===========================================================================

Record shape (the acceptance bar's exact five fields, and nothing else — a tombstone is the
same shape with an empty ``host_id``)::

    {"host_id": str, "label": str, "epoch": int, "adopted_at": iso8601Z, "expires_at": iso8601Z}

Typer-free: every failure is an exception a CLI layer maps to an exit code.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import gitref, log
from .host_lease_contracts import (
    LEASE_REF_ROOT as LEASE_REF_ROOT,
)
from .host_lease_contracts import (
    HostLease as HostLease,
)
from .host_lease_contracts import (
    _parse_stamp,
)
from .host_lease_contracts import (
    lease_ref as lease_ref,
)
from .host_lease_contracts import (
    now_stamp as now_stamp,
)

# ``refs/bh/lease/<prefix>`` — outside ``refs/heads/*`` and outside ``refs/dolt/*``, so it
# never participates in a branch merge or a Dolt merge (Decision 2's "the ref lives outside
# refs/dolt/data" property, restated for HQ's own store).


# ISO-8601 UTC, matching claim_authority.py / metadata.py's stamp format.


# Amendment 1 §3 defaults: renew every 5 min, TTL 30 min. These are the *code* defaults behind
# the `host.lease.renew_interval` / `host.lease.ttl` config keys (config_schema.HostLeaseConfig)
# — declared there as the fleet-wide contract, restated here so this module is usable without a
# loaded config (tests, and the pure-logic callers below).
DEFAULT_RENEW_INTERVAL = 300.0  # seconds (5 min)
DEFAULT_TTL = 1800.0  # seconds (30 min)

# Amendment 1 §3: the TTL trade-off is answered by a host's ROLE, not by a better number. The
# configured `host.lease.ttl` is the baseline for a `transient` machine (one that comes and goes
# for a task and releases on exit — the ADR's own 30 min figure); an always-on `executor`
# multiplies it for long stable tenure, so a machine going away costs a bounded staleness window
# instead of stalling the fleet. A `viewer` never becomes primary at all (:func:`ttl_for_role`
# raises), which is the closed set's whole point. Role vocabulary: `hosts.HOST_ROLES`.
ROLE_TTL_SCALE: dict[str, float] = {
    "executor": 4.0,  # always-on, owns repos: 30 min baseline -> 2 h tenure
    "transient": 1.0,  # comes and goes for a task: the configured baseline as-is
}


class HostLeaseError(RuntimeError):
    """A host-lease operation could not be completed. Typer-free; the CLI maps it to exit 1."""


class HostLeaseRejected(HostLeaseError):
    """A compare-and-swap lost, or the operation was refused on the current lease's state.

    Raised — never swallowed into a retry loop. "The loser is rejected, not silently retried"
    is an acceptance criterion, not a style preference: a retry converts a detected race into
    an undetected one, and the whole point of routing adopt through a CAS is that exactly one
    caller learns it won."""


def ttl_for_role(role: str, base_ttl: float = DEFAULT_TTL) -> float:
    """The lease TTL a host in `role` should take, from the configured `base_ttl`
    (``host.lease.ttl``) scaled by :data:`ROLE_TTL_SCALE`.

    Raises :class:`HostLeaseRejected` for ``viewer`` — that role's definition is *never
    primary*, so the refusal belongs here (at the point tenure is computed) rather than as a
    check every call site has to remember. An unknown role falls back to the unscaled
    baseline: a manifest from a newer bh must not silently get an infinite tenure.

    Deprecated role spellings resolve first (:func:`beadhive.hosts.canonical_role`), so a host
    still registered as `worker` in HQ is refused as the `viewer` it now is rather than falling
    through the unknown-role branch and being handed a tenure it must never have."""
    from . import hosts  # lazy: hosts imports nothing from here, but keep the cycle impossible

    role = hosts.canonical_role(role)
    if role == "viewer":
        raise HostLeaseRejected(
            "this host's role is `viewer` — a viewer never becomes primary, by definition "
            "(docs/design/multi-host-model-adr.md, Amendment 1 §3). It reads, navigates and "
            "indexes locally; it does not claim, submit or merge.\n"
            "  To let this host own work, set its role in the HQ manifest first: "
            "`bh host init --role executor --force` (always-on, owns repos) or "
            "`--role transient` (comes and goes for a task)."
        )
    return base_ttl * ROLE_TTL_SCALE.get(role, 1.0)


def _frame_plane(cwd):
    from . import host, hosts
    from .hq_control_plane import control_plane

    try:
        identity = host.host_id()
        manifest = hosts.load(cwd, identity)
    except FileNotFoundError:
        return None
    if manifest.frame_id:
        return control_plane(cwd)
    return None


def _read(remote: str, prefix: str, *, cwd: Path) -> tuple[str, HostLease | None]:
    """``(sha, lease)`` currently at HQ for `prefix`; ``("", None)`` when never adopted."""
    if plane := _frame_plane(cwd):
        return plane.read_hive_lease_record(prefix)
    sha, record = gitref.read_remote(remote, lease_ref(prefix), cwd=cwd)
    if record is None:
        return "", None
    return sha, HostLease.from_record(record)


@dataclass(frozen=True)
class LeaseOutcome:
    """A won CAS: the record now at HQ, its blob sha (the value the NEXT CAS must expect),
    and what it replaced."""

    lease: HostLease
    sha: str
    previous: HostLease | None


def _cas_or_reject(
    remote, prefix, lease, *, expected, cwd, previous, what, force=False, at=None
) -> LeaseOutcome:
    """CAS `lease` into HQ or raise :class:`HostLeaseRejected` carrying git's own message."""
    if what in {"adopt", "renew"}:
        from . import frame_eligibility

        frame_eligibility.require_eligible(lease.host_id, {"prefix": prefix}, hq_dir=cwd, at=at)
        if (
            what == "adopt"
            and previous is not None
            and previous.host_id != lease.host_id
            and not previous.is_expired(at)
            and not force
            and not frame_eligibility.evictable(previous.host_id, hq_dir=cwd, at=at)
        ):
            raise HostLeaseRejected("incumbent no longer evictable before host-lease CAS")
    if plane := _frame_plane(cwd):
        sha = plane.publish_hive_lease(
            prefix, lease, expected=expected, operation=what, force=force
        )
        return LeaseOutcome(lease=lease, sha=sha, previous=previous)
    result = gitref.cas(remote, lease_ref(prefix), lease.to_record(), expected=expected, cwd=cwd)
    if not result.ok:
        raise HostLeaseRejected(
            f"host-lease {what} for {prefix} was rejected — another host won the race "
            f"(the HQ ref moved out from under this CAS). Re-read and decide again; this is "
            f"NOT retried automatically.\n  git: {result.detail}"
        )
    return LeaseOutcome(lease=lease, sha=result.sha, previous=previous)


def adopt(
    remote: str,
    prefix: str,
    *,
    host_id: str,
    label: str,
    cwd: Path,
    ttl: float = DEFAULT_TTL,
    at: float | None = None,
    force: bool = False,
    epoch: int | None = None,
) -> LeaseOutcome:
    """Become the recorded primary for `prefix`: CAS the HQ lease from expired-or-absent to a
    fresh record at ``epoch + 1``.

    `epoch` overrides that computation. The two-phase adopt (:mod:`beadhive.host_adopt`) needs
    it: the fence is installed FIRST and the lease has to record the SAME generation the fence
    already carries, so it cannot re-derive the number from HQ alone. Left ``None`` — every
    other caller — the behaviour is exactly ``previous + 1``.

    The epoch **always** advances, including when this host re-adopts its own live lease —
    that is what makes bh-ytbb.8's half-state (fence set, lease unrecorded) recoverable by
    simply re-adopting: a fresh epoch invalidates every token minted under the old one, so
    replaying a stale fence cannot resurrect a write right.

    Refuses (:class:`HostLeaseRejected`) when another host holds an UNEXPIRED lease; that case
    is :func:`takeover`'s, and it needs `force`. `force=True` here is exactly that escape
    hatch (see :func:`takeover`, which is the spelling call sites should use).

    Raises :class:`beadhive.gitref.RemoteUnreachable` when HQ cannot be read — adopting needs
    the remote by construction (Limitation 1), and guessing offline is the one thing this
    design must never do."""
    from . import frame_eligibility

    frame_eligibility.require_eligible(host_id, {"prefix": prefix}, hq_dir=cwd, at=at)
    sha, current = _read(remote, prefix, cwd=cwd)
    if current is not None and not current.is_expired(at) and current.host_id != host_id:
        if not force and not frame_eligibility.evictable(current.host_id, hq_dir=cwd, at=at):
            raise HostLeaseRejected(
                f"{prefix} is held by another host — host lease: {current.describe()}.\n"
                f"  Wait for it to expire, have that host release it, or force a takeover "
                f"(dangerous: `--force` is how split-brain happens — Limitation 3)."
            )
        log.get_logger(__name__).warning(
            "host_lease_forced_takeover",
            hive_prefix=prefix,
            from_host=current.host_id,
            from_label=current.label,
            from_epoch=current.epoch,
            expires_at=current.expires_at,
            to_host=host_id,
            reason=(
                "FORCED takeover of an UNEXPIRED host lease — the other host may still "
                "believe it is primary until its fence CAS fails (ADR Limitation 3)"
            ),
        )
    started = at if at is not None else time.time()
    lease = HostLease(
        host_id=host_id,
        label=label,
        epoch=epoch if epoch is not None else (current.epoch if current is not None else 0) + 1,
        adopted_at=now_stamp(started),
        expires_at=now_stamp(started + ttl),
    )
    return _cas_or_reject(
        remote,
        prefix,
        lease,
        expected=sha or gitref.ABSENT,
        cwd=cwd,
        previous=current,
        what="adopt",
        force=force,
        at=at,
    )


def renew(
    remote: str,
    prefix: str,
    *,
    host_id: str,
    cwd: Path,
    ttl: float = DEFAULT_TTL,
    at: float | None = None,
) -> LeaseOutcome:
    """Extend this host's own lease: CAS from its own value to the SAME epoch with a new
    ``expires_at``.

    The epoch deliberately does not move — a renewal is not a handoff, and bumping it would
    invalidate this host's own outstanding fence token (bh-ytbb.7) on every heartbeat.

    A lease that has already lapsed is still renewable, and that is safe rather than sloppy:
    the CAS is *from this host's own blob sha*, so if any other host adopted in the meantime
    the ref has moved and the renewal is rejected. The expiry is what invites a takeover; the
    CAS is what prevents one from being overwritten. A late renewal is logged so a chronically
    lapsing renewer is visible."""
    from . import frame_eligibility

    frame_decision = frame_eligibility.require_eligible(
        host_id, {"prefix": prefix}, hq_dir=cwd, at=at
    )
    sha, current = _read(remote, prefix, cwd=cwd)
    if current is None:
        raise HostLeaseRejected(
            f"no host lease recorded for {prefix} — nothing to renew (adopt first)"
        )
    if current.host_id != host_id:
        raise HostLeaseRejected(
            f"cannot renew {prefix}: this host does not hold it — host lease: {current.describe()}"
        )
    if frame_decision is not None and current.is_expired(at):
        raise HostLeaseRejected("frame ineligible: current_hive_lease_holder")
    if current.is_expired(at):
        log.get_logger(__name__).warning(
            "host_lease_renew_after_expiry",
            hive_prefix=prefix,
            host_id=host_id,
            expired_at=current.expires_at,
            reason=(
                "renewing a lapsed host lease — safe (the CAS is from our own value, so a "
                "takeover would reject it) but it means the renew interval is not keeping up"
            ),
        )
    started = at if at is not None else time.time()
    lease = HostLease(
        host_id=current.host_id,
        label=current.label,
        epoch=current.epoch,  # SAME epoch: a renewal is not a handoff
        adopted_at=current.adopted_at,
        expires_at=now_stamp(started + ttl),
    )
    return _cas_or_reject(
        remote, prefix, lease, expected=sha, cwd=cwd, previous=current, what="renew"
    )


def release(
    remote: str,
    prefix: str,
    *,
    host_id: str,
    cwd: Path,
    at: float | None = None,
) -> LeaseOutcome:
    """Yield this host's lease: CAS from its own value to a **tombstone**.

    A tombstone is the same five-field record with an empty ``host_id`` and an already-elapsed
    ``expires_at`` — NOT a deleted ref. Deleting would drop the epoch, and the next adopt
    would restart the counter, making a fence token minted under the old epoch valid again.
    The tombstone carries the epoch forward so the monotonic sequence survives every handoff;
    that is the entire reason release is a write rather than a delete."""
    sha, current = _read(remote, prefix, cwd=cwd)
    if current is None:
        raise HostLeaseRejected(f"no host lease recorded for {prefix} — nothing to release")
    if current.is_tombstone:
        raise HostLeaseRejected(
            f"{prefix}'s host lease is already released (epoch {current.epoch})"
        )
    if current.host_id != host_id:
        raise HostLeaseRejected(
            f"cannot release {prefix}: this host does not hold it — host lease: "
            f"{current.describe()}"
        )
    tombstone = HostLease(
        host_id="",
        label="",
        epoch=current.epoch,  # preserved: the epoch sequence must never restart
        adopted_at=current.adopted_at,
        expires_at=now_stamp(at if at is not None else time.time()),
    )
    return _cas_or_reject(
        remote, prefix, tombstone, expected=sha, cwd=cwd, previous=current, what="release"
    )


def takeover(
    remote: str,
    prefix: str,
    *,
    host_id: str,
    label: str,
    cwd: Path,
    ttl: float = DEFAULT_TTL,
    at: float | None = None,
    force: bool = False,
) -> LeaseOutcome:
    """Take `prefix` from another host. Identical to :func:`adopt` except that seizing an
    UNEXPIRED lease requires `force` — and when forced, logs loudly before doing it.

    ADR Limitation 3 states the trade honestly: the escape hatch must exist (a dead host with
    an unexpired lease would otherwise block the fleet until expiry) and using it is exactly
    how split-brain happens. Mitigation is loud logging and escalation, not prevention — so
    the refusal without `force` and the warning with it are both load-bearing."""
    return adopt(remote, prefix, host_id=host_id, label=label, cwd=cwd, ttl=ttl, at=at, force=force)


def read(remote: str, prefix: str, *, cwd: Path) -> HostLease | None:
    """The lease currently recorded at HQ for `prefix`, or ``None`` when never adopted.
    A *reading with an as-of* (Amendment 1's sharpened Limitation 6), not a truth: by the time
    a caller acts on it, another host may have CASed it. Managed publication independently
    reserves the remote fence; current bd prevents that reservation from being atomic with
    its data push, so neither this read nor the fence claims absolute safety."""
    _sha, lease = _read(remote, prefix, cwd=cwd)
    return lease


def read_cached(prefix: str, *, cwd: Path) -> HostLease | None:
    """The lease as this host last saw it, from the LOCAL ref in `cwd` — no network.

    The hot-path read: per Amendment 1 §4 an established primary must keep working with HQ
    unreachable, so ``guard_primary`` consults this rather than paying an HQ round-trip on
    every write verb."""
    _sha, record = gitref.read_local(lease_ref(prefix), cwd=cwd)
    if record is None:
        return None
    try:
        return HostLease.from_record(record)
    except ValueError:
        return None


def cache(prefix: str, outcome: LeaseOutcome, *, cwd: Path) -> None:
    """Mirror a won CAS into the LOCAL ref so :func:`read_cached` can answer offline."""
    gitref.set_local(lease_ref(prefix), outcome.sha, cwd=cwd)


def refresh_cached(remote: str, prefix: str, *, cwd: Path) -> HostLease | None:
    """Re-read HQ's lease for `prefix` over the network and mirror it into the LOCAL ref;
    returns the fresh lease, or ``None`` when HQ holds none (and then the stale local ref is
    DELETED, since "no lease at HQ" means nothing was ever adopted).

    The cache's only other writer is :func:`cache`, i.e. this host's own won CAS — so a host
    that never held the lease has no way to learn it moved, which is the bh-sks7f lockout (a
    cached lease naming a wiped host, expired 17h, refusing writes forever with no self-heal).

    Deliberately NOT on the hot path: ``guard.guard_primary`` calls this only once it is
    already about to refuse, so the allow path stays local-only per Amendment 1 §4. Propagates
    :class:`gitref.RemoteUnreachable` — an offline host keeps its cached answer."""
    sha, lease = _read(remote, prefix, cwd=cwd)
    if lease is None:
        gitref.delete_local(lease_ref(prefix), cwd=cwd)
    else:
        gitref.set_local(lease_ref(prefix), sha, cwd=cwd)
    return lease


# ---- renewal loop + fleet-visible lease state (bh-ytbb.11) -----------------------------
#
# ADR Amendment 1 §3: "Renewal is a loop inside the dispatcher process that runs only while
# workers are active — no daemon, no cron." In THIS repo the dispatcher is a CLI-driven role
# (a `bh:dispatcher` session working through `bh work`/`bh plan` verbs), not a resident OS
# process — so there is no loop to embed a timer in without violating "no background process
# outside the dispatcher's lifetime". :func:`renew_if_due` is the loop's body instead: a plain
# function a write-verb boundary calls opportunistically (``guard.guard_primary`` is the one
# call site every gated write verb already funnels through). While the dispatcher keeps
# invoking write verbs, the lease keeps getting renewed on schedule; the moment it stops, no
# further boundary fires, and the lease laps on its own — exactly "an idle host lets its lease
# lapse, which is the desired handoff, not a bug."


def lease_state(
    lease: HostLease | None,
    *,
    at: float | None = None,
    renew_interval: float = DEFAULT_RENEW_INTERVAL,
) -> str:
    """Classify `lease` into the three states ``bh host list`` reports (bh-ytbb.13):

    * ``"free"``     — absent, a tombstone, or already expired: nobody currently holds it.
    * ``"expiring"`` — live, but within ONE `renew_interval` of its own `expires_at`.
    * ``"held"``     — live, with more than a `renew_interval` of runway left.

    The SAME boundary :func:`renew_if_due` uses to decide whether a renewal is due — so
    "expiring" here means exactly "the next opportunistic ``renew_if_due`` call would act on
    this lease", never a separately-tuned threshold."""
    if lease is None or lease.is_expired(at):
        return "free"
    clock = at if at is not None else time.time()
    remaining = _parse_stamp(lease.expires_at) - clock
    return "expiring" if remaining <= renew_interval else "held"


def renew_if_due(
    remote: str,
    prefix: str,
    *,
    host_id: str,
    cwd: Path,
    ttl: float = DEFAULT_TTL,
    renew_interval: float = DEFAULT_RENEW_INTERVAL,
    at: float | None = None,
) -> LeaseOutcome | None:
    """Opportunistically renew `prefix`'s host lease — the renewal "loop" body a write-verb
    boundary calls on every pass (see the section docstring above for why it is a plain
    function rather than a background timer).

    Reads the LOCAL CACHE ONLY to decide whether a renewal is due — no HQ round trip merely to
    check the clock, which is what keeps HQ off the hot path within the interval. Attempts a
    REAL renew (an HQ round trip + CAS) only when the cache names `host_id` as the current
    holder AND the cached lease is within `renew_interval` of its own `expires_at`
    (:func:`lease_state` calls this same boundary "expiring").

    Returns the :class:`LeaseOutcome` of a renewal that actually happened, or ``None`` when:
    nothing was due yet, the cache names no lease (or another host's), or the renewal attempt
    itself failed. A failure — HQ unreachable, or the CAS lost to a takeover — is LOGGED and
    SWALLOWED, never raised: an opportunistic boundary check must never crash the write verb
    it is piggybacking on. Per Amendment 1 §4 an established primary keeps working on its
    EXISTING cached lease regardless of whether THIS renewal attempt succeeded — it is that
    cache's own `expires_at`, not this function's return value, that decides when writes stop
    (``guard_primary`` is the only place that decision is made). This function only ever tries
    to push the expiry further out; failing to do so just means the next call tries again."""
    clock = at if at is not None else time.time()
    plane = _frame_plane(cwd)
    cached = (
        plane.read_hive_lease(prefix, holder_identity=host_id)
        if plane
        else read_cached(prefix, cwd=cwd)
    )
    if cached is None or cached.host_id != host_id:
        return None  # nothing of ours locally to renew
    due_at = _parse_stamp(cached.expires_at) - renew_interval
    if clock < due_at:
        return None  # not due yet — no HQ round trip within the interval

    try:
        outcome = renew(
            remote, prefix, host_id=host_id, cwd=cwd, ttl=ttl, at=at if plane else clock
        )
    except (HostLeaseError, gitref.RemoteUnreachable) as exc:
        log.get_logger(__name__).warning(
            "host_lease_renew_if_due_failed",
            hive_prefix=prefix,
            host_id=host_id,
            cached_expires_at=cached.expires_at,
            error=str(exc),
            reason=(
                "an opportunistic renewal at a write-verb boundary failed (HQ unreachable, or "
                "the CAS lost to a takeover) — swallowed rather than raised; the existing "
                "cached lease keeps backing guard_primary exactly until IT expires "
                "(Amendment 1 §4), never longer and never shorter for this reason alone"
            ),
        )
        return None
    cache(prefix, outcome, cwd=cwd)
    return outcome
