"""Decision probe (bh-xwy5x): top-level create on two ``open`` replicas, merged by the writer.

Trust-profiles ADR open question 1. Real bd (embedded) and the real Dolt CLI on the bh-eybn7
fixture (:mod:`harness.writer_fencing`): three frames and one bare ``git+file://`` hive remote,
every ``HOME``, ``DOLT_ROOT_PATH`` and data dir under ``tmp_path``, no ``dolt sql-server``.

Frame ``a`` founds the cut-over hive and is its writer; ``b`` and ``c`` pull it as replicas and
run under ``open``. Each replica creates top-level beads through the product path
(``BdEngine.invoke``: the off-primary decision, the ``branch`` guard role, bd's own id
allocation) and publishes through ``BdEngine.push_state`` to ``refs/dolt/frame/<id>``. The
writer then fetches both frame refs and merges them into ``main`` **without** ``--strategy``
(ADR section 4). The only product behaviour stubbed is the policy itself and the top-level
create refusal this decision is about; child create stays refused.

What each test records (decision record:
``docs/design/bh-xwy5x-open-top-level-create-decision.md``):

* **any two replicas** (comments only, no create): the second frame ref's merge stops on bd's
  per-clone ``metadata.clone_id`` row and nothing else, so the probe treats that one row as
  frame-local (the writer keeps its own) — a finding for the merge slice, not for create;
* **hash ids** (bd's default ``issue_id_mode``): ids, ``issue_counter`` and every other table
  merge cleanly apart from ``clone_id``, and both replicas' beads are on ``main``. Six fresh
  3-character ids collide with probability about 9/36**3 (0.02 %); that failure would be the
  real cross-replica collision, reported as such;
* **a colliding top-level id** (the cross-replica hash collision bd cannot see, forced with
  ``--id``): the second merge stops with a conflict on ``issues`` and drops nothing;
* **counter ids** (``issue_id_mode=counter``): both replicas allocate the same sequential id
  from their own copy of ``issue_counter``, so the second merge stops with a conflict.
"""

from __future__ import annotations

import json
import shutil
from contextlib import contextmanager

import pytest

from beadhive import engine, fence_data, host, registry, write_policy
from harness.writer_fencing import BD_EMBEDDED, Cluster, Frame

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("bd") is None or shutil.which("dolt") is None or shutil.which("git") is None,
        reason="bd/dolt/git not installed",
    ),
]

_ID_MODE = "issue_id_mode"


@pytest.fixture
def hive(tmp_path, monkeypatch):
    """``a`` writes the cut-over hive; ``b`` and ``c`` are pulled replicas under ``open``.

    Yields ``(cluster, act_as)``: ``act_as(frame)`` makes this process that frame (its
    environment and frame id) for the product's engine calls."""

    def found(cluster: Cluster, mode: str = "") -> None:
        a = cluster["a"]
        a.bd("create", "--title", "seed", "-t", "task", "-p", "2", "--json").check()
        if mode:
            a.bd("config", "set", _ID_MODE, mode).check()
        node_a = fence_data.FenceNode(fence_data.DoltCliEngine(a.dolt_dir, env=a.env))
        node_a.install("a", cluster.hq.place("a").epoch)
        node_a.provision_ident("a")
        a.push().check()
        for name in ("b", "c"):
            cluster[name].pull().check()

    @contextmanager
    def start(mode: str = ""):
        frames = [(name, BD_EMBEDDED) for name in ("a", "b", "c")]
        with Cluster(tmp_path / "cluster", frames) as cluster:
            found(cluster, mode)
            entry = {"prefix": cluster.prefix}
            monkeypatch.setattr(registry, "entry_for_dir", lambda _cfg, _d: entry)
            monkeypatch.setattr(
                write_policy,
                "for_entry",
                lambda *_a, **_k: write_policy.EffectivePolicy(
                    "open", "open", "open", hive=cluster.prefix
                ),
            )
            # The decision under test: what a lifted top-level create refusal would do. Child
            # create keeps the product's refusal (gastownhall/beads#4796).
            refuse = write_policy.open_refusal
            monkeypatch.setattr(
                write_policy,
                "open_refusal",
                lambda args, where: (
                    "" if write_policy.classify(args).kind == "create" else refuse(args, where)
                ),
            )

            def act_as(frame: Frame) -> None:
                for key, value in frame.env.items():
                    monkeypatch.setenv(key, value)
                monkeypatch.setattr(host, "host_id", lambda: frame.name)
                fence_data.reset_probe_cache()
                write_policy.reset_warned()

            try:
                yield cluster, act_as
            finally:
                fence_data.reset_probe_cache()
                write_policy.reset_warned()

    return start


