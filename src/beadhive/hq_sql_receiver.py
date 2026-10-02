"""Trusted, separately credentialed acceptance of signed frame inbox evidence.

The observer owns receipt and floor mutations.  Frames only INSERT into their
operator-routed inbox, and no frame process receives this observer credential.
Acceptance here is evidence only; dispatch must freshly qualify config and
authority against the protected receipt at its own read point.
"""

from __future__ import annotations

import hashlib
import json
import time

from . import hq_authority_guard as guard
from .host_lease_contracts import HostLease, _parse_stamp
from .hq_framelease_contracts import HeartbeatLease
from .hq_hive_policy import project_hive_policies
from .hq_sql_runtime import PrincipalBinding, SqlRuntimeAuthority, SqlRuntimeError
from .hq_sql_runtime_schema import inbox_table
from .hq_sql_signatures import (
    SqlSignatureError,
    canonical,
    verify_heartbeat,
    verify_hive_request,
    verify_registration,
)
from .hq_sql_transport import FnoxBroker, SqlTransportError, connect


class ReceiverError(ValueError):
    """Trusted observer rejected or could not durably accept a frame request."""


class ReceiverUnknown(ReceiverError):
    """The receiver COMMIT acknowledgment was lost; exact request readback is required."""

    def __init__(self, request_id, request_sha256):
        self.request_id = request_id
        self.request_sha256 = request_sha256
        super().__init__(f"receiver acknowledgment unknown for request {request_id}")


