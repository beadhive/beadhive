"""Bounded retention for signed-mode heartbeat inbox rows (bh-ce886).

In ``hq.sql.liveness: signed`` each heartbeat adds one row to the frame's own
``hq_live_inbox_<principal>_<epoch>`` table, and nothing removed them (the 0.22.0 trust
delta "no pruning yet"). This module decides which heartbeat rows an operator may delete.
The DELETE itself runs with the operator's ``authority_writer`` credential
(:meth:`beadhive.hq_sql_operator.SqlRuntimeOperator.prune_inbox`); frames keep their
``SELECT, INSERT, UPDATE`` inbox grant and gain no DELETE.

The bound, and why it never removes a beat anyone still needs
-------------------------------------------------------------

A heartbeat row is deleted only when **all** of these hold:

1. ``kind = 'heartbeat'``. ``registration`` and ``hive_lease`` rows are never touched: the
   receiver re-verifies those by request ID as prior incumbent evidence.
2. Its payload parses as a FrameLease and its *claimed* signed ``renewTime`` +
   ``leaseDurationSeconds`` + :data:`SKEW_SECONDS` + the retention margin is at or before the
   operator's clock. A 0.22.x signed reader counts a beat only while
   ``reader_now - renewTime < leaseDurationSeconds`` (``accepted_until``), with at most
   :data:`SKEW_SECONDS` of clock skew between hosts, and the receiver refuses a first
   observation whose age reaches ``leaseDurationSeconds``. So a row past that point can no
   longer make a frame eligible for either reader, whether or not its signature verifies;
   an unverifiable row never counted at all. The retention margin is extra slack on top.
3. It is not the newest heartbeat whose canonical payload verifies against the
   incarnation's granted key and binds to that incarnation (frame, holder, instance, epoch,
   key). That one is always kept, however old: ``heartbeat_report`` derives the next
   ``seq`` from it in signed mode, and status surfaces report the last beat a stale frame
   sent.

Rows whose payload does not parse (no claimed time) are left in place and counted, so an
operator can see sender misbehaviour. With an honest sender the table therefore holds at
most one beat per heartbeat interval inside ``lease + skew + retention``, plus the one
newest verified beat. A frame can still INSERT junk into its own inbox; this bound is about
honest growth, which was the unbounded part.

Retention margin resolution: ``--retention`` > ``hq.sql.inbox_retention_s`` (operator
settings file) > ``$BH_HQ_INBOX_RETENTION`` > :data:`INBOX_RETENTION_DEFAULT_S`. Pruning
only happens when an operator runs ``bh hq authority prune-inbox``; nothing prunes on its own.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import NamedTuple

from .hq_authority_ceiling import parse_duration
from .hq_sql_runtime import SIGNED_LIVENESS_SKEW_SECONDS, _payload_bytes

#: Seconds kept beyond a beat's own lease + skew before it may be pruned (1 hour).
INBOX_RETENTION_DEFAULT_S = 3600
ENV_VAR = "BH_HQ_INBOX_RETENTION"
SETTINGS_KEY = "hq.sql.inbox_retention_s"
#: Same clock-skew window the signed reader and the receiver apply.
SKEW_SECONDS = SIGNED_LIVENESS_SKEW_SECONDS


class Retention(NamedTuple):
    seconds: float
    source: str


def resolve_retention(cli=None, settings=None, env=None) -> Retention:
    """Resolve the retention margin and name its source."""
    environ = os.environ if env is None else env
    for raw, source in (
        (cli, "--retention"),
        (settings, f"operator settings {SETTINGS_KEY}"),
        (environ.get(ENV_VAR), f"${ENV_VAR}"),
    ):
        if raw is None or raw == "":
            continue
        try:
            return Retention(parse_duration(raw), source)
        except ValueError as exc:
            raise ValueError(f"inbox retention from {source}: {exc}") from None
    return Retention(INBOX_RETENTION_DEFAULT_S, "built-in default (1h)")


@dataclass
class PrunePlan:
    """Which heartbeat request IDs to delete, and what was deliberately kept."""

    delete: list[str] = field(default_factory=list)
    newest_verified: str | None = None
    retained: int = 0
    unparseable: int = 0


def _claimed(body: bytes):
    from .hq_framelease_contracts import HeartbeatLease

    try:
        envelope = json.loads(body)
        spec = envelope["spec"]
        lease = HeartbeatLease.model_validate(
            {key: value for key, value in spec.items() if key != "signature"}
        )
        return envelope, lease
    except (ValueError, TypeError, KeyError, AttributeError):
        return None, None


def plan_prune(rows, route, public_key: str, *, now: float, retention_s: float) -> PrunePlan:
    """Plan a prune of one inbox's ``(request_id, payload)`` heartbeat rows.

    Pure: no SQL. ``route`` is the incarnation's :class:`PrincipalBinding` and
    ``public_key`` its granted key. See the module docstring for the rule.
    """
    from .hq_sql_signatures import canonical, verify_heartbeat

    plan = PrunePlan()
    candidates = []
    for request_id, payload in rows:
        body = _payload_bytes(payload)
        envelope, lease = (None, None) if body is None else _claimed(body)
        if lease is None:
            plan.unparseable += 1
            continue
        # A non-canonical body never counts for the reader, so it can never be the kept beat.
        exact = body == canonical(envelope)
        candidates.append(
            (lease.observed_at, lease.seq, request_id, envelope if exact else None, lease)
        )
    # Newest claimed first: the first that verifies and binds is the newest verified beat.
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    binding = (
        route.frame_id,
        route.holder_identity,
        route.instance_ref,
        route.epoch,
        route.signer_fingerprint,
    )
    for _observed, _seq, request_id, envelope, _lease in candidates:
        if envelope is None:
            continue
        try:
            lease, _digest = verify_heartbeat(envelope, granted_public_key=public_key)
        except (ValueError, TypeError, KeyError):
            continue
        if (
            lease.frame_id,
            lease.holderIdentity,
            lease.instance_ref,
            lease.epoch,
            lease.key_id,
        ) == binding:
            plan.newest_verified = request_id
            break
    for observed, _seq, request_id, _envelope, lease in candidates:
        expired = observed + lease.leaseDurationSeconds + SKEW_SECONDS + retention_s <= now
        if expired and request_id != plan.newest_verified:
            plan.delete.append(request_id)
        else:
            plan.retained += 1
    return plan
