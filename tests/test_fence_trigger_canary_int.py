"""Dolt/bd trigger-semantics canary (bh-p07dv; ADR condition 9). A contract, not a spike.

The hive write guard and in-data epoch fence are shaped by Dolt/bd trigger behaviour measured on
the pinned binaries (``bh-sieai`` Evidence 1 + Recommendation 5, ``bh-vje85``, ``bh-jbb6r`` E6).
This suite re-asserts every one of those behaviours, so a Dolt or bd pin bump that changes them
fails here -- with a message naming the condition-9 assumption -- before it ships. Wiring and
selection: :mod:`harness.trigger_canary` and the comment beside the pins in ``flake.nix``.
Run just this suite with ``just fence-canary``.

Also part of the canary (``fence_canary`` marker, not duplicated here): the bh-sieai engine
probes (``tests/spikes/test_bh_sieai_write_guard_int.py``) and bh-wtsrc E3's invoker-checked
trigger DML (``tests/spikes/test_bh_wtsrc_receiver_removal_liveness.py``).

Every Dolt process runs with its own ``HOME`` / ``DOLT_ROOT_PATH`` under ``tmp_path``; the one
``sql-server`` this starts is private to the test, on its own port and data dir.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time

import pytest

from harness import composed_fence as cf
from harness import epoch_fence as ef
from harness import trigger_canary as tc
from harness import write_guard as wg
from harness.world import free_port
from harness.writer_fencing import BD_EMBEDDED, DOLT_CLI

pytestmark = [
    pytest.mark.integration,
    pytest.mark.fence_canary,
    pytest.mark.skipif(
        shutil.which("bd") is None or shutil.which("dolt") is None, reason="bd/dolt not installed"
    ),
]

expect = tc.expect


def test_installed_dolt_and_bd_are_the_canary_pins():
    """The canary measures the binaries on PATH; they must be the ones CANARY_PINS vouches for,
    or a green run says nothing about the pin."""
    installed = tc.installed_versions()
    expect(
        installed == tc.CANARY_PINS,
        "the canary runs on the pinned Dolt/bd binaries",
        f"installed {installed}, CANARY_PINS {tc.CANARY_PINS}",
    )


# --------------------------------------------------------------------------------------------
# The guard's own shape, statically and on the engine


def test_guard_ddl_inserts_the_mark_inline_before_a_final_call():
    expect(
        tc.guard_shape_violations(wg.guard_ddl()) == [],
        "BEFORE-trigger inline mark insert with the CALL last",
        "; ".join(tc.guard_shape_violations(wg.guard_ddl())),
    )
    expect(
        tc.composed_trigger_total() == tc.COMPOSED_TRIGGER_COUNT,
        f"the composed fence + guard script installs {tc.COMPOSED_TRIGGER_COUNT} triggers",
        f"script creates {tc.composed_trigger_total()}",
    )


def test_guard_marks_every_writer_statement_once_and_refusal_rolls_it_back(tmp_path):
    tc.check_guard_marks(tc.DoltRepo(tmp_path), wg.guard_ddl(("issues",)))


def _mark_in_procedure(ddl: str) -> str:
    """A plausible regression: the mark insert moved from the trigger into the procedure."""
    inline = "    insert into bh_write_mark (id, epoch, tbl) values (uuid(), e, 'issues');\n"
    assert inline in ddl
    return ddl.replace(inline, "").replace(
        "  select frame into w from bh_writer where id = 1;\n",
        "  select frame into w from bh_writer where id = 1;\n"
        "  insert into bh_write_mark (id, epoch, tbl) select uuid(), epoch, 'issues' "
        "from bh_writer where id = 1;\n",
    )


def _mark_after_call(ddl: str) -> str:
    """The other plausible regression: the CALL is no longer the last statement."""
    inline = "    insert into bh_write_mark (id, epoch, tbl) values (uuid(), e, 'issues');\n"
    call = "    call bh_guard_check();\n"
    return ddl.replace(inline + call, call + inline)


@pytest.mark.parametrize("flip", [_mark_in_procedure, _mark_after_call])
def test_a_flipped_guard_fails_the_canary_naming_condition_9(tmp_path, flip):
    """Non-vacuity: a semantics flip in the guard makes both canary checks fail loudly."""
    flipped = flip(wg.guard_ddl(("issues",)))
    assert tc.guard_shape_violations(flipped)
    with pytest.raises(tc.AssumptionBroken, match=r"condition 9 .*inline mark insert"):
        tc.check_guard_marks(tc.DoltRepo(tmp_path), flipped)


# --------------------------------------------------------------------------------------------
# bh-sieai R5: the Dolt trigger bugs the guard's shape works around, pinned as expected


_R5_SCHEMA = """
create table m (id varchar(36) primary key, src varchar(32));
create table w (id int primary key, frame varchar(16), e int);
insert into w values (1, 'A', 7);
create table probe_uv (id int primary key);
create table probe_after (id int primary key);
create table probe_txn (id int primary key);
create table probe_rows (id int primary key);
create table probe_is (id int primary key);
create table probe_sq (id int primary key);
create table probe_mt (id int primary key);
create table ident_present (id int primary key);
insert into probe_mt values (1), (2);
delimiter //
create procedure nop() begin declare x int; set x = 1; end//
create procedure mark(in s varchar(32)) begin insert into m values (uuid(), s); end//
create trigger uv before insert on probe_uv for each row begin
  set @w = (select frame from w where id = 1);
  set @me = 'B';
  if @w <> @me then signal sqlstate '45000' set message_text = 'uv refused'; end if;
