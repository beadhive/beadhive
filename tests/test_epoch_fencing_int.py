"""Spike bh-vje85 evidence: does in-data epoch fencing fence a stale writer under real bd?

Feeds ``docs/spikes/bh-vje85-in-data-epoch-fencing.md``. Test-only: the fence model lives in
:mod:`harness.epoch_fence` and runs on the bh-eybn7 fixture (:mod:`harness.writer_fencing`):
real bd 1.3 embedded frames (bd's linked Dolt), bd 1.3 shared-server frames (a frame-private
``dolt sql-server``, Dolt CLI 2.3.5) and Dolt CLI frames, all on ONE ``git+file://`` remote
(production ``git+ssh`` shape: ``refs/dolt/data`` behind ``push --force-with-lease``).

Proposal scenarios (``docs/design/hive-writer-partitioning-proposal.md``, "Validation approach"):

1. planned handoff while the old writer is idle;
2. handoff while the old writer is mid-push (barrier-ordered, both orders);
3. partitioned old writer, failover, rejoin: fenced, unpublished work diverted to an orphan
   branch, then merged deliberately by the new writer;
4. two adopters race: one placement CAS wins; a loser that reached step 2 abandons;
5. a stale writer on every default bd path (push, pull+push, sync, auto-push, raw dolt);
6. ``hive_sync`` (``bd federation sync --strategy``), ``bd sync`` and ``bd dolt pull`` never
   revert ``bh_writer``;

Scenarios 1, 2 and 4 are pure protocol: they run on the PRODUCT adopt over the HQ storage port's
in-memory fence in ``tests/test_epoch_fencing_protocol.py`` (bh-utvyj). Their bd/Dolt-specific
assertions are kept here, where other scenarios assert the same thing on real frames: the guard
triggers refusing a synced stale frame's write (scenario 3), a stale bd push rejected as
non-fast-forward and bd's settle gate refusing a stale pull (scenario 5), and the push CAS under
concurrency (the ``file://`` test). Scenario 3 is this file's end-to-end smoke for the trigger
and the orphan path.

plus the bd force paths (break-glass, with after-the-fact detection), mark growth and pruning, and
``file://`` push CAS. Judged on remote ``main`` and table contents, never on ``refs/dolt/data``
(a refused push still moves that ref: bh-eybn7 Evidence 6). Every step goes through
:class:`harness.writer_fencing.Recorder` (remote head + main + fence rows + per-frame state before
and after) so a failure names the step that let a stale write through.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from harness.epoch_fence import (
    GUARD_REFUSAL,
    MONOTONIC_REFUSAL,
    adopt,
    bump_statements,
    divert_to_orphan,
    fence_audit,
    install_fence,
    local_state,
    local_writer,
    mark_counts,
    merge_orphan,
    remote_branch_titles,
    remote_fence,
    run_script,
    sync_to_remote,
)
from harness.writer_fencing import (
    BD_EMBEDDED,
    BD_SERVER,
    DOLT_CLI,
    Cluster,
    Frame,
    Recorder,
    Run,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.dolt_server,
    pytest.mark.skipif(
        shutil.which("bd") is None or shutil.which("dolt") is None, reason="bd/dolt not installed"
    ),
]

SRC = Path(__file__).resolve().parents[1] / "src"
FRAMES = [("a", BD_EMBEDDED), ("b", BD_SERVER), ("c", DOLT_CLI)]
REMOTE_TABLES = ("bh_writer", "bh_epoch_live", "bh_write_mark")

NON_FF = "non-fast-forward"
#: bd's embedded settle gate (mergesettle.go:190-198): abort + restore, nothing committed.
SETTLE_REFUSAL = "pull merge left constraint violations bd cannot auto-repair"
#: Dolt's own merge report, seen when bd shells out to ``dolt pull`` (server mode) or raw CLI.
CLI_VIOLATION = (
    "CONSTRAINT VIOLATION (content): Merge created constraint violation in bh_write_mark"
)
#: A SQL transaction refusing to commit a working set that holds merge violations.
TXN_VIOLATION = "Committing this transaction resulted in a working set with constraint violations"
#: bd's embedded auto-resolver declining bh_* conflicts (mergesettle.go:429-566).
BH_CONFLICT_REFUSAL = "merge conflicts in bh_epoch_live, bh_writer require operator resolution"


@contextlib.contextmanager
def fenced_cluster(
    root: Path, frames: Sequence[tuple[str, str]], *, writer: str = "c"
) -> Iterator[Cluster]:
    """A cluster with the in-data fence installed from ``writer`` (HQ epoch 1, published, every
    frame pulled and given its local identity): the pooled template for ``rewound_custom``
    (bh-t8hr4). Every test here starts from exactly this state, so the install is paid once per
    composition per worker and rewound with the rest of the cluster, not re-run per test."""
    with Cluster(root / "cluster", list(frames)) as cluster:
        install_fence(cluster, writer)
        yield cluster


def _recorder(cluster: Cluster, *watch: str) -> Recorder:
    """Recorder whose per-frame state adds the frame's own fence view for ``watch`` frames."""

    def probe(frame: Frame) -> dict:
        state = frame.guard()
        if frame.name in watch and frame.alive and not frame.busy:
            state["local"] = local_state(frame)
        return state

    trace = None
    if os.environ.get("BH_SPIKE_TRACE_DIR"):  # keep the trace for the spike doc's evidence
        test = os.environ.get("PYTEST_CURRENT_TEST", "trace").split("::")[-1].split(" ")[0]
        trace = Path(os.environ["BH_SPIKE_TRACE_DIR"]) / f"{test}.jsonl"
        trace.parent.mkdir(parents=True, exist_ok=True)
        trace.unlink(missing_ok=True)
    return Recorder(cluster, trace, remote_tables=REMOTE_TABLES, guard_probe=probe)


