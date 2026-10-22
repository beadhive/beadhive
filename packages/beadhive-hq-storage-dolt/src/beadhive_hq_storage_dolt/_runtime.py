"""The HQ runtime database's Dolt-only statement surface (bh-xnn5p).

Root's runtime authority (``hq_sql_runtime``), trusted receiver (``hq_sql_receiver``), operator
(``hq_sql_operator``), session rows (``hq_sql_session`` and its provisioning) and the committed
config reads they take on the same transaction (``hq_sql_config``) verify signed, committed
history: ``AS OF`` reads at an exact head, ``HAS_ANCESTOR``, ``dolt_commit_ancestors``,
``DOLT_DIFF_SUMMARY`` and ``dolt_schemas`` history, and multi-table acceptance writes (receipt,
floor, public observation and result) that must commit together. None of that is a generic port
operation (ADR section 4: commit-graph walks and ``AS OF`` history are Dolt-only), so it is a
**Dolt-only, typed statement surface** over the caller's own DB-API cursor:

* :class:`HqRuntimeStatements` names every statement those modules run. Each method issues
  exactly the SQL root issued before and returns the rows exactly as the cursor fetched them, so
  root keeps its own connection, transaction boundaries, deadlines and verification logic, and
  its SQL-level test doubles keep matching. Root holds no SQL string.
* :class:`DoltRuntimeReads` is the ``CapabilityHost`` capability that replaced the transitional
  ``DoltHqSessions``: one principal-checked, read-only transaction yielding the same surface,
  for the director's signed-authority read. It has no raw cursor and no write.

Identifiers (an inbox or session table, a database) are validated by root before they reach a
method; values are always bound parameters.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ._incarnation import (
    ELIGIBILITY_SQL,
    EVIDENCE_SQL,
    POLICY_SQL,
    READ_EVIDENCE_SQL,
    RENEW_SQL,
)
from ._release_catalog import (
    CATALOG_DIFF_SQL,
    CATALOG_INSERT_SQL,
    CATALOG_ROWS_SQL,
    CATALOG_VERSIONS_OF_DIGEST_SQL,
)

if TYPE_CHECKING:
    from ._hq import DoltHqStorage

__all__ = ["DoltRuntimeReads", "HqRuntimeStatements", "SqlDoltRuntimeReads"]

_ROUTE_COLUMNS = (
    "SELECT principal,frame_id,holder_identity,instance_ref,epoch,"
    "inbox_table,signer_fingerprint FROM hq_principal_registry AS OF %s "
)
_RESULT_ROW = (
    "SELECT request_sha256,principal,frame_id,holder_identity,instance_ref,"
    "epoch,audience,signer_fingerprint,expected_revision,result_revision,status "
    "FROM hq_live_results WHERE request_id=%s"
)
_OBSERVATION_ROW = (
    "SELECT sequence,digest,accepted_until,lease_json,envelope_json,"
    "signer_fingerprint "
    "FROM hq_live_public_observations WHERE frame_id=%s "
    "AND holder_identity=%s AND epoch=%s"
)
_FLOOR_ROW = (
    "SELECT sequence,digest,first_seen FROM hq_live_floors "
    "WHERE frame_id=%s AND holder_identity=%s AND epoch=%s"
)
_RESULT_INSERT = "INSERT INTO hq_live_results VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"


class HqRuntimeStatements:
    """The runtime database's statements over one caller-owned DB-API cursor.

    Every method is one (rarely two) statement on ``cursor``, fetched the way root fetched it.
    :meth:`of` wraps a cursor once and passes a surface through unchanged, so root functions
    accept either."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    @classmethod
    def of(cls, cursor: Any) -> HqRuntimeStatements:
        return cursor if isinstance(cursor, HqRuntimeStatements) else cls(cursor)

    def _all(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        self._cursor.execute(sql, params)
        return self._cursor.fetchall()

    def _one(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        self._cursor.execute(sql, params)
        return self._cursor.fetchone()

    def _count(self, sql: str, params: Sequence[Any] | None = None) -> int:
        self._cursor.execute(sql, params)
        return self._cursor.rowcount

    # -- session, head and history ------------------------------------------------------------

    def identity(self) -> Any:
        """``(CURRENT_USER(), DATABASE(), ACTIVE_BRANCH(), DOLT_VERSION())``."""
        return self._one("SELECT CURRENT_USER(),DATABASE(),ACTIVE_BRANCH(),DOLT_VERSION()")

    def session_identity(self) -> Any:
        """``(CURRENT_USER(), DATABASE(), ACTIVE_BRANCH())`` (the session route resolver)."""
        return self._one("SELECT CURRENT_USER(), DATABASE(), ACTIVE_BRANCH()")

    def begin(self) -> None:
        self._cursor.execute("START TRANSACTION")

    def head(self) -> Any:
        """``DOLT_HASHOF('HEAD')`` of the current database, as fetched."""
        return self._one("SELECT DOLT_HASHOF('HEAD')")[0]

    def use(self, database: str) -> None:
        """Switch the session's database (root validates the identifier)."""
        self._cursor.execute(f"USE `{database}`")

    def has_ancestor(self, descendant: str, ancestor: str) -> Any:
        """``HAS_ANCESTOR(descendant, ancestor)`` as fetched (``1`` when it is)."""
        return self._one("SELECT HAS_ANCESTOR(%s,%s)", (descendant, ancestor))[0]

    def dirty(self) -> Any:
        """Every ``dolt_status`` row (an empty result is a clean committed working tree)."""
        return self._all("SELECT * FROM dolt_status")

    def first_parents(self, commit: str) -> Any:
        """``(parent_hash, parent_index)`` rows of ``commit``."""
        return self._all(
            "SELECT parent_hash,parent_index FROM dolt_commit_ancestors "
            "WHERE CONCAT(commit_hash,'')=%s",
            (commit,),
        )

    def diff_summary(self, before: str, after: str) -> Any:
        return self._all(
            "SELECT from_table_name,to_table_name,diff_type,data_change,schema_change "
            "FROM DOLT_DIFF_SUMMARY(%s,%s)",
            (before, after),
        )

    def schemas_at(self, revision: str) -> tuple[Any, list[str]]:
        """``(rows, column names)`` of ``dolt_schemas AS OF revision``."""
        self._cursor.execute("SELECT * FROM dolt_schemas AS OF %s", (revision,))
        rows = self._cursor.fetchall()
        return rows, [column[0] for column in self._cursor.description]

    def ignore_rows_at(self, revision: str) -> Any:
        return self._all(
            "SELECT pattern,ignored FROM dolt_ignore AS OF %s WHERE pattern='frame_*'",
            (revision,),
        )

    def ignore_all_at(self, revision: str) -> Any:
        """Every ``(pattern, ignored)`` row of ``dolt_ignore AS OF revision``, by pattern."""
        return self._all(
            "SELECT pattern,ignored FROM dolt_ignore AS OF %s ORDER BY pattern", (revision,)
        )

    def policy_table_at(self, revision: str) -> tuple[Any, list[str]]:
        """``(rows, column names)`` of the whole liveness policy table ``AS OF revision``."""
        self._cursor.execute("SELECT * FROM hq_liveness_policy AS OF %s", (revision,))
        rows = self._cursor.fetchall()
        return rows, [column[0] for column in self._cursor.description]

    def liveness_policy(self, *, at: str | None = None) -> Any:
        """The committed TTL rows, ``AS OF at`` or on the working set."""
        if at is None:
            return self._all(POLICY_SQL)
        return self._all(POLICY_SQL + " AS OF %s", (at,))

    def tables_present(self, names: Sequence[str]) -> set[Any]:
        """The ``names`` that exist in the current database."""
        rows = self._all(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=DATABASE() AND table_name IN "
            f"({','.join(['%s'] * len(names))})",
            tuple(names),
        )
        return {row[0] for row in rows}

    # -- the signed authority and the principal registry --------------------------------------

    def authority_at(self, revision: str) -> Any:
        """Every ``hq_authority`` row ``AS OF revision`` (exactly one when well-formed)."""
        return self._all(
            "SELECT singleton_id,schema_version,backend_identity,generation,revision,"
            "config_backend,config_generation,config_head,state_json,state_sha256,"
            "hive_policies_json,hive_policies_sha256,"
            "operator_signature FROM hq_authority AS OF %s",
            (revision,),
        )

    def authority_signature_at(self, revision: str) -> Any:
        """The ``(operator_signature,)`` row ``AS OF revision`` (a ``<head>~N`` ref allowed)."""
        return self._one("SELECT operator_signature FROM hq_authority AS OF %s", (revision,))

    def route_at(self, head: str, principal: str) -> Any:
        """The principal's 7-column registry route rows ``AS OF head``."""
        return self._all(_ROUTE_COLUMNS + "WHERE principal=%s", (head, principal))

    def routes_for_frame_at(self, head: str, frame: str) -> Any:
        return self._all(_ROUTE_COLUMNS + "WHERE frame_id=%s", (head, frame))

    def incarnations_at(self, head: str) -> Any:
        """``(principal, frame_id, holder_identity, epoch)`` of every registry row."""
        return self._all(
            "SELECT principal,frame_id,holder_identity,epoch FROM hq_principal_registry AS OF %s",
            (head,),
        )

    def registered_at(self, revision: str, incarnations: Sequence[tuple[str, int]]) -> Any:
        """The registry rows ``AS OF revision`` naming any of ``(principal, epoch)``."""
        conditions = " OR ".join("(principal=%s AND epoch=%s)" for _ in incarnations)
        return self._all(
            "SELECT principal,epoch FROM hq_principal_registry AS OF %s WHERE " + conditions,
            (revision, *(value for pair in incarnations for value in pair)),
        )

    def registry_epochs(self, principal: str) -> Any:
        return self._all(
            "SELECT epoch FROM hq_principal_registry WHERE principal = %s", (principal,)
        )

    def latest_route_for_frame(self, frame: str) -> Any:
        return self._one(
            "SELECT principal,epoch,inbox_table FROM hq_principal_registry "
            "WHERE frame_id=%s ORDER BY epoch DESC LIMIT 1",
            (frame,),
        )

    def insert_route(self, row: Sequence[Any]) -> None:
        self._cursor.execute(
            "INSERT INTO hq_principal_registry VALUES (%s,%s,%s,%s,%s,%s,%s)", tuple(row)
        )

    def update_authority(self, values: Sequence[Any]) -> int:
        """The authority CAS (``WHERE revision AND config_head``); returns the rowcount."""
        return self._count(
            "UPDATE hq_authority SET revision=%s,config_backend=%s,"
            "config_generation=%s,config_head=%s,state_json=%s,state_sha256=%s,"
            "hive_policies_json=%s,hive_policies_sha256=%s,operator_signature=%s "
            "WHERE singleton_id=1 AND revision=%s AND config_head=%s",
            tuple(values),
        )

    def stage_authority(self) -> None:
        self._cursor.execute("CALL DOLT_ADD('hq_authority','hq_principal_registry')")

    def commit_authority(self, message: str, author: str) -> Any:
        """``DOLT_COMMIT`` (the commit point); the fetched ``(hash,)`` row."""
        return self._one("CALL DOLT_COMMIT('-m',%s,'--author',%s)", (message, author))

    def grant_select(self, database: str, table: str, account: str) -> None:
        self._cursor.execute(f"GRANT SELECT ON `{database}`.`{table}` TO {account}")

    def grant_select_database(self, database: str, account: str) -> None:
        """Read-only ``SELECT`` on every table of ``database``, including tables created later
        (two-argument ``DOLT_DIFF_SUMMARY`` checks SELECT on all of them)."""
        self._cursor.execute(f"GRANT SELECT ON `{database}`.* TO {account}")

    def own_privileges(self) -> tuple[Any, Any]:
        """``(CURRENT_USER(), rows)``: every global and schema privilege row as
        ``(grantee, schema, privilege, is_grantable)``, global rows with schema ``'*'``.

        Dolt 2.3.5 refuses even an account's own ``SHOW GRANTS`` without ``mysql`` access, but
        serves ``information_schema`` privilege views to any account; the caller keeps only
        its own grantee's rows."""
        user = self._one("SELECT CURRENT_USER()")[0]
        rows = self._all(
            "SELECT grantee,'*',privilege_type,is_grantable "
            "FROM information_schema.user_privileges "
            "UNION ALL SELECT grantee,table_schema,privilege_type,is_grantable "
            "FROM information_schema.schema_privileges"
        )
        return user, rows

    # -- frame inboxes, results, leases and observations --------------------------------------

    def inbox_heartbeats(self, table: str) -> Any:
        """``(payload,)`` rows of the heartbeats in a frame inbox."""
        return self._all(f"SELECT payload FROM {table} WHERE kind='heartbeat'")

    def inbox_proposals(self, table: str) -> Any:
        """``(request_id, payload, payload_sha256)`` hive-lease proposal rows."""
        return self._all(
            f"SELECT request_id,payload,payload_sha256 FROM {table} WHERE kind='hive_lease'"
        )

    def inbox_heartbeat_ids(self, table: str) -> Any:
        return self._all(f"SELECT request_id,payload FROM {table} WHERE kind='heartbeat'")

    def delete_inbox_heartbeats(self, table: str, request_ids: Sequence[str]) -> None:
        self._cursor.execute(
            f"DELETE FROM {table} WHERE kind='heartbeat' "
            f"AND request_id IN ({','.join(['%s'] * len(request_ids))})",
            tuple(request_ids),
        )

    def inbox_request(self, table: str, request_id: str) -> Any:
        """``(kind, payload, payload_sha256)`` of one inbox request, or ``None``."""
        return self._one(
            f"SELECT kind,payload,payload_sha256 FROM {table} WHERE request_id=%s",
            (request_id,),
        )

    def insert_inbox(self, table: str, row: Sequence[Any]) -> None:
        self._cursor.execute(
            f"INSERT INTO {table} "
            "(request_id,kind,payload,payload_sha256,created_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            tuple(row),
        )

    def registration_witness(self, table: str, route: Sequence[Any]) -> Any:
        """The receiver-accepted registration result's signer for this exact incarnation
        (``route`` is principal, frame, holder, instance, epoch, signer)."""
        return self._one(
            f"SELECT r.signer_fingerprint FROM {table} AS i "
            "JOIN hq_live_results AS r ON r.request_id=i.request_id "
            "AND r.request_sha256=i.payload_sha256 "
            "WHERE i.kind='registration' AND r.status='accepted' AND r.principal=%s "
            "AND r.frame_id=%s AND r.holder_identity=%s AND r.instance_ref=%s AND r.epoch=%s "
            "AND r.signer_fingerprint=%s LIMIT 1",
            tuple(route),
        )

    def result(self, request_id: str) -> Any:
        """The 11-column ``hq_live_results`` row of a request, or ``None``."""
        return self._one(_RESULT_ROW, (request_id,))

    def accepted_result(self, request_id: str) -> Any:
        """The 9-column identity and outcome of a prior accepted request, or ``None``."""
        return self._one(
            "SELECT request_sha256,principal,frame_id,holder_identity,instance_ref,"
            "epoch,signer_fingerprint,result_revision,status FROM hq_live_results "
            "WHERE request_id=%s",
            (request_id,),
        )

    def result_exists(self, request_id: str) -> bool:
        return (
            self._one("SELECT 1 FROM hq_live_results WHERE request_id=%s", (request_id,))
            is not None
        )

    def insert_result(self, row: Sequence[Any]) -> None:
        self._cursor.execute(_RESULT_INSERT, tuple(row))

    def hive_lease(self, prefix: str) -> Any:
        """``(revision, lease_json, request_id, request_sha256)`` of a placement row, or
        ``None``."""
        return self._one(
            "SELECT revision,lease_json,request_id,request_sha256 "
            "FROM hq_live_hive_leases WHERE prefix=%s",
            (prefix,),
        )

    def update_hive_lease(self, values: Sequence[Any]) -> int:
        return self._count(
            "UPDATE hq_live_hive_leases SET revision=%s,lease_json=%s,"
            "request_id=%s,request_sha256=%s "
            "WHERE prefix=%s AND revision=%s",
            tuple(values),
        )

    def insert_hive_lease(self, row: Sequence[Any]) -> None:
        self._cursor.execute("INSERT INTO hq_live_hive_leases VALUES (%s,%s,%s,%s,%s)", tuple(row))

    def public_observation(self, frame: str, holder: str, epoch: int) -> Any:
        """The 6-column accepted public observation, or ``None``."""
        return self._one(_OBSERVATION_ROW, (frame, holder, epoch))

    def insert_public_observation(self, row: Sequence[Any]) -> None:
        self._cursor.execute(
            "INSERT INTO hq_live_public_observations VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            tuple(row),
        )

    def update_public_observation(self, values: Sequence[Any]) -> int:
        return self._count(
            "UPDATE hq_live_public_observations SET sequence=%s,digest=%s,"
            "accepted_until=%s,lease_json=%s,envelope_json=%s,signer_fingerprint=%s "
            "WHERE frame_id=%s AND holder_identity=%s AND epoch=%s "
            "AND sequence=%s AND digest=%s",
            tuple(values),
        )

    def floor(self, frame: str, holder: str, epoch: int) -> Any:
        """``(sequence, digest, first_seen)`` of the accepted floor, or ``None``."""
        return self._one(_FLOOR_ROW, (frame, holder, epoch))

    def insert_floor_seed(self, frame: str, holder: str, epoch: int) -> None:
        self._cursor.execute(
            "INSERT INTO hq_live_floors VALUES (%s,%s,%s,0,'',NULL)", (frame, holder, epoch)
        )

    def update_floor(self, values: Sequence[Any]) -> int:
        return self._count(
            "UPDATE hq_live_floors SET sequence=%s,digest=%s,first_seen=%s "
            "WHERE frame_id=%s AND holder_identity=%s AND epoch=%s AND sequence=%s "
            "AND digest=%s",
            tuple(values),
        )

    def receipt(self, frame: str, holder: str, epoch: int, sequence: int, digest: str) -> Any:
        """``(lease_json, envelope_json, first_seen)`` of one receipt, or ``None``."""
        return self._one(
            "SELECT lease_json,envelope_json,first_seen FROM hq_live_receipts "
            "WHERE frame_id=%s AND holder_identity=%s AND epoch=%s "
            "AND sequence=%s AND digest=%s",
            (frame, holder, epoch, sequence, digest),
        )

    def receipt_seen(self, frame: str, holder: str, epoch: int, sequence: int, digest: str) -> Any:
        """``(first_seen, lease_json, envelope_json)`` of one receipt, or ``None``."""
        return self._one(
            "SELECT first_seen,lease_json,envelope_json "
            "FROM hq_live_receipts "
            "WHERE frame_id=%s AND holder_identity=%s AND epoch=%s "
            "AND sequence=%s AND digest=%s",
            (frame, holder, epoch, sequence, digest),
        )

    def latest_receipts(self, frame: str, holder: str, epoch: int) -> Any:
        """The three newest receipts, newest first."""
        return self._all(
            "SELECT sequence,digest,first_seen,lease_json,envelope_json,"
            "signer_fingerprint "
            "FROM hq_live_receipts WHERE frame_id=%s AND holder_identity=%s "
            "AND epoch=%s ORDER BY sequence DESC LIMIT 3",
            (frame, holder, epoch),
        )

    def insert_receipt(self, row: Sequence[Any]) -> None:
        self._cursor.execute(
            "INSERT INTO hq_live_receipts "
            "(frame_id,holder_identity,epoch,sequence,digest,first_seen,"
            "lease_json,envelope_json,signer_fingerprint) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            tuple(row),
        )

    def registration(self, frame: str, holder: str, instance: str, epoch: int) -> Any:
        """``(request_id, request_sha256, manifest_json, signer_fingerprint)``, or ``None``."""
        return self._one(
            "SELECT request_id,request_sha256,manifest_json,signer_fingerprint "
            "FROM hq_live_registrations WHERE frame_id=%s AND holder_identity=%s "
            "AND instance_ref=%s AND epoch=%s",
            (frame, holder, instance, epoch),
        )

    def registration_record(self, frame: str, holder: str, instance: str, epoch: int) -> Any:
        """The registration with its ``accepted_at`` (operator evidence), or ``None``."""
        return self._one(
            "SELECT request_id,request_sha256,manifest_json,accepted_at,"
            "signer_fingerprint FROM hq_live_registrations "
            "WHERE frame_id=%s AND holder_identity=%s AND instance_ref=%s AND epoch=%s",
            (frame, holder, instance, epoch),
        )

    def update_registration(self, values: Sequence[Any]) -> int:
        return self._count(
            "UPDATE hq_live_registrations SET request_id=%s,request_sha256=%s,"
            "manifest_json=%s,accepted_at=%s WHERE frame_id=%s "
            "AND holder_identity=%s AND instance_ref=%s AND epoch=%s "
            "AND request_id=%s AND request_sha256=%s AND manifest_json=%s "
            "AND signer_fingerprint=%s",
            tuple(values),
        )

    def insert_registration(self, row: Sequence[Any]) -> None:
        self._cursor.execute(
            "INSERT INTO hq_live_registrations VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", tuple(row)
        )

    # -- the committed config, on the same transaction ----------------------------------------

    def config_meta_at(self, head: str) -> Any:
        return self._all(
            "SELECT singleton_id,schema_version,backend_identity,generation,"
            "publication_sequence,publication_id,documents_sha256,document_count "
            "FROM hq_config_meta AS OF %s",
            (head,),
        )

    def config_nonce_at(self, head: str) -> Any:
        return self._one(
            "SELECT initial_attempt_nonce FROM hq_config_meta AS OF %s WHERE singleton_id=1",
            (head,),
        )

    def config_documents_at(self, head: str) -> Any:
        return self._all(
            "SELECT path,ordinal,kind,content,content_sha256 "
            "FROM hq_config_documents AS OF %s ORDER BY ordinal",
            (head,),
        )

    def config_witness_at(self, head: str, publication_id: str) -> Any:
        return self._one(
            "SELECT publication_sequence,generation,documents_sha256,document_count "
            "FROM hq_config_publications AS OF %s WHERE publication_id=%s",
            (head, publication_id),
        )

    def config_parent_at(self, head: str, sequence: int) -> Any:
        """The ``(expected_parent_revision,)`` of publication ``sequence``, or ``None``."""
        return self._one(
            "SELECT expected_parent_revision FROM hq_config_publications AS OF %s "
            "WHERE publication_sequence=%s",
            (head, sequence),
        )

    # -- per-incarnation session and evidence rows --------------------------------------------

    def incarnation_tables(self, names: Sequence[str]) -> set[Any]:
        """The ``names`` that exist (the session data switch's spelling)."""
        names = tuple(names)
        rows = self._all(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() "
            f"AND table_name IN ({','.join(['%s'] * len(names))})",
            names,
        )
        return {row[0] for row in rows}

    def renew_session(self, session: str) -> int:
        """One renewal ``UPDATE`` (the trigger stamps server time); the rowcount."""
        return self._count(RENEW_SQL.format(session=session))

    def publish_evidence(self, evidence: str, values: Sequence[Any]) -> int:
        """The conformance job's evidence ``UPDATE`` (server-stamped); the rowcount."""
        return self._count(EVIDENCE_SQL.format(evidence=evidence), tuple(values))

    def evidence_release(self, evidence: str) -> Any:
        """``(epoch, release_id, release_digest, status, measured_at)`` of the one evidence
        row, or ``None``: the release the frame last attested, for the operator's reader check."""
        return self._one(READ_EVIDENCE_SQL.format(evidence=evidence))

    def eligibility(
        self,
        *,
        session: str,
        evidence: str,
        head: str,
        principal: str,
        epoch: int,
        desired_profile: str,
        desired_release_digest: str,
        prefix: str | None,
    ) -> Any:
        """The one eligibility statement's rows (``AS OF head`` for grant and policy)."""
        return self._all(
            ELIGIBILITY_SQL.format(session=session, evidence=evidence),
            (desired_profile, desired_release_digest, head, head, prefix, principal, epoch),
        )

    # -- operator provisioning (server-local; DoltAccounts territory, bh-3kl1k) ---------------

    def execute(self, statement: str, params: Sequence[Any] | None = None) -> None:
        """Run one operator DDL or grant statement the adapter rendered
        (:mod:`._incarnation`)."""
        self._cursor.execute(statement, params)

    def ignore_rules(self, pattern: str) -> Any:
        return self._all("SELECT pattern, ignored FROM dolt_ignore WHERE pattern = %s", (pattern,))

    def ignore_dirty(self) -> Any:
        return self._all("SELECT table_name FROM dolt_status WHERE table_name = 'dolt_ignore'")

    def status_tables(self) -> Any:
        return self._all("SELECT table_name FROM dolt_status")

    def commit_table(self, table: str, message: str) -> None:
        """Stage exactly one reviewed table (root validates the identifier) and commit it."""
        self._cursor.execute(f"CALL DOLT_ADD('{table}')")
        self._cursor.execute(
            "CALL DOLT_COMMIT('-m',%s,'--author','HQ release catalog <hq-catalog@localhost>')",
            (message,),
        )

    def catalog_diff_types(self, before: str, after: str) -> Any:
        """Distinct row-level diff types of ``hq_release_catalog`` between two revisions."""
        return self._all(CATALOG_DIFF_SQL, (before, after))

    def catalog_rows(self, catalog: str, version: str | None = None) -> Any:
        """The catalog's rows (``CATALOG_COLUMNS`` order), one version's or all."""
        if version is None:
            return self._all(CATALOG_ROWS_SQL + " WHERE catalog = %s ORDER BY version", (catalog,))
        return self._all(
            CATALOG_ROWS_SQL + " WHERE catalog = %s AND version = %s", (catalog, version)
        )

    def catalog_insert(self, values: Sequence[Any]) -> None:
        """Append one row (``CATALOG_COLUMNS`` order); the only write the catalog account holds."""
        self._cursor.execute(CATALOG_INSERT_SQL, tuple(values))

    def catalog_at(self, revision: str) -> tuple[Any, list[str]]:
        """``(rows, column names)`` of the catalog ``AS OF revision``."""
        self._cursor.execute(CATALOG_ROWS_SQL + " AS OF %s", (revision,))
        rows = self._cursor.fetchall()
        return rows, [column[0] for column in self._cursor.description]

    def release_catalog_rows(self, head: str, catalog: str, digest: str) -> Any:
        """``(catalog, version, digest, status, algorithm)`` of every version holding ``digest``,
        ``AS OF head``: the frame's read for release eligibility under a policy."""
        return self._all(CATALOG_VERSIONS_OF_DIGEST_SQL, (head, catalog, head, catalog, digest))

    def commit_schemas(self, message: str) -> None:
        self._cursor.execute("CALL DOLT_ADD('dolt_schemas')")
        self._cursor.execute(
            "CALL DOLT_COMMIT('-m',%s,'--author','HQ operator <hq-operator@localhost>')",
            (message,),
        )

    def accounts(self, principal: str, *, spaced: bool = False) -> Any:
        """``(host, ssl_type)`` of every ``mysql.user`` row of ``principal``."""
        sql = (
            "SELECT host, ssl_type FROM mysql.user WHERE user = %s"
            if spaced
            else "SELECT host,ssl_type FROM mysql.user WHERE user=%s"
        )
        return self._all(sql, (principal,))

    def table_rows(self, table: str, columns: str) -> Any:
        """``SELECT <columns> FROM <table>`` of an incarnation table (``id`` or ``id,epoch``)."""
        if columns not in ("id", "id,epoch"):
            raise ValueError("incarnation row reads are id or id,epoch")
        return self._all(f"SELECT {columns} FROM {table}")

    def trigger_fragment(self, name: str) -> Any:
        return self._all(
            "SELECT fragment FROM dolt_schemas WHERE type='trigger' AND name=%s", (name,)
        )

    def schemas(self, *, at: str | None = None) -> Any:
        if at is None:
            return self._all("SELECT * FROM dolt_schemas")
        return self._all("SELECT * FROM dolt_schemas AS OF %s", (at,))

    def describe(self, table: str) -> Any:
        return self._all(f"DESCRIBE {table}")

    def check_clauses(self, table: str) -> Any:
        return self._all(
            "SELECT check_clause FROM information_schema.check_constraints "
            "WHERE constraint_schema=DATABASE() AND constraint_name IN "
            "(SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE table_schema=DATABASE() AND table_name=%s AND constraint_type='CHECK')",
            (table,),
        )

    def indexes(self, table: str) -> Any:
        return self._all(f"SHOW INDEX FROM {table}")

    def triggers(self, database: str) -> Any:
        return self._all(
            "SELECT trigger_name, event_object_table, action_statement, action_timing, "
            "event_manipulation FROM information_schema.triggers WHERE trigger_schema = %s",
            (database,),
        )

    def require_secure_transport(self) -> Any:
        return self._all("SELECT @@require_secure_transport")

    def grants_for(self, principal: str, host: str) -> Any:
        return self._all(f"SHOW GRANTS FOR '{principal}'@'{host}'")

    def schemas_with_tables(self, names: Sequence[str]) -> Any:
        """Every schema holding any of ``names`` (the co-hosted hive database check)."""
        return self._all(
            "SELECT DISTINCT table_schema FROM information_schema.tables WHERE table_name IN "
            f"({','.join(['%s'] * len(names))})",
            tuple(names),
        )


@runtime_checkable
class DoltRuntimeReads(Protocol):
    """Dolt-only: one read-only, principal-checked runtime transaction for the signed-authority
    read (replaces the transitional ``DoltHqSessions``; no raw cursor, no write)."""

    def pinned(self) -> AbstractContextManager[HqRuntimeStatements]: ...


class SqlDoltRuntimeReads:
    """:class:`DoltRuntimeReads` over a :class:`~beadhive_hq_storage_dolt.DoltHqStorage`."""

    def __init__(self, storage: DoltHqStorage) -> None:
        self._storage = storage

    @contextmanager
    def pinned(self) -> Iterator[HqRuntimeStatements]:
        """``START TRANSACTION`` on a ``READER`` session, always rolled back. Errors propagate
        unclassified: the caller owns what a failed authority read means."""
        from ._hq import Role, _rollback

        with self._storage._session(Role.READER) as (connection, cursor):
            try:
                statements = HqRuntimeStatements(cursor)
                statements.begin()
                yield statements
            finally:
                _rollback(connection)
