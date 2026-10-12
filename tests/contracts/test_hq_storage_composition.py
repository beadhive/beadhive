"""Root composition binds the Dolt adapter to the HQ storage port (hq-storage-port-adr section 8).

A thin check, not a third run of the contract suite: root's composition yields objects that
satisfy the port protocols, it wires root's PyMySQL transport (``hq_sql_transport.connect``)
into the adapter's connection factory, and the adapter's wire encodings match root's own (the
placement witness a receiver and director use to recognise a director row, and the fence bump).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from beadhive import fence_schema, hq_sql_placement, writer_adopt
from beadhive.fence_data import DoltCliEngine
from beadhive.hq_storage_composition import (
    dolt_hive_fence,
    dolt_hq_storage,
    hq_connection_factory,
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
    DoltAccounts,
    DoltHistory,
    bump_statements,
    connection_runner,
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


def test_a_missing_role_binding_is_unavailable_before_anything_is_sent() -> None:
    with pytest.raises(StorageUnavailable):
        dolt_hq_storage({}).read(RecordKind.AUTHORITY, "frame-a")


def test_adapter_wire_encodings_match_roots() -> None:
    body = b'{"frame":"f","epoch":2,"cause":"failover"}'
    revision = "a" * 64
    assert placement_witness("bh", revision, body) == hq_sql_placement.placement_witness(
        "bh", revision, body
    )
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

        # A refused credential is a connection refusal: nothing was sent.
        refused = dolt_hq_storage(
            {role: {**binding, "user": "nobody"} for role in roles}, broker=_Broker("x")
        )
        with pytest.raises(StorageUnavailable):
            refused.read(RecordKind.AUTHORITY, "frame-a")
        assert callable(hq_connection_factory(bindings))