def _evidence(recorder: Recorder, step: str, frame: str, run: Run) -> Run:
    """Append one command's exit code and output tail to the trace (the doc quotes these)."""
    entry = {
        "seq": len(recorder.entries),
        "step": step,
        "frame": frame,
        "phase": "command",
        "argv": list(run.argv),
        "returncode": run.returncode,
        "output": run.output[-2000:],
    }
    recorder.entries.append(entry)
    with recorder.trace.open("a") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return run


def _make_stale(
    cluster: Cluster, stale: str, adopter: str, *, sentinel: bool = True
) -> tuple[int, int]:
    """``stale`` becomes the writer at a fresh epoch, then ``adopter`` takes over at the next.

    Forward-only (no rewind): a long-lived ``dolt sql-server`` keeps its old remote-tracking ref
    when the fixture moves the remote ref BACKWARDS, which production never does."""
    for frame in cluster.frames.values():
        (frame.hive / ".beads" / "push-state.json").unlink(missing_ok=True)
    held = cluster.hq.place(stale).epoch
    assert adopt(cluster, cluster[stale], held, sentinel=sentinel) == "landed"
    new = cluster.hq.place(adopter, expected_epoch=held).epoch
    assert adopt(cluster, cluster[adopter], new, sentinel=sentinel) == "landed"
    return held, new


def _stale_adopter(
    cluster: Cluster, name: str, adopter: str = "c", *, sentinel: bool = True
) -> tuple[int, int]:
    """``name`` wins placement, commits its bump locally and stalls; the director re-places to
    ``adopter``, whose bump lands. Leaves a both-changed ``bh_writer`` conflict on ``name``."""
    frame = cluster[name]
    _make_stale(cluster, name, adopter, sentinel=sentinel)
    sync_to_remote(frame)
    stale_epoch = cluster.hq.place(name).epoch
    run_script(frame, bump_statements(name, stale_epoch, sentinel=sentinel)).check()
    frame.commit(f"bh: adopt {name}@{stale_epoch}").check()
    current = cluster.hq.place(adopter).epoch
    assert adopt(cluster, cluster[adopter], current, sentinel=sentinel) == "landed"
    return stale_epoch, current


def _fetch(frame: Frame) -> None:
    if frame.kind == BD_SERVER:
        frame.bd("sql", "CALL DOLT_FETCH('origin')").check()
    else:
        frame.dolt("fetch", "origin").check()


def _hive_sync(frame: Frame, strategy: str | None) -> Run:
    """bh's own hive_sync engine call (``BdEngine.sync_state`` -> ``bd federation sync``), run
    inside the frame's environment against peer ``hub`` (the shared remote)."""
    code = (
        "import sys; from beadhive.engine import BdEngine; "
        "print(repr(BdEngine().sync_state(sys.argv[1], peer='hub', strategy=sys.argv[2] or None)))"
    )
    return frame.run(
        "env",
        f"PYTHONPATH={SRC}",
        sys.executable,
        "-c",
        code,
        str(frame.hive),
        strategy or "",
    )


# =============================================================================================
# Scenario 3 — partitioned old writer, failover, rejoin, orphan branch
# =============================================================================================


def test_s3_partitioned_writer_is_fenced_and_its_unpublished_work_survives_on_an_orphan(
    rewound_custom,
):
    cluster = rewound_custom(fenced_cluster, FRAMES)
    recorder = _recorder(cluster, "a", "b", "c")
    c = cluster["c"]
    for name in ("a", "b"):
        old = cluster[name]
        held = cluster.hq.place(name).epoch
        assert adopt(cluster, old, held) == "landed"
        old.create_issue(f"s3-{name}-published").check()
        old.push().check()

        with recorder.step(f"s3:{name}:partition", name):
            old.partition()
            cluster.hq.partition(name)
            for i in (1, 2):
                old.create_issue(f"s3-{name}-unpublished-{i}").check()
            cut = old.push()
        assert not cut.ok and "partition" in cut.output

        new = cluster.hq.place("c").epoch  # the director fails over; A cannot see HQ
        with recorder.step(f"s3:{name}:failover", "c"):
            assert adopt(cluster, c, new) == "landed"

        with recorder.step(f"s3:{name}:rejoin-raw-bd-sync", name) as raw:
            old.heal()
            cluster.hq.heal(name)
            synced = _evidence(recorder, "s3:raw-bd-sync", name, old.bd("sync"))
        assert not synced.ok, synced.output
        assert SETTLE_REFUSAL in synced.output or CLI_VIOLATION in synced.output
        assert raw["after"]["remote"]["main"] == raw["before"]["remote"]["main"]

        with recorder.step(f"s3:{name}:divert", name):
            diverted = divert_to_orphan(old, held)
        assert diverted["diverted"], diverted
        branch = diverted["branch"]
        orphan = remote_branch_titles(cluster, branch)
        assert orphan is not None
        assert {f"s3-{name}-unpublished-1", f"s3-{name}-unpublished-2"} <= orphan
        remote = remote_fence(cluster)
        assert not {f"s3-{name}-unpublished-1", f"s3-{name}-unpublished-2"} & remote["titles"]
        assert local_writer(old) == ("c", new)
        refused = old.create_issue(f"s3-{name}-after-divert")
        assert not refused.ok and GUARD_REFUSAL in refused.output

        with recorder.step(f"s3:{name}:orphan-merge", "c"):
            merged = merge_orphan(c, branch)
            _evidence(recorder, "s3:orphan-merge", "c", merged["run"])
            assert merged["run"].ok, merged["run"].output
            assert merged["state"]["violations"] == [] and not merged["state"]["merging"]
            c.push().check()
        remote = remote_fence(cluster)
        assert {f"s3-{name}-unpublished-1", f"s3-{name}-unpublished-2"} <= remote["titles"]
        assert remote["writer"] == ("c", new) and set(remote["marks"]) <= {new}
        audit = fence_audit(cluster)
        assert audit["stale_marks"] == 0 and not audit["epoch_regressed"], audit


