"""Failover reclaim's adopt-kind matrix on the product adopt over the in-memory fence (bh-utvyj).

``tests/test_failover_reclaim_int.py`` ran these on a scratch SQL HQ and the composed Dolt
fence. What they assert is protocol: which adopt kind reclaims, that the reclaim rides the ONE
bump commit, and that a re-run reclaims nothing twice. Here the same PRODUCT code runs —
:func:`beadhive.hq_sql_placement.place_cas` (the director's CAS with its cause),
:func:`beadhive.host_adopt._director_placement` / ``_adopt_director_placed`` /
``_DirectorPlacement``, :func:`beadhive.writer_adopt.coexistence_adopt` and
:class:`beadhive.failover_reclaim.FailoverReclaim` with the product
:class:`~beadhive.failover_reclaim.GitBackupProbe` and the product ``refs/bh/epoch`` adapter
over real git — with HQ and the hive's data on the HQ storage port's fakes:

* HQ is :class:`~beadhive_hq_storage.InMemoryHqStorage`; the frame reads placement through the
  same port read (:func:`~beadhive.hq_sql_placement.read_row`) its SELECT grant serves on Dolt;
* the hive's data is :class:`ReclaimingFence`, the in-memory fence plus the
  :class:`~beadhive.failover_reclaim.ReclaimData` reads the root ``FenceNode`` serves on Dolt
  (the ``bh.*`` policy rows and the ``in_progress`` claims of the synced head).

The end-to-end failover with real ``bd`` claims, a real backup remote and the reclaim's bead
rows on real Dolt stays as the family's smoke:
``test_failover_reclaim_int.py::test_failover_adopt_reclaims_only_the_dead_frames_claims_on_real_dolt``.

Time: the director's lease stamps come from a :class:`FakeClock`; nothing sleeps.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping

import pytest

from beadhive import failover_reclaim as fr
from beadhive import host_adopt, host_fence, host_lease, work_backup
from beadhive import writer_adopt as wa
from beadhive.host_lease_contracts import HostLease, now_stamp
from beadhive.hq_sql_placement import CAUSE_FAILOVER, CAUSE_PLANNED, place_cas, read_row
from beadhive_hq_storage import (
    FenceSnapshot,
    InMemoryHiveFence,
    InMemoryHqStorage,
    RecordKind,
    WriterRow,
)
from harness.clock import FakeClock

PREFIX = "fx"
DEAD = "a"
ADOPTER = "b"
#: The hive's epoch before the failover (any value above 1 works).
E0 = 3
POLICY = {"bh.pairing.enabled": "true", "bh.reclaim.failover.mode": "apply"}


class ReclaimingFence(InMemoryHiveFence):
    """The in-memory fence plus :class:`~beadhive.failover_reclaim.ReclaimData`: the policy rows
    and the ``in_progress`` claims of this node's LOCAL ``main`` (step 2 reads them right after
    its sync), as ``FenceNode.config_rows`` / ``claims`` read them on Dolt."""

    def __init__(self, clock, *, config: Mapping[str, str], **kwargs):
        super().__init__(clock, **kwargs)
        self._config = dict(config)

    def config_rows(self) -> dict[str, str]:
        return {k: v for k, v in self._config.items() if k.startswith("bh.")}

    def claims(self) -> list[fr.Claim]:
        state = self.snapshot_at(self.local_head())
        out = []
        for bead, (status, assignee) in sorted(state.beads.items()):
            if status == "in_progress":
                labels = frozenset(label for b, label in state.labels if b == bead)
                out.append(fr.Claim(bead, assignee, labels))
        return out


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _backup_remote(tmp_path):
    """A bare backup remote with ``main`` and the adopter's hive clone of it (D3's probe)."""
    bare = tmp_path / "origin.git"
    _git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    work = tmp_path / "work"
    _git("clone", "-q", str(bare), str(work), cwd=tmp_path)
    for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
        _git("config", k, v, cwd=work)
    _git("commit", "-q", "--allow-empty", "-m", "base", cwd=work)
    _git("push", "-q", "origin", "HEAD:main", cwd=work)
    hive = tmp_path / "hive-clone"
    _git("clone", "-q", str(bare), str(hive), cwd=tmp_path)
    return bare, hive


def _hive_remote(tmp_path, hive_clone) -> str:
    """The hive's git remote carrying the legacy ``refs/bh/epoch``, held by the dead frame at
    ``E0`` (``host_fence`` layout, as a pre-cutover hive carries it)."""
    remote = tmp_path / "hive-remote.git"
    _git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    host_fence.install_fence(
        str(remote), host_fence.EpochFence(epoch=E0, host_id=DEAD), expected="", cwd=hive_clone
    )
    return str(remote)


def _data(clock, bead: str) -> ReclaimingFence:
    """The hive as the dead frame left it: ``a`` writes at ``E0`` and has published one claim."""
    return ReclaimingFence(
        clock,
        config=POLICY,
        writer=WriterRow(DEAD, E0),
        beads={bead: ("in_progress", "dev/x")},
        labels={(bead, f"{work_backup.CLAIM_FRAME_DIMENSION}:{DEAD}")},
    )


def _remote(data: InMemoryHiveFence) -> FenceSnapshot:
    return data.snapshot_at(data.remote_head())


def _comments(data: InMemoryHiveFence, bead: str) -> list[tuple[str, str]]:
    return sorted(
        (author, text) for _id, b, author, text, _at in _remote(data).comments if b == bead
    )


def _adopt_commits(data: InMemoryHiveFence) -> list[str]:
    """The adopt bump commits on the remote head's history, newest first."""
    out = []
    for revision in data.chain(data.remote_head()):
        if data.sentinel_of(revision) is not None:
            writer = data.snapshot_at(revision).writer
            out.append(f"{wa.ADOPT_COMMIT_PREFIX}{writer.frame}@{writer.epoch}")
    return out


