"""Executable evidence for the bh-sieai spike; not a product contract test.

Question: can a Dolt trigger on bead tables refuse ``main`` writes on any replica that is not
``bh_writer.frame`` (raw bd included) without blocking the merge/pull that delivers an epoch bump,
and which non-primary write path -- forward to the primary's Dolt server, or commit to
``frame/<id>`` and let the primary merge -- is viable with bd 1.3's claim/lease/reclaim
semantics?  See ``docs/spikes/bh-sieai-write-guard-and-nonprimary-writes.md``.

The guard under test is ``harness.write_guard``. Cluster tests run on the bh-eybn7 fixture
(``harness.writer_fencing``): ``bd-embedded`` frames execute through bd's linked Dolt engine,
``bd-server`` frames through the frame's own ``dolt sql-server`` (the 2.3.5 CLI binary bd
starts). Every Dolt process has its own ``HOME``/``DOLT_ROOT_PATH`` under ``tmp_path``.

Set ``BH_SIEAI_EVIDENCE=<file>`` to append the measured numbers as JSON lines.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path

import pytest

from harness import write_guard as wg
from harness.writer_fencing import (
    BD_EMBEDDED,
    BD_SERVER,
    CAS,
    DOLT_CLI,
    Cluster,
    Frame,
    Recorder,
    Run,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("bd") is None or shutil.which("dolt") is None, reason="bd/dolt not installed"
    ),
]

ISSUE_INSERT = (
    "insert into issues (id, title, description, design, acceptance_criteria, notes) "
    "values ('{id}', '{id}', '', '', '', '')"
)


def _evidence(name: str, data: object) -> None:
    target = os.environ.get("BH_SIEAI_EVIDENCE")
    if target:
        wg.dump(Path(target), {"test": name, **(data if isinstance(data, dict) else {"v": data})})


# --------------------------------------------------------------------------------------------
# 1. The Dolt engine's trigger semantics the guard depends on (Dolt CLI 2.3.5, no bd)


class _Repo:
    """A Dolt CLI repo with its own HOME / DOLT_ROOT_PATH."""

    def __init__(self, root: Path, name: str):
        self.dir = root / name
        self.home = root / f"{name}-home"
        (self.home / ".dolt").mkdir(parents=True)
        (self.home / ".dolt" / "config_global.json").write_text(
            json.dumps(
                {"user.name": name, "user.email": f"{name}@x.invalid", "metrics.disabled": "true"}
            )
        )
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "DOLT_ROOT_PATH": str(self.home),
        }

    def run(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["dolt", *args],
            cwd=self.dir,
            env=self.env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def ok(self, *args: str, stdin: str | None = None) -> str:
        res = self.run(*args, stdin=stdin)
        assert res.returncode == 0, f"dolt {' '.join(args)}: {res.stdout}{res.stderr}"
        return res.stdout

    def sql(self, script: str) -> subprocess.CompletedProcess:
        return self.run("sql", stdin=script)

    def rows(self, query: str) -> list[dict]:
        out = self.ok("sql", "-r", "json", "-q", query).strip()
        return json.loads(out).get("rows", []) if out else []

    def count(self, query: str) -> int:
        return int(next(iter(self.rows(query)[0].values())))


def _refusal(res: subprocess.CompletedProcess) -> str:
    assert res.returncode != 0, f"write was accepted: {res.stdout}"
    return res.stdout + res.stderr


@pytest.mark.fence_canary  # ADR condition 9: re-run on every Dolt/bd pin bump (bh-p07dv)
def test_dolt_trigger_semantics_the_guard_depends_on(tmp_path):
    """Pins four Dolt 2.3.5 trigger behaviours that decide the guard's shape (see
    ``harness.write_guard``) and the guard's own refusals on the Dolt CLI."""
    repo = _Repo(tmp_path, "r")
    repo.dir.mkdir()
    repo.ok("init", "-b", "main")
    assert repo.rows("select dolt_version() v")[0]["v"] == "2.3.5"
    repo.ok(
        "sql",
        stdin="""
create table issues (id varchar(32) primary key, title text, description text, design text,
  acceptance_criteria text, notes text);
create table m (id varchar(36) primary key, src varchar(32));
create table w (id int primary key, frame varchar(16));
insert into w values (1, 'A');
create table probe_uv (id int primary key);
create table probe_after (id int primary key);
create table probe_txn (id int primary key);
create table probe_inline (id int primary key);
create table probe_direct (id int primary key);
create table probe_rows (id int primary key);
create table probe_is (id int primary key);
create table ident_present (id int primary key);
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
create trigger inl before insert on probe_inline for each row begin
  insert into m values (uuid(), 'inline');
end//
create trigger dir before insert on probe_direct for each row begin
  if active_branch() = 'main' then insert into m select uuid(), frame from bh_local_ident; end if;
end//
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
delimiter ;
call dolt_commit('-Am', 'probes');
""",
    )
    # (a) @user-variable comparison in a trigger IF: fails OPEN (the write is accepted).
    assert repo.run("sql", "-q", "insert into probe_uv values (1)").returncode == 0
    # (b) every statement after a CALL in a trigger body is silently skipped.
    repo.ok("sql", "-q", "insert into probe_after values (1)")
    assert repo.count("select count(*) from m where src = 'after-call'") == 0
    # (c) DML inside a procedure CALLed from a trigger is kept under autocommit but silently
    #     dropped inside an explicit transaction; inline trigger DML is kept in both.
    for txn in (False, True):
        for table in ("probe_txn", "probe_inline"):
            stmt = f"insert into {table} values ({int(txn)});"
            repo.ok("sql", stdin=f"begin;\n{stmt}\ncommit;\n" if txn else stmt)
    assert repo.count("select count(*) from m where src = 'in-proc'") == 1  # autocommit only
    assert repo.count("select count(*) from m where src = 'inline'") == 2
    # (d) tables named in a trigger body resolve when the statement is planned, even inside an
    #     IF branch that will not run: an absent (ignored, per-branch) table breaks every branch.
    repo.ok("sql", "-q", "call dolt_checkout('-b', 'frame/x')")
    direct = repo.run(
        "sql", "-q", "call dolt_checkout('frame/x'); insert into probe_direct values (1)"
    )
    assert "table not found: bh_local_ident" in _refusal(direct)
    repo.ok("checkout", "main")
    # (e) once a trigger body has CALLed, the bodies for the statement's later rows do not run.
    repo.ok("sql", "-q", "insert into probe_rows values (1), (2), (3)")
    assert repo.count("select count(*) from m where src like 'row-%'") == 1
    # (f) an information_schema read inside a CALLed procedure sees 0 rows for later rows.
    repo.ok("sql", "-q", "insert into probe_is values (1)")
    assert "information_schema saw 0" in _refusal(
        repo.run("sql", "-q", "insert into probe_is values (2), (3)")
    )

    # The guard itself: procedure indirection, mark first, CALL last.
    repo.ok("sql", stdin=wg.guard_ddl(("issues",)))
    repo.ok("sql", "-q", "insert into bh_writer values (1, 'A', 7)")
    repo.ok("sql", "-q", "call dolt_commit('-Am', 'guard')")
    triggers = repo.count(
        "select count(*) from information_schema.triggers where trigger_name like 'bh_guard_%'"
    )
    assert triggers == 3

    unprovisioned = repo.run("sql", "-q", ISSUE_INSERT.format(id="u1"))
    assert wg.NO_IDENT_REFUSAL in _refusal(unprovisioned)
    branch = repo.run(
        "sql", "-q", "call dolt_checkout('-b', 'frame/y'); " + ISSUE_INSERT.format(id="br1")
    )
    assert branch.returncode == 0, branch.stdout + branch.stderr  # non-main: unrestricted

    repo.ok("checkout", "main")
    repo.ok(
        "sql",
        "-q",
        "create table bh_local_ident (id int primary key, frame varchar(64), role varchar(16)); "
        "insert into bh_local_ident values (1, 'B', 'replica')",
    )
    assert repo.rows("select * from dolt_status where table_name = 'bh_local_ident'") == []
    multi = ISSUE_INSERT.format(id="b3") + ", ('b4', 'b4', '', '', '', '')"
    for stmt in (
        ISSUE_INSERT.format(id="b1"),
        "begin;\n" + ISSUE_INSERT.format(id="b2") + ";\ncommit;",
        multi,
        "begin;\n" + multi.replace("b3", "b5").replace("b4", "b6") + ";\ncommit;",
    ):
        assert wg.GUARD_REFUSAL in _refusal(repo.sql(stmt))
    assert repo.count("select count(*) from bh_write_mark") == 0  # the refusal rolled the mark back

    repo.ok("sql", "-q", "update bh_local_ident set frame = 'A'")
    repo.ok("sql", "-q", ISSUE_INSERT.format(id="a1"))
    repo.ok("sql", stdin="begin;\n" + ISSUE_INSERT.format(id="a2") + ";\ncommit;\n")
    repo.ok(
        "sql",
        "-q",
        "update issues set title = 'x' where id = 'a1'; delete from issues where id = 'a2'",
    )
    repo.ok("sql", "-q", ISSUE_INSERT.format(id="a3") + ", ('a4', 'a4', '', '', '', '')")
    repo.ok("sql", "-q", "update issues set notes = 'all rows'")  # 3 rows, one statement
    # One mark per statement (e), never zero: six statements, six marks, all at epoch 7.
    assert repo.rows("select tbl, epoch, count(*) n from bh_write_mark group by tbl, epoch") == [
        {"tbl": "issues", "epoch": 7, "n": 6}
    ]

    # active_branch() is NULL on detached revisions, which are read-only regardless.
    head = repo.rows("select hashof('main') h")[0]["h"]
    repo.ok("tag", "t1", "main")
    for rev in (head, "t1"):
        detached = repo.ok(
            "sql", "-r", "csv", "-q", f"use `r/{rev}`; select active_branch() is null d"
        )
        assert detached.split() == ["d", "true"], detached
        ro = repo.run("sql", "-q", f"use `r/{rev}`; " + ISSUE_INSERT.format(id="d1"))
        assert "read-only" in _refusal(ro)

    # ALTER TABLE (add / modify / rename / drop column) keeps the triggers and they still fire.
    repo.ok(
        "sql",
        "-q",
        "alter table issues add column extra int; alter table issues modify column title longtext; "
        "alter table issues rename column notes to notes2; alter table issues drop column extra",
    )
    assert (
        repo.count(
            "select count(*) from information_schema.triggers where trigger_name like 'bh_guard_%'"
        )
        == 3
    )
    repo.ok("sql", "-q", "update bh_local_ident set frame = 'B'")
    assert wg.GUARD_REFUSAL in _refusal(repo.run("sql", "-q", "update issues set title = 'y'"))
    repo.ok("sql", "-q", "alter table issues rename column notes2 to notes")
    _evidence("dolt-cli-semantics", {"triggers": triggers})


