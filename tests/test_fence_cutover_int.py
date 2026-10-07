"""The per-hive cutover, status and rollback (bh-oarxp) against real bd/Dolt frames.

The bh-32379 T1–T6 cases, now driven through the PRODUCT code (:mod:`beadhive.fence_cutover`
over :class:`beadhive.fence_data.FenceNode`) instead of the spike's ``_cutover`` stand-in, on the
bh-eybn7 fixture (:mod:`harness.writer_fencing`): a scratch ``git+file://`` hive remote, an HQ
placement stand-in, and the legacy ``refs/bh/epoch`` driven by the product
:class:`~beadhive.fence_cutover.GitLegacyRef` from the holder's own Git clone. The holder is a
bd frame in either mode (embedded: the Dolt CLI on ``.beads/embeddeddolt/<db>``; server: ``bd
sql`` + ``bd dolt commit``); the replica is a Dolt CLI frame.

* T1 — cutover seeds ``bh_writer`` at ``max(ref, placement)`` with no bump; in-flight claim
  tokens stay valid; ``refs/bh/epoch`` is reserved (``seq + 1``) and recorded; the identity never
  reaches the remote; a re-run is idempotent.
* T2 — the holder keeps writing on the managed path.
* T3 — a replica that pulled is read-only on ``main`` (unprovisioned, then provisioned).
* T4/T5 — an older bh's legacy adopt leaves the fence below its carriers: rollback refuses the
  below-floor state; a step-2 roll-forward converges it.
* T6 — rollback is one forward commit that keeps the ignore row and moves no epoch back; the
  replica's R5 drops its identity; the legacy model writes again.
* C1 refusals leave the hive untouched; a C5 push that loses a race redoes C3.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from beadhive import fence_cutover as fc
from beadhive import fence_data as fd
from beadhive import fence_schema as fs
from beadhive import writer_adopt as wa
from beadhive.claim_authority import ClaimRecord
from harness.writer_fencing import BD_EMBEDDED, BD_SERVER, DOLT_CLI, Cluster, Frame, Placement

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("bd") is None or shutil.which("dolt") is None, reason="bd/dolt not installed"
    ),
]

EPOCH_REF = "refs/bh/epoch"
#: The live hive's epoch when the spike ran; any value works, the point is "not 1".
LIVE_EPOCH = 221
FENCE_TABLES = ("bh_writer", "bh_epoch_live", "bh_write_mark")
ISSUE_INSERT = (
    "insert into issues (id, title, description, design, acceptance_criteria, notes) "
    "values ('{id}', '{id}', '', '', '', '')"
)


class _HQ:
    """:class:`beadhive.fence_cutover.PlacementReader` over the fixture's HQ stand-in."""

    def __init__(self, cluster: Cluster):
        self.cluster = cluster

    def read(self):
        p = self.cluster.hq.placement()
        return None if p.writer is None else wa.PlacementView(p.writer, p.epoch)


def _git_remote(cluster: Cluster, *args: str, data: str | None = None) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(cluster.remote.path), *args],
        input=data,
        capture_output=True,
        text=True,
        check=True,
        env=cluster.remote._env,
    ).stdout.strip()


def _read_ref(cluster: Cluster) -> tuple[str, dict]:
    sha = _git_remote(cluster, "rev-parse", "-q", "--verify", EPOCH_REF)
    return sha, json.loads(_git_remote(cluster, "cat-file", "-p", sha))


def _set_ref(cluster: Cluster, record: dict) -> str:
    blob = _git_remote(
        cluster, "hash-object", "-w", "--stdin", data=json.dumps(record, separators=(",", ":"))
    )
    _git_remote(cluster, "update-ref", EPOCH_REF, blob)
    return blob


def _node(frame: Frame) -> fd.FenceNode:
    if frame.kind == BD_SERVER:
        return fd.FenceNode(fd.BdServerEngine(frame.hive, env=frame.env))
    return fd.FenceNode(fd.DoltCliEngine(frame.dolt_dir, env=frame.env))


