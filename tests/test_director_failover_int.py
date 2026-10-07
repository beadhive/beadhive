"""``bh hq placement`` and the director failover loop against a throwaway Dolt sql-server.

bh-16347.5. A private scratch server only — never a live HQ. The server carries the product
schema (committed registry, protected live tables), a director account with ``SELECT`` and
``UPDATE`` on placement only, and per-incarnation session tables stamped by the server clock.
The operator-signed authority is stubbed (its verification is ``hq_sql_runtime``'s, covered
elsewhere); everything else — the seed INSERT, the CAS, ``read_rows``, session staleness by
``UTC_TIMESTAMP(6)``, grants and triggers — is the real server.
"""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest

from beadhive import hq_placement_ops
from beadhive.director_failover import build_loop
from beadhive.failover_observer import session_table
from beadhive.hq_sql_placement import PlacementError, SqlPlacementDirector
from beadhive.hq_sql_runtime_schema import (
    COMMITTED_SCHEMA,
    PROTECTED_LIVE_SCHEMA,
    evidence_table,
    inbox_ddl,
)
from beadhive.kernel.daemon.contracts.config import DaemonFailoverConfig
from test_placement_authority_int import HqServer

pytestmark = [
    pytest.mark.integration,
    pytest.mark.dolt_server,
    pytest.mark.skipif(shutil.which("dolt") is None, reason="dolt not installed"),
]

DB = "beadhive_hq_runtime"
DIRECTOR = ("director", "director-secret")
FRAMES = {"frame-a": "fa", "frame-b": "fb"}


def _authority(frame):
    return {
        "holder_identity": f"host-{frame}",
        "instance_ref": f"vm-{frame}",
        "epoch": 1,
        "config_revision": "desired-1",
    }


STATE = {
    "frames": {
        frame: {"active": {"authority": _authority(frame), "state": "active", "cordoned": False}}
        for frame in FRAMES
    }
}
POLICIES = {"ah": {"config_revision": "desired-1"}, "bh": {"config_revision": "desired-1"}}


class Server(HqServer):
    def connect(self, who=("root", ""), *, autocommit=True, database=DB):
        return super().connect(who, autocommit=autocommit, database=database)

    def provision(self):
        root = self.connect(database=None)
        with root.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {DB}")
        root.close()
        root = self.connect()
        with root.cursor() as cursor:
            for statement in (*COMMITTED_SCHEMA, *PROTECTED_LIVE_SCHEMA):
                cursor.execute(statement)
            for frame, principal in FRAMES.items():
                authority = _authority(frame)
                cursor.execute(
                    "INSERT INTO hq_principal_registry VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        principal,
                        frame,
                        authority["holder_identity"],
                        authority["instance_ref"],
                        1,
                        f"hq_live_inbox_{principal}_1",
                        f"SHA256:{principal}",
                    ),
                )
            cursor.execute("CALL DOLT_COMMIT('-am','registry')")
            for principal in FRAMES.values():
                cursor.execute(inbox_ddl(principal, 1))
                table = session_table(principal, 1)
                cursor.execute(
                    f"CREATE TABLE {table} (id TINYINT PRIMARY KEY, "
                    "renewed_at DATETIME(6) NOT NULL, CHECK (id = 1))"
                )
                cursor.execute(f"CREATE USER '{principal}'@'%' IDENTIFIED BY 'secret'")
                cursor.execute(f"GRANT SELECT ON {DB}.hq_live_hive_leases TO '{principal}'@'%'")
                cursor.execute(
                    f"GRANT SELECT, INSERT ON {DB}.hq_live_inbox_{principal}_1 TO '{principal}'@'%'"
                )
                cursor.execute(f"GRANT SELECT, UPDATE ON {DB}.{table} TO '{principal}'@'%'")
            cursor.execute(f"CREATE USER '{DIRECTOR[0]}'@'%' IDENTIFIED BY '{DIRECTOR[1]}'")
            cursor.execute(f"GRANT SELECT ON {DB}.* TO 'director'@'%'")
            cursor.execute(f"GRANT UPDATE ON {DB}.hq_live_hive_leases TO 'director'@'%'")
        root.close()

    def sql(self, sql, params=None):
        conn = self.connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.fetchall()
        finally:
            conn.close()

    def renew(self, principal, age_s):
        table = session_table(principal, 1)
        self.sql(
            f"REPLACE INTO {table} VALUES "
            f"(1, DATE_SUB(UTC_TIMESTAMP(6), INTERVAL {int(age_s)} SECOND))"
        )

    def director(self):
        director = SqlPlacementDirector(
            {"placement_writer": {"user": "director", "database": DB}},
            connect=lambda binding: self.connect(DIRECTOR, autocommit=False),
        )
        director._verified = lambda cursor: (STATE, POLICIES)  # the signed authority, stubbed
        return director