def test_merge_cherry_pick_and_revert_do_not_fire_the_guard_on_the_dolt_cli(tmp_path):
    """A stale replica (identity B, writer A) still takes a true pull-merge, a local branch merge,
    a cherry-pick and a revert onto ``main``: none of them fires a trigger. The last three are
    also a guard bypass for anyone who can write a non-``main`` branch."""
    remote = tmp_path / "remote"
    remote.mkdir()
    a, b = _Repo(tmp_path, "a"), _Repo(tmp_path, "b")
    a.dir.mkdir()
    a.ok("init", "-b", "main")
    a.ok(
        "sql",
        "-q",
        "create table issues (id varchar(32) primary key, title text, description text, "
        "design text, acceptance_criteria text, notes text); "
        "create table scratch (id int primary key)",
    )
    a.ok("sql", stdin=wg.guard_ddl(("issues",)) + "insert into bh_writer values (1, 'A', 1);\n")
    a.ok("sql", "-q", "call dolt_commit('-Am', 'guard')")
    a.ok("remote", "add", "origin", f"file://{remote}")
    a.ok("push", "origin", "main")
    subprocess.run(
        ["dolt", "clone", f"file://{remote}", str(b.dir)],
        env=b.env,
        check=True,
        capture_output=True,
    )
    for repo, ident in ((a, "A"), (b, "B")):
        repo.ok(
            "sql",
            "-q",
            "create table bh_local_ident "
            "(id int primary key, frame varchar(64), role varchar(16)); "
            f"insert into bh_local_ident values (1, '{ident}', 'replica')",
        )
    # Writer A publishes two guarded writes; stale B diverges on an unguarded table.
    a.ok("sql", "-q", ISSUE_INSERT.format(id="w1"))
    a.ok("sql", "-q", "call dolt_commit('-Am', 'w1')")
    a.ok("sql", "-q", ISSUE_INSERT.format(id="w2"))
    a.ok("sql", "-q", "call dolt_commit('-Am', 'w2')")
    a.ok("push", "origin", "main")
    b.ok("sql", "-q", "insert into scratch values (1); call dolt_commit('-Am', 'b local')")
    assert wg.GUARD_REFUSAL in _refusal(b.run("sql", "-q", ISSUE_INSERT.format(id="b0")))

    out = {}
    out["pull"] = b.ok("pull", "origin", "main")
    assert b.count("select count(*) from dolt_log where message like 'Merge%'") >= 1
    assert {r["id"] for r in b.rows("select id from issues")} == {"w1", "w2"}
    marks_after_pull = b.count("select count(*) from bh_write_mark")
    assert marks_after_pull == 2  # exactly A's two, none minted by the merge

    b.ok("checkout", "-b", "frame/b")
    b.ok("sql", "-q", ISSUE_INSERT.format(id="fb1") + "; call dolt_commit('-Am', 'fb1')")
    b.ok("sql", "-q", ISSUE_INSERT.format(id="fb2") + "; call dolt_commit('-Am', 'fb2')")
    fb1 = b.rows("select commit_hash h from dolt_log where message = 'fb1'")[0]["h"]
    b.ok("checkout", "main")
    out["cherry-pick"] = b.ok("cherry-pick", fb1)
    out["merge"] = b.ok("merge", "frame/b", "-m", "merge frame/b")
    w2 = b.rows("select commit_hash h from dolt_log where message = 'w2'")[0]["h"]
    out["revert"] = b.ok("revert", w2)
    assert {r["id"] for r in b.rows("select id from issues")} == {"w1", "fb1", "fb2"}
    # No op minted a mark; the revert also took back the mark w2's commit carried.
    assert b.count("select count(*) from bh_write_mark") == marks_after_pull - 1
    # ... yet a direct write is still refused afterwards.
    assert wg.GUARD_REFUSAL in _refusal(b.run("sql", "-q", "delete from issues where id = 'fb1'"))
    _evidence("dolt-cli-vc-ops", {k: v.strip()[-200:] for k, v in out.items()})