# =============================================================================================
# Scenario 5 — a stale writer on every default bd path
# =============================================================================================


def _stale_path(cluster, recorder, stale: str, path: str, adopter: str = "c") -> dict:
    """Make ``stale`` a stale writer, give it one unpublished write, drive ``path``."""
    s = cluster[stale]
    _make_stale(cluster, stale, adopter)
    title = f"s5-{stale}-{path}"
    runs: dict[str, Run] = {}
    with recorder.step(f"s5:{stale}:{path}", stale) as step:
        if path == "auto-push":
            runs["create+auto-push"] = s.run(
                "env",
                "BD_DOLT_AUTO_PUSH=true",
                "bd",
                "create",
                "--title",
                title,
                "-t",
                "task",
                "-p",
                "2",
            )
        else:
            s.create_issue(title).check()
        if path == "bd-dolt-push":
            runs["push"] = s.push()
        elif path == "bd-pull-then-push":
            runs["pull"] = s.pull()
            runs["push"] = s.push()
        elif path == "bd-sync":
            runs["sync"] = s.bd("sync")
        elif path == "raw-dolt":
            if s.kind == BD_SERVER:
                runs["pull"] = s.bd("sql", "CALL DOLT_PULL('origin', 'main')")
                runs["commit"] = s.bd("dolt", "commit", "-m", "commit the unstaged mark")
                runs["pull-again"] = s.bd("sql", "CALL DOLT_PULL('origin', 'main')")
                runs["push"] = s.bd("sql", "CALL DOLT_PUSH('origin', 'main')")
            else:
                runs["pull"] = s.dolt("pull", "origin", "main")
                runs["commit"] = s.dolt("commit", "-am", "commit the merge")
                runs["push"] = s.dolt("push", "origin", "main")
    for label, run in runs.items():
        _evidence(recorder, f"s5:{stale}:{path}:{label}", stale, run)
    state = local_state(s)
    remote = remote_fence(cluster)
    assert title not in remote["titles"], (path, runs)
    assert step["after"]["remote"]["main"] == step["before"]["remote"]["main"], (path, runs)
    return {"runs": runs, "state": state, "remote": remote}


#: Scenario 5's (stale writer, path) pairs: every default bd path on the embedded and the
#: server-mode frame, plus raw Dolt on a Dolt CLI frame. One case per pair (bh-56wtv): each
#: ``_stale_path`` starts from ``_make_stale``, which re-syncs the stale frame to the remote, so a
#: pair never depended on the one before it, and the pooled cluster rewinds between cases.
S5_PATHS = ("bd-dolt-push", "bd-pull-then-push", "bd-sync", "auto-push", "raw-dolt")
S5_CASES = [(stale, path) for stale in ("a", "b") for path in S5_PATHS] + [("c", "raw-dolt")]