class SqlTrustedReceiver:
    def __init__(self, settings, *, broker=None, clock=time.time):
        self.settings = settings
        self.broker = broker or FnoxBroker()
        self.clock = clock
        self.authority = SqlRuntimeAuthority(settings, broker=self.broker, clock=clock)

    def _open(self):
        binding = self.settings.get("observer")
        if not binding:
            raise ReceiverError("separate trusted observer binding unavailable")
        deadline = time.monotonic() + binding["operation_timeout"]
        try:
            return connect(binding, self.broker, deadline=deadline), deadline
        except SqlTransportError:
            raise ReceiverError("verified trusted observer connection unavailable") from None

    @staticmethod
    def _routed(cursor, head, principal):
        cursor.execute(
            "SELECT principal,frame_id,holder_identity,instance_ref,epoch,"
            "inbox_table,signer_fingerprint FROM hq_principal_registry AS OF %s "
            "WHERE principal=%s",
            (head, principal),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise ReceiverError("unprovisioned frame principal")
        route = PrincipalBinding(*rows[0])
        if route.inbox_table != inbox_table(route.principal, route.epoch):
            raise ReceiverError("protected principal table routing invalid")
        return route

    @staticmethod
    def _inbox(cursor, route, request_id, kind):
        cursor.execute(
            f"SELECT kind,payload,payload_sha256 FROM {route.inbox_table} WHERE request_id=%s",
            (request_id,),
        )
        row = cursor.fetchone()
        if row is None or row[0] != kind:
            raise ReceiverError("signed frame request unavailable")
        body = row[1]
        if isinstance(body, memoryview):
            body = body.tobytes()
        if isinstance(body, str):
            body = body.encode()
        if not isinstance(body, bytes) or len(body) > 65536:
            raise ReceiverError("signed frame request size invalid")
        digest = hashlib.sha256(body).hexdigest()
        if digest != row[2]:
            raise ReceiverError("signed frame request digest mismatch")
        envelope = json.loads(body)
        if body != canonical(envelope):
            raise ReceiverError("signed frame request is not canonical")
        return envelope, digest

    @staticmethod
    def _lease_record(raw):
        if (
            not isinstance(raw, dict)
            or set(raw) != {"host_id", "label", "epoch", "adopted_at", "expires_at"}
            or any(
                type(raw[name]) is not str
                for name in ("host_id", "label", "adopted_at", "expires_at")
            )
            or type(raw["epoch"]) is not int
            or raw["epoch"] < 1
            or _parse_stamp(raw["adopted_at"]) <= 0
            or _parse_stamp(raw["expires_at"]) <= 0
        ):
            raise ReceiverError("invalid signed hive lease record")
        return HostLease(**raw)

    @staticmethod
    def _read_prior(cursor, prefix):
        cursor.execute(
            "SELECT revision,lease_json,request_id,request_sha256 "
            "FROM hq_live_hive_leases WHERE prefix=%s",
            (prefix,),
        )
        row = cursor.fetchone()
        if row is None:
            return "", None, None, None, None
        body = row[1]
        if isinstance(body, memoryview):
            body = body.tobytes()
        if isinstance(body, str):
            body = body.encode()
        envelope = json.loads(body)
        if body != canonical(envelope) or set(envelope) != {"authority", "lease"}:
            raise ReceiverError("protected incumbent lease invalid")
        return (
            row[0], envelope["authority"], SqlTrustedReceiver._lease_record(envelope["lease"]),
            row[2], row[3],
        )

    @staticmethod
    def _accepted_prior_request(
        cursor, route, request_id, request_sha, result_revision, kind, public_key
    ):
        """Verify an immutable prior inbox signature and its accepted result witness."""
        envelope, digest = SqlTrustedReceiver._inbox(cursor, route, request_id, kind)
        verify = verify_hive_request if kind == "hive_lease" else verify_registration
        request, signed_sha = verify(envelope, granted_public_key=public_key)
        cursor.execute(
            "SELECT request_sha256,principal,frame_id,holder_identity,instance_ref,"
            "epoch,signer_fingerprint,result_revision,status FROM hq_live_results "
            "WHERE request_id=%s",
            (request_id,),
        )
        expected = (
            request_sha, route.principal, route.frame_id, route.holder_identity,
            route.instance_ref, route.epoch, route.signer_fingerprint,
            result_revision, "accepted",
        )
        if (
            digest != request_sha
            or signed_sha != request_sha
            or request.get("request_id") != request_id
            or cursor.fetchone() != expected
        ):
            raise ReceiverError("original signed incumbent evidence unavailable")
        return request

    @staticmethod
    def _bound_registration(cursor, route, record, owner):
        from .host_manifest_contracts import HostManifest

        cursor.execute(
            "SELECT request_id,request_sha256,manifest_json,signer_fingerprint "
            "FROM hq_live_registrations WHERE frame_id=%s AND holder_identity=%s "
            "AND instance_ref=%s AND epoch=%s",
            (route.frame_id, route.holder_identity, route.instance_ref, route.epoch),
        )
        row = cursor.fetchone()
        if row is None or row[3] != route.signer_fingerprint:
            raise ReceiverError("bound hive adoption requires current signed registration")
        stored_body = row[2].tobytes() if isinstance(row[2], memoryview) else row[2]
        request = SqlTrustedReceiver._accepted_prior_request(
            cursor, route, row[0], row[1], "sha256:" + row[1], "registration",
            record["public_key"],
        )
        manifest = HostManifest.model_validate_json(stored_body)
        body = canonical(manifest.model_dump(mode="json", exclude_none=True))
        if (
            request.get("domain") != "beadhive/sql-registration/v2"
            or request.get("beadyard_id") != owner
            or request.get("principal") != route.principal
            or request.get("frame_id") != route.frame_id
            or request.get("holder_identity") != route.holder_identity
            or request.get("instance_ref") != route.instance_ref
            or request.get("epoch") != route.epoch
            or request.get("key_fingerprint") != route.signer_fingerprint
            or request.get("manifest") != manifest.model_dump(mode="json", exclude_none=True)
            or body != (stored_body.encode() if isinstance(stored_body, str) else stored_body)
            or manifest.beadyard_id != owner
            or manifest.frame_id != route.frame_id
            or manifest.host_id != route.holder_identity
            or manifest.instance_ref != route.instance_ref
            or manifest.release is None
            or manifest.release.model_dump() != record["desired"]["release"]
            or manifest.capabilities is None
            or manifest.capabilities.model_dump() != record["desired"]["caps"]
        ):
            raise ReceiverError("bound hive adoption requires current signed registration")

    def accept_hive_lease(self, principal: str, request_id: str) -> str:
        """Accept an authenticated proposal with one protected global lease CAS.

        The acceptance point freshly rereads completed config/authority commits.
        A subsequent dispatch or claim must requalify both heads and the receipt;
        this method does not assert atomicity with a later independent commit.
        """
        if not isinstance(principal, str) or not isinstance(request_id, str):
            raise ReceiverError("principal and request ID required")
        connection, deadline = self._open()
        crossed_commit = False
        request_sha = ""
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT CURRENT_USER(),DATABASE(),ACTIVE_BRANCH(),DOLT_VERSION()")
                user, database, branch, version = cursor.fetchone()
                observer = self.settings["observer"]
                if (
                    not user.startswith(observer["user"] + "@")
                    or database != observer["database"]
                    or branch != "main"
                    or version != "2.3.5"
                ):
                    raise ReceiverError("trusted observer principal or endpoint mismatch")
                cursor.execute("START TRANSACTION")
                cursor.execute("SELECT DOLT_HASHOF('HEAD')")
                head = cursor.fetchone()[0]
                state, crossref, policies = self.authority.verified_state_at(
                    cursor, head, deadline=deadline
                )
                route = self._routed(cursor, head, principal)
                entry = state.get("frames", {}).get(route.frame_id)
                record = entry.get("active") if entry else None
                if record is None:
                    raise ReceiverError("no active operator-granted frame")
                authority = record["authority"]
                if (
                    authority["holder_identity"] != route.holder_identity
                    or authority["instance_ref"] != route.instance_ref
                    or authority["epoch"] != route.epoch
                    or authority["key_fingerprint"] != route.signer_fingerprint
                ):
                    raise ReceiverError("routed principal differs from active grant")
                envelope, request_sha = self._inbox(cursor, route, request_id, "hive_lease")
                request, signed_sha = verify_hive_request(
                    envelope, granted_public_key=record["public_key"]
                )
                expected_fields = {
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
                if authority.get("beadyard_id") is not None:
                    expected_fields.add("beadyard_id")
                if signed_sha != request_sha or set(request) != expected_fields:
                    raise ReceiverError("signed hive request shape or digest invalid")
                from .host_lease_contracts import lease_ref

                lease_ref(request["prefix"])
                if (
                    request["request_id"] != request_id
                    or request["principal"] != route.principal
                    or request["frame_id"] != route.frame_id
                    or request["holder_identity"] != route.holder_identity
                    or request["instance_ref"] != route.instance_ref
                    or request["epoch"] != route.epoch
                    or request["key_fingerprint"] != route.signer_fingerprint
                    or request["audience"] != authority["audience"]
                    or request["config_revision"] != authority["config_revision"]
                    or request.get("beadyard_id") != authority.get("beadyard_id")
                    or request["authority_revision"] != head
                    or request["force"] is not False
                    or request["operation"] not in {"adopt", "renew", "release"}
                    or not isinstance(request["expected_revision"], str)
                ):
                    raise ReceiverError("signed hive request differs from current grant")
                lease = self._lease_record(request["lease"])
                prefix = request["prefix"]
                old_revision, old_authority, old_lease, old_request_id, old_request_sha = (
                    self._read_prior(cursor, prefix)
                )
                cursor.execute(
                    "SELECT request_sha256,principal,frame_id,holder_identity,instance_ref,"
                    "epoch,audience,signer_fingerprint,expected_revision,result_revision,status "
                    "FROM hq_live_results WHERE request_id=%s",
                    (request_id,),
                )
                prior_result = cursor.fetchone()
                if prior_result is not None:
                    if (
                        prior_result[:9]
                        != (
                            request_sha,
                            route.principal,
                            route.frame_id,
                            route.holder_identity,
                            route.instance_ref,
                            route.epoch,
                            authority["audience"],
                            route.signer_fingerprint,
                            request["expected_revision"],
                        )
                        or prior_result[10] != "accepted"
                    ):
                        raise ReceiverError("request ID was reused or accepted meaning changed")
                    connection.rollback()
                    return prior_result[9]
                if old_revision != request["expected_revision"]:
                    raise ReceiverError("original protected hive lease CAS conflict")
                now = self.clock()
                identity = {
                    "frame_id": route.frame_id,
                    **authority,
                }
                same_incumbent = guard.same_incumbent_after_binding(old_authority, identity)
                operation = request["operation"]
                if operation == "release":
                    if (
                        old_lease is None
                        or not same_incumbent
                        or old_lease.host_id != route.holder_identity
                        or lease.host_id != ""
                        or lease.epoch != old_lease.epoch
                        or lease.adopted_at != old_lease.adopted_at
                        or _parse_stamp(lease.expires_at) > now
                    ):
                        raise ReceiverError("release requires exact incumbent incarnation/epoch")
                else:
                    snapshot = self.authority.load_config_at(cursor, crossref, deadline=deadline)
                    projected = project_hive_policies(
                        snapshot, valid_until=state["expires_at"], now=now
                    )
                    if projected != policies or prefix not in policies:
                        raise ReceiverError("protected hive policy differs from canonical catalog")
                    policy = policies[prefix]
                    if now >= policy["valid_until"]:
                        raise ReceiverError("protected hive policy expired")
                    if (
                        record["state"] != "active"
                        or record["cordoned"]
                        or authority["config_revision"] != policy["config_revision"]
                        or lease.host_id != route.holder_identity
                        or _parse_stamp(lease.expires_at) <= now
                        or _parse_stamp(lease.expires_at) > now + 86400
                    ):
                        raise ReceiverError("frame not authorized for hive lease")
                    cursor.execute(
                        "SELECT sequence,digest,first_seen FROM hq_live_floors "
                        "WHERE frame_id=%s AND holder_identity=%s AND epoch=%s",
                        (route.frame_id, route.holder_identity, route.epoch),
                    )
                    floor = cursor.fetchone()
                    if floor is None or floor[0] < 1 or floor[2] is None:
                        raise ReceiverError("current accepted heartbeat floor unavailable")
                    cursor.execute(
                        "SELECT lease_json,envelope_json,first_seen FROM hq_live_receipts "
                        "WHERE frame_id=%s AND holder_identity=%s AND epoch=%s "
                        "AND sequence=%s AND digest=%s",
                        (route.frame_id, route.holder_identity, route.epoch, floor[0], floor[1]),
                    )
                    receipt = cursor.fetchone()
                    if receipt is None or receipt[2] != floor[2]:
                        raise ReceiverError("current protected heartbeat receipt unavailable")
                    envelope_body = receipt[1]
                    if isinstance(envelope_body, bytes):
                        envelope_body = envelope_body.decode()
                    beat, verified_digest = verify_heartbeat(
                        json.loads(envelope_body), granted_public_key=record["public_key"]
                    )
                    matching_beat = self._matches(beat, route, authority)
                    legacy_bridge = False
                    if (
                        not matching_beat
                        and operation == "renew"
                        and authority.get("beadyard_id") is not None
                        and beat.beadyard_id is None
                        and old_authority is not None
                        and old_lease is not None
                        and old_authority
                        == {key: value for key, value in identity.items()
                            if key != "beadyard_id"}
                        and self._matches(
                            beat, route,
                            {key: value for key, value in authority.items()
                             if key != "beadyard_id"},
                        )
                    ):
                        old_request = self._accepted_prior_request(
                            cursor, route, old_request_id, old_request_sha, old_revision,
                            "hive_lease", record["public_key"],
                        )
                        legacy_bridge = (
                            old_request.get("domain") == "beadhive/sql-hive-lease/v1"
                            and "beadyard_id" not in old_request
                            and old_request.get("operation") in {"adopt", "renew"}
                            and old_request.get("principal") == route.principal
                            and old_request.get("frame_id") == route.frame_id
                            and old_request.get("holder_identity") == route.holder_identity
                            and old_request.get("instance_ref") == route.instance_ref
                            and old_request.get("epoch") == route.epoch
                            and old_request.get("key_fingerprint") == route.signer_fingerprint
                            and old_request.get("audience") == authority["audience"]
                            and old_request.get("config_revision")
                            == authority["config_revision"]
                            and old_request.get("prefix") == prefix
                            and old_request.get("lease") == old_lease.to_record()
                        )
                    if (
                        verified_digest != floor[1]
                        or canonical(beat.model_dump(mode="json", exclude_none=True))
                        != (receipt[0].encode() if isinstance(receipt[0], str) else receipt[0])
                        or not (matching_beat or legacy_bridge)
                        or now < floor[2]
                        or now - floor[2] >= beat.leaseDurationSeconds
                        or beat.release.model_dump() != record["desired"]["release"]
                        or beat.conformance.status != "conformant"
                        or beat.conformance.profile != record["desired"]["profile"]
                        or any(check.status == "fail" for check in beat.conformance.checks)
                    ):
                        raise ReceiverError("hive lease requires fresh conformant receipt")
                    if operation == "adopt" and authority.get("beadyard_id") is not None:
                        self._bound_registration(
                            cursor, route, record, authority["beadyard_id"]
                        )
                    caps = record["desired"]["caps"]
                    if type(caps.get("max_sessions")) is not int or caps["max_sessions"] <= 0:
                        raise ReceiverError("no frame intake capacity")
                    guard.requirements(policy["requires"], caps)
                    if operation == "renew":
                        if (
                            old_lease is None
                            or not same_incumbent
                            or old_lease.host_id != lease.host_id
                            or old_lease.is_expired(now)
                            or lease.epoch != old_lease.epoch
                            or lease.adopted_at != old_lease.adopted_at
                        ):
                            raise ReceiverError("renew requires current exact incumbent")
                    elif old_lease is not None:
                        if lease.epoch <= old_lease.epoch:
                            raise ReceiverError("adopt hive epoch must advance")
                        if (
                            old_lease.host_id
                            and old_lease.host_id != lease.host_id
                            and not old_lease.is_expired(now)
                        ):
                            former = [
                                r
                                for frame, r in guard.records(state)
                                if old_authority == {"frame_id": frame, **r["authority"]}
                            ]
                            if len(former) != 1:
                                raise ReceiverError("incumbent incarnation unavailable")
                            incumbent = former[0]
                            if incumbent["state"] not in {"retired", "quarantined"}:
                                former_authority = incumbent["authority"]
                                cursor.execute(
                                    "SELECT sequence,digest,first_seen FROM hq_live_floors "
                                    "WHERE frame_id=%s AND holder_identity=%s AND epoch=%s",
                                    (
                                        old_authority["frame_id"],
                                        former_authority["holder_identity"],
                                        former_authority["epoch"],
                                    ),
                                )
                                former_floor = cursor.fetchone()
                                if former_floor is None or former_floor[0] < 1:
                                    raise ReceiverError("live incumbent has no accepted receipt")
                                cursor.execute(
                                    "SELECT first_seen,lease_json,envelope_json "
                                    "FROM hq_live_receipts "
                                    "WHERE frame_id=%s AND holder_identity=%s AND epoch=%s "
                                    "AND sequence=%s AND digest=%s",
                                    (
                                        old_authority["frame_id"],
                                        former_authority["holder_identity"],
                                        former_authority["epoch"],
                                        former_floor[0],
                                        former_floor[1],
                                    ),
                                )
                                former_beat = cursor.fetchone()
                                if former_beat is None or former_beat[0] != former_floor[2]:
                                    raise ReceiverError("live incumbent receipt floor mismatch")
                                prior_envelope = former_beat[2]
                                if isinstance(prior_envelope, bytes):
                                    prior_envelope = prior_envelope.decode()
                                prior_beat, prior_digest = verify_heartbeat(
                                    json.loads(prior_envelope),
                                    granted_public_key=incumbent["public_key"],
                                )
                                if (
                                    prior_digest != former_floor[1]
                                    or canonical(
                                        prior_beat.model_dump(mode="json", exclude_none=True)
                                    )
                                    != (
                                        former_beat[1].encode()
                                        if isinstance(former_beat[1], str)
                                        else former_beat[1]
                                    )
                                    or prior_beat.frame_id != old_authority["frame_id"]
                                    or prior_beat.holderIdentity
                                    != former_authority["holder_identity"]
                                    or prior_beat.epoch != former_authority["epoch"]
                                    or now - former_beat[0] <= policy["evict_after_s"]
                                    or now - former_beat[0] < prior_beat.leaseDurationSeconds
                                ):
                                    raise ReceiverError("live incumbent is not evictable")
                    self.authority.fresh_config_head_fence(crossref[2], deadline=deadline)
                self.authority.fresh_runtime_head_fence(head, deadline=deadline)
                if time.monotonic() >= deadline:
                    raise ReceiverError("trusted receiver deadline exceeded")
                revision = hashlib.sha256(canonical(request)).hexdigest()
                lease_body = canonical({"authority": identity, "lease": lease.to_record()})
                if old_revision:
                    cursor.execute(
                        "UPDATE hq_live_hive_leases SET revision=%s,lease_json=%s,"
                        "request_id=%s,request_sha256=%s "
                        "WHERE prefix=%s AND revision=%s",
                        (revision, lease_body, request_id, request_sha, prefix, old_revision),
                    )
                    if cursor.rowcount != 1:
                        raise ReceiverError("protected hive lease CAS conflict")
                else:
                    cursor.execute(
                        "INSERT INTO hq_live_hive_leases VALUES (%s,%s,%s,%s,%s)",
                        (prefix, revision, lease_body, request_id, request_sha),
                    )
                cursor.execute(
                    "INSERT INTO hq_live_results VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        request_id,
                        request_sha,
                        route.principal,
                        route.frame_id,
                        route.holder_identity,
                        route.instance_ref,
                        route.epoch,
                        authority["audience"],
                        route.signer_fingerprint,
                        request["expected_revision"],
                        revision,
                        "accepted",
                        now,
                    ),
                )
                crossed_commit = True
                connection.commit()
                return revision
        except ReceiverError:
            if not crossed_commit:
                connection.rollback()
                raise
            raise ReceiverUnknown(request_id, request_sha) from None
        except (SqlSignatureError, SqlRuntimeError, ValueError, KeyError, TypeError):
            if not crossed_commit:
                connection.rollback()
                raise ReceiverError("hive request authentication or authority failed") from None
            raise ReceiverUnknown(request_id, request_sha) from None
        except Exception as exc:
            if not crossed_commit:
                connection.rollback()
                if getattr(exc, "args", (None,))[0] in (1213, 1205, 1062):
                    raise ReceiverError("protected hive lease CAS conflict") from None
                raise ReceiverError("trusted hive lease acceptance unavailable") from None
            raise ReceiverUnknown(request_id, request_sha) from None
        finally:
            connection.close()

    def accept_registration(self, principal: str, request_id: str) -> str:
        """Accept signed current-manifest evidence into protected incarnation custody."""
        from ruamel.yaml import YAML

        from .host_manifest_contracts import HostManifest

        connection, deadline = self._open()
        crossed_commit = False
        request_sha = ""
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT CURRENT_USER(),DATABASE(),ACTIVE_BRANCH(),DOLT_VERSION()")
                user, database, branch, version = cursor.fetchone()
                observer = self.settings["observer"]
                if (
                    not user.startswith(observer["user"] + "@")
                    or database != observer["database"]
                    or branch != "main"
                    or version != "2.3.5"
                ):
                    raise ReceiverError("trusted registration observer mismatch")
                cursor.execute("START TRANSACTION")
                cursor.execute("SELECT DOLT_HASHOF('HEAD')")
                head = cursor.fetchone()[0]
                state, crossref, _policies = self.authority.verified_state_at(
                    cursor, head, deadline=deadline
                )
                route = self._routed(cursor, head, principal)
                entry = state.get("frames", {}).get(route.frame_id)
                record = (entry or {}).get("candidate") or (entry or {}).get("active")
                if record is None or record["state"] == "retired":
                    raise ReceiverError("registration grant unavailable")
                authority = record["authority"]
                if (
                    authority["holder_identity"] != route.holder_identity
                    or authority["instance_ref"] != route.instance_ref
                    or authority["epoch"] != route.epoch
                    or authority["key_fingerprint"] != route.signer_fingerprint
                ):
                    raise ReceiverError("registration principal differs from grant")
                envelope, request_sha = self._inbox(cursor, route, request_id, "registration")
                request, signed_sha = verify_registration(
                    envelope, granted_public_key=record["public_key"]
                )
                if (
                    signed_sha != request_sha
                    or set(request)
                    != {
                        "domain",
                        "request_id",
                        "principal",
                        "frame_id",
                        "holder_identity",
                        "instance_ref",
                        "epoch",
                        "key_fingerprint",
                        "audience",
                        "authority_revision",
                        "manifest",
                    }
                    | ({"beadyard_id"} if authority.get("beadyard_id") is not None else set())
                    or (
                        request["request_id"],
                        request["principal"],
                        request["frame_id"],
                        request["holder_identity"],
                        request["instance_ref"],
                        request["epoch"],
                        request["key_fingerprint"],
                        request["audience"],
                        request["authority_revision"],
                    )
                    != (
                        request_id,
                        route.principal,
                        route.frame_id,
                        route.holder_identity,
                        route.instance_ref,
                        route.epoch,
                        route.signer_fingerprint,
                        authority["audience"],
                        head,
                    )
                ):
                    raise ReceiverError("registration identity differs from protected grant")
                if request.get("beadyard_id") != authority.get("beadyard_id"):
                    raise ReceiverError("registration belongs to a different beadyard")
                manifest = HostManifest.model_validate(request["manifest"])
                if (
                    manifest.frame_id != route.frame_id
                    or manifest.host_id != route.holder_identity
                    or manifest.instance_ref != route.instance_ref
                    or manifest.release is None
                    or manifest.release.model_dump() != record["desired"]["release"]
                    or manifest.capabilities is None
                    or manifest.capabilities.model_dump() != record["desired"]["caps"]
                ):
                    raise ReceiverError("registration differs from declared desired frame")
                snapshot = self.authority.load_config_at(cursor, crossref, deadline=deadline)
                if (
                    manifest.beadyard_id != snapshot.beadyard_id
                    or manifest.beadyard_id != authority.get("beadyard_id")
                ):
                    raise ReceiverError("registration belongs to a different beadyard")
                documents = [
                    document
                    for document in snapshot.documents
                    if document.path == f"hosts/{route.holder_identity}.yaml"
                ]
                if len(documents) != 1:
                    raise ReceiverError("committed canonical host manifest unavailable")
                raw = YAML(typ="safe").load(documents[0].content)
                if isinstance(raw, dict) and "state" not in raw:
                    raw = {**raw, "state": "active"}
                canonical_manifest = HostManifest.model_validate(raw)
                if canonical_manifest != manifest:
                    raise ReceiverError("registration differs from committed host manifest")
                cursor.execute(
                    "SELECT request_id,request_sha256,manifest_json,signer_fingerprint "
                    "FROM hq_live_registrations "
                    "WHERE frame_id=%s AND holder_identity=%s AND instance_ref=%s AND epoch=%s",
                    (route.frame_id, route.holder_identity, route.instance_ref, route.epoch),
                )
                prior = cursor.fetchone()
                manifest_body = canonical(manifest.model_dump(mode="json", exclude_none=True))
                legacy_upgrade = False
                if prior is not None:
                    prior_body = (
                        prior[2].tobytes() if isinstance(prior[2], memoryview) else prior[2]
                    )
                    if isinstance(prior_body, str):
                        prior_body = prior_body.encode()
                    if (prior[0], prior[1], prior_body, prior[3]) == (
                        request_id, request_sha, manifest_body, route.signer_fingerprint
                    ):
                        connection.rollback()
                        return "sha256:" + request_sha
                    old_manifest = HostManifest.model_validate_json(prior_body)
                    old_fields = old_manifest.model_dump(mode="json", exclude_none=True)
                    next_fields = manifest.model_dump(mode="json", exclude_none=True)
                    if (
                        authority.get("beadyard_id") is None
                        or prior[3] != route.signer_fingerprint
                        or old_manifest.beadyard_id is not None
                        or next_fields != {**old_fields, "beadyard_id": authority["beadyard_id"]}
                        or canonical(old_fields) != prior_body
                    ):
                        raise ReceiverError(
                            "registration incarnation already has different evidence"
                        )
                    original = self._accepted_prior_request(
                        cursor, route, prior[0], prior[1], "sha256:" + prior[1],
                        "registration", record["public_key"],
                    )
                    if (
                        original.get("domain") != "beadhive/sql-registration/v1"
                        or "beadyard_id" in original
                        or original.get("manifest") != old_fields
                        or original.get("principal") != route.principal
                        or original.get("frame_id") != route.frame_id
                        or original.get("holder_identity") != route.holder_identity
                        or original.get("instance_ref") != route.instance_ref
                        or original.get("epoch") != route.epoch
                        or original.get("key_fingerprint") != route.signer_fingerprint
                        or original.get("audience") != authority["audience"]
                    ):
                        raise ReceiverError("original signed registration identity unavailable")
                    legacy_upgrade = True
                self.authority.fresh_config_head_fence(crossref[2], deadline=deadline)
                self.authority.fresh_runtime_head_fence(head, deadline=deadline)
                now = self.clock()
                if legacy_upgrade:
                    cursor.execute(
                        "UPDATE hq_live_registrations SET request_id=%s,request_sha256=%s,"
                        "manifest_json=%s,accepted_at=%s WHERE frame_id=%s "
                        "AND holder_identity=%s AND instance_ref=%s AND epoch=%s "
                        "AND request_id=%s AND request_sha256=%s AND manifest_json=%s "
                        "AND signer_fingerprint=%s",
                        (
                            request_id, request_sha, manifest_body, now, route.frame_id,
                            route.holder_identity, route.instance_ref, route.epoch,
                            prior[0], prior[1], prior_body, route.signer_fingerprint,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ReceiverError("original registration row CAS conflict")
                else:
                    cursor.execute(
                        "INSERT INTO hq_live_registrations VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            route.frame_id, route.holder_identity, route.instance_ref, route.epoch,
                            request_id, request_sha, manifest_body, now, route.signer_fingerprint,
                        ),
                    )
                cursor.execute(
                    "INSERT INTO hq_live_results VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        request_id,
                        request_sha,
                        route.principal,
                        route.frame_id,
                        route.holder_identity,
                        route.instance_ref,
                        route.epoch,
                        authority["audience"],
                        route.signer_fingerprint,
                        head,
                        "sha256:" + request_sha,
                        "accepted",
                        now,
                    ),
                )
                crossed_commit = True
                connection.commit()
                return "sha256:" + request_sha
        except ReceiverError:
            if not crossed_commit:
                connection.rollback()
                raise
            raise ReceiverUnknown(request_id, request_sha) from None
        except Exception:
            if not crossed_commit:
                connection.rollback()
                raise ReceiverError(
                    "registration authentication or acceptance unavailable"
                ) from None
            raise ReceiverUnknown(request_id, request_sha) from None
        finally:
            connection.close()

    def recover_heartbeat(
        self,
        request_id: str,
        *,
        request_sha256: str,
        principal: str,
        frame_id: str,
        holder_identity: str,
        instance_ref: str,
        epoch: int,
        audience: str,
        signer_fingerprint: str,
        expected_revision: str,
    ) -> str:
        """Read historical accepted outcome without granting any current permission.

        The caller retains the original UUID, full request digest and authority
        parent.  Current revocation/expiry does not erase an immutable past result;
        a new dispatch must independently reread current config and authority.
        """
        if (
            not isinstance(request_id, str)
            or not isinstance(request_sha256, str)
            or len(request_sha256) != 64
            or not isinstance(expected_revision, str)
            or not expected_revision
        ):
            raise ReceiverError("exact historical request identity required")
        connection, deadline = self._open()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT CURRENT_USER(),DATABASE(),ACTIVE_BRANCH(),DOLT_VERSION()")
                user, database, branch, version = cursor.fetchone()
                binding = self.settings["observer"]
                if (
                    not user.startswith(binding["user"] + "@")
                    or database != binding["database"]
                    or branch != "main"
                    or version != "2.3.5"
                ):
                    raise ReceiverError("trusted observer recovery endpoint mismatch")
                cursor.execute(
                    "SELECT request_sha256,principal,frame_id,holder_identity,instance_ref,"
                    "epoch,audience,signer_fingerprint,expected_revision,result_revision,status "
                    "FROM hq_live_results WHERE request_id=%s",
                    (request_id,),
                )
                result = cursor.fetchone()
                if (
                    result is None
                    or result[:9]
                    != (
                        request_sha256,
                        principal,
                        frame_id,
                        holder_identity,
                        instance_ref,
                        epoch,
                        audience,
                        signer_fingerprint,
                        expected_revision,
                    )
                    or result[10] != "accepted"
                    or not result[9]
                    or time.monotonic() >= deadline
                ):
                    raise ReceiverError("exact accepted request outcome unavailable")
                return result[9]
        except ReceiverError:
            raise
        except Exception:
            raise ReceiverError("historical request outcome unavailable") from None
        finally:
            connection.close()

    def accept_heartbeat(self, principal: str, request_id: str) -> str:
        """Atomically insert immutable first-seen receipt and CAS the accepted floor."""
        if not isinstance(principal, str) or not isinstance(request_id, str):
            raise ReceiverError("principal and request ID required")
        connection, deadline = self._open()
        crossed_commit = False
        request_sha = ""
        try:
            with connection.cursor() as cursor:
                binding = self.settings["observer"]
                cursor.execute("SELECT CURRENT_USER(),DATABASE(),ACTIVE_BRANCH(),DOLT_VERSION()")
                user, database, branch, version = cursor.fetchone()
                if (
                    not user.startswith(binding["user"] + "@")
                    or database != binding["database"]
                    or branch != "main"
                    or version != "2.3.5"
                ):
                    raise ReceiverError("trusted observer principal or endpoint mismatch")
                cursor.execute("START TRANSACTION")
                cursor.execute("SELECT DOLT_HASHOF('HEAD')")
                head = cursor.fetchone()[0]
                state, _, _ = self.authority.verified_state_at(cursor, head, deadline=deadline)
                cursor.execute(
                    "SELECT principal,frame_id,holder_identity,instance_ref,epoch,"
                    "inbox_table,signer_fingerprint FROM hq_principal_registry AS OF %s "
                    "WHERE principal=%s",
                    (head, principal),
                )
                rows = cursor.fetchall()
                if len(rows) != 1:
                    raise ReceiverError("unprovisioned frame principal")
                routed = PrincipalBinding(*rows[0])
                if routed.inbox_table != inbox_table(routed.principal, routed.epoch):
                    raise ReceiverError("protected principal table routing invalid")
                entry = state.get("frames", {}).get(routed.frame_id)
                matching = [
                    record
                    for record in ((entry or {}).get("active"), (entry or {}).get("candidate"))
                    if record is not None
                    and record["authority"]["holder_identity"] == routed.holder_identity
                    and record["authority"]["instance_ref"] == routed.instance_ref
                    and record["authority"]["epoch"] == routed.epoch
                    and record["authority"]["key_fingerprint"] == routed.signer_fingerprint
                ]
                if len(matching) != 1:
                    raise ReceiverError("principal has no current operator-granted incarnation")
                record = matching[0]
                if record["state"] == "retired":
                    raise ReceiverError("retired incarnation cannot publish evidence")
                cursor.execute(
                    f"SELECT kind,payload,payload_sha256 FROM {routed.inbox_table} "
                    "WHERE request_id=%s",
                    (request_id,),
                )
                inbox = cursor.fetchone()
                if inbox is None or inbox[0] != "heartbeat":
                    raise ReceiverError("heartbeat request unavailable")
                body = inbox[1]
                if isinstance(body, memoryview):
                    body = body.tobytes()
                if isinstance(body, str):
                    body = body.encode()
                if not isinstance(body, bytes) or len(body) > 65536:
                    raise ReceiverError("heartbeat request size invalid")
                request_sha = hashlib.sha256(body).hexdigest()
                if request_sha != inbox[2]:
                    raise ReceiverError("heartbeat request digest mismatch")
                envelope = json.loads(body)
                if body != canonical(envelope):
                    raise ReceiverError("heartbeat request payload is not canonical")
                lease, digest = verify_heartbeat(envelope, granted_public_key=record["public_key"])
                authority = record["authority"]
                if not self._matches(lease, routed, authority):
                    raise ReceiverError("heartbeat identity differs from protected grant")
                now = self.clock()
                if (
                    authority["candidate_expires_at"] is not None
                    and now >= authority["candidate_expires_at"]
                ):
                    raise ReceiverError("candidate heartbeat grant expired")
                age = now - lease.observed_at
                if age < -30 or age >= lease.leaseDurationSeconds:
                    raise ReceiverError("heartbeat first observation expired or future-skewed")
                cursor.execute(
                    "SELECT request_sha256,principal,frame_id,holder_identity,instance_ref,"
                    "epoch,audience,signer_fingerprint,expected_revision,result_revision,status "
                    "FROM hq_live_results WHERE request_id=%s",
                    (request_id,),
                )
                result = cursor.fetchone()
                if result is not None:
                    if result != (
                        request_sha,
                        routed.principal,
                        routed.frame_id,
                        routed.holder_identity,
                        routed.instance_ref,
                        routed.epoch,
                        authority["audience"],
                        routed.signer_fingerprint,
                        head,
                        digest,
                        "accepted",
                    ):
                        raise ReceiverError("request ID was reused or accepted meaning changed")
                    connection.rollback()
                    return digest
                cursor.execute(
                    "SELECT sequence,digest,first_seen FROM hq_live_floors "
                    "WHERE frame_id=%s AND holder_identity=%s AND epoch=%s",
                    (routed.frame_id, routed.holder_identity, routed.epoch),
                )
                floor = cursor.fetchone()
                if floor is None:
                    raise ReceiverError("protected observer floor was not provisioned")
                previous_seq, previous_digest, _ = floor
                if lease.seq <= previous_seq:
                    raise ReceiverError("heartbeat replays protected accepted floor")
                if time.monotonic() >= deadline:
                    raise ReceiverError("trusted receiver deadline exceeded")
                cursor.execute(
                    "INSERT INTO hq_live_receipts "
                    "(frame_id,holder_identity,epoch,sequence,digest,first_seen,"
                    "lease_json,envelope_json,signer_fingerprint) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        routed.frame_id,
                        routed.holder_identity,
                        routed.epoch,
                        lease.seq,
                        digest,
                        now,
                        canonical(lease.model_dump(mode="json", exclude_none=True)),
                        body,
                        routed.signer_fingerprint,
                    ),
                )
                cursor.execute(
                    "UPDATE hq_live_floors SET sequence=%s,digest=%s,first_seen=%s "
                    "WHERE frame_id=%s AND holder_identity=%s AND epoch=%s AND sequence=%s "
                    "AND digest=%s",
                    (
                        lease.seq,
                        digest,
                        now,
                        routed.frame_id,
                        routed.holder_identity,
                        routed.epoch,
                        previous_seq,
                        previous_digest,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ReceiverError("protected observer floor CAS conflict")
                if previous_seq == 0:
                    cursor.execute(
                        "INSERT INTO hq_live_public_observations VALUES "
                        "(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            routed.frame_id,
                            routed.holder_identity,
                            routed.epoch,
                            lease.seq,
                            digest,
                            now + lease.leaseDurationSeconds,
                            canonical(lease.model_dump(mode="json", exclude_none=True)),
                            body,
                            routed.signer_fingerprint,
                        ),
                    )
                else:
                    cursor.execute(
                        "UPDATE hq_live_public_observations SET sequence=%s,digest=%s,"
                        "accepted_until=%s,lease_json=%s,envelope_json=%s,signer_fingerprint=%s "
                        "WHERE frame_id=%s AND holder_identity=%s AND epoch=%s "
                        "AND sequence=%s AND digest=%s",
                        (
                            lease.seq,
                            digest,
                            now + lease.leaseDurationSeconds,
                            canonical(lease.model_dump(mode="json", exclude_none=True)),
                            body,
                            routed.signer_fingerprint,
                            routed.frame_id,
                            routed.holder_identity,
                            routed.epoch,
                            previous_seq,
                            previous_digest,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ReceiverError("public observation CAS differs from protected floor")
                cursor.execute(
                    "INSERT INTO hq_live_results VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        request_id,
                        request_sha,
                        routed.principal,
                        routed.frame_id,
                        routed.holder_identity,
                        routed.instance_ref,
                        routed.epoch,
                        authority["audience"],
                        routed.signer_fingerprint,
                        head,
                        digest,
                        "accepted",
                        now,
                    ),
                )
                crossed_commit = True
                connection.commit()
                return digest
        except ReceiverError:
            if not crossed_commit:
                connection.rollback()
                raise
            raise ReceiverUnknown(request_id, request_sha) from None
        except (SqlSignatureError, SqlRuntimeError, ValueError, KeyError, TypeError):
            if not crossed_commit:
                connection.rollback()
                raise ReceiverError("heartbeat request authentication failed") from None
            raise ReceiverUnknown(request_id, request_sha) from None
        except Exception as exc:
            if not crossed_commit:
                connection.rollback()
                if getattr(exc, "args", (None,))[0] in (1213, 1205):
                    raise ReceiverError("protected observer floor CAS conflict") from None
                raise ReceiverError("trusted observer acceptance unavailable") from None
            if getattr(exc, "args", (None,))[0] in (1213, 1205):
                raise ReceiverError("protected observer floor CAS conflict") from None
            raise ReceiverUnknown(request_id, request_sha) from None
        finally:
            connection.close()

    @staticmethod
    def _matches(lease: HeartbeatLease, route: PrincipalBinding, authority: dict) -> bool:
        return (
            lease.frame_id,
            lease.holderIdentity,
            lease.instance_ref,
            lease.epoch,
            lease.key_id,
            lease.audience,
            lease.config_revision,
            lease.beadyard_id,
        ) == (
            route.frame_id,
            route.holder_identity,
            route.instance_ref,
            route.epoch,
            route.signer_fingerprint,
            authority["audience"],
            authority["config_revision"],
            authority.get("beadyard_id"),
        )
