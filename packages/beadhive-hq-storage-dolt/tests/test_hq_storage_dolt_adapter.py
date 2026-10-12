"""Adapter-specific Dolt behaviour the fake cannot prove (hq-storage-port-adr, Consequences):
the receiver-compatible placement wire format, ignored live tables, server-stamped liveness,
error classification by phase, the Dolt-only capabilities, and root's runtime tables
(``runtime_tables=True``, bh-xnn5p): the failover policy rows, the per-incarnation session and
evidence rows, and the atomic placement-plus-policy write.

Classification cases run hermetically over a scripted DB-API stub; the rest need a real
``dolt sql-server`` and self-skip without one.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from beadhive_hq_storage import (
    ABSENT,
    Applied,
    BumpRequest,
    CapabilityUnsupported,
    ChangeSet,
    Confirmed,
    Conflict,
    IntegrityViolation,
    InvalidRequest,
    LivenessKind,
    NotConfirmed,
    NotProvisioned,
    RecordKind,
    RecordWrite,
    Revision,
    StorageUnavailable,
    StreamKind,
    Unknown,
    WriterRow,
    new_operation_id,
    require_capability,
)
from beadhive_hq_storage_dolt import (
    CONFIG_TABLES,
    DoltAccounts,
    DoltConfigHistory,
    DoltHistory,
    DoltHiveFence,
    DoltHqStorage,
    DoltMerge,
    DoltRuntimeReads,
    FenceTable,
    HqRuntimeStatements,
    MergeRefused,
    Role,
    cause_token,
    config_change_set,
    config_document_kind,
    config_documents_digest,
    error_code,
    evidence_ddl,
    incarnation_key,
    incarnation_table,
    operation_revision,
    placement_seed_statement,
    placement_witness,
    policy_revision,
    session_ddl,
    unknown_reason,
)
from beadhive_hq_storage_dolt._fence import SqlConnectionFenceEngine
from beadhive_hq_storage_dolt._placement import (
    decode_placement,
    encode_placement,
    placement_witnessed,
)
from beadhive_hq_storage_dolt.contract import (
    FENCE_GUARDS,
    DoltFenceHarness,
    DoltHqHarness,
    DoltSqlServer,
    FaultPlan,
    _FaultingConnection,
)

# --- hermetic: classification by phase ------------------------------------------------------


class _Coded(Exception):
    pass


class _StubCursor:
    def __init__(self, script: _Script) -> None:
        self._script = script
        self.rowcount = 0
        self.description = None

    def execute(self, operation: str, parameters: Any = None) -> int:
        self._script.statements.append(operation)
        if operation.startswith(("UPDATE", "INSERT")):
            self.rowcount = 1
        return self.rowcount

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list:
        return []

    def close(self) -> None:
        pass


class _Script:
    def __init__(self, commit_error: BaseException | None = None) -> None:
        self.commit_error = commit_error
        self.statements: list[str] = []

    def connect(self, _role: Role) -> _Script:
        return self

    def cursor(self) -> _StubCursor:
        return _StubCursor(self)

    def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def _cas(store: DoltHqStorage) -> Any:
    return store.compare_and_set(
        RecordKind.AUTHORITY, "k", {"v": 1}, expected=ABSENT, operation=new_operation_id()
    )


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (TimeoutError("budget"), "deadline"),
        (_Coded(3024, "max_execution_time"), "deadline"),
        (_Coded(2013, "lost connection"), "transport"),
        (_Coded(2006, "gone away"), "transport"),
        (_Coded(1180, "error during commit"), "commit"),
        (_Coded(1105, "unclassified"), "commit"),
        (RuntimeError("no code at all"), "commit"),
    ],
)
def test_an_error_at_the_commit_point_is_unknown_with_its_reason(error, reason) -> None:
    script = _Script(commit_error=error)
    out = _cas(DoltHqStorage(script.connect))
    assert isinstance(out, Unknown) and out.reason == reason and out.expected is ABSENT
    assert unknown_reason(error) == reason


def test_a_serialization_failure_at_commit_is_a_conflict_not_an_error() -> None:
    script = _Script(commit_error=_Coded(1213, "serialization failure"))
    assert isinstance(_cas(DoltHqStorage(script.connect)), Conflict)


def test_root_deadline_predicate_classifies_its_codeless_error() -> None:
    error = ValueError("SQL operation deadline exceeded")
    script = _Script(commit_error=error)
    store = DoltHqStorage(script.connect, deadline=lambda exc: "deadline" in str(exc))
    out = _cas(store)
    assert isinstance(out, Unknown) and out.reason == "deadline"
    assert error_code(error) is None


def test_a_refused_connection_is_unavailable_and_validation_precedes_io() -> None:
    def refuse(_role: Role) -> Any:
        raise _Coded(2003, "cannot connect")

    store = DoltHqStorage(refuse)
    with pytest.raises(StorageUnavailable):
        _cas(store)
    with pytest.raises(InvalidRequest):  # refused before the factory is ever called
        store.compare_and_set(
            RecordKind.AUTHORITY, "", {"v": 1}, expected=ABSENT, operation=new_operation_id()
        )


def test_history_capability_reads_fence_tables_only() -> None:
    history = DoltHqStorage(_Script().connect).capability(DoltHistory)
    assert history is not None
    with pytest.raises(InvalidRequest):
        history.table_history("issues", at="0" * 32)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        FenceTable("issues")


def test_fence_has_no_accounts_capability() -> None:
    fence = DoltHiveFence(object(), guards=("g",))  # type: ignore[arg-type]
    assert fence.capability(DoltAccounts) is None
    with pytest.raises(CapabilityUnsupported):
        require_capability(fence, DoltAccounts)


# --- real Dolt ------------------------------------------------------------------------------

dolt = pytest.mark.skipif(shutil.which("dolt") is None, reason="dolt binary unavailable")


@pytest.fixture(scope="module")
def server() -> Iterator[DoltSqlServer]:
    if shutil.which("dolt") is None:
        pytest.skip("dolt binary unavailable")
    pymysql = pytest.importorskip("pymysql")
    base = Path(tempfile.mkdtemp(prefix="bh-hq-storage-dolt-adapter-"))
    try:
        with DoltSqlServer(base, pymysql.connect) as running:
            yield running
    finally:
        shutil.rmtree(base, ignore_errors=True)  # /tmp may be RAM-backed


def _head(server: DoltSqlServer, database: str) -> str:
    return str(server.sql(database, "SELECT DOLT_HASHOF('HEAD')")[0][0])


@dolt
@pytest.mark.integration
@pytest.mark.dolt_server
def test_placement_row_keeps_the_receiver_wire_format(server: DoltSqlServer) -> None:
    hq = DoltHqHarness(server)
    hq.provision(RecordKind.PLACEMENT, "bh", {"frame": "f1", "epoch": 1, "cause": "seed"})
    seen = hq.placements.read("bh")
    op = new_operation_id()
    out = hq.placements.compare_and_set(
        "bh", "f2", 2, expected=seen, cause="failover", operation=op
    )
    assert isinstance(out, Applied)
    ((revision, body, request_id, witness),) = server.sql(
        hq.database,
        "SELECT revision, lease_json, request_id, request_sha256 FROM hq_live_hive_leases "
        "WHERE prefix = 'bh'",
    )
    assert re.fullmatch(r"[0-9a-f]{64}", revision) and revision == out.revision
    assert (
        body
        == json.dumps(
            {"cause": "failover", "epoch": 2, "frame": "f2"}, separators=(",", ":")
        ).encode()
    )
    assert request_id == op  # the OperationId witness lives in the request id column
    assert witness == placement_witness("bh", revision, body)
    expected_witness = hashlib.sha256(
        json.dumps(
            {
                "domain": "beadhive/sql-placement/v1",
                "lease_sha256": hashlib.sha256(body).hexdigest(),
                "prefix": "bh",
                "revision": revision,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert witness == expected_witness


@dolt
@pytest.mark.integration
@pytest.mark.dolt_server
def test_live_writes_never_commit_and_triggers_stamp_server_time(server: DoltSqlServer) -> None:
    hq = DoltHqHarness(server)
    head = _head(server, hq.database)
    hq.records.compare_and_set(
        RecordKind.AUTHORITY, "a", {"v": 1}, expected=ABSENT, operation=new_operation_id()
    )
    hq.log.append(StreamKind.RECEIPT, "f/1/1", {"seen": 1}, operation=new_operation_id())
    stamp = hq.liveness.renew(LivenessKind.SESSION, "frame-a", {"pid": 1})
    assert _head(server, hq.database) == head
    pending = server.sql(hq.database, "SELECT table_name FROM dolt_status")
    assert not [name for (name, *_rest) in pending if str(name).startswith("hq_live")]
    # A caller-supplied stamp is overwritten by the server clock (the trigger is kept).
    server.sql(
        hq.database,
        "UPDATE hq_live_liveness SET renewed_at = '2099-01-01' WHERE live_key = 'frame-a'",
    )
    restamped = hq.liveness.stamped_at(LivenessKind.SESSION, "frame-a")
    assert restamped is not None and stamp <= restamped <= hq.liveness.now()
    assert hq.liveness.ages(LivenessKind.SESSION)["frame-a"] >= timedelta(0)
    triggers = require_capability(hq.store, DoltAccounts).triggers()
    assert {"hq_live_liveness_INS", "hq_live_liveness_UPD"} <= set(triggers)
    assert all("UTC_TIMESTAMP(6)" in body for body in triggers.values())


@dolt
@pytest.mark.integration
@pytest.mark.dolt_server
def test_accounts_create_grant_and_report(server: DoltSqlServer) -> None:
    hq = DoltHqHarness(server)
    accounts = require_capability(hq.store, DoltAccounts)
    user = "frame_" + hq.database
    accounts.create_user(user, "%", "fixture-only")
    try:
        accounts.grant(["SELECT", "UPDATE"], "hq_live_liveness", user, "%")
        grants = " ".join(accounts.grants(user, "%"))
        assert "hq_live_liveness" in grants and "UPDATE" in grants
        with pytest.raises(InvalidRequest):
            accounts.grant(["DROP"], "hq_live_liveness", user, "%")
        with pytest.raises(InvalidRequest):
            accounts.create_user("bad name", "%", "x")
    finally:
        accounts.drop_user(user, "%")


@dolt
@pytest.mark.integration
@pytest.mark.dolt_server
def test_history_and_merge_capabilities_over_a_fence(server: DoltSqlServer) -> None:
    hive = DoltFenceHarness(server)
    fence: DoltHiveFence = hive.fence_adapter
    bumped = [
        fence.commit_bump(BumpRequest(frame, epoch, f"rev-{epoch}", new_operation_id()))
        for frame, epoch in (("frame-a", 1), ("frame-b", 2))
    ]
    assert all(isinstance(out, Applied) for out in bumped)
    first, second = (out.revision for out in bumped)  # type: ignore[union-attr]
    history = require_capability(fence, DoltHistory)
    assert history.head() == second
    log = history.log(second, limit=2)
    assert [c.commit for c in log] == [second, first] and log[0].parents == (first,)
    assert log[0].message == "bh: adopt frame-b@2"
    assert history.is_ancestor(first, second) and not history.is_ancestor(second, first)
    epochs = sorted(
        int(r.row["epoch"]) for r in history.table_history(FenceTable.BH_WRITER, at=second)
    )
    assert epochs == [0, 1, 2]

    merge = require_capability(fence, DoltMerge)
    merge.reset_hard(first)
    assert fence.writer() is not None and fence.writer().epoch == 1  # type: ignore[union-attr]
    server.sql(hive.database, f"CALL DOLT_BRANCH('side', '{second}')")
    assert merge.preview_conflicts("main", "side") == ()
    merged = merge.merge("side", "merge side")
    assert history.head() == merged and fence.writer().epoch == 2  # type: ignore[union-attr]
    server.sql(hive.database, "CALL DOLT_BRANCH('clash', 'HEAD~1')")
    server.sql(
        hive.database,
        "CALL DOLT_CHECKOUT('clash')",
        "UPDATE issues SET status = 'x' WHERE id = 'bd-3'",
        "CALL DOLT_COMMIT('-Am', 'clash')",
    )
    server.sql(
        hive.database,
        "UPDATE issues SET status = 'y' WHERE id = 'bd-3'",
        "CALL DOLT_COMMIT('-Am', 'main side')",
    )
    assert merge.preview_conflicts("main", "clash") == ("issues",)
    with pytest.raises(MergeRefused):
        merge.merge("clash", "refused")


# --- the receiver placement carrier (bh-sqfvq) ----------------------------------------------

_LEASE = {
    "host_id": "host-a",
    "label": "frame-a",
    "epoch": 2,
    "adopted_at": "2026-10-12T00:00:00Z",
    "expires_at": "2026-10-13T00:00:00Z",
}
_AUTHORITY = {"frame_id": "frame-a", "holder_identity": "host-a"}


def _carrier(**extra: Any) -> dict[str, Any]:
    return {"authority": _AUTHORITY, "lease": _LEASE, **extra}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def test_a_caused_carrier_write_encodes_onto_the_receiver_columns() -> None:
    op = new_operation_id()
    row = encode_placement("ah", _carrier(cause="failover", director=True), operation=op)
    assert row.lease_json == _canonical({"authority": _AUTHORITY, "lease": _LEASE})
    assert row.revision == operation_revision("ah", row.lease_json, op)
    assert re.fullmatch(r"[0-9a-f]{64}", row.revision)
    assert row.request_id == cause_token("ah", row.revision, "failover")
    assert row.request_sha256 == placement_witness("ah", row.revision, row.lease_json)
    body = decode_placement("ah", row.revision, row.lease_json, row.request_id, row.request_sha256)
    assert body == _carrier(cause="failover", director=True)
    assert placement_witnessed(
        "ah", row.revision, row.lease_json, row.request_id, row.request_sha256, op
    )
    assert not placement_witnessed(
        "ah", row.revision, row.lease_json, row.request_id, row.request_sha256, new_operation_id()
    )


def test_an_uncaused_carrier_write_witnesses_its_operation_in_request_id() -> None:
    op = new_operation_id()
    first = encode_placement("ah", _carrier(), operation=op)
    again = encode_placement("ah", _carrier(), operation=op)
    assert first.request_id == op and first.revision != again.revision  # fresh every write
    body = decode_placement("ah", first.revision, first.lease_json, op, first.request_sha256)
    assert body == _carrier(director=True)
    # A receiver row (a signed-request digest, not the witness) is not director-written, and a
    # cause token in its request_id is never trusted.
    token = cause_token("ah", first.revision, "failover")
    assert decode_placement("ah", first.revision, first.lease_json, token, "e" * 64) == _carrier(
        director=False
    )


def test_non_carrier_and_malformed_placement_rows_decode_without_a_director() -> None:
    generic = encode_placement("ah", {"frame": "f", "epoch": 1}, operation=new_operation_id())
    assert generic.lease_json == _canonical({"epoch": 1, "frame": "f"})
    pretty = json.dumps({"authority": _AUTHORITY, "lease": _LEASE}, indent=1).encode()
    assert "director" not in decode_placement("ah", "r", pretty, "x", "y")  # not canonical
    assert decode_placement("ah", "r", b"\xff not json", "x", "y") == {}
    assert decode_placement("ah", "r", b"[1]", "x", "y") == {}
    for bad in (_carrier(director=False), _carrier(cause="forced"), _carrier(cause=None)):
        with pytest.raises(InvalidRequest):
            encode_placement("ah", bad, operation=new_operation_id())


def test_the_seed_statement_inserts_a_witnessed_tombstone_row() -> None:
    raw = _canonical({"authority": {}, "lease": _LEASE})
    sql, params = placement_seed_statement("ah", "r" * 64, raw, "id")
    assert sql == (
        "INSERT INTO hq_live_hive_leases (prefix,revision,lease_json,request_id,"
        "request_sha256) VALUES (%s,%s,%s,%s,%s)"
    )
    assert params == ("ah", "r" * 64, raw, "id", placement_witness("ah", "r" * 64, raw))


def test_the_runtime_reads_capability_is_offered_and_wraps_a_cursor_once() -> None:
    reads = DoltHqStorage(_Script().connect).capability(DoltRuntimeReads)
    assert isinstance(reads, DoltRuntimeReads)
    surface = HqRuntimeStatements.of(object())
    assert HqRuntimeStatements.of(surface) is surface


def test_a_policy_body_is_whole_seconds_and_a_session_key_names_one_incarnation() -> None:
    store = DoltHqStorage(_Script().connect, runtime_tables=True)
    for body in ({"executor": True}, {"executor": 1.5}, {"": 1}, {"x" * 33: 1}):
        with pytest.raises(InvalidRequest):
            store.compare_and_set(
                RecordKind.FAILOVER_POLICY, "ah", body, expected=ABSENT, operation=_op()
            )
    for key in ("frame_a", "frame_a/x", "Frame/1", "a/1/2"):
        with pytest.raises(InvalidRequest):
            store.renew(LivenessKind.SESSION, key)
    assert incarnation_key("frame_a", 3) == "frame_a/3"
    assert incarnation_table("frame_a", 3, "session") == "frame_frame_a_3_session"


def _op() -> Any:
    return new_operation_id()


@dolt
@pytest.mark.integration
@pytest.mark.dolt_server
def test_the_carrier_round_trips_on_the_receiver_row(server: DoltSqlServer) -> None:
    hq = DoltHqHarness(server)
    seeded = hq.provision(RecordKind.PLACEMENT, "ah", {"authority": {}, "lease": _LEASE})
    read = hq.store.read(RecordKind.PLACEMENT, "ah")
    assert read is not None and read.body == {"authority": {}, "lease": _LEASE, "director": True}
    op = new_operation_id()
    out = hq.store.compare_and_set(
        RecordKind.PLACEMENT,
        "ah",
        _carrier(cause="failover", director=True),
        expected=seeded,
        operation=op,
    )
    assert isinstance(out, Applied)
    ((revision, body, request_id, witness),) = server.sql(
        hq.database,
        "SELECT revision, lease_json, request_id, request_sha256 FROM hq_live_hive_leases "
        "WHERE prefix = 'ah'",
    )
    assert revision == out.revision and body == _canonical(
        {"authority": _AUTHORITY, "lease": _LEASE}
    )
    assert request_id == cause_token("ah", revision, "failover")  # what a 0.23 frame reads
    assert witness == placement_witness("ah", revision, body)
    read = hq.store.read(RecordKind.PLACEMENT, "ah")
    assert read is not None and read.body == _carrier(cause="failover", director=True)
    found = hq.store.lookup(RecordKind.PLACEMENT, "ah", op)
    assert isinstance(found, Confirmed) and found.revision == revision
    # The receiver's own CAS takes the row over: no witness, no cause, and op no longer visible.
    server.sql(
        hq.database,
        "UPDATE hq_live_hive_leases SET revision = %s, request_id = %s, request_sha256 = %s "
        "WHERE prefix = 'ah'",
        params=("f" * 64, "00000000-0000-4000-8000-000000000001", "e" * 64),
    )
    read = hq.store.read(RecordKind.PLACEMENT, "ah")
    assert read is not None and read.body == _carrier(director=False)
    assert isinstance(hq.store.lookup(RecordKind.PLACEMENT, "ah", op), NotConfirmed)
    release = new_operation_id()
    out = hq.store.compare_and_set(
        RecordKind.PLACEMENT, "ah", _carrier(), expected=Revision("f" * 64), operation=release
    )
    assert isinstance(out, Applied)
    assert server.sql(
        hq.database, "SELECT request_id FROM hq_live_hive_leases WHERE prefix = 'ah'"
    ) == [(release,)]
    assert isinstance(hq.store.lookup(RecordKind.PLACEMENT, "ah", release), Confirmed)


@dolt
@pytest.mark.integration
@pytest.mark.dolt_server
def test_a_principal_mismatch_is_refused_before_anything_is_sent(server: DoltSqlServer) -> None:
    hq = DoltHqHarness(server)
    hq.provision(RecordKind.PLACEMENT, "ah", {"authority": {}, "lease": _LEASE})
    for principal in (("director", hq.database), ("root", "other_db")):
        wrong = DoltHqStorage(hq._raw_factory, principals={Role.READER: principal})
        with pytest.raises(StorageUnavailable, match="principal or endpoint mismatch"):
            wrong.read(RecordKind.PLACEMENT, "ah")
    right = DoltHqStorage(hq._raw_factory, principals={Role.READER: ("root", hq.database)})
    assert right.read(RecordKind.PLACEMENT, "ah") is not None


@dolt
@pytest.mark.integration
@pytest.mark.dolt_server
def test_the_runtime_read_is_one_read_only_transaction(server: DoltSqlServer) -> None:
    hq = DoltHqHarness(server)
    hq.provision(RecordKind.PLACEMENT, "ah", {"authority": {}, "lease": _LEASE})
    reads = require_capability(hq.store, DoltRuntimeReads)
    with reads.pinned() as statements:
        assert statements.head() == _head(server, hq.database)
        assert statements.hive_lease("ah") is not None
        assert statements.tables_present(("hq_live_hive_leases", "nope")) == {"hq_live_hive_leases"}
        assert not hasattr(statements, "cursor")  # typed statements only, no raw cursor


# --- root's runtime tables (runtime_tables=True, bh-xnn5p) -----------------------------------

_POLICY_DDL = (
    "CREATE TABLE hq_live_failover_policy ("
    "scope VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL, "
    "setting VARCHAR(32) NOT NULL, seconds BIGINT NOT NULL, "
    "PRIMARY KEY (scope, setting))"
)


def _runtime(hq: DoltHqHarness) -> DoltHqStorage:
    """The director's mapping over the harness database (faults armed through the harness)."""
    return DoltHqStorage(hq._factory, runtime_tables=True)