# --------------------------------------------------------------------------------------------
# 2. The guard under bd's own engines: embedded (bd's linked Dolt) and shared-server


def _ids(frame: Frame) -> set[str]:
    return {row["id"] for row in frame.query("select id from issues")}


def _marks(frame: Frame, rev: str | None = None) -> set[str]:
    clause = f" as of '{rev}'" if rev else ""
    return {row["id"] for row in frame.query(f"select id from bh_write_mark{clause}")}


def _create(frame: Frame, title: str) -> str:
    return json.loads(frame.bd("create", "--title", title, "-t", "task", "--json").check().stdout)[
        "id"
    ]


def _adopt(cluster: Cluster, frame: Frame) -> int:
    """HQ placement CAS, then the in-data adopt on ``frame`` (no push)."""
    epoch = cluster.hq.place(frame.name).epoch
    frame.sql(f"update bh_writer set frame = '{frame.name}', epoch = {epoch} where id = 1").check()
    frame.commit(f"bh: adopt epoch {epoch}").check()
    return epoch


def _assert_refused(run: Run, message: str = wg.GUARD_REFUSAL) -> str:
    assert not run.ok and message in run.output, run.output
    return run.output.strip().splitlines()[-1]


def _raw_bd_writes(frame: Frame, victim: str) -> dict[str, Run]:
    """Every raw bd write verb a stale replica might run against ``victim``."""
    return {
        "create": frame.bd("create", "--title", "stale-write", "-t", "task"),
        "update": frame.bd("update", victim, "--title", "stale-title"),
        "claim": frame.bd("update", victim, "--claim"),
        "label": frame.bd("label", "add", victim, "stale"),
        "comment": frame.bd("comment", victim, "stale"),
        "close": frame.bd("close", victim),
    }


def _true_merge_head(frame: Frame) -> bool:
    head = frame.query("select hashof('HEAD') h")[0]["h"]
    # Filtered client-side: on the 2.3.5 sql-server a WHERE on dolt_commit_ancestors fails with
    # "result max1Row iterator returned more than one row" once HEAD is a merge commit.
    parents = frame.query("select commit_hash, parent_hash from dolt_commit_ancestors")
    return sum(row["commit_hash"] == head for row in parents) == 2


