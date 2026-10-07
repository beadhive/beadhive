"""The orphan divert and the writer's orphan merge on the pinned Dolt (bh-4z3oz, P-M6).

A partitioned writer rejoins: ``a`` founded the hive at epoch 1 and kept writing while ``b``
adopted at epoch 2. ``a``'s managed path fetches, sees the higher epoch, never pushes ``main``,
publishes its unpublished commits to ``frame/a/orphan-1-<n>`` and resets. ``b`` — the writer —
merges that orphan in one SQL session, re-stamping its marks at epoch 2, and ``fence_audit`` on
the remote head stays clean. Dolt CLI repos (embedded engine) on a ``file://`` remote, reusing
the ``test_fence_data_int`` fixtures; the server-mode engine runs through real bd.
"""

from __future__ import annotations

import re
import shutil

import pytest

from beadhive import fence_audit as fa
from beadhive import fence_data as fd
from beadhive import fence_orphan as fo
from beadhive import writer_adopt as wa
from harness import trigger_canary as tc
from test_fence_data_int import _clone, _node, _Placement, _repo, _stale_write

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("bd") is None or shutil.which("dolt") is None, reason="bd/dolt not installed"
    ),
]


@pytest.fixture
def rejoined(tmp_path):
    """``a`` writes at epoch 1 while partitioned; ``b`` has adopted at epoch 2 and published."""
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
    assert wa.run_step2(node_b, _Placement("b", 2), frame="b", epoch=2).landed
    stale = [_stale_write(a, f"partitioned-{i}") for i in (1, 2)]
    return a, node_a, b, node_b, stale


def _titles(repo: tc.DoltRepo, rev: str = "main") -> set[str]:
    return {r["title"] for r in repo.rows(f"select title from issues as of '{rev}'")}


def test_partitioned_writer_rejoins_diverts_and_its_orphan_merges_restamped(rejoined):
    a, node_a, b, node_b, stale = rejoined
    remote_before = node_b.query("select hashof('origin/main') h")[0]["h"]

    out = fo.divert_if_superseded(node_a, frame="a")
    assert out.superseded and out.reset
    assert out.branch == "frame/a/orphan-1-1" and out.commit == stale[-1]
    assert (out.held.epoch, out.remote.frame, out.remote.epoch) == (1, "b", 2)
    # main was never pushed: the remote head is still b's bump
    b.ok("fetch", "origin")
    assert node_b.query("select hashof('origin/main') h")[0]["h"] == remote_before
    # a is reset to the remote head and the guard now refuses its writes on main
    assert node_a.writer() == wa.WriterRow("b", 2, node_a.writer().revision)
    assert tc.refusal(a.sql(tc.ISSUE_INSERT.format(id="after-divert"))) is not None
    # nothing lost: the orphan carries both partitioned writes
    assert {"partitioned-1", "partitioned-2"} <= _titles(b, "origin/frame/a/orphan-1-1")

    orphans = fo.list_orphans(node_b)
    assert [(o.branch, o.frame, o.epoch, o.commit) for o in orphans] == [
        ("frame/a/orphan-1-1", "a", 1, stale[-1])
    ]

    merged = fo.merge_orphan(node_b, "frame/a/orphan-1-1", frame="b")
    assert not merged.already and merged.epoch == 2 and merged.restamped == 2
    assert {"partitioned-1", "partitioned-2"} <= _titles(b)
    assert b.rows("select count(*) n from bh_write_mark where epoch <> 2") == [{"n": 0}]
    assert b.rows("select `table` from dolt_constraint_violations") == []
    subject = b.rows("select message from dolt_log limit 1")[0]["message"]
    assert subject.startswith(fa.MERGE_PREFIX) and "epoch 2" in subject
    assert node_b.push()

    audit = fa.fence_audit(node_b, placement=wa.PlacementView("b", 2))
    assert audit.ok, audit.findings()
    assert fo.list_orphans(node_b) == []
    assert len(fo.list_orphans(node_b, include_merged=True)) == 1
    # idempotent: the orphan is already in main
    again = fo.merge_orphan(node_b, "frame/a/orphan-1-1", frame="b")
    assert again.already and again.commit is None


def test_a_second_divert_at_the_same_epoch_takes_the_next_n(rejoined):
    a, node_a, _, node_b, _ = rejoined
    assert fo.divert_if_superseded(node_a, frame="a").branch == "frame/a/orphan-1-1"
    # a frame restored from an old backup rejoins with more epoch-1 work
    a.ok("sql", "-q", "call dolt_reset('--hard', 'HEAD~1')")  # back below b's bump: a@1
    _stale_write(a, "restored-write")
    out = fo.divert_if_superseded(node_a, frame="a")
    assert out.superseded and out.branch == "frame/a/orphan-1-2"
    assert [o.branch for o in fo.list_orphans(node_b)] == [
        "frame/a/orphan-1-1",
        "frame/a/orphan-1-2",
    ]


