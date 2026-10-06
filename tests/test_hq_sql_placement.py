"""Director placement in dolt-server HQ (bh-a94qw): the CAS, its row, grants and triggers,
the receiver against director rows (Φ3 rollback), and frames reading director placement.

SQL is an in-memory fake here; ``test_hq_sql_placement_int.py`` drives a real Dolt server.
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
    DEFAULT_TENURE_S,
    PLACEMENT_TABLE,
    PlacementError,
    PlacementLost,
    PlacementUnknown,
    PlacementUnseeded,
    SqlPlacementDirector,
    check_grants,
    check_triggers,
    conformance,
    fresh_revision,
    is_director_row,
    lease_body,
    parse_grant,
    parse_row,
    place_cas,
    placement_witness,
    seed_statement,
)
from beadhive.hq_sql_signatures import canonical
from test_emergency_sql_receiver import Cursor as ReceiverCursor
from test_emergency_sql_receiver import world  # noqa: F401 — fixture resolved by name
from test_hq_signed_hive_lease import (
    NOW,
    _frame,
    _plane,
    _sandbox,  # noqa: F401 — autouse sandbox
)

HEX64 = re.compile(r"[0-9a-f]{64}")
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
# An in-memory hq_live_hive_leases behind a DB-API connection
# =============================================================================================


class SerializationFailure(Exception):
    def __init__(self):
        super().__init__(1213, "serialization failure: this transaction conflicts")


class FakeConnection:
    def __init__(self, rows=None, *, update_matches=None, fail=None):
        self.rows = {k: list(v) for k, v in (rows or {}).items()}
        self.update_matches = update_matches  # force a rowcount
        self.fail = fail  # None | "update-1213" | "commit-1213" | "commit-lost" | "select"
        self.pending = None
        self.statements = []
        self.committed = self.rolled_back = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        if self.fail == "commit-1213":
            raise SerializationFailure()
        if self.fail == "commit-lost":
            raise ConnectionResetError("server went away")
        if self.pending is not None:
            prefix, row = self.pending
            self.rows[prefix] = row
        self.pending = None
        self.committed += 1

    def rollback(self):
        self.pending = None
        self.rolled_back += 1

    def close(self):
        self.closed = True


class FakeCursor:
    def __init__(self, connection):
        self.c, self.result = connection, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        c = self.c
        c.statements.append((sql, params))
        if sql.startswith("START TRANSACTION"):
            self.result = []
            return 0
        if sql.startswith("SELECT CURRENT_USER"):
            self.result = [("director@%", "beadhive_hq_runtime", "main")]
            return 1
        if sql.startswith("SELECT DOLT_HASHOF"):
            self.result = [("head",)]
            return 1
        if sql.startswith("SELECT revision,lease_json"):
            if c.fail == "select":
                raise RuntimeError("password=hunter2 leaked in driver text")
            row = c.rows.get(params[0])
            self.result = [tuple(row)] if row else []
            return len(self.result)
        if sql.startswith(f"UPDATE {PLACEMENT_TABLE}"):
            if c.fail == "update-1213":
                raise SerializationFailure()
            revision, body, request_id, witness, prefix, expected = params
            row = c.rows.get(prefix)
            matched = 1 if row and row[0] == expected else 0
            if c.update_matches is not None:
                matched = c.update_matches
            if matched:
                c.pending = (prefix, [revision, body, request_id, witness])
            return matched
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return list(self.result)


def _seeded(prefix="ah", lease=None):
    lease = lease or _lease(epoch=1)
    return FakeConnection({prefix: _receiver_row(prefix, lease)})


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
    connection = _seeded()
    expected = connection.rows["ah"][0]
    placed = place_cas(
        connection,
        prefix="ah",
        lease=_lease(epoch=2),
        authority=AUTHORITY,
        expected_revision=expected,
    )
    stored = parse_row("ah", connection.rows["ah"])
    assert placed.revision == stored.revision != expected
    assert HEX64.fullmatch(stored.revision)
    assert stored.director and stored.lease == _lease(epoch=2)
    assert json.loads(connection.rows["ah"][1])["lease"] == _lease(epoch=2).to_record()
    (update,) = [s for s, _ in connection.statements if s.startswith("UPDATE")]
    assert "WHERE prefix=%s AND revision=%s" in update and "SET revision=%s" in update
    assert connection.committed == 1


def test_every_write_gets_a_fresh_revision_even_for_the_same_record():
    connection = _seeded()
    seen = {connection.rows["ah"][0]}
    for epoch in (2, 3, 4):
        place_cas(
            connection,
            prefix="ah",
            lease=_lease(epoch=epoch),
            authority=AUTHORITY,
            expected_revision=connection.rows["ah"][0],
        )
        seen.add(connection.rows["ah"][0])
    assert len(seen) == 4


def test_rowcount_zero_is_lost_and_nothing_commits():
    connection = _seeded()
    expected = connection.rows["ah"][0]
    connection.update_matches = 0
    with pytest.raises(PlacementLost, match="rowcount 0") as lost:
        place_cas(
            connection,
            prefix="ah",
            lease=_lease(),
            authority=AUTHORITY,
            expected_revision=expected,
        )
    assert lost.value.reason == "rowcount 0"
    assert connection.committed == 0 and connection.rows["ah"][0] == expected


@pytest.mark.parametrize("where", ["update-1213", "commit-1213"])
def test_serialization_failure_1213_is_lost_never_retried(where):
    connection = _seeded()
    expected = connection.rows["ah"][0]
    connection.fail = where
    with pytest.raises(PlacementLost, match="1213"):
        place_cas(
            connection,
            prefix="ah",
            lease=_lease(),
            authority=AUTHORITY,
            expected_revision=expected,
        )
    updates = [s for s, _ in connection.statements if s.startswith("UPDATE")]
    assert len(updates) == 1  # one attempt, never re-issued with the same expectation
    assert connection.rows["ah"][0] == expected


def test_stale_expectation_is_lost_before_any_update():
    connection = _seeded()
    with pytest.raises(PlacementLost, match="moved"):
        place_cas(
            connection,
            prefix="ah",
            lease=_lease(),
            authority=AUTHORITY,
            expected_revision="0" * 64,
        )
    assert not [s for s, _ in connection.statements if s.startswith("UPDATE")]


def test_lost_commit_acknowledgment_is_unknown_not_lost():
    connection = _seeded()
    connection.fail = "commit-lost"
    with pytest.raises(PlacementUnknown, match="read the row back") as unknown:
        place_cas(
            connection,
            prefix="ah",
            lease=_lease(),
            authority=AUTHORITY,
            expected_revision=connection.rows["ah"][0],
        )
    assert HEX64.fullmatch(unknown.value.revision)


def test_driver_errors_never_leak_and_unseeded_rows_name_the_operator():
    connection = _seeded()
    connection.fail = "select"
    with pytest.raises(PlacementError) as failed:
        place_cas(
            connection,
            prefix="ah",
            lease=_lease(),
            authority=AUTHORITY,
            expected_revision="a" * 64,
        )
    assert "hunter2" not in str(failed.value)
    with pytest.raises(PlacementUnseeded, match="operator seeds"):
        place_cas(
            FakeConnection(),
            prefix="ah",
            lease=_lease(),
            authority=AUTHORITY,
            expected_revision="a" * 64,
        )


def test_placement_raises_the_epoch_and_release_keeps_it():
    connection = _seeded(lease=_lease(epoch=5))
    expected = connection.rows["ah"][0]
    with pytest.raises(PlacementError, match="raise the epoch"):
        place_cas(
            connection,
            prefix="ah",
            lease=_lease(epoch=5),
            authority=AUTHORITY,
            expected_revision=expected,
        )
    tombstone = HostLease("", "", 4, now_stamp(NOW), now_stamp(NOW))
    with pytest.raises(PlacementError, match="keeps the placed epoch"):
        place_cas(
            connection,
            prefix="ah",
            lease=tombstone,
            authority=AUTHORITY,
            expected_revision=expected,
        )
    with pytest.raises(PlacementError, match="64-hex"):
        place_cas(
            connection,
            prefix="ah",
            lease=_lease(epoch=6),
            authority=AUTHORITY,
            expected_revision="not-a-revision",
        )


# =============================================================================================
# The director: identity from the signed authority, explicit epochs, release
# =============================================================================================


def _director(connection, *, state=None, policies=None):
    record = {"authority": AUTHORITY, "state": "active", "cordoned": False}
    state = state if state is not None else {"frames": {"frame-a": {"active": record}}}
    policies = policies if policies is not None else {"ah": {"config_revision": "desired-1"}}
    director = SqlPlacementDirector(
        {"placement_writer": {"user": "director", "database": "beadhive_hq_runtime"}},
        clock=lambda: NOW,
        connect=lambda binding: connection,
    )
    director._verified = lambda cursor: (state, policies)
    return director


def test_director_places_a_verified_grant_at_an_explicit_epoch():
    connection = _seeded(lease=_lease("host-z", epoch=21))
    expected = connection.rows["ah"][0]
    placed = _director(connection).place(
        "ah", frame_id="frame-a", expected_revision=expected, epoch=23
    )
    assert placed.lease.epoch == 23 and placed.lease.host_id == "host-a"
    stored = parse_row("ah", connection.rows["ah"])
    assert stored.authority == {"frame_id": "frame-a", **AUTHORITY}
    assert stored.director and stored.lease.label == "frame-a"
    assert stored.lease.expires_at == now_stamp(NOW + DEFAULT_TENURE_S)
    assert connection.closed


def test_director_defaults_to_the_next_epoch_and_refuses_unplaceable_frames():
    connection = _seeded(lease=_lease("host-z", epoch=7))
    placed = _director(connection).place(
        "ah", frame_id="frame-a", expected_revision=connection.rows["ah"][0]
    )
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


def test_director_release_is_a_tombstone_at_the_same_epoch():
    connection = _seeded(lease=_lease("host-a", epoch=9))
    released = _director(connection).release(
        "ah", expected_revision=connection.rows["ah"][0], at=NOW + 5
    )
    assert released.lease.is_tombstone and released.lease.epoch == 9
    assert parse_row("ah", connection.rows["ah"]).authority == AUTHORITY


def test_director_settings_never_ride_a_frame_binding():
    director = SqlPlacementDirector({"placement_writer": {"user": "d"}, "runtime": {"user": "f"}})
    with pytest.raises(PlacementError, match="capability unavailable"):
        director.read("ah")


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
        def execute(self, sql, params=None):
            if sql.startswith("SHOW GRANTS FOR "):
                self.rows = [(line,) for line in grants[sql.removeprefix("SHOW GRANTS FOR ")]]
            else:
                assert params == (DB,)
                self.rows = [("t", "hq_live_results", "SET NEW.status='x'")]

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
    body = lease_body(world.prior["authority"], lease)
    receiver_revision = proposal_revision(world.request)
    connection = FakeConnection({"bh": [receiver_revision, body, "req", "sha"]})
    placed = place_cas(
        connection,
        prefix="bh",
        lease=HostLease("host", "host", lease.epoch + 1, now_stamp(NOW), now_stamp(NOW + 60)),
        authority=world.prior["authority"],
        expected_revision=receiver_revision,
    )
    assert placed.director and connection.rows["bh"][0] != receiver_revision


# =============================================================================================
# Frames: the proposal resolver is dropped on director rows; adopt carries the placed epoch
# =============================================================================================


def _director_row_for(frame, epoch, *, host=None):
    lease = HostLease(
        host or frame.host, frame.name, epoch, now_stamp(NOW - 60), now_stamp(NOW + 3600)
    )
    authority = {"frame_id": frame.route.frame_id, **frame.record["authority"]}
    body = lease_body(authority, lease)
    revision = fresh_revision({"lease": lease.to_record()})
    return (
        revision,
        body,
        "00000000-0000-4000-8000-000000000abc",
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
    view = placement.read()
    assert (view.frame, view.epoch) == ("host-a", 23)
    with pytest.raises(writer_adopt.PlacementLost, match="ask the director"):
        placement.cas("host-a", 24, expected=view)
    seen = {}

    def coexistence(data, placement, ref, **kwargs):
        seen["view"] = placement.read()
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
