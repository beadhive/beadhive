"""Composed writer-partitioning prototype for the end-to-end spike (bh-jbb6r). Test-only.

The five merged spikes of molecule ``bh-qlgmm`` each proved one piece in isolation. This module
composes their recommendations into ONE prototype on the bh-eybn7 fixture
(:mod:`harness.writer_fencing`) so a single suite can drive all ten proposal scenarios and a
seeded randomized interleaving run against it:

* **Placement** (bh-cvk70): one linearizable CAS point in HQ. :class:`SqlHQ` is design A on an
  owned ``dolt sql-server`` (a director-held credential issues
  ``UPDATE bh_placement ... WHERE revision = <expected>`` with a fresh revision every write;
  frames hold ``SELECT`` only). :class:`GitHQ` is git mode: the existing five-field host-lease
  record at ``refs/bh/lease/<prefix>`` CAS'd with ``push --force-with-lease`` (:mod:`beadhive.
  gitref`, unchanged product code, called from here). Both duck-type the fixture's ``HQ`` so the
  fixture's ``Recorder`` and ``rewind`` keep working.
* **In-data fence** (bh-vje85 binding conditions 1-4): ``bh_writer {frame, epoch, revision}``,
  a singleton ``bh_epoch_live``, ``bh_write_mark.epoch`` a foreign key to it, monotonic
  ``BEFORE UPDATE`` triggers on both fence tables, and an adopt bump that also inserts an
  ``adopt-<epoch>`` sentinel mark. Marks are deleted only by adopt.
* **Write guard** (bh-sieai, as built): :func:`harness.write_guard.guard_ddl` -- one procedure
  plus 42 ``BEFORE`` triggers on 14 versioned bd tables, the mark written inline before the
  ``CALL`` -- installed on top of the fence tables above, plus a ``dolt_ignore``'d
  ``bh_local_ident`` per node. Adopt refuses to act as writer when the trigger set is short.
* **Non-primary writes** (bh-sieai): the forward path (:class:`Forwarder`, bd pointed at the
  primary's ``dolt sql-server``). Branch-role frames publish non-allocating edits to a
  frame-private data ref and the primary merges them deliberately (:func:`merge_branch`).
* **Failover** (bh-cvk70 R4/R5, bh-sieai T5): :class:`Director` counts only staleness it
  observed while HQ was reachable, on server-stamped session rows (bh-wtsrc); the new primary's
  adopt reverts the dead frame's ``in_progress`` beads inside the bump commit, so the revert is
  tied to the epoch and happens exactly once.
* **Liveness and evidence** (bh-wtsrc): per-frame ``frame_<id>_session`` / ``_evidence`` rows in
  :class:`SqlHQ`, stamped with server UTC by operator triggers, and a one-statement read-time
  eligibility.

:func:`check_invariants` judges remote ``main`` from the outside (bh-eybn7 Evidence 6: never on
``refs/dolt/data``) and :func:`run_schedule` is the seeded randomized interleaving driver shared
by the integration test (a few fixed seeds) and the soak script
(``tests/spikes/bh_jbb6r_soak.py``, at least 500 seeds, outside the land gate).
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from harness import epoch_fence as ef
from harness import write_guard as wg
from harness.writer_fencing import (
    BD_EMBEDDED,
    BD_SERVER,
    CAS,
    DOLT_CLI,
    Cluster,
    Frame,
    HQUnreachable,
    Placement,
    Run,
)

__all__ = [
    "ADOPT_PREFIX",
    "GUARD_TRIGGER_COUNT",
    "AdoptResult",
    "Director",
    "Forwarder",
    "GitHQ",
    "MoveLog",
    "Renewer",
    "SqlHQ",
    "World",
    "adopt",
    "bump_statements",
    "check_invariants",
    "composed_world",
    "fence_ddl",
    "install",
    "managed_push",
    "merge_branch",
    "run_schedule",
]

#: Commit messages the checker relies on. Adopt bumps and the deliberate merges (orphan, branch).
ADOPT_PREFIX = "bh: adopt "
MERGE_PREFIX = "bh: merge frame/"
#: bh-sieai's 42 guard triggers plus bh-vje85's two monotonic fence triggers.
GUARD_TRIGGER_COUNT = len(wg.trigger_names())
FENCE_TRIGGER_COUNT = GUARD_TRIGGER_COUNT + 2
NON_FF = "non-fast-forward"

# =============================================================================================
# The composed in-data fence + write guard
# =============================================================================================

_FENCE_SCHEMA = """
create table bh_writer (id int primary key, frame varchar(64) not null, epoch bigint not null,
  revision varchar(64) not null);
create table bh_epoch_live (id int primary key, epoch bigint not null,
  unique key bh_epoch_live_epoch (epoch));
create table bh_write_mark (id varchar(64) primary key, epoch bigint not null,
  tbl varchar(64) not null default '',
  constraint bh_write_mark_epoch foreign key (epoch) references bh_epoch_live (epoch));
insert ignore into dolt_ignore values ('bh_local_%', 1);
insert into bh_writer values (1, '{writer}', {epoch}, uuid());
insert into bh_epoch_live values (1, {epoch});
insert into bh_write_mark values ('adopt-{epoch}', {epoch}, 'bh_writer');
"""

_MONOTONIC = """
delimiter //
drop trigger if exists bh_writer_monotonic//
create trigger bh_writer_monotonic before update on bh_writer for each row
begin
  if new.epoch <= old.epoch then
    signal sqlstate '45000' set message_text = '{msg}';
  end if;
end//
drop trigger if exists bh_epoch_live_monotonic//
create trigger bh_epoch_live_monotonic before update on bh_epoch_live for each row
begin
  if new.epoch <= old.epoch then
    signal sqlstate '45000' set message_text = '{msg}';
  end if;
end//
delimiter ;
"""


def fence_ddl(writer: str, epoch: int) -> str:
    """The whole composed fence as one ``delimiter``-aware script.

    bh-vje85's fence tables (singleton live epoch, revisioned writer row, FK'd marks with an
    adopt sentinel) are created FIRST, so bh-sieai's guard script -- whose own
    ``create table if not exists`` for ``bh_writer`` / ``bh_write_mark`` then no-ops -- lands its
    procedure and 42 triggers on top of them unchanged. Its inline mark insert
    ``(uuid(), e, '<table>')`` fills the composed ``bh_write_mark (id, epoch, tbl)`` and is
    subject to the epoch foreign key."""
    return (
        _FENCE_SCHEMA.format(writer=writer, epoch=epoch)
        + wg.guard_ddl()
        + _MONOTONIC.format(msg=ef.MONOTONIC_REFUSAL)
    )


def bump_statements(frame: str, epoch: int, *, reclaim: bool = False) -> list[str]:
    """Adopt step 2 as ONE commit (bh-vje85 condition 1-2): name the writer with a fresh
    revision, retire every older epoch's marks, move the singleton live epoch, insert the
    ``adopt-<epoch>`` sentinel.

    ``reclaim`` (failover only, bh-cvk70 R5 / bh-sieai T5): in the same commit, revert every
    ``in_progress`` bead to ``open`` with no assignee, exactly what ``bd unclaim --force`` leaves
    (measured). Under the forward path the dead primary granted every live claim, and bd's lease
    rows are node-local, so nothing else can. The UPDATE is a guarded write on the new writer and
    stamps its mark at the new epoch."""
    statements = ef.bump_statements(frame, epoch)
    statements[-1] = f"INSERT INTO bh_write_mark VALUES ('adopt-{epoch}', {epoch}, 'bh_writer')"
    if reclaim:
        statements.append(
            "UPDATE issues SET status = 'open', assignee = '', started_at = NULL "
            "WHERE status = 'in_progress'"
        )
    return statements


def trigger_count(frame: Frame) -> int:
    rows = frame.query(
        "select count(*) n from information_schema.triggers "
        "where trigger_schema = database() and trigger_name like 'bh_%'"
    )
    return int(rows[0]["n"])


def install(cluster: Cluster, writer: str) -> int:
    """Place ``writer`` at HQ, install the composed fence + guard from it, publish, and provision
    every frame's identity (right after join, before any bd write: bh-sieai Evidence 6)."""
    epoch = cluster.hq.place(writer).epoch
    founder = cluster[writer]
    founder.pull().check()
    wg.run_sql_script(founder, fence_ddl(writer, epoch), "fence.sql").check()
    founder.commit("bh: install composed fence and write guard").check()
    founder.push().check()
    for frame in cluster.frames.values():
        if frame is not founder:
            frame.pull().check()
        wg.provision(frame)
    return epoch


def local_writer(frame: Frame) -> tuple[str, int]:
    return ef.local_writer(frame)


def in_progress(frame: Frame) -> set[str]:
    return {r["id"] for r in frame.query("select id from issues where status = 'in_progress'")}


@dataclass
class AdoptResult:
    outcome: str
    attempts: int = 0
    seconds: float = 0.0
    reclaimed: list[str] = field(default_factory=list)

    @property
    def landed(self) -> bool:
        return self.outcome == "landed"


