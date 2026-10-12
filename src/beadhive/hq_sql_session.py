"""Per-incarnation session and evidence rows in ``dolt-server`` HQ (bh-owqdg, P-M9; ADR §5).

``docs/design/hive-writer-partitioning-adr.md`` §5 replaces the receiver-mediated heartbeat with
two operator-provisioned, single-row tables per frame incarnation:

* ``frame_<principal>_<epoch>_session`` — liveness. ``CHECK (id = 1)``, UPDATE-only for that
  principal, ``dolt_ignore``'d before creation. An operator ``BEFORE UPDATE`` trigger sets
  ``NEW.renewed_at = UTC_TIMESTAMP(6)``; the body never writes another table (``bh-wtsrc``
  E1–E3). A renewal is one ``UPDATE`` (:data:`RENEW_SQL`), on its own timer, independent of
  measurement (:class:`SessionRenewer`).
* ``frame_<principal>_<epoch>_evidence`` — conformance, written by the conformance job on its
  own timer (:func:`publish_evidence`). ``measured_at`` is server-stamped the same way; expiry is
  ``measured_at`` plus the operator's ``evidence_ttl_s``, computed by the reader.

**Eligibility** is one statement (:data:`ELIGIBILITY_SQL`) joining the committed grant route
(``hq_principal_registry AS OF <head>``), the session row, the evidence row, the operator's
committed :data:`~beadhive.hq_sql_runtime_schema.LIVENESS_POLICY_TABLE` and the placement row.
It returns the predicate names unchanged — ``authenticated_fresh_heartbeat``,
``conformance_pass``, ``release_matches``, ``current_hive_lease_holder`` — plus the stamps a
claim records (:meth:`SessionObservation.stamps`, replacing the receiver's T15 audit).

**Data is the switch.** A reader uses session rows exactly when this incarnation's two tables
exist (:func:`incarnation_switch`); there is no config key. During Φ3 an incarnation has both
tables *and* its signed inbox (registry ``inbox_table`` still names the inbox, so 0.22.x readers
and the receiver keep working, and the sender dual-writes). A session-only incarnation is
registered under its session table name.

**Account hardening ships with the rows** (condition 15): frame accounts are
``'<principal>'@'<frame address>'`` with ``REQUIRE SSL``; :func:`check_provisioning` refuses a
wildcard or TLS-optional account and any hive database co-hosted on the HQ server
(:func:`beadhive.hq_sql_session_provision.check_provisioning`, the operator-side module).

git HQ is untouched: it keeps the signed ``HeartbeatLease`` and gets no session row.

Storage (bh-xnn5p): every statement -- the DDL, the renewal and evidence ``UPDATE``\ s and the
eligibility read -- is the HQ storage port's Dolt adapter's
(:mod:`beadhive_hq_storage_dolt`, through :func:`beadhive.hq_storage_composition.
runtime_statements` on the caller's own cursor). This module validates identifiers and values
and owns the freshness THRESHOLDS: the eligibility read returns each row's age by the HQ
server's clock and the committed TTLs, and :func:`within_ttl` decides. The director's failover
reads the same session rows through the port's ``Liveness.ages``
(:func:`beadhive.failover_observer.session_staleness`). Holds no SQL. Typer-free.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from beadhive_hq_storage_dolt import ELIGIBILITY_SQL, IGNORE_PATTERN, RENEW_SQL, STAMP_COLUMN
from beadhive_hq_storage_dolt import account_statements as _account_statements
from beadhive_hq_storage_dolt import evidence_ddl as _evidence_ddl
from beadhive_hq_storage_dolt import liveness_schema_statements as _liveness_schema_statements
from beadhive_hq_storage_dolt import session_ddl as _session_ddl

from .hq_sql_runtime_schema import (
    LIVENESS_POLICY_SCHEMA,
    LIVENESS_POLICY_TABLE,
    RuntimeSchemaError,
    evidence_table,
    inbox_table,
    session_table,
)

__all__ = [
    "DEFAULT_EVIDENCE_TTL_S",
    "DEFAULT_SESSION_TTL_S",
    "ELIGIBILITY_SQL",
    "HIVE_DATABASE_MARKERS",
    "IGNORE_PATTERN",
    "MAX_TTL_S",
    "RENEW_SQL",
    "EvidenceReport",
    "LivenessPolicy",
    "NotSwitched",
    "SessionError",
    "SessionObservation",
    "SessionRenewer",
    "SessionRoute",
    "account_statements",
    "evidence_ddl",
    "incarnation_switch",
    "liveness_schema_statements",
    "pinned_address",
    "publish_evidence",
    "read_eligibility",
    "resolve_route",
    "session_ddl",
    "within_ttl",
]

#: Ignore rule for every incarnation table (the adapter's ``IGNORE_PATTERN``, ``frame_*``);
#: committed before any such table exists (bh-v0k3i ordering), so session/evidence rows never
#: enter a versioned authority commit (``bh-wtsrc`` E6).
#: Code defaults for the operator's committed policy row. Every value is configurable at
#: provisioning (:func:`liveness_schema_statements`) and lives in data, never ``host.yaml``.
#: A session is fresh for five renewal intervals of the sender's 60 s timer; evidence for one
#: conformance staleness bound (``heartbeat_conformance.CONFORMANCE_MAX_AGE_SECONDS``).
DEFAULT_SESSION_TTL_S = 300
DEFAULT_EVIDENCE_TTL_S = 900
#: Upper bound for either TTL (one week). A larger value is refused, never clamped.
MAX_TTL_S = 7 * 86400
#: Databases whose presence of any of these tables marks a hive database (bd's schema, or the
#: in-data writer fence). None may be co-hosted on the HQ server (ADR §5, T16).
HIVE_DATABASE_MARKERS = ("issues", "bh_writer", "bh_epoch_live")
_SYSTEM_SCHEMAS = frozenset({"information_schema", "mysql", "performance_schema", "sys"})
_UNMEASURED = "unmeasured"
_EVIDENCE_STATUSES = frozenset({"conformant", "non-conformant", "unavailable", _UNMEASURED})
_DIGEST = re.compile(r"(sha256:[0-9a-f]{64})?")
_ADDRESS = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.:-]{0,253}[A-Za-z0-9])?")
_DATABASE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")

#: One renewal (the adapter's ``RENEW_SQL``): the operator trigger stamps ``renewed_at``; the
#: frame supplies nothing. ``_STAMP_COLUMN`` names each table's server-stamped column.
_STAMP_COLUMN = STAMP_COLUMN


class SessionError(ValueError):
    """Session/evidence provisioning, renewal or read refused or unavailable."""


class NotSwitched(SessionError):
    """This incarnation has no session tables: the hive's data has not switched it on."""


