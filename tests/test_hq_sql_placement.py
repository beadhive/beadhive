"""Director placement in dolt-server HQ (bh-a94qw): the CAS, its row, grants and triggers,
the receiver against director rows (Φ3 rollback), and frames reading director placement.

Storage is the HQ storage port's in-memory fake (bh-sqfvq): the CAS, its ``Conflict`` /
``Unknown`` handling and the director run over ``InMemoryHqStorage``. The row's Dolt wire format
(the receiver carrier, ``request_id`` / ``request_sha256``, the exact SQL) is the adapter's and is
proven on real Dolt by ``packages/beadhive-hq-storage-dolt/tests`` and
``test_hq_sql_placement_int.py``.
"""

from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

import pytest

from beadhive import host_adopt, host_fence, hq_operator_settings, writer_adopt
from beadhive.host_fence import EpochFence
from beadhive.host_lease import HostLeaseRejected
from beadhive.host_lease_contracts import HostLease, now_stamp
from beadhive.hq_control_plane import ControlPlaneError
from beadhive.hq_signed_hive_lease import proposal_revision
from beadhive.hq_sql_placement import (
    CAS_LOST,
    CAUSE_FAILOVER,
    CAUSE_PLANNED,
    DEFAULT_TENURE_S,
    PLACEMENT_TABLE,
    PlacementError,
    PlacementLost,
    PlacementUnknown,
    PlacementUnseeded,
    SqlPlacementDirector,
    cause_token,
    check_grants,
    check_triggers,
    conformance,
    fresh_revision,
    is_director_row,
    lease_body,
    parse_grant,
    parse_row,
    place_cas,
    placement_body,
    placement_cause,
    placement_witness,
    read_row,
    record_from,
    seed_statement,
)
from beadhive.hq_sql_signatures import canonical
from beadhive_hq_storage import (
    ABSENT,
    Applied,
    InMemoryHqStorage,
    InvalidRequest,
    NotConfirmed,
    RecordKind,
    StorageUnavailable,
    new_operation_id,
)
from harness.clock import FakeClock
from test_emergency_sql_receiver import Cursor as ReceiverCursor
from test_emergency_sql_receiver import world  # noqa: F401 — fixture resolved by name
from test_hq_signed_hive_lease import (
    NOW,
    _frame,
    _plane,
    _sandbox,  # noqa: F401 — autouse sandbox
)