end//
create trigger aft before insert on probe_after for each row begin
  call nop(); insert into m values (uuid(), 'after-call');
end//
create trigger txn before insert on probe_txn for each row begin call mark('in-proc'); end//
create trigger rws before insert on probe_rows for each row begin
  insert into m values (uuid(), concat('row-', new.id)); call nop();
end//
create procedure is_check() begin
  declare n int;
  select count(*) into n from information_schema.tables
    where table_schema = database() and table_name = 'ident_present';
  if n = 0 then signal sqlstate '45000' set message_text = 'information_schema saw 0'; end if;
end//
create trigger isc before insert on probe_is for each row begin call is_check(); end//
create trigger sqt before insert on probe_sq for each row begin
  insert into m values (uuid(), (select frame from w where id = 1));
end//
create trigger mtd before delete on probe_mt for each row begin
  insert into m values (uuid(), 'mt');
end//
delimiter ;
call dolt_commit('-Am', 'r5 probes');
"""


def test_r5_dolt_trigger_bugs_still_behave_as_the_guard_assumes(tmp_path):
    """bh-sieai Recommendation 5 on the Dolt CLI engine (the same engine the frame's
    sql-server runs). Each is pinned AS A BUG: if one is fixed, the canary goes red so the
    guard's workaround is re-evaluated rather than silently kept or silently relied on."""
    repo = tc.DoltRepo(tmp_path)
    repo.ok("sql", stdin=_R5_SCHEMA)

    def marks(src: str) -> int:
        return repo.count(f"select count(*) from m where src like '{src}'")

    # 1. @user-variable comparison in a trigger IF fails OPEN.
    expect(
        repo.run("sql", "-q", "insert into probe_uv values (1)").returncode == 0,
        "R5.1 an @user-variable IF comparison in a trigger fails open (guard uses DECLAREd locals)",
    )
    # 2. Statements after a CALL in a trigger body are silently skipped.
    repo.ok("sql", "-q", "insert into probe_after values (1)")
    expect(marks("after-call") == 0, "R5.2 statements after a CALL in a trigger are skipped")
    # 3. Procedure DML from a trigger: kept under autocommit, dropped inside BEGIN ... COMMIT.
    repo.ok("sql", "-q", "insert into probe_txn values (1)")
    repo.ok("sql", stdin="begin;\ninsert into probe_txn values (2);\ncommit;\n")
    expect(
        marks("in-proc") == 1,
        "R5.3 DML in a trigger-CALLed procedure is dropped inside an explicit transaction",
        f"{marks('in-proc')} of 2 procedure marks kept",
    )
    # 4. Once a trigger body has CALLed, later rows of the statement do not run it.
    repo.ok("sql", "-q", "insert into probe_rows values (1), (2), (3)")
    expect(
        marks("row-%") == 1,
        "R5.4 only the first row's trigger body runs after a CALL (one mark per statement)",
        f"{marks('row-%')} row marks for 3 rows",
    )
    # 5. information_schema inside a CALLed procedure reads 0 rows from the second row on.
    repo.ok("sql", "-q", "insert into probe_is values (1)")
    later = tc.refusal(repo.run("sql", "-q", "insert into probe_is values (2), (3)"))
    expect(
        later is not None and "information_schema saw 0" in later,
        "R5.5 information_schema reads 0 rows for later rows inside a CALLed procedure",
        later or "accepted",
    )
    # 6. A multi-table DELETE is refused on any table with triggers (why child_counters is
    #    unguarded: bd's migration 0011 runs one on every fresh clone).
    multi = tc.refusal(repo.run("sql", "-q", "delete t from probe_mt t join w on t.id = w.id"))
    expect(
        multi is not None and "does not support triggers" in multi,
        "R5.6 multi-table DELETE is refused on a table with triggers",
        multi or "accepted",
    )
    # 8 (already known upstream). A scalar subquery inside INSERT ... VALUES in a trigger.
    scalar = tc.refusal(repo.run("sql", "-q", "insert into probe_sq values (1)"))
    expect(
        scalar is not None and "unable to find field with index" in scalar,
        "R5.8 a scalar subquery inside INSERT ... VALUES in a trigger hits the Dolt field bug",
        scalar or "accepted",
    )
    # Not a bug, but the guard's fail-closed default: active_branch() is NULL when detached.
    repo.ok("tag", "t1", "main")
    detached = repo.ok(
        "sql", "-r", "csv", "-q", "use `canary/t1`; select active_branch() is null d"
    )
    expect(
        detached.split() == ["d", "true"],
        "active_branch() is NULL on a detached revision (COALESCE(..., 'main') fails closed)",
        detached,
    )


