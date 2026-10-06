"""Integration smoke tests for the multi-frame writer-fencing fixture (spike bh-eybn7).

One test per fixture capability — cluster start-up, partition, kill, barrier ordering — each
against REAL drivers: Dolt CLI frames, bd 1.3 embedded frames (bd's linked Dolt) and bd 1.3
shared-server frames (a frame-private ``dolt sql-server``), all sharing one ``git+file://``
remote. Evidence and timings: ``docs/spikes/bh-eybn7-writer-fencing-fixture.md``.

Marked ``integration`` (real bd/dolt) + ``dolt_server`` (server frames start real sql-servers,
so each test holds one of the run-wide slots ``_bound_concurrent_dolt_servers`` hands out).
"""

from __future__ import annotations

import shutil

import pytest

from harness.writer_fencing import (
    BD_EMBEDDED,
    BD_SERVER,
    CAS,
    DOLT_CLI,
    Cluster,
    HQUnreachable,
    Recorder,
    pid_alive,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.dolt_server,
    pytest.mark.skipif(
        shutil.which("bd") is None or shutil.which("dolt") is None, reason="bd/dolt not installed"
    ),
]

PARTITION = "writer-fencing fixture partition"
WRITER_TABLE = "create table bh_writer (id int primary key, frame varchar(32), epoch int)"


def _remote_titles(cluster: Cluster) -> set[str]:
    rows = cluster.remote.view(("issues",))["tables"]["issues"] or []
    return {row["title"] for row in rows}


def _install_writer(cluster: Cluster, frame: str) -> None:
    """Seed ``bh_writer`` from ``frame`` with HQ's current placement and publish it."""
    placement = cluster.hq.place(frame)
    f = cluster[frame]
    f.sql(WRITER_TABLE).check()
    f.sql(f"insert into bh_writer values (1, '{frame}', {placement.epoch})").check()
    f.commit("bh: install writer").check()
    f.push().check()


def test_cluster_starts_hermetic_frames_on_one_remote_with_hq_and_recorder(tmp_path):
    frames = [("a", BD_EMBEDDED), ("b", BD_SERVER), ("c", DOLT_CLI)]
    with Cluster(tmp_path / "cl", frames) as cluster:
        a, b, c = cluster["a"], cluster["b"], cluster["c"]
        # Hermetic: every frame lives under the cluster root with its own HOME; only the server
        # frame is in shared-server mode, on its own dir and an ephemeral (non-3308) port.
        for frame in cluster.frames.values():
            assert frame.env["HOME"].startswith(str(cluster.root))
            assert frame.alive
        assert b.env["BEADS_SHARED_SERVER_DIR"] == str(b.server_dir)
        assert b.port != 3308 and pid_alive(b.server_pid())
        assert "BEADS_DOLT_SHARED_SERVER" not in a.env | c.env
        assert cluster.remote.url.startswith("git+file://")

        # HQ stand-in: CAS-bumped placement, per-frame reachability.
        _install_writer(cluster, "a")
        assert cluster.hq.placement() == cluster.hq.placement(asker="b")
        with pytest.raises(ValueError, match="stale placement"):
            cluster.hq.place("b", expected_epoch=0)
        cluster.hq.partition("c")
        with pytest.raises(HQUnreachable):
            cluster.hq.placement(asker="c")
        cluster.hq.heal("c")

        # Writes through all three drivers, each published and recorded.
        recorder = Recorder(cluster, remote_tables=("bh_writer",), guard_tables=("bh_writer",))
        for frame in (a, b, c):
            with recorder.step("write+push", frame.name) as step:
                frame.pull().check()
                frame.create_issue(f"from-{frame.name}").check()
                frame.push().check()
            before, after = step["before"]["remote"], step["after"]["remote"]
            assert after["head"] != before["head"] and after["main"] != before["main"]

        # An adopt written through bd's server-mode SQL is what the recorder sees on the remote.
        epoch = cluster.hq.place("b", expected_epoch=1).epoch
        with recorder.step("adopt", "b") as step:
            b.pull().check()
            b.sql(f"update bh_writer set frame = 'b', epoch = {epoch} where id = 1").check()
            b.commit("bh: adopt").check()
            b.push().check()
        assert step["before"]["remote"]["tables"]["bh_writer"] == [
            {"id": 1, "frame": "a", "epoch": 1}
        ]
        assert step["after"]["remote"]["tables"]["bh_writer"] == [
            {"id": 1, "frame": "b", "epoch": 2}
        ]
        assert step["after"]["hq"] == {"writer": "b", "epoch": 2}
        assert step["after"]["frames"]["b"]["tables"]["bh_writer"][0]["frame"] == "b"
        assert recorder.trace.read_text().count("\n") == len(recorder.entries) == 8

        cluster.converge()
        titles = {"from-a", "from-b", "from-c"}
        for frame in (a, b, c):
            assert titles <= frame.issue_titles()
        assert c.query("select frame, epoch from bh_writer") == [{"frame": "b", "epoch": 2}]
        assert all(not e.get("partitioned") for e in b.gate_log())
        assert any(e["point"] == CAS for e in b.gate_log()), "server-side push bypassed the gate"
        agents = [f._proc.pid for f in cluster.frames.values()]
        server = b.server_pid()
    assert not any(pid_alive(pid) for pid in [*agents, server])


