"""``bh host dispatch run --hive <hive>`` — the hive-level picker (bh-e7r9q.5).

This is the process a supervision backend (:mod:`beadhive.dispatch_supervisor`) starts and
restarts: ONE per hive, long-lived, with no human at a terminal. Every pass:

    check the host lease -> if not held, IDLE READ-ONLY and log it, do nothing else ->
    reap any `bh work loop <epic>` children that finished (a non-zero exit arms that epic's
    respawn backoff) -> if there is room under `host.dispatch.max_epics_in_flight`, pick the
    next kicked-off ready epic that is not in backoff and spawn `bh work loop <epic>` for it ->
    sleep `host.dispatch.poll_interval` -> repeat

THE PICKER IS DELIBERATELY DUMB, AND MUST STAY THAT WAY (operator decision 2026-08-10):
kicked-off epics (`kickoff:approved`) in `bd ready` order, bounded by the concurrency cap.
NO cross-hive arbitration, NO budget reasoning — both belong to the DIRECTOR LOOP at the
second-hive trigger (docs/design/loop-ownership-and-execution-memory-adr.md Decision 4, which
records "who launches a dispatcher" as OPEN and defers it for exactly this reason). Anyone
tempted to teach this module to compare hives, weight priority beyond `bd ready`'s own
ordering, or reason about a token budget: that is the director loop's job, not this one's —
add it there when the second hive forces the issue, not here.

LEASE-ABSENT DEGRADATION IS THE MULTI-HOST MODEL'S SPECIFIED BEHAVIOUR, NOT AN ERROR. An
enabled instance for a hive this host does not (or no longer) hold the lease on idles,
read-only, and says so in the aggregate log — it never spawns a child, and a child already in
flight when the lease is lost is left to finish (killing an in-flight seat on a lease wobble
would be a worse failure than a late write, per the loop-ownership ADR).

DELEGATES ALL OF THE ACTUAL MOLECULE-DRIVING WORK to `bh work loop <epic>` (bh-c6dk.5,
:class:`beadhive.localloop.LocalLoop`) as a CHILD PROCESS per epic, rather than re-implementing
per-epic orchestration here. That is also what makes "several loops append to one sink at
once" a genuine multi-PROCESS concern — see :mod:`beadhive.dispatch_log` for how the sink stays
parseable under that.

NOTHING HERE IS PERSISTED OUTSIDE BEADS EITHER (the same invariant `localloop` holds): the
in-flight child-process map dies with this process by design; a restart re-derives which epics
are kicked-off-and-ready from `bd` and simply starts fresh, same as `LocalLoop` does one tier
down.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import bd as bd_mod
from . import config, dispatch_log, log, registry

_LOG = log.get_logger(__name__)

KICKOFF_APPROVED_LABEL = "kickoff:approved"

# ---- respawn backoff ---------------------------------------------------------------------
# A `bh work loop <epic>` child that HALTS exits 1 (deliberately — a halt is not success). This
# picker then re-picks the same epic on the very next pass, `poll_interval` later. For a bead
# awaiting human triage that loop is: escalate -> record cause -> halted -> exit 1 -> respawn.
# At the default 10s poll that is ~8,640 passes a day against one stuck bead, and in a hive where
# `bd compact`/`bd flatten` are FORBIDDEN until bh-3vs6c lands, anything the child writes per
# cycle is permanent. `localloop._escalate` latches on `escalation:raised` so it stops writing;
# this backoff stops the respawning itself, which is the other half.
#
# Volatile by construction (a plain in-process dict, cleared on restart) so it stays inside the
# execution-memory ADR's line: nothing is persisted outside beads. A restart legitimately gets
# one immediate retry — that is the ADR's "restart is a no-op" property, not a leak.
BACKOFF_BASE_SECONDS = 30.0
BACKOFF_MAX_SECONDS = 3600.0


def backoff_delay(failures: int) -> float:
    """Seconds to wait before re-picking an epic whose child has failed `failures` times in a
    row: 30s, 60s, 120s … capped at an hour. Pure, so the schedule is testable without waiting
    it out."""
    if failures <= 0:
        return 0.0
    return min(BACKOFF_BASE_SECONDS * (2 ** (failures - 1)), BACKOFF_MAX_SECONDS)


def kicked_off_ready_epics(hive_dir: Path) -> list[str]:
    """Kicked-off epics in `bd ready` order — the whole picker policy, in one function so it is
    trivially testable and trivially auditable as "this is all it does".

    `bd ready` already returns dependency order; filtering to `issue_type: epic` +
    `kickoff:approved` is the only judgement this picker is allowed to make.

    `--limit 0` IS THE WHOLE READ, and it is not optional (bh-fruer, P0): `bd ready` caps at 100
    rows by default. Measured on this hive 2026-08-10 — 20 epics visible, 48 actually ready — so
    without it every epic past position 100 in the ready order is silently never dispatched, and
    nothing anywhere reports that it was dropped. Five other call sites already spell it this
    way (`release.py`, `validate.py`, `contributor.py`, `plan.py`, `work_logic.py`); this is the
    sixth. (`bd.json` appends its own `--json`, so passing one here was redundant.)
    """
    rows = bd_mod.json(["ready", "--limit", "0"], hive_dir) or []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("issue_type") or "") != "epic":
            continue
        labels = row.get("labels") or []
        if KICKOFF_APPROVED_LABEL in labels:
            out.append(str(row.get("id") or ""))
    return [b for b in out if b]


@dataclass
class HivePassReport:
    """One pass of the picker, rendered — a return value and a log line, nothing more (same
    volatility contract as `localloop.PassReport`)."""

    number: int = 0
    lease_held: bool = True
    idle: bool = False
    reaped: tuple[str, ...] = ()
    spawned: tuple[str, ...] = ()
    #: EPICS, not seats. Named for what it counts: `bh host dispatch status` reads a seat count
    #: off `localloop`'s own `dispatch_pass` records, and a bare `in_flight` on both events made
    #: those two different nouns look like one number.
    epics_in_flight: tuple[str, ...] = ()
    #: Epics skipped this pass because their child failed recently and their backoff has not
    #: elapsed. Surfaced, never silent — a deferred epic must be legible in the log.
    deferred: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "pass": self.number,
            "lease_held": self.lease_held,
            "idle": self.idle,
            "reaped": list(self.reaped),
            "spawned": list(self.spawned),
            "epics_in_flight": list(self.epics_in_flight),
            "deferred": list(self.deferred),
        }


@dataclass
class _Child:
    epic: str
    proc: asyncio.subprocess.Process
    started_at: float = field(default_factory=time.monotonic)


class HiveDispatchRun:
    """The picker loop for one hive. Collaborators are injectable (`lease`, `pick`, `spawn`) for
    the same reason `LocalLoop`'s are: testable without a real host lease, a real `bd`, or a
    real `bh work loop` child."""

    def __init__(
        self,
        *,
        hive_dir: Path,
        hive: str,
        actor: str,
        sink_path: Path,
        bh_binary: str = "bh",
        max_epics_in_flight: int = 3,
        poll_interval: float = 10.0,
        lease=None,
        pick=None,
        env: dict[str, str] | None = None,
    ):
        self.hive_dir = Path(hive_dir)
        self.hive = hive
        self.actor = actor
        self.sink_path = Path(sink_path)
        self.bh_binary = bh_binary
        self.max_epics_in_flight = max(max_epics_in_flight, 1)
        self.poll_interval = poll_interval
        from . import localloop

        self.lease = lease or localloop.NullLeaseKeeper()
        self._pick = pick or (lambda: kicked_off_ready_epics(self.hive_dir))
        self.env = env
        self.children: dict[str, _Child] = {}
        #: epic -> (consecutive child failures, monotonic time it may be re-picked). VOLATILE.
        self.backoff: dict[str, tuple[int, float]] = {}
        self.passes = 0
        self.stopping = False

    async def _spawn(self, epic: str) -> _Child:
        env = dict(os.environ if self.env is None else self.env)
        env["BH_DISPATCH_LOG_SINK"] = str(self.sink_path)
        argv = [self.bh_binary, "work", "loop", epic, "--json"]
        if self.hive:
            argv += ["--hive", self.hive]
        if self.actor:
            argv += ["--as", self.actor]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self.hive_dir),
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        _LOG.info("hive_dispatch_child_spawned", hive=self.hive, epic=epic, pid=proc.pid)
        return _Child(epic=epic, proc=proc)

    def _reap_finished(self, *, now: float | None = None) -> tuple[str, ...]:
        clock = time.monotonic() if now is None else now
        done = [epic for epic, child in self.children.items() if child.proc.returncode is not None]
        for epic in done:
            child = self.children.pop(epic)
            if child.proc.returncode == 0:
                # Clean exit: the molecule landed or ran out of passes. Forget any history —
                # backoff punishes a repeating FAILURE, never a slow-but-healthy epic.
                self.backoff.pop(epic, None)
            else:
                failures = self.backoff.get(epic, (0, 0.0))[0] + 1
                delay = backoff_delay(failures)
                self.backoff[epic] = (failures, clock + delay)
                _LOG.warning(
                    "hive_dispatch_child_backoff",
                    hive=self.hive,
                    epic=epic,
                    exit_code=child.proc.returncode,
                    failures=failures,
                    retry_in_seconds=delay,
                )
            _LOG.info(
                "hive_dispatch_child_harvested",
                hive=self.hive,
                epic=epic,
                exit_code=child.proc.returncode,
            )
        return tuple(done)

    def _deferred_until(self, epic: str, now: float) -> float:
        """When *epic* becomes re-pickable (monotonic), or 0.0 when it is pickable right now."""
        failures, retry_at = self.backoff.get(epic, (0, 0.0))
        return retry_at if failures and retry_at > now else 0.0

    async def run_pass(self) -> HivePassReport:
        self.passes += 1
        report = HivePassReport(number=self.passes)

        # Non-blocking: a finished child's returncode is already set by asyncio's own child
        # watcher, so reaping never needs to await here.
        report.reaped = self._reap_finished()

        lease_status = self.lease.renew(active=bool(self.children))
        report.lease_held = lease_status.held
        if not lease_status.held:
            # IDLE, READ-ONLY. Never spawn while the lease is not held — the multi-host model's
            # specified degradation, not an error. In-flight children are left to finish rather
            # than killed on a lease wobble.
            report.idle = True
            report.epics_in_flight = tuple(self.children)
            _LOG.info(
                "hive_dispatch_idle_no_lease",
                hive=self.hive,
                detail=lease_status.detail,
                in_flight=list(self.children),
            )
            return report

        room = self.max_epics_in_flight - len(self.children)
        spawned: list[str] = []
        deferred: list[str] = []
        now = time.monotonic()
        if room > 0:
            for epic in self._pick():
                if room <= 0:
                    break
                if epic in self.children:
                    continue
                if self._deferred_until(epic, now):
                    deferred.append(epic)
                    continue
                self.children[epic] = await self._spawn(epic)
                spawned.append(epic)
                room -= 1
        report.spawned = tuple(spawned)
        report.deferred = tuple(deferred)
        report.epics_in_flight = tuple(self.children)
        _LOG.info("hive_dispatch_pass", **report.as_dict())
        return report

    async def run(self, *, max_passes: int | None = None) -> list[HivePassReport]:
        reports: list[HivePassReport] = []
        try:
            while not self.stopping:
                reports.append(await self.run_pass())
                if max_passes is not None and len(reports) >= max_passes:
                    break
                await asyncio.sleep(self.poll_interval)
        finally:
            await self.shutdown()
        return reports

    async def shutdown(self) -> None:
        """Best-effort: let in-flight `bh work loop` children keep running (they are their own
        supervised units under `LocalLoop`'s own process-group discipline) — this picker owns
        scheduling, not their lifecycle. Just stop waiting on them."""
        self.stopping = True


def build_run(hive: str, *, cfg: dict | None = None, actor: str = "") -> HiveDispatchRun:
    """Assemble a :class:`HiveDispatchRun` for `bh host dispatch run --hive <hive>` from config
    — the CLI-facing constructor, kept separate from the class so tests build the class
    directly with fakes."""
    from . import identity, localloop

    cfg = cfg if cfg is not None else config.load()
    main = registry.hive_dir_for(cfg, hive)
    entry = registry.entry_for_dir(cfg, main) or {}
    resolved_actor = identity.resolve_actor(actor, config.work_identity(cfg, entry)["name"] or "")
    dispatch_log.ensure_sink_dir()
    sink = dispatch_log.sink_path(cfg, entry)
    return HiveDispatchRun(
        hive_dir=main,
        hive=hive,
        actor=resolved_actor,
        sink_path=sink,
        max_epics_in_flight=config.dispatch_max_epics_in_flight(cfg),
        poll_interval=config.dispatch_hive_poll_interval(cfg),
        lease=localloop.lease_keeper_for(hive, cfg=cfg, hive_dir=main),
    )
