"""M10 (bh-7p7rf): the composed fence integration suite, every scenario ending in check_invariants.

``bh-jbb6r``'s composed run (:mod:`harness.composed_fence`) proved the writer-partitioning design
on a PROTOTYPE fence. This suite runs the same shape on the PRODUCT code of molecule
``bh-16347``: the hive is cut over by :func:`beadhive.fence_cutover.cutover` (M5) on the bh-eybn7
fixture (:mod:`harness.writer_fencing`: real bd embedded and server frames, Dolt CLI frames and one
``git+file://`` remote), so the fence and guard are M1's; adopt is M2's step 2
(:func:`beadhive.writer_adopt.run_step2`); the managed write path's divert and the writer's
orphan merge are M6's (:mod:`beadhive.fence_orphan`); failover reclaim is M3's
(:class:`beadhive.failover_reclaim.FailoverReclaim`) over M14's backup refs; forwarding is M12's
(:mod:`beadhive.hive_forward`); session liveness is M9's (:mod:`beadhive.hq_sql_session`) and the
director's observer is M8's (:mod:`beadhive.failover_observer`).

Every scenario ends in :func:`harness.composed_fence.check_invariants` judged on remote ``main``
(no stale marks, no epoch regression, one writer per epoch, no late epoch commit, no lost
acknowledged or forwarded write, no lost backed-up work, no surviving unbacked claim of a dead
frame) with the product ``fence_audit`` history check on:

* ``test_fixed_seed_product_interleavings_*`` — fixed seeds of a randomized schedule over the
  product verbs, always including the Φ2 mixed-version events (an unprovisioned replica, a
  legacy 0.22.x adopt against the cut-over hive), each shown to fail closed (T3, T5).
* ``test_multi_executor_forwarding_survives_primary_failover_*`` — two executors forward claim
  races to one primary, the primary dies, the director places the standby, whose adopt reclaims
  the dead primary's claims by backup (M14 D5a) and leaves the executors' live claims untouched;
  the executors re-point and keep claiming: 0 double wins.
* ``test_m13_*`` — option A's grant shape (bh-uhx2r, M13 amendment): excess grants are a
  conformance finding; table-scoped grants refuse E2's stale-write steps and E3's
  ``bh_local_ident`` rewrite in Dolt; an in-flight forwarded write is refused across the
  demoted primary's divert reset (E5), never acknowledged and dropped; an injected re-stamped
  late write is reported by ``fence_audit``'s history check.
* ``test_sender_stall_*`` — the Φ3 soak check for O4: a session sender stalls longer than the
  session TTL (see ``docs/HQ.md`` "Soak check: sender stall").

Private scratch Dolt/git only: every server, data dir, ``HOME`` and ``DOLT_ROOT_PATH`` is under
``tmp_path``. ``BH_M10_SEEDS=0,1,…`` widens the seeded run (the soak runs it outside the gate).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pymysql
import pytest

from beadhive import failover_reclaim as fr
from beadhive import fence_cutover as fc
from beadhive import fence_data as fd
from beadhive import fence_orphan as fo
from beadhive import fence_schema as fs
from beadhive import hive_forward, work_backup
from beadhive import writer_adopt as wa
from beadhive.failover_observer import FailoverObserver, session_table, sql_session_staleness
from beadhive.hq_sql_session import LivenessPolicy, SessionRenewer
from harness import composed_fence as cf
from harness.writer_fencing import BD_EMBEDDED, BD_SERVER, DOLT_CLI, Cluster, Run
from test_failover_reclaim_int import (
    _backup_remote,
    _claim,
    _push_backup,
    _remote_comments,
    _remote_issue,
    _remote_labels,
)
from test_fence_cutover_int import _HQ, _legacy_state, _node, _ref_port, _set_ref
from test_hive_forward_int import _bd, _client_env, _git_init, _write
from test_hq_sql_session_int import DB as SESSION_DB
from test_hq_sql_session_int import _eligibility as _session_eligibility
from test_hq_sql_session_int import _frame as _session_frame
from test_hq_sql_session_int import _provisioned as _session_provisioned
from test_hq_sql_session_int import _server as _session_server

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("bd") is None or shutil.which("dolt") is None or shutil.which("git") is None,
        reason="bd/dolt/git not installed",
    ),
]

DB = "fx"  # the fixture cluster's prefix == bd's server database
PW = "fixture-only-forwarder"
#: The fixed seeds the gate runs; the soak widens them with BH_M10_SEEDS.
FIXED_SEEDS = (0, 1, 2)
SEEDS = tuple(int(s) for s in os.environ.get("BH_M10_SEEDS", "").split(",") if s) or FIXED_SEEDS


# =============================================================================================
# The product world: a cut-over hive on the bh-eybn7 fixture
# =============================================================================================


@dataclass
class ProductWorld(cf.World):
    """:class:`harness.composed_fence.World` plus what the product invariants are judged by."""

    acknowledged: list[str] = field(default_factory=list)
    forwarded: list[str] = field(default_factory=list)
    backups: dict[str, tuple[str, str]] = field(default_factory=dict)
    backup_remote: Path | None = None
    dead: list[str] = field(default_factory=list)

    def node(self, name: str) -> fd.FenceNode:
        return _node(self[name])

    def writer(self) -> str:
        """The frame remote ``main`` names in ``bh_writer``."""
        rows = self.cluster.remote.view(("bh_writer",))["tables"]["bh_writer"]
        return rows[0]["frame"]


@contextlib.contextmanager
def product_world(root: Path, frames, *, holder: str):
    """``holder`` runs the PRODUCT cutover from the legacy state (both legacy carriers at
    ``LIVE_EPOCH``); every other frame pulls it and provisions its identity through the same
    verb (C6), as a current-version replica does."""
    with Cluster(root / "cluster", frames) as cluster:
        _legacy_state(cluster, holder)
        h = cluster[holder]
        h.push().check()
        out = fc.cutover(
            _node(h),
            _HQ(cluster),
            _ref_port(h),
            prefix=cluster.prefix,
            host_id=holder,
            others_published=True,
        )
        assert out.audit.ok and out.guard.count == fs.TRIGGER_COUNT, out
        for name, frame in cluster.frames.items():
            if name == holder:
                continue
            frame.pull().check()
            replica = fc.cutover(
                _node(frame),
                _HQ(cluster),
                _ref_port(frame),
                prefix=cluster.prefix,
                host_id=name,
                others_published=False,
            )
            assert replica.replica and replica.provisioned, replica
        yield ProductWorld(cluster, cluster.hq, cf.MoveLog(cluster))


def _take(world: ProductWorld, name: str, *, reclaim=None) -> wa.Step2Result:
    """The director places ``name``; ``name`` runs the product adopt step 2 to completion."""
    placed = world.hq.place(name)
    result = wa.run_step2(
        world.node(name), _HQ(world.cluster), frame=name, epoch=placed.epoch, reclaim=reclaim
    )
    world.moves.observe("adopt", name)
    assert result.landed, result
    return result


def _managed_push(world: ProductWorld, name: str) -> tuple[str, fo.DivertOutcome | None]:
    """The cut-over managed path (``Engine.push_state``): commit the working set, divert when
    superseded (M6), else push ``main``. Returns ``("pushed"|"rejected"|"diverted", divert)``."""
    node = world.node(name)
    node.engine.commit("bh: commit working set before managed push")
    out = fo.divert_if_superseded(node, frame=name)
    if out.superseded:
        world.moves.observe("divert", name)
        return "diverted", out
    ok = node.push()
    world.moves.observe("managed_push", name)
    return ("pushed" if ok else "rejected"), out


def _clean(world: ProductWorld, **kw) -> dict:
    """Every scenario ends here: all invariants on remote ``main``, product audit included."""
    result = cf.check_invariants(
        world,
        acknowledged=world.acknowledged,
        forwarded=world.forwarded,
        backups=world.backups,
        backup_remote=world.backup_remote,
        dead_frames=world.dead,
        history_check=True,
        **kw,
    )
    assert result["ok"], json.dumps(result["violations"], indent=1, default=str)
    return result


def _titles(world: ProductWorld) -> set[str]:
    view = world.cluster.remote.view(("issues",))["tables"]["issues"] or []
    return {r["title"] for r in view}


def _try_write(frame, title: str) -> Run:
    """One tracker write that may be refused: ``bd create`` on a bd frame, an INSERT plus a
    commit on a Dolt CLI frame (:meth:`Frame.create_issue` raises on the CLI refusal)."""
    if frame.is_bd:
        return frame.create_issue(title)
    ident = f"{frame.cluster.prefix}-{frame.name}-{hashlib.sha1(title.encode()).hexdigest()[:6]}"
    run = frame.sql(
        "insert into issues (id, title, description, design, acceptance_criteria, notes) "
        f"values ('{ident}', '{title}', '', '', '', '')"
    )
    return run if not run.ok else frame.commit(f"cli: {title}")


def _refused(run: Run | object, *needles: str) -> str:
    out = f"{getattr(run, 'stdout', '') or ''}{getattr(run, 'stderr', '') or ''}"
    assert getattr(run, "returncode", 1) != 0, f"accepted: {out[-400:]}"
    assert any(n in out for n in needles), out[-800:]
    return out


# =============================================================================================
# Fixed-seed product interleavings, Φ2 mixed-version events included
# =============================================================================================


@dataclass
class _Schedule:
    seed: int
    events: list[dict] = field(default_factory=list)


class _ProductDriver:
    """A seeded schedule over the product verbs. Every event asserts its own fail-closed
    outcome; :meth:`recover` then rejoins every frame and merges every orphan."""

    #: Drawn at random; the two Φ2 events are added to EVERY schedule once.
    RANDOM = ("write", "write", "push", "push", "adopt", "stale_push", "orphan_merge")
    MIXED_VERSION = ("legacy_adopt", "unprovisioned")

    def __init__(self, world: ProductWorld, seed: int, length: int = 7):
        self.world = world
        self.rng = random.Random(seed)
        self.schedule = _Schedule(seed)
        self.length = length
        self.pending: dict[str, list[str]] = {name: [] for name in world.cluster.frames}
        self.orphans: dict[str, list[str]] = {}
        self.n = 0

    # ---- helpers -----------------------------------------------------------------------
    def names(self) -> list[str]:
        return sorted(self.world.cluster.frames)

    def record(self, event: str, **info) -> None:
        self.schedule.events.append({"event": event, **info})

    def local_writer(self, name: str) -> str | None:
        row = self.world.node(name).writer()
        return None if row is None else row.frame

    def rejoin(self, name: str) -> None:
        """The managed rejoin: a superseded frame's unpublished writes go to an orphan."""
        node = self.world.node(name)
        node.engine.commit("bh: commit working set before managed rejoin")
        out = fo.divert_if_superseded(node, frame=name)
        if out.superseded:
            self.world.moves.observe("divert", name)
            self._diverted(name, out)

    def _diverted(self, name: str, out: fo.DivertOutcome) -> None:
        if out.branch:
            self.orphans[out.branch] = self.pending[name]
        else:
            assert not self.pending[name], (name, self.pending[name])
        self.pending[name] = []

    # ---- events ------------------------------------------------------------------------
    def ev_write(self) -> None:
        name = self.rng.choice(self.names())
        self.n += 1
        title = f"s{self.schedule.seed}-{name}-{self.n}"
        expected = self.local_writer(name) == name
        run = _try_write(self.world[name], title)
        self.record("write", frame=name, title=title, ok=run.ok, expected=expected)
        if expected:
            assert run.ok, run.output[-400:]
            self.pending[name].append(title)
        else:  # the guard refuses a non-writer on main
            _refused(run, fs.GUARD_REFUSAL)

    def ev_push(self) -> None:
        pool = [n for n in self.names() if self.pending[n]] or [self.world.writer()]
        name = self.rng.choice(pool)
        outcome, divert = _managed_push(self.world, name)
        self.record("push", frame=name, outcome=outcome, branch=divert and divert.branch)
        if outcome == "pushed":
            self.world.acknowledged.extend(self.pending[name])
            self.pending[name] = []
        elif outcome == "diverted":
            self._diverted(name, divert)
        else:  # only a frame with nothing of its own lags a remote it cannot reach past
            assert not self.pending[name], (name, outcome)

    def ev_adopt(self) -> None:
        writer = self.world.writer()
        name = self.rng.choice([n for n in self.names() if n != writer])
        self.rejoin(name)
        result = _take(self.world, name)
        self.record("adopt", frame=name, epoch=result.epoch)

    def ev_stale_push(self) -> None:
        """A superseded frame with unpublished writes pushes RAW (no managed path): the remote
        refuses the non-fast-forward, so nothing stale reaches ``main``."""
        writer = self.world.writer()
        stale = [n for n in self.names() if self.pending[n] and n != writer]
        if not stale:
            self.ev_write()  # nothing stale to push yet: a write instead
            return
        name = self.rng.choice(stale)
        run = self.world[name].push()
        self.world.moves.observe("raw_push", name)
        self.record("stale_push", frame=name, ok=run.ok)
        assert not run.ok, run.output[-400:]

    def ev_orphan_merge(self) -> None:
        if not self.orphans:
            self.ev_push()  # nothing diverted yet: a managed push (which may divert) instead
            return
        branch = self.rng.choice(sorted(self.orphans))
        self._merge(branch)

    def _merge(self, branch: str) -> None:
        writer = self.world.writer()
        self.rejoin(writer)
        merged = fo.merge_orphan(self.world.node(writer), branch, frame=writer)
        outcome, _ = _managed_push(self.world, writer)
        self.record("orphan_merge", frame=writer, branch=branch, outcome=outcome)
        assert outcome == "pushed" and not merged.already, (outcome, merged)
        self.world.acknowledged.extend(self.orphans.pop(branch) + self.pending[writer])
        self.pending[writer] = []

    def ev_legacy_adopt(self) -> None:
        """Φ2 / T5: an OLDER bh adopts against the cut-over hive. It moves HQ placement and
        ``refs/bh/epoch`` but never bumps ``bh_writer``: the adopter's writes stay refused
        (fail closed), status reports the half-state, and the product step 2 rolls it forward
        at the placed epoch."""
        writer = self.world.writer()
        name = self.rng.choice([n for n in self.names() if n != writer])
        self.rejoin(name)
        placed = self.world.hq.place(name)  # the old adopt: placement first ...
        _set_ref(self.world.cluster, {"epoch": placed.epoch, "host_id": name, "seq": 0})  # ref
        self.n += 1
        title = f"s{self.schedule.seed}-legacy-{name}-{self.n}"
        run = _try_write(self.world[name], title)
        _refused(run, fs.GUARD_REFUSAL)
        node = self.world.node(name)
        status = fc.status(
            node,
            prefix=self.world.cluster.prefix,
            placement=_HQ(self.world.cluster).read(),
            ref=_ref_port(self.world[name]),
        )
        assert any(f.startswith("placement_ahead") for f in status.findings()), status.findings()
        ahead = cf.check_invariants(self.world)
        assert not ahead["ok"] and "audit" in ahead["violations"], ahead["violations"]
        rolled = wa.run_step2(node, _HQ(self.world.cluster), frame=name, epoch=placed.epoch)
        self.world.moves.observe("roll_forward", name)
        assert rolled.landed, rolled
        self.record("legacy_adopt", frame=name, epoch=placed.epoch, refused_write=True)

    def ev_unprovisioned(self) -> None:
        """Φ2 / T3: a replica that pulled the cutover but runs a bh that never provisions its
        ``bh_local_ident`` (0.22.x) is refused on ``main`` — even when its data names it."""
        name = self.rng.choice(self.names())
        node = self.world.node(name)
        node.engine.execute(fs.drop_ident_statements())
        assert node.ident() is None
        self.n += 1
        title = f"s{self.schedule.seed}-unprovisioned-{name}-{self.n}"
        run = _try_write(self.world[name], title)
        _refused(run, fs.LOCAL_IDENT_TABLE, fs.GUARD_REFUSAL)
        node.provision_ident(name)  # upgrading bh provisions it (C6)
        self.record("unprovisioned", frame=name, refused_write=True)

    # ---- run ---------------------------------------------------------------------------
    def run(self) -> _Schedule:
        events = [self.rng.choice(self.RANDOM) for _ in range(self.length)]
        events += list(self.MIXED_VERSION)
        self.rng.shuffle(events)
        for event in events:
            getattr(self, f"ev_{event}")()
        self.recover()
        return self.schedule

    def recover(self) -> None:
        """Every frame rejoins (divert if superseded), the writer publishes, every orphan is
        merged by the writer: nothing written and acknowledged on any frame is left behind."""
        for name in self.names():
            self.rejoin(name)
        writer = self.world.writer()
        outcome, _ = _managed_push(self.world, writer)
        assert outcome == "pushed", outcome
        self.world.acknowledged.extend(self.pending[writer])
        self.pending[writer] = []
        for branch in sorted(self.orphans):
            self._merge(branch)
        assert not any(self.pending.values()), self.pending
        self.record("recover", writer=writer)