def test_partition_cuts_exactly_one_frame_from_the_remote(tmp_path):
    frames = [("a", BD_EMBEDDED), ("b", BD_SERVER), ("c", DOLT_CLI)]
    with Cluster(tmp_path / "cl", frames) as cluster:
        names = list(cluster.frames)
        for i, name in enumerate(names):
            cut, peer = cluster[name], cluster[names[(i + 1) % len(names)]]
            cluster.converge()
            cut.partition()
            cut.create_issue(f"cut-{name}").check()  # local writes still work
            head = cluster.remote.head()
            for attempt in (cut.push(), cut.pull()):
                assert not attempt.ok and PARTITION in attempt.output, attempt.output
            assert cluster.remote.head() == head
            # The peer still reaches the same remote while ``cut`` cannot.
            peer.pull().check()
            peer.create_issue(f"peer-of-{name}").check()
            peer.push().check()
            assert cluster.remote.head() != head
            assert f"cut-{name}" not in _remote_titles(cluster)

            cut.heal()
            cut.pull().check()
            cut.push().check()
            assert {f"cut-{name}", f"peer-of-{name}"} <= _remote_titles(cluster)
            assert any(e.get("partitioned") for e in cut.gate_log())


def test_kill_mid_push_leaves_the_remote_untouched_and_the_frame_recovers(tmp_path):
    with Cluster(tmp_path / "cl", [("a", BD_EMBEDDED), ("b", BD_SERVER)]) as cluster:
        for frame in cluster.frames.values():
            frame.pull().check()
            frame.create_issue(f"doomed-{frame.name}").check()
            view = cluster.remote.view(("issues",))
            hold = frame.hold(CAS)
            push = frame.push_async()
            gate = hold.wait()  # parked after reading the remote, before the manifest CAS

            victims = frame.kill()
            assert gate in victims and not any(pid_alive(pid) for pid in victims)
            assert push.result().killed and not frame.alive
            if frame.kind == BD_SERVER:
                assert not pid_alive(frame.server_pid())
            assert cluster.remote.view(("issues",)) == view  # head and main untouched

            frame.restart()
            assert f"doomed-{frame.name}" in frame.issue_titles()  # the local commit survived
            frame.push().check()
            assert f"doomed-{frame.name}" in _remote_titles(cluster)


def _race(cluster: Cluster, recorder: Recorder, first: str) -> dict:
    """Adopt (b) and stale write (a) both park before their CAS; release ``first`` first."""
    a, b = cluster["a"], cluster["b"]
    epoch = cluster.hq.place("b", expected_epoch=1).epoch
    with recorder.step(f"{first}:adopt-commit", "b"):
        b.sql(f"update bh_writer set frame = 'b', epoch = {epoch} where id = 1").check()
        b.commit("bh: adopt").check()
    with recorder.step(f"{first}:stale-commit", "a"):
        a.create_issue(f"stale-write-{first}").check()

    holds = {"adopt": b.hold(CAS), "stale": a.hold(CAS)}
    pushes = {"adopt": b.push_async(), "stale": a.push_async()}
    for hold in holds.values():
        hold.wait()
    parked = recorder.snapshot(f"{first}:both-parked")
    assert parked["frames"]["a"]["held"] == parked["frames"]["b"]["held"] == [CAS]

    second = "stale" if first == "adopt" else "adopt"
    with recorder.step(f"{first}:release-{first}") as won:
        holds[first].release()
        assert pushes[first].result().ok
    with recorder.step(f"{first}:release-{second}") as lost:
        holds[second].release()
        refused = pushes[second].result()
    assert not refused.ok and "non-fast-forward" in refused.output, refused.output
    # The loser's refused push still moved refs/dolt/data (Dolt CAS-uploads its chunks before
    # the branch update is refused), so remote ``main`` -- not ``head`` -- is the verdict.
    assert lost["after"]["remote"]["main"] == won["after"]["remote"]["main"]
    assert lost["after"]["remote"]["head"] != won["after"]["remote"]["head"]
    return cluster.remote.view(("bh_writer", "issues"))


def test_barriers_order_an_adopt_against_a_stale_write_both_ways(tmp_path):
    """Both frames read the same remote head and park before their CAS; the release order alone
    decides the winner. Both orders run on ONE cluster, rewound in between."""
    with Cluster(tmp_path / "cl", [("a", BD_EMBEDDED), ("b", DOLT_CLI)]) as cluster:
        _install_writer(cluster, "a")
        cluster["b"].pull().check()
        baseline, placement = cluster.checkpoint(), cluster.hq.placement()
        recorder = Recorder(cluster, remote_tables=("bh_writer",))

        for first in ("adopt", "stale"):
            cluster.rewind(baseline)
            cluster.hq.restore(placement)
            remote = _race(cluster, recorder, first)
            writer = remote["tables"]["bh_writer"]
            titles = {row["title"] for row in remote["tables"]["issues"]}
            if first == "adopt":
                assert writer == [{"id": 1, "frame": "b", "epoch": 2}]
                assert "stale-write-adopt" not in titles
            else:
                assert writer == [{"id": 1, "frame": "a", "epoch": 1}]
                assert "stale-write-stale" in titles
                assert "stale-write-adopt" not in titles  # the rewind really reset the remote