def _ref_port(frame: Frame) -> fc.GitLegacyRef:
    return fc.GitLegacyRef(remote="origin", cwd=frame.hive)


def _legacy_state(cluster: Cluster, holder: str = "a", epoch: int = LIVE_EPOCH) -> None:
    """Both legacy carriers name ``holder`` at ``epoch`` (seq 0), as on the live frame."""
    _set_ref(cluster, {"epoch": epoch, "host_id": holder, "seq": 0})
    cluster.hq.restore(Placement(holder, epoch))


def _cutover(cluster: Cluster, frame: Frame, **kw) -> fc.CutoverOutcome:
    return fc.cutover(
        _node(frame),
        _HQ(cluster),
        _ref_port(frame),
        prefix=cluster.prefix,
        host_id=frame.name,
        others_published=kw.pop("attest", True),
        **kw,
    )


def _rollback(cluster: Cluster, frame: Frame, node: fd.FenceNode | None = None):
    return fc.rollback(
        node or _node(frame),
        _HQ(cluster),
        _ref_port(frame) if frame.is_bd else _NoRef(),
        prefix=cluster.prefix,
        host_id=frame.name,
    )


class _NoRef:
    """A replica's rollback (R5 only) never touches the legacy ref."""

    def read(self):  # pragma: no cover - R5 does not read the ref
        raise AssertionError("R5 must not read refs/bh/epoch")

    stage = reserve = read


def _remote_tables(cluster: Cluster, *tables: str) -> dict:
    return cluster.remote.view(tables)["tables"]


# =============================================================================================
# C1 refusals: nothing written
# =============================================================================================


def test_c1_refusals_leave_the_hive_untouched(tmp_path):
    with Cluster(tmp_path / "cluster", [("a", BD_EMBEDDED)]) as cluster:
        a = cluster["a"]
        a.create_issue("pre-cutover").check()
        a.push().check()
        head = cluster.remote.head()

        with pytest.raises(fc.CutoverRefused, match="--others-published"):
            _cutover(cluster, a, attest=False)
        with pytest.raises(fc.CutoverRefused, match="never been adopted.*bh host lease adopt"):
            _cutover(cluster, a)

        _legacy_state(cluster)
        cluster.hq.restore(Placement("a", LIVE_EPOCH + 1))  # an incomplete legacy adopt
        with pytest.raises(fc.CutoverRefused, match="placement_ahead.*bh host lease adopt"):
            _cutover(cluster, a)

        _legacy_state(cluster, holder="b")
        with pytest.raises(fc.CutoverRefused, match="only the current holder"):
            _cutover(cluster, a)

        _legacy_state(cluster)
        a.create_issue("unpublished").check()  # a local commit the remote does not have
        with pytest.raises(fc.CutoverRefused, match="unpublished work.*local commit"):
            _cutover(cluster, a)

        assert cluster.remote.head() == head
        assert _remote_tables(cluster, "bh_writer")["bh_writer"] is None
        assert _read_ref(cluster)[1] == {"epoch": LIVE_EPOCH, "host_id": "a", "seq": 0}


# =============================================================================================
# T1–T3, T6: cutover, re-run, mixed versions, rollback (embedded and server mode)
# =============================================================================================