def _policy_rows(server: DoltSqlServer, hq: DoltHqHarness) -> list[tuple]:
    return sorted(
        tuple(row)
        for row in server.sql(
            hq.database, "SELECT scope, setting, seconds FROM hq_live_failover_policy"
        )
    )


@dolt
@pytest.mark.integration
@pytest.mark.dolt_server
def test_the_failover_policy_maps_column_for_column_onto_roots_table(
    server: DoltSqlServer,
) -> None:
    hq = DoltHqHarness(server)
    store = _runtime(hq)
    with pytest.raises(NotProvisioned):
        store.read_many(RecordKind.FAILOVER_POLICY)
    server.sql(hq.database, _POLICY_DDL)
    assert store.read(RecordKind.FAILOVER_POLICY, "ah") is None
    head = _head(server, hq.database)
    op = _op()
    out = store.compare_and_set(
        RecordKind.FAILOVER_POLICY,
        "ah",
        {"executor": 4500, "transient": 2400},
        expected=ABSENT,
        operation=op,
    )
    assert out == Applied(policy_revision("ah", {"executor": 4500, "transient": 2400}), op)
    assert _policy_rows(server, hq) == [("ah", "executor", 4500), ("ah", "transient", 2400)]
    assert _head(server, hq.database) == head  # an ignored live table: never a commit
    read = store.read(RecordKind.FAILOVER_POLICY, "ah")
    assert read is not None and read.revision == out.revision
    # A row written directly (a pre-port director, or the operator) reads the same way.
    server.sql(hq.database, "INSERT INTO hq_live_failover_policy VALUES ('*', 'executor', 5400)")
    assert [v.key for v in store.read_many(RecordKind.FAILOVER_POLICY)] == ["*", "ah"]
    stale = store.compare_and_set(
        RecordKind.FAILOVER_POLICY, "*", {"executor": 6000}, expected=ABSENT, operation=_op()
    )
    assert isinstance(stale, Conflict) and stale.current is not None
    assert stale.current.body == {"executor": 5400}
    cleared = store.compare_and_set(
        RecordKind.FAILOVER_POLICY,
        "ah",
        {"executor": 4500},
        expected=read.revision,
        operation=_op(),
    )
    assert isinstance(cleared, Applied)
    assert _policy_rows(server, hq) == [("*", "executor", 5400), ("ah", "executor", 4500)]
    # No witness column: a lookup can never confirm, and NotConfirmed authorises nothing.
    assert store.lookup(RecordKind.FAILOVER_POLICY, "ah", op) == NotConfirmed(op)