@pytest.mark.dolt_server
def test_r5_commit_ancestors_where_on_a_merge_head_fails_max1row_on_sql_server(tmp_path):
    """R5.7 is sql-server-only: a ``WHERE commit_hash = hashof('HEAD')`` on
    ``dolt_commit_ancestors`` fails ``max1Row`` once HEAD is a merge commit (the Dolt CLI answers
    it). The fence's audit filters ancestry client-side because of it."""
    repo = tc.DoltRepo(tmp_path)
    repo.ok("sql", "-q", "create table t (id int primary key); call dolt_commit('-Am', 'base')")
    repo.ok(
        "sql",
        "-q",
        "call dolt_checkout('-b', 'side'); insert into t values (1); "
        "call dolt_commit('-Am', 'side')",
    )
    repo.ok("sql", "-q", "insert into t values (2); call dolt_commit('-Am', 'main')")
    repo.ok("merge", "side", "-m", "merge side")
    port = free_port()
    log = (tmp_path / "server.log").open("w")
    server = subprocess.Popen(
        ["dolt", "sql-server", "-H", "127.0.0.1", "-P", str(port)],
        cwd=repo.dir,
        env=repo.env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                assert server.poll() is None and time.monotonic() < deadline, "server down"
                time.sleep(0.1)
        client = ["--host", "127.0.0.1", "--port", str(port), "--no-tls", "-u", "root"]
        client += ["--password", "", "--use-db", "canary", "sql", "-q"]
        query = "select parent_hash from dolt_commit_ancestors where commit_hash = hashof('HEAD')"
        on_server = tc.refusal(repo.run(*client, query))
        expect(
            on_server is not None and "max1Row" in on_server,
            "R5.7 dolt_commit_ancestors WHERE on a merge head fails max1Row on the sql-server",
            on_server or "answered",
        )
    finally:
        server.terminate()
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=30)
        log.close()