@pytest.mark.parametrize(
    "kind", [BD_EMBEDDED, pytest.param(BD_SERVER, marks=pytest.mark.dolt_server)]
)
def test_cutover_status_rerun_and_rollback(tmp_path, kind):
    with Cluster(tmp_path / "cluster", [("a", kind), ("c", DOLT_CLI)]) as cluster:
        a, c = cluster["a"], cluster["c"]
        _legacy_state(cluster)
        a.create_issue("pre-cutover").check()
        a.push().check()
        before_sha, _ = _read_ref(cluster)
        in_flight = ClaimRecord(bead="fx-1", seat="dev/x", worktree="/w", issued_at="", epoch=221)

        # ---- T1: C1–C6 -----------------------------------------------------------------
        out = _cutover(cluster, a)
        assert not out.already and out.provisioned
        assert (out.record.hive, out.record.epoch, out.record.holder) == ("fx", LIVE_EPOCH, "a")
        ref_sha, ref = _read_ref(cluster)
        assert ref_sha != before_sha and out.record.ref_sha == ref_sha
        assert ref == {"epoch": LIVE_EPOCH, "host_id": "a", "seq": 1}  # reserved, no bump
        tables = _remote_tables(cluster, *FENCE_TABLES, "bh_local_ident", "issues")
        assert [(r["frame"], r["epoch"]) for r in tables["bh_writer"]] == [("a", LIVE_EPOCH)]
        assert [r["epoch"] for r in tables["bh_epoch_live"]] == [LIVE_EPOCH]
        marks = {r["id"] for r in tables["bh_write_mark"]}
        assert {f"cutover-{LIVE_EPOCH}", f"adopt-{LIVE_EPOCH}"} <= marks
        assert tables["bh_local_ident"] is None  # the identity never reaches the remote
        assert "pre-cutover" in {r["title"] for r in tables["issues"]}
        assert not in_flight.is_stale(LIVE_EPOCH)  # in-flight claims survive the cutover
        assert out.audit.ok and out.guard.count == fs.TRIGGER_COUNT == 44
        assert cluster.remote.view(("bh_writer",))["main"] == out.record.commit

        node = _node(a)
        st = fc.status(node, prefix="fx", placement=_HQ(cluster).read(), ref=_ref_port(a))
        d = st.as_dict()
        assert d["cut_over"] and d["epoch"] == LIVE_EPOCH and d["writer"] == "a"
        assert d["cutover"] == out.record.as_dict()
        assert d["ref_sha"] == ref_sha and d["trigger_count"] == 44
        assert d["fence_audit"]["ok"] and d["findings"] == []
        assert d["ident"] == {"frame": "a", "role": "replica"}

        again = _cutover(cluster, a)  # idempotent: nothing new published
        assert again.already and not again.provisioned
        assert again.record == out.record
        assert _read_ref(cluster)[0] == ref_sha

        # ---- T2: the holder keeps writing ----------------------------------------------
        a.create_issue("post-cutover-a").check()
        if kind == BD_SERVER:
            a.commit("bh: commit working set before push")  # the marks ride a commit
        a.push().check()

        # ---- T3: a replica is read-only on main ----------------------------------------
        with pytest.raises(fc.CutoverRefused, match="has not pulled the cutover"):
            _cutover(cluster, c, attest=False)
        c.pull().check()
        unprovisioned = c.sql(ISSUE_INSERT.format(id="old-bh-on-c"))
        assert not unprovisioned.ok and "bh_local_ident" in unprovisioned.output
        replica = _cutover(cluster, c, attest=False)  # C6: the replica provisions its identity
        assert replica.replica and replica.provisioned and replica.record == out.record
        node_c = _node(c)
        assert node_c.ident() == ("c", "replica")
        refused = c.sql(ISSUE_INSERT.format(id="non-writer-c"))
        assert not refused.ok and fs.GUARD_REFUSAL in refused.output
        node_c.sync_to_remote()
        with pytest.raises(fc.CutoverRefused, match="R1 refused.*holder"):
            _rollback(cluster, c, node_c)  # only the writer rolls back

        # ---- T6: rollback --------------------------------------------------------------
        back = _rollback(cluster, a)
        assert back.floor == LIVE_EPOCH and back.dropped_ident and not back.already
        view = _remote_tables(cluster, *FENCE_TABLES, "issues")
        assert all(view[t] is None for t in FENCE_TABLES)
        assert {"pre-cutover", "post-cutover-a"} <= {r["title"] for r in view["issues"]}
        assert not {"old-bh-on-c", "non-writer-c"} & {r["title"] for r in view["issues"]}
        node.fetch()
        kept = node.query("select pattern from dolt_ignore as of 'origin/main'")
        assert fs.IGNORE_PATTERN in {r["pattern"] for r in kept}  # the ignore row is kept
        ref_sha2, ref2 = _read_ref(cluster)
        assert back.ref_sha == ref_sha2
        assert ref2 == {"epoch": LIVE_EPOCH, "host_id": "a", "seq": 2}  # never moved back
        assert node.ident() is None
        assert node.trigger_count() == 0

        # R5 on the replica, then the legacy model again (a plain fast-forward pull)
        r5 = _rollback(cluster, c, node_c)
        assert r5.already and r5.dropped_ident and node_c.ident() is None
        c.pull().check()
        c.create_issue("post-rollback-c").check()
        c.push().check()
        assert _rollback(cluster, a).already  # idempotent

        st = fc.status(node, prefix="fx", placement=_HQ(cluster).read(), ref=_ref_port(a))
        assert not st.cut_over and st.findings() == []