@dolt
@pytest.mark.integration
@pytest.mark.dolt_server
@pytest.mark.parametrize("applied", [True, False])
def test_the_placement_and_its_policy_are_one_atomic_write(
    server: DoltSqlServer, applied: bool
) -> None:
    hq = DoltHqHarness(server)
    server.sql(hq.database, _POLICY_DDL)
    store = _runtime(hq)
    seeded = hq.provision(RecordKind.PLACEMENT, "ah", {"authority": {}, "lease": _LEASE})
    policy = RecordWrite(RecordKind.FAILOVER_POLICY, "ah", {"executor": 4500}, ABSENT)
    lost = store.compare_and_set_all(
        [RecordWrite(RecordKind.PLACEMENT, "ah", _carrier(), Revision("0" * 64)), policy],
        operation=_op(),
    )
    assert isinstance(lost, Conflict) and lost.current is not None
    assert lost.current.revision == seeded
    assert _policy_rows(server, hq) == []  # the policy never lands without the placement
    op = _op()
    hq.faults.arm_unknown("compare_and_set_all", "commit", applied=applied)
    hq.faults.active = "compare_and_set_all"
    try:
        out = store.compare_and_set_all(
            [RecordWrite(RecordKind.PLACEMENT, "ah", _carrier(), seeded), policy], operation=op
        )
    finally:
        hq.faults.active = None
    assert out == Unknown(operation=op, expected=seeded, reason="commit")
    found = store.lookup(RecordKind.PLACEMENT, "ah", op)  # the placement carries the witness
    assert isinstance(found, Confirmed) is applied
    assert _policy_rows(server, hq) == ([("ah", "executor", 4500)] if applied else [])


