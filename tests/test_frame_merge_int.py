"""Integration (bh-sfp9o.5): the writer merges ``open``-policy frame refs without ``--strategy``.

Real bd (embedded) and the real Dolt CLI on the bh-eybn7 fixture (:mod:`harness.writer_fencing`):
a writer ``a`` and three replicas, every ``HOME``, ``DOLT_ROOT_PATH`` and data dir under
``tmp_path``, one bare ``git+file://`` hive remote, no ``dolt sql-server``.

Each replica runs under the ``branch`` guard role (what the ``open`` policy provisions,
bh-sxhnq), writes with bd and publishes its ``main`` to ``refs/dolt/frame/<id>`` with the
product's publisher. Replicas ``b`` and ``c`` both claim bead ``X`` and both reprioritise bead
``Y``; ``d`` only labels ``Y``. The writer merges every frame ref it lists on the remote:

* ``b`` lands as a sanctioned ``bh: merge frame/b`` merge commit;
* ``c`` is held unmerged — its conflicting rows are recorded, and ``X`` is a double claim
  naming both holders — while ``d`` still lands after it;
* the operator's resolve refuses without a side, records one bead's side, and completes the
  merge once both beads have one: ``X`` takes the frame's claim, ``Y`` keeps main's priority;
* ``b`` and ``e`` each create a different bead under one explicit id: ``e``'s ref is held as an
  id collision naming both beads, and resolve refuses either side (``bh-xwy5x`` condition 3).

``main``'s clone-local ``metadata.clone_id`` survives every merge (condition 2).
"""

from __future__ import annotations

import json
import shutil

import pytest

from beadhive import fence_data, frame_merge
from beadhive import write_policy_publish as publish
from harness.writer_fencing import BD_EMBEDDED, Cluster

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("bd") is None or shutil.which("dolt") is None or shutil.which("git") is None,
        reason="bd/dolt/git not installed",
    ),
]


class Clock:
    def __init__(self, at: float = 1_000.0):
        self.at = at

    def __call__(self) -> float:
        self.at += 1.0
        return self.at


def _create(frame, title: str) -> str:
    out = frame.bd("create", "--title", title, "-t", "task", "-p", "2", "--json").check().stdout
    return json.loads(out[out.index("{") :])["id"]


def _node(frame) -> fence_data.FenceNode:
    return fence_data.FenceNode(fence_data.DoltCliEngine(frame.dolt_dir, env=frame.env))


def _replica(frame) -> fence_data.FenceNode:
    frame.pull().check()
    node = _node(frame)
    node.provision_ident(frame.name, "branch")
    return node


def _publish(frame, node) -> None:
    assert publish.publish(frame.hive, frame.name, node=node) == f"refs/dolt/frame/{frame.name}"


def _issue(node, bead: str) -> dict:
    rows = node.query(f"SELECT assignee, status, priority FROM issues WHERE id = '{bead}'")
    return rows[0]


def _clone_id(node) -> str:
    return node.query("SELECT value FROM metadata WHERE `key` = 'clone_id'")[0]["value"]


