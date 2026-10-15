"""Separately credentialed operator publication of protected SQL authority.

All account/table DDL and grants are explicit server-local provisioning.  This
writer only checks a preprovisioned principal/inbox and publishes signed
authority with an original committed HEAD CAS.  It never runs in a frame.

Storage (bh-xnn5p): every statement, ``DOLT_ADD`` and ``DOLT_COMMIT`` is the HQ storage port's
Dolt adapter's (:class:`beadhive_hq_storage_dolt.HqRuntimeStatements` through
:func:`beadhive.hq_storage_composition.runtime_statements`). The authority publication is an
``AS OF``-verified CAS of the committed ``hq_authority`` row plus the registry and floor seed in
ONE Dolt commit, so it keeps its own connection on that Dolt-only surface; this module holds no
SQL.
"""

from __future__ import annotations

import hashlib
import json
import time

from . import hq_authority_enforce
from . import hq_authority_guard as guard
from .hq_hive_policy import project_hive_policies
from .hq_sql_runtime import SqlRuntimeAuthority, SqlRuntimeCapabilityMissing
from .hq_sql_runtime_schema import (
    evidence_table,
    inbox_table,
    routed_to_inbox,
    session_table,
)
from .hq_sql_signatures import (
    canonical,
    sign_authority,
    sql_principal,
    verify_authority,
    verify_heartbeat,
)
from .hq_sql_transport import FnoxBroker, SqlTransportError, connect
from .hq_storage_composition import runtime_statements