@pytest.mark.dolt_server
def test_stale_replica_raw_bd_writes_are_refused_after_pulling_the_epoch_bump(tmp_path):
    """Both bd engines: a non-writer's raw bd writes are refused in SQL; a true pull-merge that
    delivers the epoch bump is not (triggers do not fire on bd's merge path); reads keep working.
    ``bd sync`` merges too -- and then publishes the stale frame's pre-bump commit, which is the
    hole bh-vje85's epoch FK closes, not this guard."""
    with Cluster(tmp_path / "cl", [("a", BD_EMBEDDED), ("b", BD_SERVER)]) as cluster:
        a, b = cluster["a"], cluster["b"]
        rec = Recorder(cluster, remote_tables=("bh_writer",), guard_probe=wg.guard_state)
        evidence: dict = {}
        with rec.step("install", "a"):
            wg.install(a, cluster.hq.place("a").epoch)
            wg.provision(a)
            first = _create(a, "w1")
            a.push().check()
            b.pull().check()
            wg.provision(b)
        # Embedded writer: bd commits with -Am, so the trigger's mark rides in the same commit.
        assert len(_marks(a, "HEAD")) == 1

        # Non-writer on bd's server engine: every write verb refused in SQL, nothing left dirty.
        refused = _raw_bd_writes(b, first)
        evidence["server-nonwriter"] = {k: _assert_refused(v) for k, v in refused.items()}
        assert b.bd("list", "--json").ok and b.bd("show", first, "--json").ok
        assert b.query("select * from dolt_status") == []

        # a (epoch 1) writes X and does NOT push; b adopts epoch 2 and publishes Y.
        a.bd("update", first, "--title", "w1-pre-bump").check()
        x = _create(a, "x-pre-bump")
        with rec.step("adopt-b", "b"):
            assert _adopt(cluster, b) == 2
            y = _create(b, "y-after-bump")
            b.bd("label", "add", y, "post").check()
            b.bd("comment", y, "post").check()
            # Server writer: bd stages only the tables it dirtied; the mark stays uncommitted.
            evidence["server-writer-marks"] = {
                "working_set": len(_marks(b)),
                "head": len(_marks(b, "HEAD")),
                "dolt_status": b.query("select table_name, staged from dolt_status"),
            }
            b.push().check()
        assert (
            evidence["server-writer-marks"]["working_set"] > evidence["server-writer-marks"]["head"]
        )

        before = _marks(a) | _marks(b, "HEAD")
        with rec.step("stale-pull", "a"):
            pulled = a.pull()
        assert pulled.ok, pulled.output
        assert _true_merge_head(a) and {x, y} <= _ids(a)
        assert _marks(a) == before  # the merge minted no mark: no trigger fired
        assert a.query("select frame, epoch from bh_writer") == [{"frame": "b", "epoch": 2}]
        evidence["embedded-stale-after-pull"] = {
            k: _assert_refused(v) for k, v in _raw_bd_writes(a, y).items()
        }
        assert a.bd("list", "--json").ok and a.query("select * from dolt_status") == []

        # Non-main writes on the stale replica stay unrestricted (Dolt CLI: bd only writes main).
        a.dolt(
            "sql",
            "-q",
            "call dolt_checkout('-b', 'frame/a'); "
            + ISSUE_INSERT.format(id="fx-branch-a")
            + "; call dolt_commit('-Am', 'frame/a write')",
        ).check()
        a.dolt("checkout", "main").check()
        assert "fx-branch-a" not in _ids(a)

        # bd sync on the stale replica: merge OK, then it publishes X (pre-bump) onto main.
        z = _create(b, "z")
        b.push().check()
        with rec.step("stale-sync", "a") as step:
            synced = a.bd("sync", "--json")
        evidence["embedded-stale-sync"] = synced.output.strip()[-400:]
        assert synced.ok, synced.output
        assert z in _ids(a)
        remote_ids = {r["id"] for r in cluster.remote.view(("issues",))["tables"]["issues"]}
        assert x in remote_ids  # landed after the bump: the guard alone does not fence this
        assert step["after"]["remote"]["main"] != step["before"]["remote"]["main"]

        # Mirror image on the server engine: b (epoch 2) writes W unpushed, a adopts epoch 3.
        w = _create(b, "w-pre-bump")
        uncommitted = _marks(b) - _marks(b, "HEAD")
        assert _adopt(cluster, a) == 3
        v = _create(a, "v-after-bump")
        a.push().check()
        with rec.step("stale-pull", "b"):
            pulled = b.pull()
        assert pulled.ok, pulled.output
        assert _true_merge_head(b) and {w, v} <= _ids(b)
        # bd's pull first commits pending work ("auto-commit before pull"): the stale marks
        # finally enter history here, in a commit bd made, not in the write's own commit.
        assert uncommitted <= _marks(b, "HEAD")
        evidence["server-stale-after-pull"] = {
            k: _assert_refused(v) for k, v in _raw_bd_writes(b, v).items()
        }
        assert b.bd("list", "--json").ok
        evidence["recorder"] = [
            {k: e[k] for k in ("step", "phase", "frame")}
            | {
                "hq": e["hq"],
                "remote_writer": e["remote"]["tables"]["bh_writer"],
                "guards": e["frames"],
            }
            for e in rec.entries
        ]
        _evidence("bd-engines-stale-replica", evidence)


# bd 1.3's own migration 0060 (an idempotent ALTER ... ADD COLUMN on issues), inlined so the test
# does not need bd's source tree. Rolling the cursor back to 58 makes bd re-run 0059 (whose
# `UPDATE issues SET is_blocked = 0, updated_at = updated_at` touches every issue row) and 0060.
ROLLBACK_59_60 = """
alter table issues drop column storage_class;
alter table wisps drop column storage_class;
delete from schema_migrations where version >= 59;
"""


def _triggers(frame: Frame) -> int:
    return int(
        frame.query(
            "select count(*) n from information_schema.triggers "
            "where trigger_schema = database() and trigger_name like 'bh_guard_%'"
        )[0]["n"]
    )