@dolt
@pytest.mark.integration
@pytest.mark.dolt_server
def test_incarnation_liveness_is_the_provisioned_rows_stamped_by_the_server(
    server: DoltSqlServer,
) -> None:
    hq = DoltHqHarness(server)
    store = _runtime(hq)
    key = incarnation_key("frame_a", 1)
    with pytest.raises(NotProvisioned):  # never an INSERT: the operator provisions the row
        store.renew(LivenessKind.SESSION, key)
    assert (
        store.ages(LivenessKind.SESSION) == {}
        and store.stamped_at(LivenessKind.SESSION, key) is None
    )
    for table, ddl in (
        (incarnation_table("frame_a", 1, "session"), session_ddl),
        (incarnation_table("frame_a", 1, "evidence"), evidence_ddl),
    ):
        server.sql(hq.database, *ddl(table, 1))
    # Provisioned but never renewed: not observable (no age), not "infinitely stale".
    assert store.ages(LivenessKind.SESSION, [key, "frame_b/1"]) == {}
    before = store.now()
    stamp = store.renew(LivenessKind.SESSION, key)
    after = store.now()
    assert before <= stamp <= after  # ordering only: no elapsed-time assertion
    assert store.stamped_at(LivenessKind.SESSION, key) == stamp
    ages = store.ages(LivenessKind.SESSION)
    assert set(ages) == {key} and ages[key] >= timedelta(0)
    assert set(store.ages(LivenessKind.SESSION, [key, "frame_b/1"])) == {key}
    # A frame cannot freshen its own row: the trigger overwrites whatever it sends.
    session = incarnation_table("frame_a", 1, "session")
    server.sql(hq.database, f"UPDATE {session} SET renewed_at = '2099-01-01' WHERE id = 1")
    restamped = store.stamped_at(LivenessKind.SESSION, key)
    assert restamped is not None and restamped <= store.now()
    evidence = {
        "epoch": 1,
        "release_id": "r",
        "release_digest": "sha256:" + "1" * 64,
        "profile": "p",
        "status": "conformant",
        "report_digest": "",
    }
    with pytest.raises(InvalidRequest):
        store.renew(LivenessKind.EVIDENCE, key, {"status": "conformant"})
    measured = store.renew(LivenessKind.EVIDENCE, key, evidence)
    assert store.stamped_at(LivenessKind.EVIDENCE, key) == measured
    ((status,),) = server.sql(
        hq.database, f"SELECT status FROM {incarnation_table('frame_a', 1, 'evidence')}"
    )
    assert status == "conformant"