def _create(frame: Frame, title: str, *extra: str) -> str:
    """A top-level create on `frame` through the product engine; returns the new id."""
    eng = engine.BdEngine()
    args = ["create", "--title", title, "-t", "task", "-p", "2", "--json", *extra]
    res = eng.invoke(args, cwd=frame.hive, capture=True)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout[res.stdout.index("{") :])["id"]


def _publish(frame: Frame) -> None:
    published = engine.BdEngine().push_state(frame.hive, message=f"bh: {frame.name} sync")
    assert published.returncode == 0, published.stderr
    assert f"refs/dolt/frame/{frame.name}" in published.stdout


#: bd's per-clone row in the versioned ``metadata`` table: every clone rewrites it, so two
#: replicas' frame refs always disagree on it, whatever they wrote (see the comment-only test).
CLONE_ID = ("metadata", "clone_id")


def _conflict_keys(writer: Frame) -> set[tuple[str, str]]:
    """``(table, primary key)`` of every row the in-progress merge left in conflict."""
    keys = set()
    for row in writer.query("select `table` from dolt_conflicts"):
        table = row["table"]
        column = "key" if table == "metadata" else "id"
        for conflict in writer.query(f"select * from `dolt_conflicts_{table}`"):
            ident = next(
                conflict[f"{side}_{column}"]
                for side in ("our", "their", "base")
                if conflict.get(f"{side}_{column}")
            )
            keys.add((table, ident))
    return keys


def _merge_frame_ref(cluster: Cluster, writer: Frame, frame: str, *, frame_local=()) -> dict:
    """The writer fetches ``refs/dolt/frame/<frame>`` and merges it without ``--strategy``.

    Returns ``{"ok", "conflicts"}`` where ``conflicts`` is the set of ``(table, key)`` rows the
    merge stopped on. When every conflict is in `frame_local` (rows that describe one clone,
    not the tracker), the writer keeps its own value for exactly those rows and completes the
    merge; any other conflict aborts it, so ``main`` stays where it was."""
    remote = f"frame-{frame}"
    writer.dolt(
        "remote", "add", "--ref", f"refs/dolt/frame/{frame}", remote, f"{cluster.remote.url}/"
    ).check()
    writer.dolt("fetch", remote).check()
    merged = writer.dolt("merge", "-m", f"merge {remote}", f"{remote}/main")
    conflicts = _conflict_keys(writer)
    if not conflicts:
        assert merged.ok, merged.output
        return {"ok": True, "conflicts": conflicts}
    if conflicts <= set(frame_local):
        for table in sorted({table for table, _key in conflicts}):
            writer.dolt("conflicts", "resolve", "--ours", table).check()
        writer.dolt("add", "-A").check()
        writer.dolt("commit", "-m", f"merge {remote} (frame-local rows kept)").check()
        return {"ok": True, "conflicts": conflicts}
    writer.dolt("merge", "--abort").check()
    return {"ok": False, "conflicts": conflicts}


def _ids(frame: Frame) -> set[str]:
    return {row["id"] for row in frame.query("select id from issues")}


def _counter(frame: Frame) -> list[dict]:
    return frame.query("select prefix, last_id from issue_counter order by prefix")