class _FramePlane:
    """The frame's view of SQL HQ: the placement record read through the port."""

    config_backend = "sql"

    def __init__(self, store: InMemoryHqStorage):
        self.store = store

    def read_placement(self, prefix):
        return read_row(self.store, prefix)


def test_a_planned_handoff_reclaims_nothing(tmp_path):
    clock = FakeClock()
    bare, hive_clone = _backup_remote(tmp_path)
    hq = InMemoryHqStorage(clock)
    hq.provision(RecordKind.PLACEMENT, PREFIX, {"frame": DEAD, "epoch": E0})
    data = _data(clock, "fx-held")
    hook = fr.FailoverReclaim(data, fr.GitBackupProbe(hive_clone, default_remote=str(bare)))
    ref = host_adopt._GitEpochRef(remote=_hive_remote(tmp_path, hive_clone), cwd=hive_clone)

    out = wa.coexistence_adopt(
        data, hq.placements(), ref, prefix=PREFIX, frame=ADOPTER, reclaim=hook, failover=False
    )

    assert out.step2.landed and "planned handoff" in out.step2.reclaim.skipped
    assert _remote(data).beads["fx-held"][0] == "in_progress"
    assert _comments(data, "fx-held") == []
    assert _adopt_commits(data) == [f"{wa.ADOPT_COMMIT_PREFIX}{ADOPTER}@{E0 + 1}"]


# ---- director-placed failover: the cause rides the placement CAS (bh-16347.6) ---------------