# =============================================================================================
# T4/T5: rollback refuses a below-floor state; roll-forward converges it
# =============================================================================================


def test_rollback_refuses_below_floor_until_rolled_forward(tmp_path):
    with Cluster(tmp_path / "cluster", [("a", BD_EMBEDDED)]) as cluster:
        a = cluster["a"]
        _legacy_state(cluster)
        a.push().check()
        _cutover(cluster, a)
        ref_sha, _ = _read_ref(cluster)

        # An OLDER bh adopts: placement and the legacy ref move to 222, bh_writer stays at 221.
        legacy = cluster.hq.place("a", expected_epoch=LIVE_EPOCH).epoch
        _set_ref(cluster, {"epoch": legacy, "host_id": "a", "seq": 0})
        with pytest.raises(fc.CutoverRefused, match="R2 refused.*below-floor.*bh host lease adopt"):
            _rollback(cluster, a)
        assert _remote_tables(cluster, "bh_writer")["bh_writer"] is not None  # nothing dropped

        # status flags the half-state the old adopt left behind
        node = _node(a)
        st = fc.status(node, prefix="fx", placement=_HQ(cluster).read(), ref=_ref_port(a))
        assert any(f.startswith("placement_ahead") for f in st.findings())

        # Roll forward (adopt step 2 at the placed epoch), then the rollback floors at 222.
        result = wa.run_step2(node, _HQ(cluster), frame="a", epoch=legacy)
        assert result.landed, result
        back = _rollback(cluster, a)
        assert back.floor == legacy
        assert _read_ref(cluster)[1] == {"epoch": legacy, "host_id": "a", "seq": 1}
        assert _read_ref(cluster)[0] != ref_sha


# =============================================================================================
# C5: a push that loses a fast-forward race resets to the remote and redoes C3
# =============================================================================================


def test_a_lost_push_race_redoes_c3_from_the_new_head(tmp_path):
    with Cluster(tmp_path / "cluster", [("a", BD_EMBEDDED), ("c", DOLT_CLI)]) as cluster:
        a, c = cluster["a"], cluster["c"]
        _legacy_state(cluster)
        a.push().check()
        c.pull().check()

        def racer() -> None:  # a write lands on the remote between C4 and the push
            c.create_issue("raced-in").check()
            c.push().check()

        out = _cutover(cluster, a, before_push=racer)
        assert out.attempts == 2 and out.audit.ok
        tables = _remote_tables(cluster, "bh_writer", "issues")
        assert [(r["frame"], r["epoch"]) for r in tables["bh_writer"]] == [("a", LIVE_EPOCH)]
        assert "raced-in" in {r["title"] for r in tables["issues"]}
        # two reservations: the lost round's and the winning one's — the epoch never moved
        assert _read_ref(cluster)[1] == {"epoch": LIVE_EPOCH, "host_id": "a", "seq": 2}