@pytest.fixture
def server(tmp_path):
    server = Server(tmp_path / "hq")
    server.start()
    try:
        server.provision()
        yield server
    finally:
        server.stop()


def _seed(server, prefix, epoch):
    """The operator runs the rendered statement verbatim on the provisioning account."""
    out = hq_placement_ops.seed(prefix, epoch=epoch, director=server.director())
    server.sql(out["statement"].rstrip(";"))


def test_seed_show_place_release_round_trip(server):
    director = server.director()
    assert hq_placement_ops.show(director, "ah") == {"prefix": "ah", "seeded": False}
    _seed(server, "ah", 21)
    with pytest.raises(PlacementError, match="already seeded"):
        hq_placement_ops.seed("ah", epoch=21, director=director)
    seeded = hq_placement_ops.show(director, "ah")
    assert seeded["seeded"] and seeded["released"] and seeded["epoch"] == 21
    preview = hq_placement_ops.place(director, "ah", frame="frame-a", expected=seeded["revision"])
    assert preview["would_place"]["epoch"] == 22
    assert hq_placement_ops.show(director, "ah")["revision"] == seeded["revision"]  # dry run
    placed = hq_placement_ops.place(
        director, "ah", frame="frame-a", expected=seeded["revision"], confirm=True
    )["placed"]
    assert placed["host_id"] == "host-frame-a" and placed["epoch"] == 22
    with pytest.raises(PlacementError, match="moved"):
        hq_placement_ops.place(director, "ah", frame="frame-b", expected=seeded["revision"])
    released = hq_placement_ops.release(director, "ah", expected=placed["revision"], confirm=True)[
        "released"
    ]
    assert released["released"] and released["epoch"] == 22
    assert hq_placement_ops.show(director, "ah")["revision"] == released["revision"]


def test_the_director_cannot_seed_and_frames_cannot_place(server):
    import pymysql

    out = hq_placement_ops.seed("ch", epoch=1)
    for who in (DIRECTOR, ("fa", "secret")):
        conn = server.connect(who)
        try:
            with conn.cursor() as cursor, pytest.raises(pymysql.err.OperationalError):
                cursor.execute(out["statement"].rstrip(";"))
        finally:
            conn.close()


def test_conformance_check_on_the_server(server):
    def as_root(binding):
        return server.connect(autocommit=False)

    settings = {"placement_writer": {"user": "director", "database": DB}}
    out = hq_placement_ops.check(settings, connect=as_root)
    assert out["conformant"], out["problems"]
    assert out["director_accounts"] == ["'director'@'%'"]
    assert out["frame_accounts"] == ["'fa'@'%'", "'fb'@'%'"]
    server.sql(f"GRANT UPDATE ON {DB}.hq_live_hive_leases TO 'fb'@'%'")
    out = hq_placement_ops.check(settings, connect=as_root)
    assert out["problems"] == [
        "'fb'@'%': frame holds ['UPDATE'] on beadhive_hq_runtime.hq_live_hive_leases"
    ]


def test_conformance_passes_an_m9_session_and_evidence_frame(server):
    """bh-owqdg: a frame provisioned with M9's session + evidence tables (stamp triggers,
    UPDATE grants) is conformant; a write right on another frame's evidence is still flagged."""
    from beadhive.hq_sql_session import evidence_ddl, session_ddl
    from beadhive.hq_sql_session_provision import provision_liveness_schema

    root = server.connect()
    with root.cursor() as cursor:
        cursor.execute(
            "INSERT INTO hq_principal_registry VALUES (%s,%s,%s,%s,%s,%s,%s)",
            ("fc", "frame-c", "host-frame-c", "vm-frame-c", 1, session_table("fc", 1), "SHA256:fc"),
        )
        cursor.execute("CALL DOLT_COMMIT('-am','registry fc')")
        provision_liveness_schema(cursor)
        for statement in (*session_ddl("fc", 1), *evidence_ddl("fc", 1)):
            cursor.execute(statement)
        cursor.execute("CREATE USER 'fc'@'%' IDENTIFIED BY 'secret'")
        for table in (session_table("fc", 1), evidence_table("fc", 1)):
            cursor.execute(f"GRANT SELECT, UPDATE ON {DB}.{table} TO 'fc'@'%'")
    root.close()

    def as_root(binding):
        return server.connect(autocommit=False)

    settings = {"placement_writer": {"user": "director", "database": DB}}
    out = hq_placement_ops.check(settings, connect=as_root)
    assert out["conformant"], out["problems"]
    assert "'fc'@'%'" in out["frame_accounts"]
    server.sql(f"GRANT UPDATE ON {DB}.{evidence_table('fc', 1)} TO 'fa'@'%'")
    out = hq_placement_ops.check(settings, connect=as_root)
    assert out["problems"] == [
        f"'fa'@'%': frame holds ['UPDATE'] on {DB}.{evidence_table('fc', 1)}"
    ]


