"""Director placement against a real, throwaway Dolt sql-server (bh-a94qw).

The product CAS (:func:`beadhive.hq_sql_placement.place_cas`) on the product table
(``hq_live_hive_leases`` from ``PROTECTED_LIVE_SCHEMA``) with real grants: one winner per race
and every loser reported lost (``rowcount 0`` or ``1213``), frames refused by the server, the
grant/trigger conformance check reading the server's own ``SHOW GRANTS`` and
``information_schema.triggers``, and the receiver's exact CAS statement round-tripping with
director-written rows (Φ3 rollback). A private scratch server only: never a live HQ.
"""

from __future__ import annotations

import shutil
import threading
import time

import pytest

from beadhive.host_lease_contracts import HostLease, now_stamp
from beadhive.hq_sql_placement import (
    CAUSE_FAILOVER,
    PlacementLost,
    conformance,
    fresh_revision,
    lease_body,
    parse_row,
    place_cas,
    read_row,
    seed_statement,
)
from beadhive.hq_sql_runtime_schema import PROTECTED_LIVE_SCHEMA, inbox_ddl
from test_placement_authority_int import HqServer

pytestmark = [
    pytest.mark.integration,
    pytest.mark.dolt_server,
    pytest.mark.skipif(shutil.which("dolt") is None, reason="dolt not installed"),
]

DB = "beadhive_hq_runtime"
DIRECTOR = ("director", "director-secret")
FRAME = ("frame_a", "frame-a-secret")
INBOX = "hq_live_inbox_frame_a_1"
AUTHORITY = {"frame_id": "frame-a", "holder_identity": "host-a", "epoch": 1}
RACERS = 6


def _held(host, epoch, label=None):
    now = time.time()
    return HostLease(host, label or host, epoch, now_stamp(now), now_stamp(now + 3600))


class PlacementServer(HqServer):
    def connect(self, who=("root", ""), *, autocommit=True, database=DB):
        return super().connect(who, autocommit=autocommit, database=database)

    def provision(self):
        root = self.connect(database=None)
        with root.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {DB}")
        root.close()
        root = self.connect()
        with root.cursor() as cursor:
            for statement in PROTECTED_LIVE_SCHEMA:
                cursor.execute(statement)
            cursor.execute(inbox_ddl("frame_a", 1))
            for user, password in (DIRECTOR, FRAME):
                cursor.execute(f"CREATE USER '{user}'@'%' IDENTIFIED BY '{password}'")
            cursor.execute(f"GRANT SELECT, UPDATE ON {DB}.hq_live_hive_leases TO 'director'@'%'")
            cursor.execute(f"GRANT SELECT ON {DB}.hq_live_hive_leases TO 'frame_a'@'%'")
            cursor.execute(f"GRANT SELECT, INSERT, UPDATE ON {DB}.{INBOX} TO 'frame_a'@'%'")
            sql, params = seed_statement("ah", epoch=21)
            cursor.execute(sql, params)
        root.close()

    def revision(self):
        conn = self.connect(DIRECTOR, autocommit=False)
        try:
            return read_row(conn, "ah").revision
        finally:
            conn.close()


@pytest.fixture
def server(tmp_path):
    server = PlacementServer(tmp_path / "hq")
    server.start()
    try:
        server.provision()
        yield server
    finally:
        server.stop()


def _place(server, host, epoch, expected, cause=None):
    conn = server.connect(DIRECTOR, autocommit=False)
    try:
        return place_cas(
            conn,
            prefix="ah",
            lease=_held(host, epoch),
            authority={**AUTHORITY, "holder_identity": host},
            expected_revision=expected,
            cause=cause,
        )
    finally:
        conn.close()