@pytest.mark.parametrize(("stale", "path"), S5_CASES, ids=[f"{s}-{p}" for s, p in S5_CASES])
def test_s5_stale_writer_is_refused_on_every_default_bd_path(rewound_custom, stale, path):
    cluster = rewound_custom(fenced_cluster, FRAMES)
    recorder = _recorder(cluster, "a", "b")
    if stale == "c":
        # A Dolt CLI frame as the stale writer (bh-cvk70 measured this on file://).
        out = _stale_path(cluster, recorder, "c", "raw-dolt", adopter="a")
        assert CLI_VIOLATION in out["runs"]["pull"].output and not out["runs"]["push"].ok
        return

    embedded = cluster[stale].kind == BD_EMBEDDED

    if path == "bd-dolt-push":
        out = _stale_path(cluster, recorder, stale, "bd-dolt-push")
        assert NON_FF in out["runs"]["push"].output

    elif path == "bd-pull-then-push":
        out = _stale_path(cluster, recorder, stale, "bd-pull-then-push")
        assert not out["runs"]["pull"].ok and NON_FF in out["runs"]["push"].output
        if embedded:  # settle gate: abort and restore, the stale frame still names itself
            assert SETTLE_REFUSAL in out["runs"]["pull"].output
            assert not out["state"]["merging"] and out["state"]["writer"][0] == stale
        else:  # server mode shells out to `dolt pull`: the half-merge stays in the server
            assert CLI_VIOLATION in out["runs"]["pull"].output
            assert out["state"]["merging"] and out["state"]["violations"] == ["bh_write_mark"]
            assert out["state"]["writer"][0] == "c"  # ...and already names the new writer
            s = cluster[stale]
            guarded = s.create_issue("s5-b-half-merged-write")
            assert not guarded.ok and GUARD_REFUSAL in guarded.output
            keeper = s.query("SELECT id FROM issues LIMIT 1")[0]["id"]
            unguarded = s.bd("label", "add", keeper, "stale-label")
            assert not unguarded.ok and TXN_VIOLATION in unguarded.output
            commit = s.bd("dolt", "commit", "-m", "try to conclude")
            assert not commit.ok and "constraint violations" in commit.output
            for label, run in (("guarded", guarded), ("label", unguarded), ("commit", commit)):
                _evidence(recorder, f"s5:b:half-merged:{label}", "b", run)
            assert not s.push().ok

    elif path == "bd-sync":
        out = _stale_path(cluster, recorder, stale, "bd-sync")
        assert not out["runs"]["sync"].ok
        assert (SETTLE_REFUSAL if embedded else CLI_VIOLATION) in out["runs"]["sync"].output

    elif path == "auto-push":
        out = _stale_path(cluster, recorder, stale, "auto-push")
        run = out["runs"]["create+auto-push"]
        assert run.ok and "auto-push failed" in run.output and NON_FF in run.output

    else:
        out = _stale_path(cluster, recorder, stale, "raw-dolt")
        if embedded:  # the Dolt CLI opened on bd's embedded store
            assert CLI_VIOLATION in out["runs"]["pull"].output
            assert "constraint violations" in out["runs"]["commit"].output
            assert not out["runs"]["commit"].ok
        else:  # bd left the trigger's mark unstaged, so the first pull refuses to start
            assert "cannot merge with uncommitted changes" in out["runs"]["pull"].output
            assert not out["runs"]["pull-again"].ok
        assert not out["runs"]["push"].ok


#: The server-mode mark case's independent parts (bh-56wtv): the proposal's bump on each frame
#: (the embedded refusal, the server-mode hole), the adopt-sentinel guard, and the committed-mark
#: control per frame and ``bd vc merge`` strategy. Every part opens with ``adopt`` or
#: ``_make_stale`` (both re-sync the frame to the remote), so none relied on an earlier one.
S5VC_STRATEGIES = (None, "ours", "theirs")
S5VC_PARTS = [("vc-merge", "a", None), ("vc-merge", "b", None), ("sentinel", "b", None)] + [
    ("committed-mark", name, strategy) for name in ("a", "b") for strategy in S5VC_STRATEGIES
]


