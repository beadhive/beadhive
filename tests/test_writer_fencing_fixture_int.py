"""Integration smoke test for the multi-frame writer-fencing fixture (spike bh-eybn7).

One test (bh-3nfbn) exercises every fixture capability -- cluster start-up, partition, kill,
barrier ordering -- on a SINGLE cluster, against REAL drivers: Dolt CLI frames, bd 1.3 embedded
frames (bd's linked Dolt) and bd 1.3 shared-server frames (a frame-private ``dolt sql-server``),
all sharing one ``git+file://`` remote. Evidence and timings:
``docs/spikes/bh-eybn7-writer-fencing-fixture.md``.

Marked ``integration`` (real bd/dolt) + ``dolt_server`` (server frames start real sql-servers,
so the test holds one of the run-wide slots ``_bound_concurrent_dolt_servers`` hands out).
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


def test_fixture_starts_partitions_kills_and_orders_barriers_on_one_cluster(tmp_path):
    frames = [("a", BD_EMBEDDED), ("b", BD_SERVER), ("c", DOLT_CLI)]
    with Cluster(tmp_path / "cl", frames) as cluster:
        a, b, c = cluster["a"], cluster["b"], cluster["c"]

        # --- start-up: hermetic frames on one remote, with the HQ stand-in and the recorder ---
        for frame in cluster.frames.values():
            assert frame.env["HOME"].startswith(str(cluster.root))
            assert frame.alive
        assert b.env["BEADS_SHARED_SERVER_DIR"] == str(b.server_dir)
        assert b.port != 3308 and pid_alive(b.server_pid())
        assert "BEADS_DOLT_SHARED_SERVER" not in a.env | c.env
        assert cluster.remote.url.startswith("git+file://")

        _install_writer(cluster, "a")
        assert cluster.hq.placement() == cluster.hq.placement(asker="b")
        with pytest.raises(ValueError, match="stale placement"):
            cluster.hq.place("b", expected_epoch=0)
        cluster.hq.partition("c")
        with pytest.raises(HQUnreachable):
            cluster.hq.placement(asker="c")
        cluster.hq.heal("c")

        recorder = Recorder(cluster, remote_tables=("bh_writer",), guard_tables=("bh_writer",))
        for frame in (a, b, c):
            with recorder.step("write+push", frame.name) as step:
                frame.pull().check()
                frame.create_issue(f"from-{frame.name}").check()
                frame.push().check()
            before, after = step["before"]["remote"], step["after"]["remote"]
            assert after["head"] != before["head"] and after["main"] != before["main"]
        assert recorder.trace.read_text().count("\n") == len(recorder.entries) == 6

        cluster.converge()
        for frame in (a, b, c):
            assert {"from-a", "from-b", "from-c"} <= frame.issue_titles()
        assert all(not e.get("partitioned") for e in b.gate_log())
        assert any(e["point"] == CAS for e in b.gate_log()), "server-side push bypassed the gate"

        # --- partition: cuts exactly one frame (the server frame) from the remote ---
        cut, peer = b, c
        cut.partition()
        cut.create_issue("cut-b").check()  # local writes still work
        head = cluster.remote.head()
        for attempt in (cut.push(), cut.pull()):
            assert not attempt.ok and PARTITION in attempt.output, attempt.output
        assert cluster.remote.head() == head
        peer.pull().check()
        peer.create_issue("peer-of-b").check()
        peer.push().check()
        assert cluster.remote.head() != head
        assert "cut-b" not in _remote_titles(cluster)
        cut.heal()
        cut.pull().check()
        cut.push().check()
        assert {"cut-b", "peer-of-b"} <= _remote_titles(cluster)
        assert any(e.get("partitioned") for e in cut.gate_log())
        cluster.converge()

        # --- kill: a push parked before its CAS dies; the remote is untouched, the frame recovers
        b.create_issue("doomed-b").check()
        view = cluster.remote.view(("issues",))
        hold = b.hold(CAS)
        push = b.push_async()
        gate = hold.wait()
        victims = b.kill()
        assert gate in victims and not any(pid_alive(pid) for pid in victims)
        assert push.result().killed and not b.alive
        assert not pid_alive(b.server_pid())
        assert cluster.remote.view(("issues",)) == view  # head and main untouched
        b.restart()
        assert "doomed-b" in b.issue_titles()  # the local commit survived
        b.push().check()
        assert "doomed-b" in _remote_titles(cluster)

        # --- barriers: the release order alone decides the winner; both orders, one rewind ---
        cluster.converge()
        epoch = cluster.hq.placement().epoch
        assert epoch == 1
        baseline, placement = cluster.checkpoint(), cluster.hq.placement()
        race_recorder = Recorder(cluster, remote_tables=("bh_writer",))
        for first in ("adopt", "stale"):
            cluster.rewind(baseline)
            cluster.hq.restore(placement)
            remote = _race(cluster, race_recorder, first)
            writer = remote["tables"]["bh_writer"]
            titles = {row["title"] for row in remote["tables"]["issues"]}
            if first == "adopt":
                assert writer == [{"id": 1, "frame": "b", "epoch": 2}]
                assert "stale-write-adopt" not in titles
            else:
                assert writer == [{"id": 1, "frame": "a", "epoch": 1}]
                assert "stale-write-stale" in titles
                assert "stale-write-adopt" not in titles  # the rewind really reset the remote

        agents = [f._proc.pid for f in cluster.frames.values()]
        server = b.server_pid()
    assert not any(pid_alive(pid) for pid in [*agents, server])
