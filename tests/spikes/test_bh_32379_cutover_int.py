"""Executable evidence for the bh-32379 spike; not a product contract test.

Question: can a hive that is fenced today by ``refs/bh/epoch`` (beside ``refs/dolt/data``) plus
the HQ host lease be cut over to the in-data fence (``bh_writer`` / ``bh_epoch_live`` /
``bh_write_mark`` and the guard triggers) with epochs that stay monotonic across the two
carriers, while a replica that runs an older bh still fails closed, and can the cutover be
rolled back without moving any epoch backwards? See
``docs/spikes/bh-32379-writer-partitioning-migration.md``.

The fence model is ``harness.epoch_fence`` (bh-vje85) on the bh-eybn7 fixture
(``harness.writer_fencing``). The legacy fence ref is written into the fixture's bare remote
exactly as ``host_fence`` lays it out: a JSON blob ``{"epoch", "host_id", "seq"}`` at
``refs/bh/epoch``, moved only by an old-oid compare-and-swap (``git update-ref <ref> <new>
<old>``, the local equivalent of ``gitref.cas``'s ``--force-with-lease``). The writer is a
bd shared-server frame, as the live ``bh`` hive is (``dolt_mode: server``); the second replica is
a Dolt CLI frame that first plays an unprovisioned older bh and then the next writer.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from beadhive.claim_authority import ClaimRecord
from harness.epoch_fence import (
    GUARD_REFUSAL,
    adopt,
    fence_statements,
    remote_fence,
    run_script,
    set_identity,
    sync_to_remote,
)
from harness.writer_fencing import BD_SERVER, DOLT_CLI, Cluster, Frame, Placement, Recorder

pytestmark = [
    pytest.mark.integration,
    pytest.mark.dolt_server,
    pytest.mark.skipif(
        shutil.which("bd") is None or shutil.which("dolt") is None, reason="bd/dolt not installed"
    ),
]

FRAMES = [("a", BD_SERVER), ("c", DOLT_CLI)]
REMOTE_TABLES = ("bh_writer", "bh_epoch_live", "bh_write_mark")
EPOCH_REF = "refs/bh/epoch"
#: The live ``bh`` hive's fence and lease epoch, read on 2026-10-04 (``refs/bh/epoch`` blob
#: ``{"epoch": 221, ..., "seq": 0}``; every claim record minted that day carries epoch 221).
LIVE_EPOCH = 221

#: A raw main write on a Dolt CLI replica (``Frame.create_issue`` would raise on refusal).
ISSUE_INSERT = (
    "insert into issues (id, title, description, design, acceptance_criteria, notes) "
    "values ('{id}', '{id}', '', '', '', '')"
)

ROLLBACK_STATEMENTS = [
    # Triggers first (the guard reads bh_writer), then the FK child before its parent. The
    # dolt_ignore row for bh_local_ident is KEPT: dropping it would let a later
    # `bd dolt commit` stage a replica's leftover identity table.
    "DROP TRIGGER IF EXISTS bh_guard_issues_ins",
    "DROP TRIGGER IF EXISTS bh_guard_issues_upd",
    "DROP TRIGGER IF EXISTS bh_writer_monotonic",
    "DROP TRIGGER IF EXISTS bh_epoch_live_monotonic",
    "DROP TABLE bh_write_mark",
    "DROP TABLE bh_epoch_live",
    "DROP TABLE bh_writer",
]


def _git_remote(cluster: Cluster, *args: str, data: str | None = None) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(cluster.remote.path), *args],
        input=data,
        capture_output=True,
        text=True,
        check=True,
        env=cluster.remote._env,
    ).stdout.strip()


def _read_epoch_ref(cluster: Cluster) -> tuple[str, dict]:
    sha = _git_remote(cluster, "rev-parse", "-q", "--verify", EPOCH_REF)
    return sha, json.loads(_git_remote(cluster, "cat-file", "-p", sha))


def _cas_epoch_ref(cluster: Cluster, record: dict, *, expected: str | None) -> str:
    """Move ``refs/bh/epoch`` to ``record`` only if it still points at ``expected``."""
    blob = _git_remote(
        cluster, "hash-object", "-w", "--stdin", data=json.dumps(record, separators=(",", ":"))
    )
    _git_remote(cluster, "update-ref", EPOCH_REF, blob, *([expected] if expected else []))
    return blob


def _cutover(cluster: Cluster, writer: Frame) -> int:
    """The per-hive cutover, steps C1-C6 of the spike doc, on the current holder.

    Seeds ``bh_writer`` at ``max(refs/bh/epoch, placement)`` for the SAME holder, so every claim
    token minted under that epoch stays current, then publishes through the coexistence managed
    path: reserve ``refs/bh/epoch`` (``seq + 1``, CAS on its sha), push, verify the reservation.
    """
    ref_sha, ref = _read_epoch_ref(cluster)  # C1: read both legacy carriers
    placement = cluster.hq.placement()
    if (ref["host_id"], ref["epoch"]) != (placement.writer, placement.epoch):
        raise AssertionError("adopt incomplete: converge with a normal adopt before cutover")
    if placement.writer != writer.name:
        raise AssertionError("only the current holder cuts a hive over")
    epoch = max(int(ref["epoch"]), placement.epoch)  # C2: seed epoch, never below either
    sync_to_remote(writer)
    statements = fence_statements(writer.name, epoch)  # C3: schema + seed + guard, one commit
    statements.append(f"INSERT INTO bh_write_mark VALUES ('cutover-{epoch}', {epoch})")
    run_script(writer, statements).check()
    writer.commit(f"bh: cut over to the in-data epoch fence at {epoch}").check()
    set_identity(writer)  # C4: identity after the ignore row is committed, before any bd write
    reserved = _cas_epoch_ref(cluster, {**ref, "seq": int(ref["seq"]) + 1}, expected=ref_sha)
    writer.push().check()  # C5: managed push (reserve, push, verify)
    assert _read_epoch_ref(cluster)[0] == reserved
    return epoch


def test_cutover_seeds_from_the_epoch_ref_fails_closed_across_versions_and_rolls_back(tmp_path):
    with Cluster(tmp_path, FRAMES) as cluster:
        a, c = cluster["a"], cluster["c"]
        recorder = Recorder(cluster, tmp_path / "trace.jsonl", remote_tables=REMOTE_TABLES)

        # Legacy state as on the live factory frame: fence ref and placement agree on (a, 221).
        _cas_epoch_ref(cluster, {"epoch": LIVE_EPOCH, "host_id": "a", "seq": 0}, expected=None)
        cluster.hq.restore(Placement("a", LIVE_EPOCH))
        a.create_issue("pre-cutover").check()
        a.push().check()
        c.pull().check()
        in_flight = ClaimRecord(bead="fx-1", seat="dev/x", worktree="/w", issued_at="", epoch=221)

        # Cutover: bh_writer is seeded from refs/bh/epoch; nothing moves backwards.
        with recorder.step("cutover", "a"):
            epoch = _cutover(cluster, a)
        fence = remote_fence(cluster)
        _, ref = _read_epoch_ref(cluster)
        assert epoch == LIVE_EPOCH
        assert fence["writer"] == ("a", LIVE_EPOCH) and fence["live"] == [LIVE_EPOCH]
        assert fence["marks"] == [LIVE_EPOCH]  # the cutover sentinel
        assert (ref["epoch"], ref["seq"]) == (LIVE_EPOCH, 1)
        assert "pre-cutover" in fence["titles"]
        assert not in_flight.is_stale(fence["writer"][1])  # in-flight claims survive cutover
        # The identity row is node-local: it never reaches the remote.
        assert cluster.remote.view(("bh_local_ident",))["tables"]["bh_local_ident"] is None

        # The writer keeps writing on the managed path: commit the working set, then push.
        a.create_issue("post-cutover-a").check()
        a.commit("bh: commit working set before push")  # server-mode mark (bh-vje85 E7)
        a.push().check()
        assert "post-cutover-a" in remote_fence(cluster)["titles"]

        # Mixed version: a replica whose bh never provisioned an identity fails closed on main,
        # and once provisioned (new bh) it is an ordinary refused non-writer.
        c.pull().check()
        unprovisioned = c.sql(ISSUE_INSERT.format(id="old-bh-on-c"))
        assert not unprovisioned.ok and "bh_local_ident" in unprovisioned.output
        sync_to_remote(c)
        set_identity(c)
        refused = c.sql(ISSUE_INSERT.format(id="non-writer-c"))
        assert not refused.ok and GUARD_REFUSAL in refused.output
        sync_to_remote(c)

        # Coexistence adopt a -> c: placement first, then the legacy ref, then the data bump.
        before_sha, _ = _read_epoch_ref(cluster)
        with recorder.step("coexistence-adopt", "c"):
            new = cluster.hq.place("c", expected_epoch=LIVE_EPOCH).epoch
            _cas_epoch_ref(cluster, {"epoch": new, "host_id": "c", "seq": 0}, expected=before_sha)
            assert adopt(cluster, c, new) == "landed"
        fence = remote_fence(cluster)
        _, ref = _read_epoch_ref(cluster)
        assert new == LIVE_EPOCH + 1
        assert fence["writer"] == ("c", new) and fence["live"] == [new] and fence["marks"] == [new]
        assert (ref["epoch"], ref["host_id"], cluster.hq.placement().epoch) == (new, "c", new)
        assert in_flight.is_stale(new)  # a real handoff does invalidate old tokens
        # An older bh on a still reserves the legacy ref from the sha it last saw: refused.
        with pytest.raises(subprocess.CalledProcessError):
            stale_reservation = {"epoch": LIVE_EPOCH, "host_id": "a", "seq": 2}
            _cas_epoch_ref(cluster, stale_reservation, expected=before_sha)
        # A new bh on a pulls the bump and is fenced by the guard.
        a.pull().check()
        stale = a.create_issue("stale-a")
        assert not stale.ok and GUARD_REFUSAL in stale.output
        c.create_issue("post-adopt-c").check()
        c.push().check()

        # Mixed version, the dangerous order: an OLDER bh adopts on a cut-over hive. It moves
        # placement and the legacy ref but not bh_writer, so nobody can write main (fail closed):
        # the new holder is refused by the guard, the data writer's managed push by the ref.
        seen_by_c, _ = _read_epoch_ref(cluster)
        with recorder.step("legacy-adopt", "a"):
            legacy = cluster.hq.place("a", expected_epoch=new).epoch
            _cas_epoch_ref(cluster, {"epoch": legacy, "host_id": "a", "seq": 0}, expected=seen_by_c)
        assert remote_fence(cluster)["writer"] == ("c", new)
        holder = a.create_issue("legacy-holder-a")
        assert not holder.ok and GUARD_REFUSAL in holder.output
        with pytest.raises(subprocess.CalledProcessError):
            _cas_epoch_ref(cluster, {"epoch": new, "host_id": "c", "seq": 1}, expected=seen_by_c)
        # Recovery is roll-forward: the (upgraded) holder re-runs adopt step 2 at its epoch.
        with recorder.step("adopt-step2-recovery", "a"):
            assert adopt(cluster, a, legacy) == "landed"
        assert remote_fence(cluster)["writer"] == ("a", legacy) == ("a", new + 1)
        a.create_issue("recovered-a").check()
        a.commit("bh: commit working set before push")
        a.push().check()

        # Rollback (R1-R5): carry the epoch floor in the legacy carriers, then drop the fence in
        # one forward commit. Never force-push, never move refs/bh/epoch back.
        with recorder.step("rollback", "a"):
            sync_to_remote(a)
            history_max = int(a.query("SELECT MAX(epoch) AS m FROM dolt_history_bh_writer")[0]["m"])
            ref_sha, ref = _read_epoch_ref(cluster)
            floor = max(history_max, int(ref["epoch"]), cluster.hq.placement().epoch)
            assert floor == int(ref["epoch"]) == legacy  # the ref already carries the floor
            run_script(a, ROLLBACK_STATEMENTS).check()
            a.commit("bh: roll back the in-data epoch fence").check()
            a.push().check()
        view = cluster.remote.view(("bh_writer", "bh_epoch_live", "bh_write_mark", "issues"))
        assert all(view["tables"][t] is None for t in REMOTE_TABLES)
        titles = {row["title"] for row in view["tables"]["issues"]}
        assert {"pre-cutover", "post-cutover-a", "post-adopt-c", "recovered-a"} <= titles
        assert not {"stale-a", "old-bh-on-c", "non-writer-c", "legacy-holder-a"} & titles
        assert _read_epoch_ref(cluster) == (ref_sha, ref)  # untouched by the rollback

        # After rollback the legacy model is in force again and resumes ABOVE the floor.
        c.pull().check()
        c.create_issue("post-rollback-c").check()
        c.push().check()
        assert max(int(ref["epoch"]), cluster.hq.placement().epoch) + 1 > history_max