def test_any_two_open_replicas_conflict_on_bd_clone_id(hive):
    """Not about create: two replicas that only comment still stop the second merge, on bd's
    per-clone ``metadata.clone_id`` row and nothing else (an input for bh-sfp9o.5)."""
    with hive() as (cluster, act_as):
        a, b, c = cluster["a"], cluster["b"], cluster["c"]
        seed = next(iter(_ids(a)))
        eng = engine.BdEngine()
        for frame in (b, c):
            act_as(frame)
            wrote = eng.invoke(["comments", "add", seed, f"from-{frame.name}"], cwd=frame.hive)
            assert wrote.returncode == 0
            _publish(frame)

        assert _merge_frame_ref(cluster, a, "b") == {"ok": True, "conflicts": set()}
        assert _merge_frame_ref(cluster, a, "c") == {"ok": False, "conflicts": {CLONE_ID}}


def test_hash_ids_from_two_open_replicas_merge_cleanly(hive):
    with hive() as (cluster, act_as):
        a, b, c = cluster["a"], cluster["b"], cluster["c"]
        main_ids = _ids(a)
        counter_before = _counter(a)

        act_as(b)
        from_b = {_create(b, f"b-{n}") for n in range(3)}
        _publish(b)
        act_as(c)
        from_c = {_create(c, f"c-{n}") for n in range(3)}
        _publish(c)

        assert not from_b & from_c, "hash ids collided across replicas"
        assert all("." not in ident for ident in from_b | from_c)  # top-level, not <epic>.N

        assert _merge_frame_ref(cluster, a, "b") == {"ok": True, "conflicts": set()}
        # The only conflict is bd's clone_id row; no issue, counter or event row conflicts.
        second = _merge_frame_ref(cluster, a, "c", frame_local=[CLONE_ID])
        assert second == {"ok": True, "conflicts": {CLONE_ID}}

        assert _ids(a) == main_ids | from_b | from_c, "the merge dropped or invented a bead"
        titles = {row["id"]: row["title"] for row in a.query("select id, title from issues")}
        assert {titles[i] for i in from_b | from_c} == {f"{f}-{n}" for f in "bc" for n in range(3)}
        assert _counter(a) == counter_before, "hash mode moved issue_counter"


def test_a_colliding_top_level_id_stops_the_merge_and_drops_nothing(hive):
    with hive() as (cluster, act_as):
        a, b, c = cluster["a"], cluster["b"], cluster["c"]
        same = f"{cluster.prefix}-zzz"  # the cross-replica hash collision bd cannot see

        act_as(b)
        assert _create(b, "from-b", "--id", same) == same
        _publish(b)
        act_as(c)
        assert _create(c, "from-c", "--id", same) == same
        _publish(c)

        assert _merge_frame_ref(cluster, a, "b")["ok"]
        second = _merge_frame_ref(cluster, a, "c", frame_local=[CLONE_ID])
        assert second == {"ok": False, "conflicts": {CLONE_ID, ("issues", same)}}
        rows = a.query(f"select title from issues where id = '{same}'")
        assert [row["title"] for row in rows] == ["from-b"]  # main kept b's bead, c unmerged
        assert a.query("select count(*) n from dolt_conflicts")[0]["n"] == 0  # aborted cleanly


def test_counter_ids_from_two_open_replicas_collide(hive):
    with hive(mode="counter") as (cluster, act_as):
        a, b, c = cluster["a"], cluster["b"], cluster["c"]

        act_as(b)
        from_b = _create(b, "from-b")
        _publish(b)
        act_as(c)
        from_c = _create(c, "from-c")
        _publish(c)

        assert from_b == from_c, "counter mode allocated distinct ids on two replicas"
        # Both replicas advanced their own issue_counter copy to the same value, so the counter
        # merges "cleanly" and the collision lands on the issues row instead.
        assert _counter(b) == _counter(c)
        assert _merge_frame_ref(cluster, a, "b")["ok"]
        second = _merge_frame_ref(cluster, a, "c", frame_local=[CLONE_ID])
        assert second == {"ok": False, "conflicts": {CLONE_ID, ("issues", from_b)}}
        rows = a.query(f"select title from issues where id = '{from_b}'")
        assert [row["title"] for row in rows] == ["from-b"]
