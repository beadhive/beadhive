"""Spike bh-cvk70 evidence: HQ placement CAS authority and clock-independent failover.

Feeds ``docs/spikes/bh-cvk70-placement-authority-and-failover.md``. Everything here is
TEST-ONLY model code; no product module changes. The model is the proposal's
(``docs/design/hive-writer-partitioning-proposal.md`` §§1-3):

* **placement** ``{hive, frame, epoch}`` lives at ONE linearizable CAS point in HQ:
  ``refs/bh/lease/<prefix>`` via ``git push --force-with-lease`` (:mod:`beadhive.gitref`) in
  git mode, or one protected row updated by ``UPDATE ... WHERE revision=<expected>`` in
  dolt-server mode;
* **enforcement** is the hive's own Dolt data: a ``bh_writer`` row plus epoch retirement, so
  a writer keeps writing while HQ is down and time never gates a write;
* **time** only decides *when to fail over*, measured on the HQ side, never on the frame.

Scenarios exercised (proposal "Validation approach" table): **4** racing adopters and **7**
HQ unreachable for longer than the lease TTL, in both HQ modes, plus the half-done adopt
recovery (placement CAS'd, ``bh_writer`` not yet pushed) and the per-role ``failover_after``
defaults checked against bd's reclaim invariants.

Fully local: ``file://`` bare git remotes for HQ (as ``test_host_fence_int.py``),
``file://`` Dolt remotes for hive data, and a throwaway ``dolt sql-server`` on a free port for
dolt-server HQ. Racing adopters are real processes from ``harness.processes.process_context``.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from beadhive import gitref
from beadhive.host_lease_contracts import HostLease, lease_ref, now_stamp
from harness.processes import process_context
from harness.world import free_port

pytestmark = [
    pytest.mark.integration,
    pytest.mark.dolt_server,
    pytest.mark.skipif(shutil.which("dolt") is None, reason="dolt not installed"),
    pytest.mark.skipif(shutil.which("git") is None, reason="git not installed"),
]

PREFIX = "bh"
RACERS = 6

# ---------------------------------------------------------------------------------------------
# Hive data: a Dolt database behind a file:// remote, carrying the proposal's fence tables.
# ---------------------------------------------------------------------------------------------

FENCE_SCHEMA = (
    "CREATE TABLE bh_writer (id INT PRIMARY KEY, frame VARCHAR(64) NOT NULL, "
    "epoch BIGINT NOT NULL)",
    "CREATE TABLE bh_epoch_live (epoch BIGINT PRIMARY KEY)",
    "CREATE TABLE bh_write_mark (id VARCHAR(64) PRIMARY KEY, epoch BIGINT NOT NULL, "
    "FOREIGN KEY (epoch) REFERENCES bh_epoch_live(epoch))",
    "CREATE TABLE issues (id VARCHAR(64) PRIMARY KEY, title TEXT NOT NULL)",
)

# Variant for bh-vje85: the live epoch as ONE row, so two adopts both-modify the same cell.
SINGLETON_EPOCH_LIVE = (
    "CREATE TABLE bh_epoch_live (id INT PRIMARY KEY, epoch BIGINT NOT NULL UNIQUE)"
)


class Dolt:
    """The Dolt CLI with an isolated ``DOLT_ROOT_PATH`` (never the operator's global config)."""

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.env = {**os.environ, "DOLT_ROOT_PATH": str(root)}
        for key, value in (
            ("user.name", "spike"),
            ("user.email", "spike@example.invalid"),
            ("metrics.disabled", "true"),
        ):
            self(["config", "--global", "--add", key, value], root)

    def __call__(self, args, cwd, *, check=True):
        result = subprocess.run(
            [shutil.which("dolt"), *args],
            cwd=str(cwd),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if check:
            assert result.returncode == 0, (args, result.stdout, result.stderr)
        return result


class Clone:
    """One frame's local clone of the hive database."""

    def __init__(self, dolt: Dolt, path: Path, frame: str):
        self.dolt, self.path, self.frame = dolt, path, frame

    def sql(self, query: str, *, check=True):
        return self.dolt(["sql", "-q", query], self.path, check=check)

    def rows(self, query: str) -> list[list[str]]:
        out = self.dolt(["sql", "-r", "csv", "-q", query], self.path).stdout
        return list(csv.reader(io.StringIO(out)))[1:]

    def writer(self) -> tuple[str, int]:
        ((frame, epoch),) = self.rows("SELECT frame, epoch FROM bh_writer WHERE id = 1")
        return frame, int(epoch)

    def live_epochs(self) -> list[int]:
        return sorted(int(r[0]) for r in self.rows("SELECT epoch FROM bh_epoch_live"))

    def commit(self, message: str):
        self.dolt(["add", "-A"], self.path)
        self.dolt(["commit", "-m", message], self.path)

    def push(self):
        return self.dolt(["push", "origin", "main"], self.path, check=False)

    def sync_to_remote(self):
        """Adopt the remote head exactly (an adopter never merges into its own stale main)."""
        self.dolt(["fetch", "origin"], self.path)
        self.dolt(["reset", "--hard", "origin/main"], self.path)

    def may_write(self) -> bool:
        """The clock-free write gate: local ``bh_writer`` names this frame. No lease, no TTL."""
        return self.writer()[0] == self.frame

    def guarded_write(self, issue_id: str) -> bool:
        """One guarded ``main`` write (issue + write mark at the current epoch), then push.

        Returns whether the write landed on the remote. Refuses up front when the local
        ``bh_writer`` names another frame; never consults a clock."""
        if not self.may_write():
            return False
        self.sql(
            f"INSERT INTO issues VALUES ('{issue_id}', 'written by {self.frame}'); "
            "INSERT INTO bh_write_mark SELECT UUID(), epoch FROM bh_writer WHERE id = 1"
        )
        self.commit(f"{self.frame}: {issue_id}")
        return self.push().returncode == 0


@dataclass
class Hive:
    dolt: Dolt
    remote: Path
    root: Path

    def clone(self, frame: str) -> Clone:
        path = self.root / f"clone-{frame}"
        self.dolt(["clone", f"file://{self.remote}", str(path)], self.root)
        return Clone(self.dolt, path, frame)

    def head(self) -> Clone:
        """A fresh observer view of the remote's ``main``."""
        observer = self.root / "observer"
        if observer.exists():
            shutil.rmtree(observer)
        self.dolt(["clone", f"file://{self.remote}", str(observer)], self.root)
        return Clone(self.dolt, observer, "observer")


def make_hive(tmp_path: Path, *, frame: str = "A", epoch: int = 1, singleton=False) -> Hive:
    dolt = Dolt(tmp_path / "dolt-root")
    remote = tmp_path / "hive-remote"
    remote.mkdir()
    seed = tmp_path / "hive-seed"
    seed.mkdir()
    dolt(["init"], seed)
    schema = [
        SINGLETON_EPOCH_LIVE if "bh_epoch_live (" in t and singleton else t for t in FENCE_SCHEMA
    ]
    dolt(["sql", "-q", "; ".join(schema)], seed)
    live = f"(1, {epoch})" if singleton else f"({epoch})"
    dolt(
        [
            "sql",
            "-q",
            f"INSERT INTO bh_writer VALUES (1, '{frame}', {epoch}); "
            f"INSERT INTO bh_epoch_live VALUES {live}",
        ],
        seed,
    )
    dolt(["add", "-A"], seed)
    dolt(["commit", "-m", f"seed: {frame}@{epoch}"], seed)
    dolt(["remote", "add", "origin", f"file://{remote}"], seed)
    dolt(["push", "origin", "main"], seed)
    return Hive(dolt, remote, tmp_path)


def bump_sql(frame: str, epoch: int, *, singleton=False) -> str:
    """The proposal's adopt step 2, one commit: name the writer, retire every older epoch."""
    if singleton:
        return (
            f"UPDATE bh_writer SET frame = '{frame}', epoch = {epoch} WHERE id = 1; "
            f"DELETE FROM bh_write_mark WHERE epoch < {epoch}; "
            f"UPDATE bh_epoch_live SET epoch = {epoch} WHERE id = 1"
        )
    return (
        f"UPDATE bh_writer SET frame = '{frame}', epoch = {epoch} WHERE id = 1; "
        f"DELETE FROM bh_write_mark WHERE epoch < {epoch}; "
        f"DELETE FROM bh_epoch_live WHERE epoch < {epoch}; "
        f"INSERT INTO bh_epoch_live VALUES ({epoch})"
    )


def adopt_step2(clone: Clone, epoch: int, placement, *, before_push=None, attempts=4) -> str:
    """Placement-first adopt, step 2 (enforcement). Idempotent, so it is also the recovery.

    Each attempt starts from the remote head and checks, in order:
      1. the data: ``bh_writer.epoch`` already >= ours -> done (ours) or superseded (not ours);
      2. HQ: placement still names ``(frame, epoch)`` -> otherwise abandon (needs HQ);
    then commits the bump and pushes. A non-fast-forward rejection loops back to 1.
    """
    for _ in range(attempts):
        clone.sync_to_remote()
        current = clone.writer()
        if current == (clone.frame, epoch):
            return "landed"
        if current[1] >= epoch:
            return "abandoned: superseded in data"
        if placement() != (clone.frame, epoch):
            return "abandoned: placement moved"
        clone.sql(bump_sql(clone.frame, epoch))
        clone.commit(f"adopt {clone.frame}@{epoch}")
        if before_push is not None:
            hook, before_push = before_push, None
            hook()
        if clone.push().returncode == 0:
            return "landed"
    return "abandoned: retries exhausted"


# ---------------------------------------------------------------------------------------------
# Git-mode HQ: placement is the existing host-lease record at refs/bh/lease/<prefix>.
# ---------------------------------------------------------------------------------------------


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60, check=False
    )


def make_git_hq(tmp_path: Path) -> Path:
    remote = tmp_path / "hq.git"
    assert _git(["init", "--bare", "-q", str(remote)], tmp_path).returncode == 0
    return remote


def git_workdir(tmp_path: Path, name: str) -> Path:
    """A scratch repo whose object store holds the blobs a frame CASes into HQ."""
    path = tmp_path / f"gitwork-{name}"
    path.mkdir()
    assert _git(["init", "-q"], path).returncode == 0
    return path


def placement_record(frame: str, epoch: int) -> dict:
    """The EXISTING five-field host-lease shape. ``expires_at`` survives only as a failover
    hint; nothing in the write path reads it."""
    return HostLease(
        host_id=frame,
        label=frame,
        epoch=epoch,
        adopted_at=now_stamp(),
        expires_at=now_stamp(time.time() + 1800),
    ).to_record()


def git_placement(hq: Path, cwd: Path) -> tuple[str, tuple[str, int] | None]:
    sha, record = gitref.read_remote(str(hq), lease_ref(PREFIX), cwd=cwd)
    if record is None:
        return sha, None
    lease = HostLease.from_record(record)
    return sha, (lease.host_id, lease.epoch)


def git_place(hq: Path, cwd: Path, frame: str, epoch: int, expected: str) -> gitref.CasResult:
    return gitref.cas(
        str(hq),
        lease_ref(PREFIX),
        placement_record(frame, epoch),
        expected=expected,
        cwd=cwd,
    )


def _git_racer(hq: str, workdir: str, frame: str, expected: str, barrier, results) -> None:
    """One adopter process: everyone read the same placement, everyone CASes at once."""
    barrier.wait(timeout=60)
    result = gitref.cas(
        hq,
        lease_ref(PREFIX),
        placement_record(frame, 2),
        expected=expected,
        cwd=Path(workdir),
    )
    results.put((frame, result.ok, result.sha, result.detail))


# ---------------------------------------------------------------------------------------------
# dolt-server HQ: placement is one protected row; frames only read it.
# ---------------------------------------------------------------------------------------------

DIRECTOR = ("director", "director-secret")
FRAME_A = ("frame_a", "frame-a-secret")
FRAME_B = ("frame_b", "frame-b-secret")

HQ_SCHEMA = (
    # The CAS row. `revision` is a fresh UUID on EVERY write: Dolt merges concurrent
    # transactions cell by cell, so a write that leaves `revision` untouched can merge past a
    # competing CAS instead of conflicting (pinned below).
    "CREATE TABLE bh_placement (hive VARCHAR(64) PRIMARY KEY, frame VARCHAR(64) NOT NULL, "
    "epoch BIGINT UNSIGNED NOT NULL, revision CHAR(36) NOT NULL, "
    "note VARCHAR(64) NOT NULL DEFAULT '')",
    # Server-stamped liveness, one table per frame principal (no row-level grants exist).
    "CREATE TABLE bh_session_frame_a (id TINYINT PRIMARY KEY, renewed_at DATETIME(6) NOT NULL)",
    "CREATE TABLE bh_candidacy_frame_b (id INT AUTO_INCREMENT PRIMARY KEY, "
    "hive VARCHAR(64) NOT NULL, observed_epoch BIGINT UNSIGNED NOT NULL)",
    "INSERT INTO dolt_ignore VALUES ('bh_session_%', TRUE)",
    f"CREATE USER '{DIRECTOR[0]}'@'%' IDENTIFIED BY '{DIRECTOR[1]}'",
    f"CREATE USER '{FRAME_A[0]}'@'%' IDENTIFIED BY '{FRAME_A[1]}'",
    f"CREATE USER '{FRAME_B[0]}'@'%' IDENTIFIED BY '{FRAME_B[1]}'",
    f"GRANT SELECT, INSERT, UPDATE ON hq.bh_placement TO '{DIRECTOR[0]}'@'%'",
    f"GRANT SELECT ON hq.bh_session_frame_a TO '{DIRECTOR[0]}'@'%'",
    f"GRANT SELECT ON hq.bh_candidacy_frame_b TO '{DIRECTOR[0]}'@'%'",
    f"GRANT SELECT ON hq.bh_placement TO '{FRAME_A[0]}'@'%'",
    f"GRANT SELECT, UPDATE ON hq.bh_session_frame_a TO '{FRAME_A[0]}'@'%'",
    f"GRANT SELECT ON hq.bh_placement TO '{FRAME_B[0]}'@'%'",
    f"GRANT SELECT, INSERT ON hq.bh_candidacy_frame_b TO '{FRAME_B[0]}'@'%'",
)


class HqServer:
    """A throwaway ``dolt sql-server`` that can be stopped and restarted on the same data."""

    def __init__(self, root: Path):
        self.root = root
        (root / "data").mkdir(parents=True)
        self.port = free_port()
        self.env = {**os.environ, "DOLT_ROOT_PATH": str(root / "dolt-root")}
        (root / "dolt-root").mkdir()
        (root / "server.yaml").write_text(
            f"data_dir: {root / 'data'}\nlistener:\n  host: 127.0.0.1\n  port: {self.port}\n"
        )
        self.proc = None

    def start(self):
        log = (self.root / "server.log").open("a")
        self.proc = subprocess.Popen(
            [shutil.which("dolt"), "sql-server", "--config", str(self.root / "server.yaml")],
            cwd=self.root,
            env=self.env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        log.close()
        end = time.monotonic() + 30
        while True:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                if self.proc.poll() is not None or time.monotonic() >= end:
                    raise AssertionError("throwaway HQ dolt sql-server failed to start") from None
                time.sleep(0.1)

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
        self.proc = None

    def connect(self, who=("root", ""), *, autocommit=True, database="hq"):
        import pymysql

        return pymysql.connect(
            host="127.0.0.1",
            port=self.port,
            user=who[0],
            password=who[1],
            database=database,
            autocommit=autocommit,
            connect_timeout=3,
        )

    def provision(self):
        root = self.connect(database=None)
        with root.cursor() as cursor:
            cursor.execute("CREATE DATABASE hq")
        root.close()
        root = self.connect()
        with root.cursor() as cursor:
            for statement in HQ_SCHEMA:
                cursor.execute(statement)
            cursor.execute(
                "INSERT INTO bh_placement (hive, frame, epoch, revision) VALUES (%s,'A',1,%s)",
                (PREFIX, str(uuid.uuid4())),
            )
            cursor.execute("INSERT INTO bh_session_frame_a VALUES (1, UTC_TIMESTAMP(6))")
        root.close()

    def placement(self, who=DIRECTOR) -> tuple[str, int, str]:
        conn = self.connect(who)
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT frame, epoch, revision FROM bh_placement WHERE hive=%s", (PREFIX,)
                )
                frame, epoch, revision = cursor.fetchone()
                return frame, int(epoch), revision
        finally:
            conn.close()


def sql_place(server: HqServer, frame: str, epoch: int, expected_revision: str) -> bool:
    """The dolt-server placement CAS, issued with the DIRECTOR's credential.

    True only when exactly one row matched the expected revision AND the autocommit
    transaction committed. A 1213 serialization failure is a lost race, not a retry."""
    import pymysql

    conn = server.connect(DIRECTOR)
    try:
        with conn.cursor() as cursor:
            matched = cursor.execute(
                "UPDATE bh_placement SET frame=%s, epoch=%s, revision=%s "
                "WHERE hive=%s AND revision=%s",
                (frame, epoch, str(uuid.uuid4()), PREFIX, expected_revision),
            )
        return matched == 1
    except pymysql.err.OperationalError as exc:
        if exc.args[0] == 1213:
            return False
        raise
    finally:
        conn.close()


def _sql_racer(port: int, frame: str, expected: str, barrier, results) -> None:
    """One adopter process holding a director-issued CAS for itself."""
    import pymysql

    conn = pymysql.connect(
        host="127.0.0.1",
        port=port,
        user=DIRECTOR[0],
        password=DIRECTOR[1],
        database="hq",
        autocommit=True,
    )
    barrier.wait(timeout=60)
    try:
        with conn.cursor() as cursor:
            matched = cursor.execute(
                "UPDATE bh_placement SET frame=%s, epoch=epoch+1, revision=%s "
                "WHERE hive=%s AND revision=%s",
                (frame, str(uuid.uuid4()), PREFIX, expected),
            )
        results.put((frame, "matched" if matched == 1 else "no-match", None))
    except pymysql.err.OperationalError as exc:
        results.put((frame, "error", exc.args[0]))
    finally:
        conn.close()


@pytest.fixture
def hq_server(tmp_path):
    server = HqServer(tmp_path / "hq-server")
    server.start()
    try:
        server.provision()
        yield server
    finally:
        server.stop()


def _race(target, args_for, frames):
    ctx = process_context()
    barrier = ctx.Barrier(len(frames))
    results = ctx.Queue()
    procs = [ctx.Process(target=target, args=(*args_for(f), barrier, results)) for f in frames]
    for proc in procs:
        proc.start()
    outcomes = [results.get(timeout=120) for _ in frames]
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0
    return outcomes


# =============================================================================================
# Scenario 4 — two (or more) adopters race for the same epoch
# =============================================================================================


def test_s4_git_placement_cas_racing_adopters_exactly_one_wins(tmp_path):
    hq = make_git_hq(tmp_path)
    admin = git_workdir(tmp_path, "admin")
    assert git_place(hq, admin, "A", 1, gitref.ABSENT).ok
    seen, placed = git_placement(hq, admin)
    assert placed == ("A", 1)

    frames = [f"F{i}" for i in range(RACERS)]
    for frame in frames:
        git_workdir(tmp_path, frame)
    outcomes = _race(
        _git_racer,
        lambda f: (str(hq), str(tmp_path / f"gitwork-{f}"), f, seen),
        frames,
    )

    winners = [o for o in outcomes if o[1]]
    assert len(winners) == 1, outcomes
    winner_frame, _, winner_sha, _ = winners[0]
    final_sha, final = git_placement(hq, admin)
    assert final == (winner_frame, 2)
    assert final_sha == winner_sha
    # Every loser was told it lost (git's own verdict), and none was retried into a win.
    losers = [(ok, detail) for frame, ok, _sha, detail in outcomes if frame != winner_frame]
    assert len(losers) == RACERS - 1 and all(not ok and detail for ok, detail in losers)


def test_s4_sql_placement_cas_racing_adopters_exactly_one_wins(hq_server):
    for round_ in range(3):
        before_frame, before_epoch, expected = hq_server.placement()
        frames = [f"R{round_}F{i}" for i in range(RACERS)]
        outcomes = _race(_sql_racer, lambda f, e=expected: (hq_server.port, f, e), frames)
        winners = [o for o in outcomes if o[1] == "matched"]
        assert len(winners) == 1, outcomes
        # Losers either matched nothing (they read the committed winner) or were refused at
        # commit with Dolt's 1213 serialization failure. Never a second commit.
        assert all(o[1] == "no-match" or o[2] == 1213 for o in outcomes if o not in winners)
        frame, epoch, revision = hq_server.placement()
        assert (frame, epoch) == (winners[0][0], before_epoch + 1)
        assert revision != expected and frame != before_frame


def test_s4_sql_cas_needs_a_fresh_revision_on_every_write(hq_server):
    """Dolt merges concurrent transactions CELL by cell. Pin both halves of the rule."""
    import pymysql

    _, _, rev0 = hq_server.placement()
    renew, adopt = (
        hq_server.connect(DIRECTOR, autocommit=False),
        hq_server.connect(DIRECTOR, autocommit=False),
    )
    with renew.cursor() as r, adopt.cursor() as a:
        r.execute("START TRANSACTION")
        a.execute("START TRANSACTION")
        # A "renew" guarded by the old revision that does NOT rewrite it ...
        assert r.execute(
            "UPDATE bh_placement SET note='renewed' WHERE hive=%s AND revision=%s", (PREFIX, rev0)
        )
        assert a.execute(
            "UPDATE bh_placement SET frame='Z', epoch=epoch+1, revision=%s "
            "WHERE hive=%s AND revision=%s",
            (str(uuid.uuid4()), PREFIX, rev0),
        )
        adopt.commit()
        renew.commit()  # ... merges silently past the competing CAS: the hazard.
    frame, _, _ = hq_server.placement()
    assert frame == "Z"
    conn = hq_server.connect()
    with conn.cursor() as cursor:
        cursor.execute("SELECT note FROM bh_placement WHERE hive=%s", (PREFIX,))
        assert cursor.fetchone()[0] == "renewed"
    conn.close()

    _, _, rev1 = hq_server.placement()
    renew, adopt = (
        hq_server.connect(DIRECTOR, autocommit=False),
        hq_server.connect(DIRECTOR, autocommit=False),
    )
    with renew.cursor() as r, adopt.cursor() as a:
        r.execute("START TRANSACTION")
        a.execute("START TRANSACTION")
        r.execute(
            "UPDATE bh_placement SET note='renewed-2', revision=%s WHERE hive=%s AND revision=%s",
            (str(uuid.uuid4()), PREFIX, rev1),
        )
        a.execute(
            "UPDATE bh_placement SET frame='Y', epoch=epoch+1, revision=%s "
            "WHERE hive=%s AND revision=%s",
            (str(uuid.uuid4()), PREFIX, rev1),
        )
        adopt.commit()
        with pytest.raises(pymysql.err.OperationalError) as lost:
            renew.commit()  # rewriting `revision` turns the overlap into a real conflict
    assert lost.value.args[0] == 1213
    assert hq_server.placement()[0] == "Y"


def test_s4_sql_trust_boundary_grants_procedure_and_trigger(hq_server):
    """What each dolt-server design can trust, on the pinned server (research leads verified)."""
    import pymysql

    root = hq_server.connect()
    with root.cursor() as cursor:
        cursor.execute(
            "CREATE DEFINER='root'@'localhost' PROCEDURE noop_definer() SQL SECURITY DEFINER "
            "BEGIN SELECT 1; END"
        )
        cursor.execute(
            "CREATE DEFINER='root'@'localhost' PROCEDURE place_b() SQL SECURITY DEFINER "
            "BEGIN UPDATE bh_placement SET frame='B-proc' WHERE hive='bh'; END"
        )
        cursor.execute(f"GRANT EXECUTE ON hq.* TO '{FRAME_B[0]}'@'%'")
    frame_b = hq_server.connect(FRAME_B)
    with frame_b.cursor() as cursor:
        # (a) table-level grants ARE enforced: a frame cannot write placement directly.
        with pytest.raises(pymysql.err.OperationalError, match="command denied"):
            cursor.execute("UPDATE bh_placement SET frame='B' WHERE hive='bh'")
        # (b) SQL SECURITY DEFINER is parsed, EXECUTE works, but the body runs with the
        #     INVOKER's rights: a procedure cannot delegate placement writes (dolthub/dolt#10190).
        cursor.execute("CALL noop_definer()")
        with pytest.raises(pymysql.err.OperationalError, match="command denied"):
            cursor.execute("CALL place_b()")
        # (c) candidacy rows: the frame may write only its own candidacy table.
        cursor.execute("INSERT INTO bh_candidacy_frame_b (hive, observed_epoch) VALUES ('bh', 1)")
    assert hq_server.placement()[0] == "A"

    # (d) HAZARD: DML inside a trigger body is NOT checked against the invoker's write grant,
    #     so a trigger on ANY frame-writable table is a definer-like path into placement.
    with root.cursor() as cursor:
        cursor.execute(
            "CREATE TRIGGER candidacy_escalates AFTER INSERT ON bh_candidacy_frame_b "
            "FOR EACH ROW BEGIN UPDATE bh_placement SET frame='B-trigger' WHERE hive=NEW.hive; END"
        )
    with frame_b.cursor() as cursor:
        cursor.execute("INSERT INTO bh_candidacy_frame_b (hive, observed_epoch) VALUES ('bh', 1)")
    assert hq_server.placement()[0] == "B-trigger"
    frame_b.close()
    root.close()


def test_s4_loser_that_reached_step2_is_rejected_and_abandons(tmp_path):
    """A stale adopter (won epoch 2, stalled) races the current one (epoch 3) in the DATA.

    Both orders end with exactly one writer in data: the higher placement epoch."""
    hive = make_hive(tmp_path, frame="A", epoch=1)
    hq = make_git_hq(tmp_path)
    admin = git_workdir(tmp_path, "admin")
    assert git_place(hq, admin, "A", 1, gitref.ABSENT).ok

    def placement():
        return git_placement(hq, admin)[1]

    # B wins epoch 2 at HQ, then stalls before step 2; the director fails over to C at 3.
    sha_a, _ = git_placement(hq, admin)
    assert git_place(hq, admin, "B", 2, sha_a).ok
    b, c = hive.clone("B"), hive.clone("C")

    # --- order 1: B passed its HQ check (placement still B@2), then C re-placed + landed. ---
    def c_fails_over_and_lands():
        sha_b, _ = git_placement(hq, admin)
        assert git_place(hq, admin, "C", 3, sha_b).ok
        assert adopt_step2(c, 3, placement) == "landed"

    assert adopt_step2(b, 2, placement, before_push=c_fails_over_and_lands) == (
        "abandoned: superseded in data"
    )
    head = hive.head()
    assert head.writer() == ("C", 3)
    assert head.live_epochs() == [3]
    # The loser cannot write main afterwards: its gate reads the data, which names C.
    b.sync_to_remote()
    assert not b.may_write() and not b.guarded_write("b-after")
    assert c.guarded_write("c-1")


def test_s4_lower_epoch_landing_first_is_overtaken_and_retired(tmp_path):
    """Reverse order: the stale adopter's bump lands first; the current adopter retries on top
    of it and retires its epoch, so the stale adopter's writes are fenced."""
    hive = make_hive(tmp_path, frame="A", epoch=1)
    hq = make_git_hq(tmp_path)
    admin = git_workdir(tmp_path, "admin")
    assert git_place(hq, admin, "A", 1, gitref.ABSENT).ok
    sha_a, _ = git_placement(hq, admin)
    assert git_place(hq, admin, "B", 2, sha_a).ok
    b, c = hive.clone("B"), hive.clone("C")

    def placement():
        return git_placement(hq, admin)[1]

    def b_lands_first():
        # B checked placement (B@2) before the failover CAS below, and now pushes its bump.
        b.sync_to_remote()
        b.sql(bump_sql("B", 2))
        b.commit("adopt B@2")
        assert b.push().returncode == 0
        assert b.guarded_write("b-at-2")  # B is the data writer for this interval only

    sha_b, _ = git_placement(hq, admin)
    assert git_place(hq, admin, "C", 3, sha_b).ok
    assert adopt_step2(c, 3, placement, before_push=b_lands_first) == "landed"

    head = hive.head()
    assert head.writer() == ("C", 3)
    assert head.live_epochs() == [3]
    assert head.rows("SELECT COUNT(*) FROM bh_write_mark WHERE epoch < 3") == [["0"]]
    # B's next write is fenced: its push is non-fast-forward, and its pull of the bump fails
    # on the retired epoch's foreign key, so remote main never carries a stale write.
    assert b.may_write()
    assert not b.guarded_write("b-stale")
    pulled = b.dolt(["pull", "origin", "main"], b.path, check=False)
    assert pulled.returncode != 0
    assert "CONSTRAINT VIOLATION" in pulled.stdout + pulled.stderr
    assert b.rows("SELECT `table` FROM dolt_constraint_violations") == [["bh_write_mark"]]
    assert not b.may_write()  # the half-merged working set already names C
    assert b.push().returncode != 0
    after = hive.head()
    assert after.rows("SELECT id FROM issues WHERE id = 'b-stale'") == []
    assert after.writer() == ("C", 3)


@pytest.mark.parametrize(
    ("singleton", "conflicted", "live_after_merge"),
    [(False, {"bh_writer"}, [2, 3]), (True, {"bh_writer", "bh_epoch_live"}, [2])],
    ids=["epoch-pk-rows", "singleton-row"],
)
def test_two_adopts_merge_epoch_live_into_a_union_unless_it_is_a_singleton(
    tmp_path, singleton, conflicted, live_after_merge
):
    """Evidence for bh-vje85: with the proposal's ``bh_epoch_live(epoch PK)`` shape, merging two
    adopt commits conflicts ONLY on ``bh_writer``; ``bh_epoch_live`` silently becomes {2, 3}, so
    any resolution of the ``bh_writer`` conflict would keep BOTH epochs live. A singleton row
    turns the epoch itself into a second both-modified conflict."""
    hive = make_hive(tmp_path, frame="A", epoch=1, singleton=singleton)
    b, c = hive.clone("B"), hive.clone("C")
    for clone, epoch in ((b, 2), (c, 3)):
        clone.sql(bump_sql(clone.frame, epoch, singleton=singleton))
        clone.commit(f"adopt {clone.frame}@{epoch}")
    assert c.push().returncode == 0
    assert b.push().returncode != 0
    assert b.dolt(["pull", "origin", "main"], b.path, check=False).returncode != 0
    assert {row[0] for row in b.rows("SELECT `table` FROM dolt_conflicts")} == conflicted
    assert b.live_epochs() == live_after_merge  # "ours" side of a conflicted singleton
    b.dolt(["merge", "--abort"], b.path)
    b.sync_to_remote()
    assert b.writer() == ("C", 3) and b.live_epochs() == [3]


# =============================================================================================
# Half-done adopt: placement CAS'd, bh_writer not yet pushed
# =============================================================================================


def _writers(hive: Hive, clones: list[Clone]) -> list[str]:
    """Frames whose clock-free gate would allow a ``main`` write after their next sync."""
    allowed = []
    for clone in clones:
        clone.sync_to_remote()
        if clone.may_write():
            allowed.append(clone.frame)
    return allowed


def test_half_done_adopt_never_yields_two_writers(tmp_path):
    hive = make_hive(tmp_path, frame="A", epoch=1)
    hq = make_git_hq(tmp_path)
    admin = git_workdir(tmp_path, "admin")
    assert git_place(hq, admin, "A", 1, gitref.ABSENT).ok
    a, b, c = hive.clone("A"), hive.clone("B"), hive.clone("C")

    def placement():
        return git_placement(hq, admin)[1]

    # B wins placement@2 and crashes before step 2: placement says B, data still says A.
    sha_a, _ = git_placement(hq, admin)
    assert git_place(hq, admin, "B", 2, sha_a).ok
    assert _writers(hive, [a, b, c]) == ["A"]  # exactly one writer: the data's
    assert a.guarded_write("a-during-half-done")

    # Path 1 — B restarts: step 2 is idempotent and completes; A is fenced.
    assert adopt_step2(b, 2, placement) == "landed"
    assert adopt_step2(b, 2, placement) == "landed"  # re-running after success is a no-op
    assert _writers(hive, [a, b, c]) == ["B"]

    # Path 2 — the next placement crashes too, the director re-places past it, and the
    # late adopter comes back afterwards: it is superseded in data, never a second writer.
    sha_b, _ = git_placement(hq, admin)
    assert git_place(hq, admin, "C", 3, sha_b).ok  # C@3 placed, C crashes before step 2
    assert _writers(hive, [a, b, c]) == ["B"]
    sha_c, _ = git_placement(hq, admin)
    assert git_place(hq, admin, "A", 4, sha_c).ok  # director re-places to A@4
    assert adopt_step2(a, 4, placement) == "landed"
    assert adopt_step2(c, 3, placement) == "abandoned: superseded in data"
    assert _writers(hive, [a, b, c]) == ["A"]
    head = hive.head()
    assert head.writer() == ("A", 4) and head.live_epochs() == [4]


# =============================================================================================
# Scenario 7 — HQ unreachable for longer than the lease TTL
# =============================================================================================


def test_s7_git_hq_down_past_ttl_primary_keeps_writing_and_handoff_is_refused(tmp_path):
    hive = make_hive(tmp_path, frame="A", epoch=1)
    hq = make_git_hq(tmp_path)
    admin = git_workdir(tmp_path, "admin")
    assert git_place(hq, admin, "A", 1, gitref.ABSENT).ok
    sha_before, _ = git_placement(hq, admin)
    _, record = gitref.read_remote(str(hq), lease_ref(PREFIX), cwd=admin)
    lease = HostLease.from_record(record)
    a, b = hive.clone("A"), hive.clone("B")
    b_work = git_workdir(tmp_path, "B")

    # HQ disappears, and the clock runs past the lease TTL.
    parked = hq.with_name("hq.git.unreachable")
    hq.rename(parked)
    past_ttl = time.time() + 3 * 3600
    # Today's gate: the cached lease is expired, so guard_primary would degrade A to read-only.
    assert lease.is_expired(past_ttl) and not lease.held_by("A", past_ttl)

    # Proposed gate: A keeps writing — it reads only its own data, never a clock or HQ.
    for n in range(3):
        assert a.guarded_write(f"a-offline-{n}")
    assert len(hive.head().rows("SELECT id FROM issues WHERE id LIKE 'a-offline-%'")) == 3

    # Handoff is refused: B can neither read placement nor CAS it, so step 2 never starts.
    with pytest.raises(gitref.RemoteUnreachable):
        git_placement(hq, b_work)
    assert not git_place(hq, b_work, "B", 2, sha_before).ok
    with pytest.raises(gitref.RemoteUnreachable):
        adopt_step2(b, 2, lambda: git_placement(hq, b_work)[1])
    assert not b.may_write()

    # HQ returns: nothing moved while it was gone.
    parked.rename(hq)
    assert git_placement(hq, admin) == (sha_before, ("A", 1))
    assert hive.head().writer() == ("A", 1)


class FailoverObserver:
    """The director's failover trigger: time decides WHEN, never WHO may write.

    Staleness comes from the HQ server's own clock (``renewed_at`` is server-stamped). The
    director only counts staleness it OBSERVED while HQ was reachable, on its own monotonic
    clock, so an HQ outage never makes every frame look dead the moment HQ returns."""

    def __init__(self, failover_after: float):
        self.failover_after = failover_after
        self.observed_since: float | None = None

    def due(self, staleness: float | None) -> bool:
        now = time.monotonic()
        if staleness is None:  # HQ unreachable: observe nothing, decide nothing
            self.observed_since = None
            return False
        if self.observed_since is None:
            self.observed_since = now
        return min(staleness, now - self.observed_since) > self.failover_after


def _session_staleness(server: HqServer) -> float | None:
    import pymysql

    try:
        conn = server.connect(DIRECTOR)
    except pymysql.err.OperationalError:
        return None
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT TIMESTAMPDIFF(MICROSECOND, renewed_at, UTC_TIMESTAMP(6)) "
                "FROM bh_session_frame_a WHERE id = 1"
            )
            return int(cursor.fetchone()[0]) / 1e6
    finally:
        conn.close()


