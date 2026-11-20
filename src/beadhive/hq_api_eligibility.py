"""Route=api reads bound to the signed authority record (bh-h9dn1).

Pure projections of hq-service v0.1.0 read bodies onto what the control plane's callers already
evaluate. No I/O and no SQL: :class:`~beadhive.hq_control_plane.ApiControlPlane` does the port
calls and maps :class:`ApiReadError` onto its own error taxonomy.

**Binding (operator decision, 2026-11-19).** The eligibility body is unsigned and the config
body carries an authority envelope; neither is trusted on its own. Each is bound to the signed
record a separate, verified ``GET /authority`` read served, as the SQL reader binds its
composite to the signed ``hq_authority`` row (the crossref):

* the record is internally consistent: ``state.revision`` is the record's ``revision``;
* an envelope a read carries is that same head and that same record, byte for byte;
* the eligibility ``active`` grant is the signed record's ``state.frames[frame].active``.

A mismatch is :class:`ApiReadError` ``authority_unbound``: refused, never returned.

**Eligibility.** :func:`eligibility` folds ``{head, config_head, frame_id, active, principal,
epoch, route, liveness, release}`` into a session-carrier observation
(:class:`ApiSessionObservation`, duck-compatible with
:class:`~beadhive.hq_sql_session.SessionObservation`), so
:func:`beadhive.frame_eligibility.eligible` evaluates the same predicates on route=api:

* ``session_epoch_matches``: the service read liveness for the active grant's own (principal,
  epoch), the registry routes that principal (``route`` is not null) under the grant's signing
  key, and the body's epoch is the grant's.
* ``authenticated_fresh_heartbeat``: the server's session verdict is ``fresh``.
* ``conformance_pass``: the server's evidence verdict is ``fresh`` and its status
  ``conformant``. Gap: v0.1.0 does not serve the evidence profile, so the SQL statement's
  ``profile = desired.profile`` conjunct cannot be checked here.
* ``release_matches``: the service's verdict (``release.admits``) on the release the caller
  measured, which is the committed manifest's release. Gap: v0.1.0 does not serve the evidence
  report's own measured release, which the SQL statement judges; the manifest release is the
  frame's declared one. Under a release policy the verdict's catalog row becomes
  ``manifest_release_catalog_row``, as the SQL plane sets it.
* ``current_hive_lease_holder``: the hive-lease row (``GET /hive-leases``) names this host.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: ``stamps()["carrier"]`` and ``observation.carrier``: the data-switched session carrier.
SESSION_CARRIER = "session"


class ApiReadError(ValueError):
    """An api read the plane must not return. ``code`` is ``authority_unbound`` (refused: it
    does not match the signed record), ``grant_mismatch`` (the manifest or policy differs from
    the current grant) or ``malformed`` (not the served shape)."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class ApiSessionObservation:
    """The api plane's observation of one frame's active incarnation (session carrier)."""

    status: str
    fresh: bool
    age_seconds: float | None
    authenticated_fresh_heartbeat: bool
    conformance_pass: bool
    release_matches: bool
    current_hive_lease_holder: bool
    session_epoch_matches: bool
    principal: str
    epoch: int | None
    renewed_at: str
    measured_at: str
    evidence_age_seconds: float | None
    evidence_status: str
    evidence_digest: str
    session_ttl_s: int | None
    evidence_ttl_s: int | None
    candidate: bool = False
    verified: bool = True
    lease: None = None
    reason: str = ""
    age_basis: str = "hq-server-session-row"
    carrier: str = SESSION_CARRIER
    predicates: dict = field(default_factory=dict)
    manifest_release_catalog_row: dict | None = None

    @property
    def sha(self) -> str:
        return self.evidence_digest

    def stamps(self) -> dict:
        """The admitted stamps a claim record carries (the SQL carrier's keys)."""
        return {
            "carrier": self.carrier,
            "session_table": f"api:{self.principal}/{self.epoch}",
            "session_renewed_at": self.renewed_at,
            "evidence_measured_at": self.measured_at,
            "evidence_digest": self.evidence_digest,
            "evidence_status": self.evidence_status,
            "predicates": dict(self.predicates),
        }


def _iso(stamp: Any) -> str:
    if type(stamp) not in (int, float) or not math.isfinite(stamp):
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))


def _seconds(value: Any) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ApiReadError("malformed", "a liveness age is not a number")
    return float(value)