def test_frame_refs_merge_without_strategy_and_conflicts_wait_for_the_operator(tmp_path):
    names = ("a", "b", "c", "d", "e")
    with Cluster(tmp_path / "cluster", [(n, BD_EMBEDDED) for n in names]) as cluster:
        a, b, c, d, e = (cluster[n] for n in names)
        dup = f"{cluster.prefix}-dup"
        x, y = _create(a, "claimed twice"), _create(a, "reprioritised twice")
        epoch = cluster.hq.place("a").epoch
        writer = _node(a)
        writer.install("a", epoch)
        writer.provision_ident("a")
        a.push().check()
        clone_id = _clone_id(writer)

        for frame, priority in ((b, "1"), (c, "3")):
            node = _replica(frame)
            frame.bd("update", x, "--claim").check()
            frame.bd("update", y, "--priority", priority).check()
            frame.bd("comments", "add", x, f"note from {frame.name}").check()
            if frame is b:
                b.bd("create", "--id", dup, "--title", "from b", "-t", "task").check()
            _publish(frame, node)
        node_d = _replica(d)
        d.bd("label", "add", y, "from-d").check()
        _publish(d, node_d)
        node_e = _replica(e)
        e.bd("create", "--id", dup, "--title", "from e", "-t", "task").check()
        _publish(e, node_e)

        state = tmp_path / "state" / "frame-merge.json"
        ctx = frame_merge.WriterContext(cluster.prefix, writer, "a")
        clock = Clock()
        assert frame_merge.list_frames(writer) == ["b", "c", "d", "e"]
        outcomes = {o.frame: o for o in frame_merge.merge_hive(ctx, now=clock, path=state)}

        # b lands, sanctioned for the fence audit; c is held; d still lands after it.
        assert outcomes["b"].status == frame_merge.Status.MERGED, outcomes["b"].error
        assert outcomes["d"].status == frame_merge.Status.MERGED, outcomes["d"].error
        held = outcomes["c"]
        assert held.status == frame_merge.Status.CONFLICT
        assert set(held.subjects) == {x, y}
        rows = {(row.table, row.bead): row for row in held.conflicts}
        assert rows[("issues", x)].main["assignee"] == "b"
        assert rows[("issues", x)].frame["assignee"] == "c"
        assert (rows[("issues", y)].main["priority"], rows[("issues", y)].frame["priority"]) == (
            1,
            3,
        )
        [claim] = held.double_claims
        assert (claim.bead, claim.holders, claim.frame) == (x, ("b", "c"), "c")
        log = writer.query("SELECT message FROM dolt_log LIMIT 2")
        assert {row["message"].split(" from ")[0] for row in log} == {
            "bh: merge frame/b",
            "bh: merge frame/d",
        }
        assert _issue(writer, x)["assignee"] == "b"
        labels = writer.query(f"SELECT label FROM labels WHERE issue_id = '{y}'")
        assert {r["label"] for r in labels} == {"from-d"}
        assert _clone_id(writer) == clone_id
        assert not writer.query("SELECT * FROM dolt_status")

        # An id collision is held and never resolved by taking a side.
        collided = outcomes["e"]
        assert collided.status == frame_merge.Status.CONFLICT
        [row] = collided.collisions
        assert (row.bead, row.main["title"], row.frame["title"]) == (dup, "from b", "from e")
        assert not collided.double_claims
        for take in ("main", "frame"):
            with pytest.raises(frame_merge.FrameMergeRefused, match="id collision on .*-dup"):
                frame_merge.resolve(ctx, "e", dup, take, now=clock, path=state)
        titles = writer.query(f"SELECT title FROM issues WHERE id = '{dup}'")
        assert [t["title"] for t in titles] == ["from b"]

        # Reported per hive and frame for bh doctor and bh hq why.
        doctor = frame_merge.doctor_data(path=state)
        assert doctor["level"] == "warn" and doctor["held"] == 2
        text, detail = frame_merge.why_summary(cluster.prefix, path=state)
        assert "refs/dolt/frame/c" in text and x in text and "claimed twice" in text
        assert [item["frame"] for item in detail] == ["c", "e"]

        # The operator resolves per bead; never without a side.
        head = frame_merge._hashof(writer, "main")
        with pytest.raises(frame_merge.FrameMergeRefused, match="explicit side"):
            frame_merge.resolve(ctx, "c", x, None, now=clock, path=state)
        pending = frame_merge.resolve(ctx, "c", x, "frame", now=clock, path=state)
        assert pending.status == frame_merge.Status.CONFLICT and pending.unresolved == (y,)
        assert frame_merge._hashof(writer, "main") == head  # nothing merged yet
        done = frame_merge.resolve(ctx, "c", y, "main", now=clock, path=state)
        assert done.status == frame_merge.Status.MERGED, done.error
        assert done.resolutions == {x: "frame", y: "main"}
        assert _issue(writer, x)["assignee"] == "c"
        assert _issue(writer, y)["priority"] == 1
        notes = {r["text"] for r in writer.query(f"SELECT text FROM comments WHERE issue_id='{x}'")}
        assert notes == {"note from b", "note from c"}
        assert _clone_id(writer) == clone_id
        assert frame_merge.doctor_data(path=state)["held"] == 1  # only e's collision is left
        assert [o["frame"] for o in frame_merge.why_summary(cluster.prefix, path=state)[1]] == ["e"]

        # Merged refs are up to date on the next pass; the collision stays held.
        again = {o.frame: o.status for o in frame_merge.merge_hive(ctx, now=clock, path=state)}
        assert again == {
            "b": frame_merge.Status.UP_TO_DATE,
            "c": frame_merge.Status.UP_TO_DATE,
            "d": frame_merge.Status.UP_TO_DATE,
            "e": frame_merge.Status.CONFLICT,
        }