# =============================================================================================
# Operator DDL
# =============================================================================================


@dataclass(frozen=True)
class LivenessPolicy:
    """The operator's committed TTLs. Validated on load and on provisioning; refused, never
    clamped, outside ``1 .. MAX_TTL_S`` seconds."""

    session_ttl_s: int = DEFAULT_SESSION_TTL_S
    evidence_ttl_s: int = DEFAULT_EVIDENCE_TTL_S

    def __post_init__(self):
        for name in ("session_ttl_s", "evidence_ttl_s"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= MAX_TTL_S:
                raise SessionError(f"{name} must be an integer from 1 to {MAX_TTL_S} seconds")


def liveness_schema_statements(policy: LivenessPolicy | None = None) -> tuple[str, ...]:
    """Once per runtime database, before any incarnation table: the ``frame_*`` ignore rule and
    the committed policy row, in one operator commit (the adapter renders it)."""
    policy = policy or LivenessPolicy()
    return _liveness_schema_statements(
        LIVENESS_POLICY_SCHEMA, policy.session_ttl_s, policy.evidence_ttl_s
    )


def session_ddl(principal: str, epoch: int) -> tuple[str, ...]:
    """The session table, its stamping trigger and its one seeded row.

    The row is seeded ``renewed_at = NULL`` (never renewed): there is deliberately no INSERT
    trigger, so provisioning alone never makes a frame look live. Only an UPDATE — the frame's
    renewal — stamps server time."""
    return _session_ddl(session_table(principal, epoch), epoch)


def evidence_ddl(principal: str, epoch: int) -> tuple[str, ...]:
    """The evidence table, its stamping trigger and its one seeded (unmeasured) row."""
    return _evidence_ddl(evidence_table(principal, epoch), epoch)


def pinned_address(address: str) -> bool:
    """A literal host or address: no MySQL host wildcard (``%``/``_``), netmask or empty host."""
    return (
        isinstance(address, str)
        and bool(_ADDRESS.fullmatch(address))
        and "%" not in address
        and "_" not in address
    )


def _account(principal: str, address: str) -> str:
    session_table(principal, 0)  # validates the principal identifier
    if not pinned_address(address):
        raise SessionError("frame account must be pinned to its frame address (no wildcard host)")
    return f"'{principal}'@'{address}'"


def account_statements(
    principal: str, epoch: int, *, frame_address: str, database: str, inbox: bool = False
) -> tuple[str, ...]:
    """The host-pinned, TLS-required account and its grants. ``CREATE USER`` takes the password
    as the one bound parameter (``%s``). Grants on the HQ authority tables a frame already reads
    are the operator's existing procedure and are not repeated here."""
    if not isinstance(database, str) or not _DATABASE.fullmatch(database):
        raise SessionError("invalid runtime database identifier")
    account = _account(principal, frame_address)
    return _account_statements(
        account,
        database,
        session=session_table(principal, epoch),
        evidence=evidence_table(principal, epoch),
        inbox=inbox_table(principal, epoch) if inbox else None,
    )


def _statements(cursor):
    from .hq_storage_composition import runtime_statements

    return runtime_statements(cursor)


def _existing(cursor, names: Iterable[str]) -> set[str]:
    return _statements(cursor).incarnation_tables(tuple(names))


# =============================================================================================
# The data switch and the frame's writes
# =============================================================================================


@dataclass(frozen=True)
class SessionRoute:
    principal: str
    epoch: int
    session: str
    evidence: str


def incarnation_switch(cursor, principal: str, epoch: int) -> SessionRoute | None:
    """The switch: ``None`` when neither table exists (legacy reader), the route when both do.
    Exactly one existing is a half-provisioned incarnation and fails closed."""
    session, evidence = session_table(principal, epoch), evidence_table(principal, epoch)
    present = _existing(cursor, (session, evidence))
    if not present:
        return None
    if present != {session, evidence}:
        raise SessionError("incarnation session/evidence tables are half-provisioned")
    return SessionRoute(principal, epoch, session, evidence)


def resolve_route(cursor, *, database: str | None = None) -> SessionRoute:
    """The authenticated principal's own session route, from its registry row.

    Raises :class:`NotSwitched` when the incarnation has no session tables."""
    sql = _statements(cursor)
    user, current, branch = sql.session_identity()
    if branch != "main" or (database is not None and current != database):
        raise SessionError("session route endpoint mismatch")
    principal = str(user).split("@", 1)[0]
    rows = sql.registry_epochs(principal)
    if len(rows) != 1:
        raise SessionError("authenticated frame principal is not provisioned")
    try:
        route = incarnation_switch(sql, principal, int(rows[0][0]))
    except RuntimeSchemaError:
        raise SessionError(
            "authenticated frame principal is not a provisioned identifier"
        ) from None
    if route is None:
        raise NotSwitched("incarnation has no session tables (legacy liveness)")
    return route


class SessionRenewer:
    """The renewal loop: one ``UPDATE`` per tick, on its own timer, never waiting on
    measurement (ADR §5). ``connect`` opens a fresh DB-API connection per tick (the strict HQ
    transport binds each connection to one operation deadline); the route is resolved once and
    re-resolved only after a failed tick."""

    def __init__(self, connect: Callable[[], object], *, database: str | None = None):
        self._connect = connect
        self._database = database
        self.route: SessionRoute | None = None
        self.renewals = 0

    def tick(self) -> SessionRoute:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                sql = _statements(cursor)
                if self.route is None:
                    self.route = resolve_route(sql, database=self._database)
                if sql.renew_session(self.route.session) != 1:
                    raise SessionError("session row missing: renewal matched no row")
            connection.commit()
            self.renewals += 1
            return self.route
        except BaseException:
            self.route = None
            try:
                connection.rollback()
            except Exception:  # noqa: BLE001 - the original failure is what matters
                pass
            raise
        finally:
            connection.close()

    def run(
        self,
        *,
        interval_s: float,
        stop: Callable[[float], bool],
        on_error: Callable[[BaseException], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Tick every ``interval_s`` until ``stop(wait_s)`` returns True (an Event's ``wait``).

        ``clock`` measures each tick's duration (monotonic seconds); tests pass a fake clock so
        ``wait_s`` is exactly ``interval_s`` minus virtual time (bh-qwbrz)."""
        if type(interval_s) not in (int, float) or not math.isfinite(interval_s) or interval_s <= 0:
            raise SessionError("renewal interval must be a positive number of seconds")
        while True:
            started = clock()
            try:
                self.tick()
            except NotSwitched:
                raise
            except Exception as exc:  # noqa: BLE001 - one failed renewal never ends the loop
                if on_error is not None:
                    on_error(exc)
            if stop(max(0.0, interval_s - (clock() - started))):
                return


@dataclass(frozen=True)
class EvidenceReport:
    """What the conformance job measured, as written to the evidence row. Self-attested (T9):
    the reader compares release digest and profile against the grant."""

    epoch: int
    release_id: str
    release_digest: str
    profile: str
    status: str
    report_digest: str = ""

    def __post_init__(self):
        if (
            type(self.epoch) is not int
            or self.epoch < 0
            or self.status not in _EVIDENCE_STATUSES
            or not _DIGEST.fullmatch(self.release_digest or "")
            or not _DIGEST.fullmatch(self.report_digest or "")
            or len(self.release_id) > 128
            or len(self.profile) > 64
        ):
            raise SessionError("invalid evidence report")


def publish_evidence(connection, report: EvidenceReport, *, database: str | None = None):
    """The conformance job's one ``UPDATE`` of its own evidence row (server-stamped)."""
    try:
        with connection.cursor() as cursor:
            sql = _statements(cursor)
            route = resolve_route(sql, database=database)
            if report.epoch != route.epoch:
                raise SessionError("evidence epoch differs from the authenticated incarnation")
            written = sql.publish_evidence(
                route.evidence,
                (
                    report.epoch,
                    report.release_id,
                    report.release_digest,
                    report.profile,
                    report.status,
                    report.report_digest,
                ),
            )
            if written != 1:
                raise SessionError("evidence row missing: publication matched no row")
        connection.commit()
        return route
    except BaseException:
        try:
            connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise


# =============================================================================================
# The reader: one statement
# =============================================================================================

#: Eligibility is ONE statement, the adapter's ``ELIGIBILITY_SQL``: grant route (committed,
#: ``AS OF`` the authority head) ⋈ session ⋈ evidence ⋈ policy (committed) ⟕ placement, each age
#: by the HQ server's own clock. The freshness thresholds are :func:`within_ttl`'s (bh-xnn5p):
#: ``age_us < ttl * 1000000`` in whole microseconds, exactly the comparison the statement made
#: before, so a row is fresh up to, and never past, its TTL (bh-7crof).


def within_ttl(age_us, ttl_s: int) -> bool:
    """Whether a row aged ``age_us`` microseconds (by the store clock; ``None`` when it was
    never stamped) is still inside a ``ttl_s``-second TTL."""
    return age_us is not None and int(age_us) < int(ttl_s) * 1_000_000


def _policy(rows) -> LivenessPolicy:
    if len(rows) != 1:
        raise SessionError(f"{LIVENESS_POLICY_TABLE} must hold exactly one row")
    try:
        return LivenessPolicy(int(rows[0][0]), int(rows[0][1]))
    except (TypeError, ValueError):
        raise SessionError(f"{LIVENESS_POLICY_TABLE} values invalid") from None


def _iso(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC).isoformat()
    return str(value)


@dataclass(frozen=True)
class SessionObservation:
    """The switched reader's observation. Duck-compatible with ``VerifiedObservation`` (status,
    verified, fresh, age_seconds, lease, candidate, age_basis) for listing and eviction, and
    carries the statement's predicates and stamps. ``lease`` is always ``None``: there is no
    signed beat on this path."""

    status: str
    fresh: bool
    age_seconds: float | None
    authenticated_fresh_heartbeat: bool
    conformance_pass: bool
    release_matches: bool
    current_hive_lease_holder: bool
    session_epoch_matches: bool
    renewed_at: str
    measured_at: str
    evidence_age_seconds: float | None
    evidence_status: str
    evidence_release: dict
    evidence_profile: str
    evidence_digest: str
    session_ttl_s: int
    evidence_ttl_s: int
    session_table: str
    evidence_table: str
    candidate: bool = False
    verified: bool = True
    lease: None = None
    reason: str = ""
    age_basis: str = "hq-server-session-row"
    carrier: str = "session"
    predicates: dict = field(default_factory=dict)

    @property
    def sha(self) -> str:
        return self.evidence_digest

    def stamps(self) -> dict:
        """The admitted stamps a claim record carries (replaces the receiver's T15 audit)."""
        return {
            "carrier": self.carrier,
            "session_table": self.session_table,
            "session_renewed_at": self.renewed_at,
            "evidence_measured_at": self.measured_at,
            "evidence_digest": self.evidence_digest,
            "evidence_status": self.evidence_status,
            "predicates": dict(self.predicates),
        }


def _seconds(value) -> float | None:
    if value is None:
        return None
    return int(value) / 1e6


def read_eligibility(
    cursor,
    *,
    head: str,
    principal: str,
    epoch: int,
    desired_release_digest: str,
    desired_profile: str,
    prefix: str | None = None,
    candidate: bool = False,
) -> SessionObservation:
    """Run :data:`ELIGIBILITY_SQL` once, inside the caller's authority transaction, and judge
    its ages against the committed TTLs (:func:`within_ttl`)."""
    sql = _statements(cursor)
    route = incarnation_switch(sql, principal, epoch)
    if route is None:
        raise NotSwitched("incarnation has no session tables (legacy liveness)")
    rows = sql.eligibility(
        session=route.session,
        evidence=route.evidence,
        head=head,
        principal=principal,
        epoch=epoch,
        desired_profile=desired_profile,
        desired_release_digest=desired_release_digest,
        prefix=prefix,
    )
    if len(rows) != 1:
        raise SessionError("session eligibility row missing (registry, policy or row absent)")
    (
        grant_epoch,
        session_epoch,
        renewed_at,
        _evidence_epoch,
        release_id,
        release_digest,
        profile,
        status,
        _report_digest,
        measured_at,
        session_age_us,
        evidence_age_us,
        session_ttl,
        evidence_ttl,
        epoch_current,
        evidence_conformant,
        release,
        holder,
        digest,
    ) = rows[0]
    policy = _policy([(session_ttl, evidence_ttl)])
    fresh = bool(epoch_current) and within_ttl(session_age_us, policy.session_ttl_s)
    conformance = bool(evidence_conformant) and within_ttl(evidence_age_us, policy.evidence_ttl_s)
    predicates = {
        "authenticated_fresh_heartbeat": fresh,
        "conformance_pass": bool(conformance),
        "release_matches": bool(release),
        "current_hive_lease_holder": bool(holder),
    }
    return SessionObservation(
        status="fresh" if fresh else ("missing" if renewed_at is None else "stale"),
        fresh=fresh,
        age_seconds=_seconds(session_age_us),
        authenticated_fresh_heartbeat=fresh,
        conformance_pass=bool(conformance),
        release_matches=bool(release),
        current_hive_lease_holder=bool(holder),
        session_epoch_matches=session_epoch == grant_epoch,
        renewed_at=_iso(renewed_at),
        measured_at=_iso(measured_at),
        evidence_age_seconds=_seconds(evidence_age_us),
        evidence_status=str(status),
        evidence_release={"id": str(release_id), "digest": str(release_digest)},
        evidence_profile=str(profile),
        evidence_digest="sha256:" + str(digest) if digest else "",
        session_ttl_s=policy.session_ttl_s,
        evidence_ttl_s=policy.evidence_ttl_s,
        session_table=route.session,
        evidence_table=route.evidence,
        candidate=candidate,
        predicates=predicates,
    )