def test_director_cas_rewrites_the_row_and_frames_cannot_write_it(server):
    import pymysql

    seeded = server.revision()
    placed = _place(server, "host-a", 22, seeded)
    conn = server.connect(FRAME, autocommit=False)
    try:
        row = read_row(conn, "ah")  # frames read placement through SELECT
        assert row.director and row.revision == placed.revision != seeded
        assert row.lease.epoch == 22 and row.lease.host_id == "host-a"
        with conn.cursor() as cursor, pytest.raises(pymysql.err.OperationalError):
            cursor.execute(
                "UPDATE hq_live_hive_leases SET revision=%s WHERE prefix='ah'", ("0" * 64,)
            )
    finally:
        conn.close()
    with pytest.raises(PlacementLost, match="moved"):
        _place(server, "host-b", 23, seeded)  # the same expectation never wins twice


def _racer(server, i, epoch, expected, barrier, outcomes):
    conn = server.connect(DIRECTOR, autocommit=False)
    try:
        barrier.wait(timeout=60)
        place_cas(
            conn,
            prefix="ah",
            lease=_held(f"host-{i}", epoch, "x"),
            authority=AUTHORITY,
            expected_revision=expected,
        )
        outcomes[i] = "won"
    except PlacementLost as exc:
        outcomes[i] = exc.reason
    finally:
        conn.close()