def test_a_current_writer_does_not_divert(rejoined):
    _, _, b, node_b, _ = rejoined
    head = b.rows("select hashof('main') h")[0]["h"]
    out = fo.divert_if_superseded(node_b, frame="b")
    assert not out.superseded and not out.reset and out.branch is None
    assert b.rows("select hashof('main') h")[0]["h"] == head


def test_superseded_with_nothing_unpublished_only_resets(tmp_path):
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
    assert wa.run_step2(node_b, _Placement("b", 2), frame="b", epoch=2).landed
    out = fo.divert_if_superseded(node_a, frame="a")
    assert out.superseded and out.reset and out.branch is None
    assert fo.list_orphans(node_a) == []


def test_orphan_merge_refusals_write_nothing(rejoined):
    a, node_a, b, node_b, _ = rejoined
    fo.divert_if_superseded(node_a, frame="a")
    head = b.rows("select hashof('main') h")[0]["h"]
    with pytest.raises(fo.OrphanMergeRefused, match="not an orphan branch"):
        fo.merge_orphan(node_b, "main", frame="b")
    with pytest.raises(fo.OrphanMergeRefused, match="only the writer"):
        fo.merge_orphan(node_b, "frame/a/orphan-1-1", frame="a")
    with pytest.raises(fo.OrphanMergeRefused, match="no orphan"):
        fo.merge_orphan(node_b, "frame/a/orphan-1-9", frame="b")
    # a superseded frame (a, reset to epoch 2 but not the writer) cannot merge either
    with pytest.raises(fo.OrphanMergeRefused, match="only the writer"):
        fo.merge_orphan(node_a, "frame/a/orphan-1-1", frame="a")
    assert b.rows("select hashof('main') h")[0]["h"] == head


def test_orphan_merge_refuses_any_conflict_and_never_resolves(rejoined):
    a, node_a, b, node_b, _ = rejoined
    fo.divert_if_superseded(node_a, frame="a")
    # the writer edits the same row the orphan wrote: a data conflict on issues
    b.ok("fetch", "origin")
    b.ok(
        "sql",
        "-q",
        "insert into issues (id, title, description, design, acceptance_criteria, notes) "
        "values ('partitioned-1', 'writer-side', '', '', '', ''); "
        "call dolt_commit('-Am', 'writer edit')",
    )
    head = b.rows("select hashof('main') h")[0]["h"]
    with pytest.raises(fo.OrphanMergeRefused, match="conflict on issues.*never resolves"):
        fo.merge_orphan(node_b, "frame/a/orphan-1-1", frame="b")
    assert b.rows("select hashof('main') h")[0]["h"] == head
    assert b.rows("select is_merging from dolt_merge_status")[0]["is_merging"] in (0, False)


@pytest.mark.dolt_server
def test_server_mode_divert_and_one_session_orphan_merge_through_bd(tmp_path):
    """The server-mode engine: ``CALL DOLT_PUSH`` for the orphan, the forwarder quiesce in the
    reset, and the one-session merge as one ``bd sql`` batch."""
    from harness.writer_fencing import BD_SERVER, Cluster

    with Cluster(tmp_path / "cluster", [("s", BD_SERVER), ("t", BD_SERVER)]) as cluster:
        s, t = cluster["s"], cluster["t"]
        node_s = fd.FenceNode(fd.BdServerEngine(s.hive, env=s.env))
        node_t = fd.FenceNode(fd.BdServerEngine(t.hive, env=t.env))
        node_s.install("s", 1)
        node_s.provision_ident("s")
        assert node_s.push()
        node_t.sync_to_remote()
        node_t.provision_ident("t")
        assert wa.run_step2(node_t, _Placement("t", 2), frame="t", epoch=2).landed

        s.create_issue("partitioned-server-write").check()  # s still names itself: stamped
        out = fo.divert_if_superseded(node_s, frame="s")
        assert out.superseded and out.branch == "frame/s/orphan-1-1", out
        assert node_s.writer().frame == "t"

        merged = fo.merge_orphan(node_t, out.branch, frame="t")
        assert merged.restamped >= 1 and merged.epoch == 2
        assert "partitioned-server-write" in t.issue_titles()
        assert node_t.push()
        audit = fa.fence_audit(node_t, placement=wa.PlacementView("t", 2))
        assert audit.ok, audit.findings()


# ---- a DOLT_MERGE that fails mid-batch leaves local main exactly at its pre-merge head ---------