# --------------------------------------------------------------------------------------------
# The composed install through bd's own engine (bh-jbb6r E6, bh-vje85 conditions 1-4)


def test_composed_install_on_bd_holds_every_fence_trigger_assumption(tmp_path):
    """A real bd 1.3 embedded writer (bd's linked Dolt) and a Dolt CLI replica on one remote,
    git-mode HQ (no server). Pins: the 44-trigger count, ``bh_local_%`` staying unstaged,
    bd's own write stamping an inline mark, monotonic BEFORE UPDATE refusal and FK epoch
    retirement."""
    with cf.composed_world(tmp_path, [("a", BD_EMBEDDED), ("c", DOLT_CLI)], hq_mode="git") as w:
        a, c = w["a"], w["c"]
        writer, epoch = cf.local_writer(a)
        assert writer == "a"

        for frame in (a, c):
            count = cf.trigger_count(frame)
            expect(
                count == tc.COMPOSED_TRIGGER_COUNT,
                f"a composed install leaves {tc.COMPOSED_TRIGGER_COUNT} bh_ triggers "
                "(adopt refuses to act as writer when short)",
                f"{frame.name} ({frame.kind}) has {count}",
            )
            staged = frame.query(
                "select table_name from dolt_status where table_name like 'bh_local_%'"
            )
            expect(
                "bh_local_ident" in frame.tables() and staged == [],
                "dolt_ignore'd bh_local_% stays unstaged (identity never committed or pushed)",
                f"{frame.name}: dolt_status lists {staged}",
            )

        # bd's write runs inside withRetryTx: the inline mark must survive it.
        def bd_marks() -> int:
            return a.query(
                f"select count(*) n from bh_write_mark where tbl = 'issues' and epoch = {epoch}"
            )[0]["n"]

        before = int(bd_marks())
        cf.write(a, "canary-bd-write").check()
        expect(
            int(bd_marks()) > before,
            "BEFORE-trigger inline mark insert survives bd's explicit-transaction write",
            f"{before} -> {bd_marks()} issues marks at epoch {epoch}",
        )

        # Monotonic BEFORE UPDATE on both fence tables (on the CLI replica; nothing is pushed).
        for stmt in (
            "update bh_writer set epoch = epoch where id = 1",
            "update bh_epoch_live set epoch = epoch - 1 where id = 1",
        ):
            run = c.sql(stmt)
            expect(
                not run.ok and ef.MONOTONIC_REFUSAL in run.output,
                "monotonic BEFORE UPDATE trigger refuses a non-increasing fence epoch",
                f"{stmt!r}: {run.output.strip() or 'accepted'}",
            )

        # FK epoch retirement: the live epoch cannot move while marks point at it; the adopt
        # bump (retire, then move) can; and a mark at a retired epoch is a FK violation.
        moved = c.sql(f"update bh_epoch_live set epoch = {epoch + 1} where id = 1")
        expect(
            not moved.ok and "foreign key" in moved.output.lower(),
            "the bh_write_mark -> bh_epoch_live FK pins the live epoch while marks reference it",
            moved.output.strip() or "accepted",
        )
        ef.run_script(c, cf.bump_statements("c", epoch + 1)).check()
        retired = c.query(f"select count(*) n from bh_write_mark where epoch < {epoch + 1}")
        expect(retired[0]["n"] == 0, "the adopt bump retires every older epoch's marks")
        stale = c.sql(f"insert into bh_write_mark values ('stale', {epoch}, 'issues')")
        expect(
            not stale.ok and "foreign key" in stale.output.lower(),
            "a mark at a retired epoch is a foreign-key violation (FK epoch retirement)",
            stale.output.strip() or "accepted",
        )