@pytest.mark.parametrize("seed", SEEDS)
def test_fixed_seed_product_interleavings_hold_every_invariant(tmp_path, seed):
    frames = [("a", BD_EMBEDDED), ("b", BD_EMBEDDED), ("c", DOLT_CLI)]
    with product_world(tmp_path, frames, holder="a") as world:
        schedule = _ProductDriver(world, seed).run()
        kinds = {e["event"] for e in schedule.events}
        assert set(_ProductDriver.MIXED_VERSION) <= kinds, schedule.events
        result = _clean(world)
        print("BH_M10", json.dumps({"seed": seed, "events": schedule.events}, default=str))
        assert result["product_audit"]["cut_over"]


# =============================================================================================
# Forwarding helpers (M12) on a fixture bd-server frame's sql-server
# =============================================================================================


def _root(frame):
    return pymysql.connect(
        host="127.0.0.1", port=frame.port, user="root", database=DB, autocommit=True
    )


def _login(frame, user: str, *, autocommit: bool = True):
    return pymysql.connect(
        host="127.0.0.1",
        port=frame.port,
        user=user,
        password=PW,
        database=DB,
        autocommit=autocommit,
        read_timeout=60,
    )


def _endpoint(frame, user: str) -> dict:
    return {
        "host": "127.0.0.1",
        "port": frame.port,
        "database": DB,
        "user": user,
        "tls_mode": "disabled",
    }