@pytest.mark.parametrize("cause", ["failover", "planned", "no-cause", "older-frame"])
def test_a_director_placed_adopt_reclaims_exactly_once_and_only_on_failover(
    tmp_path, monkeypatch, cause
):
    """The director places ``b`` at the next epoch; ``b`` adopts through the director-placed path,
    which RESUMES at the placed epoch, so the displaced placement cannot say why. The cause the
    director wrote in the same CAS does: a failover placement reclaims ``a``'s claim once (a
    re-run reclaims nothing), a planned one reclaims nothing, and a row without a cause or a
    frame that does not read it (older 0.22.x/0.23 builds) is unknown, which writes nothing."""
    clock = FakeClock()
    _bare, hive_clone = _backup_remote(tmp_path)
    remote = _hive_remote(tmp_path, hive_clone)
    ref = host_adopt._GitEpochRef(remote=remote, cwd=hive_clone)
    bead = "fx-unbacked"
    data = _data(clock, bead)

    # ---- the director places b (the operator seeded the hive at a's epoch) ------------------
    hq = InMemoryHqStorage(clock)
    seed = HostLease("", "", E0, now_stamp(clock.wall()), now_stamp(clock.wall()))
    hq.provision(
        RecordKind.PLACEMENT,
        PREFIX,
        {"authority": {}, "lease": seed.to_record(), "director": True},
    )
    seeded = read_row(hq, PREFIX).revision
    now = clock.wall()
    place_cas(
        hq,
        prefix=PREFIX,
        lease=HostLease(ADOPTER, ADOPTER, E0 + 1, now_stamp(now), now_stamp(now + 3600)),
        authority={"frame_id": ADOPTER, "holder_identity": ADOPTER},
        expected_revision=seeded,
        cause={
            "failover": CAUSE_FAILOVER,
            "planned": CAUSE_PLANNED,
            "older-frame": CAUSE_FAILOVER,
        }.get(cause),
    )

    # ---- frame b adopts through the director-placed path --------------------------------
    plane = _FramePlane(hq)
    monkeypatch.setattr(host_lease, "_frame_plane", lambda cwd: plane)

    def adopt():
        if cause == "older-frame":  # a frame that never reads the cause
            return wa.coexistence_adopt(
                data,
                host_adopt._DirectorPlacement(plane, PREFIX),
                ref,
                prefix=PREFIX,
                frame=ADOPTER,
                reclaim=fr.for_fence_data(data, cwd=hive_clone),
            )
        placed = host_adopt._director_placement(PREFIX, tmp_path)
        assert placed is not None and placed.director
        return host_adopt._adopt_director_placed(
            placed,
            prefix=PREFIX,
            hive_remote=remote,
            hive_cwd=hive_clone,
            hq_cwd=tmp_path,
            host_id=ADOPTER,
            fence_data=data,
            attempts=None,
            failover=None,
        ).coexistence

    out = adopt()
    assert out.resumed and out.epoch == E0 + 1 and out.step2.landed
    plan = out.step2.reclaim
    bump = f"{wa.ADOPT_COMMIT_PREFIX}{ADOPTER}@{E0 + 1}"
    assert _adopt_commits(data) == [bump]
    assert (ref.read().frame, ref.read().epoch) == (ADOPTER, E0 + 1)  # the ref in lockstep
    if cause == "failover":
        assert plan.applied and plan.dead_frame == DEAD
        assert {r.bead: r.outcome for r in plan.rows} == {bead: fr.Outcome.REWOUND}
        assert _remote(data).beads[bead][0] == "open"
        assert len(_comments(data, bead)) == 1
        # the reclaim rode the bump commit itself: its parent still holds the claim
        head = data.remote_head()
        assert data.sentinel_of(head) is not None
        assert data.snapshot_at(data.parent_of(head)).beads[bead][0] == "in_progress"
    else:
        why = "planned handoff" if cause == "planned" else "adopt kind unknown"
        assert not plan.applied and why in plan.skipped
        assert _remote(data).beads[bead][0] == "in_progress"
        assert _comments(data, bead) == []

    # ---- a re-run reclaims nothing twice ------------------------------------------------
    again = adopt()
    assert again.resumed and again.step2.reclaim is None
    assert _adopt_commits(data) == [bump]
    assert len(_comments(data, bead)) == (1 if cause == "failover" else 0)