def signed_record(authority: Any) -> tuple[str, dict]:
    """``(head, record)`` of a verified ``GET /authority`` body, refused unless its signed state
    names the record's own revision (the SQL reader's ``state["revision"] != sequence``)."""
    if not isinstance(authority, Mapping):
        raise ApiReadError("malformed", "the verified authority is not an envelope")
    head, record = authority.get("head"), authority.get("record")
    if not isinstance(head, str) or not head or not isinstance(record, dict):
        raise ApiReadError("malformed", "the verified authority is not an envelope")
    state = record.get("state")
    revision = record.get("revision")
    if not isinstance(state, dict) or type(revision) is not int:
        raise ApiReadError("malformed", "the signed authority record carries no state")
    if state.get("revision") != revision:
        raise ApiReadError(
            "authority_unbound",
            f"the signed state revision {state.get('revision')!r} is not the record's {revision!r}",
        )
    return head, record


def bind_envelope(served: Any, authority: Any, *, what: str) -> tuple[str, dict]:
    """``(head, record)``: *served* (the envelope a read carries) is the signed *authority*
    (a separate verified ``GET /authority``), at the same head and with the same record."""
    head, record = signed_record(authority)
    if (
        not isinstance(served, Mapping)
        or not isinstance(served.get("head"), str)
        or not isinstance(served.get("record"), dict)
    ):
        raise ApiReadError("malformed", f"the {what} carries no authority envelope")
    if served.get("head") != head:
        raise ApiReadError(
            "authority_unbound",
            f"the {what} is bound to authority head {str(served.get('head'))[:12]}, not the "
            f"signed head {head[:12]}; re-read if HQ just published",
        )
    if served.get("record") != record:
        raise ApiReadError(
            "authority_unbound",
            f"the {what}'s authority record (revision {_field(served, 'revision')}, config head "
            f"{str(_field(served, 'config_head'))[:12]}) is not the signed record at {head[:12]}",
        )
    return head, record


def _field(envelope: Mapping, name: str) -> Any:
    record = envelope.get("record")
    return record.get(name) if isinstance(record, Mapping) else None


def signed_grant(record: Mapping, frame: str) -> dict | None:
    """The signed record's active grant for *frame*, or ``None``."""
    frames = record["state"].get("frames") or {}
    entry = frames.get(frame) if isinstance(frames, Mapping) else None
    active = entry.get("active") if isinstance(entry, Mapping) else None
    return active if isinstance(active, dict) else None


def desired_of(active: Mapping) -> dict:
    """The desired state the SQL plane builds from a grant record."""
    return {
        **active["desired"],
        **({"emergency": active["emergency"]} if "emergency" in active else {}),
        "state": active["state"],
        "cordoned": active["cordoned"],
        "authority": active["authority"],
    }


def check_manifest(manifest: Any, active: Mapping, policies: Mapping, *, enforce: bool) -> None:
    """The SQL plane's identity check: the committed manifest is the grant's incarnation and,
    when enforced, a signed hive policy names the grant's config revision."""
    authority = active["authority"]
    if (
        getattr(manifest, "frame_id", None) != authority.get("frame_id")
        or getattr(manifest, "host_id", None) != authority.get("holder_identity")
        or getattr(manifest, "instance_ref", None) != authority.get("instance_ref")
        or getattr(manifest, "beadyard_id", None) != authority.get("beadyard_id")
        or enforce
        and not any(
            isinstance(policy, Mapping)
            and policy.get("config_revision") == authority.get("config_revision")
            for policy in policies.values()
        )
    ):
        raise ApiReadError(
            "grant_mismatch", "manifest or desired policy differs from current grant"
        )


def manifest_release(manifest: Any) -> dict | None:
    """``{id, digest}`` of the committed manifest's release, the measured release asked for."""
    release = getattr(manifest, "release", None)
    if release is None:
        return None
    dumped = release.model_dump() if hasattr(release, "model_dump") else dict(release)
    return {"id": str(dumped["id"]), "digest": str(dumped["digest"])}