@pytest.mark.parametrize(
    ("part", "name", "strategy"),
    S5VC_PARTS,
    ids=[
        f"{part}-{name}" + (f"-{strategy}" if part == "committed-mark" else "")
        for part, name, strategy in S5VC_PARTS
    ],
)
def test_s5_server_mode_mark_is_not_in_the_write_commit_and_the_adopt_sentinel_closes_vc_merge(
    rewound_custom, part, name, strategy
):
    """bd's server-mode write commit stages only ``issues`` and ``events`` (GH#2455), so the guard
    trigger's ``bh_write_mark`` row stays unstaged and the commit that carries the stale write
    carries no mark. Every default pull/sync path commits the working set before merging
    (commitBeforePull), so they still trip the foreign key. ``bd vc merge`` does not: with the
    proposal's bump it lands the stale write in server mode. Embedded bd commits with
    ``DOLT_COMMIT('-Am')`` and is refused. An ``adopt-<epoch>`` sentinel mark in every bump makes
    the server-mode merge refuse too."""
    cluster = rewound_custom(fenced_cluster, FRAMES)
    recorder = _recorder(cluster, "a", "b")

    if part == "vc-merge":
        s = cluster[name]
        held = cluster.hq.place(name).epoch
        assert adopt(cluster, s, held) == "landed"
        s.create_issue(f"s5vc-{name}-legit").check()
        staged = sorted(
            r["table_name"]
            for r in s.query("SELECT table_name FROM dolt_diff WHERE commit_hash = hashof('main')")
        )
        s.push().check()
        if name == "a":
            assert "bh_write_mark" in staged
        else:
            assert staged == ["issues"]  # the server-mode write commit has no mark

        _make_stale(cluster, name, "c", sentinel=False)  # the proposal's original bump
        s.create_issue(f"s5vc-{name}-stale").check()
        _fetch(s)
        with recorder.step(f"s5vc:{name}:vc-merge", name):
            merged = _evidence(
                recorder, f"s5vc:{name}:vc-merge", name, s.bd("vc", "merge", "origin/main")
            )
            pushed = _evidence(recorder, f"s5vc:{name}:push", name, s.push())
        titles = remote_fence(cluster)["titles"]
        if name == "a":
            assert not merged.ok and TXN_VIOLATION in merged.output, merged.output
            assert not pushed.ok and "s5vc-a-stale" not in titles
        else:
            assert merged.ok and pushed.ok, (merged.output, pushed.output)
            assert "s5vc-b-stale" in titles  # the hole: a stale write after the bump
            audit = fence_audit(cluster)
            assert audit["stale_marks"] >= 1, audit  # ...but bh can see it afterwards
        sync_to_remote(s)

    elif part == "sentinel":
        # The bh-side guard: a bump that always inserts an `adopt-<epoch>` sentinel mark changes
        # bh_write_mark, so Dolt refuses to merge it over the stale frame's uncommitted mark.
        assert adopt(cluster, cluster["c"], cluster.hq.place("c").epoch) == "landed"
        b = cluster["b"]
        _make_stale(cluster, "b", "c")
        b.create_issue("s5vc-b-stale-sentinel").check()
        _fetch(b)
        with recorder.step("s5vc:b:vc-merge-sentinel", "b") as step:
            merged = _evidence(
                recorder, "s5vc:b:vc-merge-sentinel", "b", b.bd("vc", "merge", "origin/main")
            )
            pushed = b.push()
        assert not merged.ok and "local changes would be stomped by merge" in merged.output
        assert "bh_write_mark" in merged.output and not pushed.ok
        assert step["after"]["remote"]["main"] == step["before"]["remote"]["main"]
        assert "s5vc-b-stale-sentinel" not in remote_fence(cluster)["titles"]
        sync_to_remote(b)

    else:
        # Control: once the mark IS in the stale commit, every vc merge variant is refused in
        # both modes and `bd conflicts resolve --conclude` will not settle the violation.
        s = cluster[name]
        _make_stale(cluster, name, "c")
        s.create_issue(f"s5vc-{name}-committed-{strategy}").check()
        if s.kind == BD_SERVER:
            s.bd("dolt", "commit", "-m", "bh: commit the unstaged mark").check()
        _fetch(s)
        args = ["vc", "merge", "origin/main"] + (["--strategy", strategy] if strategy else [])
        with recorder.step(f"s5vc:{name}:committed-mark:{strategy}", name) as step:
            merged = _evidence(recorder, f"s5vc:{name}:{strategy}", name, s.bd(*args))
            concluded = _evidence(
                recorder,
                f"s5vc:{name}:{strategy}:conclude",
                name,
                s.bd("conflicts", "resolve", "--conclude"),
            )
            pushed = s.push()
        assert not merged.ok and "constraint violations" in merged.output, merged.output
        assert not pushed.ok
        assert step["after"]["remote"]["main"] == step["before"]["remote"]["main"]
        assert f"s5vc-{name}-committed-{strategy}" not in remote_fence(cluster)["titles"]
        if local_state(s)["merging"]:
            assert not concluded.ok and "constraint violations" in concluded.output
        sync_to_remote(s)


# =============================================================================================
# Scenario 6 — hive_sync / bd sync / bd dolt pull never revert bh_writer
# =============================================================================================


#: Scenario 6's (frame, sync path) parts (bh-56wtv). Each part opens with ``_make_stale`` or
#: ``_stale_adopter`` (both re-sync the frame to the remote first), so none relied on an earlier
#: part; the local-regression check stays with the pull part whose ``stale_epoch`` it replays.
S6_STEPS = (
    "hive-sync-ours-one-sided",
    "bd-dolt-pull-both-changed",
    "bd-sync-both-changed",
    "hive-sync-ours-both-changed",
)
S6_CASES = [(name, step) for name in ("a", "b") for step in S6_STEPS]