def adopt(
    cluster: Cluster,
    frame: Frame,
    epoch: int,
    *,
    reclaim: bool = False,
    before_push: Callable[[], None] | None = None,
    attempts: int = 4,
) -> AdoptResult:
    """Placement-first, idempotent adopt step 2 (bh-cvk70 R3, bh-vje85 R2) on the composed fence.

    Every attempt starts from the remote head and stops when the data already holds ``>= epoch``
    (``landed`` if it names this frame), when HQ is unreachable or names someone else, or when
    this node's guard is short of the full trigger set (bh-sieai R1: a frame short of them
    refuses to act as writer). Otherwise it commits the bump (+ reclaim) and pushes; a
    non-fast-forward rejection loops back to the data check."""
    started = time.monotonic()
    result = AdoptResult("abandoned: retries exhausted")
    for attempt in range(1, attempts + 1):
        result.attempts = attempt
        try:
            ef.sync_to_remote(frame)
        except AssertionError as exc:
            result.outcome = f"abandoned: remote unreachable ({str(exc).splitlines()[0][:80]})"
            break
        current = local_writer(frame)
        if current == (frame.name, epoch):
            result.outcome = "landed"
            break
        if current[1] >= epoch:
            result.outcome = "abandoned: superseded in data"
            break
        try:
            placement = cluster.hq.placement(asker=frame.name)
        except HQUnreachable:
            result.outcome = "abandoned: hq unreachable"
            break
        if (placement.writer, placement.epoch) != (frame.name, epoch):
            result.outcome = "abandoned: placement moved"
            break
        if trigger_count(frame) < FENCE_TRIGGER_COUNT:
            result.outcome = "refused: guard incomplete"
            break
        reverting = sorted(in_progress(frame)) if reclaim else []
        ef.run_script(frame, bump_statements(frame.name, epoch, reclaim=reclaim)).check()
        frame.commit(f"{ADOPT_PREFIX}{frame.name}@{epoch}").check()
        if before_push is not None:
            hook, before_push = before_push, None
            hook()
        if frame.push().ok:
            result.outcome = "landed"
            result.reclaimed = reverting
            break
    result.seconds = round(time.monotonic() - started, 3)
    return result


def write(frame: Frame, title: str) -> Run:
    """One tracker write that never raises: ``bd create`` on bd frames, INSERT + commit on a
    Dolt CLI frame (where a guard refusal surfaces from the INSERT)."""
    if frame.is_bd:
        return frame.create_issue(title)
    ident = f"{frame.cluster.prefix}-{frame.name}-{uuid.uuid4().hex[:8]}"
    inserted = frame.sql(
        "insert into issues (id, title, description, design, acceptance_criteria, notes) "
        f"values ('{ident}', '{title}', '', '', '', '')"
    )
    if not inserted.ok:
        return inserted
    return frame.commit(f"cli: {title}")


def _fetch(frame: Frame) -> None:
    if frame.kind == BD_SERVER:
        frame.bd("sql", "CALL DOLT_FETCH('origin')").check()
    else:
        frame.dolt("fetch", "origin").check()


def published(frame: Frame) -> bool:
    """Whether the frame's committed ``main`` is already contained in ``origin/main``."""
    row = frame.query("select dolt_merge_base('main', 'origin/main') = hashof('main') as published")
    return bool(row[0]["published"]) if row else True


def committed_writer(frame: Frame, rev: str = "main") -> tuple[str, int]:
    """``bh_writer`` as of a COMMIT: a refused server-mode pull leaves the new writer merged into
    the working set only (bh-vje85 E6), so the held epoch must be read from history."""
    row = frame.query(f"SELECT frame, epoch FROM bh_writer AS OF '{rev}' WHERE id = 1")[0]
    return row["frame"], int(row["epoch"])


def divert(frame: Frame) -> dict:
    """The managed rejoin (bh-vje85 E3/R3): fetch without merging; when ``origin/main`` names a
    newer epoch than the one this frame's committed ``main`` holds, publish its unpublished
    commits to a fresh ``frame/<id>/orphan-<epoch>-<n>`` branch and reset to the remote. Never
    merges, never pushes ``main``."""
    _fetch(frame)
    held = committed_writer(frame)[1]
    remote = committed_writer(frame, "origin/main")
    if remote[1] <= held:
        return {"diverted": False, "reason": "current", "remote": remote}
    branch = None
    if not published(frame):
        if frame.kind == BD_SERVER:
            frame.bd("dolt", "commit", "-m", "bh: commit working set before orphan diversion")
        branch = f"frame/{frame.name}/orphan-{held}-{uuid.uuid4().hex[:6]}"
        if frame.kind == BD_SERVER:
            frame.bd("sql", f"CALL DOLT_PUSH('origin', 'main:{branch}')").check()
        else:
            frame.dolt("push", "origin", f"main:{branch}").check()
    ef.sync_to_remote(frame)
    return {"diverted": branch is not None, "branch": branch, "remote": remote}


def orphan_branches(frame: Frame) -> list[str]:
    """Published orphan branches not yet merged into ``origin/main`` (after a fetch)."""
    _fetch(frame)
    out = []
    for row in frame.query("select name, hash from dolt_remote_branches"):
        name = row["name"].removeprefix("remotes/origin/")
        if "/orphan" not in name:
            continue
        merged = frame.query(
            f"select dolt_merge_base('{row['hash']}', 'origin/main') = '{row['hash']}' as m"
        )
        if not (merged and merged[0]["m"]):
            out.append(name)
    return sorted(out)


def prune_remote_branches(frame: Frame) -> None:
    """Drop every remote-tracking branch, then fetch: after a rewind moved the remote BACKWARDS,
    no stale ``origin/*`` (main or an orphan from an earlier schedule) may survive."""
    for row in frame.query("select name from dolt_remote_branches"):
        name = row["name"].removeprefix("remotes/")
        frame.sql(f"CALL DOLT_BRANCH('-d', '-r', '-f', '{name}')")
    _fetch(frame)


def managed_push(frame: Frame) -> str:
    """bh's managed push on the composed fence (bh-vje85 R3).

    A server frame first commits its working set, so the trigger's mark rides with the write
    (bd's server-mode write commit leaves it unstaged: bh-vje85 E7). A rejected push fetches
    WITHOUT merging; if ``origin/main`` names a newer epoch the frame stops writing ``main`` and
    diverts its unpublished commits to an orphan branch (:func:`divert`). Returns ``pushed``,
    ``diverted``, ``reset`` (superseded with nothing unpublished), ``rejected`` or
    ``unreachable``."""
    if frame.kind == BD_SERVER:
        frame.bd("dolt", "commit", "-m", "bh: commit working set before managed push")
    pushed = frame.push()
    if pushed.ok:
        return "pushed"
    if "partition" in pushed.output:
        return "unreachable"
    try:
        diverted = divert(frame)
    except AssertionError:
        return "unreachable"
    if diverted["diverted"]:
        return "diverted"
    return "rejected" if diverted.get("reason") == "current" else "reset"


def merge_branch(primary: Frame, remote: str, branch: str, label: str) -> Run:
    """The primary's deliberate merge of a branch-path frame's published ``main`` (bh-sieai R2:
    a frame-private data ref). Merges never fire the guard; the commit carries the
    ``bh: merge frame/<label>`` message the invariant checker treats as a sanctioned merge."""
    message = f"{MERGE_PREFIX}{label}"
    if primary.kind == BD_SERVER:
        primary.bd("sql", f"CALL DOLT_FETCH('{remote}')").check()
        merge = f"CALL DOLT_MERGE('{remote}/{branch}', '--no-ff', '-m', '{message}')"
        return primary.bd("sql", merge)
    primary.dolt("fetch", remote).check()
    return primary.dolt("merge", "--no-ff", "-m", message, f"{remote}/{branch}")


# =============================================================================================
# HQ placement authorities (duck-typing harness.writer_fencing.HQ)
# =============================================================================================

_DIRECTOR = ("director", "director-fixture-only")
_READER = ("reader", "reader-fixture-only")
_FRAME_PASSWORD = "frame-fixture-only"
RELEASE = "sha256:" + "1" * 64

_ELIGIBILITY = """
SELECT
  g.state = 'active'                                                          AS granted,
  s.renewed_at > UTC_TIMESTAMP(6) - INTERVAL g.session_ttl_ms * 1000 MICROSECOND AS session_fresh,
  e.status = 'conformant' AND e.profile = g.profile                           AS evidence_pass,
  e.measured_at > UTC_TIMESTAMP(6) - INTERVAL g.evidence_ttl_ms * 1000 MICROSECOND
                                                                              AS evidence_unexpired,
  e.release_digest = g.release_digest                                         AS release_matches,
  p.frame = g.frame_id                                                        AS placed_here
FROM hq_grant AS g
JOIN frame_{frame}_session AS s ON s.id = 1
JOIN frame_{frame}_evidence AS e ON e.id = 1
LEFT JOIN bh_placement AS p ON p.hive = %s
WHERE g.frame_id = %s
"""
PREDICATES = (
    "granted",
    "session_fresh",
    "evidence_pass",
    "evidence_unexpired",
    "release_matches",
    "placed_here",
)


class _Reachability:
    """Per-frame cut between a frame and HQ (the fixture HQ's ``partition`` / ``heal``)."""

    def __init__(self) -> None:
        self._cut: set[str] = set()
        self._last = Placement(None, 0)

    def partition(self, frame: str) -> None:
        self._cut.add(frame)

    def heal(self, frame: str) -> None:
        self._cut.discard(frame)

    def reachable(self, frame: str) -> bool:
        return frame not in self._cut and self.up

    def _require(self, asker: str | None) -> None:
        if asker is not None and asker in self._cut:
            raise HQUnreachable(f"{asker} cannot reach HQ")

    up = True