def eligibility(
    result: Any,
    manifest: Any,
    *,
    enforce: bool,
    measured: Mapping[str, str] | None = None,
    hive_lease: Any = None,
) -> tuple[str, dict | None, dict, dict, Any]:
    """``(head, active, desired, policies, observation)`` from the adapter's eligibility result
    (the body plus the verified ``authority`` envelope it was bound to).

    ``hive_lease`` is the hive's lease (a ``HostLease`` or ``None``) when the caller named a
    hive; ``measured`` is the release the read asked the service to judge. ``observation`` is
    ``None`` when the frame has no active grant (then ``active`` is ``None`` and ``desired``
    empty)."""
    try:
        head, record = signed_record(result["authority"])
        frame = result["frame_id"]
        active = result["active"]
        if active != signed_grant(record, frame):
            raise ApiReadError(
                "authority_unbound",
                f"the eligibility grant of {frame} is not the signed authority's at {head[:12]}",
            )
        policies = record.get("hive_policies") or {}
        if not isinstance(policies, Mapping):
            raise ApiReadError("malformed", "the signed hive policies are not a mapping")
        if active is None:  # no grant: the caller's ``missing`` observation
            return head, None, {}, dict(policies), None
        check_manifest(manifest, active, policies, enforce=enforce)
        desired = desired_of(active)
        authority = active["authority"]
        principal, epoch, route = (result.get(k) for k in ("principal", "epoch", "route"))
        liveness = result.get("liveness")
        session = (liveness or {}).get("session") or {}
        evidence = (liveness or {}).get("evidence") or {}
        verdict = session.get("verdict")
        fresh = verdict == "fresh"
        never = verdict in (None, "never-renewed")
        status = "fresh" if fresh else ("missing" if never else "stale")
        epoch_matches = (
            liveness is not None
            and isinstance(route, Mapping)
            and type(epoch) is int
            and epoch == authority.get("epoch")
            and route.get("signer_fingerprint") == authority.get("key_fingerprint")
        )
        conformant = evidence.get("verdict") == "fresh" and evidence.get("status") == "conformant"
        release = result.get("release")
        release_ok = (
            measured is not None
            and isinstance(release, Mapping)
            and release.get("admits") is True
            and (release.get("id"), release.get("digest")) == (measured["id"], measured["digest"])
        )
        row = None
        if isinstance(release, Mapping) and release.get("basis") == "catalog":
            row = release.get("row")
            row = dict(row) if isinstance(row, Mapping) else None
        holder = bool(
            hive_lease is not None
            and getattr(hive_lease, "host_id", "")
            and hive_lease.host_id == getattr(manifest, "host_id", None)
        )
        predicates = {
            "authenticated_fresh_heartbeat": fresh,
            "conformance_pass": conformant,
            "release_matches": release_ok,
            "current_hive_lease_holder": holder,
        }
        observation = ApiSessionObservation(
            status=status,
            fresh=fresh,
            age_seconds=_seconds(session.get("age_s")),
            authenticated_fresh_heartbeat=fresh,
            conformance_pass=conformant,
            release_matches=release_ok,
            current_hive_lease_holder=holder,
            session_epoch_matches=bool(epoch_matches),
            principal=str(principal or ""),
            epoch=epoch if type(epoch) is int else None,
            renewed_at=_iso(session.get("renewed_at")),
            measured_at=_iso(evidence.get("measured_at")),
            evidence_age_seconds=_seconds(evidence.get("age_s")),
            evidence_status=str(evidence.get("status") or ""),
            evidence_digest=str(evidence.get("digest") or ""),
            session_ttl_s=session.get("ttl_s"),
            evidence_ttl_s=evidence.get("ttl_s"),
            predicates=predicates,
            manifest_release_catalog_row=row,
        )
        return head, active, desired, dict(policies), observation
    except ApiReadError:
        raise
    except (KeyError, TypeError, AttributeError) as exc:
        raise ApiReadError("malformed", "the verified result is not eligibility") from exc


#: The five fields of a stored hive-lease record (``lease.Lease``, ``HostLease``).
LEASE_FIELDS = frozenset({"host_id", "label", "epoch", "adopted_at", "expires_at"})


def hive_lease(prefix: str, body: Any) -> tuple[str, Any]:
    """``(revision, HostLease)`` from ``GET /hive-leases`` (``HiveLeaseRead``), validated as the
    SQL reader validates its carrier. Expiry is a failover hint on this carrier (session
    liveness): ``advisory_expiry`` is set, as the SQL session reader sets it."""
    from .host_lease_contracts import HostLease, _parse_stamp

    try:
        raw = body["lease"]
        revision = body["revision"]
        if (
            body.get("prefix") != prefix
            or not isinstance(revision, str)
            or not revision
            or not isinstance(raw, dict)
            or set(raw) != LEASE_FIELDS
            or any(
                type(raw[key]) is not str
                for key in ("host_id", "label", "adopted_at", "expires_at")
            )
            or type(raw["epoch"]) is not int
            or raw["epoch"] < 1
            or _parse_stamp(raw["adopted_at"]) <= 0
            or _parse_stamp(raw["expires_at"]) <= 0
        ):
            raise ApiReadError("malformed", "protected hive lease record invalid")
        return revision, HostLease(**raw, advisory_expiry=True)
    except ApiReadError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiReadError("malformed", "protected hive lease record invalid") from exc


__all__ = [
    "LEASE_FIELDS",
    "SESSION_CARRIER",
    "ApiReadError",
    "ApiSessionObservation",
    "bind_envelope",
    "check_manifest",
    "desired_of",
    "eligibility",
    "hive_lease",
    "manifest_release",
    "signed_grant",
    "signed_record",
]
