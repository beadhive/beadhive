"""The ONE `bh host dispatch status` computation (bh-e7r9q.5), reused verbatim by `bh doctor`'s
dispatch section (bh-e7r9q.6) rather than re-derived there.

bh-e7r9q.6's whole reason for existing is a DISTINCTION `bh host list` cannot make today: it
prints one word, `stale`, whether the loop died or the lease merely lapsed while work was in
flight, and those need different operator responses. This module is where that distinction is
computed exactly once — :func:`compute_status` reads the supervisor backend
(:mod:`beadhive.dispatch_supervisor`), the host lease (:mod:`beadhive.guard`), and the
aggregate log tail (:mod:`beadhive.dispatch_log`), and returns a single
:class:`DispatchStatus` whose `.state` is one of the four the CLI renders and doctor asserts
against. Two answers that can disagree is worse than one answer — so `bh host dispatch status`
and `bh doctor`'s dispatch section both call THIS function and only format its result.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import config, dispatch_log, dispatch_supervisor, guard, registry, state

# ---- the closed state vocabulary -----------------------------------------------------------

STATE_NOT_ENABLED = "not_enabled"
#: Installed + persisted, but the process is not running right now — the "dead loop" bh-e7r9q.6
#: exists to distinguish from a lapsed lease.
STATE_ENABLED_STOPPED = "enabled_stopped"
#: The process IS running but this host does not currently hold the hive's lease — either it is
#: correctly idling (multi-host handoff) or the lease genuinely lapsed mid-flight; `detail`
#: (and `lease_detail`) is what tells the two apart for a human.
STATE_RUNNING_WITHOUT_LEASE = "running_without_lease"
STATE_RUNNING_HEALTHY = "running_healthy"

DISPATCH_STATES: tuple[str, ...] = (
    STATE_NOT_ENABLED,
    STATE_ENABLED_STOPPED,
    STATE_RUNNING_WITHOUT_LEASE,
    STATE_RUNNING_HEALTHY,
)

#: A lease with less than this fraction of its TTL remaining is "expiring soon" for doctor's
#: purposes — a separate, additive concern from the four states above (a healthy, running,
#: leased loop can still be about to lose that lease).
EXPIRING_SOON_FRACTION = 0.1


@dataclass(frozen=True)
class DispatchStatus:
    hive: str
    hive_slug: str
    backend: str
    installed: bool
    running: bool
    persisted: bool
    lease_in_force: bool  # False = single-host / never adopted: nothing to hold or lose
    lease_held: bool
    lease_expires_at: str
    lease_detail: str
    last_pass_at: str
    #: SEAT processes the epic loop last reported in flight (`localloop`'s `dispatch_pass`).
    seats_in_flight: int
    last_escalation: dict | None
    state: str
    detail: str
    #: `bh work loop` children the per-hive PICKER last reported in flight — a different noun,
    #: so a different key (`hive_dispatch_pass`'s `epics_in_flight`). Defaulted, and last, so
    #: existing constructors stay valid.
    epics_in_flight: int = 0

    @property
    def lease_expiring_soon(self) -> bool:
        if not self.lease_in_force or not self.lease_held or not self.lease_expires_at:
            return False
        try:
            import calendar

            expires = calendar.timegm(time.strptime(self.lease_expires_at, "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            return False
        ttl = config.host_lease_ttl(None)
        remaining = expires - time.time()
        return 0 < remaining < (ttl * EXPIRING_SOON_FRACTION)

    def stale_since_seconds(self, *, at: float | None = None) -> float | None:
        """Seconds since the last recorded pass, or `None` when there has never been one."""
        if not self.last_pass_at:
            return None
        try:
            import calendar

            last = calendar.timegm(time.strptime(self.last_pass_at, "%Y-%m-%dT%H:%M:%S.%fZ"))
        except ValueError:
            try:
                import calendar

                last = calendar.timegm(time.strptime(self.last_pass_at, "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                return None
        clock = at if at is not None else time.time()
        return max(clock - last, 0.0)

    def as_dict(self) -> dict:
        return {
            "hive": self.hive,
            "hive_slug": self.hive_slug,
            "backend": self.backend,
            "installed": self.installed,
            "running": self.running,
            "persisted": self.persisted,
            "lease_in_force": self.lease_in_force,
            "lease_held": self.lease_held,
            "lease_expires_at": self.lease_expires_at,
            "lease_detail": self.lease_detail,
            "lease_expiring_soon": self.lease_expiring_soon,
            "last_pass_at": self.last_pass_at,
            "seats_in_flight": self.seats_in_flight,
            "epics_in_flight": self.epics_in_flight,
            "last_escalation": self.last_escalation,
            "state": self.state,
            "detail": self.detail,
        }


def _classify(installed: bool, running: bool, lease_in_force: bool, lease_held: bool) -> str:
    if not installed:
        return STATE_NOT_ENABLED
    if not running:
        return STATE_ENABLED_STOPPED
    if lease_in_force and not lease_held:
        return STATE_RUNNING_WITHOUT_LEASE
    return STATE_RUNNING_HEALTHY


def compute_status(
    hive: str = "",
    *,
    cfg: dict | None = None,
    backend: dispatch_supervisor.SupervisorBackend | None = None,
) -> DispatchStatus:
    """Read-only: no writes, no lease renewal, no process signals. Safe to call from `status`,
    from `doctor`, and from a test — the SAME function every one of those calls."""
    cfg = cfg if cfg is not None else config.load()
    main = registry.hive_dir_for(cfg, hive)
    entry = registry.entry_for_dir(cfg, main) or {}
    slug = dispatch_log.hive_slug(entry)

    sup = backend or dispatch_supervisor.get_supervisor_backend(cfg)
    sup_state = sup.status(slug)

    lease_in_force = False
    lease_held = True
    lease_expires_at = ""
    lease_detail = ""
    state_info = guard.primary_state(hive, cfg=cfg)
    if state_info is not None:
        _prefix, this_host, lease = state_info
        lease_in_force = True
        lease_held = lease.held_by(this_host)
        lease_expires_at = lease.expires_at if not lease.is_tombstone else ""
        lease_detail = lease.describe()

    sink = dispatch_log.sink_path_for_slug(slug)
    records = dispatch_log.tail_records(sink, lines=500)
    last_pass_at = ""
    seats_in_flight = 0
    epics_in_flight = 0
    last_escalation: dict | None = None
    seen_seats = False
    seen_epics = False
    # TWO DIFFERENT NOUNS, read from two different events. `hive_dispatch_pass` is the per-hive
    # PICKER's pass and its `epics_in_flight` counts `bh work loop` children; `dispatch_pass` is
    # one of those loops' own pass and its `in_flight` counts SEAT processes. `--help` advertises
    # "seats in flight?", and reading the picker's key for it reported epics as seats — while
    # `PassReport.as_dict()` carried no `in_flight` key at all, so the number was structurally
    # always 0 no matter which event was read.
    for record in reversed(records):
        event = str(record.get("event") or "")
        if event in ("hive_dispatch_pass", "dispatch_pass"):
            if not last_pass_at:
                last_pass_at = str(record.get("timestamp") or "")
            if event == "dispatch_pass" and not seen_seats:
                value = record.get("in_flight")
                if isinstance(value, list):
                    seats_in_flight, seen_seats = len(value), True
            if event == "hive_dispatch_pass" and not seen_epics:
                value = record.get("epics_in_flight")
                if isinstance(value, list):
                    epics_in_flight, seen_epics = len(value), True
        if last_escalation is None and event == "dispatch_cause_recorded":
            if str(record.get("cause") or "") == state.CAUSE_ESCALATED:
                last_escalation = record
        if last_pass_at and seen_seats and seen_epics and last_escalation is not None:
            break

    # `dispatch_state`, not `state` — the module-level `state` import owns that name.
    dispatch_state = _classify(sup_state.installed, sup_state.running, lease_in_force, lease_held)
    detail = sup_state.detail or lease_detail

    return DispatchStatus(
        hive=hive or registry.hive_key(entry),
        hive_slug=slug,
        backend=sup.name,
        installed=sup_state.installed,
        running=sup_state.running,
        persisted=sup_state.persisted,
        lease_in_force=lease_in_force,
        lease_held=lease_held,
        lease_expires_at=lease_expires_at,
        lease_detail=lease_detail,
        last_pass_at=last_pass_at,
        seats_in_flight=seats_in_flight,
        epics_in_flight=epics_in_flight,
        last_escalation=last_escalation,
        state=dispatch_state,
        detail=detail,
    )


def compute_status_all(cfg: dict | None = None) -> list[DispatchStatus]:
    """Every registered hive's status, for `bh host dispatch status --all`."""
    cfg = cfg if cfg is not None else config.load()
    out = []
    for entry in cfg.get("managed_repos", []) or []:
        hive = registry.hive_key(entry)
        try:
            out.append(compute_status(hive, cfg=cfg))
        except Exception:  # noqa: BLE001 - one broken hive must not blank the whole table
            continue
    return out