class SqlHQ(_Reachability):
    """dolt-server HQ: bh-cvk70 design A placement plus bh-wtsrc session/evidence rows.

    One owned ``dolt sql-server`` (Dolt 2.3.5, private ``DOLT_ROOT_PATH`` and ``HOME``). The
    director principal alone may ``UPDATE bh_placement``; each frame principal may read placement
    and update only its own server-stamped session and evidence rows; ``reader`` selects only.
    ``down()`` stops the server (a whole-HQ outage); ``up()`` restarts it on the same data."""

    mode = "dolt-server"

    def __init__(
        self,
        root: Path,
        frames: Sequence[str],
        *,
        hive: str = "hive",
        session_ttl_ms: int = 300_000,
        evidence_ttl_ms: int = 900_000,
    ):
        super().__init__()
        self.root = root
        self.frames = tuple(frames)
        self.hive = hive
        self.session_ttl_ms = session_ttl_ms
        self.evidence_ttl_ms = evidence_ttl_ms
        self.port = 0
        self.proc: subprocess.Popen | None = None
        self.up = False

    # -- server lifecycle -------------------------------------------------------------------
    def start(self) -> SqlHQ:
        from harness.world import free_port

        dolt = shutil.which("dolt")
        if dolt is None:
            raise RuntimeError("dolt not on PATH")
        first = not (self.root / "data").exists()
        for sub in ("data", "dolt-root/.dolt", "home"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        if first:
            (self.root / "dolt-root" / ".dolt" / "config_global.json").write_text(
                json.dumps(
                    {
                        "user.name": "hq-operator",
                        "user.email": "hq@frames.invalid",
                        "metrics.disabled": "true",
                        "versioncheck.disabled": "true",
                    }
                )
            )
            self.port = free_port()
            (self.root / "server.yaml").write_text(
                f"data_dir: {self.root / 'data'}\n"
                f"listener:\n  host: 127.0.0.1\n  port: {self.port}\n"
            )
        env = {
            "PATH": os.environ.get("PATH", ""),
            "DOLT_ROOT_PATH": str(self.root / "dolt-root"),
            "HOME": str(self.root / "home"),
        }
        with (self.root / "server.log").open("a") as log:
            self.proc = subprocess.Popen(
                [dolt, "sql-server", "--config", str(self.root / "server.yaml")],
                cwd=self.root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + 30
        while True:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    break
            except OSError:
                if self.proc.poll() is not None or time.monotonic() >= deadline:
                    raise AssertionError("SqlHQ dolt sql-server failed to start") from None
                time.sleep(0.05)
        self.up = True
        if first:
            self._provision()
        return self

    def down(self) -> None:
        self.up = False
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
        self.proc = None

    def resume(self) -> None:
        """End a whole-HQ outage: restart the server on the same data and port."""
        self.start()

    def close(self) -> None:
        self.down()

    def connect(self, who: tuple[str, str] = ("root", ""), database: str | None = "hq"):
        import pymysql

        if not self.up:
            raise HQUnreachable("HQ is down")
        try:
            return pymysql.connect(
                host="127.0.0.1",
                port=self.port,
                user=who[0],
                password=who[1],
                database=database,
                autocommit=True,
                connect_timeout=3,
                read_timeout=15,
                write_timeout=15,
            )
        except pymysql.err.OperationalError as exc:
            raise HQUnreachable(f"HQ unreachable: {exc}") from exc

    def _exec(self, who, statement: str, args=None, *, database: str | None = "hq"):
        conn = self.connect(who, database)
        try:
            with conn.cursor() as cursor:
                matched = cursor.execute(statement, args)
                return matched, cursor.fetchall()
        finally:
            conn.close()

    def _provision(self) -> None:
        root = ("root", "")
        self._exec(root, "CREATE DATABASE hq", database=None)
        statements = [
            # ignore rules before any live table exists (bh-v0k3i ordering)
            "INSERT INTO dolt_ignore VALUES ('frame_%', TRUE)",
            "CREATE TABLE bh_placement (hive VARCHAR(64) PRIMARY KEY, frame VARCHAR(64), "
            "epoch BIGINT UNSIGNED NOT NULL, revision CHAR(36) NOT NULL)",
            "CREATE TABLE hq_grant (frame_id VARCHAR(64) PRIMARY KEY, state VARCHAR(16) NOT NULL, "
            "release_digest VARCHAR(80) NOT NULL, profile VARCHAR(64) NOT NULL, "
            "session_ttl_ms INT UNSIGNED NOT NULL, evidence_ttl_ms INT UNSIGNED NOT NULL)",
            f"INSERT INTO bh_placement VALUES ('{self.hive}', NULL, 0, '{uuid.uuid4()}')",
            f"CREATE USER '{_DIRECTOR[0]}'@'%' IDENTIFIED BY '{_DIRECTOR[1]}'",
            f"GRANT SELECT, UPDATE ON hq.bh_placement TO '{_DIRECTOR[0]}'@'%'",
            f"CREATE USER '{_READER[0]}'@'%' IDENTIFIED BY '{_READER[1]}'",
            f"GRANT SELECT ON hq.* TO '{_READER[0]}'@'%'",
        ]
        for frame in self.frames:
            statements += [
                f"INSERT INTO hq_grant VALUES ('{frame}', 'active', '{RELEASE}', 'factory', "
                f"{self.session_ttl_ms}, {self.evidence_ttl_ms})",
                f"CREATE TABLE frame_{frame}_session (id TINYINT PRIMARY KEY CHECK (id = 1), "
                "renewed_at DATETIME(6) NOT NULL)",
                f"CREATE TRIGGER frame_{frame}_session_ins BEFORE INSERT ON frame_{frame}_session "
                "FOR EACH ROW SET NEW.renewed_at = UTC_TIMESTAMP(6)",
                f"CREATE TRIGGER frame_{frame}_session_upd BEFORE UPDATE ON frame_{frame}_session "
                "FOR EACH ROW SET NEW.renewed_at = UTC_TIMESTAMP(6)",
                f"INSERT INTO frame_{frame}_session VALUES (1, '1970-01-02')",
                f"CREATE TABLE frame_{frame}_evidence (id TINYINT PRIMARY KEY CHECK (id = 1), "
                "release_digest VARCHAR(80) NOT NULL, profile VARCHAR(64) NOT NULL, "
                "status VARCHAR(32) NOT NULL, measured_at DATETIME(6) NOT NULL)",
                f"CREATE TRIGGER frame_{frame}_evidence_ins BEFORE INSERT ON frame_{frame}_evidence"
                " FOR EACH ROW SET NEW.measured_at = UTC_TIMESTAMP(6)",
                f"CREATE TRIGGER frame_{frame}_evidence_upd BEFORE UPDATE ON frame_{frame}_evidence"
                " FOR EACH ROW SET NEW.measured_at = UTC_TIMESTAMP(6)",
                f"INSERT INTO frame_{frame}_evidence VALUES (1, 'none', 'none', 'unknown', "
                "'1970-01-02')",
                f"CREATE USER 'frame_{frame}'@'%' IDENTIFIED BY '{_FRAME_PASSWORD}'",
                f"GRANT SELECT ON hq.bh_placement TO 'frame_{frame}'@'%'",
                f"GRANT SELECT ON hq.hq_grant TO 'frame_{frame}'@'%'",
                f"GRANT SELECT, UPDATE ON hq.frame_{frame}_session TO 'frame_{frame}'@'%'",
                f"GRANT SELECT, UPDATE ON hq.frame_{frame}_evidence TO 'frame_{frame}'@'%'",
                f"GRANT SELECT ON hq.frame_{frame}_session TO '{_DIRECTOR[0]}'@'%'",
            ]
        statements += [
            "CALL DOLT_ADD('bh_placement', 'hq_grant', 'dolt_ignore')",
            "CALL DOLT_COMMIT('-m', 'operator: placement, grants, ignore policy')",
        ]
        for statement in statements:
            self._exec(root, statement)

    @staticmethod
    def frame_principal(frame: str) -> tuple[str, str]:
        return (f"frame_{frame}", _FRAME_PASSWORD)

    # -- placement (design A) ---------------------------------------------------------------
    def _read(self, who, hive: str) -> tuple[Placement, str]:
        _, rows = self._exec(
            who, "SELECT frame, epoch, revision FROM bh_placement WHERE hive = %s", (hive,)
        )
        frame, epoch, revision = rows[0]
        return Placement(frame, int(epoch)), revision

    def placement(self, hive: str = "hive", *, asker: str | None = None) -> Placement:
        self._require(asker)
        who = self.frame_principal(asker) if asker is not None else _READER
        try:
            placement, _ = self._read(who, hive)
        except HQUnreachable:
            if asker is not None:
                raise
            return self._last  # the recorder's view during an outage: the last one it saw
        self._last = placement
        return placement

    def cas(self, writer: str, expected_revision: str, hive: str = "hive") -> bool:
        """The design-A CAS under the DIRECTOR's credential: True only when exactly one row
        matched the expected revision and the autocommit transaction committed. A 1213
        serialization failure is a lost race, never a retry."""
        import pymysql

        try:
            matched, _ = self._exec(
                _DIRECTOR,
                "UPDATE bh_placement SET frame = %s, epoch = epoch + 1, revision = %s "
                "WHERE hive = %s AND revision = %s",
                (writer, str(uuid.uuid4()), hive, expected_revision),
            )
        except pymysql.err.OperationalError as exc:
            if exc.args and exc.args[0] == 1213:
                return False
            raise
        return matched == 1

    def place(
        self,
        writer: str,
        hive: str = "hive",
        *,
        expected_epoch: int | None = None,
        asker: str | None = None,
    ) -> Placement:
        self._require(asker)
        current, revision = self._read(_DIRECTOR, hive)
        if expected_epoch is not None and current.epoch != expected_epoch:
            raise ValueError(f"stale placement: epoch {current.epoch} != {expected_epoch}")
        if not self.cas(writer, revision, hive):
            raise ValueError("stale placement: lost the placement CAS")
        placed, _ = self._read(_DIRECTOR, hive)
        self._last = placed
        return placed

    def restore(self, placement: Placement, hive: str = "hive") -> None:
        """Operator override after a rewind (root, not the director; fresh revision)."""
        self._exec(
            ("root", ""),
            "UPDATE bh_placement SET frame = %s, epoch = %s, revision = %s WHERE hive = %s",
            (placement.writer, placement.epoch, str(uuid.uuid4()), hive),
        )
        self._last = placement

    # -- liveness and evidence (bh-wtsrc) ---------------------------------------------------
    def renew(self, frame: str) -> None:
        self._require(frame)
        self._exec(self.frame_principal(frame), f"UPDATE frame_{frame}_session SET id = 1")

    def staleness(self, frame: str) -> float | None:
        """Seconds since ``frame`` last renewed, by the HQ server's clock; None if HQ is down."""
        try:
            _, rows = self._exec(
                _DIRECTOR,
                "SELECT TIMESTAMPDIFF(MICROSECOND, renewed_at, UTC_TIMESTAMP(6)) "
                f"FROM frame_{frame}_session WHERE id = 1",
            )
        except HQUnreachable:
            return None
        return int(rows[0][0]) / 1e6

    def publish_evidence(self, frame: str, status: str = "conformant") -> None:
        self._exec(
            self.frame_principal(frame),
            f"UPDATE frame_{frame}_evidence SET release_digest = %s, profile = 'factory', "
            "status = %s WHERE id = 1",
            (RELEASE, status),
        )

    def eligibility(self, frame: str, hive: str = "hive") -> dict[str, bool]:
        _, rows = self._exec(_READER, _ELIGIBILITY.format(frame=frame), (hive, frame))
        return {name: bool(value) for name, value in zip(PREDICATES, rows[0], strict=True)}


class GitHQ(_Reachability):
    """git-mode HQ: placement is the EXISTING host-lease record at ``refs/bh/lease/<prefix>``,
    CAS'd with ``push --force-with-lease`` by :func:`beadhive.gitref.cas` (bh-cvk70 E1-E3).
    ``expires_at`` is written as a failover hint only; nothing in the write path reads it.
    ``down()`` renames the bare repo away (bh-cvk70 E14)."""

    mode = "git"

    def __init__(self, root: Path, *, prefix: str = "fx"):
        super().__init__()
        self.root = root
        self.prefix = prefix
        self.repo = root / "hq.git"
        self.work = root / "hq-work"
        self._parked = root / "hq.git.down"

    def start(self) -> GitHQ:
        self.root.mkdir(parents=True, exist_ok=True)
        for args in (["init", "-q", "--bare", str(self.repo)], ["init", "-q", str(self.work)]):
            subprocess.run(["git", *args], check=True, capture_output=True)
        self.up = True
        return self

    def down(self) -> None:
        if self.repo.exists():
            self.repo.rename(self._parked)
        self.up = False

    def resume(self) -> None:
        if self._parked.exists():
            self._parked.rename(self.repo)
        self.up = True

    def close(self) -> None:
        self.resume()

    def _ref(self) -> str:
        from beadhive.host_lease_contracts import lease_ref

        return lease_ref(self.prefix)

    def _read(self) -> tuple[str, Placement]:
        from beadhive import gitref
        from beadhive.host_lease_contracts import HostLease

        try:
            sha, record = gitref.read_remote(str(self.repo), self._ref(), cwd=self.work)
        except gitref.RemoteUnreachable as exc:
            raise HQUnreachable(str(exc)) from exc
        if record is None:
            return sha, Placement(None, 0)
        lease = HostLease.from_record(record)
        return sha, Placement(lease.host_id, lease.epoch)

    def placement(self, hive: str = "hive", *, asker: str | None = None) -> Placement:
        self._require(asker)
        try:
            _, placement = self._read()
        except HQUnreachable:
            if asker is not None:
                raise
            return self._last
        self._last = placement
        return placement

    def _record(self, writer: str, epoch: int) -> dict:
        from beadhive.host_lease_contracts import HostLease, now_stamp

        return HostLease(
            host_id=writer,
            label=writer,
            epoch=epoch,
            adopted_at=now_stamp(),
            expires_at=now_stamp(time.time() + 1800),
        ).to_record()

    def place(
        self,
        writer: str,
        hive: str = "hive",
        *,
        expected_epoch: int | None = None,
        asker: str | None = None,
    ) -> Placement:
        from beadhive import gitref

        self._require(asker)
        sha, current = self._read()
        if expected_epoch is not None and current.epoch != expected_epoch:
            raise ValueError(f"stale placement: epoch {current.epoch} != {expected_epoch}")
        placed = Placement(writer, current.epoch + 1)
        result = gitref.cas(
            str(self.repo),
            self._ref(),
            self._record(writer, placed.epoch),
            expected=sha or gitref.ABSENT,
            cwd=self.work,
        )
        if not result.ok:
            raise ValueError(f"stale placement: {result.detail}")
        self._last = placed
        return placed

    def restore(self, placement: Placement, hive: str = "hive") -> None:
        from beadhive import gitref

        sha, _ = self._read()
        record = self._record(placement.writer or "", placement.epoch)
        assert gitref.cas(str(self.repo), self._ref(), record, expected=sha, cwd=self.work).ok
        self._last = placement


def placement_racer(mode: str, target: str, frame: str, expected: str, barrier, results) -> None:
    """One racing adopter process (module scope: spawn target). Everyone read the same placement
    token; everyone issues the single CAS at once. ``target`` is the HQ port (dolt-server) or
    the HQ repo path (git); ``frame`` gets its own scratch git dir for the record blob."""
    if mode == "dolt-server":
        import pymysql

        conn = pymysql.connect(
            host="127.0.0.1",
            port=int(target),
            user=_DIRECTOR[0],
            password=_DIRECTOR[1],
            database="hq",
            autocommit=True,
        )
        barrier.wait(timeout=60)
        try:
            with conn.cursor() as cursor:
                matched = cursor.execute(
                    "UPDATE bh_placement SET frame = %s, epoch = epoch + 1, revision = %s "
                    "WHERE hive = 'hive' AND revision = %s",
                    (frame, str(uuid.uuid4()), expected),
                )
            results.put((frame, matched == 1, "matched" if matched == 1 else "no-match"))
        except pymysql.err.OperationalError as exc:
            results.put((frame, False, f"error {exc.args[0]}"))
        finally:
            conn.close()
        return
    from beadhive import gitref
    from beadhive.host_lease_contracts import HostLease, lease_ref, now_stamp

    work = Path(target).parent / f"race-{frame}"
    work.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(work)], check=True, capture_output=True)
    _, record = gitref.read_remote(target, lease_ref("fx"), cwd=work)
    epoch = HostLease.from_record(record).epoch + 1 if record else 1
    lease = HostLease(
        host_id=frame,
        label=frame,
        epoch=epoch,
        adopted_at=now_stamp(),
        expires_at=now_stamp(time.time() + 1800),
    ).to_record()
    barrier.wait(timeout=60)
    result = gitref.cas(target, lease_ref("fx"), lease, expected=expected, cwd=work)
    results.put((frame, result.ok, result.detail[:120]))