def _provision(frame, *names: str) -> hive_forward.DbapiSql:
    admin = hive_forward.DbapiSql(_root(frame))
    for name in names:
        account = hive_forward.Account(name, "127.0.0.1")
        hive_forward.provision(admin, database=DB, account=account, password=PW, require_tls=False)
    assert hive_forward.conformance(admin, database=DB, require_tls=False) == []
    return admin


def _open_sql(ep):
    return hive_forward.DbapiSql(
        pymysql.connect(
            host=ep["host"],
            port=int(ep["port"]),
            user=ep["user"],
            password=PW,
            database=ep["database"],
            autocommit=True,
        )
    )


class Executor:
    """An executor frame's forwarded checkout: bh's bd goes to the placed primary (M12)."""

    def __init__(self, base: Path, name: str, primaries: dict):
        self.name = name
        self.ws = base / name
        self.env_dir = base / f"{name}-env"
        _git_init(self.ws, _client_env(self.env_dir))
        (self.ws / ".beads").mkdir()
        (self.ws / ".beads").chmod(0o700)
        meta = {"database": "dolt", "backend": "dolt", "dolt_mode": "server", "dolt_database": DB}
        _write(self.ws / ".beads" / "metadata.json", json.dumps(meta))
        self.endpoints = {frame: _endpoint(p, name) for frame, p in primaries.items()}

    def repoint(self, placed: tuple[str, int]) -> hive_forward.Marker:
        return hive_forward.repoint(
            self.ws,
            placed,
            prefix=DB,
            self_frame=self.name,
            endpoints=self.endpoints,
            open_sql=_open_sql,
        )

    def bd(self, *args: str, actor: str | None = None):
        env = hive_forward.bd_env(
            self.ws, base=_client_env(self.env_dir, BEADS_ACTOR=actor or f"{self.name}/w0")
        )
        assert env is not None
        return _bd(self.ws, env, *args)

    def create(self, world: ProductWorld, title: str) -> str:
        made = self.bd("create", "--title", title, "-t", "task", "--json")
        assert made.returncode == 0, made.stderr
        world.forwarded.append(title)  # acknowledged to the executor
        return json.loads(made.stdout)["id"]


