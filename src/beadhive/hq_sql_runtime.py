"""Read the separately protected, signed and committed SQL runtime authority.

This module owns no config publisher or frame signer.  It never provisions DDL or
uses a sender-owned inbox as an authority source.  Eligibility must additionally
join config and accepted observer evidence at its own qualified read boundary.

The one exception is ``hq.sql.liveness: signed``: the frame's own inbox then
supplies liveness *evidence* (never authority).  Each row is a heartbeat envelope
the frame signed with its granted key; only rows that verify against the committed
grant and bind to its exact incarnation are considered, and the newest signed
``renewTime`` wins.  See :func:`newest_signed_heartbeat`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import hq_authority_guard as guard
from .hq_hive_policy import project_hive_policies, validate_sql_hive_policies
from .hq_sql_deadline import flock_until
from .hq_sql_runtime_schema import inbox_table
from .hq_sql_signatures import SqlSignatureError, canonical, verify_authority, verify_heartbeat
from .hq_sql_transport import FnoxBroker, SqlTransportError, connect

#: A signed ``renewTime`` up to this far ahead of the reader's clock still counts (its age
#: clamps to zero); anything further ahead is skipped. Same window the receiver applies.
SIGNED_LIVENESS_SKEW_SECONDS = 30
#: Inbox payload ceiling, matching the receiver's heartbeat size bound.
_MAX_HEARTBEAT_BYTES = 65536


class SqlRuntimeError(ValueError):
    """The protected runtime binding is absent, stale, inconsistent or inaccessible."""


class SqlRuntimeDeadline(SqlRuntimeError):
    """The operation budget ran out (before or during a read/open) — not evidence of an outage."""


class SqlRuntimeUnavailable(SqlRuntimeError):
    """A connection/credential/transport failure with budget still remaining."""


class InboxUnknown(SqlRuntimeError):
    """A frame evidence COMMIT may have succeeded; inspect its immutable request ID."""

    def __init__(self, request_id: str, payload_sha256: str):
        self.request_id = request_id
        self.payload_sha256 = payload_sha256
        super().__init__(f"HQ frame evidence acknowledgment unknown for request {request_id}")


@dataclass(frozen=True)
class PrincipalBinding:
    principal: str
    frame_id: str
    holder_identity: str
    instance_ref: str
    epoch: int
    inbox_table: str
    signer_fingerprint: str


#: Process-level override for ``hq.sql.liveness``. It lets a newer bh run in signed mode
#: beside an older install sharing the same HOST file (whose strict schema would reject the
#: new key). Set, it wins over the config key; an unknown value is an error, never a fallback.
LIVENESS_ENV = "BH_HQ_SQL_LIVENESS"
LIVENESS_MODES = ("receiver", "signed")


def liveness_mode(settings) -> str:
    """Resolve the frame liveness mode: ``$BH_HQ_SQL_LIVENESS`` > ``hq.sql.liveness``."""
    raw = os.environ.get(LIVENESS_ENV)
    if raw is not None:
        if raw not in LIVENESS_MODES:
            raise SqlRuntimeError(
                f"{LIVENESS_ENV} must be 'receiver' or 'signed' (got {raw[:32]!r}); "
                "unset it to use hq.sql.liveness"
            )
        return raw
    value = (settings or {}).get("liveness", "receiver")
    if value not in LIVENESS_MODES:
        raise SqlRuntimeError("hq.sql.liveness must be 'receiver' or 'signed'")
    return value


def signed_liveness(settings) -> bool:
    """Whether this HOST selected signed liveness (env override first, then config)."""
    return liveness_mode(settings) == "signed"


def _payload_bytes(value):
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, str):
        value = value.encode()
    return value if isinstance(value, bytes) else None


def newest_signed_heartbeat(rows, route: PrincipalBinding, record: dict, *, now: float):
    """Pick the newest authentic heartbeat in the frame's own inbox.

    ``rows`` are ``(payload,)`` tuples of ``kind='heartbeat'`` inbox rows. A row counts only
    when its canonical envelope verifies against the grant's public key AND its lease binds to
    the exact committed incarnation (frame, holder, instance, epoch, key, audience, config
    revision, beadyard). Malformed, foreign-key, wrong-epoch or wrong-frame rows are skipped,
    never raised: the inbox is sender-written, so one bad row must not mask a good one.

    Candidates are ordered by their *claimed* ``(renewTime, seq)`` and verified newest-first,
    so the first one that verifies is the greatest signed ``renewTime`` — an older valid
    envelope can never outrank a newer one, and a forged "newer" row just costs one failed
    verification. A ``renewTime`` more than :data:`SIGNED_LIVENESS_SKEW_SECONDS` ahead of the
    reader's clock is skipped. The client-supplied ``created_at`` column is never consulted.

    Returns the same 6-tuple shape as an ``hq_live_public_observations`` row —
    ``(sequence, digest, accepted_until, lease_json, envelope_json, signer)`` with
    ``accepted_until = renewTime + leaseDurationSeconds`` — or ``None`` when nothing verifies.
    """
    from .hq_framelease_contracts import HeartbeatLease

    authority = record["authority"]
    expected = (
        route.frame_id,
        route.holder_identity,
        route.instance_ref,
        route.epoch,
        route.signer_fingerprint,
        authority["audience"],
        authority["config_revision"],
        authority.get("beadyard_id"),
    )
    candidates = []
    for row in rows:
        body = _payload_bytes(row[0] if isinstance(row, (tuple, list)) else row)
        if body is None or len(body) > _MAX_HEARTBEAT_BYTES:
            continue
        try:
            envelope = json.loads(body)
            if body != canonical(envelope):
                continue
            spec = envelope["spec"]
            claimed = HeartbeatLease.model_validate(
                {key: value for key, value in spec.items() if key != "signature"}
            )
            observed = claimed.observed_at
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        if observed - now > SIGNED_LIVENESS_SKEW_SECONDS:
            continue
        candidates.append((observed, claimed.seq, body, envelope))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    for _observed, _seq, body, envelope in candidates:
        try:
            lease, digest = verify_heartbeat(envelope, granted_public_key=record["public_key"])
        except (ValueError, TypeError, KeyError):
            continue
        if (
            lease.frame_id,
            lease.holderIdentity,
            lease.instance_ref,
            lease.epoch,
            lease.key_id,
            lease.audience,
            lease.config_revision,
            lease.beadyard_id,
        ) != expected:
            continue
        return (
            lease.seq,
            digest,
            lease.observed_at + lease.leaseDurationSeconds,
            canonical(lease.model_dump(mode="json", exclude_none=True)),
            body,
            route.signer_fingerprint,
        )
    return None


def _ascii(value):
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        return value.decode("ascii")
    if isinstance(value, str):
        return value
    raise SqlRuntimeError("runtime authority encoding invalid")


class SqlRuntimeAuthority:
    """One authenticated committed authority read with a durable HOST restore floor."""

    def __init__(self, settings, *, broker=None, clock=time.time):
        self.settings = settings
        self.broker = broker or FnoxBroker()
        self.clock = clock

    def _open(self, *, deadline=None):
        binding = self.settings.get("runtime")
        if not binding:
            raise SqlRuntimeError("AUTHORITY_NOT_READY: runtime binding unavailable")
        own_deadline = time.monotonic() + binding["operation_timeout"]
        deadline = min(deadline, own_deadline) if deadline is not None else own_deadline
        if time.monotonic() >= deadline:
            raise SqlRuntimeDeadline("HQ runtime operation deadline exceeded")
        try:
            return connect(binding, self.broker, deadline=deadline), deadline
        except SqlTransportError as exc:
            # A connect that ran out of the operation budget is deadline exhaustion, never a
            # claim that HQ is unreachable (bh-ktw0o). The transport message is a fixed,
            # secret-free string; only its deadline classification is consulted here.
            if time.monotonic() >= deadline or "deadline" in str(exc):
                raise SqlRuntimeDeadline("HQ runtime operation deadline exceeded") from None
            raise SqlRuntimeUnavailable("verified HQ runtime connection unavailable") from None

    def _check_floor(self, cursor, backend, generation, sequence, head, *, deadline=None):
        settings = self.settings
        if (
            backend != settings["runtime_backend_identity"]
            or generation != settings["runtime_generation"]
            or sequence < 1
        ):
            raise SqlRuntimeError("HQ runtime trust pin or authority generation changed")
        path = Path(settings["runtime_floor_path"])
        if not path.is_absolute() or path.is_symlink():
            raise SqlRuntimeError("HQ runtime floor custody invalid")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with lock_path.open("a+b") as lock:
            try:
                flock_until(lock, deadline or time.monotonic() + 15)
            except TimeoutError:
                raise SqlRuntimeError("HQ runtime floor custody wait exceeded deadline") from None
            if path.exists():
                if path.stat().st_mode & 0o077:
                    raise SqlRuntimeError("HQ runtime floor custody changed")
                try:
                    prior = json.loads(path.read_text())
                    if prior["backend"] != backend or prior["generation"] != generation:
                        raise SqlRuntimeError("HQ runtime floor binding changed")
                    previous_sequence, previous_head = prior["sequence"], prior["head"]
                except (OSError, ValueError, TypeError, KeyError):
                    raise SqlRuntimeError("HQ runtime floor unavailable") from None
            else:
                previous_sequence, previous_head = 1, settings["runtime_initial_revision"]
            if sequence < previous_sequence or (
                sequence == previous_sequence and head != previous_head
            ):
                raise SqlRuntimeError("HQ runtime rollback or amended authority detected")
            if sequence > previous_sequence:
                cursor.execute("SELECT HAS_ANCESTOR(%s,%s)", (head, previous_head))
                if cursor.fetchone()[0] != 1:
                    raise SqlRuntimeError("HQ runtime history fork detected")
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

    def verified_state_at(self, cursor, head, *, deadline=None, allow_expired=False):
        """Verify exact authority bytes on an already-owned SQL transaction."""
        cursor.execute(
            "SELECT singleton_id,schema_version,backend_identity,generation,revision,"
            "config_backend,config_generation,config_head,state_json,state_sha256,"
            "hive_policies_json,hive_policies_sha256,"
            "operator_signature FROM hq_authority AS OF %s",
            (head,),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise SqlRuntimeError("protected HQ runtime authority missing or duplicated")
        (
            singleton,
            schema,
            backend,
            generation,
            sequence,
            config_backend,
            config_generation,
            config_head,
            encoded,
            digest,
            policy_encoded,
            policy_digest,
            signature,
        ) = rows[0]
        if singleton != 1 or schema != 1 or not config_head:
            raise SqlRuntimeError("protected HQ runtime metadata invalid")
        if isinstance(encoded, memoryview):
            encoded = encoded.tobytes()
        if isinstance(encoded, str):
            encoded = encoded.encode()
        if not isinstance(encoded, bytes) or len(encoded) > 4 * 1024 * 1024:
            raise SqlRuntimeError("protected HQ runtime state size invalid")
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise SqlRuntimeError("protected HQ runtime state digest mismatch")
        if isinstance(policy_encoded, memoryview):
            policy_encoded = policy_encoded.tobytes()
        if isinstance(policy_encoded, str):
            policy_encoded = policy_encoded.encode()
        if (
            not isinstance(policy_encoded, bytes)
            or len(policy_encoded) > 4 * 1024 * 1024
            or hashlib.sha256(policy_encoded).hexdigest() != policy_digest
        ):
            raise SqlRuntimeError("protected HQ hive policy digest or size invalid")
        state = json.loads(encoded)
        policies = json.loads(policy_encoded)
        if encoded != canonical(state, limit=4 * 1024 * 1024):
            raise SqlRuntimeError("protected HQ runtime state is not canonical")
        if policy_encoded != canonical(policies, limit=4 * 1024 * 1024):
            raise SqlRuntimeError("protected HQ hive policy is not canonical")
        signed = {
            "backend_identity": backend,
            "generation": generation,
            "revision": sequence,
            "config_backend": config_backend,
            "config_generation": config_generation,
            "config_head": config_head,
            "state": state,
            "hive_policies": policies,
        }
        verify_authority(
            signed,
            _ascii(signature),
            granted_public_key=self.settings["runtime_operator_public_key"],
        )
        guard.validate_state(state)
        validate_sql_hive_policies(
            policies, config_head=config_head, now=self.clock(), require_fresh=False
        )
        if any(policy["valid_until"] > state["expires_at"] for policy in policies.values()):
            raise SqlRuntimeError("protected hive policy exceeds signed authority expiry")
        now = self.clock()
        if (
            state["generation"] != generation
            or state["revision"] != sequence
            or now < state["issued_at"] - 30
            or now >= state["expires_at"]
            and not allow_expired
        ):
            raise SqlRuntimeError("protected HQ runtime authority expired or inconsistent")
        self._check_floor(cursor, backend, generation, sequence, head, deadline=deadline)
        return state, (config_backend, config_generation, config_head), policies

    def load_config_at(self, cursor, crossref, *, deadline=None):
        """Read committed config through the same physical cross-database transaction.

        A split config/authority publication is rejected by exact protected
        backend, generation and HEAD cross-reference.  Callers still perform a
        fresh authoritative reread at their dispatch/claim point.
        """
        snapshot = self.load_latest_config_at(cursor, deadline=deadline)
        if (snapshot.backend_identity, snapshot.generation, snapshot.commit_revision) != crossref:
            raise SqlRuntimeError("HQ config and authority publications are not bound")
        return snapshot

    def load_latest_config_at(self, cursor, *, deadline=None):
        """Operator-only refresh may read the new config head before authority catches up."""
        from .hq_sql_config import SqlFleetConfigRevisionStore

        observer = (
            self.settings.get("observer")
            or self.settings.get("runtime")
            or self.settings.get("authority_writer")
        )
        config = self.settings.get("reader")
        if (
            not observer
            or not config
            or any(
                observer.get(key, "required" if key == "tls_mode" else "")
                != config.get(key, "required" if key == "tls_mode" else "")
                for key in (
                    ("host", "port", "tls_mode", "server_name", "ca_file")
                    if config.get("tls_mode", "required") == "required"
                    else ("host", "port", "tls_mode")
                )
            )
        ):
            raise SqlRuntimeError("HQ config and runtime SQL endpoint or transport policy mismatch")
        config_database = config["database"]
        runtime_database = observer["database"]
        if (
            not all(
                re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name)
                for name in (config_database, runtime_database)
            )
            or config_database == runtime_database
        ):
            raise SqlRuntimeError("HQ config/runtime database separation invalid")
        store = SqlFleetConfigRevisionStore(self.settings, broker=self.broker, clock=self.clock)
        cursor.execute(f"USE `{config_database}`")
        try:
            cursor.execute("SELECT DOLT_HASHOF('HEAD')")
            config_head = cursor.fetchone()[0]
            snapshot = store._snapshot(cursor, config_head, deadline=deadline)
        finally:
            cursor.execute(f"USE `{runtime_database}`")
        return snapshot

    def fresh_config_head_fence(self, expected_head: str, *, deadline: float) -> None:
        """Outside the pinned snapshot, reject a completed config publication.

        This is a fresh authoritative reread point, not a claim of atomicity
        between a later config commit and this runtime transaction's COMMIT.
        Every later dispatch/claim must perform its own current eligibility read.
        """
        observer = (
            self.settings.get("observer")
            or self.settings.get("runtime")
            or self.settings.get("authority_writer")
        )
        config = self.settings.get("reader")
        if (
            not observer
            or not config
            or any(
                observer.get(key, "required" if key == "tls_mode" else "")
                != config.get(key, "required" if key == "tls_mode" else "")
                for key in (
                    ("host", "port", "tls_mode", "server_name", "ca_file")
                    if config.get("tls_mode", "required") == "required"
                    else ("host", "port", "tls_mode")
                )
            )
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", config["database"])
        ):
            raise SqlRuntimeError("fresh config fence endpoint mismatch")
        if time.monotonic() >= deadline:
            raise SqlRuntimeError("fresh config fence deadline exceeded")
        try:
            connection = connect(observer, self.broker, deadline=deadline)
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT CURRENT_USER(),DATABASE(),ACTIVE_BRANCH(),DOLT_VERSION()"
                    )
                    user, database, branch, version = cursor.fetchone()
                    if (
                        not user.startswith(observer["user"] + "@")
                        or database != observer["database"]
                        or branch != "main"
                        or version != "2.3.5"
                    ):
                        raise SqlRuntimeError("fresh config fence observer mismatch")
                    cursor.execute(f"USE `{config['database']}`")
                    cursor.execute("SELECT DOLT_HASHOF('HEAD')")
                    if cursor.fetchone()[0] != expected_head or time.monotonic() >= deadline:
                        raise SqlRuntimeError("fresh config head differs from protected projection")
            finally:
                connection.close()
        except SqlRuntimeError:
            raise
        except Exception:
            raise SqlRuntimeError("fresh authoritative config reread unavailable") from None

    def fresh_runtime_head_fence(self, expected_head: str, *, deadline: float) -> None:
        """Reject a completed authority commit after the pinned receiver read."""
        observer = (
            self.settings.get("observer")
            or self.settings.get("runtime")
            or self.settings.get("authority_writer")
        )
        if not observer:
            raise SqlRuntimeError("fresh runtime observer unavailable")
        if time.monotonic() >= deadline:
            raise SqlRuntimeError("fresh runtime fence deadline exceeded")
        try:
            connection = connect(observer, self.broker, deadline=deadline)
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT CURRENT_USER(),DATABASE(),ACTIVE_BRANCH(),DOLT_VERSION()"
                    )
                    user, database, branch, version = cursor.fetchone()
                    if (
                        not user.startswith(observer["user"] + "@")
                        or database != observer["database"]
                        or branch != "main"
                        or version != "2.3.5"
                    ):
                        raise SqlRuntimeError("fresh runtime observer endpoint mismatch")
                    cursor.execute("SELECT DOLT_HASHOF('HEAD')")
                    if cursor.fetchone()[0] != expected_head or time.monotonic() >= deadline:
                        raise SqlRuntimeError("fresh authority head changed before acceptance")
            finally:
                connection.close()
        except SqlRuntimeError:
            raise
        except Exception:
            raise SqlRuntimeError("fresh authoritative runtime reread unavailable") from None

    def fresh_public_observation_fence(
        self, route: PrincipalBinding, expected, *, deadline: float
    ) -> None:
        """Reject a completed later observer receipt without exposing its private floor."""
        binding = self.settings.get("runtime")
        if not binding or time.monotonic() >= deadline:
            raise SqlRuntimeError("fresh public observation fence unavailable")
        try:
            connection = connect(binding, self.broker, deadline=deadline)
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT CURRENT_USER(),DATABASE(),ACTIVE_BRANCH(),DOLT_VERSION()"
                    )
                    user, database, branch, version = cursor.fetchone()
                    if (
                        not user.startswith(route.principal + "@")
                        or database != binding["database"]
                        or branch != "main"
                        or version != "2.3.5"
                    ):
                        raise SqlRuntimeError("fresh public observation principal mismatch")
                    cursor.execute(
                        "SELECT sequence,digest,accepted_until,lease_json,envelope_json,"
                        "signer_fingerprint "
                        "FROM hq_live_public_observations WHERE frame_id=%s "
                        "AND holder_identity=%s AND epoch=%s",
                        (route.frame_id, route.holder_identity, route.epoch),
                    )
                    current = cursor.fetchone()
                    if current != expected or time.monotonic() >= deadline:
                        raise SqlRuntimeError("accepted observer receipt changed during read")
            finally:
                connection.close()
        except SqlRuntimeError:
            raise
        except Exception:
            raise SqlRuntimeError("fresh public observation reread unavailable") from None

    def fresh_hive_lease_fence(self, prefix: str, expected, *, deadline: float) -> None:
        binding = self.settings.get("runtime")
        if not binding or time.monotonic() >= deadline:
            raise SqlRuntimeError("fresh hive lease fence unavailable")
        try:
            connection = connect(binding, self.broker, deadline=deadline)
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT CURRENT_USER(),DATABASE(),ACTIVE_BRANCH(),DOLT_VERSION()"
                    )
                    user, database, branch, version = cursor.fetchone()
                    if (
                        not user.startswith(binding["user"] + "@")
                        or database != binding["database"]
                        or branch != "main"
                        or version != "2.3.5"
                    ):
                        raise SqlRuntimeError("fresh hive lease principal mismatch")
                    cursor.execute(
                        "SELECT revision,lease_json,request_id,request_sha256 "
                        "FROM hq_live_hive_leases WHERE prefix=%s",
                        (prefix,),
                    )
                    if cursor.fetchone() != expected or time.monotonic() >= deadline:
                        raise SqlRuntimeError("protected hive lease changed during read")
            finally:
                connection.close()
        except SqlRuntimeError:
            raise
        except Exception:
            raise SqlRuntimeError("fresh hive lease reread unavailable") from None

    def read_frame_composite(self, *, prefix: str | None = None, deadline=None, enforce=True):
        """Read current config, grant, observer projection and optional hive lease together.

        The caller separately applies candidate/holder eligibility to these
        authenticated facts.  An outside-snapshot head reread fences completed
        publication before the result is returned.

        With ``hq.sql.liveness: signed`` the observation slot is instead the newest
        verified heartbeat from the frame's own inbox (:func:`newest_signed_heartbeat`),
        in the same 6-tuple shape, and the public-observation reread fence is skipped.

        ``enforce=False`` (``BH_HQ_AUTHORITY_ENFORCE=false``, UNSUPPORTED, dev-only) accepts
        expired signed authority and a config head that the authority does not cross-reference:
        the latest committed config is read, the signed policy projection is not compared to it,
        and the config fence pins the head actually read. Signatures, the replay floor, the
        principal route and the incarnation match are verified exactly as when enforcing.
        """
        signed = signed_liveness(self.settings)
        connection, deadline = self._open(deadline=deadline)
        try:
            binding = self.settings["runtime"]
            with connection.cursor() as cursor:
                cursor.execute("SELECT CURRENT_USER(),DATABASE(),ACTIVE_BRANCH(),DOLT_VERSION()")
                user, database, branch, version = cursor.fetchone()
                if (
                    not user.startswith(binding["user"] + "@")
                    or database != binding["database"]
                    or branch != "main"
                    or version != "2.3.5"
                ):
                    raise SqlRuntimeError("frame composite endpoint identity mismatch")
                principal = user.split("@", 1)[0]
                cursor.execute("START TRANSACTION")
                cursor.execute("SELECT DOLT_HASHOF('HEAD')")
                head = cursor.fetchone()[0]
                state, crossref, policies = self.verified_state_at(
                    cursor, head, deadline=deadline, allow_expired=not enforce
                )
                cursor.execute(
                    "SELECT principal,frame_id,holder_identity,instance_ref,epoch,"
                    "inbox_table,signer_fingerprint FROM hq_principal_registry AS OF %s "
                    "WHERE principal=%s",
                    (head, principal),
                )
                rows = cursor.fetchall()
                if len(rows) != 1:
                    raise SqlRuntimeError("frame composite principal not provisioned")
                route = PrincipalBinding(*rows[0])
                if route.inbox_table != inbox_table(principal, route.epoch):
                    raise SqlRuntimeError("frame composite table routing invalid")
                entry = state.get("frames", {}).get(route.frame_id)
                matches = [
                    (slot, record)
                    for slot, record in (
                        ("active", (entry or {}).get("active")),
                        ("candidate", (entry or {}).get("candidate")),
                    )
                    if record is not None
                    and record["authority"]["holder_identity"] == route.holder_identity
                    and record["authority"]["instance_ref"] == route.instance_ref
                    and record["authority"]["epoch"] == route.epoch
                    and record["authority"]["key_fingerprint"] == route.signer_fingerprint
                ]
                if len(matches) != 1 or matches[0][1]["state"] == "retired":
                    raise SqlRuntimeError("frame composite incarnation unavailable")
                slot, record = matches[0]
                if enforce:
                    snapshot = self.load_config_at(cursor, crossref, deadline=deadline)
                    projected = project_hive_policies(
                        snapshot, valid_until=state["expires_at"], now=self.clock()
                    )
                    if projected != policies:
                        raise SqlRuntimeError(
                            "frame composite policy differs from canonical catalog"
                        )
                else:
                    snapshot = self.load_latest_config_at(cursor, deadline=deadline)
                if signed:
                    # Signed liveness: this frame's own inbox (route-derived identifier,
                    # never sender-named), verified at read time. No receiver projection.
                    cursor.execute(
                        f"SELECT payload FROM {route.inbox_table} WHERE kind='heartbeat'"
                    )
                    observation = newest_signed_heartbeat(
                        cursor.fetchall(), route, record, now=self.clock()
                    )
                else:
                    cursor.execute(
                        "SELECT sequence,digest,accepted_until,lease_json,envelope_json,"
                        "signer_fingerprint "
                        "FROM hq_live_public_observations WHERE frame_id=%s "
                        "AND holder_identity=%s AND epoch=%s",
                        (route.frame_id, route.holder_identity, route.epoch),
                    )
                    observation = cursor.fetchone()
                if observation is not None and observation[5] != route.signer_fingerprint:
                    raise SqlRuntimeError("public observation signer differs from grant")
                lease_row = None
                if prefix is not None:
                    if not isinstance(prefix, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", prefix):
                        raise SqlRuntimeError("invalid hive lease prefix")
                    cursor.execute(
                        "SELECT revision,lease_json,request_id,request_sha256 "
                        "FROM hq_live_hive_leases WHERE prefix=%s",
                        (prefix,),
                    )
                    lease_row = cursor.fetchone()
                if time.monotonic() >= deadline:
                    raise SqlRuntimeError("frame composite deadline exceeded")
            connection.rollback()
            self.fresh_config_head_fence(
                crossref[2] if enforce else snapshot.commit_revision, deadline=deadline
            )
            self.fresh_runtime_head_fence(head, deadline=deadline)
            if not signed:
                self.fresh_public_observation_fence(route, observation, deadline=deadline)
            if prefix is not None:
                self.fresh_hive_lease_fence(prefix, lease_row, deadline=deadline)
            return head, state, route, slot, record, snapshot, policies, observation, lease_row
        except SqlRuntimeError:
            raise
        except Exception:
            raise SqlRuntimeError("qualified frame composite unavailable") from None
        finally:
            connection.close()

    def load_state(self, *, deadline=None, allow_expired=False):
        """Return (authority commit, validated state, exact config cross-reference)."""
        connection, deadline = self._open(deadline=deadline)
        try:
            binding = self.settings["runtime"]
            with connection.cursor() as cursor:

                def execute(sql, params=None):
                    if time.monotonic() >= deadline:
                        raise SqlRuntimeError("HQ runtime operation deadline exceeded")
                    cursor.execute(sql, params)
                    if time.monotonic() >= deadline:
                        raise SqlRuntimeError("HQ runtime operation deadline exceeded")

                execute("SELECT CURRENT_USER(),DATABASE(),ACTIVE_BRANCH(),DOLT_VERSION()")
                principal, database, branch, version = cursor.fetchone()
                if (
                    not principal.startswith(binding["user"] + "@")
                    or database != binding["database"]
                    or branch != "main"
                    or version != "2.3.5"
                ):
                    raise SqlRuntimeError(
                        "HQ runtime principal, database, branch or version mismatch"
                    )
                execute("START TRANSACTION")
                execute("SELECT DOLT_HASHOF('HEAD')")
                head = cursor.fetchone()[0]
                state, crossref, policies = self.verified_state_at(
                    cursor, head, deadline=deadline, allow_expired=allow_expired
                )
            connection.rollback()
            return head, state, crossref, policies
        except SqlRuntimeError:
            raise
        except (SqlSignatureError, ValueError, TypeError, KeyError):
            raise SqlRuntimeError("protected HQ runtime authority invalid") from None
        except Exception:
            raise SqlRuntimeError("protected HQ runtime authority unavailable") from None
        finally:
            connection.close()

    def load_frame_binding(self, *, expected_head: str, deadline=None) -> PrincipalBinding:
        """Resolve a frame's table from the *authenticated* principal and committed registry."""
        connection, deadline = self._open(deadline=deadline)
        try:
            binding = self.settings["runtime"]
            with connection.cursor() as cursor:
                if time.monotonic() >= deadline:
                    raise SqlRuntimeError("HQ runtime operation deadline exceeded")
                cursor.execute("SELECT CURRENT_USER(),DATABASE(),ACTIVE_BRANCH(),DOLT_VERSION()")
                principal, database, branch, version = cursor.fetchone()
                if (
                    not principal.startswith(binding["user"] + "@")
                    or database != binding["database"]
                    or branch != "main"
                    or version != "2.3.5"
                ):
                    raise SqlRuntimeError("HQ frame principal or runtime endpoint mismatch")
                principal_name = principal.split("@", 1)[0]
                cursor.execute("START TRANSACTION")
                cursor.execute("SELECT DOLT_HASHOF('HEAD')")
                head = cursor.fetchone()[0]
                if head != expected_head:
                    raise SqlRuntimeError("HQ authority changed during frame binding")
                cursor.execute(
                    "SELECT principal,frame_id,holder_identity,instance_ref,epoch,"
                    "inbox_table,signer_fingerprint FROM hq_principal_registry AS OF %s "
                    "WHERE principal=%s",
                    (head, principal_name),
                )
                rows = cursor.fetchall()
                if len(rows) != 1:
                    raise SqlRuntimeError("authenticated frame principal is not provisioned")
                selected = PrincipalBinding(*rows[0])
                if selected.inbox_table != inbox_table(principal_name, selected.epoch):
                    raise SqlRuntimeError("protected frame table routing changed")
            connection.rollback()
            if time.monotonic() >= deadline:
                raise SqlRuntimeError("HQ runtime operation deadline exceeded")
            return selected
        except SqlRuntimeError:
            raise
        except Exception:
            raise SqlRuntimeError("protected frame routing unavailable") from None
        finally:
            connection.close()

    def publish_inbox(
        self,
        kind: str,
        payload: dict,
        *,
        binding: PrincipalBinding,
        expected_head: str,
        request_id: str | None = None,
        deadline=None,
    ) -> tuple[str, str]:
        """Publish only into the authenticated principal's operator-routed live table."""
        if kind not in {"heartbeat", "registration", "hive_lease"}:
            raise SqlRuntimeError("unsupported frame evidence domain")
        if binding.inbox_table != inbox_table(binding.principal, binding.epoch):
            raise SqlRuntimeError("frame inbox identifier changed")
        body = canonical(payload)
        digest = hashlib.sha256(body).hexdigest()
        request_id = request_id or str(uuid.uuid4())
        try:
            if str(uuid.UUID(request_id)) != request_id:
                raise ValueError()
        except (TypeError, ValueError):
            raise SqlRuntimeError("invalid immutable frame request ID") from None
        connection, deadline = self._open(deadline=deadline)
        crossed_commit = False
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT CURRENT_USER(),DATABASE(),ACTIVE_BRANCH(),DOLT_VERSION()")
                principal, database, branch, version = cursor.fetchone()
                expected = self.settings["runtime"]
                if (
                    principal.split("@", 1)[0] != binding.principal
                    or not principal.startswith(expected["user"] + "@")
                    or database != expected["database"]
                    or branch != "main"
                    or version != "2.3.5"
                ):
                    raise SqlRuntimeError("frame inbox principal or endpoint mismatch")
                cursor.execute("START TRANSACTION")
                cursor.execute("SELECT DOLT_HASHOF('HEAD')")
                if cursor.fetchone()[0] != expected_head:
                    raise SqlRuntimeError("HQ authority changed before evidence publication")
                if time.monotonic() >= deadline:
                    raise SqlRuntimeError("HQ runtime operation deadline exceeded")
                cursor.execute(
                    f"INSERT INTO {binding.inbox_table} "
                    "(request_id,kind,payload,payload_sha256,created_at) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (request_id, kind, body, digest, self.clock()),
                )
                crossed_commit = True
                connection.commit()
            if time.monotonic() >= deadline:
                raise InboxUnknown(request_id, digest)
            return request_id, "sha256:" + digest
        except InboxUnknown:
            raise
        except SqlRuntimeError:
            if not crossed_commit:
                connection.rollback()
                raise
            raise InboxUnknown(request_id, digest) from None
        except Exception:
            if not crossed_commit:
                connection.rollback()
                raise SqlRuntimeError("frame evidence publication failed") from None
            raise InboxUnknown(request_id, digest) from None
        finally:
            connection.close()

    def read_public_result(
        self,
        request_id: str,
        *,
        request_sha256: str,
        principal: PrincipalBinding,
        audience: str,
        expected_revision: str,
        deadline=None,
    ) -> tuple[str, str | None] | None:
        """Read an accepted/rejected public result without private receipt/floor access."""
        connection, deadline = self._open(deadline=deadline)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT CURRENT_USER(),DATABASE(),ACTIVE_BRANCH(),DOLT_VERSION()")
                user, database, branch, version = cursor.fetchone()
                binding = self.settings["runtime"]
                if (
                    user.split("@", 1)[0] != principal.principal
                    or not user.startswith(binding["user"] + "@")
                    or database != binding["database"]
                    or branch != "main"
                    or version != "2.3.5"
                ):
                    raise SqlRuntimeError("public result principal or endpoint mismatch")
                cursor.execute(
                    "SELECT request_sha256,principal,frame_id,holder_identity,instance_ref,"
                    "epoch,audience,signer_fingerprint,expected_revision,result_revision,status "
                    "FROM hq_live_results WHERE request_id=%s",
                    (request_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    if time.monotonic() >= deadline:
                        raise SqlRuntimeDeadline("public result deadline exceeded")
                    return None
                if row[:9] != (
                    request_sha256,
                    principal.principal,
                    principal.frame_id,
                    principal.holder_identity,
                    principal.instance_ref,
                    principal.epoch,
                    audience,
                    principal.signer_fingerprint,
                    expected_revision,
                ):
                    raise SqlRuntimeError("public result does not match original signed request")
                if row[10] in {"accepted", "rejected"} and time.monotonic() >= deadline:
                    raise SqlRuntimeDeadline("public result state invalid or deadline exceeded")
                if row[10] not in {"accepted", "rejected"}:
                    raise SqlRuntimeError("public result state invalid or deadline exceeded")
                return row[10], row[9]
        except SqlRuntimeError:
            raise
        except Exception:
            if time.monotonic() >= deadline:
                raise SqlRuntimeDeadline("public result deadline exceeded") from None
            raise SqlRuntimeUnavailable("public result unavailable") from None
        finally:
            connection.close()