def race_placement(hq: SqlHQ | GitHQ, frames: Sequence[str]) -> list[tuple[str, bool, str]]:
    """N spawned adopters CAS placement from the same observed token at once."""
    from harness.processes import process_context

    if isinstance(hq, SqlHQ):
        _, expected = hq._read(_DIRECTOR, hq.hive)
        target = str(hq.port)
    else:
        expected, _ = hq._read()
        target = str(hq.repo)
    ctx = process_context()
    barrier = ctx.Barrier(len(frames))
    results = ctx.Queue()
    procs = [
        ctx.Process(target=placement_racer, args=(hq.mode, target, f, expected, barrier, results))
        for f in frames
    ]
    for proc in procs:
        proc.start()
    outcomes = [results.get(timeout=120) for _ in frames]
    for proc in procs:
        proc.join(60)
    return outcomes


# =============================================================================================
# Director (failover trigger) and session renewers
# =============================================================================================


class Director:
    """The failover trigger (bh-cvk70 R4): time decides WHEN, never WHO may write.

    Staleness is the HQ server's own clock on the placed frame's session row (bh-wtsrc). Only
    staleness the director OBSERVED while HQ was reachable counts, on its monotonic clock, so an
    HQ outage never makes every frame look dead the moment HQ returns."""

    def __init__(self, hq: SqlHQ, failover_after: float, hive: str = "hive"):
        self.hq = hq
        self.failover_after = failover_after
        self.hive = hive
        self.observed_since: float | None = None
        self.last_seen: float | None = None
        #: A gap between two successful observations longer than this is an unobserved window
        #: (the director could not see HQ, or did not look), so the window restarts. Without it
        #: one look before an outage and one after would count the whole outage as observed
        #: (measured: a director that did not poll during a 5 s outage fired at once).
        self.max_gap = failover_after / 2

    def due(self) -> bool:
        placed = self.hq.placement(self.hive).writer
        staleness = self.hq.staleness(placed) if placed and self.hq.up else None
        now = time.monotonic()
        if staleness is None:
            self.observed_since = self.last_seen = None
            return False
        if self.last_seen is None or now - self.last_seen > self.max_gap:
            self.observed_since = now
        self.last_seen = now
        return min(staleness, now - self.observed_since) > self.failover_after

    def failover(self, to: str) -> Placement:
        current = self.hq.placement(self.hive)
        placed = self.hq.place(to, self.hive, expected_epoch=current.epoch)
        self.observed_since = self.last_seen = None
        return placed


