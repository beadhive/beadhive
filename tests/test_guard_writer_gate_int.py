"""The clock-free write gate (bh-12hev) against REAL Dolt on the composed fence prototype.

The hive is the composed in-data fence + write guard (:mod:`harness.composed_fence`,
``bh-jbb6r``) on Dolt frames sharing one remote, with git-mode HQ placement at
``refs/bh/lease/<prefix>`` — the product fence module (M1, ``bh-uz46l``) is not built, so this
is the faithful stand-in for its schema. Only the hive's ``bh_writer`` read is the test adapter
below; the guard, the lease cache and the claim token are the product code.

It pins the acceptance end to end: the composed install seeds ``bh_writer.epoch`` equal to the
placement (lease) epoch, so a claim minted from the lease before the hive is seen as cut over
stays valid after; with HQ taken DOWN and the clock far past any expiry the writer frame keeps
writing, the other frame is refused, and ``live_epoch`` reads the data.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time

import pytest
import typer

from beadhive import claim_authority, fence_data_port, guard, host, host_lease, registry
from beadhive import host_lease_contracts as contracts
from beadhive import writer_adopt as wa
from harness import composed_fence as cf
from harness.writer_fencing import BD_EMBEDDED, DOLT_CLI, Frame

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("dolt") is None or shutil.which("git") is None, reason="dolt/git missing"
    ),
]

FRAMES = [("a", BD_EMBEDDED), ("b", DOLT_CLI)]


class LocalWriter:
    """The ``writer()`` leg of :class:`~beadhive.writer_adopt.FenceData` over one frame's LOCAL
    ``main`` — never the remote, exactly what the guard is allowed to read."""

    def __init__(self, frame: Frame):
        self.frame = frame

    def writer(self) -> wa.WriterRow | None:
        res = self.frame.sql("SELECT frame, epoch, revision FROM bh_writer WHERE id = 1")
        if not res.ok:
            if "not found" in res.output:
                return None
            raise AssertionError(res.output)
        rows = json.loads(res.stdout or "{}").get("rows", []) if res.stdout.strip() else []
        if not rows:
            return None
        return wa.WriterRow(rows[0]["frame"], int(rows[0]["epoch"]), rows[0]["revision"])


def test_writer_keeps_writing_with_hq_down_and_a_pre_cutover_claim_survives(tmp_path, monkeypatch):
    with cf.composed_world(tmp_path, FRAMES, hq_mode="git", writer="a") as world:
        hq = world.hq
        # This host's HQ clone: the harness HQ worktree, wired to the placement remote, with the
        # lease mirrored into the local cache exactly as a won adopt leaves it.
        subprocess.run(
            ["git", "-C", str(hq.work), "remote", "add", "origin", str(hq.repo)],
            check=True,
            capture_output=True,
        )
        lease = host_lease.refresh_cached("origin", hq.prefix, cwd=hq.work)
        assert lease is not None and lease.host_id == "a"
        monkeypatch.setenv("BH_HQ", str(hq.work))

        current = {"frame": "a"}
        monkeypatch.setattr(host, "host_id", lambda: current["frame"])
        entry = {"provider": "github", "org": "o", "repo": "r", "prefix": hq.prefix}
        monkeypatch.setattr(registry, "hive_dir_for", lambda _cfg, _hive: tmp_path / "hive")
        monkeypatch.setattr(registry, "entry_for_dir", lambda _cfg, _dir: entry)

        # Pre-cutover view (no fence adapter registered): the claim is minted from the lease.
        record = claim_authority.ClaimRecord(
            bead=f"{hq.prefix}-1",
            seat="dev/x",
            worktree="/w",
            issued_at="t",
            host_id="a",
            epoch=guard.live_epoch("", cfg={}),
        )
        assert record.epoch == lease.epoch

        # The hive's data switches it on: the adapter reads each frame's local bh_writer.
        monkeypatch.setattr(
            fence_data_port,
            "_fence_data_resolver",
            lambda _prefix, _dir: LocalWriter(world[current["frame"]]),
        )
        assert guard.live_epoch("", cfg={}) == lease.epoch  # seed equality (T1)

        hq.down()  # HQ unreachable from here on
        far = time.time() + 365 * 86400  # far past the lease's expires_at hint
        monkeypatch.setattr(contracts.time, "time", lambda: far)
        for _ in range(2):
            guard.guard_primary("", cfg={}, verb="claim")  # the writer keeps writing
        guard.guard_claim_epoch(record, "", cfg={}, verb="work submit")  # token still valid
        assert cf.write(world["a"], "written while HQ is down").ok  # and the data agrees

        current["frame"] = "b"  # the same question asked on the other frame
        with pytest.raises(typer.Exit):
            guard.guard_primary("", cfg={})
        assert guard.live_epoch("", cfg={}) == lease.epoch