def test_the_loop_fails_a_dead_frame_over_on_the_server(server):
    director = server.director()
    _seed(server, "ah", 5)
    _seed(server, "bh", 9)
    rev = hq_placement_ops.show(director, "ah")["revision"]
    hq_placement_ops.place(director, "ah", frame="frame-a", expected=rev, confirm=True)
    rev = hq_placement_ops.show(director, "bh")["revision"]
    hq_placement_ops.place(director, "bh", frame="frame-b", expected=rev, confirm=True)
    server.renew("fa", 7200)  # frame-a's session row stopped renewing two hours ago
    server.renew("fb", 2)
    clock = SimpleNamespace(t=0.0)
    loop = build_loop(
        DaemonFailoverConfig(enabled=True, interval_seconds=60),
        hq_mode="dolt-server",
        director=director,
        clock=lambda: clock.t,
    )
    loop.director.monitor.failover_after = lambda frame: 300.0
    results = []
    for _ in range(10):
        clock.t += 120
        server.renew("fb", 2)
        results = loop.tick()
        if results:
            break
    assert [(r.prefix, r.from_frame, r.to_frame, r.outcome) for r in results] == [
        ("ah", "frame-a", "frame-b", "placed")
    ]
    assert clock.t > 300  # the observed window had to exceed failover_after first
    after = hq_placement_ops.show(director, "ah")
    assert after["frame_id"] == "frame-b" and after["epoch"] == 7 and after["source"] == "director"
    assert hq_placement_ops.show(director, "bh")["frame_id"] == "frame-b"  # live: untouched
    assert loop.tick() == []  # nothing left on the dead frame


def test_a_frame_without_a_session_table_never_fails_over(server):
    director = server.director()
    _seed(server, "ah", 1)
    rev = hq_placement_ops.show(director, "ah")["revision"]
    hq_placement_ops.place(director, "ah", frame="frame-a", expected=rev, confirm=True)
    server.sql(f"DROP TABLE {session_table('fa', 1)}")
    server.renew("fb", 1)
    clock = SimpleNamespace(t=0.0)
    loop = build_loop(
        DaemonFailoverConfig(enabled=True),
        hq_mode="dolt-server",
        director=director,
        clock=lambda: clock.t,
    )
    loop.director.monitor.failover_after = lambda frame: 60.0
    for _ in range(10):
        clock.t += 20
        assert loop.tick() == []
    assert hq_placement_ops.show(director, "ah")["frame_id"] == "frame-a"


def test_the_loop_uses_each_hives_failover_after_and_never_clamps_a_refused_row(server):
    """bh-4biq8: per-hive ``failover_after`` from HQ data. ``ah`` carries 45 min (set in its
    placement CAS) and fails over then; ``bh`` carries 1000 s, written raw below the 45 min
    floor, so it is refused — never clamped to the floor — and the 60 min default applies."""
    from beadhive.failover_policy import provision_statements

    for statement in provision_statements(
        {"placement_writer": {"user": "director", "database": DB}}
    ):
        server.sql(statement.replace("'<host>'", "'%'"))
    director = server.director()
    _seed(server, "ah", 1)
    _seed(server, "bh", 1)
    rev = hq_placement_ops.show(director, "ah")["revision"]
    hq_placement_ops.place(
        director, "ah", frame="frame-a", expected=rev, confirm=True,
        failover_after={"executor": 2700},
    )  # fmt: skip
    rev = hq_placement_ops.show(director, "bh")["revision"]
    hq_placement_ops.place(director, "bh", frame="frame-a", expected=rev, confirm=True)
    server.sql("INSERT INTO hq_live_failover_policy VALUES ('bh','executor',1000)")
    shown = hq_placement_ops.policy(director, "bh")["policy"]
    assert shown["effective"]["executor"] == 3600.0
    assert any("bh executor=1000 refused" in r for r in shown["refusals"])
    server.renew("fa", 9000)
    clock = SimpleNamespace(t=0.0)
    loop = build_loop(
        DaemonFailoverConfig(enabled=True, interval_seconds=60),
        hq_mode="dolt-server",
        director=director,
        clock=lambda: clock.t,
    )
    first = {}
    while clock.t < 3600 + 240 and len(first) < 2:
        clock.t += 60
        server.renew("fb", 2)
        for result in loop.tick():
            first.setdefault(result.prefix, (clock.t, result.outcome))
    assert first["ah"][1] == "placed" and 2700 < first["ah"][0] <= 2700 + 120
    assert first["bh"][1] == "placed" and 3600 < first["bh"][0] <= 3600 + 120
