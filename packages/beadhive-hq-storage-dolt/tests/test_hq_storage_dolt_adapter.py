"""Adapter-specific Dolt behaviour the fake cannot prove (hq-storage-port-adr, Consequences):
the receiver-compatible placement wire format, ignored live tables, server-stamped liveness,
error classification by phase, and the Dolt-only capabilities.

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
    Conflict,
    InvalidRequest,
    LivenessKind,
    RecordKind,
    StorageUnavailable,
    StreamKind,
    Unknown,
    new_operation_id,
    require_capability,
)
from beadhive_hq_storage_dolt import (
    DoltAccounts,
    DoltHistory,
    DoltHiveFence,
    DoltHqStorage,
    DoltMerge,
    FenceTable,
    MergeRefused,
    Role,
    error_code,
    placement_witness,
    unknown_reason,
)
from beadhive_hq_storage_dolt.contract import (
    DoltFenceHarness,
    DoltHqHarness,
    DoltSqlServer,
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
    first = fence.commit_bump(BumpRequest("frame-a", 1, "rev-a-1", new_operation_id()))
    second = fence.commit_bump(BumpRequest("frame-b", 2, "rev-b-2", new_operation_id()))
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