def _renew_loop(port: int, frame: str, password: str, interval: float, stop: str) -> None:
    """A frame's liveness loop (module scope: spawn target). A no-op UPDATE; the operator
    trigger stamps server UTC. Dies with the frame (``Renewer.kill``). The stop signal is a
    plain file, not a multiprocessing Event: SIGKILLing a process parked in ``Event.wait``
    leaves the Event's lock held and the controller's later ``set()`` deadlocks (measured)."""
    import pymysql

    conn = None
    while not os.path.exists(stop):
        try:
            if conn is None:
                conn = pymysql.connect(
                    host="127.0.0.1",
                    port=port,
                    user=f"frame_{frame}",
                    password=password,
                    database="hq",
                    autocommit=True,
                    connect_timeout=2,
                )
            with conn.cursor() as cursor:
                cursor.execute(f"UPDATE frame_{frame}_session SET id = 1")
        except Exception:  # HQ down or restarting: keep trying on cadence
            with contextlib.suppress(Exception):
                if conn is not None:
                    conn.close()
            conn = None
        time.sleep(interval)


class Renewer:
    """One spawned renewal process per frame (``harness.processes``)."""

    def __init__(self, hq: SqlHQ, frame: str, interval: float):
        from harness.processes import process_context

        ctx = process_context()
        self.stop_file = hq.root / f"renewer-{frame}-{uuid.uuid4().hex[:6]}.stop"
        self.proc = ctx.Process(
            target=_renew_loop,
            args=(hq.port, frame, _FRAME_PASSWORD, interval, str(self.stop_file)),
            name=f"renewer-{frame}",
            daemon=True,
        )
        self.proc.start()

    def kill(self) -> None:
        """The frame's host died: its renewer dies with it (no graceful stop)."""
        if self.proc.is_alive():
            self.proc.kill()
        self.proc.join(10)

    def stop(self) -> None:
        self.stop_file.touch()
        self.proc.join(10)
        if self.proc.is_alive():
            self.kill()


# =============================================================================================
# Forward path (bh-sieai T5)
# =============================================================================================

_RELEASE_GATE = (
    "import os, sys, time\n"
    "gate = sys.argv[1]\n"
    "while not os.path.exists(gate):\n"
    "    time.sleep(0.0005)\n"
    "os.execvp('bd', ['bd', *sys.argv[2:]])\n"
)


