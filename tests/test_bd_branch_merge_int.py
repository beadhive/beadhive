"""bd upstream-behaviour characterization for branch-role frames (bh-sieai T6; bh-rdcjw).

Not a bh product test: it pins what bd 1.3 / Dolt 2.3.5 do when several branch-role frames each
write bd's local ``main``, publish it as ``frame/<id>`` and the primary merges them. If a bd or
Dolt bump changes any of it, the branch-path design in
``docs/spikes/bh-sieai-write-guard-and-nonprimary-writes.md`` (Evidence 9) must be re-read.

* Frame-scoped child IDs collide (bd #4796): both branches mint ``<epic>.1``.
* Both branches' ``--claim`` of one bead "win" locally.
* The second ``bd vc merge`` conflicts and rolls back; ``--strategy ours`` silently drops the
  second branch's side (child row and claim).
* Disjoint label rows union cleanly.
* A branch-role frame's raw ``bd dolt push`` publishes straight to remote ``main``.

The product-guard half (branch role never moves ``main``; merged only by the primary) is
``tests/test_fence_composed_int.py::test_branch_role_frame_edits_reach_main_only_through_the_primarys_merge``;
manifest-CAS contention timings from the spike are intentionally not carried (timing dependent).
"""

from __future__ import annotations

import json
import shutil

import pytest

from harness import write_guard as wg
from harness.writer_fencing import BD_EMBEDDED, Cluster, Frame

pytestmark = [
    pytest.mark.integration,
    pytest.mark.dolt_server,
    pytest.mark.skipif(
        shutil.which("bd") is None or shutil.which("dolt") is None, reason="bd/dolt not installed"
    ),
]


def _create(frame: Frame, title: str) -> str:
    return json.loads(frame.bd("create", "--title", title, "-t", "task", "--json").check().stdout)[
        "id"
    ]


def test_branch_frames_collide_on_child_ids_and_strategy_ours_silently_drops_a_side(tmp_path):
    frames = [("p", BD_EMBEDDED), ("b1", BD_EMBEDDED), ("b2", BD_EMBEDDED)]
    with Cluster(tmp_path / "cl", frames) as cluster:
        p, b1, b2 = cluster["p"], cluster["b1"], cluster["b2"]
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
            b.spawn(
                "dolt", "push", "origin", f"main:frame/{b.name}", cwd=b.dolt_dir
            ).result().check()
        remote = cluster.remote.view(("issues",))
        assert f"{epic}.1" not in {r["id"] for r in remote["tables"]["issues"]}  # main untouched

        p.dolt("fetch", "origin").check()
        p.bd("vc", "merge", "origin/frame/b1").check()
        conflicted = p.bd("vc", "merge", "origin/frame/b2")
        assert not conflicted.ok and "Merge conflict detected" in conflicted.output
        assert p.query("select * from dolt_status") == []  # rolled back, nothing half-merged
        forced = p.bd("vc", "merge", "origin/frame/b2", "--strategy", "ours")
        assert forced.ok, forced.output
        rows = {r["id"]: r for r in p.query("select id, title, assignee, status from issues")}
        labels = {
            r["label"] for r in p.query(f"select label from labels where issue_id = '{labelled}'")
        }
        assert rows[f"{epic}.1"]["title"] == "child-b1"
        assert not any(r["title"] == "child-b2" for r in rows.values())  # silently dropped
        assert rows[contested]["assignee"] == "b1"  # b2 still believes it holds the claim
        assert labels == {"lab-b1", "lab-b2"}  # disjoint label rows union cleanly
        p.push().check()

        # Hazard: a branch-role frame running raw `bd dolt pull` + `bd dolt push` publishes to main.
        b1.bd("dolt", "pull").check()
        stray = _create(b1, "stray-from-branch-frame")
        assert b1.bd("dolt", "push").ok
        assert stray in {r["id"] for r in cluster.remote.view(("issues",))["tables"]["issues"]}
