"""The product coexistence adopt (:mod:`beadhive.writer_adopt`, bh-4c7p4) against REAL Dolt.

The hive is the composed in-data fence + write guard prototype (:mod:`harness.composed_fence`,
``bh-jbb6r``) on Dolt CLI frames sharing one ``git+file://`` remote, the faithful stand-in for the
product fence schema. Placement is the PRODUCT git-HQ adapter over ``refs/bh/lease/<prefix>``,
``refs/bh/epoch`` is the PRODUCT fence adapter, and the hive's data is reached through the HQ
storage port's PRODUCT Dolt ``HiveFence`` (bh-jhgum); only the statement engine that runs SQL
on a frame is the test adapter below. Judged on remote ``main``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time

import pytest

from beadhive import host_adopt
from beadhive import writer_adopt as wa
from beadhive_hq_storage_dolt import DoltHiveFence
from harness import composed_fence as cf
from harness import epoch_fence as ef
from harness import write_guard as wg
from harness.writer_fencing import BD_EMBEDDED, BD_SERVER, DOLT_CLI, Frame, Placement

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("dolt") is None or shutil.which("git") is None, reason="dolt/git missing"
    ),
]

FRAMES = [("a", BD_EMBEDDED), ("b", DOLT_CLI)]


class Crash(BaseException):
    """An injected process death."""


class FrameEngine:
    """A :class:`~beadhive_hq_storage_dolt.FenceStatementEngine` over one composed-fence frame:
    the statements run through the frame's own engine, exactly as the harness's adopt does."""

    def __init__(self, frame: Frame):
        self.frame = frame

    def query(self, sql: str) -> list[dict]:
        res = self.frame.sql(sql)
        if not res.ok:
            raise RuntimeError(res.output)
        text = res.stdout.strip()
        data = json.loads(text) if text else {}
        return data.get("rows", []) if isinstance(data, dict) else data

    def execute(self, statements) -> None:
        ef.run_script(self.frame, list(statements)).check()

    def commit(self, message: str) -> bool:
        res = self.frame.commit(message)
        if not res.ok and "nothing to commit" in res.output:
            return False
        res.check()
        return True

    def fetch(self) -> None:
        if self.frame.kind == BD_SERVER:
            self.frame.bd("sql", "CALL DOLT_FETCH('origin')").check()
        else:
            self.frame.dolt("fetch", "origin").check()

    def reset_to_remote(self) -> None:
        ef.sync_to_remote(self.frame)

    def push(self) -> bool:
        return self.frame.push().ok


#: The composed fence's guard set: the 42 guard triggers plus the two monotonic triggers.
COMPOSED_GUARDS = (*wg.trigger_names(), "bh_writer_monotonic", "bh_epoch_live_monotonic")


class FrameData(DoltHiveFence):
    """The port's Dolt :class:`~beadhive_hq_storage.HiveFence` over one composed-fence frame:
    the PRODUCT adapter (every bump, push and sentinel lookup statement is the adapter's) on the
    composed prototype's schema, through :class:`FrameEngine`."""

    def __init__(self, frame: Frame):
        super().__init__(FrameEngine(frame), guards=COMPOSED_GUARDS)
        self.frame = frame


def _ports(world: cf.World, tmp_path):
    hq = world.hq
    placement = host_adopt._LeasePlacement(
        remote=str(hq.repo),
        prefix=hq.prefix,
        cwd=hq.work,
        label="int",
        ttl=1800.0,
        at=time.time() + 7200,  # the founder's lease is past its hint: no takeover needed
        force=False,
    )
    work = tmp_path / "ref-work"
    subprocess.run(["git", "init", "-q", str(work)], check=True, capture_output=True)
    ref = host_adopt._GitEpochRef(remote=str(world.cluster.remote.path), cwd=work)
    return placement, ref


def _seed_ref(ref, frame: str, epoch: int) -> None:
    """The legacy fence as a pre-cutover hive carries it (``host_fence`` layout)."""
    from beadhive import host_fence

    host_fence.install_fence(
        ref._remote, host_fence.EpochFence(epoch=epoch, host_id=frame), expected="", cwd=ref._cwd
    )


def _remote(world: cf.World) -> dict:
    view = world.cluster.remote.view(("bh_writer", "bh_epoch_live", "bh_write_mark"))
    t = view["tables"]
    return {
        "writer": t["bh_writer"][0] if t["bh_writer"] else None,
        "live": sorted(r["epoch"] for r in t["bh_epoch_live"] or []),
        "marks": sorted((r["id"], r["epoch"]) for r in t["bh_write_mark"] or []),
    }


def _adopt_commits(world: cf.World) -> list[str]:
    world.cluster.remote.view(("bh_writer",))  # fetches the observer
    res = world.cluster.remote._dolt(
        "sql", "-r", "json", "-q", "SELECT message FROM dolt_log('origin/main')"
    )
    rows = json.loads(res.stdout).get("rows", [])
    return [r["message"] for r in rows if r["message"].startswith(wa.ADOPT_COMMIT_PREFIX)]


def test_coexistence_adopt_lands_the_sentinel_bump_with_placement_and_ref_in_lockstep(tmp_path):
    with cf.composed_world(tmp_path, FRAMES, hq_mode="git", writer="a") as world:
        placement, ref = _ports(world, tmp_path)
        e0 = world.hq.placement().epoch
        _seed_ref(ref, "a", e0)
        before = _remote(world)["writer"]

        out = wa.coexistence_adopt(FrameData(world["b"]), placement, ref, prefix="fx", frame="b")

        assert out.epoch == e0 + 1 and not out.resumed
        remote = _remote(world)
        assert (remote["writer"]["frame"], remote["writer"]["epoch"]) == ("b", e0 + 1)
        assert remote["writer"]["revision"] != before["revision"]
        assert len(remote["writer"]["revision"]) == 64
        assert remote["live"] == [e0 + 1]
        assert remote["marks"] == [(f"adopt-{e0 + 1}", e0 + 1)]  # older marks retired
        assert world.hq.placement() == Placement("b", e0 + 1)
        assert (ref.read().frame, ref.read().epoch) == ("b", e0 + 1)
        # The old writer is fenced by the guard once it pulls the bump.
        a = world["a"]
        ef.sync_to_remote(a)
        assert not cf.write(a, "stale-a").ok


def test_a_crash_before_push_reruns_to_one_bump_on_real_dolt(tmp_path):
    with cf.composed_world(tmp_path, FRAMES, hq_mode="git", writer="a") as world:
        placement, ref = _ports(world, tmp_path)
        e0 = world.hq.placement().epoch
        _seed_ref(ref, "a", e0)
        data = FrameData(world["b"])

        def crash():
            raise Crash("killed after the bump commit, before its push")

        with pytest.raises(Crash):
            wa.coexistence_adopt(data, placement, ref, prefix="fx", frame="b", before_push=crash)
        assert _remote(world)["writer"]["epoch"] == e0  # nothing published
        report = wa.adopt_report("fx", wa.PlacementView("b", e0 + 1), data.remote_writer())
        assert report is not None  # adopt incomplete, as doctor would show it

        out = wa.coexistence_adopt(data, placement, ref, prefix="fx", frame="b")
        assert out.resumed and out.epoch == e0 + 1
        assert world.hq.placement() == Placement("b", e0 + 1)  # no second placement bump
        assert _adopt_commits(world) == [f"{wa.ADOPT_COMMIT_PREFIX}b@{e0 + 1}"]
        assert _remote(world)["marks"] == [(f"adopt-{e0 + 1}", e0 + 1)]


def test_a_dropped_and_recreated_bh_writer_cannot_regress_the_epoch(tmp_path):
    with cf.composed_world(tmp_path, FRAMES, hq_mode="git", writer="a") as world:
        placement, ref = _ports(world, tmp_path)
        a = world["a"]
        high = cf.adopt(world.cluster, a, world.hq.place("a").epoch).outcome
        assert high == "landed"
        high_epoch = _remote(world)["writer"]["epoch"]

        # Recreate bh_writer far lower and pull placement down with it: only history remembers.
        ef.sync_to_remote(a)
        ef.run_script(
            a,
            [
                "DROP TABLE bh_writer",
                "CREATE TABLE bh_writer (id INT PRIMARY KEY, frame VARCHAR(64) NOT NULL, "
                "epoch BIGINT NOT NULL, revision VARCHAR(64) NOT NULL)",
                "INSERT INTO bh_writer VALUES (1, 'a', 1, UUID())",
                "CREATE TRIGGER bh_writer_monotonic BEFORE UPDATE ON bh_writer FOR EACH ROW "
                f"{ef._MONOTONIC_BODY}",
            ],
        ).check()
        a.commit("drop and recreate bh_writer low").check()
        a.push().check()
        world.hq.restore(Placement("a", 1))
        assert _remote(world)["writer"]["epoch"] == 1
        data = FrameData(world["b"])
        data.sync_to_remote()
        assert data.epoch_floor().history_max >= high_epoch

        out = wa.coexistence_adopt(data, placement, ref, prefix="fx", frame="b")
        assert out.epoch == high_epoch + 1
        assert _remote(world)["writer"]["epoch"] == high_epoch + 1