@pytest.mark.parametrize(("name", "path"), S6_CASES, ids=[f"{n}-{p}" for n, p in S6_CASES])
def test_s6_hive_sync_bd_sync_and_pull_never_revert_bh_writer(rewound_custom, name, path):
    cluster = rewound_custom(fenced_cluster, FRAMES)
    recorder = _recorder(cluster, "a", "b")
    for peer in ("a", "b"):
        cluster[peer].bd("federation", "add-peer", "hub", cluster.remote.url).check()

    s = cluster[name]
    if path == "hive-sync-ours-one-sided":
        # One-sided: a stale WRITER never edits bh_writer, so the merge has no conflict to
        # resolve in its favour; the bump arrives together with the FK violation.
        _make_stale(cluster, name, "c")
        s.create_issue(f"s6-{name}-stale").check()
        with recorder.step(f"s6:{name}:hive-sync-ours-one-sided", name) as step:
            synced = _evidence(recorder, "s6:hive-sync-one-sided", name, _hive_sync(s, "ours"))
        assert step["after"]["remote"]["main"] == step["before"]["remote"]["main"]
        assert remote_fence(cluster)["writer"][0] == "c"
        # bd prints the merge failure, exits 0, and serialises the error as {} in --json (see
        # the doc). bh used to report that as success; since bh-hpzav it reads the in-band
        # marker and reports the failed sync honestly.
        assert "ok=False" in synced.stdout, synced.output
        assert "Error from peer hub" in synced.stdout, synced.output
        sync_to_remote(s)

    elif path == "bd-dolt-pull-both-changed":
        # Both-changed: a stale ADOPTER (scenario 4 loser) holding its own bump commit.
        stale_epoch, current = _stale_adopter(cluster, name)
        with recorder.step(f"s6:{name}:bd-dolt-pull-both-changed", name) as step:
            pulled = _evidence(recorder, "s6:pull-both-changed", name, s.pull())
        assert not pulled.ok
        if s.kind == BD_EMBEDDED:
            assert BH_CONFLICT_REFUSAL in pulled.output, pulled.output
            assert local_state(s)["writer"] == (name, stale_epoch)  # aborted and restored
        else:
            assert "CONFLICT (content): Merge conflict in bh_writer" in pulled.output
            state = local_state(s)
            assert state["conflicts"] == ["bh_epoch_live", "bh_writer"]
            resolved = s.bd("conflicts", "resolve", "--all", "--table", "bh_writer", "--ours")
            _evidence(recorder, "s6:server-conflicts-resolve", name, resolved)
            assert not resolved.ok and "autocommit" in resolved.output
            assert not s.push().ok
        assert step["after"]["remote"]["main"] == step["before"]["remote"]["main"]
        assert remote_fence(cluster)["writer"] == ("c", current)

        # The stale adopter re-running its old bump ON TOP of the new head would be a
        # one-sided change that every sync path fast-forwards; the monotonic trigger refuses
        # it locally (adopt itself never gets here: it stops on `bh_writer.epoch >= mine`).
        sync_to_remote(s)
        regress = run_script(s, bump_statements(name, stale_epoch))
        _evidence(recorder, "s6:local-regression", name, regress)
        assert not regress.ok and MONOTONIC_REFUSAL in regress.output, regress.output

    elif path == "bd-sync-both-changed":
        stale_epoch, current = _stale_adopter(cluster, name)
        with recorder.step(f"s6:{name}:bd-sync-both-changed", name) as step:
            synced = _evidence(recorder, "s6:bd-sync-both-changed", name, s.bd("sync"))
        assert not synced.ok, synced.output
        assert step["after"]["remote"]["main"] == step["before"]["remote"]["main"]
        sync_to_remote(s)

    else:
        stale_epoch, current = _stale_adopter(cluster, name)
        with recorder.step(f"s6:{name}:hive-sync-ours-both-changed", name) as step:
            synced = _evidence(recorder, "s6:hive-sync-both", name, _hive_sync(s, "ours"))
        assert step["after"]["remote"]["main"] == step["before"]["remote"]["main"]
        # Nothing merged, and since bh-hpzav bh says so instead of reporting success.
        assert "ok=False" in synced.stdout, synced.output
        assert "Error from peer hub" in synced.stdout, synced.output
        assert remote_fence(cluster)["writer"] == ("c", current)
        assert fence_audit(cluster)["epoch_regressed"] is False
        sync_to_remote(s)


# =============================================================================================
# bd force paths — break-glass, and what bh can detect afterwards
# =============================================================================================


#: The force paths, one case each (bh-56wtv). Every path opens with ``_stale_adopter`` or
#: ``_make_stale`` (both re-sync the frame to the remote first), so none relied on an earlier one.
FORCE_CASES = [
    ("pull-strategy-ours", False),
    ("pull-strategy-ours", True),
    ("vc-merge-ours-conclude", False),
    ("vc-merge-ours-conclude", True),
    ("bd-dolt-push-force", None),
    ("raw-dolt-commit-force", None),
]