# --- the HQ config publication (bh-x7xtc) ---------------------------------------------------

_CONFIG_SCHEMA = (
    "CREATE TABLE hq_config_meta (singleton_id TINYINT NOT NULL PRIMARY KEY, "
    "schema_version INT NOT NULL, backend_identity VARCHAR(128) NOT NULL, "
    "generation VARCHAR(128) NOT NULL, publication_sequence BIGINT UNSIGNED NOT NULL, "
    "publication_id CHAR(36) NOT NULL, documents_sha256 CHAR(64) NOT NULL, "
    "document_count INT UNSIGNED NOT NULL, initial_attempt_nonce CHAR(36) NOT NULL)",
    "CREATE TABLE hq_config_documents (path VARCHAR(512) CHARACTER SET utf8mb4 "
    "COLLATE utf8mb4_bin NOT NULL PRIMARY KEY, ordinal INT UNSIGNED NOT NULL UNIQUE, "
    "kind VARCHAR(32) NOT NULL, content LONGBLOB NOT NULL, content_sha256 CHAR(64) NOT NULL)",
    "CREATE TABLE hq_config_publications (publication_id CHAR(36) NOT NULL PRIMARY KEY, "
    "publication_sequence BIGINT UNSIGNED NOT NULL UNIQUE, generation VARCHAR(128) NOT NULL, "
    "expected_parent_revision VARCHAR(128) NOT NULL, documents_sha256 CHAR(64) NOT NULL, "
    "document_count INT UNSIGNED NOT NULL)",
    "CALL DOLT_ADD('hq_config_meta', 'hq_config_documents', 'hq_config_publications')",
    "CALL DOLT_COMMIT('-m', 'schema', '--author', 'Fixture <fixture@example.invalid>')",
)
_SEED_FLEET = b"hq:\n  mode: dolt-server\n"
_PUBLISHED = (
    ("fleet.yaml", b"hq:\n  mode: dolt-server\nmanaged_repos: []\n"),
    ("hosts/a.yaml", b"name: a\n"),
    ("workspace.toml", b"[x]\n"),
)