def _race(executors: list[Executor], bead: str, per: int = 2) -> dict[str, int]:
    """Every executor runs ``per`` concurrent ``bd update --claim`` on ``bead``."""
    gate = threading.Barrier(len(executors) * per)
    codes: dict[str, int] = {}

    def claim(ex: Executor, actor: str) -> None:
        gate.wait()
        codes[actor] = ex.bd("update", bead, "--claim", actor=actor).returncode

    threads = [
        threading.Thread(target=claim, args=(ex, f"{ex.name}/r{i}"))
        for ex in executors
        for i in range(per)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return codes


def _assignee(admin: hive_forward.DbapiSql, bead: str) -> tuple[str, str]:
    row = admin.query(f"SELECT status, assignee FROM issues WHERE id = {fs.quote(bead)}")[0]
    return str(row["status"]), str(row.get("assignee") or "")


# =============================================================================================
# Multi-executor forwarding with primary failover; backup-driven reclaim (M14 D5a)
# =============================================================================================


@pytest.mark.dolt_server
def test_multi_executor_forwarding_survives_primary_failover_with_backup_reclaim(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(hive_forward.PASSWORD_ENV, PW)
    bare, work, hive_clone = _backup_remote(tmp_path)
    frames = [("p", BD_SERVER), ("q", BD_SERVER)]
    with product_world(tmp_path, frames, holder="p") as world:
        world.backup_remote = bare
        p, q = world["p"], world["q"]
        for key, value in (("bh.pairing.enabled", "true"), ("bh.reclaim.failover.mode", "apply")):
            p.bd("config", "set", key, value).check()
        admin_p = _provision(p, "e1", "e2", "e3")
        admin_q = _provision(q, "e1", "e2", "e3")
        e0 = world.hq.placement().epoch
        executors = [Executor(tmp_path / "exec", n, {"p": p, "q": q}) for n in ("e1", "e2")]
        for ex in executors:
            marker = ex.repoint(("p", e0))
            assert marker is not None and marker.state == "forwarding", marker

        # ---- executors race claims through the primary; the winner labels its claim ------
        live: dict[str, str] = {}  # bead -> the winning actor (an executor's live claim)
        for i in range(3):
            bead = executors[i % 2].create(world, f"m10-race-{i}")
            codes = _race(executors, bead)
            winners = [a for a, rc in codes.items() if rc == 0]
            assert len(winners) == 1, codes
            assert _assignee(admin_p, bead) == ("in_progress", winners[0])
            owner = next(ex for ex in executors if winners[0].startswith(f"{ex.name}/"))
            assert owner.bd("label", "add", bead, f"claim-frame:{owner.name}").returncode == 0
            live[bead] = winners[0]

        # ---- the primary's own claims: backed up, unbacked, submitted (M14 pairing) ------
        backed = _claim(p, "m10-backed", "claim-frame:p")
        unbacked = _claim(p, "m10-unbacked", "claim-frame:p")
        submitted = _claim(p, "m10-submitted", "claim-frame:p", "review:pending")
        assert _managed_push(world, "p")[0] == "pushed"
        world.backups[backed] = (
            work_backup.backup_ref(backed, "p"),
            _push_backup(work, backed, "p", with_work=True),
        )
        _push_backup(work, unbacked, "p", with_work=False)  # tip already in base: no work
        _push_backup(work, submitted, "p", with_work=True)
        # acknowledged to an executor after the last push: it rides p's working set
        executors[0].create(world, "m10-after-last-push")
        before = {b: (_remote_issue(world, b), _remote_labels(world, b)) for b in live}

        # ---- the primary dies; the director places q; q's adopt reclaims by backup -------
        p.kill()
        world.dead.append("p")
        hook = fr.FailoverReclaim(
            world.node("q"), fr.GitBackupProbe(hive_clone, default_remote=str(bare))
        )
        result = _take(world, "q", reclaim=hook)
        outcomes = {r.bead: r.outcome for r in result.reclaim.rows}
        assert outcomes == {
            backed: fr.Outcome.RESUMABLE,
            unbacked: fr.Outcome.REWOUND,
            submitted: fr.Outcome.SUBMITTED,
            **{b: fr.Outcome.OTHER_FRAME for b in live},
        }, outcomes
        assert _remote_issue(world, unbacked) == {
            "status": "open",
            "assignee": "",
            "started_at": None,
        }
        assert _remote_labels(world, backed) == ["recovery:resumable"]
        assert _remote_issue(world, submitted)["status"] == "in_progress"
        # The surviving executors' live claims are untouched by the reclaim.
        assert {b: (_remote_issue(world, b), _remote_labels(world, b)) for b in live} == before
        for bead in (backed, unbacked):
            [comment] = _remote_comments(world, bead)
            assert "dead frame p" in comment["text"]

        # ---- the executors re-point to q and keep claiming: 0 double wins ----------------
        e1 = result.epoch
        for ex in executors:
            marker = ex.repoint(("q", e1))
            assert marker.state == "forwarding" and marker.frame == "q", marker
        for bead, winner in list(live.items())[:1]:  # a held claim is never won by another
            codes = _race(executors, bead)
            # bd's claim is idempotent for its holder; every other racer is refused
            assert {a for a, rc in codes.items() if rc == 0} <= {winner}, (winner, codes)
            assert _assignee(admin_q, bead) == ("in_progress", winner)
        for i in range(2):
            bead = executors[i].create(world, f"m10-after-failover-{i}")
            codes = _race(executors, bead)
            winners = [a for a, rc in codes.items() if rc == 0]
            assert len(winners) == 1, codes
            assert _assignee(admin_q, bead) == ("in_progress", winners[0])
        resumed = _race(executors, backed)  # the resumable bead is claimable again, once
        assert len([a for a, rc in resumed.items() if rc == 0]) == 1, resumed

        # ---- the dead primary returns: its managed rejoin diverts, it fails closed -------
        p.restart()
        stale = Executor(tmp_path / "exec", "e3", {"p": p, "q": q})
        hive_forward.point(
            stale.ws,
            hive_forward.ForwardTarget("p", e0, stale.endpoints["p"]),
            prefix=DB,
            self_frame="e3",
            endpoints=stale.endpoints,
        )  # a stale cache that still names p
        outcome, divert = _managed_push(world, "p")
        assert outcome == "diverted" and divert.branch, divert
        refused = stale.repoint(("p", e0))
        assert refused.state == "refused" and "demoted primary" in refused.reason, refused
        hive_forward.point(
            stale.ws,
            hive_forward.ForwardTarget("p", e0, stale.endpoints["p"]),
            prefix=DB,
            self_frame="e3",
            endpoints=stale.endpoints,
        )  # bypass the preflight: p's own guard refuses
        _refused(stale.bd("create", "--title", "m10-to-demoted", "-t", "task"), fs.GUARD_REFUSAL)

        # ---- q merges p's orphan: the late forwarded write is not lost --------------------
        merged = fo.merge_orphan(world.node("q"), divert.branch, frame="q")
        assert not merged.already
        assert _managed_push(world, "q")[0] == "pushed"
        assert {"m10-after-last-push", "m10-after-failover-0"} <= _titles(world)
        admin_p.connection.close()
        admin_q.connection.close()
        _clean(world)


# =============================================================================================
# M13 amendment: option A's grant shape, E5 quiesce, and the history check
# =============================================================================================


def _try(conn, statement: str) -> str:
    try:
        with conn.cursor() as cur:
            cur.execute(statement)
            cur.fetchall()
        return "ok"
    except pymysql.MySQLError as exc:
        return "refused: " + str(exc.args[-1]).splitlines()[0][:110]


#: bh-uhx2r E2's stale-write steps (commit, merge the bump into the demoted primary, re-stamp the
#: stale mark, clear the violation) plus E4's ``DOLT_RESET --hard`` (discarding the primary's
#: unpublished work), and E3's identity spoof.
E2_STEPS = (
    "CALL DOLT_COMMIT('-Am', 'commit working set')",
    "CALL DOLT_MERGE('origin/main')",
    "UPDATE bh_write_mark SET epoch = epoch + 1",
    "DELETE FROM dolt_constraint_violations_bh_write_mark",
    "CALL DOLT_RESET('--hard')",
)
E3_SPOOF = "UPDATE bh_local_ident SET frame = 'q'"


@pytest.mark.dolt_server
def test_m13_grant_shape_quiesce_and_history_check(tmp_path, monkeypatch):
    monkeypatch.setenv(hive_forward.PASSWORD_ENV, PW)
    frames = [("q", BD_EMBEDDED), ("p", BD_SERVER)]  # an embedded frame founds the fixture
    with product_world(tmp_path, frames, holder="p") as world:
        p = world["p"]
        admin = hive_forward.DbapiSql(_root(p))

        # ---- excess (database-wide) grants are a conformance finding --------------------
        wide = hive_forward.Account("fwd-wide", "127.0.0.1")
        admin.execute(
            [
                f"CREATE USER {wide.sql} IDENTIFIED BY '{PW}'",
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON {DB}.* TO {wide.sql}",
            ]
        )
        findings = hive_forward.conformance(admin, database=DB, require_tls=False)
        assert any("fwd-wide" in f and "database-wide" in f for f in findings), findings
        spoofer = _login(p, "fwd-wide")
        assert _try(spoofer, "UPDATE bh_local_ident SET frame = frame") == "ok"  # E3 reachable
        spoofer.close()
        narrowed = hive_forward.provision(
            admin, database=DB, account=wide, password=PW, require_tls=False
        )
        assert (
            narrowed.revoked
            and hive_forward.conformance(admin, database=DB, require_tls=False) == []
        )
        _provision(p, "fwd-n")

        ex = Executor(tmp_path / "exec", "fwd-n", {"p": p})
        e0 = world.hq.placement().epoch
        assert ex.repoint(("p", e0)).state == "forwarding"
        ex.create(world, "m13-landed")
        assert _managed_push(world, "p")[0] == "pushed"
        ex.create(world, "m13-unpublished")  # acknowledged; rides p's working set

        # ---- E5: a forwarded transaction in flight when p is demoted ---------------------
        txn = _login(p, "fwd-n", autocommit=False)
        with txn.cursor() as cur:
            cur.execute("START TRANSACTION")
            cur.execute(
                "INSERT INTO issues (id, title, description, design, acceptance_criteria, notes)"
                f" VALUES ('{DB}-inflight', 'm13-inflight', '', '', '', '')"
            )
        _take(world, "q")

        # ---- E2 / E3 with table-scoped grants: Dolt refuses every step -------------------
        p.bd("sql", "CALL DOLT_FETCH('origin')").check()  # the primary's routine fetch
        attacker = _login(p, "fwd-n")
        probes = {s: _try(attacker, s) for s in (*E2_STEPS, E3_SPOOF)}
        attacker.close()
        assert all(v.startswith("refused") for v in probes.values()), probes

        # ---- p's managed rejoin: divert, quiesce (kill fwd-n), reset ---------------------
        outcome, divert = _managed_push(world, "p")
        assert outcome == "diverted" and divert.branch, divert
        with pytest.raises(pymysql.MySQLError):
            with txn.cursor() as cur:
                cur.execute("COMMIT")
        with contextlib.suppress(Exception):
            txn.close()
        assert not p.query("SELECT id FROM issues WHERE title = 'm13-inflight'")
        # after the reset the spoof is still refused, and so is a forwarded write
        attacker = _login(p, "fwd-n")
        assert _try(attacker, E3_SPOOF).startswith("refused")
        attacker.close()
        _refused(ex.bd("create", "--title", "m13-after-demotion", "-t", "task"), fs.GUARD_REFUSAL)
        assert ex.repoint(("p", e0)).state == "refused"

        merged = fo.merge_orphan(world.node("q"), divert.branch, frame="q")
        assert not merged.already
        assert _managed_push(world, "q")[0] == "pushed"
        assert "m13-inflight" not in _titles(world)
        admin.connection.close()
        _clean(world)

        # ---- an injected late-epoch, re-stamped write: only the history check sees it ----
        _take(world, "p")
        p.create_issue("m13-late").check()
        p.commit("late write at the retired epoch")
        _take(world, "q")  # p is superseded, holding an unpublished write
        root = _root(p)
        for statement in (
            "CALL DOLT_FETCH('origin')",
            "SET @@dolt_force_transaction_commit = 1",
            "CALL DOLT_MERGE('origin/main')",
            "SET @live = (SELECT epoch FROM bh_epoch_live WHERE id = 1)",
            "UPDATE bh_write_mark SET epoch = @live WHERE epoch <> @live",
            "DELETE FROM dolt_constraint_violations_bh_write_mark",
            "CALL DOLT_COMMIT('-Am', 'routine')",
        ):
            assert _try(root, statement) == "ok", statement
        root.close()
        assert p.push().ok  # a fast-forward: nothing in the push path can refuse it
        world.moves.observe("forced", "p")
        result = cf.check_invariants(world, history_check=True)
        flagged = sorted(result["violations"])
        assert not result["ok"] and "history" in flagged, result["violations"]
        assert "audit" not in flagged  # stale_marks / regression / placement: all clean
        assert result["product_audit"]["stale_marks"] == 0
        late = result["product_audit"]["late_writes"]
        assert any("late write" in str(w) for w in late), late
        print("BH_M10", json.dumps({"m13_probes": probes, "late_writes": late}, default=str))


# =============================================================================================
# Φ3 soak check (O4): the session sender stalls longer than the session TTL
# =============================================================================================


@pytest.mark.dolt_server
def test_sender_stall_longer_than_the_ttl_is_stale_but_fails_over_only_past_failover_after(
    tmp_path,
):
    """M9's renewal sender stalls (a host under load, a hung timer) longer than
    ``session_ttl_s``: eligibility goes stale at once, but the director's observer (M8) only
    fails the frame over once the stall outlasts ``failover_after``; a stall that ends before
    then is a fresh session again and never a failover. Time decides WHEN to reassign, never
    who may write (the hive's data does that). ``BH_M10_SESSION_TTL_S`` scales the run for the
    Φ3 soak (docs/HQ.md "Soak check: sender stall")."""
    ttl = int(os.environ.get("BH_M10_SESSION_TTL_S", "2"))
    failover_after = 4.0 * ttl
    with _session_server(tmp_path) as port:
        root = _session_provisioned(
            port, frames=(("frame_a", 1),), policy=LivenessPolicy(session_ttl_s=ttl)
        )
        root.select_db(SESSION_DB)
        table = session_table("frame_a", 1)
        renewer = SessionRenewer(
            lambda: _session_frame(port, tmp_path, "frame_a"), database=SESSION_DB
        )
        reader = _session_frame(port, tmp_path, "frame_a")
        observer = FailoverObserver(failover_after)
        stalled, stop, errors = threading.Event(), threading.Event(), []

        def wait(wait_s: float) -> bool:
            stop.wait(wait_s)
            while stalled.is_set() and not stop.is_set():  # the sender is stuck
                time.sleep(0.05)
            return stop.is_set()

        # When each successful renewal STARTED (bh-eeyxt): its server stamp is no earlier, so
        # the session's server staleness never exceeds the monotonic time since then.
        renewal_starts: list[float] = []
        tick = renewer.tick

        def timed_tick():
            started = time.monotonic()
            route = tick()
            renewal_starts.append(started)
            return route

        renewer.tick = timed_tick

        sender = threading.Thread(
            target=renewer.run,
            kwargs={"interval_s": ttl / 4, "stop": wait, "on_error": errors.append},
            daemon=True,
        )

        def poll() -> tuple[bool, object]:
            with root.cursor() as cursor:
                age = sql_session_staleness(cursor, table)
            return _session_eligibility(reader).authenticated_fresh_heartbeat, observer.observe(age)

        def watch(seconds: float) -> list[tuple[bool, object]]:
            out, end = [], time.monotonic() + seconds
            while time.monotonic() < end:
                out.append(poll())
                time.sleep(0.25)
            return out

        renewer.tick()
        sender.start()
        try:
            healthy = watch(2 * ttl)
            assert all(fresh for fresh, _ in healthy) and not any(d.due for _, d in healthy)

            stalled.set()  # stall 1: longer than the TTL, shorter than failover_after
            short = watch(2.5 * ttl)
            stalled.clear()
            assert not short[-1][0], "eligibility must go stale past the session TTL"
            assert not any(d.due for _, d in short), "a stall shorter than failover_after"
            recovered = watch(1.5 * ttl)
            assert recovered[-1][0] and not any(d.due for _, d in recovered)

            stalled.set()  # stall 2: longer than failover_after
            began, due_after = time.monotonic(), None
            while time.monotonic() - began < failover_after + 6 * ttl:
                fresh, decision = poll()
                if decision.due:
                    due_at = time.monotonic()
                    due_after = due_at - began
                    since_renewal = due_at - renewal_starts[-1]
                    break
                time.sleep(0.25)
            stalled.clear()
            # Measured from the last renewal actually made, not from when the stall was
            # flagged: the sender's last tick may land up to one interval before `began`, and
            # a loaded host stretches it further (bh-eeyxt; 7.95 s was seen against 8.0 s).
            assert due_after is not None, "a stall past failover_after must fail over"
            assert since_renewal > failover_after, (since_renewal, failover_after)
            assert decision.staleness > failover_after and decision.window > failover_after
            assert not fresh
        finally:
            stop.set()
            stalled.clear()
            sender.join(15)
            reader.close()
            root.close()
        assert errors == []
        print(
            "BH_M10",
            json.dumps(
                {
                    "sender_stall": {
                        "session_ttl_s": ttl,
                        "failover_after_s": failover_after,
                        "due_after_stall_s": round(due_after, 2),
                        "renewals": renewer.renewals,
                    }
                }
            ),
        )
