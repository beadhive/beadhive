"""Failover reclaim (bh-4z2rx, M3; M14 D5a) against REAL Dolt and a REAL git backup remote.

The hive is the composed in-data fence + write guard prototype (:mod:`harness.composed_fence`,
``bh-jbb6r``): frame ``a`` (bd-embedded) is the primary that dies, frame ``b`` (Dolt CLI) is the
surviving executor that adopts. Claims are made with real ``bd`` on ``a`` and published; the
reclaim hook (:class:`beadhive.failover_reclaim.FailoverReclaim`) reads them on ``b`` through a
test :class:`~beadhive.failover_reclaim.ReclaimData` adapter (M1 is not built) and probes a bare
git remote with :class:`~beadhive.failover_reclaim.GitBackupProbe`, the product D3 check. Judged
on the remote head.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from beadhive import failover_reclaim as fr
from beadhive import work_backup
from beadhive import writer_adopt as wa
from harness import composed_fence as cf
from harness.writer_fencing import BD_EMBEDDED, DOLT_CLI
from test_writer_adopt_int import FrameData, _adopt_commits, _ports, _seed_ref

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("dolt") is None or shutil.which("git") is None or shutil.which("bd") is None,
        reason="dolt/git/bd missing",
    ),
]

FRAMES = [("a", BD_EMBEDDED), ("b", DOLT_CLI)]


class ReclaimFrameData(FrameData):
    """FenceData + ReclaimData over one composed-fence frame (the M1 adapter's stand-in)."""

    def config_rows(self):
        rows = self.frame.query(
            "SELECT `key` AS k, `value` AS v FROM config WHERE `key` LIKE 'bh.%'"
        )
        return {r["k"]: r["v"] for r in rows}

    def claims(self):
        rows = self.frame.query(
            "SELECT i.id, i.assignee, l.label FROM issues i LEFT JOIN labels l "
            "ON l.issue_id = i.id WHERE i.status = 'in_progress'"
        )
        by: dict[str, tuple[str, set[str]]] = {}
        for r in rows:
            assignee, labels = by.setdefault(r["id"], (r.get("assignee") or "", set()))
            if r.get("label"):
                labels.add(r["label"])
        return [fr.Claim(bead, a, frozenset(labels)) for bead, (a, labels) in by.items()]


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _backup_remote(tmp_path):
    """A bare remote with ``main`` and a clone that commits work; returns (bare, work, hive)."""
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
    return bare, work, hive


def _push_backup(work, bead, frame, *, with_work: bool) -> str:
    if with_work:
        _git("commit", "-q", "--allow-empty", "-m", f"wip {bead}", cwd=work)
    sha = _git("rev-parse", "HEAD", cwd=work).stdout.strip()
    _git("push", "-q", "origin", f"{sha}:{work_backup.backup_ref(bead, frame)}", cwd=work)
    if with_work:
        _git("reset", "-q", "--hard", "HEAD~1", cwd=work)
    return sha


def _remote_refs(bare) -> dict[str, str]:
    out = _git("for-each-ref", "--format=%(refname) %(objectname)", "refs/bh/backup/", cwd=bare)
    return dict(line.split() for line in out.stdout.splitlines())


def _bd_json(frame, *args):
    res = frame.bd(*args, "--json")
    res.check()
    return json.loads(res.stdout)


def _claim(frame, title, *labels) -> str:
    created = _bd_json(frame, "create", "--title", title, "-t", "task", "-p", "2")
    bead = created["id"] if isinstance(created, dict) else created[0]["id"]
    frame.bd("update", bead, "--claim").check()
    for label in labels:
        frame.bd("label", "add", bead, label).check()
    return bead


def _remote_rows(world, sql):
    world.cluster.remote.view(("bh_writer",))  # fetches the observer
    res = world.cluster.remote._dolt("sql", "-r", "json", "-q", sql)
    return json.loads(res.stdout or "{}").get("rows", [])


def _remote_issue(world, bead):
    [row] = _remote_rows(
        world,
        f"SELECT status, assignee, started_at FROM issues AS OF 'origin/main' WHERE id = '{bead}'",
    )
    # Dolt's JSON output omits NULL columns.
    return {k: row.get(k) for k in ("status", "assignee", "started_at")}


def _remote_labels(world, bead):
    rows = _remote_rows(
        world, f"SELECT label FROM labels AS OF 'origin/main' WHERE issue_id = '{bead}'"
    )
    return sorted(r["label"] for r in rows)


def _remote_comments(world, bead):
    return _remote_rows(
        world, f"SELECT author, text FROM comments AS OF 'origin/main' WHERE issue_id = '{bead}'"
    )


def test_failover_adopt_reclaims_only_the_dead_frames_claims_on_real_dolt(tmp_path):
    bare, work, hive_clone = _backup_remote(tmp_path)
    with cf.composed_world(tmp_path, FRAMES, hq_mode="git", writer="a") as world:
        placement, ref = _ports(world, tmp_path)
        e0 = world.hq.placement().epoch
        _seed_ref(ref, "a", e0)
        a = world["a"]
        for key, value in (("bh.pairing.enabled", "true"), ("bh.reclaim.failover.mode", "apply")):
            a.bd("config", "set", key, value).check()
        rewound = _claim(a, "unbacked", "claim-frame:a")
        resumable = _claim(a, "backed", "claim-frame:a")
        submitted = _claim(a, "submitted", "claim-frame:a", "review:pending")
        live = _claim(a, "live elsewhere", "claim-frame:c")  # a surviving executor's claim
        legacy = _claim(a, "unattributed")
        a.push().check()
        work_sha = _push_backup(work, resumable, "a", with_work=True)
        empty_sha = _push_backup(work, rewound, "a", with_work=False)  # tip already in base
        _push_backup(work, submitted, "a", with_work=True)
        before_live = _remote_issue(world, live)

        # ---- frame a dies; b fails over (placement names a, not released) ----------------
        a.kill()
        data = ReclaimFrameData(world["b"])
        hook = fr.FailoverReclaim(data, fr.GitBackupProbe(hive_clone, default_remote=str(bare)))
        out = wa.coexistence_adopt(data, placement, ref, prefix="fx", frame="b", reclaim=hook)

        assert out.step2.landed and out.epoch == e0 + 1
        plan = out.step2.reclaim
        outcomes = {r.bead: r.outcome for r in plan.rows}
        assert outcomes == {
            rewound: fr.Outcome.REWOUND,
            resumable: fr.Outcome.RESUMABLE,
            submitted: fr.Outcome.SUBMITTED,
            live: fr.Outcome.OTHER_FRAME,
            legacy: fr.Outcome.UNATTRIBUTED,
        }
        # ONE adopt commit carries the bump and the reclaim.
        assert _adopt_commits(world) == [f"{wa.ADOPT_COMMIT_PREFIX}b@{e0 + 1}"]

        # Unbacked: lifecycle fields identical to never-claimed; empty backup ref deleted.
        assert _remote_issue(world, rewound) == {
            "status": "open",
            "assignee": "",
            "started_at": None,
        }
        assert _remote_labels(world, rewound) == []
        refs = _remote_refs(bare)
        assert work_backup.backup_ref(rewound, "a") not in refs
        assert empty_sha  # it pointed at base
        # Backed up: open, resumable, the work still reachable on the remote.
        assert _remote_issue(world, resumable)["status"] == "open"
        assert _remote_labels(world, resumable) == ["recovery:resumable"]
        assert refs[work_backup.backup_ref(resumable, "a")] == work_sha
        # Untouched: submitted, the surviving executor's live claim, the unattributed one.
        assert _remote_issue(world, submitted)["status"] == "in_progress"
        assert _remote_issue(world, live) == before_live
        assert _remote_labels(world, live) == ["claim-frame:c"]
        assert _remote_issue(world, legacy)["status"] == "in_progress"
        # Audit: who, when, why on each reclaimed bead.
        for bead in (rewound, resumable):
            [c] = _remote_comments(world, bead)
            assert c["author"] == "ops/adopt@b" and "dead frame a" in c["text"]
            assert plan.at in c["text"]

        # ---- re-running the adopt reclaims nothing twice ------------------------------
        again = wa.coexistence_adopt(data, placement, ref, prefix="fx", frame="b", reclaim=hook)
        assert again.resumed and again.step2.reclaim is None
        assert _adopt_commits(world) == [f"{wa.ADOPT_COMMIT_PREFIX}b@{e0 + 1}"]
        assert len(_remote_comments(world, rewound)) == 1


def test_a_planned_handoff_reclaims_nothing_on_real_dolt(tmp_path):
    bare, _work, hive_clone = _backup_remote(tmp_path)
    with cf.composed_world(tmp_path, FRAMES, hq_mode="git", writer="a") as world:
        placement, ref = _ports(world, tmp_path)
        a = world["a"]
        for key, value in (("bh.pairing.enabled", "true"), ("bh.reclaim.failover.mode", "apply")):
            a.bd("config", "set", key, value).check()
        bead = _claim(a, "held", "claim-frame:a")
        a.push().check()
        data = ReclaimFrameData(world["b"])
        hook = fr.FailoverReclaim(data, fr.GitBackupProbe(hive_clone, default_remote=str(bare)))

        out = wa.coexistence_adopt(
            data, placement, ref, prefix="fx", frame="b", reclaim=hook, failover=False
        )

        assert out.step2.landed and "planned handoff" in out.step2.reclaim.skipped
        assert _remote_issue(world, bead)["status"] == "in_progress"
        assert _remote_comments(world, bead) == []