def test_the_config_change_set_is_the_three_table_allowlist_in_order() -> None:
    changes = config_change_set(2, _PUBLISHED)
    assert changes.tables == frozenset(CONFIG_TABLES)
    assert changes.documents[0] == ("hq_config_meta/schema_version", b"2")
    assert [name for name, _ in changes.documents[1:]] == [
        "hq_config_documents/fleet.yaml",
        "hq_config_documents/hosts/a.yaml",
        "hq_config_documents/workspace.toml",
    ]
    assert [config_document_kind(path) for path, _ in _PUBLISHED] == ["fleet", "host", "workspace"]


@pytest.mark.parametrize(
    "changes",
    [
        ChangeSet((("hq_config_documents/fleet.yaml", b"x"),), frozenset(CONFIG_TABLES)),
        ChangeSet((("hq_config_meta/schema_version", b"1"),), frozenset(CONFIG_TABLES)),
        ChangeSet(
            (("hq_config_meta/schema_version", b"1"), ("hq_config_documents/fleet.yaml", b"x")),
            frozenset({"hq_config_documents", "hq_config_meta"}),
        ),
        ChangeSet(
            (("hq_config_meta/schema_version", b"1"), ("hq_config_documents/nope.txt", b"x")),
            frozenset(CONFIG_TABLES),
        ),
        ChangeSet(
            (
                ("hq_config_meta/schema_version", b"1"),
                ("hq_config_documents/fleet.yaml", b"x"),
                ("hq_config_documents/fleet.yaml", b"y"),
            ),
            frozenset(CONFIG_TABLES),
        ),
    ],
)
def test_a_malformed_config_change_set_is_refused_before_io(changes: ChangeSet) -> None:
    script = _Script()
    with pytest.raises(InvalidRequest):
        DoltConfigHistory(script.connect).publish(
            changes,
            expected_head=Revision("h"),
            operation=new_operation_id(),
            author="a",
            message="m",
        )
    assert script.statements == []


