"""Separately credentialed operator publication of protected SQL authority.

All account/table DDL and grants are explicit server-local provisioning.  This
writer only checks a preprovisioned principal/inbox and publishes signed
authority with an original committed HEAD CAS.  It never runs in a frame.
"""

from __future__ import annotations

import hashlib
import json
import time

from . import hq_authority_guard as guard
from .hq_hive_policy import project_hive_policies
from .hq_sql_runtime import SqlRuntimeAuthority
from .hq_sql_runtime_schema import inbox_table
from .hq_sql_signatures import (
    canonical,
    sign_authority,
    verify_authority,
    verify_heartbeat,
)
from .hq_sql_transport import FnoxBroker, SqlTransportError, connect


class SqlOperatorError(ValueError):
    """Protected operator authority publication is unavailable or conflicted."""


class AuthorityPublicationUnknown(SqlOperatorError):
    def __init__(self, expected_revision: str):
        self.expected_revision = expected_revision
        super().__init__(
            "authority commit acknowledgment unknown against original revision " + expected_revision
        )


class SqlRuntimeOperator:
    def __init__(self, settings, *, broker=None, clock=time.time):
        self.settings = settings
        self.broker = broker or FnoxBroker()
        self.clock = clock
        self.authority = SqlRuntimeAuthority(settings, broker=self.broker, clock=clock)

    def _open(self, *, deadline=None):
        binding = self.settings.get("authority_writer")
        if not binding or self.settings.get("runtime") is not None:
            raise SqlOperatorError("separate authority writer capability unavailable")
        if deadline is None:
            deadline = time.monotonic() + binding["operation_timeout"]
        if time.monotonic() >= deadline:
            raise SqlOperatorError("authority writer deadline exceeded")
        try:
            return connect(binding, self.broker, deadline=deadline), deadline
        except SqlTransportError:
            raise SqlOperatorError("verified authority writer connection unavailable") from None

    @staticmethod
    def principal_for(authority) -> str:
        """Stable SQL account for one incarnation across its HQ-ID binding."""
        from .hq_authority_payload import authority_payload

        identity = authority_payload(authority)
        identity.pop("beadyard_id", None)
        return "frame_" + hashlib.sha256(canonical(identity)).hexdigest()[:20]

    def _identity(self, cursor):
        binding = self.settings["authority_writer"]
        cursor.execute("SELECT CURRENT_USER(),DATABASE(),ACTIVE_BRANCH(),DOLT_VERSION()")
        user, database, branch, version = cursor.fetchone()
        if (
            not user.startswith(binding["user"] + "@")
            or database != binding["database"]
            or branch != "main"
            or version != "2.3.5"
        ):
            raise SqlOperatorError("authority writer principal or endpoint mismatch")

    def load(self, *, deadline=None):
        connection, deadline = self._open(deadline=deadline)
        try:
            with connection.cursor() as cursor:
                self._identity(cursor)
                cursor.execute("START TRANSACTION")
                cursor.execute("SELECT DOLT_HASHOF('HEAD')")
                head = cursor.fetchone()[0]
                state, crossref, policies = self.authority.verified_state_at(
                    cursor, head, deadline=deadline, allow_expired=True
                )
            connection.rollback()
            return head, state, crossref, policies
        except SqlOperatorError:
            raise
        except Exception:
            raise SqlOperatorError("protected authority writer read unavailable") from None
        finally:
            connection.close()

    def evidence(self, frame: str, holder_identity: str, *, expected_revision: str, deadline=None):
        """Read protected registration and consecutive accepted observer receipts."""
        from .host_manifest_contracts import HostManifest

        connection, deadline = self._open(deadline=deadline)
        try:
            with connection.cursor() as cursor:
                self._identity(cursor)
                cursor.execute("START TRANSACTION")
                cursor.execute("SELECT DOLT_HASHOF('HEAD')")
                head = cursor.fetchone()[0]
                if head != expected_revision:
                    raise SqlOperatorError("protected authority changed before observation")
                state, _crossref, _policies = self.authority.verified_state_at(
                    cursor, head, deadline=deadline, allow_expired=True
                )
                entry = state.get("frames", {}).get(frame)
                matching = [
                    record
                    for record in ((entry or {}).get("active"), (entry or {}).get("candidate"))
                    if record is not None
                    and record["authority"]["holder_identity"] == holder_identity
                ]
                if len(matching) != 1:
                    raise SqlOperatorError("granted observation incarnation unavailable")
                record = matching[0]
                authority = record["authority"]
                cursor.execute(
                    "SELECT request_id,request_sha256,manifest_json,accepted_at,"
                    "signer_fingerprint FROM hq_live_registrations "
                    "WHERE frame_id=%s AND holder_identity=%s AND instance_ref=%s AND epoch=%s",
                    (frame, holder_identity, authority["instance_ref"], authority["epoch"]),
                )
                registration_row = cursor.fetchone()
                registration = None
                if registration_row is not None:
                    if registration_row[4] != authority["key_fingerprint"]:
                        raise SqlOperatorError("registration signer differs from grant")
                    registration = HostManifest.model_validate_json(registration_row[2])
                    if (
                        registration.frame_id != frame
                        or registration.host_id != holder_identity
                        or registration.instance_ref != authority["instance_ref"]
                        or registration.beadyard_id != authority.get("beadyard_id")
                        or registration.release is None
                        or registration.release.model_dump() != record["desired"]["release"]
                        or registration.capabilities is None
                        or registration.capabilities.model_dump() != record["desired"]["caps"]
                    ):
                        raise SqlOperatorError("registration differs from desired grant")
                cursor.execute(
                    "SELECT sequence,digest,first_seen FROM hq_live_floors "
                    "WHERE frame_id=%s AND holder_identity=%s AND epoch=%s",
                    (frame, holder_identity, authority["epoch"]),
                )
                floor = cursor.fetchone()
                receipts = []
                if floor is not None and floor[0] > 0:
                    cursor.execute(
                        "SELECT sequence,digest,first_seen,lease_json,envelope_json,"
                        "signer_fingerprint "
                        "FROM hq_live_receipts WHERE frame_id=%s AND holder_identity=%s "
                        "AND epoch=%s ORDER BY sequence DESC LIMIT 3",
                        (frame, holder_identity, authority["epoch"]),
                    )
                    for (
                        sequence,
                        digest,
                        first_seen,
                        body,
                        envelope_body,
                        signer,
                    ) in cursor.fetchall():
                        if isinstance(envelope_body, bytes):
                            envelope_body = envelope_body.decode()
                        lease, verified_digest = verify_heartbeat(
                            json.loads(envelope_body),
                            granted_public_key=record["public_key"],
                        )
                        if (
                            digest != verified_digest
                            or canonical(lease.model_dump(mode="json", exclude_none=True))
                            != (body.encode() if isinstance(body, str) else body)
                            or signer != authority["key_fingerprint"]
                            or lease.seq != sequence
                            or lease.frame_id != frame
                            or lease.holderIdentity != holder_identity
                            or lease.instance_ref != authority["instance_ref"]
                            or lease.epoch != authority["epoch"]
                            or lease.key_id != signer
                            or lease.audience != authority["audience"]
                            or lease.config_revision != authority["config_revision"]
                        ):
                            raise SqlOperatorError("protected accepted receipt identity invalid")
                        if lease.beadyard_id != authority.get("beadyard_id"):
                            # Earlier signed v1 beats remain in protected history
                            # after binding. They never count toward a new bound
                            # admission streak, and the latest floor must already
                            # be a verified beat for the current HQ identity.
                            if not (
                                authority.get("beadyard_id") is not None
                                and lease.beadyard_id is None
                                and receipts
                                and receipts[0][0] == floor[0]
                                and receipts[0][3].beadyard_id == authority["beadyard_id"]
                                and sequence < floor[0]
                            ):
                                raise SqlOperatorError(
                                    "protected accepted receipt identity invalid"
                                )
                            continue
                        receipts.append((sequence, digest, first_seen, lease))
                    if not receipts or (receipts[0][0], receipts[0][1], receipts[0][2]) != floor:
                        raise SqlOperatorError("protected accepted receipt floor mismatch")
                cursor.execute(
                    "SELECT sequence,digest,accepted_until,lease_json,envelope_json,"
                    "signer_fingerprint "
                    "FROM hq_live_public_observations WHERE frame_id=%s "
                    "AND holder_identity=%s AND epoch=%s",
                    (frame, holder_identity, authority["epoch"]),
                )
                public = cursor.fetchone()
                if (
                    bool(public) != bool(receipts)
                    or public
                    and (
                        public[0] != floor[0]
                        or public[1] != floor[1]
                        or public[2] != floor[2] + receipts[0][3].leaseDurationSeconds
                        or public[5] != authority["key_fingerprint"]
                    )
                ):
                    raise SqlOperatorError("public observation differs from protected receipt")
                if public:
                    public_body = public[3].encode() if isinstance(public[3], str) else public[3]
                    public_envelope = (
                        public[4].decode() if isinstance(public[4], bytes) else public[4]
                    )
                    public_lease, public_digest = verify_heartbeat(
                        json.loads(public_envelope),
                        granted_public_key=record["public_key"],
                    )
                    if (
                        public_digest != public[1]
                        or public_lease != receipts[0][3]
                        or public_body
                        != canonical(public_lease.model_dump(mode="json", exclude_none=True))
                    ):
                        raise SqlOperatorError("public observation signed envelope invalid")
            connection.rollback()
            self.authority.fresh_runtime_head_fence(head, deadline=deadline)
            # A subsequent lifecycle publication rereads the same original HEAD;
            # private receipt changes remain protected and are checked again there.
            return record, registration, receipts
        except SqlOperatorError:
            raise
        except Exception:
            raise SqlOperatorError("protected observer evidence unavailable") from None
        finally:
            connection.close()

    def publish(
        self,
        state: dict,
        *,
        expected_revision: str,
        operator_key: str,
        provisioned_route=None,
        deadline=None,
    ) -> str:
        """Sign current canonical catalog projection and exact authority CAS."""
        if not isinstance(expected_revision, str) or not expected_revision:
            raise SqlOperatorError("original authority revision required")
        connection, deadline = self._open(deadline=deadline)
        crossed_commit = False
        try:
            with connection.cursor() as cursor:
                self._identity(cursor)
                cursor.execute("START TRANSACTION")
                cursor.execute("SELECT DOLT_HASHOF('HEAD')")
                head = cursor.fetchone()[0]
                if head != expected_revision:
                    raise SqlOperatorError("original authority revision changed")
                cursor.execute("SELECT * FROM dolt_status")
                if cursor.fetchall():
                    raise SqlOperatorError("runtime committed working tree is dirty")
                current, _crossref, _policies = self.authority.verified_state_at(
                    cursor, head, deadline=deadline, allow_expired=True
                )
                if (
                    not isinstance(state, dict)
                    or state.get("generation") != current["generation"]
                    or state.get("revision") != current["revision"] + 1
                    or state.get("issued_at", -1) < current["issued_at"]
                    or state.get("issued_at", -1) > self.clock() + 30
                    or state.get("expires_at", 0) <= self.clock()
                ):
                    raise SqlOperatorError("new authority sequence, time or generation invalid")
                guard.validate_state(state)
                if current["domain"] == guard.DOMAIN_V2 and state["domain"] != guard.DOMAIN_V2:
                    raise SqlOperatorError("bound authority carrier cannot downgrade")
                if (
                    current["domain"] == guard.DOMAIN
                    and state["domain"] == guard.DOMAIN_V2
                    and any(
                        entry["active"] is not None or entry["candidate"] is not None
                        for entry in current["frames"].values()
                    )
                ):
                    guard.validate_legacy_binding_transition(
                        current, state, trusted_now=self.clock()
                    )
                snapshot = self.authority.load_latest_config_at(cursor, deadline=deadline)
                bound_ids = {
                    record["authority"].get("beadyard_id")
                    for _, record in guard.records(state)
                    if record["authority"].get("beadyard_id") is not None
                }
                if bound_ids and bound_ids != {snapshot.beadyard_id}:
                    raise SqlOperatorError("authority belongs to a different beadyard")
                policies = project_hive_policies(
                    snapshot, valid_until=state["expires_at"], now=self.clock()
                )
                crossref = (
                    snapshot.backend_identity,
                    snapshot.generation,
                    snapshot.commit_revision,
                )
                if provisioned_route is not None:
                    principal, frame_id, holder, instance, epoch, signer = provisioned_route
                    table = inbox_table(principal, epoch)
                    cursor.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema=DATABASE() AND table_name=%s",
                        (table,),
                    )
                    if cursor.fetchone() != (table,):
                        raise SqlOperatorError("preprovisioned frame inbox table missing")
                    cursor.execute(
                        "INSERT INTO hq_principal_registry VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (principal, frame_id, holder, instance, epoch, table, signer),
                    )
                    cursor.execute(
                        "INSERT INTO hq_live_floors VALUES (%s,%s,%s,0,'',NULL)",
                        (frame_id, holder, epoch),
                    )
                body = canonical(state, limit=4 * 1024 * 1024)
                policy_body = canonical(policies, limit=4 * 1024 * 1024)
                signed = {
                    "backend_identity": self.settings["runtime_backend_identity"],
                    "generation": self.settings["runtime_generation"],
                    "revision": state["revision"],
                    "config_backend": crossref[0],
                    "config_generation": crossref[1],
                    "config_head": crossref[2],
                    "state": state,
                    "hive_policies": policies,
                }
                signature = sign_authority(signed, signing_key=operator_key)
                verify_authority(
                    signed,
                    signature,
                    granted_public_key=self.settings["runtime_operator_public_key"],
                )
                cursor.execute(
                    "UPDATE hq_authority SET revision=%s,config_backend=%s,"
                    "config_generation=%s,config_head=%s,state_json=%s,state_sha256=%s,"
                    "hive_policies_json=%s,hive_policies_sha256=%s,operator_signature=%s "
                    "WHERE singleton_id=1 AND revision=%s AND config_head=%s",
                    (
                        state["revision"],
                        *crossref,
                        body,
                        hashlib.sha256(body).hexdigest(),
                        policy_body,
                        hashlib.sha256(policy_body).hexdigest(),
                        signature,
                        current["revision"],
                        _crossref[2],
                    ),
                )
                if cursor.rowcount != 1:
                    raise SqlOperatorError("protected authority row CAS conflict")
                if time.monotonic() >= deadline:
                    raise SqlOperatorError("authority writer deadline exceeded")
                self.authority.fresh_config_head_fence(crossref[2], deadline=deadline)
                self.authority.fresh_runtime_head_fence(head, deadline=deadline)
                cursor.execute("CALL DOLT_ADD('hq_authority','hq_principal_registry')")
                crossed_commit = True
                cursor.execute(
                    "CALL DOLT_COMMIT('-m',%s,'--author',%s)",
                    (
                        f"HQ signed authority {state['revision']}",
                        "HQ operator <hq-operator@localhost>",
                    ),
                )
                committed = cursor.fetchone()
                if not committed or not isinstance(committed[0], str):
                    raise AuthorityPublicationUnknown(expected_revision)
                new_head = committed[0]
            connection.commit()
            verify_head, verify_state, verify_ref, verify_policies = self.load(deadline=deadline)
            if (
                verify_head != new_head
                or verify_state != state
                or verify_ref != crossref
                or verify_policies != policies
            ):
                raise AuthorityPublicationUnknown(expected_revision)
            return new_head
        except SqlOperatorError:
            if not crossed_commit:
                connection.rollback()
                raise
            raise AuthorityPublicationUnknown(expected_revision) from None
        except Exception:
            if not crossed_commit:
                connection.rollback()
                raise SqlOperatorError("protected authority publication unavailable") from None
            raise AuthorityPublicationUnknown(expected_revision) from None
        finally:
            connection.close()
