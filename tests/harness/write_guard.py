"""Local write guard for the writer-fencing spikes (bh-sieai). Test-only prototype, not product.

The guard is committed SQL objects plus one node-local row:

* ``bh_writer`` (versioned, one row ``{frame, epoch}``) and ``bh_write_mark`` (versioned, one row
  ``{id, epoch, tbl}`` per guarded ``main`` write) -- the shapes the proposal names. bh-vje85 owns
  the epoch/FK fence that sits on top of ``bh_write_mark``; this module only stamps it.
* ``bh_local_ident`` -- this replica's frame name. ``dolt_ignore``'d, so it is never committed,
  pushed or merged: each frame provisions its own (:func:`provision`).
* One stored procedure and one ``BEFORE`` trigger per event on every guarded table::

      IF COALESCE(active_branch(), 'main') = 'main' THEN
        SELECT epoch INTO e FROM bh_writer WHERE id = 1;
        INSERT INTO bh_write_mark (id, epoch, tbl) VALUES (uuid(), e, '<t>');
        CALL bh_guard_check();         -- LAST: SIGNALs (and so rolls back) on refusal
      END IF;

Why this shape (each point is pinned by ``tests/spikes/test_bh_sieai_write_guard_int.py``):

* **The identity check goes through a procedure.** Dolt resolves every table a trigger body names
  when it plans the triggering statement, even inside an ``IF`` branch that will not run. A
  trigger naming the ignored ``bh_local_ident`` directly fails ``table not found`` on every branch
  where that table is absent (a new branch, a fresh clone), non-``main`` ones included. A
  procedure body is resolved only when it is CALLed, so an unprovisioned replica fails closed on
  ``main`` only (``table not found: bh_local_ident``) and writes other branches freely.
* **The mark is written inline, BEFORE the CALL, and the CALL is the last statement.** On Dolt
  2.3.5 a trigger silently skips every statement after a ``CALL``; DML inside a procedure CALLed
  from a trigger is silently dropped when the triggering statement runs inside an explicit
  transaction (bd's ``withRetryTx``); and once a trigger body has CALLed, the trigger bodies of
  the statement's later rows do not run (one mark per multi-row statement, not per row). SIGNAL
  from the procedure still aborts the whole statement in every case, so a refusal also discards
  the inline mark, and the first row's check stands for the statement (identity and writer are
  the same for every row).
* **No existence check via ``information_schema`` inside the procedure.** It reads 0 rows for
  the later rows of a multi-row statement, which turned a writer's multi-row UPDATE (bd's own
  migration 0059) into a refusal.
* **DECLAREd locals, never ``@user`` variables, in conditions.** On Dolt 2.3.5 an
  ``IF @a <> @b THEN SIGNAL`` inside a trigger lets the write through even when the two values
  differ (fail-open), while DECLAREd locals and inline subqueries refuse correctly.
* **No scalar subquery inside ``INSERT ... VALUES``** (Dolt "unable to find field with index N");
  values come from ``SELECT ... INTO`` locals.
* **``active_branch()`` is NULL on a detached revision** (``db/<hash>``, ``db/<tag>``); those are
  read-only anyway, and ``COALESCE(..., 'main')`` makes an unknown branch fail closed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path

from harness.writer_fencing import BD_SERVER, Frame, Run

__all__ = [
    "GUARDED_TABLES",
    "GUARD_REFUSAL",
    "NO_IDENT_REFUSAL",
    "guard_ddl",
    "guard_state",
    "install",
    "provision",
    "run_sql_script",
    "trigger_names",
]

# Every VERSIONED table bd 1.3 writes (schema v66), less child_counters and metadata. The
# dolt_ignore'd ones -- events, leases, local_metadata, repo_mtimes, bd_events_*,
# ignored_schema_migrations, wisps/wisp_* -- are never merged or pushed, so a guard on them would
# fence nothing; schema_migrations is bd's own cursor.
#
# child_counters: Dolt refuses ANY multi-table DELETE on a table that has triggers ("delete from
# with explicit target tables does not support triggers"), and bd's clone-local ignored migration
# 0011 runs `DELETE cc FROM child_counters cc ... JOIN` on every fresh clone, so a guard there
# stops `bd init` joining the hive. A child-counter bump only happens inside the transaction of
# the guarded child INSERT into issues, which refuses the whole transaction.
#
# metadata: bd writes clone-local values there (repo_id, clone_id, last_import_time) on every
# `bd init` join and import, from non-writers too; a guard turns each join into "failed to write
# ... metadata" warnings. bd's auto-resolver already treats metadata conflicts as machine-local.
GUARDED_TABLES: tuple[str, ...] = (
    "issues",
    "dependencies",
    "labels",
    "comments",
    "config",
    "issue_counter",
    "issue_snapshots",
    "compaction_snapshots",
    "custom_statuses",
    "custom_types",
    "federation_peers",
    "routes",
    "interactions",
    "provenance_events",
)
EVENTS = ("insert", "update", "delete")
GUARD_REFUSAL = "bh-guard: this replica is not the bh_writer for main"
NO_IDENT_REFUSAL = "table not found: bh_local_ident"

_SCHEMA = """
create table if not exists bh_writer (id int primary key, frame varchar(64), epoch bigint);
create table if not exists bh_write_mark
  (id varchar(36) primary key, epoch bigint, tbl varchar(64));