class _ConfigDb:
    """A fresh HQ config database seeded with one publication, as root's seed leaves it."""

    def __init__(self, server: DoltSqlServer, faults: Any = None) -> None:
        self.server = server
        self.database = server.fresh_name("cfg")
        server.sql(None, f"CREATE DATABASE {self.database}")
        server.sql(self.database, *_CONFIG_SCHEMA)
        schema = _head(server, self.database)
        digest = config_documents_digest((("fleet.yaml", _SEED_FLEET),))
        seed = "00000000-0000-4000-8000-000000000001"
        server.sql(
            self.database,
            "INSERT INTO hq_config_meta VALUES (1, 3, 'backend', 'gen-1', 1, "
            f"'{seed}', '{digest}', 1, '00000000-0000-4000-8000-0000000000aa')",
            "INSERT INTO hq_config_documents VALUES ('fleet.yaml', 0, 'fleet', "
            f"'{_SEED_FLEET.decode()}', '{hashlib.sha256(_SEED_FLEET).hexdigest()}')",
            f"INSERT INTO hq_config_publications VALUES ('{seed}', 1, 'gen-1', '{schema}', "
            f"'{digest}', 1)",
            "CALL DOLT_ADD('hq_config_meta', 'hq_config_documents', 'hq_config_publications')",
            "CALL DOLT_COMMIT('-m', 'seed', '--author', 'Fixture <fixture@example.invalid>')",
        )
        self.faults = faults
        self.history = DoltConfigHistory(self._factory)

    def _factory(self, _role: Role) -> Any:
        connection = self.server.connect(self.database, autocommit=False)
        if self.faults is not None:
            return _FaultingConnection(connection, self.faults)
        return connection

    def head(self) -> str:
        return _head(self.server, self.database)

    def sql(self, *statements: str, params: Any = ()) -> list:
        return self.server.sql(self.database, *statements, params=params)


def _publish(db: _ConfigDb, expected: str, op: Any = None) -> Any:
    return db.history.publish(
        config_change_set(2, _PUBLISHED),
        expected_head=Revision(expected),
        operation=op or new_operation_id(),
        author="HQ operator <hq-operator@localhost>",
        message="HQ config publication x",
    )


class _FailingOn:
    """Connection wrapper whose cursors raise on any statement containing ``needle``."""

    def __init__(self, inner: Any, needle: str) -> None:
        self._inner, self._needle = inner, needle

    def cursor(self) -> Any:
        inner, needle = self._inner.cursor(), self._needle

        class Cursor:
            def execute(self, operation: str, parameters: Any = None) -> Any:
                if needle in operation:
                    raise _Coded(2013, "injected: lost connection on the readback")
                return inner.execute(operation, parameters)

            def __getattr__(self, name: str) -> Any:
                return getattr(inner, name)

        return Cursor()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@dolt
@pytest.mark.integration
@pytest.mark.dolt_server
def test_a_config_publication_writes_roots_three_tables_in_one_commit(
    server: DoltSqlServer,
) -> None:
    db = _ConfigDb(server)
    parent = db.head()
    assert db.history.head() == parent
    op = new_operation_id()
    out = _publish(db, parent, op)
    assert isinstance(out, Applied) and out.revision == db.head() != parent
    digest = config_documents_digest(_PUBLISHED)
    assert db.sql(
        "SELECT schema_version, backend_identity, generation, publication_sequence, "
        "publication_id, documents_sha256, document_count, initial_attempt_nonce "
        "FROM hq_config_meta"
    ) == [(2, "backend", "gen-1", 2, op, digest, 3, "00000000-0000-4000-8000-0000000000aa")]
    assert db.sql(
        "SELECT path, ordinal, kind, content, content_sha256 FROM hq_config_documents "
        "ORDER BY ordinal"
    ) == [
        (path, ordinal, config_document_kind(path), content, hashlib.sha256(content).hexdigest())
        for ordinal, (path, content) in enumerate(_PUBLISHED)
    ]
    assert db.sql(
        "SELECT publication_sequence, generation, expected_parent_revision, documents_sha256, "
        "document_count FROM hq_config_publications WHERE publication_id = %s",
        params=(op,),
    ) == [(2, "gen-1", parent, digest, 3)]
    assert db.sql(
        "SELECT parent_hash FROM dolt_commit_ancestors WHERE commit_hash = %s",
        params=(out.revision,),
    ) == [(parent,)]
    ((message, committer),) = db.sql(
        "SELECT message, CONCAT(committer, ' <', email, '>') FROM dolt_log LIMIT 1"
    )
    assert (message, committer) == (
        "HQ config publication x",
        "HQ operator <hq-operator@localhost>",
    )
    assert db.sql("SELECT * FROM dolt_status") == []
    assert db.history.read_at(out.revision) == list(config_change_set(2, _PUBLISHED).documents)

    found = db.history.lookup_publication(op, expected_head=Revision(parent))
    assert isinstance(found, Confirmed) and found.revision == out.revision
    assert isinstance(
        db.history.lookup_publication(op, expected_head=Revision(out.revision)), NotConfirmed
    )
    assert isinstance(
        db.history.lookup_publication(new_operation_id(), expected_head=Revision(parent)),
        NotConfirmed,
    )
    with db.history.consistent_read() as token:
        assert (token.backend_identity, token.generation, token.revision) == (
            "backend",
            "gen-1",
            out.revision,
        )
    with pytest.raises(InvalidRequest):  # the witness is immutable: never reuse its id
        _publish(db, out.revision, op)
    # A later publication: the original stays confirmed against its original parent, at the
    # head where its witness is still visible.
    later = _publish(db, out.revision)
    assert isinstance(later, Applied)
    found = db.history.lookup_publication(op, expected_head=Revision(parent))
    assert isinstance(found, Confirmed) and found.revision == later.revision


