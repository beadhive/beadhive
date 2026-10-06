"""The product fence + guard on the pinned Dolt/bd (bh-uz46l), reusing the F3 canary fixtures.

* Dolt CLI repos (:class:`harness.trigger_canary.DoltRepo`, own HOME / DOLT_ROOT_PATH under
  ``tmp_path``) for the embedded-mode engine: install order and idempotence, the 44-trigger
  check, the canary's mark/refusal matrix against the PRODUCT guard, ``bh_local_ident``
  provisioning, the unguarded tables, and ``fence_audit`` on a ``file://`` remote — including
  the ``bh-uhx2r`` E2 re-stamped late write that only the history check sees.
* The bh-eybn7 fixture (:mod:`harness.writer_fencing`) for real bd: an embedded bd store (the
  Dolt CLI on ``.beads/embeddeddolt/<db>``) and a server-mode bd (``bd sql`` + ``bd dolt
  commit``), with bd's own writes stamped and a non-writer refused.

Marked ``fence_canary`` too: these are condition-9 checks of the product DDL, so they re-run on
every Dolt or bd pin bump with the rest of the canary (``just fence-canary``).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from beadhive import fence_audit as fa
from beadhive import fence_data as fd
from beadhive import fence_schema as fs
from beadhive import writer_adopt as wa
from harness import trigger_canary as tc
from harness.writer_fencing import BD_EMBEDDED, BD_SERVER, DOLT_CLI, Cluster

pytestmark = [
    pytest.mark.integration,
    pytest.mark.fence_canary,
    pytest.mark.skipif(
        shutil.which("bd") is None or shutil.which("dolt") is None, reason="bd/dolt not installed"
    ),
]

expect = tc.expect

#: The 13 guarded tables besides ``issues``, as stubs, plus the two left unguarded.
_STUBS = "\n".join(
    [tc.ISSUES_DDL]
    + [
        f"create table `{t}` (id varchar(64) primary key, v text);"
        for t in fs.GUARDED_TABLES
        if t != "issues"
    ]
    + [
        "create table child_counters (parent_id varchar(64) primary key, last_child int);",
        "create table metadata (`key` varchar(64) primary key, value text);",
    ]
)


def _repo(root: Path, name: str) -> tc.DoltRepo:
    repo = tc.DoltRepo(root, name)
    repo.ok("sql", stdin=_STUBS)
    repo.ok("sql", "-q", "call dolt_commit('-Am', 'bd schema stand-in')")
    return repo


def _node(repo: tc.DoltRepo) -> fd.FenceNode:
    return fd.FenceNode(fd.DoltCliEngine(repo.dir, env=repo.env))


def _clone(root: Path, name: str, url: str) -> tc.DoltRepo:
    repo = tc.DoltRepo(root, name)
    shutil.rmtree(repo.dir)
    subprocess.run(
        ["dolt", "clone", url, str(repo.dir)],
        cwd=root,
        env=repo.env,
        check=True,
        capture_output=True,
        text=True,
    )
    return repo


class _Placement:
    """HQ placement stand-in for writer_adopt.run_step2 (the M2 port)."""

    def __init__(self, frame: str, epoch: int):
        self.view = wa.PlacementView(frame, epoch)

    def read(self):
        return self.view

    def cas(self, frame, epoch, *, expected):  # pragma: no cover - step 2 never CASes
        raise AssertionError("step 2 must not move placement")


# =============================================================================================
# Install, the 44-trigger check, the guard and identity (embedded engine: the Dolt CLI)
# =============================================================================================


def test_install_is_idempotent_in_composed_order_and_leaves_exactly_44(tmp_path):
    repo = _repo(tmp_path, "a")
    node = _node(repo)
    assert not node.is_cut_over()
    first = node.install("a", 7)
    expect(
        first.guard.total == fs.TRIGGER_COUNT == 44 and first.guard.complete,
        "the product install leaves exactly 44 bh_* triggers and the guard procedure",
        first.guard.describe(),
    )
    assert first.seeded and first.committed
    assert (first.writer.frame, first.writer.epoch) == ("a", 7)
    assert node.history_max_epoch() == 7
    marks = repo.rows("select id, epoch, tbl from bh_write_mark")
    assert marks == [{"id": "adopt-7", "epoch": 7, "tbl": "bh_writer"}]
    assert repo.rows("select table_name from dolt_status") == []

    again = node.install("z", 99)
    assert not again.seeded
    assert (again.writer.frame, again.writer.epoch, again.writer.revision) == (
        "a",
        7,
        first.writer.revision,
    )
    assert again.guard.total == 44 and node.trigger_count() == 44
    assert repo.rows("select count(*) n from bh_write_mark") == [{"n": 1}]
    assert repo.rows("select table_name from dolt_status") == []


def test_a_node_short_of_44_refuses_to_act_as_writer(tmp_path):
    repo = _repo(tmp_path, "a")
    node = _node(repo)
    node.install("a", 1)
    repo.ok("sql", "-q", "drop trigger bh_guard_labels_del")
    assert node.trigger_count() == 43
    with pytest.raises(fd.GuardIncomplete, match="43 of 44.*bh_guard_labels_del"):
        node.require_writer_ready()
    node.install("a", 1)  # the re-install repairs it
    assert node.require_writer_ready().complete


def test_product_guard_marks_every_writer_statement_once_and_refuses_a_non_writer(tmp_path):
    """The canary's mark matrix (tests/harness/trigger_canary.check_guard_marks) on the PRODUCT
    guard: autocommit and bd-style explicit transactions, single- and multi-row."""
    repo = _repo(tmp_path, "a")
    node = _node(repo)
    node.install("A", 7)
    node.provision_ident("A")
    multi = tc.ISSUE_INSERT.format(id="{a}") + ", ('{b}', '{b}', '', '', '', '')"
    statements = {
        "autocommit single-row": tc.ISSUE_INSERT.format(id="a1"),
        "explicit-transaction single-row": "begin;\n"
        + tc.ISSUE_INSERT.format(id="a2")
        + ";\ncommit;",
        "autocommit multi-row": multi.format(a="a3", b="a4"),
        "explicit-transaction multi-row": "begin;\n" + multi.format(a="a5", b="a6") + ";\ncommit;",
    }
    for label, stmt in statements.items():
        before = repo.count("select count(*) from bh_write_mark where tbl = 'issues'")
        res = repo.sql(stmt)
        expect(res.returncode == 0, "the writer's own guarded write is accepted", label)
        after = repo.count("select count(*) from bh_write_mark where tbl = 'issues'")
        expect(after - before == 1, "one inline mark per guarded statement", label)
    expect(
        repo.rows("select distinct epoch from bh_write_mark where tbl = 'issues'")
        == [{"epoch": 7}],
        "every mark carries the writer's epoch",
    )

    node.provision_ident("B")
    before = repo.count("select count(*) from bh_write_mark")
    refused = tc.refusal(repo.sql("begin;\n" + tc.ISSUE_INSERT.format(id="b1") + ";\ncommit;"))
    expect(
        refused is not None and fs.GUARD_REFUSAL in refused,
        "the product guard refuses a non-writer's write",
        refused or "accepted",
    )
    expect(repo.count("select count(*) from bh_write_mark") == before, "refusal rolls back")
    for table in ("labels", "config", "provenance_events"):
        res = repo.sql(f"insert into `{table}` (id, v) values ('x', 'y')")
        expect(tc.refusal(res) is not None, f"{table} is guarded", table)

    # child_counters, metadata and the ignored tables stay unguarded (bh-sieai E6)
    for stmt in (
        "insert into child_counters values ('p', 1)",
        "insert into metadata values ('clone_id', 'b')",
        "delete cc from child_counters cc join issues i on i.id = cc.parent_id",
    ):
        res = repo.sql(stmt)
        expect(res.returncode == 0, "child_counters / metadata stay unguarded", stmt)
    on_unguarded = repo.rows(
        "select trigger_name from information_schema.triggers where trigger_schema = database() "
        "and event_object_table in ('child_counters', 'metadata', 'bh_local_ident')"
    )
    assert on_unguarded == []
    # the guard scopes itself to main
    repo.ok("sql", "-q", "call dolt_checkout('-b', 'side')")
    repo.ok(
        "sql",
        "-q",
        "call dolt_checkout('side'); " + tc.ISSUE_INSERT.format(id="side1"),
    )


def test_ident_provisioning_refuses_before_the_ignore_row_is_committed(tmp_path):
    repo = _repo(tmp_path, "a")
    node = _node(repo)
    node.engine.execute(fs.fence_table_statements())  # the ignore row, NOT yet committed
    with pytest.raises(fd.IdentNotIgnored):
        node.provision_ident("a")
    assert "bh_local_ident" not in {next(iter(r.values())) for r in repo.rows("show tables")}
    node.install("a", 1)
    node.provision_ident("a")
    assert node.ident() == ("a", "replica")
    expect(
        node.staged_local_tables() == []
        and repo.rows("select table_name from dolt_status where table_name like 'bh_local_%'")
        == [],
        "dolt_ignore'd bh_local_% stays unstaged",
    )
    repo.ok("add", "-A")
    head = {next(iter(r.values())) for r in repo.rows("show tables as of 'HEAD'")}
    assert "bh_local_ident" not in head


# =============================================================================================
# fence_audit and the history check on a file:// remote (bh-uhx2r E2)
# =============================================================================================


@pytest.fixture
def adopted(tmp_path):
    """``a`` founds the hive at epoch 1 and publishes; ``b`` clones and adopts at epoch 2 through
    M2's step 2 on the product adapter. ``a`` has not synced: it still names itself."""
    url = f"file://{tmp_path / 'remote'}"
    a = _repo(tmp_path, "a")
    node_a = _node(a)
    node_a.install("a", 1)
    node_a.provision_ident("a")
    a.ok("remote", "add", "origin", url)
    a.ok("push", "origin", "main")
    b = _clone(tmp_path, "b", url)
    node_b = _node(b)
    node_b.provision_ident("b")
    result = wa.run_step2(node_b, _Placement("b", 2), frame="b", epoch=2)
    assert result.landed, result
    assert node_b.remote_writer() == wa.WriterRow("b", 2, result.revision or "")
    return a, node_a, b, node_b