@pytest.mark.parametrize(
    ("path", "sentinel"),
    FORCE_CASES,
    ids=[p if s is None else f"{p}-sentinel={s}" for p, s in FORCE_CASES],
)
def test_force_paths_defeat_the_fence_and_are_detectable_afterwards(rewound_custom, path, sentinel):
    cluster = rewound_custom(fenced_cluster, FRAMES)
    recorder = _recorder(cluster, "a", "b")
    a, b, c = cluster["a"], cluster["b"], cluster["c"]

    def recover() -> dict:
        """Re-run adopt step 2 at a fresh placement epoch: it retires every stale mark."""
        assert adopt(cluster, c, cluster.hq.place("c").epoch) == "landed"
        for frame in (a, b):
            sync_to_remote(frame)
        return fence_audit(cluster)

    # 1. embedded `bd dolt pull --strategy ours`, 2. `bd vc merge --strategy ours` + `bd
    #    conflicts resolve --conclude` (server mode), each against a stale ADOPTER. With the
    #    proposal's bump both resolve bh_writer + bh_epoch_live to the stale side and the
    #    revert lands (the history shows the epoch went backwards). With the adopt sentinel
    #    the losing side's `adopt-<e>` mark points at an epoch that is no longer live, so
    #    every resolution leaves a foreign-key violation and bd refuses it.
    if path == "pull-strategy-ours":
        stale_epoch, current = _stale_adopter(cluster, "a", sentinel=sentinel)
        with recorder.step(f"force:pull-strategy-ours:sentinel={sentinel}", "a") as step:
            run = _evidence(
                recorder,
                f"force:pull-ours:sentinel={sentinel}",
                "a",
                a.bd("dolt", "pull", "--strategy", "ours"),
            )
            pushed = a.push()
        if not sentinel:
            assert run.ok and pushed.ok, (run.output, pushed.output)
            assert remote_fence(cluster)["writer"] == ("a", stale_epoch)
            audit = fence_audit(cluster)
            assert audit["epoch_regressed"] and audit["placement_ahead"], audit
            recovered = recover()
            assert not any(recovered[k] for k in ("epoch_regressed", "placement_ahead"))
        else:
            assert not run.ok and SETTLE_REFUSAL in run.output, run.output
            assert not pushed.ok
            assert step["after"]["remote"]["main"] == step["before"]["remote"]["main"]
            assert remote_fence(cluster)["writer"] == ("c", current)
            sync_to_remote(a)

    elif path == "vc-merge-ours-conclude":
        stale_epoch, current = _stale_adopter(cluster, "b", sentinel=sentinel)
        _fetch(b)
        with recorder.step(f"force:vc-merge-ours-conclude:sentinel={sentinel}", "b") as step:
            run = _evidence(
                recorder,
                f"force:vc-merge-ours:sentinel={sentinel}",
                "b",
                b.bd("vc", "merge", "origin/main", "--strategy", "ours"),
            )
            concluded = _evidence(
                recorder,
                f"force:conclude:sentinel={sentinel}",
                "b",
                b.bd("conflicts", "resolve", "--conclude"),
            )
            pushed = b.push()
        if not sentinel:
            assert run.ok and concluded.ok and pushed.ok, (run.output, concluded.output)
            assert remote_fence(cluster)["writer"] == ("b", stale_epoch)
            audit = fence_audit(cluster)
            assert audit["epoch_regressed"] and audit["placement_ahead"], audit
            recovered = recover()
            assert not any(recovered[k] for k in ("epoch_regressed", "placement_ahead"))
        else:
            assert not run.ok and "constraint violations" in run.output, run.output
            assert not pushed.ok
            assert step["after"]["remote"]["main"] == step["before"]["remote"]["main"]
            assert remote_fence(cluster)["writer"] == ("c", current)
            sync_to_remote(b)

    elif path == "bd-dolt-push-force":
        # 3. `bd dolt push --force`: overwrites remote main, bump and all.
        _make_stale(cluster, "a", "c")
        a.create_issue("force-push-stale").check()
        with recorder.step("force:bd-dolt-push-force", "a"):
            run = _evidence(recorder, "force:push-force", "a", a.bd("dolt", "push", "--force"))
        assert run.ok, run.output
        remote = remote_fence(cluster)
        assert remote["writer"][0] == "a" and "force-push-stale" in remote["titles"]
        assert fence_audit(cluster)["placement_ahead"]
        assert not recover()["placement_ahead"]

    else:
        # 4. raw `dolt commit --force` on a half-merged stale store, then a plain push.
        _make_stale(cluster, "a", "c")
        a.create_issue("raw-force-commit-stale").check()
        with recorder.step("force:raw-dolt-commit-force", "a"):
            pulled = a.dolt("pull", "origin", "main")
            forced = _evidence(
                recorder, "force:raw-commit-force", "a", a.dolt("commit", "--force", "-am", "x")
            )
            pushed = _evidence(recorder, "force:raw-push", "a", a.dolt("push", "origin", "main"))
        assert CLI_VIOLATION in pulled.output and forced.ok and pushed.ok
        assert "raw-force-commit-stale" in remote_fence(cluster)["titles"]
        assert fence_audit(cluster)["stale_marks"] >= 1


def test_force_remote_reset_data_lets_a_stale_store_republish(rewound_custom):
    """``bd dolt remote reset-data`` clears ``refs/dolt/data``; the stale store's next plain push
    then lands because there is no history left to be non-fast-forward against."""
    cluster = rewound_custom(fenced_cluster, [("a", BD_EMBEDDED), ("c", DOLT_CLI)])
    recorder = _recorder(cluster, "a")
    a = cluster["a"]
    _make_stale(cluster, "a", "c")
    a.create_issue("reset-data-stale").check()
    with recorder.step("force:reset-data", "a"):
        reset = _evidence(
            recorder,
            "force:reset-data",
            "a",
            a.bd("dolt", "remote", "reset-data", "origin", "--yes"),
        )
        pushed = _evidence(recorder, "force:reset-data-push", "a", a.push())
    assert reset.ok, reset.output
    assert pushed.ok, pushed.output
    remote = remote_fence(cluster)
    assert remote["writer"][0] == "a" and "reset-data-stale" in remote["titles"]
    assert fence_audit(cluster)["placement_ahead"]


# =============================================================================================
# Marks: growth, pruning
# =============================================================================================


