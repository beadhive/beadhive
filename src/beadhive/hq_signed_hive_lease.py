"""Receiver-free hive-lease acceptance for ``hq.sql.liveness: signed`` (bh-qv8ig, 0.22.8).

The 0.22.x bridge until 0.23 removes the receiver from this path. In receiver mode a hive
lease exists only once the separately deployed trusted receiver
(:meth:`beadhive.hq_sql_receiver.SqlTrustedReceiver.accept_hive_lease`) verifies a frame's
signed proposal and wins one protected global lease-row CAS. When that receiver silently
rejects or never processes a proposal, adopt strands with the hive fence advanced and no lease.

In signed hive-lease mode (:func:`beadhive.hq_sql_runtime.signed_hive_lease`) readers accept
the frame's *own* signed proposals at read time instead, the way 0.22.0 signed liveness reads
heartbeats. The receiver's global CAS is replaced by the hive's remote epoch fence
(``refs/bh/epoch``), whose Git CAS already admits exactly one adopter per epoch:

* A proposal counts only if it passes the request checks the receiver makes (signature under
  the active grant's key, exact request shape and digest, principal/frame/holder/instance/
  epoch/key/audience/config/beadyard identity, unforced operation, a well-formed bounded
  lease record) AND the prefix was in the operator-signed hive policy *at the authority
  revision the frame signed against* (so a proposal the receiver would have rejected can
  never be replayed into a lease once policy changes) AND its lease epoch equals the
  current fence epoch AND the fence names its holder.
* Proposals at the fence epoch from a host the fence does not name are race losers and are
  ignored. Two qualifying but different tenures at one epoch, a release that does not match
  the tenure, or a protected receiver row at the fence epoch that disagrees with the fence,
  all fail closed (:class:`SignedHiveLeaseConflict`).
* The receiver's accepted row still counts when its epoch equals the fence epoch, so leases
  accepted before the upgrade keep working. A row at an older epoch is superseded.
* With the fence naming another host whose proposals this frame cannot read, the reader
  reports that host as an advisory holder (never "free"), so an unforced adopt cannot seize
  it; eviction still goes through the authenticated-staleness path.

Pure and SQL-free: :meth:`beadhive.hq_sql_runtime.SqlRuntimeAuthority.read_frame_composite`
gathers :class:`beadhive.hq_sql_runtime.HiveLeaseEvidence`; this module only decides.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .host_lease_contracts import HostLease, _parse_stamp

#: A hive-lease proposal envelope larger than this is never considered (receiver bound).
MAX_PROPOSAL_BYTES = 65536
#: Longest tenure a signed adopt/renew may claim (the receiver's ``now + 86400`` bound,
#: measured from the lease's own signed ``adopted_at`` since there is no acceptance clock).
MAX_TENURE_SECONDS = 86400

_REQUEST_FIELDS = frozenset(
    {
        "domain",
        "request_id",
        "principal",
        "frame_id",
        "holder_identity",
        "instance_ref",
        "epoch",
        "key_fingerprint",
        "audience",
        "config_revision",
        "authority_revision",
        "prefix",
        "expected_revision",
        "operation",
        "force",
        "lease",
    }
)
_LEASE_FIELDS = frozenset({"host_id", "label", "epoch", "adopted_at", "expires_at"})


class SignedHiveLeaseConflict(ValueError):
    """Qualifying evidence at the current fence epoch disagrees; no holder is granted."""


@dataclass(frozen=True)
class Resolution:
    """The resolved hive lease: ``source`` is ``proposal``, ``receiver``, ``fence`` or ``none``."""

    revision: str
    lease: HostLease | None
    source: str


@dataclass(frozen=True)
class _Candidate:
    source: str
    revision: str
    lease: HostLease


def proposal_revision(request: dict) -> str:
    """The lease revision of an accepted request — the same digest the receiver records."""
    from .hq_sql_signatures import canonical

    return hashlib.sha256(canonical(request)).hexdigest()


def _lease_record(raw) -> HostLease | None:
    if (
        not isinstance(raw, dict)
        or set(raw) != _LEASE_FIELDS
        or any(
            type(raw[key]) is not str for key in ("host_id", "label", "adopted_at", "expires_at")
        )
        or type(raw["epoch"]) is not int
        or raw["epoch"] < 1
        or _parse_stamp(raw["adopted_at"]) <= 0
        or _parse_stamp(raw["expires_at"]) <= 0
    ):
        return None
    return HostLease(**raw)


def verify_proposal(row, *, prefix, route, record, evidence, fence):
    """``(request, lease)`` for one inbox row that qualifies at `fence`, else ``None``.

    Never raises: the inbox is sender-written, so one malformed row must not mask a good one.
    """
    from . import hq_authority_guard as guard
    from .host_lease_contracts import lease_ref
    from .hq_sql_signatures import canonical, verify_hive_request

    try:
        request_id, body, payload_sha = row
        if isinstance(body, memoryview):
            body = body.tobytes()
        if isinstance(body, str):
            body = body.encode()
        if not isinstance(body, bytes) or len(body) > MAX_PROPOSAL_BYTES:
            return None
        digest = hashlib.sha256(body).hexdigest()
        envelope = json.loads(body)
        if digest != payload_sha or body != canonical(envelope):
            return None
        request, signed_sha = verify_hive_request(envelope, granted_public_key=record["public_key"])
        authority = record["authority"]
        fields = set(_REQUEST_FIELDS)
        if authority.get("beadyard_id") is not None:
            fields.add("beadyard_id")
        if signed_sha != digest or set(request) != fields:
            return None
        lease_ref(request["prefix"])
        if (
            request["request_id"] != request_id
            or request["prefix"] != prefix
            or request["principal"] != route.principal
            or request["frame_id"] != route.frame_id
            or request["holder_identity"] != route.holder_identity
            or request["instance_ref"] != route.instance_ref
            or request["epoch"] != route.epoch
            or request["key_fingerprint"] != route.signer_fingerprint
            or authority["holder_identity"] != route.holder_identity
            or authority["instance_ref"] != route.instance_ref
            or authority["epoch"] != route.epoch
            or authority["key_fingerprint"] != route.signer_fingerprint
            or request["audience"] != authority["audience"]
            or request["config_revision"] != authority["config_revision"]
            or request.get("beadyard_id") != authority.get("beadyard_id")
            or request["force"] is not False
            or request["operation"] not in {"adopt", "renew", "release"}
            or not isinstance(request["expected_revision"], str)
        ):
            return None
        # The prefix had to be authorized when the frame signed: the receiver rejects a
        # prefix outside the signed hive policy, and that rejection must not be undone later.
        signed_policies = evidence.policies_at.get(request["authority_revision"])
        policy = (signed_policies or {}).get(prefix)
        if (
            not isinstance(policy, dict)
            or policy.get("config_revision") != (authority["config_revision"])
        ):
            return None
        lease = _lease_record(request["lease"])
        if lease is None or lease.epoch != fence.epoch or fence.host_id != route.holder_identity:
            return None
        if request["operation"] == "release":
            return (request, lease) if lease.host_id == "" else None
        adopted, expires = _parse_stamp(lease.adopted_at), _parse_stamp(lease.expires_at)
        caps = record["desired"]["caps"]
        if (
            lease.host_id != route.holder_identity
            or not adopted < expires <= adopted + MAX_TENURE_SECONDS
            or type(caps.get("max_sessions")) is not int
            or caps["max_sessions"] <= 0
        ):
            return None
        guard.requirements(policy.get("requires", {}), caps)
        if (
            request["operation"] == "adopt"
            and authority.get("beadyard_id") is not None
            and evidence.registration_signer != route.signer_fingerprint
        ):
            return None
        return request, lease
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def resolve(*, prefix, route, slot, record, fence, evidence, receiver=None) -> Resolution:
    """Resolve `prefix`'s hive lease at the current `fence` (an ``EpochFence`` or ``None``).

    `receiver` is the protected receiver row as ``(revision, HostLease)``, or ``None``.
    Returned leases carry ``advisory_expiry`` (signed liveness): tenure rests on the holder's
    signed heartbeat, which holder reads check separately.
    """
    if fence is None:
        # Never fenced: the two-phase adopt always installs the fence first.
        return Resolution(receiver[0] if receiver else "", None, "none")
    candidates = []
    if slot == "active" and evidence is not None:
        for row in evidence.rows:
            verified = verify_proposal(
                row, prefix=prefix, route=route, record=record, evidence=evidence, fence=fence
            )
            if verified is not None:
                request, lease = verified
                candidates.append(_Candidate("proposal", proposal_revision(request), lease))
    if receiver is not None and receiver[1].epoch == fence.epoch:
        if not receiver[1].is_tombstone and receiver[1].host_id != fence.host_id:
            raise SignedHiveLeaseConflict(
                f"{prefix}: protected lease at epoch {fence.epoch} names a different host "
                "than the epoch fence"
            )
        candidates.append(_Candidate("receiver", receiver[0], receiver[1]))
    holders = [c for c in candidates if not c.lease.is_tombstone]
    releases = [c for c in candidates if c.lease.is_tombstone]
    tenures = {(c.lease.host_id, c.lease.label, c.lease.adopted_at) for c in holders}
    if len(tenures) > 1:
        raise SignedHiveLeaseConflict(
            f"{prefix}: conflicting hive lease tenures at fence epoch {fence.epoch}"
        )
    released = {c.lease.adopted_at for c in releases}
    if len(released) > 1 or (tenures and released and released != {next(iter(tenures))[2]}):
        raise SignedHiveLeaseConflict(
            f"{prefix}: hive lease release does not match the tenure at epoch {fence.epoch}"
        )
    if releases:
        chosen = max(releases, key=lambda c: (c.source == "receiver", c.revision))
        return Resolution(chosen.revision, _advisory(chosen.lease), chosen.source)
    if holders:
        chosen = max(
            holders,
            key=lambda c: (_parse_stamp(c.lease.expires_at), c.source == "receiver", c.revision),
        )
        return Resolution(chosen.revision, _advisory(chosen.lease), chosen.source)
    if fence.host_id and fence.host_id != route.holder_identity:
        # Another host won this epoch; its proposals live in its own inbox. Report it as the
        # holder so nothing here treats the hive as free to seize.
        return Resolution(
            "",
            HostLease(
                host_id=fence.host_id,
                label="",
                epoch=fence.epoch,
                adopted_at="unknown",
                expires_at="unknown",
                advisory_expiry=True,
            ),
            "fence",
        )
    # This host's own fence with no qualifying lease: the designed adopt half-state. Nobody
    # may write; a fresh unforced adopt (next epoch) recovers it.
    return Resolution(receiver[0] if receiver else "", None, "none")


def _advisory(lease: HostLease) -> HostLease:
    return HostLease(**lease.to_record(), advisory_expiry=True)
