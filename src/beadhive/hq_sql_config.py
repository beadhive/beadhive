"""Committed, ordered fleet configuration in the dedicated Dolt config database."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import tomllib
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML

from .beadyard_identity import (
    DOCUMENT_PATH,
    BeadyardIdentityError,
    parse_document,
    validate_publication_identity,
)
from .hq_document_validation import (
    DocumentValidationError,
    validate_documents,
    validate_repair_carrier,
)
from .hq_sql_deadline import flock_until
from .hq_sql_transport import FnoxBroker, SqlTransportError, connect
from .modules.config.domain.ports import (
    FleetConfigDocument,
    FleetConfigSnapshot,
    RawFleetConfigRevision,
    ordered_documents_digest,
)

TABLES = ("hq_config_meta", "hq_config_documents", "hq_config_publications")
_SCHEMA_COLUMNS = {
    "hq_config_meta": (
        ("singleton_id", "tinyint"),
        ("schema_version", "int"),
        ("backend_identity", "varchar(128)"),
        ("generation", "varchar(128)"),
        ("publication_sequence", "bigint unsigned"),
        ("publication_id", "char(36)"),
        ("documents_sha256", "char(64)"),
        ("document_count", "int unsigned"),
        ("initial_attempt_nonce", "char(36)"),
    ),
    "hq_config_documents": (
        ("path", "varchar(512)"),
        ("ordinal", "int unsigned"),
        ("kind", "varchar(32)"),
        ("content", "longblob"),
        ("content_sha256", "char(64)"),
    ),
    "hq_config_publications": (
        ("publication_id", "char(36)"),
        ("publication_sequence", "bigint unsigned"),
        ("generation", "varchar(128)"),
        ("expected_parent_revision", "varchar(128)"),
        ("documents_sha256", "char(64)"),
        ("document_count", "int unsigned"),
    ),
}
_SCHEMA_UNIQUE = {
    ("hq_config_meta", "singleton_id"),
    ("hq_config_documents", "path"),
    ("hq_config_documents", "ordinal"),
    ("hq_config_publications", "publication_id"),
    ("hq_config_publications", "publication_sequence"),
}
MAX_BYTES = 4 * 1024 * 1024
PATH = re.compile(
    r"beadyard\.json|fleet\.yaml|workspace(?:-[A-Za-z0-9_-]+)?\.toml|allowed_signers|"
    r"hosts/[A-Za-z0-9_-]+\.yaml|"
    r"hives/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.yaml"
)


class SqlConfigError(ValueError):
    """Committed configuration unavailable, inconsistent, or conflicted."""


class PublicationUnknown(SqlConfigError):
    """DOLT_COMMIT may have succeeded; recover by immutable publication ID."""

    def __init__(self, publication_id: str, expected_revision: str):
        self.publication_id = publication_id
        self.expected_revision = expected_revision
        super().__init__(
            "configuration publication acknowledgment unknown; recover exact "
            f"publication {publication_id} against original parent {expected_revision}"
        )


@dataclass(frozen=True)
class PublicationRecovery:
    """A committed witness for the exact original publication, not a new CAS."""

    publication_id: str
    expected_revision: str
    observed_head: str
    publication_sequence: int
    documents_sha256: str


@dataclass(frozen=True)
class InitialSeedReceipt:
    """The original immutable first config commit, distinct from current HEAD."""

    publication_id: str
    expected_schema_parent: str
    committed_revision: str
    observed_head: str
    backend_identity: str
    generation: str
    documents_sha256: str
    document_count: int
    beadyard_id: str


@dataclass(frozen=True)
class InitialDestination:
    """Authenticated read-only preflight; neither a reservation nor authority."""

    schema_revision: str
    empty: bool
    backend_identity: str
    generation: str


_digest = ordered_documents_digest


def _publication_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (TypeError, ValueError):
        raise SqlConfigError("immutable publication ID invalid") from None
    if parsed.version != 4 or str(parsed) != value:
        raise SqlConfigError("immutable publication ID invalid")
    return value


def _kind(path):
    if path == DOCUMENT_PATH:
        return "beadyard"
    if path == "fleet.yaml":
        return "fleet"
    if path.startswith("workspace"):
        return "workspace"
    if path == "allowed_signers":
        return "trust"
    if path.startswith("hosts/"):
        return "host"
    if path.startswith("hives/"):
        return "hive"
    raise SqlConfigError("unsupported configuration document path")


def _validate_carrier(documents, *, version=None):
    """Mandatory SQL carrier, partition and secret checks, including for repair."""
    if not isinstance(documents, tuple) or not documents:
        raise SqlConfigError("ordered configuration documents required")
    seen = set()
    size = 0
    for document in documents:
        if (
            not isinstance(document, FleetConfigDocument)
            or not PATH.fullmatch(document.path)
            or document.path in seen
            or not isinstance(document.content, str)
        ):
            raise SqlConfigError("invalid or duplicate configuration document")
        seen.add(document.path)
        size += len(document.path.encode()) + len(document.content.encode())
        if size > MAX_BYTES:
            raise SqlConfigError("configuration snapshot exceeds size bound")
        if document.path == DOCUMENT_PATH:
            try:
                parse_document(document.content)
            except BeadyardIdentityError:
                raise SqlConfigError("beadyard identity document invalid") from None
    if "fleet.yaml" not in seen:
        raise SqlConfigError("fleet document required")
    if version is not None and (version == 1) == (DOCUMENT_PATH in seen):
        raise SqlConfigError("HQ config storage version and beadyard identity disagree")
    from .modules.config.application.partition import HOST, partition_of

    fleet = next(doc.content for doc in documents if doc.path == "fleet.yaml")
    try:
        parsed = YAML(typ="safe").load(fleet)
    except Exception:
        raise SqlConfigError("fleet configuration syntax invalid") from None
    if not isinstance(parsed, dict):
        raise SqlConfigError("fleet configuration must be a mapping")

    def leaves(node, prefix=""):
        if isinstance(node, dict):
            for key, value in node.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                yield from leaves(value, path)
        elif prefix:
            yield prefix

    if any(partition_of(path) == HOST for path in leaves(parsed)):
        raise SqlConfigError("host-only values cannot enter committed fleet config")
    for document in documents:
        if document.path == "fleet.yaml":
            source = parsed
        elif document.path.startswith("workspace"):
            try:
                source = tomllib.loads(document.content)
            except (ValueError, UnicodeError):
                raise SqlConfigError("workspace configuration syntax invalid") from None
        elif document.path in ("allowed_signers", DOCUMENT_PATH):
            continue
        else:
            try:
                source = YAML(typ="safe").load(document.content)
            except Exception:
                raise SqlConfigError("configuration document syntax invalid") from None
        if not isinstance(source, dict):
            raise SqlConfigError("configuration document must be a mapping")

        def reject_secret_values(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if re.fullmatch(
                        r"(?i)(?:password|secret|token|api[_-]?key|private[_-]?key|credential)",
                        str(key),
                    ):
                        raise SqlConfigError("secret value key cannot enter committed config")
                    reject_secret_values(value)
            elif isinstance(node, list):
                for value in node:
                    reject_secret_values(value)
            elif isinstance(node, str) and re.search(r"://[^/@\s]+:[^/@\s]+@", node):
                raise SqlConfigError("credential-bearing URI cannot enter committed config")

        reject_secret_values(source)


def _validate(documents):
    _validate_carrier(documents)
    try:
        validate_documents(documents)
    except DocumentValidationError as exc:
        raise SqlConfigError(str(exc)) from None


class _BoundedCursor:
    def __init__(self, cursor, connection, deadline, timeout):
        self.cursor, self.connection = cursor, connection
        self.deadline, self.timeout = deadline, timeout

    def _check(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise SqlConfigError("HQ SQL operation deadline exceeded")
        self.connection._sock.settimeout(min(self.timeout, remaining))

    def execute(self, sql, params=None):
        self._check()
        result = self.cursor.execute(sql, params)
        self._check()
        return result

    def fetchone(self):
        self._check()
        return self.cursor.fetchone()

    def fetchall(self):
        self._check()
        return self.cursor.fetchall()

    @property
    def rowcount(self):
        return self.cursor.rowcount


@contextmanager
def _bounded_cursor(connection, deadline, timeout):
    with connection.cursor() as cursor:
        yield _BoundedCursor(cursor, connection, deadline, timeout)


class SqlFleetConfigRevisionStore:
    """One-connection AS OF read and original-parent Dolt publication CAS."""

    def __init__(self, settings, *, broker=None, clock=time.time):
        self.settings = settings
        self.broker = broker or FnoxBroker()
        self.clock = clock

    def _open(self, role, *, deadline=None):
        binding = self.settings.get(role)
        if not binding:
            raise SqlConfigError(f"HQ config {role} capability unavailable")
        own_deadline = time.monotonic() + binding["operation_timeout"]
        deadline = min(deadline, own_deadline) if deadline is not None else own_deadline
        if time.monotonic() >= deadline:
            raise SqlConfigError("HQ config operation deadline exceeded")
        try:
            return connect(binding, self.broker, deadline=deadline), deadline
        except SqlTransportError:
            raise SqlConfigError("verified HQ config connection unavailable") from None

    @staticmethod
    def _head(cursor):
        cursor.execute("SELECT DOLT_HASHOF('HEAD')")
        head = cursor.fetchone()[0]
        if not isinstance(head, str) or not head:
            raise SqlConfigError("committed HQ config head unavailable")
        return head

    @staticmethod
    def _schema_contract(cursor):
        """Pin the committed provisioning layout, including singleton CAS keys."""
        cursor.execute(
            "SELECT table_name,column_name,column_type,is_nullable,ordinal_position,"
            "collation_name "
            "FROM information_schema.columns WHERE table_schema=DATABASE() "
            "AND table_name IN ('hq_config_meta','hq_config_documents',"
            "'hq_config_publications') ORDER BY table_name,ordinal_position"
        )
        columns = {table: [] for table in TABLES}
        for table, name, kind, nullable, ordinal, collation in cursor.fetchall():
            columns[table].append((name, kind.lower(), nullable, ordinal, collation))
        for table, expected in _SCHEMA_COLUMNS.items():
            actual = columns[table]
            if [
                (name, kind, nullable, position) for name, kind, nullable, position, _ in actual
            ] != [(name, kind, "NO", index) for index, (name, kind) in enumerate(expected, 1)] or (
                table == "hq_config_documents" and actual[0][4] != "utf8mb4_bin"
            ):
                raise SqlConfigError("HQ config committed schema layout changed")
        cursor.execute(
            "SELECT table_name,index_name,column_name,non_unique,seq_in_index "
            "FROM information_schema.statistics WHERE table_schema=DATABASE() "
            "AND table_name IN ('hq_config_meta','hq_config_documents',"
            "'hq_config_publications')"
        )
        indexes = {}
        for table, index, name, non_unique, position in cursor.fetchall():
            indexes.setdefault((table, index), []).append((position, name, non_unique))
        unique = {
            (table, columns[0][1])
            for (table, _index), columns in indexes.items()
            if len(columns) == 1 and columns[0][0] == 1 and columns[0][2] == 0
        }
        if unique != _SCHEMA_UNIQUE:
            raise SqlConfigError("HQ config singleton/ordinal uniqueness changed")

    def _identity(self, cursor, role):
        binding = self.settings[role]
        cursor.execute("SELECT CURRENT_USER(),DATABASE(),ACTIVE_BRANCH(),DOLT_VERSION()")
        principal, database, branch, version = cursor.fetchone()
        if (
            not principal.startswith(binding["user"] + "@")
            or database != binding["database"]
            or branch != "main"
            or version != "2.3.5"
        ):
            raise SqlConfigError("HQ SQL principal, schema, branch or version mismatch")

    @staticmethod
    def _committed(cursor, head, *, validate_semantics=True):
        # Dolt accepts parameterized revision expressions with AS OF. All three
        # reads use the *captured* immutable hash, never a moving HEAD alias.
        cursor.execute(
            "SELECT singleton_id,schema_version,backend_identity,generation,"
            "publication_sequence,publication_id,documents_sha256,document_count "
            "FROM hq_config_meta AS OF %s",
            (head,),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise SqlConfigError("HQ config metadata missing or duplicated")
        (singleton, version, backend, generation, sequence, publication_id, digest, count) = rows[0]
        if (
            singleton != 1
            or version not in (1, 2, 3)
            or not backend
            or not generation
            or sequence < 1
        ):
            raise SqlConfigError("HQ config schema or authority metadata invalid")
        if version == 3:
            cursor.execute(
                "SELECT initial_attempt_nonce FROM hq_config_meta AS OF %s WHERE singleton_id=1",
                (head,),
            )
            nonce_row = cursor.fetchone()
            if nonce_row is None:
                raise SqlConfigError("HQ config initial arbitration witness missing")
            try:
                _publication_uuid(nonce_row[0])
            except SqlConfigError:
                raise SqlConfigError("HQ config initial arbitration witness invalid") from None
        cursor.execute(
            "SELECT path,ordinal,kind,content,content_sha256 "
            "FROM hq_config_documents AS OF %s ORDER BY ordinal",
            (head,),
        )
        raw = cursor.fetchall()
        documents = []
        for ordinal, (path, position, kind, content, content_sha) in enumerate(raw):
            if position != ordinal or kind != _kind(path):
                raise SqlConfigError("HQ config document order or kind invalid")
            if isinstance(content, memoryview):
                content = content.tobytes()
            if isinstance(content, str):
                content = content.encode("utf-8")
            if hashlib.sha256(content).hexdigest() != content_sha:
                raise SqlConfigError("HQ config document hash mismatch")
            try:
                documents.append(FleetConfigDocument(path, content.decode("utf-8")))
            except UnicodeError:
                raise SqlConfigError("HQ config document encoding invalid") from None
        documents = tuple(documents)
        _validate_carrier(documents, version=version)
        if validate_semantics:
            try:
                validate_documents(documents)
            except DocumentValidationError as exc:
                raise SqlConfigError(str(exc)) from None
        else:
            try:
                validate_repair_carrier(documents)
            except DocumentValidationError as exc:
                raise SqlConfigError(str(exc)) from None
        if len(documents) != count or _digest(documents) != digest:
            raise SqlConfigError("HQ config publication digest mismatch")
        cursor.execute(
            "SELECT publication_sequence,generation,documents_sha256,document_count "
            "FROM hq_config_publications AS OF %s WHERE publication_id=%s",
            (head, publication_id),
        )
        witness = cursor.fetchone()
        if witness != (sequence, generation, digest, count):
            raise SqlConfigError("HQ config publication witness mismatch")
        return backend, generation, sequence, documents, version

    def _snapshot(self, cursor, head, *, revision=None, deadline=None):
        if revision is not None and revision != head:
            raise SqlConfigError("requested configuration revision is no longer current")
        backend, generation, sequence, documents, _version = self._committed(cursor, head)
        self._check_floor(cursor, backend, generation, sequence, head, deadline=deadline)
        now = self.clock()
        return FleetConfigSnapshot(
            backend_identity="sql:" + backend,
            commit_revision=head,
            generation=generation,
            fetched_at=now,
            valid_until=now + self.settings["cache_ttl"],
            documents=documents,
        )

    def _check_floor(self, cursor, backend, generation, sequence, head, *, deadline=None):
        settings = self.settings
        if (
            not settings.get("floor_path")
            or not settings.get("backend_identity")
            or not settings.get("generation")
            or not settings.get("initial_revision")
            or backend != settings["backend_identity"]
            or generation != settings["generation"]
            or sequence < settings["minimum_sequence"]
        ):
            raise SqlConfigError("HQ config host trust/generation pin unavailable")
        path = Path(settings["floor_path"])
        if not path.is_absolute() or path.is_symlink():
            raise SqlConfigError("HQ config floor path must be protected absolute path")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with lock_path.open("a+b") as lock:
            try:
                flock_until(
                    lock, deadline or time.monotonic() + settings["reader"]["operation_timeout"]
                )
            except TimeoutError:
                raise SqlConfigError("HQ config floor custody wait exceeded deadline") from None
            if path.exists():
                if path.stat().st_mode & 0o077:
                    raise SqlConfigError("HQ config floor custody changed")
                try:
                    floor = json.loads(path.read_text())
                    prior_sequence = floor["sequence"]
                    prior_head = floor["head"]
                    if floor["generation"] != generation or floor["backend"] != backend:
                        raise SqlConfigError("HQ config generation or backend changed")
                except (OSError, ValueError, KeyError, TypeError):
                    raise SqlConfigError("HQ config floor unavailable") from None
            else:
                prior_sequence = settings["minimum_sequence"]
                prior_head = settings["initial_revision"]
            if sequence < prior_sequence or (sequence == prior_sequence and head != prior_head):
                raise SqlConfigError("HQ configuration rollback or amended head detected")
            if sequence > prior_sequence:
                cursor.execute("SELECT HAS_ANCESTOR(%s,%s)", (head, prior_head))
                if cursor.fetchone()[0] != 1:
                    raise SqlConfigError("HQ configuration history fork detected")
                # The first new witness after the floor must name the exact prior
                # immutable commit. A gap without its witness is a fork/recovery event.
                cursor.execute(
                    "SELECT expected_parent_revision FROM hq_config_publications AS OF %s "
                    "WHERE publication_sequence=%s",
                    (head, prior_sequence + 1),
                )
                row = cursor.fetchone()
                if row is None or row[0] != prior_head:
                    raise SqlConfigError("HQ configuration publication ancestry changed")
            body = json.dumps(
                {"backend": backend, "generation": generation, "sequence": sequence, "head": head},
                sort_keys=True,
            )
            temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                temporary.unlink(missing_ok=True)

    def load_snapshot(self, *, revision=None, deadline=None):
        connection, deadline = self._open("reader", deadline=deadline)
        try:
            binding = self.settings["reader"]
            timeout = min(binding["read_timeout"], binding["write_timeout"])
            with _bounded_cursor(connection, deadline, timeout) as cursor:
                self._identity(cursor, "reader")
                cursor.execute("START TRANSACTION")
                head = self._head(cursor)
                snapshot = self._snapshot(cursor, head, revision=revision, deadline=deadline)
            connection.rollback()
            if time.monotonic() >= deadline:
                raise SqlConfigError("HQ config operation deadline exceeded")
            return snapshot
        except SqlConfigError:
            raise
        except Exception:
            raise SqlConfigError("committed HQ configuration unavailable") from None
        finally:
            connection.close()

    def inspect_initial_destination(self) -> InitialDestination:
        """Describe a clean committed v3 schema without manufacturing config."""
        connection, deadline = self._open("publisher")
        try:
            binding = self.settings["publisher"]
            timeout = min(binding["read_timeout"], binding["write_timeout"])
            with _bounded_cursor(connection, deadline, timeout) as cursor:
                self._identity(cursor, "publisher")
                cursor.execute("START TRANSACTION")
                head = self._head(cursor)
                cursor.execute("SELECT * FROM dolt_status")
                if cursor.fetchall():
                    raise SqlConfigError("HQ config working tree is dirty")
                cursor.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=DATABASE() AND table_type='BASE TABLE' "
                    "AND table_name NOT LIKE 'dolt\\_%' ORDER BY table_name"
                )
                if {row[0] for row in cursor.fetchall()} != set(TABLES):
                    raise SqlConfigError("HQ config schema table allowlist changed")
                self._schema_contract(cursor)
                counts = []
                for table in TABLES:
                    cursor.execute(f"SELECT COUNT(*) FROM {table} AS OF %s", (head,))
                    counts.append(cursor.fetchone()[0])
            connection.rollback()
            return InitialDestination(
                head,
                counts == [0, 0, 0],
                self.settings["backend_identity"],
                self.settings["generation"],
            )
        except SqlConfigError:
            raise
        except Exception:
            raise SqlConfigError("HQ config initial destination unavailable") from None
        finally:
            connection.close()

    def inspect_raw_for_repair(self):
        """Return authenticated raw bytes and original HEAD to a publisher only.

        Integrity, publication witness and HOST floor remain mandatory. Semantic
        validation is deferred solely for this explicit repair view; it cannot be
        used for settings, runtime authority, or admission.
        """
        connection, deadline = self._open("publisher")
        try:
            binding = self.settings["publisher"]
            timeout = min(binding["read_timeout"], binding["write_timeout"])
            with _bounded_cursor(connection, deadline, timeout) as cursor:
                self._identity(cursor, "publisher")
                cursor.execute("START TRANSACTION")
                head = self._head(cursor)
                backend, generation, sequence, documents, _version = self._committed(
                    cursor, head, validate_semantics=False
                )
                self._check_floor(cursor, backend, generation, sequence, head, deadline=deadline)
            connection.rollback()
            return RawFleetConfigRevision(head, documents)
        except SqlConfigError:
            raise
        except Exception:
            raise SqlConfigError(
                "committed HQ configuration repair inspection unavailable"
            ) from None
        finally:
            connection.close()

    def recover_publication(self, publication_id, *, expected_revision):
        """Read committed current history for an uncertain original publication.

        None means no *confirmed* outcome on this branch; callers must not retry
        with a refreshed parent or UUID. A later valid publication may have
        advanced HEAD, so the returned witness is deliberately not a snapshot.
        """
        try:
            if str(uuid.UUID(publication_id)) != publication_id:
                raise ValueError()
        except (TypeError, ValueError):
            raise SqlConfigError("immutable publication ID invalid") from None
        if not isinstance(expected_revision, str) or not expected_revision:
            raise SqlConfigError("original publication parent required")
        connection, deadline = self._open("reader")
        try:
            binding = self.settings["reader"]
            timeout = min(binding["read_timeout"], binding["write_timeout"])
            with _bounded_cursor(connection, deadline, timeout) as cursor:
                self._identity(cursor, "reader")
                cursor.execute("START TRANSACTION")
                head = self._head(cursor)
                self._snapshot(cursor, head, deadline=deadline)
                cursor.execute(
                    "SELECT publication_sequence,expected_parent_revision,"
                    "documents_sha256 FROM hq_config_publications AS OF %s "
                    "WHERE publication_id=%s",
                    (head, publication_id),
                )
                rows = cursor.fetchall()
                if not rows:
                    connection.rollback()
                    return None
                if len(rows) != 1 or rows[0][1] != expected_revision:
                    raise SqlConfigError("committed publication identity or parent mismatch")
                cursor.execute("SELECT HAS_ANCESTOR(%s,%s)", (head, expected_revision))
                if cursor.fetchone()[0] != 1:
                    raise SqlConfigError("committed publication parent is not an ancestor")
                sequence, _, digest = rows[0]
            connection.rollback()
            return PublicationRecovery(publication_id, expected_revision, head, sequence, digest)
        except SqlConfigError:
            raise
        except Exception:
            raise SqlConfigError("committed publication recovery unavailable") from None
        finally:
            connection.close()

    def initialize_snapshot(self, documents, *, expected_schema_parent, publication_id):
        """First publication against a committed, empty three-table schema.

        The caller must persist the UUID and exact source digest before entering
        this method. An ambiguous DOLT_COMMIT never authorizes a new UUID or a
        refreshed parent. Normal load remains unavailable until a verified seed
        commit has been installed as the host's initial floor.
        """
        _validate(documents)
        _publication_uuid(publication_id)
        if not isinstance(expected_schema_parent, str) or not expected_schema_parent:
            raise SqlConfigError("original schema parent required")
        try:
            owner = next(doc.content for doc in documents if doc.path == DOCUMENT_PATH)
            beadyard_id = parse_document(owner)
        except (StopIteration, BeadyardIdentityError):
            raise SqlConfigError("first publication requires canonical beadyard identity") from None
        backend = self.settings.get("backend_identity")
        generation = self.settings.get("generation")
        if not backend or not generation or self.settings.get("minimum_sequence") != 1:
            raise SqlConfigError("first publication backend/generation pin unavailable")
        _validate_carrier(documents, version=2)
        digest = _digest(documents)
        connection, deadline = self._open("publisher")
        crossed_commit = False
        try:
            binding = self.settings["publisher"]
            timeout = min(binding["read_timeout"], binding["write_timeout"])
            with _bounded_cursor(connection, deadline, timeout) as cursor:
                self._identity(cursor, "publisher")
                cursor.execute("START TRANSACTION")
                head = self._head(cursor)
                if head != expected_schema_parent:
                    raise SqlConfigError("expected schema parent changed")
                cursor.execute("SELECT * FROM dolt_status")
                if cursor.fetchall():
                    raise SqlConfigError("HQ config working tree is dirty")
                cursor.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=DATABASE() AND table_type='BASE TABLE' "
                    "AND table_name NOT LIKE 'dolt\\_%' ORDER BY table_name"
                )
                if {row[0] for row in cursor.fetchall()} != set(TABLES):
                    raise SqlConfigError("HQ config schema table allowlist changed")
                self._schema_contract(cursor)
                for table in TABLES:
                    cursor.execute(f"SELECT COUNT(*) FROM {table} AS OF %s", (head,))
                    if cursor.fetchone()[0] != 0:
                        raise SqlConfigError("HQ config destination is already initialized")
                # This absent-key INSERT is the first data write. Competing
                # sessions contend on the same PK; a preceding SELECT is no lock.
                cursor.execute(
                    "INSERT INTO hq_config_meta VALUES (1,3,%s,%s,1,%s,%s,%s,%s)",
                    (
                        backend,
                        generation,
                        publication_id,
                        digest,
                        len(documents),
                        str(uuid.uuid4()),
                    ),
                )
                if cursor.rowcount != 1:
                    raise SqlConfigError("first publication singleton conflict")
                for ordinal, document in enumerate(documents):
                    body = document.content.encode("utf-8")
                    cursor.execute(
                        "INSERT INTO hq_config_documents "
                        "(path,ordinal,kind,content,content_sha256) VALUES (%s,%s,%s,%s,%s)",
                        (
                            document.path,
                            ordinal,
                            _kind(document.path),
                            body,
                            hashlib.sha256(body).hexdigest(),
                        ),
                    )
                cursor.execute(
                    "INSERT INTO hq_config_publications VALUES (%s,1,%s,%s,%s,%s)",
                    (publication_id, generation, expected_schema_parent, digest, len(documents)),
                )
                cursor.execute(
                    "CALL DOLT_ADD('hq_config_meta','hq_config_documents','hq_config_publications')"
                )
                crossed_commit = True
                cursor.execute(
                    "CALL DOLT_COMMIT('-m',%s,'--author',%s)",
                    (
                        f"HQ config initial publication {publication_id}",
                        "HQ operator <hq-operator@localhost>",
                    ),
                )
                row = cursor.fetchone()
                if not row or not isinstance(row[0], str):
                    raise PublicationUnknown(publication_id, expected_schema_parent)
                committed = row[0]
            receipt = self._initial_receipt_at(
                committed,
                expected_schema_parent,
                publication_id,
                digest,
                len(documents),
                beadyard_id,
                deadline=deadline,
            )
            return receipt
        except SqlConfigError as exc:
            if not crossed_commit:
                connection.rollback()
                raise
            if isinstance(exc, PublicationUnknown):
                raise
            raise PublicationUnknown(publication_id, expected_schema_parent) from None
        except Exception as exc:
            if not crossed_commit:
                connection.rollback()
                if getattr(exc, "args", (None,))[0] in (1213, 1205, 1062):
                    raise SqlConfigError("first publication singleton conflict") from None
                raise SqlConfigError("first publication failed") from None
            raise PublicationUnknown(publication_id, expected_schema_parent) from None
        finally:
            connection.close()

    def _initial_receipt_at(
        self,
        committed,
        expected_parent,
        publication_id,
        digest,
        count,
        beadyard_id,
        *,
        deadline=None,
    ):
        connection, deadline = self._open("publisher", deadline=deadline)
        try:
            binding = self.settings["publisher"]
            timeout = min(binding["read_timeout"], binding["write_timeout"])
            with _bounded_cursor(connection, deadline, timeout) as cursor:
                self._identity(cursor, "publisher")
                cursor.execute("START TRANSACTION")
                observed = self._head(cursor)
                if observed != committed:
                    cursor.execute("SELECT HAS_ANCESTOR(%s,%s)", (observed, committed))
                    if cursor.fetchone()[0] != 1:
                        raise SqlConfigError("initial publication is not current ancestry")
                backend, generation, sequence, documents, version = self._committed(
                    cursor, committed
                )
                if (
                    version != 3
                    or sequence != 1
                    or backend != self.settings["backend_identity"]
                    or generation != self.settings["generation"]
                    or len(documents) != count
                    or _digest(documents) != digest
                    or parse_document(
                        next(doc.content for doc in documents if doc.path == DOCUMENT_PATH)
                    )
                    != beadyard_id
                ):
                    raise SqlConfigError("initial publication immutable content mismatch")
                cursor.execute(
                    "SELECT expected_parent_revision FROM hq_config_publications AS OF %s "
                    "WHERE publication_id=%s",
                    (committed, publication_id),
                )
                row = cursor.fetchone()
                if row != (expected_parent,):
                    raise SqlConfigError("initial publication original parent mismatch")
                cursor.execute(
                    "SELECT commit_hash FROM dolt_log WHERE commit_hash=%s", (committed,)
                )
                if cursor.fetchone() != (committed,):
                    raise SqlConfigError("initial publication is not reachable on main")
                cursor.execute(
                    "SELECT parent_hash FROM dolt_commit_ancestors WHERE commit_hash=%s",
                    (committed,),
                )
                if cursor.fetchall() != ((expected_parent,),):
                    raise SqlConfigError("initial publication direct parent mismatch")
            connection.rollback()
            return InitialSeedReceipt(
                publication_id,
                expected_parent,
                committed,
                observed,
                backend,
                generation,
                digest,
                count,
                beadyard_id,
            )
        except SqlConfigError:
            raise
        except Exception:
            raise SqlConfigError("initial publication immutable receipt unavailable") from None
        finally:
            connection.close()

    def recover_initial_snapshot(
        self,
        *,
        publication_id,
        expected_schema_parent,
        documents_sha256,
        document_count,
        beadyard_id,
    ):
        """Resolve one original first commit after an uncertain acknowledgment.

        An unchanged publication row appears in history at every later commit;
        direct-parent evidence selects only the original first commit. Absence is
        not permission to retry with a new parent or publication identity.
        """
        _publication_uuid(publication_id)
        if (
            not isinstance(expected_schema_parent, str)
            or not expected_schema_parent
            or not isinstance(documents_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", documents_sha256)
            or type(document_count) is not int
            or document_count < 1
        ):
            raise SqlConfigError("initial publication intent invalid")
        try:
            from .beadyard_identity import parse_id

            parse_id(beadyard_id)
        except BeadyardIdentityError:
            raise SqlConfigError("initial publication beadyard identity invalid") from None
        connection, deadline = self._open("publisher")
        try:
            binding = self.settings["publisher"]
            timeout = min(binding["read_timeout"], binding["write_timeout"])
            with _bounded_cursor(connection, deadline, timeout) as cursor:
                self._identity(cursor, "publisher")
                cursor.execute("START TRANSACTION")
                observed = self._head(cursor)
                # A fresh schema with no seed is a pending outcome, never a
                # usable configuration or a safe refreshed-parent retry.
                cursor.execute("SELECT COUNT(*) FROM hq_config_meta AS OF %s", (observed,))
                if cursor.fetchone()[0] == 0:
                    connection.rollback()
                    return None
                self._committed(cursor, observed)
                cursor.execute(
                    "SELECT h.commit_hash FROM dolt_history_hq_config_publications h "
                    "JOIN dolt_commit_ancestors a ON a.commit_hash=h.commit_hash "
                    "JOIN dolt_log l ON l.commit_hash=h.commit_hash "
                    "WHERE h.publication_id=%s AND h.publication_sequence=1 "
                    "AND h.generation=%s AND h.expected_parent_revision=%s "
                    "AND h.documents_sha256=%s AND h.document_count=%s "
                    "AND a.parent_hash=%s AND a.parent_index=0",
                    (
                        publication_id,
                        self.settings["generation"],
                        expected_schema_parent,
                        documents_sha256,
                        document_count,
                        expected_schema_parent,
                    ),
                )
                candidates = {row[0] for row in cursor.fetchall()}
                if len(candidates) > 1:
                    raise SqlConfigError("initial publication history is ambiguous")
                if not candidates:
                    connection.rollback()
                    return None
                committed = candidates.pop()
            connection.rollback()
            return self._initial_receipt_at(
                committed,
                expected_schema_parent,
                publication_id,
                documents_sha256,
                document_count,
                beadyard_id,
                deadline=deadline,
            )
        except SqlConfigError:
            raise
        except Exception:
            raise SqlConfigError("initial publication recovery unavailable") from None
        finally:
            connection.close()

    def publish_snapshot(
        self, documents, *, expected_revision, explicit_adoption=False, publication_id=None
    ):
        return self._publish_snapshot(
            documents,
            expected_revision=expected_revision,
            explicit_adoption=explicit_adoption,
            publication_id=publication_id,
        )

    def repair_snapshot(self, documents, *, expected_revision):
        """Replace a semantically invalid prior HEAD by its exact original CAS.

        The candidate is fully validated before any DML. Only the privileged
        publisher may inspect the invalid prior carrier; normal reads still deny.
        """
        return self._publish_snapshot(
            documents, expected_revision=expected_revision, allow_invalid_previous=True
        )

    def _publish_snapshot(
        self,
        documents,
        *,
        expected_revision,
        allow_invalid_previous=False,
        explicit_adoption=False,
        publication_id=None,
    ):
        _validate(documents)
        if not isinstance(expected_revision, str) or not expected_revision:
            raise SqlConfigError("original expected configuration revision required")
        if publication_id is None:
            publication_id = str(uuid.uuid4())
        else:
            try:
                parsed_publication = uuid.UUID(publication_id)
            except (TypeError, ValueError):
                raise SqlConfigError("immutable publication ID invalid") from None
            if parsed_publication.version != 4 or str(parsed_publication) != publication_id:
                raise SqlConfigError("immutable publication ID invalid")
        digest = _digest(documents)
        connection, deadline = self._open("publisher")
        crossed_commit = False
        try:
            binding = self.settings["publisher"]
            timeout = min(binding["read_timeout"], binding["write_timeout"])
            with _bounded_cursor(connection, deadline, timeout) as cursor:
                self._identity(cursor, "publisher")
                cursor.execute("START TRANSACTION")
                head = self._head(cursor)
                if head != expected_revision:
                    raise SqlConfigError("expected configuration revision changed")
                # A trusted publisher has branch-wide staging power. Refuse a dirty
                # working tree instead of silently absorbing unrelated edits.
                cursor.execute("SELECT * FROM dolt_status")
                if cursor.fetchall():
                    raise SqlConfigError("HQ config working tree is dirty")
                cursor.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=DATABASE() AND table_type='BASE TABLE' "
                    "AND table_name NOT LIKE 'dolt\\_%' ORDER BY table_name"
                )
                visible = {row[0] for row in cursor.fetchall()}
                if visible != set(TABLES):
                    raise SqlConfigError("HQ config schema table allowlist changed")
                backend, generation, sequence, previous_documents, previous_version = (
                    self._committed(cursor, head, validate_semantics=not allow_invalid_previous)
                )
                try:
                    proposed_id = validate_publication_identity(
                        previous_documents, documents, explicit_adoption=explicit_adoption
                    )
                except BeadyardIdentityError:
                    raise SqlConfigError("beadyard identity publication conflict") from None
                target_version = max(previous_version, 2 if proposed_id is not None else 1)
                if target_version < previous_version:
                    raise SqlConfigError("HQ config storage version cannot regress")
                _validate_carrier(documents, version=target_version)
                self._check_floor(cursor, backend, generation, sequence, head, deadline=deadline)
                cursor.execute(
                    "UPDATE hq_config_meta SET schema_version=%s,"
                    "publication_sequence=%s,publication_id=%s,"
                    "documents_sha256=%s,document_count=%s "
                    "WHERE singleton_id=1 AND publication_sequence=%s",
                    (
                        target_version,
                        sequence + 1,
                        publication_id,
                        digest,
                        len(documents),
                        sequence,
                    ),
                )
                if cursor.rowcount != 1:
                    raise SqlConfigError("configuration publication CAS conflict")
                cursor.execute("DELETE FROM hq_config_documents")
                for ordinal, document in enumerate(documents):
                    kind = _kind(document.path)
                    body = document.content.encode("utf-8")
                    cursor.execute(
                        "INSERT INTO hq_config_documents "
                        "(path,ordinal,kind,content,content_sha256) VALUES (%s,%s,%s,%s,%s)",
                        (document.path, ordinal, kind, body, hashlib.sha256(body).hexdigest()),
                    )
                cursor.execute(
                    "INSERT INTO hq_config_publications VALUES (%s,%s,%s,%s,%s,%s)",
                    (
                        publication_id,
                        sequence + 1,
                        generation,
                        expected_revision,
                        digest,
                        len(documents),
                    ),
                )
                cursor.execute(
                    "CALL DOLT_ADD('hq_config_meta','hq_config_documents','hq_config_publications')"
                )
                crossed_commit = True
                cursor.execute(
                    "CALL DOLT_COMMIT('-m',%s,'--author',%s)",
                    (
                        f"HQ config publication {publication_id}",
                        "HQ operator <hq-operator@localhost>",
                    ),
                )
                committed = cursor.fetchone()
                if not committed or not isinstance(committed[0], str):
                    raise PublicationUnknown(publication_id, expected_revision)
                committed_hash = committed[0]
            # DOLT_COMMIT commits the SQL transaction; rollback cannot undo it.
            snapshot = self.load_snapshot(revision=committed_hash, deadline=deadline)
            if snapshot.documents != documents:
                raise PublicationUnknown(publication_id, expected_revision)
            return snapshot
        except SqlConfigError as exc:
            if not crossed_commit:
                connection.rollback()
                raise
            if isinstance(exc, PublicationUnknown):
                raise
            raise PublicationUnknown(publication_id, expected_revision) from None
        except Exception as exc:
            if not crossed_commit:
                connection.rollback()
                if getattr(exc, "args", (None,))[0] in (1213, 1205):
                    raise SqlConfigError("configuration publication CAS conflict") from None
                raise SqlConfigError("configuration publication failed") from None
            if getattr(exc, "args", (None,))[0] in (1213, 1205):
                raise SqlConfigError("configuration publication CAS conflict") from None
            raise PublicationUnknown(publication_id, expected_revision) from None
        finally:
            connection.close()