def test_racing_directors_exactly_one_wins_and_every_loser_is_lost(server):
    for round_ in range(3):
        expected, epoch = server.revision(), 22 + round_
        barrier, outcomes = threading.Barrier(RACERS), {}
        threads = [
            threading.Thread(target=_racer, args=(server, i, epoch, expected, barrier, outcomes))
            for i in range(RACERS)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        winners = [i for i, o in outcomes.items() if o == "won"]
        assert len(outcomes) == RACERS and len(winners) == 1, outcomes
        losers = [o for o in outcomes.values() if o != "won"]
        assert all(
            o in {"rowcount 0", "1213 serialization failure", "the row moved since it was read"}
            for o in losers
        ), losers
        conn = server.connect(DIRECTOR, autocommit=False)
        row = read_row(conn, "ah")
        conn.close()
        assert row.lease.host_id == f"host-{winners[0]}" and row.lease.epoch == epoch


def test_receiver_cas_round_trips_with_director_rows(server):
    """Φ3 rollback: the receiver's own UPDATE presents a director revision and wins; the
    director then takes the receiver-written row over by its revision. The director's placement
    cause (bh-16347.6) rides in ``request_id``: the receiver's CAS is indifferent to it and its
    own row carries none."""
    director_rev = _place(server, "host-a", 22, server.revision(), CAUSE_FAILOVER).revision
    conn = server.connect(FRAME, autocommit=False)
    assert read_row(conn, "ah").cause == CAUSE_FAILOVER  # what the adopting frame reads
    conn.close()
    lease = _held("host-a", 22, "a")
    body = lease_body(AUTHORITY, lease)
    receiver_rev = fresh_revision({"receiver": True})
    root = server.connect(autocommit=False)
    with root.cursor() as cursor:
        # Verbatim the statement SqlTrustedReceiver.accept_hive_lease issues.
        matched = cursor.execute(
            "UPDATE hq_live_hive_leases SET revision=%s,lease_json=%s,"
            "request_id=%s,request_sha256=%s "
            "WHERE prefix=%s AND revision=%s",
            (
                receiver_rev,
                body,
                "00000000-0000-4000-8000-000000000001",
                "e" * 64,
                "ah",
                director_rev,
            ),
        )
    root.commit()
    root.close()
    assert matched == 1
    conn = server.connect(DIRECTOR, autocommit=False)
    row = read_row(conn, "ah")
    conn.close()
    assert not row.director and row.cause is None
    assert parse_row("ah", (row.revision, body, "x", "e" * 64)).lease == lease
    taken = _place(server, "host-b", 23, receiver_rev)
    assert taken.director and taken.lease.epoch == 23


def test_conformance_reads_real_grants_and_triggers(server):
    root = server.connect()
    try:
        with root.cursor() as cursor:
            frames = {"'frame_a'@'%'": (INBOX,)}
            assert conformance(cursor, database=DB, director="'director'@'%'", frames=frames) == []
            cursor.execute(
                f"CREATE TRIGGER escalate AFTER INSERT ON {INBOX} FOR EACH ROW "
                "UPDATE hq_live_hive_leases SET request_id = NEW.request_id WHERE prefix = 'ah'"
            )
            cursor.execute(f"GRANT INSERT ON {DB}.hq_live_hive_leases TO 'director'@'%'")
            cursor.execute(f"GRANT UPDATE ON {DB}.hq_live_hive_leases TO 'frame_a'@'%'")
            problems = conformance(cursor, database=DB, director="'director'@'%'", frames=frames)
    finally:
        root.close()
    assert any("trigger escalate" in p and "hq_live_hive_leases" in p for p in problems)
    assert any(p.startswith("'director'@'%'") and "INSERT" in p for p in problems)
    assert any(p.startswith("'frame_a'@'%'") and "UPDATE" in p for p in problems)


def _provision_failover_policy(server):
    """The bh-4biq8 provisioning: the policy table beside placement and the director's grant."""
    from beadhive.failover_policy import provision_statements

    root = server.connect()
    try:
        with root.cursor() as cursor:
            for statement in provision_statements(
                {"placement_writer": {"user": "director", "database": DB}}
            ):
                cursor.execute(statement.replace("'<host>'", "'%'"))
            cursor.execute(f"GRANT SELECT ON {DB}.hq_live_failover_policy TO 'frame_a'@'%'")
    finally:
        root.close()


def test_failover_after_rides_the_placement_cas_and_the_row_stays_receiver_shaped(server):
    """bh-4biq8: ``place_cas(failover_after=...)`` writes the hive's override in the CAS's own
    transaction; a refused value writes neither; the placement row keeps the strict five-field
    receiver carrier (Φ3 rollback); frames read the policy and cannot write it; the director's
    extra grant is conformant."""
    import pymysql

    from beadhive.failover_policy import read_policy
    from beadhive.hq_sql_placement import PlacementError

    _provision_failover_policy(server)
    seeded = server.revision()
    conn = server.connect(DIRECTOR, autocommit=False)
    try:
        with pytest.raises(PlacementError, match="below the executor floor"):
            place_cas(
                conn,
                prefix="ah",
                lease=_held("host-a", 22),
                authority=AUTHORITY,
                expected_revision=seeded,
                failover_after={"executor": 1200},
            )
        assert server.revision() == seeded  # refused: the placement did not move either
        placed = place_cas(
            conn,
            prefix="ah",
            lease=_held("host-a", 22),
            authority=AUTHORITY,
            expected_revision=seeded,
            failover_after={"executor": 4500, "transient": 2400},
        )
    finally:
        conn.close()
    frame = server.connect(FRAME, autocommit=False)
    try:
        row = read_row(frame, "ah")  # the strict reader the receiver shares
        assert row.revision == placed.revision and row.lease.epoch == 22
        with frame.cursor() as cursor:
            cursor.execute("START TRANSACTION")
            policy = read_policy(cursor)
            assert policy.failover_after("ah", "executor") == 4500.0
            assert policy.failover_after("ah", "transient") == 2400.0
            with pytest.raises(pymysql.err.OperationalError):
                cursor.execute("UPDATE hq_live_failover_policy SET seconds=60 WHERE scope='ah'")
    finally:
        frame.close()
    root = server.connect()
    try:
        with root.cursor() as cursor:
            # Not committed: the runtime database's `hq_live_*` ignore rule covers it, so the
            # policy never lands in a Dolt commit (no head move, no authority renewal).
            cursor.execute("SELECT COUNT(*) FROM dolt_log")
            commits = cursor.fetchone()[0]
            frames = {"'frame_a'@'%'": (INBOX,)}
            assert conformance(cursor, database=DB, director="'director'@'%'", frames=frames) == []
            cursor.execute("SELECT COUNT(*) FROM dolt_log")
            assert cursor.fetchone()[0] == commits
    finally:
        root.close()