insert ignore into dolt_ignore values ('bh_local_%', 1);
"""

_PROCEDURES = f"""
drop procedure if exists bh_guard_check//
create procedure bh_guard_check()
begin
  declare w varchar(64);
  declare me varchar(64);
  declare r varchar(16);
  select frame into w from bh_writer where id = 1;
  select frame, role into me, r from bh_local_ident where id = 1;
  if coalesce(r, '') <> 'branch' and (w is null or me is null or w <> me) then
    signal sqlstate '45000' set message_text = '{GUARD_REFUSAL}';
  end if;
end//
"""


def trigger_names(tables: Iterable[str] = GUARDED_TABLES) -> list[str]:
    return [f"bh_guard_{t}_{ev[:3]}" for t in tables for ev in EVENTS]


def guard_ddl(tables: Sequence[str] = GUARDED_TABLES) -> str:
    """The whole guard as one ``delimiter //`` SQL script (idempotent: drop-then-create)."""
    parts = [_SCHEMA, "delimiter //", _PROCEDURES]
    for table in tables:
        for event in EVENTS:
            name = f"bh_guard_{table}_{event[:3]}"
            parts.append(
                f"drop trigger if exists {name}//\n"
                f"create trigger {name} before {event} on `{table}` for each row\n"
                "begin\n  declare e bigint;\n"
                "  if coalesce(active_branch(), 'main') = 'main' then\n"
                "    select epoch into e from bh_writer where id = 1;\n"
                f"    insert into bh_write_mark (id, epoch, tbl) values (uuid(), e, '{table}');\n"
                "    call bh_guard_check();\n"
                "  end if;\nend//"
            )
    parts.append("delimiter ;")
    return "\n".join(parts) + "\n"


def _dolt_argv(frame: Frame) -> list[str]:
    """The Dolt CLI pointed at the frame's engine: its embedded store directory, or (bd-server)
    a client connection to the frame's own ``dolt sql-server``."""
    if frame.kind == BD_SERVER:
        return [
            "dolt",
            "--host",
            "127.0.0.1",
            "--port",
            str(frame.port),
            "--no-tls",
            "-u",
            "root",
            "--password",
            "",
            "--use-db",
            frame.cluster.prefix,
        ]
    return ["dolt"]


def run_sql_script(frame: Frame, script: str, name: str = "script.sql") -> Run:
    """Run a multi-statement script (``delimiter`` aware) on the frame's engine."""
    path = frame.dir / name
    path.write_text(script)
    cwd = frame.dolt_dir if frame.kind != BD_SERVER else frame.dir
    return frame.run(*_dolt_argv(frame), "sql", "--file", str(path), cwd=cwd)


def install(frame: Frame, epoch: int, tables: Sequence[str] = GUARDED_TABLES) -> None:
    """Create the guard on ``frame`` (the writer), seed ``bh_writer`` and commit. Not pushed."""
    run_sql_script(frame, guard_ddl(tables), "guard.sql").check()
    frame.sql(f"replace into bh_writer values (1, '{frame.name}', {epoch})").check()
    frame.commit("bh: install write guard").check()


def provision(frame: Frame, ident: str | None = None, role: str = "replica") -> None:
    """Give this replica its node-local identity (dolt_ignore'd: never committed or pushed).

    ``role='branch'`` marks a branch-path frame: its local ``main`` is a staging branch that bh
    publishes to ``frame/<id>`` (or a frame-private data ref), so the guard lets bd write it.
    """
    script = (
        "create table if not exists bh_local_ident "
        "(id int primary key, frame varchar(64), role varchar(16) default 'replica');\n"
        f"replace into bh_local_ident values (1, '{ident or frame.name}', '{role}');\n"
    )
    run_sql_script(frame, script, "ident.sql").check()


def guard_state(frame: Frame) -> dict:
    """``guard_probe`` for :class:`harness.writer_fencing.Recorder`: identity, writer row, mark
    count and how many guard triggers exist on the frame (idle frames only)."""
    if not frame.alive or frame.busy:
        return {"alive": frame.alive, "busy": frame.busy}
    present = frame.tables()
    ident = (
        frame.query("select frame, role from bh_local_ident") if "bh_local_ident" in present else []
    )
    writer = frame.query("select frame, epoch from bh_writer") if "bh_writer" in present else []
    marks = (
        frame.query("select count(*) n from bh_write_mark")[0]["n"]
        if "bh_write_mark" in present
        else None
    )
    triggers = frame.query(
        "select count(*) n from information_schema.triggers "
        "where trigger_schema = database() and trigger_name like 'bh_guard_%'"
    )[0]["n"]
    return {
        "ident": ident[0]["frame"] if ident else None,
        "role": ident[0]["role"] if ident else None,
        "writer": writer[0] if writer else None,
        "marks": marks,
        "triggers": int(triggers),
        "partitioned": frame.partitioned,
    }


def dump(path: Path, data: object) -> None:
    """Append one evidence record (JSON line) for the spike doc."""
    with path.open("a") as fh:
        fh.write(json.dumps(data, sort_keys=True, default=str) + "\n")