def test_marks_grow_one_per_write_and_only_published_marks_may_be_pruned(rewound_custom):
    cluster = rewound_custom(fenced_cluster, [("a", BD_EMBEDDED), ("c", DOLT_CLI)])
    recorder = _recorder(cluster, "a")
    a, c = cluster["a"], cluster["c"]
    held = cluster.hq.place("a").epoch
    assert adopt(cluster, a, held) == "landed"
    with recorder.step("marks:growth", "a"):
        for i in range(5):
            a.create_issue(f"marks-{i}").check()
        a.push().check()
    assert remote_fence(cluster)["marks"] == [held] * 6  # 5 writes + the adopt sentinel
    assert mark_counts(a) == {held: 6}

    # The live writer prunes PUBLISHED marks (as of origin/main) while an adopt races it:
    # both delete the same rows, so the merge is clean and nothing conflicts.
    a.create_issue("marks-unpublished").check()
    _fetch(a)
    run_script(
        a,
        [
            "DELETE FROM bh_write_mark WHERE id IN "
            "(SELECT id FROM bh_write_mark AS OF 'origin/main')"
        ],
    ).check()
    a.commit("bh: prune published marks").check()
    assert mark_counts(a) == {held: 1}  # only the unpublished write's mark is left
    new = cluster.hq.place("c", expected_epoch=held).epoch
    assert adopt(cluster, c, new) == "landed"
    with recorder.step("marks:safe-prune-stale", "a"):
        pulled = _evidence(recorder, "marks:safe-prune-pull", "a", a.pull())
    assert not pulled.ok and SETTLE_REFUSAL in pulled.output  # the unpublished mark fences
    assert local_state(a)["conflicts"] == []
    assert "marks-unpublished" not in remote_fence(cluster)["titles"]

    # The hazard: a stale writer that deletes its UNPUBLISHED marks escapes the fence.
    _make_stale(cluster, "a", "c")
    a.create_issue("marks-unsafe-prune").check()
    run_script(a, ["DELETE FROM bh_write_mark"]).check()
    a.commit("prune every mark (unsafe)").check()
    with recorder.step("marks:unsafe-prune-stale", "a"):
        pulled = _evidence(recorder, "marks:unsafe-prune-pull", "a", a.pull())
        pushed = _evidence(recorder, "marks:unsafe-prune-push", "a", a.push())
    assert pulled.ok and pushed.ok, (pulled.output, pushed.output)
    assert "marks-unsafe-prune" in remote_fence(cluster)["titles"]


# =============================================================================================
# Remote types — is a non-fast-forward rejection a true CAS on file:// too?
# =============================================================================================


def _cli(root: Path):
    env = {**os.environ, "DOLT_ROOT_PATH": str(root), "HOME": str(root)}
    root.mkdir(parents=True, exist_ok=True)
    (root / ".dolt").mkdir(exist_ok=True)
    (root / ".dolt" / "config_global.json").write_text(
        '{"user.name": "spike", "user.email": "spike@frames.invalid", "metrics.disabled": "true"}'
    )

    def dolt(*args, cwd: Path, check=True):
        res = subprocess.run(
            ["dolt", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=120
        )
        if check:
            assert res.returncode == 0, (args, res.stdout, res.stderr)
        return res

    return dolt, env


def test_file_remote_push_is_a_cas_concurrent_pushes_from_one_base_yield_one_winner(tmp_path):
    """Two clones commit on the same base and push at the same instant, 8 rounds: exactly one
    push wins each round and remote main is the winner's commit (Dolt's file manifest update is
    a lock-held compare of the last lock hash: store/nbs/file_manifest.go)."""
    dolt, env = _cli(tmp_path / "dolt-root")
    remote, seed = tmp_path / "remote", tmp_path / "seed"
    remote.mkdir()
    seed.mkdir()
    dolt("init", "-b", "main", cwd=seed)
    dolt("sql", "-q", "CREATE TABLE t (id VARCHAR(16) PRIMARY KEY)", cwd=seed)
    dolt("add", "-A", cwd=seed)
    dolt("commit", "-m", "seed", cwd=seed)
    dolt("remote", "add", "origin", f"file://{remote}", cwd=seed)
    dolt("push", "origin", "main", cwd=seed)
    clones = []
    for name in ("x", "y"):
        dolt("clone", f"file://{remote}", str(tmp_path / name), cwd=tmp_path)
        clones.append(tmp_path / name)
    observer = tmp_path / "observer"
    dolt("clone", f"file://{remote}", str(observer), cwd=tmp_path)
    for rnd in range(8):
        heads = {}
        for clone in clones:
            dolt("fetch", "origin", cwd=clone)
            dolt("reset", "--hard", "origin/main", cwd=clone)
            dolt("sql", "-q", f"INSERT INTO t VALUES ('{clone.name}-{rnd}')", cwd=clone)
            dolt("commit", "-am", f"{clone.name} {rnd}", cwd=clone)
            heads[clone.name] = dolt("sql", "-r", "csv", "-q", "select hashof('main')", cwd=clone)
        procs = {
            clone.name: subprocess.Popen(
                ["dolt", "push", "origin", "main"],
                cwd=clone,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for clone in clones
        }
        results = {name: (p.wait(120), p.stdout.read()) for name, p in procs.items()}
        winners = [name for name, (code, _) in results.items() if code == 0]
        assert len(winners) == 1, results
        loser = next(name for name in results if name not in winners)
        assert NON_FF in results[loser][1], results[loser][1]
        dolt("fetch", "origin", cwd=observer)
        main = dolt("sql", "-r", "csv", "-q", "select hashof('origin/main')", cwd=observer)
        assert main.stdout.splitlines()[1] == heads[winners[0]].stdout.splitlines()[1]