class Forwarder:
    """A bd workspace on ``host`` whose metadata points at ``primary``'s ``dolt sql-server``:
    what a non-primary frame on the LAN does on the forward path. Writes execute on the
    primary, under the primary's guard and identity."""

    def __init__(self, host: Frame, primary: Frame, name: str = "forward"):
        if primary.kind != BD_SERVER:
            raise ValueError("the forward path needs a bd-server primary")
        self.dir = host.dir / name
        (self.dir / ".beads").mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir / ".beads", 0o700)
        self.repoint(primary)
        self.env = dict(host.env)

    def repoint(self, primary: Frame) -> None:
        meta = json.loads((primary.hive / ".beads" / "metadata.json").read_text())
        meta.update(
            {
                "dolt_mode": "server",
                "dolt_server_host": "127.0.0.1",
                "dolt_server_port": primary.port,
            }
        )
        (self.dir / ".beads" / "metadata.json").write_text(json.dumps(meta))
        self.node = primary.name

    def bd(self, *args: str, actor: str = "fwd", timeout: float = 120):
        env = dict(self.env, BEADS_ACTOR=actor, BEADS_NODE_ID=self.node)
        return subprocess.run(
            ["bd", *args],
            cwd=self.dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def race(self, bead: str, actors: Sequence[str], gate: Path) -> dict[str, int]:
        """One ``bd update --claim`` per actor, all released by one gate file."""
        procs = {
            actor: subprocess.Popen(
                [sys.executable, "-c", _RELEASE_GATE, str(gate)]
                + ["update", bead, "--claim", "--json"],
                cwd=self.dir,
                env=dict(self.env, BEADS_ACTOR=actor, BEADS_NODE_ID=self.node),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for actor in actors
        }
        time.sleep(0.3)
        gate.touch()
        out: dict[str, int] = {}
        for actor, proc in procs.items():
            proc.communicate(timeout=120)
            out[actor] = proc.returncode
        return out


def claim_won(row: dict, actor: str) -> bool:
    """bh's ``work_next.claim_won`` read-back semantics."""
    return row.get("assignee") == actor and str(row.get("status") or "") not in (
        "",
        "open",
        "closed",
    )


# =============================================================================================
# World: cluster + HQ + move log
# =============================================================================================


@dataclass
class Move:
    """One observed change of remote ``main``, attributed to the frame whose push made it."""

    op: str
    frame: str
    before: str | None
    after: str | None
    writer_after: tuple[str | None, int | None]


class MoveLog:
    """Outside-in record of remote ``main`` over time (invariants I1 and I2's time series)."""

    def __init__(self, cluster: Cluster):
        self.cluster = cluster
        self.moves: list[Move] = []
        self.epochs: list[tuple[str, int | None]] = []
        self._main, self._writer = self._read()

    def _read(self) -> tuple[str | None, tuple[str | None, int | None]]:
        view = self.cluster.remote.view(("bh_writer",))
        rows = view["tables"].get("bh_writer") or [{}]
        return view["main"], (rows[0].get("frame"), rows[0].get("epoch"))

    def observe(self, op: str, frame: str | None) -> Move | None:
        """Call after ``frame`` finished ``op`` (single-frame ops only)."""
        main, writer = self._read()
        self.epochs.append((op, writer[1]))
        move = None
        if main != self._main and frame is not None:
            move = Move(op, frame or "?", self._main, main, writer)
            self.moves.append(move)
        self._main, self._writer = main, writer
        return move

    def landed(self, op: str, frame: Frame) -> None:
        """Attribute a push that returned ok on ``frame`` during a concurrent op: the commit it
        published is its local ``main``; the frame named on THAT commit must be the pusher."""
        head = frame.local_main()
        if head == self._main:
            return  # an "ok" push with nothing new (Everything up-to-date) moved nothing
        row = frame.query(f"select frame, epoch from bh_writer as of '{head}' where id = 1")
        writer = (row[0]["frame"], int(row[0]["epoch"])) if row else (None, None)
        self.moves.append(Move(op, frame.name, None, head, writer))

    def resync(self) -> None:
        self._main, self._writer = self._read()


@dataclass
class World:
    cluster: Cluster
    hq: SqlHQ | GitHQ
    moves: MoveLog | None = None

    def __getitem__(self, name: str) -> Frame:
        return self.cluster[name]


@contextlib.contextmanager
def composed_world(
    root: Path,
    frames: Sequence[tuple[str, str]],
    *,
    hq_mode: str = "dolt-server",
    writer: str | None = None,
    session_ttl_ms: int = 300_000,
    evidence_ttl_ms: int = 900_000,
) -> Iterator[World]:
    """Build the fixture cluster, swap its HQ stand-in for a real placement authority, install
    the composed fence from ``writer`` (default: the founder) and provision every frame."""
    cluster = Cluster(root / "cluster", frames)
    if hq_mode == "dolt-server":
        hq: SqlHQ | GitHQ = SqlHQ(
            root / "hq",
            [name for name, _ in frames],
            session_ttl_ms=session_ttl_ms,
            evidence_ttl_ms=evidence_ttl_ms,
        )
    elif hq_mode == "git":
        hq = GitHQ(root / "hq", prefix=cluster.prefix)
    else:
        raise ValueError(f"unknown HQ mode {hq_mode!r}")
    hq.start()
    cluster.hq = hq  # type: ignore[assignment]  # duck-types harness.writer_fencing.HQ
    try:
        with cluster:
            first = writer or next((n for n, k in frames if k != DOLT_CLI), frames[0][0])
            install(cluster, first)
            world = World(cluster, hq)
            world.moves = MoveLog(cluster)
            yield world
    finally:
        hq.close()


# =============================================================================================
# Invariant checker
# =============================================================================================


def _observer_docs(cluster: Cluster, *queries: str) -> list[list[dict]]:
    """Run ``queries`` on the observer clone checked out at remote ``main``; one row list each."""
    observer = cluster.remote
    fetched = observer._dolt("fetch", "origin")
    if fetched.returncode:
        raise AssertionError(f"observer fetch failed: {fetched.stdout}{fetched.stderr}")
    observer._dolt("branch", "-f", "bh-check", "origin/main")
    script = "CALL DOLT_CHECKOUT('bh-check'); " + " ".join(f"{q};" for q in queries)
    res = observer._dolt("sql", "-r", "json", "-q", script)
    if res.returncode:
        raise AssertionError(f"checker query failed: {res.stdout}{res.stderr}")
    docs = [line for line in res.stdout.strip().splitlines() if line.startswith("{")]
    docs = docs[-len(queries) :]
    return [json.loads(doc).get("rows", []) for doc in docs]


@dataclass
class History:
    """Remote ``main``'s commit DAG with the fence rows as of every commit."""

    parents: dict[str, list[str]]
    message: dict[str, str]
    writer: dict[str, tuple[str, int]]
    live: dict[str, set[int]]
    marks: dict[str, list[int]]
    head: str

    def ancestors(self, commit: str) -> set[str]:
        seen: set[str] = set()
        stack = [commit]
        while stack:
            c = stack.pop()
            if c in seen:
                continue
            seen.add(c)
            stack.extend(self.parents.get(c, ()))
        return seen


def read_history(cluster: Cluster) -> History:
    log, ancestry, writers, live, marks, head = _observer_docs(
        cluster,
        "SELECT commit_hash, message FROM dolt_log",
        "SELECT commit_hash, parent_hash, parent_index FROM dolt_commit_ancestors",
        "SELECT commit_hash, frame, epoch FROM dolt_history_bh_writer",
        "SELECT commit_hash, epoch FROM dolt_history_bh_epoch_live",
        "SELECT commit_hash, epoch FROM dolt_history_bh_write_mark",
        "SELECT hashof('HEAD') h",
    )
    reachable = {row["commit_hash"] for row in log}
    parents: dict[str, list[str]] = {c: [] for c in reachable}
    for row in sorted(ancestry, key=lambda r: int(r["parent_index"])):
        if row["commit_hash"] in reachable and row.get("parent_hash"):
            parents[row["commit_hash"]].append(row["parent_hash"])
    live_map: dict[str, set[int]] = {}
    for row in live:
        live_map.setdefault(row["commit_hash"], set()).add(int(row["epoch"]))
    mark_map: dict[str, list[int]] = {}
    for row in marks:
        mark_map.setdefault(row["commit_hash"], []).append(int(row["epoch"]))
    return History(
        parents=parents,
        message={row["commit_hash"]: row["message"] for row in log},
        writer={r["commit_hash"]: (r["frame"], int(r["epoch"])) for r in writers},
        live=live_map,
        marks=mark_map,
        head=head[0]["h"],
    )


def check_invariants(
    world: World,
    *,
    adopt_in_flight: bool = False,
    acknowledged: Sequence[str] = (),
    forwarded: Sequence[str] = (),
    backups: Mapping[str, tuple[str, str]] | None = None,
    backup_remote: Path | None = None,
    dead_frames: Sequence[str] = (),
    history_check: bool = False,
) -> dict:
    """The bh-jbb6r invariant checker, judged on remote ``main`` (and HQ) only.

    * **I1 one writer per epoch**: every observed move of remote ``main`` was made by the frame
      that the moved-to ``main`` names in ``bh_writer`` (so per epoch, only that epoch's writer
      extended ``main``); and no epoch is named with two different frames anywhere in history.
    * **I2 epoch monotonic**: across every parent->child edge of ``main``'s DAG, and across the
      time series of observed remote ``main``, ``bh_writer.epoch`` never decreases; the head holds
      the highest epoch its history ever reached (bh-vje85 E12 ``epoch_regressed``).
    * **I3 no late epoch-e commit**: every non-adopt commit stamped with epoch ``e`` is an
      ancestor of every adopt commit with a higher epoch, unless it entered ``main`` only through
      a deliberate ``bh: merge frame/...`` merge made by that merge's writer (the orphan merge).
    * **I4 no retired mark in a merge**: no merge commit on ``main`` carries a ``bh_write_mark``
      row whose epoch is not that commit's live epoch (non-merge commits are checked too, as
      ``i4_any_commit``).
    * **audit**: bh-vje85's ``fence_audit``; ``placement_ahead`` is a violation unless an adopt is
      knowingly in flight.
    * **I5 no acknowledged write lost** (``acknowledged`` titles): every write whose push was
      acknowledged is still on remote ``main``.

    The product suite (M10, ``bh-7p7rf``) adds, each opt-in so the spikes' calls are unchanged:

    * **I6 no acknowledged forwarded write lost** (``forwarded`` titles): every write a
      forwarder's bd was told succeeded through the primary is on remote ``main`` (M13 E5: a
      write in flight across a divert reset must be refused, never acknowledged and dropped).
    * **I7 no lost backed-up work** (``backups``: bead -> ``(ref, sha)`` on the git
      ``backup_remote``): the backup ref still exists and still contains ``sha``, and the bead
      is still on remote ``main``.
    * **I8 no surviving unbacked claim of a dead frame** (``dead_frames``): no ``in_progress``
      bead on remote ``main`` is claimed (``claim-frame:<dead>``) by a dead frame unless it is
      submitted (``review:pending``) or its work is on a backup ref.
    * **history** (``history_check``): the PRODUCT ``beadhive.fence_audit`` on the observer,
      whose history check reports a late or re-stamped write that ``stale_marks`` cannot see
      (M13 E2), and whose findings must otherwise be empty.
    """
    history = read_history(world.cluster)
    fenced = set(history.writer)
    violations: dict[str, list] = {
        "i1_one_writer_per_epoch": [],
        "i2_epoch_monotonic": [],
        "i3_late_epoch_commit": [],
        "i4_retired_mark_in_merge": [],
        "i4_any_commit": [],
        "audit": [],
        "i5_acknowledged_lost": [],
        "i6_forwarded_lost": [],
        "i7_backed_up_work_lost": [],
        "i8_unbacked_dead_claim": [],
        "history": [],
    }
    # I1 -- outside-in: who moved main, and whom does the moved-to main name?
    moves = world.moves.moves if world.moves is not None else []
    for move in moves:
        if move.writer_after[0] is not None and move.writer_after[0] != move.frame:
            violations["i1_one_writer_per_epoch"].append(
                {"op": move.op, "mover": move.frame, "main_names": move.writer_after}
            )
    by_epoch: dict[int, set[str]] = {}
    for frame, epoch in history.writer.values():
        by_epoch.setdefault(epoch, set()).add(frame)
    for epoch, frames in sorted(by_epoch.items()):
        if len(frames) > 1:
            violations["i1_one_writer_per_epoch"].append({"epoch": epoch, "frames": sorted(frames)})

    # I2 -- DAG edges, head vs history, time series
    for child, parents in history.parents.items():
        if child not in fenced:
            continue
        for parent in parents:
            if parent in fenced and history.writer[child][1] < history.writer[parent][1]:
                violations["i2_epoch_monotonic"].append(
                    {
                        "commit": child,
                        "epoch": history.writer[child][1],
                        "parent": parent,
                        "parent_epoch": history.writer[parent][1],
                    }
                )
    if fenced:
        top = max(epoch for _, epoch in history.writer.values())
        if history.writer.get(history.head, (None, top))[1] < top:
            violations["i2_epoch_monotonic"].append(
                {"head_epoch": history.writer[history.head][1], "history_max": top}
            )
    if world.moves is not None:
        seen = [e for _, e in world.moves.epochs if e is not None]
        for i in range(1, len(seen)):
            if seen[i] < seen[i - 1]:
                violations["i2_epoch_monotonic"].append(
                    {"time_series": seen[max(0, i - 2) : i + 1], "op": world.moves.epochs[i][0]}
                )

    # I3 -- ancestry: epoch-e writes must sit below every later bump, orphan merges excepted
    def is_merge(c: str) -> bool:
        return len(history.parents.get(c, ())) > 1

    def is_bump(c: str) -> bool:
        first = history.parents.get(c, [None])[0] if history.parents.get(c) else None
        return (
            c in fenced
            and not is_merge(c)
            and (first not in fenced or history.writer[first][1] < history.writer[c][1])
        )

    sanctioned: set[str] = set()
    for commit, message in history.message.items():
        if is_merge(commit) and message.startswith(MERGE_PREFIX) and commit in fenced:
            first, *others = history.parents[commit]
            below_first = history.ancestors(first)
            for other in others:
                sanctioned |= history.ancestors(other) - below_first
    bumps = [c for c in fenced if is_bump(c)]
    below = {b: history.ancestors(b) for b in bumps}
    for commit in fenced:
        if commit in sanctioned or is_merge(commit) or is_bump(commit):
            continue
        epoch = history.writer[commit][1]
        for bump in bumps:
            if history.writer[bump][1] > epoch and commit not in below[bump]:
                violations["i3_late_epoch_commit"].append(
                    {
                        "commit": commit,
                        "epoch": epoch,
                        "message": history.message.get(commit, "")[:60],
                        "after_bump": bump,
                        "bump_epoch": history.writer[bump][1],
                    }
                )
                break

    # I4 -- marks at a retired epoch, per commit
    for commit in fenced:
        live = history.live.get(commit, set())
        retired = sorted({e for e in history.marks.get(commit, []) if e not in live})
        if retired:
            key = "i4_retired_mark_in_merge" if is_merge(commit) else "i4_any_commit"
            violations[key].append({"commit": commit, "retired": retired, "live": sorted(live)})

    audit = ef.fence_audit(world.cluster)
    if audit["stale_marks"] or audit["epoch_regressed"]:
        violations["audit"].append(audit)
    if audit["placement_ahead"] and not adopt_in_flight:
        violations["audit"].append(audit)

    if acknowledged or forwarded:
        titles = ef.remote_fence(world.cluster)["titles"]
        lost = sorted(set(acknowledged) - titles)
        if lost:
            violations["i5_acknowledged_lost"].append(lost)
        lost = sorted(set(forwarded) - titles)
        if lost:
            violations["i6_forwarded_lost"].append(lost)

    if backups or dead_frames:
        _check_claims(world, violations, backups or {}, backup_remote, dead_frames)

    product_audit = None
    if history_check:
        product_audit = _product_audit(world)
        findings = product_audit.findings()
        if not product_audit.cut_over or findings:
            if not (adopt_in_flight and all(f.startswith("placement_ahead") for f in findings)):
                violations["history"].append(findings or ["not cut over"])

    return {
        "ok": not any(violations.values()),
        "violations": {k: v for k, v in violations.items() if v},
        "commits": len(history.parents),
        "fenced": len(fenced),
        "bumps": len(bumps),
        "orphan_admitted": len(sanctioned),
        "audit": audit,
        "product_audit": None if product_audit is None else product_audit.as_dict(),
    }


def _backup_refs(remote: Path) -> dict[str, str]:
    out = subprocess.run(
        ["git", "--git-dir", str(remote), "for-each-ref", "--format=%(refname) %(objectname)"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return dict(line.split() for line in out.splitlines() if line.strip())


def _contains(remote: Path, sha: str, tip: str) -> bool:
    return (
        sha == tip
        or subprocess.run(
            ["git", "--git-dir", str(remote), "merge-base", "--is-ancestor", sha, tip],
            capture_output=True,
        ).returncode
        == 0
    )


def _check_claims(
    world: World,
    violations: dict[str, list],
    backups: Mapping[str, tuple[str, str]],
    remote: Path | None,
    dead_frames: Sequence[str],
) -> None:
    """I7 and I8 on remote ``main`` plus the git backup remote (M14 pairing, M3 reclaim)."""
    from beadhive import work_backup  # product naming of refs/bh/backup/<bead>/<frame>

    (rows,) = _observer_docs(
        world.cluster,
        "SELECT i.id AS id, i.status AS status, l.label AS label FROM issues i "
        "LEFT JOIN labels l ON l.issue_id = i.id",
    )
    status: dict[str, str] = {}
    labels: dict[str, set[str]] = {}
    for row in rows:
        status[row["id"]] = row["status"]
        if row.get("label"):
            labels.setdefault(row["id"], set()).add(row["label"])
    refs = _backup_refs(remote) if remote is not None else {}
    for bead, (ref, sha) in sorted(backups.items()):
        tip = refs.get(ref)
        if bead not in status or tip is None or not _contains(remote, sha, tip):
            violations["i7_backed_up_work_lost"].append(
                {"bead": bead, "ref": ref, "sha": sha, "tip": tip, "on_main": bead in status}
            )
    for bead, state in sorted(status.items()):
        mine = labels.get(bead, set())
        for dead in dead_frames:
            if state != "in_progress" or f"claim-frame:{dead}" not in mine:
                continue
            if "review:pending" in mine:
                continue
            ref = work_backup.backup_ref(bead, dead)
            if ref in refs:
                continue
            violations["i8_unbacked_dead_claim"].append({"bead": bead, "dead_frame": dead})


def _product_audit(world: World):
    """The product ``fence_audit`` (history check included) on the observer's ``origin/main``,
    against HQ placement."""
    from beadhive import fence_audit as fa
    from beadhive import fence_data as fd
    from beadhive.writer_adopt import PlacementView

    observer = world.cluster.remote
    node = fd.FenceNode(fd.DoltCliEngine(observer._observer, env=observer._env))
    placed = world.hq.placement()
    view = None if placed.writer is None else PlacementView(placed.writer, placed.epoch)
    return fa.fence_audit(node, placement=view)


# =============================================================================================
# Seeded randomized interleavings
# =============================================================================================

EVENTS = (
    "write",
    "push",
    "pull_push",
    "bd_sync",
    "auto_push",
    "adopt",
    "adopt_mid_push",
    "race_push",
    "partition",
    "heal",
    "kill",
    "divert",
    "orphan_merge",
    "hq_outage",
)
_WEIGHTS = {
    "write": 4,
    "push": 3,
    "pull_push": 3,
    "bd_sync": 2,
    "auto_push": 1,
    "adopt": 3,
    "adopt_mid_push": 2,
    "race_push": 1,
    "partition": 1,
    "heal": 1,
    "kill": 1,
    "divert": 2,
    "orphan_merge": 1,
    "hq_outage": 1,
}


def _outcome(run: Run) -> str:
    if run.killed:
        return "killed"
    if run.ok:
        return "ok"
    text = run.output
    for needle, label in (
        (wg.GUARD_REFUSAL, "guard"),
        ("bh_local_ident", "guard"),
        (NON_FF, "non-ff"),
        ("partition", "partitioned"),
        ("constraint violation", "fk"),
        ("CONSTRAINT VIOLATION", "fk"),
        ("constraint violations", "fk"),
        ("stomped by merge", "stomp"),
        ("conflict", "conflict"),
    ):
        if needle in text:
            return label
    return f"failed({run.returncode})"


@dataclass
class Schedule:
    seed: int
    events: list[dict] = field(default_factory=list)
    acknowledged: list[str] = field(default_factory=list)
    result: dict | None = None
    seconds: float = 0.0
    error: str | None = None


class _Driver:
    """Executes one seeded schedule against a world; every frame failure is an outcome."""

    def __init__(self, world: World, seed: int, length: int):
        self.world = world
        self.cluster = world.cluster
        self.rng = random.Random(seed)
        self.schedule = Schedule(seed)
        self.length = length
        self.n = 0

    # -- helpers ------------------------------------------------------------------------------
    @property
    def names(self) -> list[str]:
        return list(self.cluster.frames)

    def alive(self) -> list[str]:
        return [n for n in self.names if self.cluster[n].alive]

    def pick(self, pool: Sequence[str] | None = None) -> str | None:
        pool = list(pool if pool is not None else self.alive())
        return self.rng.choice(pool) if pool else None

    def title(self, frame: str) -> str:
        self.n += 1
        return f"s{self.schedule.seed}-{self.n}-{frame}"

    def record(self, event: str, **info) -> None:
        self.schedule.events.append({"event": event, **info})

    def observe(self, op: str, frame: str | None) -> None:
        self.world.moves.observe(op, frame)

    def push(self, frame: Frame) -> Run:
        """A RAW push (bd dolt push / dolt push): the fence must hold without bh's help."""
        return frame.push()

    def _ack_if_landed(self, frame: Frame, titles: Sequence[str], run: Run) -> None:
        if run.ok:
            remote = ef.remote_fence(self.cluster)["titles"]
            self.schedule.acknowledged.extend(t for t in titles if t in remote)

    # -- events -------------------------------------------------------------------------------
    def _write(self, name: str, event: str = "write") -> Run:
        """A write, flagged ``stale`` when HQ no longer places this frame (the frame may not
        know yet: that is exactly the stale writer the fence has to stop)."""
        title = self.title(name)
        stale = self.world.hq.placement().writer != name
        run = write(self.cluster[name], title)
        self.record(event, frame=name, title=title, stale=stale, outcome=_outcome(run))
        return run

    def ev_write(self) -> None:
        name = self.pick()
        if name is not None:
            self._write(name)

    def _maybe_write(self, name: str) -> None:
        """Push-like events carry fresh data most of the time, so stale paths push stale data."""
        if self.rng.random() < 0.7:
            self._write(name, "write+")

    def ev_push(self) -> None:
        name = self.pick()
        if name is None:
            return
        frame = self.cluster[name]
        self._maybe_write(name)
        pending = self._unpublished(frame)
        run = self.push(frame)
        self.observe("push", name)
        self._ack_if_landed(frame, pending, run)
        self.record("push", frame=name, outcome=_outcome(run))

    def ev_pull_push(self) -> None:
        """The stale pull-then-push path, through bd on bd frames (Dolt CLI otherwise)."""
        name = self.pick()
        if name is None:
            return
        frame = self.cluster[name]
        self._maybe_write(name)
        pending = self._unpublished(frame)
        pulled = frame.pull()
        pushed = self.push(frame)
        self.observe("pull_push", name)
        self._ack_if_landed(frame, pending, pushed)
        self.record("pull_push", frame=name, pull=_outcome(pulled), push=_outcome(pushed))

    def ev_bd_sync(self) -> None:
        name = self.pick([n for n in self.alive() if self.cluster[n].is_bd])
        if name is None:
            return
        frame = self.cluster[name]
        self._maybe_write(name)
        pending = self._unpublished(frame)
        run = frame.bd("sync")
        self.observe("bd_sync", name)
        self._ack_if_landed(frame, pending, run)
        self.record("bd_sync", frame=name, outcome=_outcome(run))

    def ev_auto_push(self) -> None:
        name = self.pick([n for n in self.alive() if self.cluster[n].is_bd])
        if name is None:
            return
        frame = self.cluster[name]
        title = self.title(name)
        run = frame.run(
            "env",
            "BD_DOLT_AUTO_PUSH=true",
            "bd",
            "create",
            "--title",
            title,
            "-t",
            "task",
            "--json",
        )
        self.observe("auto_push", name)
        auto = "auto-push failed" not in run.output
        self.record("auto_push", frame=name, outcome=_outcome(run), pushed=run.ok and auto)

    def ev_adopt(self) -> None:
        reclaim = self.rng.random() < 0.3  # a failover-style adopt that reverts in_progress
        target = self.pick()
        if target is None:
            return
        try:
            placed = self.world.hq.place(target)
        except (HQUnreachable, ValueError) as exc:
            self.record("adopt", frame=target, outcome=f"place refused: {exc}"[:80])
            return
        result = adopt(self.cluster, self.cluster[target], placed.epoch, reclaim=reclaim)
        self.observe("adopt", target)
        self.record(
            "adopt",
            frame=target,
            epoch=placed.epoch,
            reclaim=reclaim,
            outcome=result.outcome,
            s=result.seconds,
        )

    def ev_adopt_mid_push(self) -> None:
        """bh-vje85 S2 as a random event: an incumbent parked at its push CAS while another
        frame adopts; the parked push is released before or inside the adopter's push."""
        live = [n for n in self.alive() if not self.cluster[n].partitioned]
        if len(live) < 2:
            return
        old_name, new_name = self.rng.sample(live, 2)
        old, new = self.cluster[old_name], self.cluster[new_name]
        title = self.title(old_name)
        wrote = write(old, title)
        hold = old.hold(CAS)
        pending = old.push_async()
        try:
            hold.wait(60)
        except (AssertionError, TimeoutError):
            run = pending.result()
            hold.release()
            self.observe("adopt_mid_push:old", old_name)
            self.record("adopt_mid_push", frame=old_name, outcome=f"no push ({_outcome(run)})")
            return
        order = self.rng.choice(("bump-first", "write-first"))
        released: list[Run] = []

        def release_old() -> None:
            hold.release()
            released.append(pending.result())
            if released[-1].ok:
                self.world.moves.landed("adopt_mid_push:old", old)

        try:
            placed = self.world.hq.place(new_name)
        except (HQUnreachable, ValueError) as exc:
            release_old()
            self.observe("adopt_mid_push:old", old_name)
            self.record("adopt_mid_push", frame=old_name, outcome=f"place refused {exc}"[:80])
            return
        result = adopt(
            self.cluster,
            new,
            placed.epoch,
            before_push=release_old if order == "write-first" else None,
        )
        if order == "bump-first":
            release_old()
        if result.landed:
            self.world.moves.landed("adopt_mid_push:new", new)
        self.world.moves.observe("adopt_mid_push", None)
        if released and released[0].ok and wrote.ok:
            self._ack_if_landed(old, [title], released[0])
        self.record(
            "adopt_mid_push",
            old=old_name,
            new=new_name,
            order=order,
            write=_outcome(wrote),
            old_push=_outcome(released[0]) if released else "?",
            adopt=result.outcome,
            epoch=placed.epoch,
        )

    def ev_race_push(self) -> None:
        """Two frames write and push at once, both parked at CAS, released in random order."""
        live = [n for n in self.alive() if not self.cluster[n].partitioned]
        if len(live) < 2:
            return
        pair = self.rng.sample(live, 2)
        frames = [self.cluster[n] for n in pair]
        titles = [self.title(n) for n in pair]
        wrote = [write(f, t) for f, t in zip(frames, titles, strict=True)]
        holds = [f.hold(CAS) for f in frames]
        pend = [f.push_async() for f in frames]
        parked = []
        for h in holds:
            try:
                h.wait(60)
                parked.append(True)
            except (AssertionError, TimeoutError):
                parked.append(False)
        order = list(range(2))
        self.rng.shuffle(order)
        results: dict[int, Run] = {}
        for i in order:
            holds[i].release()
            results[i] = pend[i].result()
            if results[i].ok:
                self.world.moves.landed("race_push", frames[i])
        self.world.moves.observe("race_push", None)
        for i in range(2):
            self._ack_if_landed(frames[i], [titles[i]] if wrote[i].ok else [], results[i])
        self.record(
            "race_push",
            frames=pair,
            order=[pair[i] for i in order],
            writes=[_outcome(w) for w in wrote],
            pushes=[_outcome(results[i]) for i in range(2)],
            parked=parked,
        )

    def ev_partition(self) -> None:
        name = self.pick()
        if name is None:
            return
        self.cluster[name].partition()
        self.world.hq.partition(name)
        self.record("partition", frame=name)

    def ev_heal(self) -> None:
        cut = [n for n in self.names if self.cluster[n].partitioned]
        name = self.pick(cut) if cut else None
        if name is None:
            return
        self.cluster[name].heal()
        self.world.hq.heal(name)
        self.record("heal", frame=name)

    def ev_kill(self) -> None:
        """Kill a frame, half the time parked mid-push at its CAS; it restarts at once (a
        crash-and-reboot) so later events keep a full cluster."""
        name = self.pick()
        if name is None:
            return
        frame = self.cluster[name]
        mid = self.rng.random() < 0.5 and not frame.partitioned
        if mid:
            write(frame, self.title(name))
            hold = frame.hold(CAS)
            pending = frame.push_async()
            try:
                hold.wait(60)
            except (AssertionError, TimeoutError):
                pending.result()
                mid = False
        frame.kill()
        frame.restart()
        self.observe("kill", name)
        self.record("kill", frame=name, mid_push=mid)

    def ev_divert(self) -> None:
        """The managed rejoin: a frame whose local main is behind a newer epoch diverts its
        unpublished commits to frame/<id>/orphan and resets (bh-vje85 E3)."""
        name = self.pick([n for n in self.alive() if not self.cluster[n].partitioned])
        if name is None:
            return
        frame = self.cluster[name]
        try:
            out = divert(frame)
            outcome = "diverted" if out["diverted"] else out.get("reason", "reset")
        except AssertionError as exc:
            outcome = f"failed: {str(exc).splitlines()[0][:60]}"
        self.observe("divert", name)
        self.record("divert", frame=name, outcome=outcome)

    def ev_orphan_merge(self) -> None:
        """The current writer merges every published orphan branch on purpose (CLI/embedded
        writers; bh-vje85 E3), then publishes."""
        placed = self.world.hq.placement().writer
        if placed is None or not self.cluster[placed].alive:
            return
        writer = self.cluster[placed]
        if writer.kind == BD_SERVER or writer.partitioned:
            self.record("orphan_merge", frame=placed, outcome="skipped (server/partitioned)")
            return
        try:
            ef.sync_to_remote(writer)
        except AssertionError:
            return
        if local_writer(writer)[0] != placed:
            self.record("orphan_merge", frame=placed, outcome="skipped (adopt incomplete)")
            return
        branches = orphan_branches(writer)
        merged = []
        for branch in branches:
            out = ef.merge_orphan(writer, branch)
            merged.append((branch, _outcome(out["run"])))
            if out["run"].ok:
                pushed = writer.push()
                self.observe("orphan_merge", placed)
                if not pushed.ok:
                    ef.sync_to_remote(writer)
            else:
                ef.sync_to_remote(writer)
        self.record("orphan_merge", frame=placed, merged=merged)

    def ev_hq_outage(self) -> None:
        """A whole-HQ outage (scenario 7 as an event): handoff is refused while it lasts, the
        placed writer's managed write path does not consult HQ at all."""
        hq = self.world.hq
        hq.down()
        try:
            other = self.pick()
            try:
                hq.place(other or "x")
                place = "accepted"
            except (HQUnreachable, ValueError):
                place = "refused"
            placed = hq.placement().writer  # the recorder's cached view
            wrote = push = "skipped"
            if placed and self.cluster[placed].alive:
                frame = self.cluster[placed]
                title = self.title(placed)
                run = write(frame, title)
                wrote = _outcome(run)
                if run.ok:
                    push = managed_push(frame)
                    self.observe("hq_outage", placed)
                    if push == "pushed":
                        self._ack_if_landed(frame, [title], run)
        finally:
            hq.resume()
        self.record("hq_outage", place=place, writer=placed, write=wrote, push=push)

    def _unpublished(self, frame: Frame) -> list[str]:
        """Titles on the frame's local main that remote main lacks (cheap for 3-frame runs)."""
        if not frame.alive or frame.busy:
            return []
        try:
            local = frame.issue_titles()
        except AssertionError:
            return []
        remote = ef.remote_fence(self.cluster)["titles"]
        return sorted(t for t in local - remote if t.startswith(f"s{self.schedule.seed}-"))

    # -- schedule -----------------------------------------------------------------------------
    def run(self) -> Schedule:
        events = [e for e in EVENTS]
        weights = [_WEIGHTS[e] for e in events]
        for _ in range(self.length):
            event = self.rng.choices(events, weights)[0]
            getattr(self, f"ev_{event}")()
        return self.schedule

    def recover(self) -> dict:
        """End of schedule: heal and restart everything, divert stale unpublished work, and
        roll any incomplete adopt forward (never back), so the checker judges a quiesced hive."""
        for name in self.names:
            frame = self.cluster[name]
            frame.heal()
            self.world.hq.heal(name)
            if not frame.alive:
                frame.restart()
        steps: dict = {}
        placement = self.world.hq.placement()
        for name in self.names:
            frame = self.cluster[name]
            with contextlib.suppress(AssertionError):
                steps[name] = divert(frame)["diverted"]
        remote = ef.remote_fence(self.cluster)["writer"]
        if placement.writer and remote != (placement.writer, placement.epoch):
            result = adopt(self.cluster, self.cluster[placement.writer], placement.epoch)
            self.world.moves.observe("recover:adopt", placement.writer)
            steps["roll_forward"] = result.outcome
        for name in self.names:
            with contextlib.suppress(AssertionError):
                ef.sync_to_remote(self.cluster[name])
        return steps


def run_schedule(world: World, seed: int, *, length: int = 6) -> Schedule:
    """Run one seeded schedule of ``length`` random events, recover, and check every invariant.

    The caller owns the checkpoint/rewind around it (:func:`rewind_world`)."""
    started = time.monotonic()
    world.moves = MoveLog(world.cluster)
    driver = _Driver(world, seed, length)
    try:
        driver.run()
        steps = driver.recover()
        driver.schedule.events.append({"event": "recover", **steps})
        driver.schedule.result = check_invariants(world, acknowledged=driver.schedule.acknowledged)
    except Exception as exc:  # a harness failure is recorded with its seed, never swallowed
        driver.schedule.error = f"{type(exc).__name__}: {exc}"[:2000]
    driver.schedule.seconds = round(time.monotonic() - started, 2)
    return driver.schedule


@dataclass(frozen=True)
class WorldCheckpoint:
    cluster: object
    placement: Placement


def checkpoint_world(world: World) -> WorldCheckpoint:
    return WorldCheckpoint(world.cluster.checkpoint(), world.hq.placement())


def rewind_world(world: World, cp: WorldCheckpoint) -> None:
    """Rewind the remote, every frame and HQ placement. A server frame's remote-tracking refs are
    force-refetched so ``origin/main`` cannot stay ahead of a remote that moved backwards."""
    cluster = world.cluster
    for frame in cluster.frames.values():
        if frame.alive and frame.kind != BD_SERVER:
            frame.dolt("merge", "--abort")
        (frame.hive / ".beads" / "push-state.json").unlink(missing_ok=True)
    cluster.rewind(cp.cluster)
    world.hq.restore(cp.placement)
    for frame in cluster.frames.values():
        prune_remote_branches(frame)
    world.moves = MoveLog(cluster)


__all__ += ["EVENTS", "Schedule", "WorldCheckpoint", "checkpoint_world", "rewind_world"]

#: Frame kinds, re-exported for callers building frame lists.
FRAME_KINDS = (BD_EMBEDDED, BD_SERVER, DOLT_CLI)