def _renew_session(server: HqServer) -> None:
    conn = server.connect(FRAME_A)
    with conn.cursor() as cursor:
        assert cursor.execute(
            "UPDATE bh_session_frame_a SET renewed_at = UTC_TIMESTAMP(6) WHERE id = 1"
        )
    conn.close()


def test_s7_sql_hq_down_past_ttl_primary_keeps_writing_and_failover_waits(tmp_path, hq_server):
    import pymysql

    failover_after = 2.0  # scaled down from minutes; the shape, not the number, is under test
    hive = make_hive(tmp_path, frame="A", epoch=1)
    a, b = hive.clone("A"), hive.clone("B")
    _renew_session(hq_server)
    frame, epoch, revision = hq_server.placement()
    assert (frame, epoch) == ("A", 1)

    observer = FailoverObserver(failover_after)
    assert not observer.due(_session_staleness(hq_server))

    # HQ goes down for longer than failover_after (standing in for "longer than the TTL").
    hq_server.stop()
    outage_started = time.monotonic()
    while time.monotonic() - outage_started < failover_after + 1.0:
        assert not observer.due(_session_staleness(hq_server))  # nothing observable
        with pytest.raises(pymysql.err.OperationalError):
            sql_place(hq_server, "B", 2, revision)  # handoff refused: no CAS point
        assert a.guarded_write(f"a-offline-{int(time.monotonic() * 1000)}")
        time.sleep(0.3)
    assert len(hive.head().rows("SELECT id FROM issues WHERE id LIKE 'a-offline-%'")) >= 2

    # HQ returns. The raw server-clock staleness now spans the outage ...
    hq_server.start()
    staleness = _session_staleness(hq_server)
    assert staleness is not None and staleness > failover_after
    # ... so a naive "stale > failover_after" rule would evict a healthy primary, but the
    # observer has seen none of that time and decides nothing.
    assert not observer.due(staleness)
    _renew_session(hq_server)  # A renews on its normal cadence once HQ is back
    assert not observer.due(_session_staleness(hq_server))
    assert hq_server.placement()[:2] == ("A", 1)

    # Now A genuinely goes silent: failover fires only after failover_after OBSERVED time,
    # and only then does the director CAS placement; the write fence does the rest.
    silent_from = time.monotonic()
    while not observer.due(_session_staleness(hq_server)):
        assert time.monotonic() - silent_from < failover_after + 10
        time.sleep(0.2)
    assert time.monotonic() - silent_from >= failover_after
    _, _, revision = hq_server.placement()
    assert sql_place(hq_server, "B", 2, revision)
    assert not sql_place(hq_server, "C", 2, revision)  # the same expectation cannot win twice

    def placement():
        return hq_server.placement()[:2]

    assert adopt_step2(b, 2, placement) == "landed"
    assert not a.guarded_write("a-after-failover")  # push rejected: non-fast-forward
    a.sync_to_remote()
    assert not a.may_write()


