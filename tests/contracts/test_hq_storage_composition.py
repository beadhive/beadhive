"""Root composition binds the Dolt adapter to the HQ storage port (hq-storage-port-adr section 8).

A thin check, not a third run of the contract suite: root's composition yields objects that
satisfy the port protocols, it wires root's PyMySQL transport (``hq_sql_transport.connect``)
into the adapter's connection factory, and the adapter's wire encodings match root's own (the
placement witness a receiver and director use to recognise a director row, the placement cause
a frame reads, the operator's seed statement, and the fence bump). The director's store
(bh-sqfvq, the port's first production consumer) is composed here too.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from beadhive import fence_schema, hq_sql_placement, writer_adopt
from beadhive.fence_data import DoltCliEngine
from beadhive.host_lease_contracts import HostLease, now_stamp
from beadhive.hq_storage_composition import (
    dolt_hive_fence,
    dolt_hq_storage,
    hq_connection_factory,
    placement_storage,
)
from beadhive_hq_storage import (
    ABSENT,
    AppendLog,
    Applied,
    CapabilityHost,
    CommittedHistory,
    HiveFence,
    Liveness,
    PlacementStore,
    RecordKind,
    StorageUnavailable,
    VersionedRecords,
    new_operation_id,
)
from beadhive_hq_storage_dolt import (
    CAUSE_DOMAIN,
    PLACEMENT_CAUSES,
    PLACEMENT_DOMAIN,
    DoltAccounts,
    DoltHistory,
    DoltHqSessions,
    DoltHqStorage,
    bump_statements,
    cause_token,
    connection_runner,
    placement_cause,
    placement_seed_statement,
    placement_witness,
)


def test_composition_binds_adapters_that_satisfy_the_port_protocols(tmp_path: Path) -> None:
    store = dolt_hq_storage({})
    for protocol in (VersionedRecords, AppendLog, Liveness, CommittedHistory, CapabilityHost):
        assert isinstance(store, protocol), protocol.__name__
    assert isinstance(store.placements(), PlacementStore)
    assert store.capability(DoltHistory) is not None
    assert store.capability(DoltAccounts) is not None

    fence = dolt_hive_fence(DoltCliEngine(tmp_path))
    assert isinstance(fence, HiveFence) and isinstance(fence, CapabilityHost)
    assert fence.capability(DoltHistory) is not None
    assert fence.capability(DoltAccounts) is None  # an HQ capability, never a hive fence's


def test_the_director_store_is_the_dolt_adapter_with_the_transitional_sessions() -> None:
    binding = {"user": "director", "database": "beadhive_hq_runtime"}
    store = placement_storage(binding)
    assert isinstance(store, DoltHqStorage) and isinstance(store, VersionedRecords)
    assert isinstance(store.capability(DoltHqSessions), DoltHqSessions)
    director = hq_sql_placement.SqlPlacementDirector({"placement_writer": binding})
    assert isinstance(director._storage(), DoltHqStorage)
    with pytest.raises(StorageUnavailable):  # no transport credential: nothing was sent
        store.read(RecordKind.PLACEMENT, "ah")


def test_a_missing_role_binding_is_unavailable_before_anything_is_sent() -> None:
    with pytest.raises(StorageUnavailable):
        dolt_hq_storage({}).read(RecordKind.AUTHORITY, "frame-a")


def test_adapter_wire_encodings_match_roots() -> None:
    body = b'{"frame":"f","epoch":2,"cause":"failover"}'
    revision = "a" * 64
    assert placement_witness("bh", revision, body) == hq_sql_placement.placement_witness(
        "bh", revision, body
    )
    assert (PLACEMENT_DOMAIN, CAUSE_DOMAIN, PLACEMENT_CAUSES) == (
        hq_sql_placement.PLACEMENT_DOMAIN,
        hq_sql_placement.CAUSE_DOMAIN,
        hq_sql_placement.PLACEMENT_CAUSES,
    )
    for cause in PLACEMENT_CAUSES:
        token = cause_token("bh", revision, cause)
        assert token == hq_sql_placement.cause_token("bh", revision, cause)
        assert placement_cause("bh", revision, token) == cause
        assert hq_sql_placement.placement_cause("bh", revision, token) == cause
    sql, params = hq_sql_placement.seed_statement("bh", epoch=4, at=1_700_000_000)
    assert (sql, params) == placement_seed_statement("bh", *params[1:4])
    assert hq_sql_placement.parse_row("bh", params[1:]).director
    assert bump_statements("frame-a", 3, revision) == writer_adopt.bump_statements(
        "frame-a", 3, revision
    )
    assert len(fence_schema.trigger_names()) == fence_schema.TRIGGER_COUNT


class _Broker:
    def __init__(self, password: str) -> None:
        self.password = password

    def get(self, reference: dict, *, deadline: float) -> str:
        return self.password


@pytest.mark.integration
@pytest.mark.dolt_server
def test_root_transport_drives_the_adapter_against_real_dolt(tmp_path: Path) -> None:
    """PyMySQL through ``hq_sql_transport.connect`` (TLS disabled on loopback only), into the
    adapter: a placement CAS whose row root's receiver-side parser recognises as a director
    row. Root's autouse fixture already holds this test's Dolt slot."""
    if shutil.which("dolt") is None:
        pytest.skip("dolt binary unavailable")
    pymysql = pytest.importorskip("pymysql")
    from beadhive_hq_storage_dolt.contract import DoltHqHarness, DoltSqlServer

    with DoltSqlServer(tmp_path / "server", pymysql.connect, hold_slot=False) as server:
        hq = DoltHqHarness(server)
        accounts = connection_runner(lambda _role: server.connect(hq.database, autocommit=False))
        accounts.execute(
            [
                "CREATE USER hq_composer@'%' IDENTIFIED BY 'fixture-only'",
                f"GRANT ALL ON {hq.database}.* TO hq_composer@'%'",
            ]
        )
        binding = {
            "host": "127.0.0.1",
            "port": server.port,
            "database": hq.database,
            "user": "hq_composer",
            "tls_mode": "disabled",
            "credential": {"config_path": "/fixture/fnox.toml", "profile": "p", "key": "k"},
        }
        roles = ("operator", "placement_writer", "publisher", "frame", "reader")
        bindings = {role: binding for role in roles}
        store = dolt_hq_storage(bindings, broker=_Broker("fixture-only"))

        seeded = hq.provision(RecordKind.PLACEMENT, "bh", {"frame": "f1", "epoch": 1})
        op = new_operation_id()
        out = store.compare_and_set(
            RecordKind.PLACEMENT,
            "bh",
            {"frame": "f2", "epoch": 2, "cause": "failover"},
            expected=seeded,
            operation=op,
        )
        assert isinstance(out, Applied) and out.revision != seeded
        assert store.lookup(RecordKind.PLACEMENT, "bh", op).operation == op
        row = server.sql(
            hq.database,
            "SELECT revision, lease_json, request_id, request_sha256 FROM hq_live_hive_leases "
            "WHERE prefix = 'bh'",
        )[0]
        assert hq_sql_placement.is_director_row("bh", row)
        created = store.compare_and_set(
            RecordKind.AUTHORITY, "frame-a", {"grant": 1}, expected=ABSENT, operation=op
        )
        assert isinstance(created, Applied)
        assert store.now().tzinfo is not None

        # The director's store over the same transport: the receiver carrier and the cause.
        director = placement_storage(binding, broker=_Broker("fixture-only"))
        stamp = now_stamp(1_700_000_000)
        hq.provision(
            RecordKind.PLACEMENT,
            "ah",
            {"authority": {}, "lease": HostLease("", "", 1, stamp, stamp).to_record()},
        )
        expected = hq_sql_placement.read_row(director, "ah").revision
        placed = hq_sql_placement.place_cas(
            director,
            prefix="ah",
            lease=HostLease("host-a", "a", 2, stamp, now_stamp(1_700_003_600)),
            authority={"frame_id": "frame-a", "holder_identity": "host-a"},
            expected_revision=expected,
            cause=hq_sql_placement.CAUSE_FAILOVER,
        )
        raw = server.sql(
            hq.database,
            "SELECT revision, lease_json, request_id, request_sha256 FROM hq_live_hive_leases "
            "WHERE prefix = 'ah'",
        )[0]
        parsed = hq_sql_placement.parse_row("ah", raw)  # the frame's strict raw-row reader
        assert parsed.director and parsed.cause == hq_sql_placement.CAUSE_FAILOVER
        assert parsed.revision == placed.revision and parsed.request_id == placed.request_id
        assert parsed.request_sha256 == placed.request_sha256

        # A refused credential is a connection refusal: nothing was sent.
        refused = dolt_hq_storage(
            {role: {**binding, "user": "nobody"} for role in roles}, broker=_Broker("x")
        )
        with pytest.raises(StorageUnavailable):
            refused.read(RecordKind.AUTHORITY, "frame-a")
        assert callable(hq_connection_factory(bindings))