def _stale_write(a: tc.DoltRepo, title: str) -> str:
    a.ok("sql", "-q", tc.ISSUE_INSERT.format(id=title))  # a still names itself: accepted
    a.ok("sql", "-q", f"call dolt_commit('-Am', '{title}')")
    return a.rows("select hashof('HEAD') h")[0]["h"]


def test_audit_is_clean_after_an_honest_adopt(adopted):
    _, _, _, node_b = adopted
    audit = fa.fence_audit(node_b, placement=wa.PlacementView("b", 2))
    assert audit.cut_over and audit.ok, audit.findings()
    assert (audit.writer_frame, audit.writer_epoch, audit.live_epoch) == ("b", 2, 2)
    assert len(audit.adopt_commits) == 1 and audit.late_writes == ()


def test_e2_restamped_late_write_is_seen_only_by_the_history_check(adopted):
    """bh-uhx2r E2: the stale node force-merges the bump, re-stamps its mark and deletes the
    violation rows; its next push fast-forwards. stale_marks / epoch_regressed /
    placement_ahead stay clean — the history check reports the late write."""
    a, _, _, node_b = adopted
    stale = _stale_write(a, "e2-stale")
    a.ok("fetch", "origin")
    a.ok(
        "sql",
        "-q",
        "SET @@dolt_force_transaction_commit = 1; CALL DOLT_MERGE('origin/main'); "
        "SET @live = (SELECT epoch FROM bh_epoch_live WHERE id = 1); "
        "UPDATE bh_write_mark SET epoch = @live WHERE epoch <> @live; "
        "DELETE FROM dolt_constraint_violations_bh_write_mark; "
        "CALL DOLT_COMMIT('-Am', 'routine')",
    )
    a.ok("push", "origin", "main")

    audit = fa.fence_audit(node_b, placement=wa.PlacementView("b", 2))
    assert audit.stale_mark_count == 0
    assert not audit.epoch_regressed and not audit.placement_ahead
    assert [(w.commit, w.epoch, w.adopt_epoch) for w in audit.late_writes] == [(stale, 1, 2)]
    assert audit.late_writes[0].message == "e2-stale"
    assert [f.split(":")[0] for f in audit.findings()] == ["late_write"]
    # pure read: the audit moved nothing but the remote-tracking ref
    assert node_b.query("select table_name from dolt_status") == []