HEX64 = re.compile(r"[0-9a-f]{64}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
AUTHORITY = {
    "frame_id": "frame-a",
    "holder_identity": "host-a",
    "instance_ref": "vm-a",
    "key_fingerprint": "SHA256:a",
    "epoch": 1,
    "audience": "fixture-fleet",
    "config_revision": "desired-1",
    "candidate_expires_at": None,
}


def _lease(host="host-a", epoch=2, *, at=NOW, tenure=3600):
    return HostLease(host, host.removeprefix("host-"), epoch, now_stamp(at), now_stamp(at + tenure))


def _receiver_row(prefix, lease, authority=AUTHORITY):
    """A row as the receiver writes it: its revision is the request digest."""
    body = lease_body(authority, lease)
    revision = hashlib.sha256(b"signed-request" + body).hexdigest()
    return [revision, body, "00000000-0000-4000-8000-000000000001", "f" * 64]


# =============================================================================================
# The HQ storage port's in-memory fake
# =============================================================================================


def _receiver_body(lease, authority=AUTHORITY):
    """A receiver-written row as the port reads it: the carrier, not director-written."""
    return {"authority": dict(authority), "lease": lease.to_record(), "director": False}


def _seeded(prefix="ah", lease=None):
    store = InMemoryHqStorage(FakeClock())
    store.provision(RecordKind.PLACEMENT, prefix, _receiver_body(lease or _lease(epoch=1)))
    return store


def _revision(store, prefix="ah"):
    return store.read(RecordKind.PLACEMENT, prefix).revision


class Counting:
    """The fake, counting compare-and-sets; ``race`` lands a competing director's CAS between
    our read and our CAS, ``supersede`` a later write between our ``Unknown`` and its lookup."""

    def __init__(self, store, *, race=None, supersede=None):
        self.store, self.race, self.supersede = store, race, supersede
        self.cas = []

    def __getattr__(self, name):
        return getattr(self.store, name)

    def _write(self, prefix, lease, expected):
        out = self.store.compare_and_set(
            RecordKind.PLACEMENT,
            prefix,
            placement_body(AUTHORITY, lease),
            expected=expected,
            operation=new_operation_id(),
        )
        assert isinstance(out, Applied)

    def compare_and_set(self, kind, key, body, *, expected, operation):
        self.cas.append((key, expected, operation))
        if self.race is not None:
            self._write(key, self.race, expected)
        return self.store.compare_and_set(kind, key, body, expected=expected, operation=operation)

    def lookup(self, kind, key, operation):
        if self.supersede is not None:
            self._write(key, self.supersede, _revision(self.store, key))
        return self.store.lookup(kind, key, operation)


# =============================================================================================
# Revisions, the row and its witness
# =============================================================================================


def test_fresh_revision_is_receiver_format_and_never_repeats():
    record = {"prefix": "ah", "lease": _lease().to_record()}
    revisions = {fresh_revision(record) for _ in range(64)}
    assert len(revisions) == 64
    assert all(HEX64.fullmatch(r) for r in revisions)  # the receiver's 64-hex shape


def test_director_witness_marks_only_director_rows():
    lease = _lease()
    receiver = _receiver_row("ah", lease)
    assert not is_director_row("ah", receiver)
    body = lease_body(AUTHORITY, lease)
    revision = fresh_revision({"x": 1})
    row = (revision, body, "id", placement_witness("ah", revision, body))
    assert is_director_row("ah", row)
    assert not is_director_row("bh", row)  # bound to the prefix
    tampered = (revision, lease_body(AUTHORITY, _lease(epoch=9)), "id", row[3])
    assert not is_director_row("ah", tampered)  # bound to the record
    assert not is_director_row("ah", ("short",))  # never raises


def test_parse_row_keeps_the_receiver_carrier_and_rejects_deviations():
    lease = _lease()
    parsed = parse_row("ah", _receiver_row("ah", lease))
    assert parsed.lease == lease and parsed.authority == AUTHORITY and not parsed.director
    assert parsed.frame_id == "frame-a"
    body = json.dumps({"authority": AUTHORITY, "lease": lease.to_record()}, indent=1).encode()
    with pytest.raises(PlacementError, match="carrier"):
        parse_row("ah", ["r", body, "id", "sha"])  # not canonical
    bad = lease.to_record() | {"epoch": 0}
    with pytest.raises(PlacementError, match="lease record"):
        parse_row("ah", ["r", canonical({"authority": {}, "lease": bad}), "id", "sha"])


def test_seed_statement_is_an_operator_insert_of_a_director_tombstone():
    sql, params = seed_statement("ah", epoch=21, at=NOW)
    assert sql.startswith(f"INSERT INTO {PLACEMENT_TABLE}")
    row = parse_row("ah", params[1:])
    assert row.director and row.lease.is_tombstone and row.lease.epoch == 21
    with pytest.raises(PlacementError):
        seed_statement("ah", epoch=0)
    with pytest.raises(PlacementError):
        seed_statement("Bad;Prefix", epoch=1)


# =============================================================================================
# The CAS (condition 5)
# =============================================================================================


def test_cas_rewrites_revision_and_writes_the_five_field_record():
    store = _seeded()
    expected = _revision(store)
    placed = place_cas(
        store,
        prefix="ah",
        lease=_lease(epoch=2),
        authority=AUTHORITY,
        expected_revision=expected,
    )
    stored = read_row(store, "ah")
    assert placed.revision == stored.revision != expected
    assert HEX64.fullmatch(stored.revision)
    assert stored.director and stored.lease == _lease(epoch=2)
    assert store.read(RecordKind.PLACEMENT, "ah").body["lease"] == _lease(epoch=2).to_record()
    # The witness the Dolt adapter writes is the one root derives for the returned record.
    assert placed.request_sha256 == placement_witness(
        "ah", placed.revision, lease_body(AUTHORITY, _lease(epoch=2))
    )


def test_every_write_gets_a_fresh_revision_even_for_the_same_record():
    store = _seeded()
    seen = {_revision(store)}
    for epoch in (2, 3, 4):
        place_cas(
            store,
            prefix="ah",
            lease=_lease(epoch=epoch),
            authority=AUTHORITY,
            expected_revision=_revision(store),
        )
        seen.add(_revision(store))
    assert len(seen) == 4


def test_a_lost_cas_is_lost_and_never_retried():
    """A port ``Conflict`` — ``rowcount 0`` or a ``1213`` serialization failure, which the port
    does not tell apart (``test_hq_storage_dolt_adapter.py`` proves the 1213 mapping on Dolt)."""
    store = Counting(_seeded(), race=_lease("host-z", epoch=9))
    expected = _revision(store)
    with pytest.raises(PlacementLost, match="rowcount 0") as lost:
        place_cas(
            store,
            prefix="ah",
            lease=_lease(),
            authority=AUTHORITY,
            expected_revision=expected,
        )
    assert lost.value.reason == CAS_LOST and "1213" in str(lost.value)
    assert len(store.cas) == 1  # one attempt, never re-issued with the same expectation
    assert read_row(store, "ah").lease.host_id == "host-z"  # the competitor's row stands


def test_stale_expectation_is_lost_before_any_update():
    store = Counting(_seeded())
    with pytest.raises(PlacementLost, match="moved"):
        place_cas(
            store,
            prefix="ah",
            lease=_lease(),
            authority=AUTHORITY,
            expected_revision="0" * 64,
        )
    assert store.cas == []


def test_lost_commit_acknowledgment_is_unknown_not_lost():
    store = Counting(_seeded())
    expected = _revision(store)
    store.faults.arm_unknown("transport", applied=False, on="compare_and_set")
    with pytest.raises(PlacementUnknown, match="read the row back") as unknown:
        place_cas(
            store,
            prefix="ah",
            lease=_lease(),
            authority=AUTHORITY,
            expected_revision=expected,
        )
    assert UUID.fullmatch(unknown.value.operation) and unknown.value.reason == "transport"
    assert unknown.value.expected == expected and _revision(store) == expected
    assert len(store.cas) == 1  # NotConfirmed never authorises a retry


def test_an_unknown_that_landed_is_confirmed_by_its_operation():
    store = Counting(_seeded())
    store.faults.arm_unknown("commit", applied=True, on="compare_and_set")
    placed = place_cas(
        store,
        prefix="ah",
        lease=_lease(epoch=2),
        authority=AUTHORITY,
        expected_revision=_revision(store),
        cause=CAUSE_FAILOVER,
    )
    stored = read_row(store, "ah")
    assert placed.revision == stored.revision and stored.cause == CAUSE_FAILOVER
    assert len(store.cas) == 1


def test_an_unknown_superseded_before_its_lookup_is_never_retried():
    """ADR rule 2: an applied-then-superseded write reads ``NotConfirmed``, which is not proof it
    did not apply and never authorises a retry."""
    store = Counting(_seeded(), supersede=_lease("host-z", epoch=9))
    store.faults.arm_unknown("deadline", applied=True, on="compare_and_set")
    with pytest.raises(PlacementUnknown, match="never re-issue"):
        place_cas(
            store,
            prefix="ah",
            lease=_lease(epoch=2),
            authority=AUTHORITY,
            expected_revision=_revision(store),
        )
    assert len(store.cas) == 1 and read_row(store, "ah").lease.host_id == "host-z"


def test_driver_errors_never_leak_and_unseeded_rows_name_the_operator():
    store = _seeded()
    store.faults.arm_unavailable(on="compare_and_set")
    with pytest.raises(PlacementError, match="unavailable; nothing written"):
        place_cas(
            store,
            prefix="ah",
            lease=_lease(),
            authority=AUTHORITY,
            expected_revision=_revision(store),
        )

    class Leaky:
        def read(self, kind, key):
            raise StorageUnavailable("HQ read unavailable")

    with pytest.raises(PlacementError) as failed:
        place_cas(
            Leaky(),
            prefix="ah",
            lease=_lease(),
            authority=AUTHORITY,
            expected_revision="a" * 64,
        )
    assert "unavailable; nothing written" in str(failed.value)

    class Raw:
        def read(self, kind, key):
            raise RuntimeError("password=hunter2 leaked in driver text")

    with pytest.raises(PlacementError) as failed:
        place_cas(
            Raw(),
            prefix="ah",
            lease=_lease(),
            authority=AUTHORITY,
            expected_revision="a" * 64,
        )
    assert "hunter2" not in str(failed.value)
    with pytest.raises(PlacementUnseeded, match="operator seeds"):
        place_cas(
            InMemoryHqStorage(FakeClock()),
            prefix="ah",
            lease=_lease(),
            authority=AUTHORITY,
            expected_revision="a" * 64,
        )


def test_placement_raises_the_epoch_and_release_keeps_it():
    store = _seeded(lease=_lease(epoch=5))
    expected = _revision(store)
    with pytest.raises(PlacementError, match="raise the epoch"):
        place_cas(
            store,
            prefix="ah",
            lease=_lease(epoch=5),
            authority=AUTHORITY,
            expected_revision=expected,
        )
    tombstone = HostLease("", "", 4, now_stamp(NOW), now_stamp(NOW))
    with pytest.raises(PlacementError, match="keeps the placed epoch"):
        place_cas(
            store,
            prefix="ah",
            lease=tombstone,
            authority=AUTHORITY,
            expected_revision=expected,
        )
    with pytest.raises(PlacementError, match="64-hex"):
        place_cas(
            store,
            prefix="ah",
            lease=_lease(epoch=6),
            authority=AUTHORITY,
            expected_revision="not-a-revision",
        )


def test_the_port_record_parses_like_the_raw_row():
    """``record_from`` is ``parse_row`` over the port body: the carrier plus ``director`` (the
    witness, decoded by the adapter) and a cause that only a director row carries."""
    lease = _lease()
    record = SimpleNamespace(key="ah", revision="r" * 64, body=_receiver_body(lease))
    parsed = record_from("ah", record)
    assert parsed.lease == lease and parsed.authority == AUTHORITY and not parsed.director
    caused = SimpleNamespace(**{**vars(record), "body": {**record.body, "cause": "failover"}})
    assert record_from("ah", caused).cause is None  # never trusted off a receiver row
    for body in (
        {"authority": AUTHORITY, "lease": lease.to_record()},  # the adapter's non-canonical read
        {**record.body, "director": "yes"},
        {**record.body, "extra": 1},
    ):
        with pytest.raises(PlacementError, match="carrier"):
            record_from("ah", SimpleNamespace(key="ah", revision="r" * 64, body=body))
    with pytest.raises(PlacementError, match="lease record"):
        record_from(
            "ah",
            SimpleNamespace(key="ah", revision="r", body={**record.body, "lease": {"epoch": 0}}),
        )


def test_failover_after_rides_one_atomic_write_never_a_lone_cas():
    """bh-xnn5p: the policy and the placement are ONE ``AtomicRecords`` write, on the fake as on
    Dolt (``test_failover_policy.py`` covers its lost, unknown and refused branches)."""
    store = Counting(_seeded())
    placed = place_cas(
        store,
        prefix="ah",
        lease=_lease(epoch=2),
        authority=AUTHORITY,
        expected_revision=_revision(store),
        failover_after={"executor": 4500},
    )
    assert store.cas == []  # no single-record CAS: the atomic write carried both
    assert placed.lease == _lease(epoch=2) and read_row(store, "ah").lease == _lease(epoch=2)
    assert store.read(RecordKind.FAILOVER_POLICY, "ah").body == {"executor": 4500}


# =============================================================================================
# The director: identity from the signed authority, explicit epochs, release
# =============================================================================================


def _director(store, *, state=None, policies=None):
    record = {"authority": AUTHORITY, "state": "active", "cordoned": False}
    state = state if state is not None else {"frames": {"frame-a": {"active": record}}}
    policies = policies if policies is not None else {"ah": {"config_revision": "desired-1"}}
    director = SqlPlacementDirector(
        {"placement_writer": {"user": "director", "database": "beadhive_hq_runtime"}},
        clock=lambda: NOW,
        storage=store,
    )
    director._verified = lambda session: (state, policies)
    return director


def test_director_places_a_verified_grant_at_an_explicit_epoch():
    store = _seeded(lease=_lease("host-z", epoch=21))
    expected = _revision(store)
    placed = _director(store).place("ah", frame_id="frame-a", expected_revision=expected, epoch=23)
    assert placed.lease.epoch == 23 and placed.lease.host_id == "host-a"
    stored = read_row(store, "ah")
    assert stored.authority == {"frame_id": "frame-a", **AUTHORITY}
    assert stored.director and stored.lease.label == "frame-a"
    assert stored.lease.expires_at == now_stamp(NOW + DEFAULT_TENURE_S)


def test_director_defaults_to_the_next_epoch_and_refuses_unplaceable_frames():
    store = _seeded(lease=_lease("host-z", epoch=7))
    placed = _director(store).place("ah", frame_id="frame-a", expected_revision=_revision(store))
    assert placed.lease.epoch == 8
    cordoned = {
        "frames": {
            "frame-a": {"active": {"authority": AUTHORITY, "state": "active", "cordoned": True}}
        }
    }
    with pytest.raises(PlacementError, match="PLACEMENT: frame frame-a"):
        _director(_seeded(), state=cordoned).place(
            "ah", frame_id="frame-a", expected_revision="a" * 64
        )
    with pytest.raises(PlacementError, match="PLACEMENT: hive ah"):
        _director(_seeded(), policies={}).place(
            "ah", frame_id="frame-a", expected_revision="a" * 64
        )


def test_director_survey_reads_every_row_and_the_policy_through_the_port():
    """bh-xnn5p: placements, the failover policy and (through `observe`) session liveness are
    port reads of the same store; the authority comes from the verified runtime read."""
    store = _seeded(lease=_lease("host-z", epoch=7))
    store.provision(RecordKind.PLACEMENT, "bh", {"authority": {}, "lease": {"epoch": 0}})
    seen = []
    survey = _director(store).survey(lambda live, registry, state: seen.append((live, registry)))
    assert set(survey.placements) == {"ah"} and survey.placements["ah"].lease.epoch == 7
    assert survey.invalid == {"bh": "placement row carrier invalid"}  # reported, never placed
    assert seen == [(store, ())]  # the store's Liveness; no registry without a runtime read
    assert survey.failover.provisioned and survey.failover.hives == {}  # code defaults
    store.compare_and_set(
        RecordKind.FAILOVER_POLICY,
        "ah",
        {"executor": 4500},
        expected=ABSENT,
        operation=new_operation_id(),
    )
    observed = _director(store).survey(lambda live, registry, state: 1)
    assert observed.observed == 1
    assert observed.failover.failover_after("ah", "executor") == 4500.0


# ---- the placement cause (bh-16347.6) ------------------------------------------------------


def test_cause_token_fits_the_receiver_request_id_column_and_is_bound_to_the_row():
    import uuid

    revision = fresh_revision({"x": 1})
    token = cause_token("ah", revision, CAUSE_FAILOVER)
    assert len(token) == 36 and str(uuid.UUID(token)) == token  # CHAR(36), a UUID shape
    assert uuid.UUID(token).version == 8 and uuid.UUID(token).variant == uuid.RFC_4122
    assert token != cause_token("ah", revision, CAUSE_PLANNED)
    assert placement_cause("ah", revision, token) == CAUSE_FAILOVER
    assert placement_cause("ah", fresh_revision({"x": 1}), token) is None  # another revision
    assert placement_cause("bh", revision, token) is None  # another hive
    assert placement_cause("ah", revision, str(uuid.uuid4())) is None  # pre-cause director row
    assert placement_cause("ah", revision, None) is None  # never raises
    with pytest.raises(PlacementError, match="cause"):
        cause_token("ah", revision, "forced")


def test_director_place_defaults_to_planned_and_the_cause_rides_in_the_same_cas():
    store = Counting(_seeded(lease=_lease("host-z", epoch=7)))
    director = _director(store)
    planned = director.place("ah", frame_id="frame-a", expected_revision=_revision(store))
    stored = read_row(store, "ah")
    assert planned.cause == stored.cause == CAUSE_PLANNED and stored.failover is False
    failed_over = director.place(
        "ah", frame_id="frame-a", expected_revision=stored.revision, cause=CAUSE_FAILOVER
    )
    stored = read_row(store, "ah")
    assert failed_over.cause == stored.cause == CAUSE_FAILOVER and stored.failover is True
    assert stored.request_id == failed_over.request_id
    # The body is the receiver's carrier plus the cause and the director flag; the Dolt adapter
    # stores exactly {authority, lease} in lease_json (test_hq_storage_dolt_adapter.py).
    body = store.read(RecordKind.PLACEMENT, "ah").body
    assert set(body) == {"authority", "lease", "director", "cause"}
    assert set(body["lease"]) == {"host_id", "label", "epoch", "adopted_at", "expires_at"}
    assert body["authority"] == {"frame_id": "frame-a", **AUTHORITY}
    assert len(store.cas) == 2  # one CAS per placement: the cause rides in it
    with pytest.raises(PlacementError, match="cause"):
        director.place("ah", frame_id="frame-a", expected_revision=stored.revision, cause="x")


def test_releases_seeds_and_receiver_rows_carry_no_cause():
    store = _seeded(lease=_lease("host-a", epoch=9))
    released = _director(store).release("ah", expected_revision=_revision(store))
    assert released.cause is None and read_row(store, "ah").cause is None
    with pytest.raises(PlacementError, match="release carries no placement cause"):
        place_cas(
            store,
            prefix="ah",
            lease=released.lease,
            authority=AUTHORITY,
            expected_revision=released.revision,
            cause=CAUSE_PLANNED,
        )
    _sql, params = seed_statement("ah", epoch=3, at=NOW)
    assert parse_row("ah", params[1:]).failover is None
    # A receiver row whose request_id happens to equal a cause token is still not a director
    # row, so it never carries a cause (the token is only trusted under the witness).
    row = _receiver_row("ah", _lease())
    row[2] = cause_token("ah", row[0], CAUSE_FAILOVER)
    parsed = parse_row("ah", row)
    assert not parsed.director and parsed.cause is None and parsed.failover is None


def test_director_release_is_a_tombstone_at_the_same_epoch():
    store = _seeded(lease=_lease("host-a", epoch=9))
    released = _director(store).release("ah", expected_revision=_revision(store), at=NOW + 5)
    assert released.lease.is_tombstone and released.lease.epoch == 9
    assert read_row(store, "ah").authority == AUTHORITY


def test_director_settings_never_ride_a_frame_binding():
    director = SqlPlacementDirector({"placement_writer": {"user": "d"}, "runtime": {"user": "f"}})
    with pytest.raises(PlacementError, match="capability unavailable"):
        director.read("ah")
    store = _seeded()
    with pytest.raises(PlacementError, match="capability unavailable"):
        SqlPlacementDirector(
            {"placement_writer": {"user": "d"}, "runtime": {"user": "f"}}, storage=store
        ).read("ah")


def test_director_storage_failures_never_leak_driver_text():
    class Down:
        def read(self, kind, key):
            raise StorageUnavailable("HQ reader principal or endpoint mismatch")

        def read_many(self, kind, keys=None):
            raise StorageUnavailable("HQ reader principal or endpoint mismatch")

        def capability(self, kind):
            return None

    director = _director(Down())
    with pytest.raises(PlacementError, match="principal or endpoint mismatch"):
        director.read("ah")
    with pytest.raises(PlacementError, match="verified placement for ah unavailable"):
        director.place("ah", frame_id="frame-a", expected_revision="a" * 64)
    with pytest.raises(PlacementError, match="^failover policy read unavailable$"):
        director.failover_policy()
    with pytest.raises(PlacementError, match="^failover policy write unavailable; nothing"):
        director.set_failover_policy("ah", {"executor": 4500})
    # Without the Dolt-only runtime read, a verified read is refused (the fake has none).
    with pytest.raises(PlacementError, match="needs dolt-server"):
        SqlPlacementDirector({"placement_writer": {"user": "d"}}, storage=_seeded())._authority()


def test_operator_settings_carry_the_placement_writer(tmp_path):
    binding = {
        "host": "hq.example.net",
        "database": "beadhive_hq_runtime",
        "user": "director",
        "server_name": "hq.example.net",
        "ca_file": "/etc/ssl/hq.pem",
        "credential": {"config_path": "/etc/fnox.toml", "profile": "hq", "key": "DIRECTOR"},
    }
    path = tmp_path / "director.json"
    path.write_text(json.dumps({"hq": {"sql": {"placement_writer": binding, "runtime": None}}}))
    settings = hq_operator_settings.load_placement_settings(path)
    assert settings["placement_writer"]["user"] == "director"
    with pytest.raises(ControlPlaneError, match="authority_writer is required"):
        hq_operator_settings.load_settings(path)
    path.write_text(json.dumps({"hq": {"sql": {"placement_writer": {"user": "bad user!"}}}}))
    with pytest.raises(ControlPlaneError, match="placement_writer invalid"):
        hq_operator_settings.load_placement_settings(path)
    path.write_text(json.dumps({"hq": {"sql": {"placement_writer": binding, "runtime": binding}}}))
    with pytest.raises(ControlPlaneError, match="runtime must be null"):
        hq_operator_settings.load_placement_settings(path)


# =============================================================================================
# Conformance (conditions 5-6)
# =============================================================================================

DB = "beadhive_hq_runtime"


def test_grant_lines_parse_dolt_and_mysql_spellings():
    grant = parse_grant(f"GRANT SELECT, UPDATE ON `{DB}`.`hq_live_hive_leases` TO `director`@`%`")
    assert grant.privileges == {"SELECT", "UPDATE"} and grant.table == "hq_live_hive_leases"
    usage = parse_grant("GRANT USAGE ON *.* TO `frame_a`@`localhost`")
    assert usage.database == "*" and usage.privileges == {"USAGE"}
    proc = parse_grant(f"GRANT EXECUTE ON PROCEDURE `{DB}`.`dolt_commit` TO `x`@`%`")
    assert proc.privileges == {"EXECUTE"} and proc.table == "dolt_commit"
    assert parse_grant("GRANT `role_a` TO `x`@`%`") is None


def test_director_must_hold_update_on_placement_only():
    good = [
        "GRANT USAGE ON *.* TO `director`@`%`",
        f"GRANT SELECT ON `{DB}`.`hq_authority` TO `director`@`%`",
        f"GRANT SELECT, UPDATE ON `{DB}`.`hq_live_hive_leases` TO `director`@`%`",
    ]
    assert check_grants(good, role="director", database=DB) == []
    assert check_grants(good[:2], role="director", database=DB) == [
        f"director lacks UPDATE on {DB}.hq_live_hive_leases"
    ]
    for extra in (
        f"GRANT INSERT ON `{DB}`.`hq_live_hive_leases` TO `director`@`%`",
        f"GRANT UPDATE ON `{DB}`.`hq_authority` TO `director`@`%`",
        f"GRANT ALL PRIVILEGES ON `{DB}`.* TO `director`@`%`",
        "GRANT SUPER ON *.* TO `director`@`%`",
        f"GRANT SELECT ON `{DB}`.`hq_authority` TO `director`@`%` WITH GRANT OPTION",
    ):
        assert check_grants([*good, extra], role="director", database=DB), extra


def test_frames_hold_select_only_on_placement():
    inbox = "hq_live_inbox_frame_a_1"
    good = [
        "GRANT USAGE ON *.* TO `frame_a`@`localhost`",
        f"GRANT SELECT ON `{DB}`.`hq_live_hive_leases` TO `frame_a`@`localhost`",
        f"GRANT SELECT, INSERT, UPDATE ON `{DB}`.`{inbox}` TO `frame_a`@`localhost`",
    ]
    assert check_grants(good, role="frame", database=DB, writable=[inbox]) == []
    bad = f"GRANT SELECT, UPDATE ON `{DB}`.`hq_live_hive_leases` TO `frame_a`@`localhost`"
    assert check_grants([*good, bad], role="frame", database=DB, writable=[inbox])
    assert check_grants(good, role="frame", database=DB)  # inbox not declared writable


def test_trigger_check_fails_on_any_trigger_touching_a_protected_table():
    assert check_triggers([]) == []
    stamp = ("session_stamp", "frame_a_1_session", "SET NEW.renewed_at = UTC_TIMESTAMP(6)")
    assert check_triggers([stamp], frame_writable=["frame_a_1_session"]) == []
    escalates = (
        "escalates",
        "hq_live_inbox_frame_a_1",
        "BEGIN UPDATE hq_live_hive_leases SET revision='x'; END",
    )
    on_protected = ("audit", "hq_live_hive_leases", "SET NEW.revision = NEW.revision")
    dml = ("copy", "frame_a_1_session", "INSERT INTO frame_b_1_session VALUES (1)")
    problems = check_triggers([escalates, on_protected, dml], frame_writable=["frame_a_1_session"])
    assert len(problems) == 3
    assert "touches protected hq_live_hive_leases" in problems[0]
    assert "sits on protected table" in problems[1]
    assert "runs DML" in problems[2]


def test_conformance_reads_grants_and_triggers_from_the_server():
    grants = {
        "'director'@'%'": [f"GRANT SELECT, UPDATE ON `{DB}`.`hq_live_hive_leases` TO d"],
        "'frame_a'@'h'": [f"GRANT SELECT ON `{DB}`.`hq_live_hive_leases` TO f"],
    }

    class Cursor:
        # The reads are the Dolt adapter's DoltAccounts over this cursor (bh-3kl1k); triggers
        # come from the database's dolt_schemas, never information_schema.triggers.
        def execute(self, sql, params=None):
            assert params is None
            if sql.startswith("SHOW GRANTS FOR "):
                self.description = (("Grants",),)
                self.rows = [(line,) for line in grants[sql.removeprefix("SHOW GRANTS FOR ")]]
            else:
                assert f"`{DB}`.dolt_schemas" in sql and "type = 'trigger'" in sql
                self.description = (("n",), ("f",))
                fragment = (
                    "CREATE TRIGGER t BEFORE INSERT ON hq_live_results FOR EACH ROW "
                    "SET NEW.status='x'"
                )
                self.rows = [("t", fragment)]

        def fetchall(self):
            return self.rows

    problems = conformance(
        Cursor(), database=DB, director="'director'@'%'", frames={"'frame_a'@'h'": ()}
    )
    assert problems == ["trigger t sits on protected table hq_live_results"]


# =============================================================================================
# The receiver keeps working against director-written rows (Φ3 rollback path)
# =============================================================================================


def _director_prior(fixture):
    """Rewrite the fixture's incumbent row as the director would have written it: the placed
    frame's identity from its signed grant (which already carries ``frame_id``)."""
    lease = HostLease(**fixture.prior["lease"])
    body = lease_body(fixture.prior["authority"], lease)
    revision = fresh_revision({"prefix": "bh", "lease": lease.to_record()})
    return revision, body, placement_witness("bh", revision, body)


def test_receiver_renews_over_a_director_written_row(world, monkeypatch):  # noqa: F811
    revision, body, witness = _director_prior(world)
    assert HEX64.fullmatch(revision)
    original = ReceiverCursor.execute

    def execute(self, sql, params=None):
        original(self, sql, params)
        if "FROM hq_live_hive_leases" in sql:
            self.rows = [(revision, body, "00000000-0000-4000-8000-0000000000dd", witness)]

    monkeypatch.setattr(ReceiverCursor, "execute", execute)
    world.request["expected_revision"] = revision
    accepted = world.receiver.accept_hive_lease("frame", "request")
    assert HEX64.fullmatch(accepted) and accepted != revision
    assert world.committed
    (update,) = [p for s, p in world.cursor.statements if s.startswith("UPDATE hq_live")]
    assert update[-1] == revision  # the receiver's own CAS presents the director's revision


def test_director_takes_over_a_receiver_written_row(world):  # noqa: F811
    lease = HostLease(**world.prior["lease"])
    store = InMemoryHqStorage(FakeClock())
    store.provision(RecordKind.PLACEMENT, "bh", _receiver_body(lease, world.prior["authority"]))
    receiver_revision = _revision(store, "bh")
    placed = place_cas(
        store,
        prefix="bh",
        lease=HostLease("host", "host", lease.epoch + 1, now_stamp(NOW), now_stamp(NOW + 60)),
        authority=world.prior["authority"],
        expected_revision=receiver_revision,
    )
    assert placed.director and _revision(store, "bh") != receiver_revision
    # The receiver's own revision format (its request digest) is taken over on real Dolt in
    # test_hq_sql_placement_int.py::test_receiver_cas_round_trips_with_director_rows.
    assert HEX64.fullmatch(proposal_revision(world.request))


# =============================================================================================
# Frames: the proposal resolver is dropped on director rows; adopt carries the placed epoch
# =============================================================================================


def _director_row_for(frame, epoch, *, host=None, cause=None):
    lease = HostLease(
        host or frame.host, frame.name, epoch, now_stamp(NOW - 60), now_stamp(NOW + 3600)
    )
    authority = {"frame_id": frame.route.frame_id, **frame.record["authority"]}
    body = lease_body(authority, lease)
    revision = fresh_revision({"lease": lease.to_record()})
    return (
        revision,
        body,
        cause_token("ah", revision, cause) if cause else "00000000-0000-4000-8000-000000000abc",
        placement_witness("ah", revision, body),
    )


def test_director_row_is_the_lease_and_proposals_no_longer_count(tmp_path):
    a = _frame(tmp_path)
    proposal, _ = a.propose(22)
    row = _director_row_for(a, 22)
    plane, _ = _plane(a, fence=EpochFence(22, "host-a"), rows=[proposal], receiver_row=row)
    revision, lease = plane.read_hive_lease_record("ah", holder_identity="host-a")
    assert revision == row[0] and lease.epoch == 22 and lease.held_by("host-a")
    # A conflicting proposal at the fence epoch would fail the 0.22.8 resolver closed; on a
    # director-owned row proposals are simply not evidence any more.
    other, _ = a.propose(22, adopted=NOW - 30, request_id="00000000-0000-4000-8000-0000000000ff")
    plane, _ = _plane(a, fence=EpochFence(22, "host-a"), rows=[proposal, other], receiver_row=row)
    assert plane.read_hive_lease_record("ah", holder_identity="host-a")[0] == row[0]


def test_director_row_stays_bound_to_the_epoch_fence(tmp_path):
    a = _frame(tmp_path)
    ahead = _director_row_for(a, 23)  # placed, adopt not yet carried into refs/bh/epoch
    plane, _ = _plane(a, fence=EpochFence(22, "host-z"), receiver_row=ahead)
    revision, lease = plane.read_hive_lease_record("ah")
    assert lease.host_id == "host-z"  # the fence's holder, advisory: never "free"
    assert plane.read_hive_lease_record("ah", holder_identity="host-a")[1] is None
    contradicting = _director_row_for(a, 22)
    plane, _ = _plane(a, fence=EpochFence(22, "host-z"), receiver_row=contradicting)
    with pytest.raises(ControlPlaneError, match="fails closed"):
        plane.read_hive_lease_record("ah", holder_identity="host-a")


def test_signed_frames_cannot_propose_on_a_director_placed_hive(tmp_path, monkeypatch):
    a = _frame(tmp_path)
    plane, _ = _plane(a, fence=EpochFence(22, "host-a"), receiver_row=_director_row_for(a, 22))
    plane.settings["runtime"] = {"operation_timeout": 15}
    monkeypatch.setattr(plane, "propose_hive_lease", lambda *a, **k: pytest.fail("proposed"))
    with pytest.raises(ControlPlaneError, match="PLACEMENT: hive ah is placed by the director"):
        plane.publish_hive_lease("ah", _lease(epoch=23), expected="", operation="adopt")
    placed = plane.read_placement("ah")
    assert placed.director and placed.lease.epoch == 22


def test_receiver_mode_keeps_accepting_proposals_against_director_rows(tmp_path):
    a = _frame(tmp_path)
    plane, _ = _plane(a, fence=None, settings={}, receiver_row=_director_row_for(a, 22))
    assert plane.require_frame_placeable("ah") is None  # Φ3 rollback: the receiver decides
    _, lease = plane.read_hive_lease_record("ah", holder_identity="host-a")
    assert lease is not None and lease.epoch == 22


def _adopt_world(tmp_path, monkeypatch, frame, *, row, fence):
    plane, _ = _plane(frame, fence=fence, receiver_row=row)
    plane.config_backend = "sql"
    hive = tmp_path / "hive"
    (hive / ".git").mkdir(parents=True, exist_ok=True)
    installed = []
    monkeypatch.setattr("beadhive.frame_eligibility.require_eligible", lambda *a, **k: None)
    monkeypatch.setattr("beadhive.host_lease._frame_plane", lambda cwd: plane)
    monkeypatch.setattr(host_fence, "read_fence", lambda *a, **k: ("sha-old", fence))

    def install(remote, new, *, expected, cwd):
        installed.append((new, expected))
        return "sha-new"

    monkeypatch.setattr(host_fence, "install_fence", install)
    monkeypatch.setattr("beadhive.host_lease.read", lambda *a, **k: pytest.fail("legacy read"))
    monkeypatch.setattr(
        "beadhive.frame_eligibility.evictable", lambda *a, **k: pytest.fail("frame eviction")
    )

    def adopt(host="host-a"):
        return host_adopt.adopt(
            prefix="ah",
            hive_remote="origin",
            hq_remote="origin",
            hive_cwd=hive,
            hq_cwd=tmp_path,
            host_id=host,
            label="a",
            fence_data=_NotCutOver(),
        )

    return adopt, installed


class _NotCutOver:
    def remote_writer(self):
        return None


def test_adopt_carries_the_placed_epoch_into_the_fence(tmp_path, monkeypatch):
    a = _frame(tmp_path)
    adopt, installed = _adopt_world(
        tmp_path, monkeypatch, a, row=_director_row_for(a, 23), fence=EpochFence(22, "host-z")
    )
    outcome = adopt()
    assert outcome.epoch == 23 and outcome.fence_sha == "sha-new"
    assert installed == [(EpochFence(23, "host-a"), "sha-old")]


def test_adopt_is_idempotent_and_refuses_unplaced_frames_or_a_fence_ahead(tmp_path, monkeypatch):
    a = _frame(tmp_path)
    adopt, installed = _adopt_world(
        tmp_path, monkeypatch, a, row=_director_row_for(a, 23), fence=EpochFence(23, "host-a")
    )
    assert adopt().fence_sha == "sha-old" and installed == []
    with pytest.raises(HostLeaseRejected, match="PLACEMENT: hive ah is placed by the director"):
        adopt("host-b")
    adopt, installed = _adopt_world(
        tmp_path, monkeypatch, a, row=_director_row_for(a, 23), fence=EpochFence(24, "host-z")
    )
    with pytest.raises(HostLeaseRejected, match="re-place it at epoch 25"):
        adopt()
    assert installed == []


def test_cut_over_adopt_resumes_at_the_director_placed_epoch(tmp_path, monkeypatch):
    a = _frame(tmp_path)
    plane, _ = _plane(a, fence=EpochFence(22, "host-z"), receiver_row=_director_row_for(a, 23))
    placement = host_adopt._DirectorPlacement(plane, "ah")
    view = placement.read("ah")
    assert (view.frame, view.epoch) == ("host-a", 23)
    operation = new_operation_id()
    with pytest.raises(writer_adopt.PlacementLost, match="ask the director"):
        placement.compare_and_set(
            "ah", "host-a", 24, expected=view, cause=CAUSE_FAILOVER, operation=operation
        )
    # It never reports an outcome, so there is nothing a lookup could confirm.
    assert placement.lookup("ah", operation) == NotConfirmed(operation)
    with pytest.raises(InvalidRequest):
        placement.read("other")
    seen = {}

    def coexistence(data, placement, ref, **kwargs):
        seen["view"] = placement.read("ah")
        return SimpleNamespace(epoch=23, ref=SimpleNamespace(sha="ref-23"))

    adopt, _ = _adopt_world(
        tmp_path, monkeypatch, a, row=_director_row_for(a, 23), fence=EpochFence(22, "host-z")
    )
    monkeypatch.setattr(writer_adopt, "coexistence_adopt", coexistence)
    monkeypatch.setattr(host_adopt.failover_reclaim, "for_fence_data", lambda *a, **k: None)
    monkeypatch.setattr(host_adopt, "fence_data_for", lambda *a: object())
    outcome = host_adopt.adopt(
        prefix="ah",
        hive_remote="origin",
        hq_remote="origin",
        hive_cwd=tmp_path / "hive",
        hq_cwd=tmp_path,
        host_id="host-a",
        label="a",
    )
    assert outcome.epoch == 23 and outcome.fence_sha == "ref-23"
    assert seen["view"].epoch == 23


@pytest.mark.parametrize(
    "cause,override,kind",
    [
        (CAUSE_FAILOVER, None, True),
        (CAUSE_PLANNED, None, False),
        (None, None, None),  # a director row written before the cause: unknown, no reclaim
        (CAUSE_PLANNED, True, True),  # an explicit caller kind still wins
    ],
    ids=["failover", "planned", "no-cause", "override"],
)
def test_cut_over_adopt_takes_its_kind_from_the_placement_cause(
    tmp_path, monkeypatch, cause, override, kind
):
    """bh-16347.6: the director-placed adopt resumes at the placed epoch, so the displaced
    placement cannot tell failover from handoff; the cause in the row does."""
    a = _frame(tmp_path)
    row = _director_row_for(a, 23, cause=cause)
    seen = {}

    def coexistence(data, placement, ref, **kwargs):
        seen["failover"] = kwargs["failover"]
        seen["view"] = placement.read("ah")
        return SimpleNamespace(epoch=23, ref=SimpleNamespace(sha="ref-23"))

    _adopt_world(tmp_path, monkeypatch, a, row=row, fence=EpochFence(22, "host-z"))
    monkeypatch.setattr(writer_adopt, "coexistence_adopt", coexistence)
    monkeypatch.setattr(host_adopt.failover_reclaim, "for_fence_data", lambda *a, **k: None)
    monkeypatch.setattr(host_adopt, "fence_data_for", lambda *a: object())
    host_adopt.adopt(
        prefix="ah",
        hive_remote="origin",
        hq_remote="origin",
        hive_cwd=tmp_path / "hive",
        hq_cwd=tmp_path,
        host_id="host-a",
        label="a",
        failover=override,
    )
    assert seen["failover"] is kind and seen["view"].epoch == 23
    # The 0.22.8 write-gate reader is indifferent to the cause: same record, same holder.
    plane, _ = _plane(a, fence=EpochFence(23, "host-a"), receiver_row=row)
    revision, lease = plane.read_hive_lease_record("ah", holder_identity="host-a")
    assert revision == row[0] and lease.held_by("host-a")
    assert plane.read_placement("ah").cause == cause


def test_director_placement_pins_the_row_the_kind_came_from(tmp_path):
    a = _frame(tmp_path)
    row = _director_row_for(a, 23, cause=CAUSE_FAILOVER)
    plane, _ = _plane(a, fence=EpochFence(22, "host-z"), receiver_row=row)
    moved = host_adopt._DirectorPlacement(plane, "ah", pinned="0" * 64)
    with pytest.raises(writer_adopt.PlacementLost, match="moved while the adopt was reading"):
        moved.read("ah")
    pinned = host_adopt._DirectorPlacement(plane, "ah", pinned=row[0])
    assert pinned.read("ah").epoch == 23 and pinned.read("ah").token == row[0]  # first read only


def test_git_hq_keeps_the_gitref_cas_placement_path(tmp_path, monkeypatch):
    """No SQL plane: director placement never engages and the lease CAS stays gitref.cas."""
    monkeypatch.setattr("beadhive.host_lease._frame_plane", lambda cwd: None)
    assert host_adopt._director_placement("ah", tmp_path) is None
    calls = []

    def cas(remote, ref, record, *, expected, cwd):
        calls.append((ref, record["epoch"], expected))
        return SimpleNamespace(ok=True, sha="new", detail="")

    monkeypatch.setattr("beadhive.gitref.cas", cas)
    monkeypatch.setattr("beadhive.frame_eligibility.require_eligible", lambda *a, **k: None)
    monkeypatch.setattr("beadhive.host_lease.gitref.read_remote", lambda *a, **k: ("", None))
    from beadhive import gitref, host_lease

    outcome = host_lease.adopt("origin", "ah", host_id="host-a", label="a", cwd=tmp_path)
    assert calls == [("refs/bh/lease/ah", 1, gitref.ABSENT)]
    assert outcome.lease.epoch == 1