def _has_column(frame: Frame, table: str, column: str) -> bool:
    return bool(
        frame.query(
            "select count(*) n from information_schema.columns where table_schema = database() "
            f"and table_name = '{table}' and column_name = '{column}'"
        )[0]["n"]
    )


@pytest.mark.dolt_server
def test_import_migrations_doctor_and_fresh_joins_with_the_guard(tmp_path):
    """``bd import`` and DML migrations fire the guard (writer: pass and mark; non-writer:
    refused); DDL migrations keep the triggers; ``bd doctor`` neither flags nor drops them; a fresh
    ``bd init`` join of a guarded hive works and its main writes fail closed until provisioned."""
    frames = [("a", BD_EMBEDDED), ("b", BD_SERVER), ("c", BD_EMBEDDED), ("d", BD_SERVER)]
    with Cluster(tmp_path / "cl", frames) as cluster:
        a, b, c, d = (cluster[n] for n in "abcd")
        evidence: dict = {}
        wg.install(a, cluster.hq.place("a").epoch)
        wg.provision(a)
        for i in range(3):
            _create(a, f"seed-{i}")
        a.push().check()
        b.pull().check()
        wg.provision(b)
        expected = 3 * len(wg.GUARDED_TABLES)
        assert _triggers(a) == _triggers(b) == expected

        # bd import: DML through bd's import path fires the guard on both engines.
        jsonl = "".join(
            json.dumps({"title": f"imp-{i}", "issue_type": "task", "labels": ["imp"]}) + "\n"
            for i in range(3)
        )
        for frame in (a, b):
            (frame.dir / "imp.jsonl").write_text(jsonl)
        marks = len(_marks(a))
        imported = a.bd("import", str(a.dir / "imp.jsonl"), "--json")
        assert imported.ok and json.loads(imported.stdout)["created"] == 3, imported.output
        assert len(_marks(a)) > marks
        evidence["import-writer-marks"] = len(_marks(a)) - marks
        evidence["import-nonwriter"] = _assert_refused(b.bd("import", str(b.dir / "imp.jsonl")))
        assert b.query("select * from dolt_status") == []

        # bd doctor on the shared-server frame (embedded mode supports almost no checks).
        doctor = {
            "default": b.bd("doctor"),
            "server": b.bd("doctor", "--server"),
            "validate": b.bd("doctor", "--check=validate"),
            "fix": b.bd("doctor", "--fix", "--yes"),
        }
        for run in doctor.values():
            assert "bh_" not in run.output and "trigger" not in run.output.lower(), run.output
        assert doctor["server"].ok and doctor["validate"].ok
        assert _triggers(b) == expected and wg.guard_state(b)["ident"] == "b"
        evidence["doctor"] = {k: v.output.strip().splitlines()[-1] for k, v in doctor.items()}
        evidence["doctor-embedded"] = a.bd("doctor").output.strip().splitlines()[0]

        # Migrations on the writer (embedded engine): 0059's multi-row UPDATE passes the guard
        # and stamps a mark; 0060's ALTER keeps every trigger.
        wg.run_sql_script(a, ROLLBACK_59_60, "rollback.sql").check()
        a.commit("test: roll schema back to v58").check()
        assert not _has_column(a, "issues", "storage_class")
        marks = len(_marks(a))
        migrated = a.bd("migrate", "schema", "--force")
        assert migrated.ok, migrated.output
        assert a.query("select max(version) v from schema_migrations") == [{"v": 66}]
        assert _has_column(a, "issues", "storage_class") and _triggers(a) == expected
        evidence["migrate-writer-embedded-new-marks"] = len(_marks(a)) - marks
        assert evidence["migrate-writer-embedded-new-marks"] >= 1

        # The same rollback on the non-writer (server engine): its local migration is refused
        # by the guard at 0059, leaving bd's migration lock behind on a broken connection.
        wg.run_sql_script(b, ROLLBACK_59_60, "rollback.sql").check()
        wg.run_sql_script(b, "call dolt_commit('-Am', 'test: roll back to v58');\n").check()
        refused = b.bd("migrate", "schema", "--force")
        evidence["migrate-nonwriter-server"] = _assert_refused(refused)
        assert "0059_recompute_null_gate_is_blocked" in refused.output
        # ... and the identical command passes once b is the writer (adopt through SQL: bd itself
        # refuses every command on a schema behind its binary).
        epoch = cluster.hq.place("b").epoch
        wg.run_sql_script(b, f"update bh_writer set frame = 'b', epoch = {epoch};\n").check()
        migrated = b.bd("migrate", "schema", "--force")
        assert migrated.ok, migrated.output
        assert _has_column(b, "issues", "storage_class") and _triggers(b) == expected
        evidence["migrate-writer-server"] = migrated.output.strip().splitlines()[0]

        # Fresh joins of the guarded hive: wipe c and d and `bd init` them from the remote.
        a.pull().check()
        a.push().check()
        for frame in (c, d):
            if frame.kind == BD_SERVER:
                from harness.world import reap_dolt_server

                frame.bd("dolt", "stop", timeout=30)
                reap_dolt_server(frame.server_dir)
                shutil.rmtree(frame.server_dir, ignore_errors=True)
            shutil.rmtree(frame.hive / ".beads")
            joined = frame.run("bd", *cluster._init_args(frame), timeout=180)
            assert joined.ok and "Warning" not in joined.output, joined.output
            assert _triggers(frame) == expected and wg.guard_state(frame)["ident"] is None
            assert frame.bd("list", "--json").ok and frame.pull().ok
            unprovisioned = frame.bd("create", "--title", "unprovisioned", "-t", "task")
            evidence[f"join-{frame.kind}-unprovisioned"] = _assert_refused(
                unprovisioned, wg.NO_IDENT_REFUSAL
            )
            branch = wg.run_sql_script(
                frame,
                "call dolt_checkout('-b', 'frame/x');\n"
                + ISSUE_INSERT.format(id=f"{frame.name}-branch")
                + ";\ncall dolt_commit('-Am', 'branch write');\ncall dolt_checkout('main');\n",
                "branch.sql",
            )
            assert branch.ok, branch.output
            wg.provision(frame)
            evidence[f"join-{frame.kind}-provisioned"] = _assert_refused(
                frame.bd("create", "--title", "provisioned", "-t", "task")
            )
        _evidence("import-migrations-doctor-join", evidence)