_GUARDS = (
    (re.compile(r" AND @bh_merging > 0"), ""),
    (re.compile(r" WHERE @bh_merging > 0"), ""),
    (re.compile(r"IF\(@bh_merging > 0, ('[^']*'), NULL\)"), r"\1"),
)


def _fail_the_merge(monkeypatch, engine, *, unguarded: bool, live: int) -> None:
    """Make the batch's DOLT_MERGE fail and keep running the later statements anyway (bd's
    batch does not stop at an error). ``unguarded`` also strips the in-SQL guards and dirties
    the working set first, so a NON-merge re-stamp commit really lands on main — the case the
    postflight must undo."""
    original = engine.execute_session

    def run(statements):
        out = []
        for st in statements:
            if "DOLT_MERGE" in st:
                st = re.sub(r"'origin/frame/[^']*'", "'origin/no-such-orphan'", st)
            if unguarded:
                for pattern, repl in _GUARDS:
                    st = pattern.sub(repl, st)
            out.append(st)
        if unguarded:
            engine.execute(
                [f"INSERT INTO bh_write_mark (id, epoch, tbl) VALUES ('dirty', {live}, 'issues')"]
            )
            for st in out[1:]:  # one statement at a time, every error swallowed
                try:
                    original([out[0], st])
                except fd.SqlFailed:
                    pass
            return
        original(out)

    monkeypatch.setattr(engine, "execute_session", run)


def _assert_restored(node, prev: str, live: int, err: str, *, discarded: bool) -> None:
    assert node.query("select hashof('main') h")[0]["h"] == prev
    assert node.query(f"select count(*) n from bh_write_mark where epoch <> {live}")[0]["n"] in (
        0,
        "0",
    )
    assert node.query("select count(*) n from bh_write_mark where id = 'dirty'")[0]["n"] in (0, "0")
    merging = node.query("select is_merging from dolt_merge_status")
    assert not merging or str(merging[0]["is_merging"]).lower() in ("0", "false")
    assert "restored" in err and "NOT RESTORED" not in err
    assert ("discarded" in err) is discarded


@pytest.mark.parametrize("unguarded", [False, True], ids=["guarded", "unguarded"])
def test_a_failed_merge_mid_batch_restores_main_to_prev(rejoined, monkeypatch, unguarded):
    _, node_a, b, node_b, _ = rejoined
    fo.divert_if_superseded(node_a, frame="a")
    b.ok("fetch", "origin")
    prev = b.rows("select hashof('main') h")[0]["h"]
    _fail_the_merge(monkeypatch, node_b.engine, unguarded=unguarded, live=2)
    with pytest.raises(fo.OrphanMergeFailed) as failed:
        fo.merge_orphan(node_b, "frame/a/orphan-1-1", frame="b")
    _assert_restored(node_b, prev, 2, str(failed.value), discarded=unguarded)
    assert not ({"partitioned-1", "partitioned-2"} & _titles(b))
    assert [o.branch for o in fo.list_orphans(node_b)] == ["frame/a/orphan-1-1"]


@pytest.mark.dolt_server
@pytest.mark.parametrize("unguarded", [False, True], ids=["guarded", "unguarded"])
def test_server_mode_failed_merge_mid_batch_restores_main_to_prev(tmp_path, monkeypatch, unguarded):
    from harness.writer_fencing import BD_SERVER, Cluster

    with Cluster(tmp_path / "cluster", [("s", BD_SERVER), ("t", BD_SERVER)]) as cluster:
        s, t = cluster["s"], cluster["t"]
        node_s = fd.FenceNode(fd.BdServerEngine(s.hive, env=s.env))
        node_t = fd.FenceNode(fd.BdServerEngine(t.hive, env=t.env))
        node_s.install("s", 1)
        node_s.provision_ident("s")
        assert node_s.push()
        node_t.sync_to_remote()
        node_t.provision_ident("t")
        assert wa.run_step2(node_t, _Placement("t", 2), frame="t", epoch=2).landed
        s.create_issue("partitioned-server-write").check()
        out = fo.divert_if_superseded(node_s, frame="s")
        assert out.branch == "frame/s/orphan-1-1"

        node_t.engine.commit("bh: settle before the test's prev")
        prev = node_t.query("select hashof('main') h")[0]["h"]
        _fail_the_merge(monkeypatch, node_t.engine, unguarded=unguarded, live=2)
        with pytest.raises(fo.OrphanMergeFailed) as failed:
            fo.merge_orphan(node_t, out.branch, frame="t")
        _assert_restored(node_t, prev, 2, str(failed.value), discarded=unguarded)
        assert "partitioned-server-write" not in t.issue_titles()