# =============================================================================================
# failover_after per role, checked against bd's reclaim invariants
# =============================================================================================

# bd 1.3.0 (gastownhall/beads f45b249ce): internal/storage/issueops/lease.go DefaultLeaseTTL,
# cmd/bd/reclaim.go `--older-than` default 2 x DefaultLeaseTTL.
BD_LEASE_TTL = 5 * 60
BD_RECLAIM_GRACE = 2 * BD_LEASE_TTL

# Proposed session cadence (bh-wtsrc shape): renew every 60 s; stale after a TTL that must
# exceed the bridge's sync interval (bd's "TTL > sync interval"), hence longer in git mode.
SESSION_RENEW = 60
SESSION_TTL = {"git": 10 * 60, "dolt-server": 5 * 60}
# Cadence at which the director's view of a session can lag the frame: one HQ fetch in git
# mode (an assumed 5-minute poll), zero for a direct server-stamped row.
HQ_SYNC_INTERVAL = {"git": 5 * 60, "dolt-server": 0}

PROPOSED_FAILOVER_AFTER = {"executor": 60 * 60, "transient": 30 * 60, "viewer": None}


@pytest.mark.parametrize("mode", sorted(HQ_SYNC_INTERVAL))
@pytest.mark.parametrize("role", sorted(PROPOSED_FAILOVER_AFTER))
def test_proposed_failover_after_satisfies_bd_reclaim_invariants(role, mode):
    failover_after = PROPOSED_FAILOVER_AFTER[role]
    if failover_after is None:
        return  # a viewer is never placed, so it never needs failing over
    sync, session_ttl = HQ_SYNC_INTERVAL[mode], SESSION_TTL[mode]
    # bd's own two invariants, restated for the HQ bridge: TTL > sync, grace > sync.
    assert session_ttl > sync and session_ttl > SESSION_RENEW
    assert failover_after > sync
    # A frame is declared dead strictly after its session could have refreshed several times
    # across the bridge, so one slow fetch or a burst-accepting receiver never moves a hive.
    assert failover_after >= 2 * (session_ttl + sync)
    # Worker death is bd's job on the live primary: a dead worker's claim must be reclaimable
    # locally (TTL + grace) well before frame-level failover could move the whole hive.
    assert failover_after > BD_LEASE_TTL + BD_RECLAIM_GRACE
    # Live evidence (2026-10-04, beadhive-factory): accepted-heartbeat stalls of up to 35.4 min.
    # Executors, which own long-running sessions, must ride through that without failover.
    if role == "executor":
        assert failover_after > 35.4 * 60
