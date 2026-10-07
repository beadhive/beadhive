"""Per-role, per-hive ``failover_after`` and the executor floor, in HQ data (bh-4biq8, M8c).

``docs/design/hive-writer-partitioning-adr.md`` §4: ``failover_after`` defaults to 60 min for an
executor and 30 min for a transient frame; a viewer is never placed. Overrides are per role and
per hive, held in HQ **data** beside the placement row — never a ``host.yaml`` or fleet-config key
(``bh-32379`` S2 "data is the switch"; no 0.21.3 unknown-key skew; no authority renewal,
``bh-87l3y``). The executor floor (default 45 min) is held in the same data.

**Where it lives.** :data:`POLICY_TABLE` (``hq_live_failover_policy``), one row per
``(scope, setting)``: ``scope`` is a hive prefix or :data:`FLEET_SCOPE` (``*``), ``setting`` is a
role (``executor`` / ``transient``) or :data:`FLOOR_SETTING` (fleet scope only). It is NOT inside
``hq_live_hive_leases.lease_json``: every pre-0.23 reader of that row is strict — the trusted
receiver (``SqlTrustedReceiver._read_prior``) and :func:`beadhive.hq_sql_placement.parse_row`
refuse an envelope with any key beyond ``{authority, lease}`` or a lease with any field beyond the
five, and the receiver compares the stored ``authority`` by equality, so an extra key there would
break the Φ3 rollback. Nor is it a new column: the receiver INSERTs that row positionally
(``VALUES (%s,%s,%s,%s,%s)``). A sibling table is the place no older reader parses. Its
``hq_live_`` name keeps it under the runtime database's ``dolt_ignore`` rule, so a change never
commits, never moves the config head and never unbinds authority.

**Validated on load, refused — never clamped** (``bh-cvk70`` E20). A value below a hard bound —
bd lease TTL + reclaim grace (:data:`BD_RECLAIM_S`, 15 min) or ``2 x (session TTL + sync
interval)`` — is refused; an executor value below the configured floor is refused; a floor below
a hard bound is refused. A refused row is reported with a named reason and the observer falls
back to the default (the fleet row, else the code default). A floor in ``[hard bound, 35.4 min)``
is accepted with a warning naming the measured false-stale stretch.

Pure apart from the cursor helpers; Typer-free.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

from .hq_sql_runtime_schema import FAILOVER_POLICY_SCHEMA
from .hq_sql_runtime_schema import FAILOVER_POLICY_TABLE as POLICY_TABLE

__all__ = [
    "ROLE_FAILOVER_AFTER_S",
    "failover_after_for",
    "BD_RECLAIM_S",
    "DEFAULT_EXECUTOR_FLOOR_S",
    "DOLT_SERVER_SYNC_INTERVAL_S",
    "FAILOVER_POLICY_SCHEMA",
    "FALSE_STALE_STRETCH_S",
    "FLEET_SCOPE",
    "FLOOR_SETTING",
    "POLICY_TABLE",
    "ROLES",
    "SETTINGS",
    "Bounds",
    "FailoverPolicy",
    "FailoverPolicyError",
    "Refusal",
    "apply_changes",
    "effective",
    "load_policy",
    "parse_changes",
    "plan_changes",
    "provision_statements",
    "read_policy",
]

FLEET_SCOPE = "*"
FLOOR_SETTING = "executor_floor"
#: Roles a placement row can carry an override for. A viewer is never placed (ADR §4).
ROLES = ("executor", "transient")
SETTINGS = (*ROLES, FLOOR_SETTING)

#: bd's lease TTL (5 min) + reclaim grace (10 min): bd reclaims a dead worker on the live primary
#: long before a dead frame could move the hive (``bh-sieai`` T5; ``bh-cvk70`` E20).
BD_RECLAIM_S = 900
#: The HQ sync interval in ``dolt-server`` HQ (every read is the server's own state).
DOLT_SERVER_SYNC_INTERVAL_S = 0
#: The largest false-stale stretch measured live (``bh-cvk70`` E17): 35.4 min.
FALSE_STALE_STRETCH_S = 2124
#: The configurable executor floor's code default (operator decision after the ADR): 45 min.
DEFAULT_EXECUTOR_FLOOR_S = 2700

_PREFIX = re.compile(r"[a-z][a-z0-9-]*")
_TABLES_SQL = (
    "SELECT table_name FROM information_schema.tables WHERE table_schema=DATABASE() "
    "AND table_name IN (%s,%s)"
)


class FailoverPolicyError(ValueError):
    """A failover-policy change refused (it would be refused on load); nothing was written.
    :mod:`beadhive.hq_sql_placement` re-raises it as a ``PlacementError``."""


#: Code defaults (ADR §4): an executor rides the 35.4 min false-stale stretch observed live; a
#: transient frame comes and goes; a viewer is never placed, so it never fails over.
ROLE_FAILOVER_AFTER_S: dict[str, float | None] = {
    "executor": 3600.0,
    "transient": 1800.0,
    "viewer": None,
}


def failover_after_for(role: str, *, override: float | None = None) -> float | None:
    """``failover_after`` seconds for `role`; ``None`` means never fail over (never placed).

    `override` is the per-role, per-hive value from HQ data (M8c, ``bh-4biq8``:
    :mod:`beadhive.failover_policy` owns loading and floor validation — refused, never
    clamped). An unknown role takes the executor default: the longest, so a newer role never
    fails over early."""
    from .host_manifest_contracts import canonical_role

    if override is not None:
        if type(override) not in (int, float) or not math.isfinite(override) or override <= 0:
            raise ValueError("failover_after override must be a positive, finite number")
        return float(override)
    role = canonical_role(role)
    if role in ROLE_FAILOVER_AFTER_S:
        return ROLE_FAILOVER_AFTER_S[role]
    return ROLE_FAILOVER_AFTER_S["executor"]


def _minutes(seconds: float) -> str:
    return f"{seconds:g} s ({seconds / 60:g} min)"


@dataclass(frozen=True)
class Bounds:
    """The ``bh-cvk70`` E20 hard bounds for one HQ (correctness limits, not tunables)."""

    session_ttl_s: float
    sync_interval_s: float = DOLT_SERVER_SYNC_INTERVAL_S
    bd_reclaim_s: float = BD_RECLAIM_S

    @property
    def session_bound_s(self) -> float:
        return 2 * (self.session_ttl_s + self.sync_interval_s)

    @property
    def minimum_s(self) -> float:
        return max(self.bd_reclaim_s, self.session_bound_s)

    def violation(self, seconds: float) -> str | None:
        """The first hard bound `seconds` is below, named; ``None`` when it clears both."""
        if seconds < self.bd_reclaim_s:
            return (
                f"below the bd lease TTL + reclaim grace hard bound {_minutes(self.bd_reclaim_s)}"
            )
        if seconds < self.session_bound_s:
            return (
                "below the 2 x (session TTL + sync interval) hard bound "
                f"{_minutes(self.session_bound_s)} (session TTL {self.session_ttl_s:g} s, sync "
                f"interval {self.sync_interval_s:g} s)"
            )
        return None

    def as_dict(self) -> dict:
        return {
            "session_ttl_s": self.session_ttl_s,
            "sync_interval_s": self.sync_interval_s,
            "bd_reclaim_s": self.bd_reclaim_s,
            "session_bound_s": self.session_bound_s,
        }


@dataclass(frozen=True)
class Refusal:
    """One refused row: never clamped, reported, and its default used instead."""

    scope: str
    setting: str
    seconds: object
    reason: str

    def describe(self) -> str:
        where = "fleet" if self.scope == FLEET_SCOPE else f"hive {self.scope}"
        return f"{where} {self.setting}={self.seconds!r} refused: {self.reason}"


@dataclass(frozen=True)
class FailoverPolicy:
    """The loaded, validated policy. :meth:`failover_after` is what the observer uses."""

    bounds: Bounds
    executor_floor_s: float = DEFAULT_EXECUTOR_FLOOR_S
    defaults: Mapping[str, float] = field(
        default_factory=lambda: {role: ROLE_FAILOVER_AFTER_S[role] for role in ROLES}
    )
    hives: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    refusals: tuple[Refusal, ...] = ()
    warnings: tuple[str, ...] = ()
    provisioned: bool = True

    def failover_after(self, prefix: str, role: str) -> float | None:
        """Seconds before `role`'s frame on `prefix` may be failed over; ``None`` = never.

        A viewer is never placed. An unknown role takes the executor value (the longest, so a
        newer role never fails over early), as :func:`failover_after_for` does."""
        from .host_manifest_contracts import canonical_role

        role = canonical_role(role)
        if failover_after_for(role) is None:
            return None
        if role not in ROLES:
            role = "executor"
        override = (self.hives.get(prefix) or {}).get(role)
        return failover_after_for(role, override=override if override else self.defaults[role])

    def as_dict(self) -> dict:
        return {
            "provisioned": self.provisioned,
            "bounds": self.bounds.as_dict(),
            "executor_floor_s": self.executor_floor_s,
            "defaults": dict(self.defaults),
            "hives": {prefix: dict(values) for prefix, values in sorted(self.hives.items())},
            "refusals": [r.describe() for r in self.refusals],
            "warnings": list(self.warnings),
        }


def _text(value) -> str | None:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        try:
            value = value.decode()
        except UnicodeDecodeError:
            return None
    return value if isinstance(value, str) else None


def _whole_seconds(value) -> int | None:
    return value if type(value) is int and value > 0 else None


def load_policy(rows: Iterable, bounds: Bounds, *, provisioned: bool = True) -> FailoverPolicy:
    """Validate raw ``(scope, setting, seconds)`` rows into a :class:`FailoverPolicy`.

    Every invalid row is refused with a named reason and never clamped; its default applies."""
    refusals: list[Refusal] = []
    warnings: list[str] = []
    fleet: dict[str, int] = {}
    hives: dict[str, dict[str, int]] = {}

    def refuse(scope, setting, seconds, reason):
        refusals.append(Refusal(str(scope), str(setting), seconds, reason))

    for row in rows:
        try:
            raw_scope, raw_setting, seconds = row
        except (TypeError, ValueError):
            refuse("?", "?", None, "row shape invalid")
            continue
        scope, setting = _text(raw_scope), _text(raw_setting)
        if scope is None or (scope != FLEET_SCOPE and not _PREFIX.fullmatch(scope)):
            refuse(raw_scope, raw_setting, seconds, "scope is neither a hive prefix nor '*'")
            continue
        if setting == "viewer":
            refuse(scope, setting, seconds, "a viewer is never placed, so it never fails over")
            continue
        if setting not in SETTINGS:
            refuse(scope, raw_setting, seconds, f"unknown setting (expected one of {SETTINGS})")
            continue
        if setting == FLOOR_SETTING and scope != FLEET_SCOPE:
            refuse(scope, setting, seconds, "the executor floor is fleet-wide (scope '*') only")
            continue
        value = _whole_seconds(seconds)
        if value is None:
            refuse(scope, setting, seconds, "not a positive whole number of seconds")
            continue
        (fleet if scope == FLEET_SCOPE else hives.setdefault(scope, {}))[setting] = value

    # The floor first: against the hard bounds, then against the executor default it governs.
    floor: float = DEFAULT_EXECUTOR_FLOOR_S
    if FLOOR_SETTING in fleet:
        candidate = fleet.pop(FLOOR_SETTING)
        executor_default = fleet.get("executor")
        if executor_default is None or bounds.violation(executor_default) is not None:
            executor_default = ROLE_FAILOVER_AFTER_S["executor"]
        reason = bounds.violation(candidate)
        if reason is None and candidate > executor_default:
            reason = (
                f"above the executor failover_after default {_minutes(executor_default)}, "
                "which it would refuse"
            )
        if reason is not None:
            refuse(FLEET_SCOPE, FLOOR_SETTING, candidate, reason)
        else:
            floor = candidate
    if floor < FALSE_STALE_STRETCH_S:
        warnings.append(
            f"executor floor {_minutes(floor)} is below the measured "
            f"{_minutes(FALSE_STALE_STRETCH_S)} false-stale stretch (bh-cvk70 E17): an executor "
            "that is alive but not renewing for that long can be failed over"
        )
    if floor < bounds.minimum_s:
        warnings.append(
            f"code-default executor floor {_minutes(floor)} is below a hard bound "
            f"({bounds.violation(floor)}); the hard bound governs"
        )

    def check(scope: str, role: str, value: int) -> str | None:
        reason = bounds.violation(value)
        if reason is None and role == "executor" and value < floor:
            reason = f"below the executor floor {_minutes(floor)}"
        return reason

    defaults: dict[str, float] = {}
    for role in ROLES:
        code = ROLE_FAILOVER_AFTER_S[role]
        defaults[role] = code
        if role in fleet:
            reason = check(FLEET_SCOPE, role, fleet[role])
            if reason is None:
                defaults[role] = fleet[role]
            else:
                refuse(FLEET_SCOPE, role, fleet[role], reason)
        if defaults[role] == code and check(FLEET_SCOPE, role, int(code)) is not None:
            warnings.append(
                f"code-default {role} failover_after {_minutes(code)} is "
                f"{check(FLEET_SCOPE, role, int(code))}; set a fleet '*' {role} row"
            )

    accepted: dict[str, dict[str, float]] = {}
    for prefix, values in sorted(hives.items()):
        for role, value in sorted(values.items()):
            reason = check(prefix, role, value)
            if reason is None:
                accepted.setdefault(prefix, {})[role] = value
            else:
                refuse(prefix, role, value, reason)
    return FailoverPolicy(
        bounds=bounds,
        executor_floor_s=floor,
        defaults=defaults,
        hives=accepted,
        refusals=tuple(refusals),
        warnings=tuple(warnings),
        provisioned=provisioned,
    )


def parse_changes(
    text: str, *, allowed: tuple[str, ...] = ROLES, parse=None
) -> dict[str, int | None]:
    """``"executor=75m,transient=40m"`` → ``{"executor": 4500, "transient": 2400}``.

    ``ROLE=default`` clears that override (``None``). Durations take seconds or e.g. ``45m`` /
    ``1h``; anything else is refused, never adjusted."""
    if parse is None:
        from .hq_authority_ceiling import parse_duration as parse
    changes: dict[str, int | None] = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        setting, sep, value = part.partition("=")
        setting, value = setting.strip(), value.strip()
        if not sep or not setting or not value:
            raise FailoverPolicyError(f"failover_after entry {part!r} is not ROLE=DURATION")
        if setting not in allowed:
            raise FailoverPolicyError(
                f"unknown failover setting {setting!r} (expected one of {allowed})"
            )
        if setting in changes:
            raise FailoverPolicyError(f"failover setting {setting!r} given twice")
        if value == "default":
            changes[setting] = None
            continue
        try:
            seconds = parse(value)
        except ValueError as exc:
            raise FailoverPolicyError(str(exc)) from None
        if seconds != int(seconds):
            raise FailoverPolicyError(f"{setting}={value!r} must be whole seconds")
        changes[setting] = int(seconds)
    if not changes:
        raise FailoverPolicyError("no failover setting given (ROLE=DURATION[,...])")
    return changes


# =============================================================================================
# HQ reads and writes (a DB-API cursor on an open transaction)
# =============================================================================================


def _tables(cursor) -> set[str]:
    from .hq_sql_runtime_schema import LIVENESS_POLICY_TABLE

    cursor.execute(_TABLES_SQL, (POLICY_TABLE, LIVENESS_POLICY_TABLE))
    return {_text(row[0]) or "" for row in cursor.fetchall()}


def _bounds(cursor, tables: set[str]) -> tuple[Bounds, list[str]]:
    from .hq_sql_runtime_schema import LIVENESS_POLICY_TABLE
    from .hq_sql_session import DEFAULT_SESSION_TTL_S, SessionError, _policy

    if LIVENESS_POLICY_TABLE not in tables:
        return Bounds(DEFAULT_SESSION_TTL_S), []
    cursor.execute(f"SELECT session_ttl_s,evidence_ttl_s FROM {LIVENESS_POLICY_TABLE}")
    try:
        return Bounds(_policy(cursor.fetchall()).session_ttl_s), []
    except SessionError as exc:
        return Bounds(DEFAULT_SESSION_TTL_S), [
            f"{exc}; hard bounds use the default session TTL {DEFAULT_SESSION_TTL_S} s"
        ]


def _rows(cursor, tables: set[str]) -> list[tuple]:
    if POLICY_TABLE not in tables:
        return []
    cursor.execute(f"SELECT scope,setting,seconds FROM {POLICY_TABLE}")
    return [tuple(row) for row in cursor.fetchall()]


def read_policy(cursor) -> FailoverPolicy:
    """The policy on `cursor`'s open transaction. Absent tables are code defaults
    (``provisioned`` is false when :data:`POLICY_TABLE` is not there yet)."""
    tables = _tables(cursor)
    bounds, notes = _bounds(cursor, tables)
    policy = load_policy(_rows(cursor, tables), bounds, provisioned=POLICY_TABLE in tables)
    if notes:
        policy = replace(policy, warnings=(*notes, *policy.warnings))
    return policy


def plan_changes(cursor, scope: str, changes: Mapping[str, int | None]) -> FailoverPolicy:
    """Validate `changes` for `scope` against the policy on `cursor`'s open transaction, writing
    nothing. Refuses any change that would itself be refused on load or would newly refuse
    another row (e.g. a floor raised above a hive's executor override). Returns the policy as it
    would load."""
    if scope != FLEET_SCOPE and not (isinstance(scope, str) and _PREFIX.fullmatch(scope)):
        raise FailoverPolicyError("failover policy scope must be a hive prefix or '*'")
    if FLOOR_SETTING in changes and scope != FLEET_SCOPE:
        raise FailoverPolicyError("the executor floor is fleet-wide: set it without a PREFIX")
    for setting, seconds in changes.items():
        if setting not in SETTINGS:
            raise FailoverPolicyError(f"unknown failover setting {setting!r}")
        if seconds is not None and _whole_seconds(seconds) is None:
            raise FailoverPolicyError(f"{setting} must be a positive whole number of seconds")
    tables = _tables(cursor)
    if POLICY_TABLE not in tables:
        raise FailoverPolicyError(
            f"{POLICY_TABLE} is not provisioned; the operator creates it first "
            "(`bh hq placement policy` prints the DDL)"
        )
    bounds, _notes = _bounds(cursor, tables)
    current = _rows(cursor, tables)
    before = load_policy(current, bounds)
    kept = [row for row in current if not (_text(row[0]) == scope and _text(row[1]) in changes)]
    after = load_policy(kept + [(scope, s, v) for s, v in changes.items() if v is not None], bounds)
    known = {(r.scope, r.setting, r.seconds) for r in before.refusals}
    fresh = [r for r in after.refusals if (r.scope, r.setting, r.seconds) not in known]
    if fresh:
        raise FailoverPolicyError(
            "failover policy change refused (never clamped): "
            + "; ".join(r.describe() for r in fresh)
        )
    return after


def apply_changes(cursor, scope: str, changes: Mapping[str, int | None]) -> FailoverPolicy:
    """:func:`plan_changes`, then write them on `cursor`'s open transaction (the caller
    commits). Refused changes write nothing."""
    after = plan_changes(cursor, scope, changes)
    for setting, seconds in sorted(changes.items()):
        if seconds is None:
            cursor.execute(
                f"DELETE FROM {POLICY_TABLE} WHERE scope=%s AND setting=%s", (scope, setting)
            )
        else:
            cursor.execute(
                f"INSERT INTO {POLICY_TABLE} (scope,setting,seconds) VALUES (%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE seconds=VALUES(seconds)",
                (scope, setting, seconds),
            )
    return after


def effective(policy: FailoverPolicy, prefix: str | None = None) -> dict[str, float | None]:
    """``{role: failover_after}`` for `prefix` (or the fleet defaults), viewer included."""
    roles = (*ROLES, "viewer")
    return {role: policy.failover_after(prefix or "", role) for role in roles}


def provision_statements(settings: Mapping | None = None) -> tuple[str, ...]:
    """The operator DDL for :data:`POLICY_TABLE` plus the director's grant on it (when the
    settings name the director account). Rendered, never executed."""
    statements = list(FAILOVER_POLICY_SCHEMA)
    binding = (settings or {}).get("placement_writer") or {}
    user, database = binding.get("user"), binding.get("database")
    if isinstance(user, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,31}", user):
        safe = isinstance(database, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", database)
        target = f"{database}.{POLICY_TABLE}" if safe else POLICY_TABLE
        statements.append(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {target} TO '{user}'@'<host>'")
    return tuple(statements)