# --------------------------------------------------------------------------------------------
# 3. Non-primary write path A: forward -- bd on another frame points at the primary's server

_RELEASE = (
    "import os, sys, time\n"
    "gate = sys.argv[1]\n"
    "while not os.path.exists(gate):\n"
    "    time.sleep(0.0005)\n"
    "os.execvp('bd', ['bd', *sys.argv[2:]])\n"
)


class _Forwarder:
    """A bd workspace on frame ``host`` whose metadata points at ``primary``'s sql-server --
    what a non-primary frame on the LAN does on the forward path. ``bd`` runs as any actor."""

    def __init__(self, host: Frame, primary: Frame, node_id: str):
        self.dir = host.dir / "forward"
        (self.dir / ".beads").mkdir(parents=True)
        os.chmod(self.dir / ".beads", 0o700)
        meta = json.loads((primary.hive / ".beads" / "metadata.json").read_text())
        meta.update(
            {
                "dolt_mode": "server",
                "dolt_server_host": "127.0.0.1",
                "dolt_server_port": primary.port,
            }
        )
        (self.dir / ".beads" / "metadata.json").write_text(json.dumps(meta))
        # One replica = one store: every client of the primary's server shares its node id.
        self.env = dict(host.env, BEADS_NODE_ID=node_id)

    def bd(self, *args: str, actor: str = "fwd") -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bd", *args],
            cwd=self.dir,
            env=dict(self.env, BEADS_ACTOR=actor),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def race(self, bead: str, actors: list[str], gate: Path) -> dict[str, int]:
        """Start one ``bd update --claim`` per actor, all released by one gate file."""
        procs = {
            actor: subprocess.Popen(
                [os.environ.get("PYTHON", "python3"), "-c", _RELEASE, str(gate)]
                + ["update", bead, "--claim", "--json"],
                cwd=self.dir,
                env=dict(self.env, BEADS_ACTOR=actor),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for actor in actors
        }
        time.sleep(0.3)  # every racer is spinning on the gate
        gate.touch()
        return {actor: proc.wait(120) for actor, proc in procs.items()}


def _claim_won(row: dict, actor: str) -> bool:
    """bh's ``work_next.claim_won`` read-back, verbatim semantics."""
    return row.get("assignee") == actor and str(row.get("status") or "") not in (
        "",
        "open",
        "closed",
    )


@pytest.mark.dolt_server
def test_forward_path_claims_leases_and_failover(tmp_path):
    """Forwarded bd writes execute on the primary, so the primary's guard admits them and stamps
    marks there. Concurrent ``bd update --claim`` through one server: measured double-win rate
    and claim_won read-back. Heartbeat/reclaim work on the granting replica; after failover the
    new primary has no lease rows, so ``bd reclaim`` (even ``--any-replica``) cannot see the dead
    primary's claims and bh must revert them itself. A forwarder pointed at a demoted primary is
    refused once that primary pulls the bump."""
    rounds = int(os.environ.get("BH_SIEAI_CLAIM_ROUNDS", "20"))
    racers = int(os.environ.get("BH_SIEAI_CLAIM_RACERS", "6"))
    # Founder must be embedded: an embedded bd cannot open a store a bd-server founder created
    # ("schema skew check: probing schema_migrations existence: table has unknown fields").
    frames = [("q", BD_EMBEDDED), ("p", BD_SERVER), ("f", DOLT_CLI)]
    with Cluster(tmp_path / "cl", frames) as cluster:
        p, q, f = cluster["p"], cluster["q"], cluster["f"]
        evidence: dict = {}
        p.pull().check()
        wg.install(p, cluster.hq.place("p").epoch)
        wg.provision(p)
        p.push().check()
        q.pull().check()
        wg.provision(q)
        fwd = _Forwarder(f, p, node_id="p")

        made = fwd.bd("create", "--title", "forwarded", "-t", "task", "--json", actor="f")
        assert made.returncode == 0, made.stdout + made.stderr
        assert json.loads(made.stdout)["created_by"] == "f"
        assert len(_marks(p)) == 1  # stamped on the primary (working set: server mode)

        # Claim race through the primary's server.
        beads = [
            json.loads(fwd.bd("create", "--title", f"race-{i}", "-t", "task", "--json").stdout)[
                "id"
            ]
            for i in range(rounds)
        ]
        actors = [f"w{i}" for i in range(racers)]
        tally = {
            "rounds": rounds,
            "racers": racers,
            "exit0": Counter(),
            "readback_winners": Counter(),
        }
        started = time.monotonic()
        for n, bead in enumerate(beads):
            codes = f.dir / f"gate-{n}"
            exits = fwd.race(bead, actors, codes)
            row = json.loads(fwd.bd("show", bead, "--json").stdout)[0]
            winners = [a for a, code in exits.items() if code == 0]
            tally["exit0"][len(winners)] += 1
            tally["readback_winners"][sum(_claim_won(row, a) for a in actors)] += 1
            assert set(winners) <= {row["assignee"]}, (bead, exits, row)
        tally["seconds"] = round(time.monotonic() - started, 1)
        evidence["claim-race"] = {
            k: dict(v) if isinstance(v, Counter) else v for k, v in tally.items()
        }
        assert tally["exit0"] == Counter({1: rounds}), tally  # never two exit-0 claimers
        assert tally["readback_winners"] == Counter({1: rounds}), tally

        # Heartbeat / reclaim on the granting replica.
        held = beads[0]
        holder = json.loads(fwd.bd("show", held, "--json").stdout)[0]["assignee"]
        assert fwd.bd("heartbeat", held, actor=holder).returncode == 0
        other = fwd.bd("heartbeat", held, actor="intruder")
        assert other.returncode != 0 and "already claimed" in other.stdout + other.stderr
        lease = p.query(f"select holder, granted_node from leases where issue_id = '{held}'")
        assert lease == [{"holder": holder, "granted_node": "p"}]
        assert json.loads(fwd.bd("reclaim", "--older-than", "0s", "--json").stdout)["count"] == 0
        p.sql(
            f"update leases set lease_expires_at = '2000-01-01' where issue_id = '{held}'"
        ).check()
        reaped = json.loads(fwd.bd("reclaim", "--older-than", "0s", "--json").stdout)
        assert reaped["count"] == 1 and reaped["reclaimed"][0]["id"] == held
        evidence["reclaim-granting-replica"] = reaped

        # Failover: p dies holding claims; q adopts and sees them in_progress with no leases.
        stranded = beads[1]
        p.push().check()
        p.kill()
        assert _adopt(cluster, q) == 2
        q.pull().check()
        row = json.loads(q.bd("show", stranded, "--json").check().stdout)[0]
        assert row["status"] == "in_progress"
        assert q.query("select count(*) n from leases") == [{"n": 0}]  # leases are dolt_ignore'd
        q.env["BEADS_NODE_ID"] = "q"
        reclaims = {
            "default": q.bd("reclaim", "--older-than", "0s", "--json"),
            "any-replica": q.bd("reclaim", "--any-replica", "--older-than", "0s", "--json"),
            "any-replica-id": q.bd(
                "reclaim", "--any-replica", "--id", stranded, "--older-than", "0s", "--json"
            ),
        }
        for run in reclaims.values():
            assert json.loads(run.check().stdout)["count"] == 0
        q.bd("unclaim", stranded, "--force").check()  # what bh has to do itself
        assert json.loads(q.bd("show", stranded, "--json").stdout)[0]["status"] == "open"
        evidence["failover-in-progress"] = sum(
            r["status"] == "in_progress" for r in json.loads(q.bd("list", "--json", "--all").stdout)
        )
        q.push().check()

        # The dead primary comes back stale: forwarded writes follow its guard and are refused.
        p.restart()
        p.pull().check()
        late = fwd.bd("create", "--title", "late", "-t", "task", actor="f")
        assert late.returncode != 0 and wg.GUARD_REFUSAL in late.stdout + late.stderr
        evidence["forward-to-demoted-primary"] = (
            (late.stdout + late.stderr).strip().splitlines()[-1]
        )
        _evidence("forward-path", evidence)


# --------------------------------------------------------------------------------------------
# 4. Non-primary write path B: branch -- commit locally, publish frame/<id>, primary merges


def _cas_calls(frame: Frame, since: int) -> int:
    return sum(e["point"] == CAS for e in frame.gate_log()[since:])


def _push_branch(frame: Frame, target: str = "", remote: str = "origin"):
    """Publish the frame's local ``main`` (bd's only branch) as ``target`` on ``remote``."""
    refspec = f"main:{target}" if target else "main"
    return frame.spawn("dolt", "push", remote, refspec, cwd=frame.dolt_dir)


def _race_pushes(first, second, *, hold: bool) -> dict:
    """Push two frames at once; with ``hold`` both park at their first CAS and are released
    together, otherwise they free-run. Returns ok flags, wall seconds and CAS calls per side."""
    (fa, start_a), (fb, start_b) = first, second
    logs = (len(fa.gate_log()), len(fb.gate_log()))
    holds = (fa.hold(CAS), fb.hold(CAS)) if hold else ()
    started = time.monotonic()
    pending = (start_a(), start_b())
    for h in holds:
        h.wait()
    for h in holds:
        h.release()
    runs = [p.result() for p in pending]
    return {
        "ok": [r.ok for r in runs],
        "seconds": round(time.monotonic() - started, 2),
        "cas": [_cas_calls(fa, logs[0]), _cas_calls(fb, logs[1])],
        "rejected": [("non-fast-forward" in r.output) for r in runs],
    }


@pytest.mark.dolt_server
def test_branch_path_merges_contention_and_raw_push_hazard(tmp_path):
    """Branch-role frames write bd's local ``main`` and publish it as ``frame/<id>``; the primary
    merges. Child IDs collide (#4796) and both branches' claims "win" locally, so the second merge
    conflicts and ``--strategy`` silently drops one side; labels union cleanly (#5657 does not
    arise across branches). Branch pushes share the ``refs/dolt/data`` manifest CAS with main
    pushes (measured), a frame-private data ref does not. A branch-role frame's raw
    ``bd dolt push`` publishes straight to ``main``."""
    rounds = int(os.environ.get("BH_SIEAI_PUSH_ROUNDS", "6"))
    frames = [("p", BD_EMBEDDED), ("b1", BD_EMBEDDED), ("b2", BD_EMBEDDED)]
    with Cluster(tmp_path / "cl", frames) as cluster:
        p, b1, b2 = cluster["p"], cluster["b1"], cluster["b2"]
        evidence: dict = {}
        wg.install(p, cluster.hq.place("p").epoch)
        wg.provision(p)
        epic = json.loads(p.bd("create", "--title", "epic", "-t", "epic", "--json").stdout)["id"]
        labelled, contested = _create(p, "labelled"), _create(p, "contested")
        p.push().check()
        for b in (b1, b2):
            b.pull().check()
            wg.provision(b, role="branch")
            child = b.bd(
                "create", "--title", f"child-{b.name}", "-t", "task", "--parent", epic, "--json"
            )
            assert json.loads(child.check().stdout)["id"] == f"{epic}.1"  # both: #4796
            b.bd("label", "add", labelled, f"lab-{b.name}").check()
            b.bd("update", contested, "--claim").check()  # both claims "win" locally
            _push_branch(b, f"frame/{b.name}").result().check()
        remote = cluster.remote.view(("issues",))
        assert f"{epic}.1" not in {r["id"] for r in remote["tables"]["issues"]}  # main untouched

        p.dolt("fetch", "origin").check()
        p.bd("vc", "merge", "origin/frame/b1").check()
        conflicted = p.bd("vc", "merge", "origin/frame/b2")
        assert not conflicted.ok and "Merge conflict detected" in conflicted.output
        evidence["second-merge"] = conflicted.output.strip().splitlines()[-1][:300]
        assert p.query("select * from dolt_status") == []  # rolled back, nothing half-merged
        forced = p.bd("vc", "merge", "origin/frame/b2", "--strategy", "ours")
        assert forced.ok, forced.output
        rows = {r["id"]: r for r in p.query("select id, title, assignee, status from issues")}
        labels = {
            r["label"] for r in p.query(f"select label from labels where issue_id = '{labelled}'")
        }
        evidence["after-strategy-ours"] = {
            "child": rows[f"{epic}.1"]["title"],
            "contested_assignee": rows[contested]["assignee"],
            "labels": sorted(labels),
            "child_b2_present": any(r["title"] == "child-b2" for r in rows.values()),
        }
        assert rows[f"{epic}.1"]["title"] == "child-b1"
        assert not evidence["after-strategy-ours"]["child_b2_present"]  # silently dropped
        assert rows[contested]["assignee"] == "b1"  # b2 still believes it holds the claim
        assert labels == {"lab-b1", "lab-b2"}  # disjoint label rows union cleanly
        p.push().check()

        # Hazard: a branch-role frame running raw `bd dolt pull` + `bd dolt push` publishes to main.
        b1.bd("dolt", "pull").check()
        stray = _create(b1, "stray-from-branch-frame")
        assert b1.bd("dolt", "push").ok
        assert stray in {r["id"] for r in cluster.remote.view(("issues",))["tables"]["issues"]}
        p.pull().check()

        # Frame-private data ref: b2's `origin` is the same Git remote under refs/dolt/frame/b2.
        url = cluster.remote.url
        b2.dolt("remote", "remove", "origin").check()
        b2.dolt("remote", "add", "--ref", "refs/dolt/frame/b2", "origin", url).check()
        b2.dolt("remote", "add", "hive", url).check()
        private = _create(b2, "private-ref-write")
        before = cluster.remote.view(("issues",))
        pushed = b2.bd("dolt", "push")
        assert pushed.ok, pushed.output
        assert cluster.remote.view(("issues",))["main"] == before["main"]
        refs = subprocess.run(
            ["git", "--git-dir", str(cluster.remote.path), "for-each-ref", "--format=%(refname)"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        assert "refs/dolt/frame/b2" in refs and "refs/dolt/data" in refs
        p.dolt("remote", "add", "--ref", "refs/dolt/frame/b2", "frame-b2", url).check()
        p.dolt("fetch", "frame-b2").check()
        merged = p.bd("vc", "merge", "frame-b2/main", "--strategy", "ours")
        assert merged.ok and private in _ids(p), merged.output
        p.push().check()
        evidence["private-ref"] = {"refs": refs, "merge": merged.output.strip().splitlines()[-1]}

        # Manifest contention: main push (primary, bd) against frame/b1 push (Dolt CLI), forced
        # into the same CAS window and free-running; then the private ref; then a same-branch
        # control where exactly one of two main pushes may win.
        b1.bd("dolt", "pull").check()
        trials: dict[str, list] = {
            "branch-held": [],
            "branch-free": [],
            "private-free": [],
            "main-held": [],
        }
        for n in range(rounds):
            for kind in ("branch-held", "branch-free", "private-free"):
                _create(p, f"p-{kind}-{n}")
                other = b2 if kind == "private-free" else b1
                _create(other, f"{other.name}-{kind}-{n}")
                start_other = (
                    (lambda o=other: o.spawn("bd", "dolt", "push"))
                    if kind == "private-free"
                    else (lambda o=other: _push_branch(o, f"frame/{o.name}"))
                )
                trials[kind].append(
                    _race_pushes(
                        (p, p.push_async), (other, start_other), hold=kind.endswith("held")
                    )
                )
        for n in range(rounds):
            b1.bd("dolt", "pull").check()
            _create(p, f"p-main-{n}")
            _create(b1, f"b1-main-{n}")
            trials["main-held"].append(
                _race_pushes((p, p.push_async), (b1, lambda: _push_branch(b1)), hold=True)
            )
            p.pull().check()
        summary = {}
        for kind, results in trials.items():
            summary[kind] = {
                "rounds": len(results),
                "both_ok": sum(all(r["ok"]) for r in results),
                "one_rejected": sum(r["ok"].count(False) == 1 for r in results),
                "cas_calls": [r["cas"] for r in results],
                "seconds_max": max(r["seconds"] for r in results),
                "seconds_median": sorted(r["seconds"] for r in results)[len(results) // 2],
            }
        evidence["contention"] = summary
        assert summary["branch-held"]["both_ok"] == rounds
        assert summary["branch-free"]["both_ok"] == rounds
        assert summary["private-free"]["both_ok"] == rounds
        assert summary["main-held"]["one_rejected"] == rounds
        _evidence("branch-path", evidence)