def test_the_writers_deliberate_orphan_merge_is_not_a_late_write(adopted):
    a, _, b, node_b = adopted
    _stale_write(a, "orphaned")
    a.ok("push", "origin", "main:frame/a/orphan-1-abc")
    b.ok("fetch", "origin")
    b.ok(
        "sql",
        "-q",
        "SET @@dolt_force_transaction_commit = 1; "
        "CALL DOLT_MERGE('--no-ff', '-m', 'bh: merge frame/a/orphan-1-abc', "
        "'origin/frame/a/orphan-1-abc'); "
        "SET @live = (SELECT epoch FROM bh_epoch_live WHERE id = 1); "
        "UPDATE bh_write_mark SET epoch = @live WHERE epoch <> @live; "
        "DELETE FROM dolt_constraint_violations_bh_write_mark; "
        "CALL DOLT_COMMIT('-Am', 'bh: merge frame/a/orphan-1-abc')",
    )
    b.ok("push", "origin", "main")
    audit = fa.fence_audit(node_b, placement=wa.PlacementView("b", 2))
    assert audit.ok, audit.findings()


def test_stale_marks_regression_and_placement_ahead_carry_evidence(adopted):
    _, _, b, node_b = adopted
    ahead = fa.fence_audit(node_b, placement=wa.PlacementView("c", 3))
    assert ahead.placement_ahead and "c@3" in ahead.findings()[0]

    # a forced commit of a retired-epoch mark (`dolt commit --force` shape)
    b.ok(
        "sql",
        "-q",
        "SET foreign_key_checks = 0; INSERT INTO bh_write_mark VALUES ('forced', 1, 'issues'); "
        "CALL DOLT_COMMIT('-Am', 'forced')",
    )
    stale = fa.fence_audit(node_b, ref="main", fetch=False)
    assert stale.stale_mark_count == 1
    assert stale.stale_marks == (fa.StaleMark("forced", 1, "issues"),)

    # bh_writer resolved back to an older epoch (the --strategy merge shape)
    node_b.sync_to_remote()
    b.ok("sql", "-q", "drop trigger bh_writer_monotonic")
    b.ok("sql", "-q", "update bh_writer set epoch = 1; call dolt_commit('-Am', 'regress')")
    regressed = fa.fence_audit(node_b, ref="main", fetch=False)
    assert regressed.epoch_regressed and regressed.history_max_epoch == 2
    assert regressed.history_max_commit


