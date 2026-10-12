"""Root composition binds the Dolt adapter to the HQ storage port (hq-storage-port-adr section 8).

A thin check, not a third run of the contract suite: root's composition yields objects that
satisfy the port protocols, it wires root's PyMySQL transport (``hq_sql_transport.connect``)
into the adapter's connection factory, and the adapter's wire encodings match root's own (the
placement witness a receiver and director use to recognise a director row, the placement cause
a frame reads, the operator's seed statement, the fence bump, and the HQ config publication's
change set, kinds and digest). The director's store (bh-sqfvq) and the config publisher's
history (bh-x7xtc) are composed here too, and so are the runtime database's Dolt-only statement
surface and the director's runtime-table mappings (bh-xnn5p): the failover policy rows, the
per-incarnation session and evidence tables and their DDL, pinned equal to root's names.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from beadhive import (
    failover_observer,
    failover_policy,
    fence_schema,
    hq_sql_config,
    hq_sql_placement,
    hq_sql_runtime_schema,
    hq_sql_session,
    hq_sql_session_provision,
    work_backup,
    writer_adopt,
)
from beadhive.fence_data import DoltCliEngine
from beadhive.host_lease_contracts import HostLease, now_stamp
from beadhive.hq_storage_composition import (
    config_history,
    dolt_hive_fence,
    dolt_hq_storage,
    hq_connection_factory,
    placement_storage,
    runtime_statements,
)
from beadhive.modules.config.domain.ports import FleetConfigDocument, ordered_documents_digest
from beadhive_hq_storage import (
    ABSENT,
    ADOPT_SENTINEL_PREFIX,
    CLAIM_FRAME_LABEL_PREFIX,
    RECOVERY_LABEL_PREFIX,
    RESUMABLE_LABEL,
    AppendLog,
    Applied,
    AtomicRecords,
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
    CONFIG_TABLES,
    PLACEMENT_CAUSES,
    PLACEMENT_DOMAIN,
    DoltAccounts,
    DoltConfigHistory,
    DoltHistory,
    DoltHqStorage,
    DoltRuntimeReads,
    HqRuntimeStatements,
    bump_statements,
    cause_token,
    config_change_set,
    config_document_kind,
    config_documents_digest,
    connection_runner,
    incarnation_key,
    incarnation_table,
    placement_cause,
    placement_seed_statement,
    placement_witness,
    stamp_trigger,
)
from beadhive_hq_storage_dolt import FAILOVER_POLICY_TABLE as ADAPTER_POLICY_TABLE
from beadhive_hq_storage_dolt import IGNORE_PATTERN as ADAPTER_IGNORE_PATTERN
from beadhive_hq_storage_dolt import LIVENESS_POLICY_TABLE as ADAPTER_LIVENESS_POLICY_TABLE


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


def test_the_director_store_is_the_dolt_adapter_over_the_runtime_tables() -> None:
    binding = {"user": "director", "database": "beadhive_hq_runtime"}
    store = placement_storage(binding)
    assert isinstance(store, DoltHqStorage) and isinstance(store, VersionedRecords)
    assert isinstance(store, AtomicRecords) and isinstance(store, Liveness)
    # The signed-authority read is the Dolt-only runtime read; DoltHqSessions is gone.
    assert isinstance(store.capability(DoltRuntimeReads), DoltRuntimeReads)
    import beadhive_hq_storage_dolt

    assert not hasattr(beadhive_hq_storage_dolt, "DoltHqSessions")
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
    # The bump statements are the adapter's alone now (bh-jhgum); the frozen SQL is pinned here
    # so a change to the committed fence write is a visible, reviewed change.
    assert bump_statements("frame-a", 3, revision) == [
        f"UPDATE bh_writer SET frame = 'frame-a', epoch = 3, revision = '{revision}' WHERE id = 1",
        "DELETE FROM bh_write_mark WHERE epoch < 3",
        "UPDATE bh_epoch_live SET epoch = 3 WHERE id = 1",
        "INSERT INTO bh_write_mark (id, epoch, tbl) VALUES ('adopt-3', 3, 'bh_writer')",
    ]
    assert writer_adopt.ADOPT_SENTINEL_PREFIX == ADOPT_SENTINEL_PREFIX
    assert len(fence_schema.trigger_names()) == fence_schema.TRIGGER_COUNT
    # The reclaim's bead labels are root's work_backup dimensions.
    assert CLAIM_FRAME_LABEL_PREFIX == f"{work_backup.CLAIM_FRAME_DIMENSION}:"
    assert RECOVERY_LABEL_PREFIX == f"{work_backup.RECOVERY_DIMENSION}:"
    assert RESUMABLE_LABEL == f"{work_backup.RECOVERY_DIMENSION}:{work_backup.RESUMABLE}"


def test_the_runtime_statements_and_tables_are_roots() -> None:
    """bh-xnn5p: the runtime database's statements are the adapter's; its table names, keys
    and DDL equal root's, so 0.22.x/0.23.x readers and the operator's provisioning agree."""
    cursor = object()
    surface = runtime_statements(cursor)
    assert isinstance(surface, HqRuntimeStatements) and runtime_statements(surface) is surface
    assert ADAPTER_POLICY_TABLE == failover_policy.POLICY_TABLE
    assert ADAPTER_POLICY_TABLE == hq_sql_runtime_schema.FAILOVER_POLICY_TABLE
    assert ADAPTER_LIVENESS_POLICY_TABLE == hq_sql_runtime_schema.LIVENESS_POLICY_TABLE
    assert ADAPTER_IGNORE_PATTERN == hq_sql_session.IGNORE_PATTERN == "frame_*"
    for principal, epoch in (("frame_a", 0), ("frame_0123456789abcdef0123", 9999999999)):
        assert incarnation_table(principal, epoch, "session") == (
            hq_sql_runtime_schema.session_table(principal, epoch)
        )
        assert incarnation_table(principal, epoch, "evidence") == (
            hq_sql_runtime_schema.evidence_table(principal, epoch)
        )
        assert incarnation_key(principal, epoch) == failover_observer.session_key(principal, epoch)
    # The one trigger body condition 6 allows is the stamping trigger the DDL creates.
    create, trigger, _seed = hq_sql_session.session_ddl("frame_a", 1)
    assert trigger == stamp_trigger("frame_frame_a_1_session", "renewed_at")
    assert hq_sql_session_provision._stamp_body("session") in trigger
    assert hq_sql_session_provision._stamp_body("evidence") == (
        "SET NEW.measured_at = UTC_TIMESTAMP(6)"
    )
    # The policy write root issued before the port, pinned so a change is a reviewed one.
    from beadhive_hq_storage_dolt import policy_statements

    assert policy_statements("ah", {"executor": 3600, "transient": 1800}, {"executor": 4500}) == [
        (
            "INSERT INTO hq_live_failover_policy (scope,setting,seconds) VALUES (%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE seconds=VALUES(seconds)",
            ("ah", "executor", 4500),
        ),
        ("DELETE FROM hq_live_failover_policy WHERE scope=%s AND setting=%s", ("ah", "transient")),
    ]


def test_the_config_publisher_history_is_the_adapters_config_mapping() -> None:
    binding = {"user": "bh_hq_config_publisher", "database": "beadhive_hq_config"}
    settings = {"reader": {**binding, "user": "bh_hq_config_reader"}, "publisher": binding}
    history = config_history(settings)
    assert isinstance(history, DoltConfigHistory) and isinstance(history, CommittedHistory)
    store = hq_sql_config.SqlFleetConfigRevisionStore(settings, broker=object())
    assert isinstance(store.history, DoltConfigHistory)
    with pytest.raises(StorageUnavailable):  # no transport credential: nothing was sent
        history.head()
    with pytest.raises(StorageUnavailable):  # an absent role binding is refused the same way
        config_history({}).head()


def test_adapter_config_publication_encoding_matches_roots() -> None:
    assert CONFIG_TABLES == hq_sql_config.TABLES
    documents = (
        FleetConfigDocument("fleet.yaml", "hq:\n  mode: dolt-server\n"),
        FleetConfigDocument("beadyard.json", "{}\n"),
        FleetConfigDocument("workspace-extra.toml", "[x]\n"),
        FleetConfigDocument("allowed_signers", "k\n"),
        FleetConfigDocument("hosts/a.yaml", "é: 1\n"),
        FleetConfigDocument("hives/github/o/r.yaml", "x: 1\n"),
    )
    raw = tuple((doc.path, doc.content.encode("utf-8")) for doc in documents)
    assert hq_sql_config.publication_changes(documents, schema_version=3) == config_change_set(
        3, raw
    )
    assert config_documents_digest(raw) == ordered_documents_digest(documents)
    for document in documents:
        assert config_document_kind(document.path) == hq_sql_config._kind(document.path)


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