@dolt
@pytest.mark.integration
@pytest.mark.dolt_server
def test_a_config_publication_refuses_a_stale_parent_a_dirty_tree_and_an_extra_table(
    server: DoltSqlServer,
) -> None:
    db = _ConfigDb(server)
    parent = db.head()
    assert isinstance(_publish(db, "0" * 32), Conflict)
    db.sql("UPDATE hq_config_documents SET content = 'dirty' WHERE path = 'fleet.yaml'")
    with pytest.raises(IntegrityViolation, match="working tree is dirty"):
        _publish(db, parent)
    db.sql("CALL DOLT_RESET('--hard')")
    db.sql("CREATE TABLE stray (id INT PRIMARY KEY)")
    db.sql("CALL DOLT_ADD('stray')", "CALL DOLT_COMMIT('-m', 'stray', '--author', 'F <f@x>')")
    with pytest.raises(NotProvisioned, match="allowlist"):
        _publish(db, db.head())
    assert db.sql("SELECT publication_sequence FROM hq_config_meta") == [(1,)]


@dolt
@pytest.mark.integration
@pytest.mark.dolt_server
@pytest.mark.parametrize("applied", [True, False])
def test_a_lost_config_commit_acknowledgment_is_unknown_and_its_lookup_tells(
    server: DoltSqlServer, applied: bool
) -> None:
    plan = FaultPlan()
    db = _ConfigDb(server, plan)
    parent = db.head()
    op = new_operation_id()
    plan.active = "publish"
    plan.arm_unknown("publish", "transport", applied=applied)
    out = _publish(db, parent, op)
    plan.active = None
    assert out == Unknown(operation=op, expected=Revision(parent), reason="transport")
    found = db.history.lookup_publication(op, expected_head=Revision(parent))
    assert isinstance(found, Confirmed if applied else NotConfirmed)
    assert (db.head() != parent) is applied


@dolt
@pytest.mark.integration
@pytest.mark.dolt_server
def test_a_failed_post_commit_parent_readback_is_unknown_then_confirmed(
    server: DoltSqlServer,
) -> None:
    from beadhive_hq_storage import ChangeSet

    hq = DoltHqHarness(server)
    failing = DoltHqStorage(lambda role: _FailingOn(hq._raw_factory(role), "dolt_commit_ancestors"))
    head = failing.head()
    operation = new_operation_id()
    out = failing.publish(
        ChangeSet((("cfg/a", b"1"),), frozenset({"cfg"})),
        expected_head=head,
        operation=operation,
        author="Tester <t@example.com>",
        message="post-commit readback fails",
    )
    assert isinstance(out, Unknown)
    assert out.operation == operation
    assert out.expected == head
    assert out.reason == "transport"
    assert hq.store.head() != head  # the commit really landed
    found = hq.store.lookup_publication(operation, expected_head=head)
    assert isinstance(found, Confirmed)


@dolt
@pytest.mark.integration
@pytest.mark.dolt_server
def test_a_config_publication_issues_no_post_commit_read_that_could_misclassify(
    server: DoltSqlServer,
) -> None:
    db = _ConfigDb(server)
    parent = db.head()
    db.history = DoltConfigHistory(
        lambda role: _FailingOn(db._factory(role), "dolt_commit_ancestors")
    )
    op = new_operation_id()
    out = _publish(db, parent, op)
    assert isinstance(out, Applied)  # never StorageUnavailable once DOLT_COMMIT has landed
    assert isinstance(db.history.lookup_publication(op, expected_head=Revision(parent)), Confirmed)


@dolt
@pytest.mark.integration
@pytest.mark.dolt_server
def test_a_failed_post_commit_head_read_in_commit_bump_is_unknown_then_confirmed(
    server: DoltSqlServer,
) -> None:
    """bh-xxkf7.6: the bump commits, then the head read after it loses the connection. That
    is ``Unknown`` (never ``StorageUnavailable``, which would mean nothing was committed);
    re-issuing the bump would hit the epoch floor; a push plus ``lookup_bump`` under the same
    operation resolves it."""
    hive = DoltFenceHarness(server)

    def raw() -> Any:
        return server.connect(hive.database)

    class _ReadbackLost(SqlConnectionFenceEngine):
        def commit(self, message: str) -> bool:
            committed = super().commit(message)
            self._connect = lambda: _FailingOn(raw(), "DOLT_HASHOF('HEAD')")
            return committed

    fence = DoltHiveFence(_ReadbackLost(raw), guards=FENCE_GUARDS)
    before = Revision(_head(server, hive.database))
    bump = BumpRequest("frame-a", 1, "rev-a-1", new_operation_id())
    out = fence.commit_bump(bump)
    assert isinstance(out, Unknown)
    assert (out.operation, out.expected, out.reason) == (bump.operation, before, "transport")
    assert _head(server, hive.database) != before  # the bump really committed
    healthy = DoltHiveFence(SqlConnectionFenceEngine(raw), guards=FENCE_GUARDS)
    assert healthy.writer() == WriterRow("frame-a", 1, "rev-a-1")
    with pytest.raises(IntegrityViolation):  # why the bump must never be re-issued
        healthy.commit_bump(bump)
    assert healthy.lookup_bump("frame-a", 1, operation=bump.operation) == NotConfirmed(
        bump.operation
    )
    assert isinstance(healthy.push(), Applied)
    found = healthy.lookup_bump("frame-a", 1, operation=bump.operation)
    assert isinstance(found, Confirmed) and found.revision == _head(server, hive.database)
