"""Integration (bh-sxhnq): the ``open`` write policy on a PRIVATE scratch cut-over hive.

Real bd (embedded) and the real Dolt CLI on the bh-eybn7 fixture (:mod:`harness.writer_fencing`):
two frames and one bare ``git+file://`` hive remote, every ``HOME``, ``DOLT_ROOT_PATH`` and data
dir under ``tmp_path``. No ``dolt sql-server`` is started (both frames are embedded), and the
cluster's frame agents are stopped when the ``with`` block exits.

Frame ``a`` founds the hive, installs the product fence as its writer and publishes. Frame ``b``
pulls it as a replica. Under ``open`` (the policy is stubbed; the off-primary decision, the guard
role, bd's write and the publish are the product's), ``b``'s allowed write lands on its own
data, and ``Engine.push_state`` publishes it to ``refs/dolt/frame/b`` while the remote's ``main``
(``refs/dolt/data``) does not move. A frame ref that is no longer a fast-forward of ``b``'s
``main`` is reported, never forced.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from beadhive import engine, fence_data, host, registry, write_policy
from beadhive.writer_adopt import WriterRow
from harness.writer_fencing import BD_EMBEDDED, Cluster

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("bd") is None or shutil.which("dolt") is None or shutil.which("git") is None,
        reason="bd/dolt/git not installed",
    ),
]


def _git_ref(cluster: Cluster, ref: str) -> str:
    res = subprocess.run(
        ["git", "--git-dir", str(cluster.remote.path), "rev-parse", "-q", "--verify", ref],
        capture_output=True,
        text=True,
    )
    return res.stdout.strip()


def _frame_ref_comments(cluster: Cluster, root: Path, frame: str, env: dict) -> list[str]:
    """The comment texts on the frame ref, read by a fresh observer clone of the remote."""
    observer = root / f"frame-ref-observer-{frame}"
    run = lambda *a, cwd=None: subprocess.run(  # noqa: E731
        ["dolt", *a], cwd=cwd, env=env, capture_output=True, text=True, timeout=180, check=True
    )
    run("clone", cluster.remote.url, str(observer))
    run(
        "remote",
        "add",
        "--ref",
        f"refs/dolt/frame/{frame}",
        "fr",
        f"{cluster.remote.url}/",
        cwd=observer,
    )
    run("fetch", "fr", cwd=observer)
    out = run(
        "sql", "-r", "csv", "-q", "select text from comments as of 'fr/main'", cwd=observer
    ).stdout
    return out.splitlines()[1:]


@pytest.fixture
def open_replica(tmp_path, monkeypatch):
    """``a`` writes the cut-over hive; ``b`` is a pulled replica running under ``open``."""
    with Cluster(tmp_path / "cluster", [("a", BD_EMBEDDED), ("b", BD_EMBEDDED)]) as cluster:
        a, b = cluster["a"], cluster["b"]
        created = a.bd("create", "--title", "seed", "-t", "task", "-p", "2", "--json").check()
        bead_id = json.loads(created.stdout[created.stdout.index("{") :])["id"]
        epoch = cluster.hq.place("a").epoch
        node_a = fence_data.FenceNode(fence_data.DoltCliEngine(a.dolt_dir, env=a.env))
        node_a.install("a", epoch)
        node_a.provision_ident("a")
        a.push().check()
        b.pull().check()

        # This process acts as frame b: its environment, its frame id, its registered hive.
        for key, value in b.env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(host, "host_id", lambda: "b")
        entry = {"prefix": cluster.prefix}
        monkeypatch.setattr(registry, "entry_for_dir", lambda _cfg, _d: entry)
        monkeypatch.setattr(
            write_policy,
            "for_entry",
            lambda *_a, **_k: write_policy.EffectivePolicy(
                "open", "open", "open", hive=cluster.prefix
            ),
        )
        fence_data.reset_probe_cache()
        write_policy.reset_warned()
        yield cluster, a, b, bead_id, epoch
        fence_data.reset_probe_cache()
        write_policy.reset_warned()


def test_an_open_replica_write_is_published_only_to_its_frame_ref(open_replica, tmp_path):
    cluster, a, b, bead_id, epoch = open_replica
    main_before = cluster.remote.head()
    where = write_policy.off_primary(cfg={}, hive_dir=b.hive)
    assert where is not None and (where.writer, where.this_frame) == ("a", "b")

    eng = engine.BdEngine()
    wrote = eng.invoke(["comments", "add", bead_id, "from-the-replica"], cwd=b.hive, capture=True)
    assert wrote.returncode == 0, wrote.stderr
    node_b = fence_data.FenceNode(fence_data.DoltCliEngine(b.dolt_dir, env=b.env))
    assert node_b.ident() == ("b", "branch")  # the guard accepted b's write to its own main
    assert node_b.writer() == WriterRow("a", epoch, node_b.writer().revision)

    refused = eng.invoke(["create", "--title", "x", "--parent", bead_id], cwd=b.hive, capture=True)
    assert refused.returncode == 1 and "#4796" in refused.stderr

    published = eng.push_state(b.hive, message="bh: open replica sync")
    assert published.returncode == 0, published.stderr
    assert "refs/dolt/frame/b" in published.stdout

    assert cluster.remote.head() == main_before, "the hive's main must not move"
    assert _git_ref(cluster, "refs/dolt/frame/b")
    assert "from-the-replica" in _frame_ref_comments(cluster, tmp_path, "b", b.env)
    a.pull().check()  # the writer's view of main does not have it until the merge slice
    assert "from-the-replica" not in {row["text"] for row in a.query("select text from comments")}


def test_a_non_fast_forward_frame_ref_is_reported_not_forced(open_replica):
    cluster, _a, b, bead_id, _epoch = open_replica
    eng = engine.BdEngine()
    assert eng.invoke(["comments", "add", bead_id, "first"], cwd=b.hive).returncode == 0
    assert eng.push_state(b.hive, message="bh: first").returncode == 0
    published = _git_ref(cluster, "refs/dolt/frame/b")
    main_before = cluster.remote.head()

    # b's main leaves the published history: back to the hive's main, then a different write.
    b.dolt("reset", "--hard", "origin/main").check()
    assert eng.invoke(["comments", "add", bead_id, "second"], cwd=b.hive).returncode == 0
    diverged = eng.push_state(b.hive, message="bh: second")
    assert diverged.returncode == 1
    assert "not a fast-forward" in diverged.stderr and "nothing was overwritten" in diverged.stderr
    assert _git_ref(cluster, "refs/dolt/frame/b") == published
    assert cluster.remote.head() == main_before