#: Heartbeat rows deleted per statement (and per commit) by ``prune_inbox``.
PRUNE_BATCH = 500


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
        return sql_principal(authority)

    def _identity(self, cursor):
        binding = self.settings["authority_writer"]
        user, database, branch, version = runtime_statements(cursor).identity()
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
                sql = runtime_statements(cursor)
                self._identity(sql)
                sql.begin()
                head = sql.head()
                state, crossref, policies = self.authority.verified_state_at(
                    sql, head, deadline=deadline, allow_expired=True
                )
            connection.rollback()
            return head, state, crossref, policies
        except SqlOperatorError:
            raise
        except SqlRuntimeCapabilityMissing as error:
            # A named privilege gap (bh-aprgo), still a refusal; never an outage guess.
            raise SqlOperatorError(
                f"protected authority writer read unavailable: {error}"
            ) from None
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
                sql = runtime_statements(cursor)
                self._identity(sql)
                sql.begin()
                head = sql.head()
                if head != expected_revision:
                    raise SqlOperatorError("protected authority changed before observation")
                state, _crossref, _policies = self.authority.verified_state_at(
                    sql, head, deadline=deadline, allow_expired=True
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
                registration_row = sql.registration_record(
                    frame, holder_identity, authority["instance_ref"], authority["epoch"]
                )
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
                floor = sql.floor(frame, holder_identity, authority["epoch"])
                receipts = []
                if floor is not None and floor[0] > 0:
                    for (
                        sequence,
                        digest,
                        first_seen,
                        body,
                        envelope_body,
                        signer,
                    ) in sql.latest_receipts(frame, holder_identity, authority["epoch"]):
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
                public = sql.public_observation(frame, holder_identity, authority["epoch"])
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
        release_upgrade=None,
        deadline=None,
    ) -> str:
        """Sign current canonical catalog projection and exact authority CAS.

        Without *operator_key* in trusted mode (bh-l4q0s) the row carries the unsigned marker
        :data:`beadhive.hq_authority_enforce.UNSIGNED_SIGNATURE` instead of a signature."""
        if not isinstance(expected_revision, str) or not expected_revision:
            raise SqlOperatorError("original authority revision required")
        connection, deadline = self._open(deadline=deadline)
        crossed_commit = False
        try:
            with connection.cursor() as cursor:
                sql = runtime_statements(cursor)
                self._identity(sql)
                sql.begin()
                head = sql.head()
                if head != expected_revision:
                    raise SqlOperatorError("original authority revision changed")
                if sql.dirty():
                    raise SqlOperatorError("runtime committed working tree is dirty")
                current, _crossref, _policies = self.authority.verified_state_at(
                    sql, head, deadline=deadline, allow_expired=True
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
                snapshot = self.authority.load_latest_config_at(sql, deadline=deadline)
                from .frame_release_upgrade import preserve_history, validate_publication

                if release_upgrade is None:
                    preserve_history(current, state)
                else:
                    validate_publication(
                        current,
                        state,
                        head,
                        snapshot,
                        release_upgrade,
                        provisioned_route,
                        now=self.clock(),
                    )
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
                    table = self._provisioned_table(sql, principal, epoch)
                    if release_upgrade is not None and table == session_table(principal, epoch):
                        self._validate_session_route(sql, principal, epoch)
                    sql.insert_route((principal, frame_id, holder, instance, epoch, table, signer))
                    sql.insert_floor_seed(frame_id, holder, epoch)
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
                if operator_key:
                    signature = sign_authority(signed, signing_key=operator_key)
                    verify_authority(
                        signed,
                        signature,
                        granted_public_key=self.settings["runtime_operator_public_key"],
                    )
                elif hq_authority_enforce.trusted():
                    # Key-less trusted publication (bh-l4q0s): the unsigned marker, accepted
                    # only by trusted frames; signed frames refuse it fail-closed.
                    signature = hq_authority_enforce.UNSIGNED_SIGNATURE
                else:
                    raise SqlOperatorError("mutation requires separate --operator-key")
                written = sql.update_authority(
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
                    )
                )
                if written != 1:
                    raise SqlOperatorError("protected authority row CAS conflict")
                if time.monotonic() >= deadline:
                    raise SqlOperatorError("authority writer deadline exceeded")
                self.authority.fresh_config_head_fence(crossref[2], deadline=deadline)
                self.authority.fresh_runtime_head_fence(head, deadline=deadline)
                sql.stage_authority()
                crossed_commit = True
                committed = sql.commit_authority(
                    f"HQ {'signed' if operator_key else 'unsigned (trusted)'} authority "
                    f"{state['revision']}",
                    "HQ operator <hq-operator@localhost>",
                )
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

    def _validate_session_route(self, sql, principal, epoch):
        """Re-check the provisioned session route, naming why it cannot (bh-t759y).

        Drift and a missing inspection privilege both refuse before any write; neither may
        surface as the publisher's generic "publication unavailable"."""
        from .hq_sql_capabilities import denied, session_inspection
        from .hq_sql_session import SessionError
        from .hq_sql_session_provision import validate_incarnation

        database = self.settings["authority_writer"]["database"]
        try:
            validate_incarnation(sql, principal, epoch, database=database)
        except SessionError as error:
            missing = session_inspection(sql, database)
            if missing:
                raise SqlOperatorError(_inspection_refusal(missing)) from None
            raise SqlOperatorError(f"reviewed session route invalid: {error}") from None
        except Exception as error:
            if denied(error):
                raise SqlOperatorError(
                    _inspection_refusal(session_inspection(sql, database))
                ) from None
            raise

    def preflight(self, *, route, deadline=None) -> dict:
        """Redacted capability report of this binding for the reviewed route (bh-aprgo).

        Read-only: probes reads and its own privilege rows; never writes or commits."""
        from .hq_sql_capabilities import CapabilityProbeError, report

        connection, _deadline = self._open(deadline=deadline)
        try:
            with connection.cursor() as cursor:
                sql = runtime_statements(cursor)
                self._identity(sql)
                head = sql.head()
                return report(
                    sql,
                    database=self.settings["authority_writer"]["database"],
                    config_database=(self.settings.get("reader") or {}).get("database"),
                    head=head,
                    route=route,
                )
        except SqlOperatorError:
            raise
        except CapabilityProbeError as error:
            raise SqlOperatorError(str(error)) from None
        except Exception:
            raise SqlOperatorError("operator capability preflight unavailable") from None
        finally:
            connection.close()

    @staticmethod
    def _provisioned_table(cursor, principal: str, epoch: int) -> str:
        """The registry ``inbox_table`` for a new incarnation, chosen by what the operator
        provisioned (data is the switch, bh-owqdg): its signed inbox when one exists (legacy, or
        Φ3 dual-write beside session tables), else its session table when both session and
        evidence tables exist (session-only). Anything else is refused."""
        inbox = inbox_table(principal, epoch)
        session, evidence = session_table(principal, epoch), evidence_table(principal, epoch)
        present = runtime_statements(cursor).tables_present((inbox, session, evidence))
        if inbox in present:
            return inbox
        if {session, evidence} <= present:
            return session
        raise SqlOperatorError("preprovisioned frame inbox or session tables missing")

    def describe_release_route(self, principal, epoch, frame, *, deadline=None):
        """Report the actual new route, or inherit the incumbent's provisioning family."""
        connection, _deadline = self._open(deadline=deadline)
        try:
            with connection.cursor() as cursor:
                sql = runtime_statements(cursor)
                self._identity(sql)
                inbox = inbox_table(principal, epoch)
                session, evidence = (
                    session_table(principal, epoch),
                    evidence_table(principal, epoch),
                )
                present = sql.tables_present((inbox, session, evidence))
                incomplete = (
                    bool(present & {session, evidence}) and not {session, evidence} <= present
                )
                table = (
                    inbox
                    if inbox in present
                    else session
                    if present & {session, evidence}
                    else None
                )
                if table is None:
                    prior = sql.latest_route_for_frame(frame)
                    table = (
                        session
                        if prior and prior[2] == session_table(prior[0], prior[1])
                        else inbox
                    )
                return {
                    "principal": principal,
                    "inbox_table": table,
                    "route": "session" if table == session else "inbox",
                    "session_table": session if table == session else None,
                    "evidence_table": evidence if table == session else None,
                    "provisioned": table in present and not incomplete,
                    "provisioning_incomplete": incomplete,
                }
        finally:
            connection.close()

    def provision_release_route(
        self, route, *, expected_revision, frame_address, password, deadline=None
    ):
        """Server-local, resumable provisioning; never publishes authority or receipts."""
        from .hq_sql_session_provision import resume_incarnation

        principal, _frame, _holder, _instance, epoch, _signer = route
        connection, deadline = self._open(deadline=deadline)
        try:
            with connection.cursor() as cursor:
                sql = runtime_statements(cursor)
                self._identity(sql)
                head = sql.head()
                if head != expected_revision:
                    raise SqlOperatorError(
                        "original authority revision changed before provisioning"
                    )
                self.authority.verified_state_at(sql, head, deadline=deadline, allow_expired=True)
                database = self.settings["authority_writer"]["database"]
                resume_incarnation(
                    sql,
                    principal,
                    epoch,
                    frame_address=frame_address,
                    password=password,
                    database=database,
                )
                account = f"'{principal}'@'{frame_address}'"
                # Read-only, database-wide (bh-aprgo): the frame's restore floor re-verifies
                # later provisioning commits with DOLT_DIFF_SUMMARY, which Dolt authorizes only
                # with SELECT on every table. The frame still writes only its own two rows.
                sql.grant_select_database(database, account)
                config_database = self.settings["reader"]["database"]
                for table in ("hq_config_meta", "hq_config_documents", "hq_config_publications"):
                    sql.grant_select(config_database, table, account)
                result = sql.head()
                self.authority.verified_state_at(sql, result, deadline=deadline, allow_expired=True)
                return result
        except (SqlOperatorError, ValueError):
            raise
        except Exception:
            raise SqlOperatorError(
                "session provisioning unavailable; use a server-local operator binding with "
                "DDL, CREATE USER and GRANT privileges, then replan/resume through the CLI"
            ) from None
        finally:
            connection.close()

    def prune_inbox(self, frame: str, *, retention_s: float, dry_run=False, deadline=None):
        """Delete expired signed-mode heartbeat rows from a frame's inboxes (bh-ce886).

        Runs only with the operator's ``authority_writer`` credential, which the operator
        grants ``SELECT, DELETE`` on each inbox table. Frames gain no DELETE. Which rows go
        is decided by :func:`beadhive.hq_sql_inbox_retention.plan_prune`; an inbox whose
        incarnation is no longer in the signed authority is skipped, never emptied blind.
        """
        from .hq_sql_inbox_retention import plan_prune
        from .hq_sql_runtime import PrincipalBinding

        if not isinstance(frame, str) or not frame:
            raise SqlOperatorError("prune requires a frame identity")
        connection, deadline = self._open(deadline=deadline)
        try:
            with connection.cursor() as cursor:
                sql = runtime_statements(cursor)
                self._identity(sql)
                sql.begin()
                head = sql.head()
                state, _crossref, _policies = self.authority.verified_state_at(
                    sql, head, deadline=deadline, allow_expired=True
                )
                routes = [PrincipalBinding(*row) for row in sql.routes_for_frame_at(head, frame)]
                if not routes:
                    raise SqlOperatorError("frame has no provisioned inbox")
                records = [record for identity, record in guard.records(state) if identity == frame]
                now = self.clock()
                inboxes, complete = [], True
                for route in sorted(routes, key=lambda item: item.epoch):
                    if route.inbox_table == session_table(route.principal, route.epoch):
                        inboxes.append(
                            {
                                "inbox": route.inbox_table,
                                "epoch": route.epoch,
                                "skipped": "session-only incarnation",
                            }
                        )
                        continue
                    if not routed_to_inbox(route.inbox_table, route.principal, route.epoch):
                        raise SqlOperatorError("protected principal table routing invalid")
                    matching = [
                        record
                        for record in records
                        if record["authority"]["holder_identity"] == route.holder_identity
                        and record["authority"]["instance_ref"] == route.instance_ref
                        and record["authority"]["epoch"] == route.epoch
                        and record["authority"]["key_fingerprint"] == route.signer_fingerprint
                    ]
                    summary = {"inbox": route.inbox_table, "epoch": route.epoch}
                    if len(matching) != 1:
                        inboxes.append({**summary, "skipped": "incarnation not in authority"})
                        continue
                    plan = plan_prune(
                        sql.inbox_heartbeat_ids(route.inbox_table),
                        route,
                        matching[0]["public_key"],
                        now=now,
                        retention_s=retention_s,
                    )
                    deleted = 0
                    if not dry_run:
                        # Each batch commits on its own: a deadline stops between batches with
                        # the progress kept, so a large first backlog drains over reruns.
                        for start in range(0, len(plan.delete), PRUNE_BATCH):
                            if time.monotonic() >= deadline:
                                complete = False
                                break
                            batch = plan.delete[start : start + PRUNE_BATCH]
                            sql.delete_inbox_heartbeats(route.inbox_table, tuple(batch))
                            connection.commit()
                            deleted += len(batch)
                    if dry_run:
                        counts = {"would_delete": len(plan.delete), "retained": plan.retained}
                    else:
                        left = plan.retained + len(plan.delete) - deleted
                        counts = {"deleted": deleted, "retained": left}
                    inboxes.append(
                        {
                            **summary,
                            **counts,
                            "unparseable": plan.unparseable,
                            "newest_verified": plan.newest_verified,
                        }
                    )
                    if not complete:
                        break
            connection.rollback()
            return {
                "frame": frame,
                "dry_run": bool(dry_run),
                "complete": complete,
                "retention_s": retention_s,
                "inboxes": inboxes,
            }
        except SqlOperatorError:
            connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise SqlOperatorError("inbox prune unavailable") from None
        finally:
            connection.close()


def _inspection_refusal(missing) -> str:
    named = "; ".join(str(item) for item in missing) or "a session inspection privilege"
    return (
        "this authority writer cannot inspect the provisioned session route ("
        + named
        + "). Run apply with the server-local administrative --operator-settings used for "
        "provision (docs/design/frame-release-upgrade-runbook.md), or grant the listed "
        "read capabilities; the authority is unchanged"
    )