def test_a_legacy_ref_is_not_cut_over(tmp_path):
    repo = _repo(tmp_path, "a")
    audit = fa.fence_audit(_node(repo), ref="main", fetch=False, placement=None)
    assert not audit.cut_over and audit.ok


# =============================================================================================
# Real bd: an embedded store through the Dolt CLI, and a server-mode store through bd sql
# =============================================================================================


def test_embedded_bd_store_is_switched_on_by_its_data_alone(tmp_path, monkeypatch):
    with Cluster(tmp_path / "cluster", [("a", BD_EMBEDDED), ("c", DOLT_CLI)]) as cluster:
        a, c = cluster["a"], cluster["c"]
        for key in ("HOME", "DOLT_ROOT_PATH"):
            monkeypatch.setenv(key, a.env[key])  # the resolver's dolt runs under a's HOME
        fd.reset_probe_cache()
        assert fd.resolve_cut_over(cluster.prefix, a.hive) is None  # legacy until the data says

        epoch = cluster.hq.place("a").epoch
        node_a = fd.FenceNode(fd.DoltCliEngine(a.dolt_dir, env=a.env))
        report = node_a.install("a", epoch)
        expect(
            report.guard.total == 44, "44 bh_* triggers on a real bd store", report.guard.describe()
        )
        node_a.provision_ident("a")
        a.push().check()

        resolved = fd.resolve_cut_over(cluster.prefix, a.hive)
        assert isinstance(resolved, fd.FenceNode)
        assert resolved.writer() == wa.WriterRow("a", epoch, report.writer.revision)

        before = node_a.query("select count(*) n from bh_write_mark where tbl = 'issues'")[0]["n"]
        a.create_issue("bd-write-on-the-writer").check()
        after = node_a.query("select count(*) n from bh_write_mark where tbl = 'issues'")[0]["n"]
        expect(int(after) > int(before), "bd's own write is stamped by the product guard")

        c.pull().check()
        node_c = fd.FenceNode(fd.DoltCliEngine(c.dolt_dir, env=c.env))
        assert node_c.trigger_count() == 44
        node_c.provision_ident("c")
        refused = c.sql(
            "insert into issues (id, title, description, design, acceptance_criteria, notes) "
            "values ('fx-c1', 't', '', '', '', '')"
        )
        expect(
            not refused.ok and fs.GUARD_REFUSAL in refused.output,
            "a non-writer replica is refused on main",
            refused.output,
        )
        audit = fa.fence_audit(node_c, placement=wa.PlacementView("a", epoch))
        assert audit.cut_over and audit.ok, audit.findings()


@pytest.mark.dolt_server
def test_server_mode_install_goes_through_bd_sql_and_bd_dolt_commit(tmp_path):
    with Cluster(tmp_path / "cluster", [("s", BD_SERVER)]) as cluster:
        s = cluster["s"]
        epoch = cluster.hq.place("s").epoch
        node = fd.FenceNode(fd.BdServerEngine(s.hive, env=s.env))
        report = node.install("s", epoch)
        expect(
            report.guard.total == 44,
            "44 bh_* triggers through bd's server",
            report.guard.describe(),
        )
        assert report.seeded and report.committed
        node.provision_ident("s")
        assert node.staged_local_tables() == []
        s.create_issue("bd-write-through-the-server").check()
        marks = node.query("select count(*) n from bh_write_mark where tbl = 'issues'")
        expect(int(marks[0]["n"]) >= 1, "bd's server-mode write is stamped")
        assert node.engine.commit("bh: commit the marks")  # bh-vje85 E7: marks ride a commit
