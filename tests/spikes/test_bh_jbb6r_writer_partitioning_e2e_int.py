"""Executable evidence for the bh-jbb6r spike; not a product contract test.

Question: composed together -- placement CAS, in-data epoch, write guard, the forward write
path and failover -- does the writer-partitioning prototype pass the proposal's full validation
table under fault injection, with acceptable latency, and with no stale write ever reaching
``main``? See ``docs/spikes/bh-jbb6r-writer-partitioning-e2e.md``.

The prototype is :mod:`harness.composed_fence` on the bh-eybn7 fixture: real bd 1.3 embedded
frames (bd's linked Dolt), bd 1.3 shared-server frames (a frame-private ``dolt sql-server``,
Dolt 2.3.5) and Dolt CLI frames on ONE ``git+file://`` hive remote, with HQ placement on an owned
``dolt sql-server`` (bh-cvk70 design A) or a bare git repo (``refs/bh/lease/<prefix>``).

One test per proposal scenario (``docs/design/hive-writer-partitioning-proposal.md``,
"Validation approach"), a negative control proving the invariant checker is not vacuous, and a
FIXED-seed randomized interleaving pass (at most 10 seeds here; the >= 500-seed soak runs outside
the land gate via ``tests/spikes/bh_jbb6r_soak.py``). Every scenario ends with
:func:`harness.composed_fence.check_invariants` on remote ``main``.

``BH_JBB6R_EVIDENCE=<file>`` appends measured latencies and outcomes as JSON lines;
``BH_SPIKE_TRACE_DIR=<dir>`` keeps each scenario's Recorder trace.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from harness import composed_fence as cf
from harness import epoch_fence as ef
from harness import write_guard as wg
from harness.writer_fencing import (
    BD_EMBEDDED,
    BD_SERVER,
    CAS,
    DOLT_CLI,
    HQUnreachable,
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

FRAMES = [("a", BD_EMBEDDED), ("b", BD_SERVER), ("c", DOLT_CLI)]
SRC = Path(__file__).resolve().parents[2] / "src"
NON_FF = cf.NON_FF
#: bd's embedded settle gate / Dolt's merge report, the two faces of the epoch foreign key.
FK_REFUSALS = (
    "pull merge left constraint violations bd cannot auto-repair",
    "CONSTRAINT VIOLATION",
    "constraint violation",
)
#: The fixed seeds the integration gate runs (the soak runs >= 500 outside it).
FIXED_SEEDS = (0, 1, 2, 3)


def _evidence(name: str, data: object) -> None:
    line = json.dumps({"test": name, "data": data}, sort_keys=True, default=str)
    print(f"BH_JBB6R {line}")
    target = os.environ.get("BH_JBB6R_EVIDENCE")
    if target:
        with Path(target).open("a") as fh:
            fh.write(line + "\n")


def _recorder(world: cf.World) -> Recorder:
    trace = None
    if os.environ.get("BH_SPIKE_TRACE_DIR"):
        test = os.environ.get("PYTEST_CURRENT_TEST", "trace").split("::")[-1].split(" ")[0]
        trace = Path(os.environ["BH_SPIKE_TRACE_DIR"]) / f"{test}.jsonl"
        trace.parent.mkdir(parents=True, exist_ok=True)
        trace.unlink(missing_ok=True)
    return Recorder(
        world.cluster, trace, remote_tables=("bh_writer", "bh_epoch_live", "bh_write_mark")
    )


def _clean(world: cf.World, name: str, **kw) -> dict:
    """Every scenario ends here: all invariants on remote main, or the failing step's data."""
    result = cf.check_invariants(world, **kw)
    _evidence(f"{name}:invariants", {k: v for k, v in result.items() if k != "audit"})
    assert result["ok"], json.dumps(result["violations"], indent=1, default=str)
    return result


def _refused(run: Run, *needles: str) -> str:
    assert not run.ok, f"accepted: {run.output[-400:]}"
    assert any(n in run.output for n in needles), run.output[-800:]
    return run.output.strip().splitlines()[-1][:200] if run.output.strip() else ""


def _take(world: cf.World, name: str, **kw) -> cf.AdoptResult:
    """Director places ``name`` and ``name`` runs adopt step 2 to completion."""
    placed = world.hq.place(name)
    result = cf.adopt(world.cluster, world[name], placed.epoch, **kw)
    world.moves.observe("adopt", name)
    assert result.landed, result
    return result


def _push(world: cf.World, name: str) -> str:
    out = cf.managed_push(world[name])
    world.moves.observe("managed_push", name)
    return out


def _main(world: cf.World) -> str | None:
    return world.cluster.remote.view(("bh_writer",))["main"]


def _make_stale(world: cf.World, stale: str, adopter: str, title: str) -> int:
    """``stale`` becomes the writer and commits one unpublished write; then ``adopter`` takes
    over. Returns the stale frame's held epoch."""
    for frame in world.cluster.frames.values():
        (frame.hive / ".beads" / "push-state.json").unlink(missing_ok=True)
    _take(world, stale)
    held = cf.committed_writer(world[stale])[1]
    assert cf.write(world[stale], title).ok
    _take(world, adopter)
    return held


# =============================================================================================
# Scenario 1 -- planned handoff while the old writer is idle
# =============================================================================================


def test_s1_planned_handoff_fences_the_idle_old_writer(tmp_path):
    """B's bump lands; A's next push is rejected; A's local main writes fail. Old writer bd
    embedded then bd server. Measures adopt latency and handoff downtime."""
    with cf.composed_world(tmp_path, FRAMES) as w:
        rec = _recorder(w)
        measured = []
        for old_name, new_name in (("a", "b"), ("b", "c")):
            old, new = w[old_name], w[new_name]
            assert cf.write(old, f"s1-{old_name}-legit").ok
            assert _push(w, old_name) == "pushed"
            with rec.step(f"s1:{old_name}->{new_name}:handoff", new_name) as step:
                t0 = time.monotonic()
                placed = w.hq.place(new_name, expected_epoch=w.hq.placement().epoch)
                t_cas = time.monotonic()
                adopted = cf.adopt(w.cluster, new, placed.epoch)
                t_bump = time.monotonic()
                w.moves.observe("adopt", new_name)
                assert adopted.landed, adopted
                assert cf.write(new, f"s1-{new_name}-first").ok
                assert _push(w, new_name) == "pushed"
                t_first = time.monotonic()
            remote = ef.remote_fence(w.cluster)
            assert remote["writer"] == (new_name, placed.epoch)
            assert remote["live"] == [placed.epoch] and set(remote["marks"]) == {placed.epoch}
            assert step["after"]["remote"]["main"] != step["before"]["remote"]["main"]
            measured.append(
                {
                    "handoff": f"{old.kind}->{new.kind}",
                    "placement_cas_s": round(t_cas - t0, 3),
                    "adopt_step2_s": adopted.seconds,
                    "adopt_total_s": round(t_bump - t0, 3),
                    "handoff_downtime_s": round(t_first - t0, 3),
                }
            )

            # A has not seen the bump: its local guard still names A, its push is non-ff.
            assert cf.write(old, f"s1-{old_name}-late").ok
            with rec.step(f"s1:{old_name}:next-push", old_name) as late:
                pushed = old.push()
                w.moves.observe("raw_push", old_name)
            _refused(pushed, NON_FF)
            assert late["after"]["remote"]["main"] == late["before"]["remote"]["main"]
            # The managed path sees the newer epoch, diverts and resets: now the guard refuses.
            assert cf.divert(old)["diverted"]
            refused = cf.write(old, f"s1-{old_name}-after")
            _refused(refused, wg.GUARD_REFUSAL)
            assert cf.local_writer(old) == (new_name, placed.epoch)
            titles = ef.remote_fence(w.cluster)["titles"]
            assert not {f"s1-{old_name}-late", f"s1-{old_name}-after"} & titles
        _evidence("s1:latency", measured)
        _clean(w, "s1")


# =============================================================================================
# Scenario 2 -- handoff while the old writer is mid-write
# =============================================================================================


def test_s2_handoff_mid_write_lands_before_the_bump_or_is_rejected(tmp_path):
    """The old writer is parked at its push CAS while the adopter runs. Bump first: the parked
    push is rejected. Write first: it lands BELOW the bump (adopt retries from the new head)."""
    with cf.composed_world(tmp_path, FRAMES) as w:
        rec = _recorder(w)
        c = w["c"]
        for name in ("a", "b"):
            old = w[name]
            for order in ("bump-first", "write-first"):
                _take(w, name)
                title = f"s2-{name}-{order}"
                assert cf.write(old, title).ok
                if old.kind == BD_SERVER:  # the managed path commits the mark with the write
                    old.bd("dolt", "commit", "-m", "bh: commit working set before managed push")
                hold = old.hold(CAS)
                pending = old.push_async()
                hold.wait()
                placed = w.hq.place("c")
                released: list[Run] = []

                def release(hold=hold, pending=pending, released=released, old=old):
                    hold.release()
                    released.append(pending.result())
                    if released[-1].ok:
                        w.moves.landed("parked_push", old)

                with rec.step(f"s2:{name}:{order}", "c"):
                    result = cf.adopt(
                        w.cluster,
                        c,
                        placed.epoch,
                        before_push=release if order == "write-first" else None,
                    )
                    if order == "bump-first":
                        release()
                    if result.landed:
                        w.moves.landed("adopt", c)
                    w.moves.observe("s2", None)
                assert result.landed, result
                remote = ef.remote_fence(w.cluster)
                assert remote["writer"] == ("c", placed.epoch)
                if order == "bump-first":
                    _refused(released[0], NON_FF)
                    assert title not in remote["titles"]
                    assert cf.divert(old)["diverted"]
                else:
                    assert released[0].ok, released[0].output
                    assert title in remote["titles"]
                    assert result.attempts >= 2  # the adopter lost the race and retried on top
                    ef.sync_to_remote(old)
                _evidence(
                    f"s2:{name}:{order}",
                    {
                        "old_push": released[0].returncode,
                        "adopt": result.outcome,
                        "attempts": result.attempts,
                        "adopt_s": result.seconds,
                    },
                )
        _clean(w, "s2")


# =============================================================================================
# Scenario 3 -- partitioned old writer, failover, rejoin
# =============================================================================================


def test_s3_partitioned_writer_is_fenced_and_its_work_survives_on_an_orphan(tmp_path):
    with cf.composed_world(tmp_path, FRAMES) as w:
        rec = _recorder(w)
        c = w["c"]
        for name in ("a", "b"):
            old = w[name]
            _take(w, name)
            assert cf.write(old, f"s3-{name}-published").ok
            assert _push(w, name) == "pushed"
            unpublished = {f"s3-{name}-unpublished-{i}" for i in (1, 2)}
            with rec.step(f"s3:{name}:partition", name):
                old.partition()
                w.hq.partition(name)
                for title in sorted(unpublished):
                    assert cf.write(old, title).ok  # local writes keep working
                cut = cf.managed_push(old)
            assert cut == "unreachable"
            with pytest.raises(HQUnreachable):
                w.hq.placement(asker=name)
            _take(w, "c")  # the director fails over; the partitioned writer cannot see it

            with rec.step(f"s3:{name}:rejoin-raw-bd-sync", name) as raw:
                old.heal()
                w.hq.heal(name)
                synced = old.bd("sync")
                w.moves.observe("bd_sync", name)
            _refused(synced, *FK_REFUSALS)
            assert raw["after"]["remote"]["main"] == raw["before"]["remote"]["main"]

            diverted = cf.divert(old)
            assert diverted["diverted"], diverted
            assert unpublished <= (ef.remote_branch_titles(w.cluster, diverted["branch"]) or set())
            assert not unpublished & ef.remote_fence(w.cluster)["titles"]
            _refused(cf.write(old, f"s3-{name}-after-divert"), wg.GUARD_REFUSAL)

            with rec.step(f"s3:{name}:orphan-merge", "c"):
                assert cf.orphan_branches(c) == [diverted["branch"]]
                merged = ef.merge_orphan(c, diverted["branch"])
                assert merged["run"].ok, merged["run"].output
                assert merged["state"]["violations"] == [] and not merged["state"]["merging"]
                assert _push(w, "c") == "pushed"
            remote = ef.remote_fence(w.cluster)
            assert unpublished <= remote["titles"]
            assert set(remote["marks"]) == {remote["writer"][1]}
        result = _clean(w, "s3")
        assert result["orphan_admitted"] >= 4  # both orphans entered only via sanctioned merges


# =============================================================================================
# Scenario 4 -- two adopters race for the same epoch
# =============================================================================================


def test_s4_placement_cas_has_one_winner_in_both_hq_modes(tmp_path):
    """N spawned adopters CAS placement from the same token at once, in dolt-server mode
    (design A, director credential) and git mode (``refs/bh/lease/<prefix>``)."""
    sql = cf.SqlHQ(tmp_path / "sql", ["a"]).start()
    git = cf.GitHQ(tmp_path / "git").start()
    try:
        sql.place("a")
        git.place("a")
        rounds = []
        for hq in (sql, git):
            for round_ in range(3):
                before = hq.placement()
                frames = [f"r{round_}f{i}" for i in range(6)]
                started = time.monotonic()
                outcomes = cf.race_placement(hq, frames)
                winners = [f for f, ok, _ in outcomes if ok]
                assert len(winners) == 1, outcomes
                assert hq.placement() == cf.Placement(winners[0], before.epoch + 1)
                rounds.append(
                    {
                        "mode": hq.mode,
                        "losers": sorted(
                            {detail.split(" ")[0] for _, ok, detail in outcomes if not ok}
                        ),
                        "s": round(time.monotonic() - started, 2),
                    }
                )
        _evidence("s4:placement-race", rounds)
    finally:
        sql.close()
        git.close()


def test_s4_loser_that_reached_step_2_is_rejected_and_abandons(tmp_path):
    with cf.composed_world(tmp_path, FRAMES) as w:
        a, b = w["a"], w["b"]
        # a won epoch e at HQ and passed its HQ check; the director then fails over to b, whose
        # bump lands first. a's push is non-fast-forward; its retry sees b@e+1 in the data.
        epoch = w.hq.place("a").epoch

        def fail_over_to_b():
            later = w.hq.place("b", expected_epoch=epoch).epoch
            assert cf.adopt(w.cluster, b, later).landed
            w.moves.landed("adopt", b)

        outcome = cf.adopt(w.cluster, a, epoch, before_push=fail_over_to_b)
        w.moves.observe("s4", None)
        assert outcome.outcome == "abandoned: superseded in data", outcome
        assert ef.remote_fence(w.cluster)["writer"] == ("b", epoch + 1)
        _refused(cf.write(a, "s4-a-after-abandon"), wg.GUARD_REFUSAL)

        # Reverse order: the stale adopter's bump lands first and it writes once at its epoch;
        # the current adopter retries on top and retires that epoch; the stale frame is fenced.
        stale = w.hq.place("a").epoch
        current = w.hq.place("c", expected_epoch=stale).epoch

        def a_lands_first():
            ef.sync_to_remote(a)
            ef.run_script(a, cf.bump_statements("a", stale)).check()
            a.commit(f"{cf.ADOPT_PREFIX}a@{stale}").check()
            a.push().check()
            w.moves.landed("stale-adopt", a)
            assert cf.write(a, "s4-a-at-stale-epoch").ok
            a.push().check()
            w.moves.landed("stale-write", a)

        assert cf.adopt(w.cluster, w["c"], current, before_push=a_lands_first).landed
        w.moves.landed("adopt", w["c"])
        w.moves.observe("s4", None)
        remote = ef.remote_fence(w.cluster)
        assert remote["writer"] == ("c", current) and remote["live"] == [current]
        assert "s4-a-at-stale-epoch" in remote["titles"]  # below the bump: legitimate
        assert cf.write(a, "s4-a-stale").ok  # a's local data still names a
        _refused(a.pull(), *FK_REFUSALS)
        _refused(a.push(), NON_FF)
        w.moves.observe("s4-stale", "a")
        assert "s4-a-stale" not in ef.remote_fence(w.cluster)["titles"]
        cf.divert(a)
        _clean(w, "s4")


# =============================================================================================
# Scenario 5 -- a stale writer on every default bd path
# =============================================================================================

PATHS = ("bd-dolt-push", "bd-pull-then-push", "bd-sync", "auto-push")


def test_s5_stale_writer_never_lands_on_any_default_bd_path(tmp_path):
    with cf.composed_world(tmp_path, FRAMES) as w:
        rec = _recorder(w)
        table = []
        for stale in ("a", "b"):
            s = w[stale]
            for path in PATHS:
                title = f"s5-{stale}-{path}"
                if path == "auto-push":
                    _make_stale(w, stale, "c", f"s5-{stale}-{path}-seed")
                else:
                    _make_stale(w, stale, "c", title)
                runs: dict[str, Run] = {}
                with rec.step(f"s5:{stale}:{path}", stale) as step:
                    if path == "bd-dolt-push":
                        runs["push"] = s.push()
                    elif path == "bd-pull-then-push":
                        runs["pull"] = s.pull()
                        runs["push"] = s.push()
                    elif path == "bd-sync":
                        runs["sync"] = s.bd("sync")
                    else:
                        runs["create+auto-push"] = s.run(
                            "env",
                            "BD_DOLT_AUTO_PUSH=true",
                            "bd",
                            "create",
                            "--title",
                            title,
                            "-t",
                            "task",
                        )
                    w.moves.observe(path, stale)
                assert step["after"]["remote"]["main"] == step["before"]["remote"]["main"], path
                assert title not in ef.remote_fence(w.cluster)["titles"]
                if path == "auto-push":
                    run = runs["create+auto-push"]
                    assert "auto-push failed" in run.output and NON_FF in run.output, run.output
                else:
                    last = runs.get("push") or runs["sync"]
                    assert not last.ok
                    if "pull" in runs:
                        _refused(runs["pull"], *FK_REFUSALS)
                    if path == "bd-sync":
                        _refused(last, *FK_REFUSALS)
                table.append(
                    {
                        "stale": s.kind,
                        "path": path,
                        **{
                            k: v.output.strip().splitlines()[-1][:120]
                            if v.output.strip()
                            else v.returncode
                            for k, v in runs.items()
                        },
                    }
                )
                cf.divert(s)  # the managed rejoin keeps the work on an orphan branch
        _evidence("s5:paths", table)
        _clean(w, "s5")


# =============================================================================================
# Scenario 6 -- hive_sync --strategy ours on a stale replica
# =============================================================================================


def _hive_sync(frame, strategy: str) -> Run:
    """bh's own hive_sync engine call (``BdEngine.sync_state`` -> ``bd federation sync``)."""
    code = (
        "import sys; from beadhive.engine import BdEngine; "
        "print(repr(BdEngine().sync_state(sys.argv[1], peer='hub', strategy=sys.argv[2] or None)))"
    )
    return frame.run(
        "env",
        f"PYTHONPATH={SRC}",
        os.environ.get("PYTHON", "python3"),
        "-c",
        code,
        str(frame.hive),
        strategy,
    )


def _reset_to(frame, commit: str) -> None:
    if frame.kind == BD_SERVER:
        frame.bd("sql", f"CALL DOLT_RESET('--hard', '{commit}')").check()
    else:
        frame.dolt("merge", "--abort")
        frame.dolt("reset", "--hard", commit).check()


def test_s6_hive_sync_strategy_ours_never_reverts_bh_writer(tmp_path):
    with cf.composed_world(tmp_path, FRAMES) as w:
        for name in ("a", "b"):
            w[name].bd("federation", "add-peer", "hub", w.cluster.remote.url).check()
        evidence = []
        for name in ("a", "b"):
            s = w[name]
            # one-sided: a stale WRITER with an unpublished write
            _make_stale(w, name, "c", f"s6-{name}-stale")
            before = _main(w)
            synced = _hive_sync(s, "ours")
            w.moves.observe("hive_sync", name)
            assert _main(w) == before and ef.remote_fence(w.cluster)["writer"][0] == "c"
            evidence.append({"frame": s.kind, "case": "stale-writer", "out": synced.output[-160:]})
            cf.divert(s)

            # both-changed: a stale ADOPTER holding its own bump commit
            ef.sync_to_remote(s)
            stale = w.hq.place(name).epoch
            ef.run_script(s, cf.bump_statements(name, stale)).check()
            s.commit(f"{cf.ADOPT_PREFIX}{name}@{stale}").check()
            stale_head = s.local_main()
            _take(w, "c")
            for label, run in (
                ("hive-sync-ours", lambda s=s: _hive_sync(s, "ours")),
                ("bd-sync", lambda s=s: s.bd("sync")),
                ("bd-dolt-pull", lambda s=s: s.pull()),
            ):
                before = _main(w)
                out = run()
                w.moves.observe(label, name)
                assert _main(w) == before, label
                writer = ef.remote_fence(w.cluster)["writer"]
                assert writer[0] == "c", (label, writer)
                evidence.append(
                    {
                        "frame": s.kind,
                        "case": label,
                        "rc": out.returncode,
                        "out": out.output.strip()[-160:],
                    }
                )
                _reset_to(s, stale_head)  # the next path starts from the same stale adopter
            ef.sync_to_remote(s)
            # re-running the old bump on top of the new head is refused locally (monotonic)
            regress = ef.run_script(s, cf.bump_statements(name, stale))
            _refused(regress, ef.MONOTONIC_REFUSAL)
        _evidence("s6", evidence)
        _clean(w, "s6")


# =============================================================================================
# Scenario 7 -- HQ unreachable for longer than the lease TTL
# =============================================================================================


def _outage(w: cf.World, writer: str, other: str, seconds: float) -> dict:
    """HQ down for ``seconds``: the placed writer keeps writing; handoff is refused."""
    started = time.monotonic()
    w.hq.down()
    writes = refused_place = refused_adopt = 0
    try:
        while time.monotonic() - started < seconds or writes < 3:
            assert cf.write(w[writer], f"s7-{w.hq.mode}-{writes}").ok
            assert _push(w, writer) == "pushed"
            writes += 1
            with pytest.raises((HQUnreachable, ValueError)):
                w.hq.place(other)
            refused_place += 1
            if refused_adopt == 0:
                current = ef.remote_fence(w.cluster)["writer"][1]
                result = cf.adopt(w.cluster, w[other], current + 1)
                assert result.outcome == "abandoned: hq unreachable", result
                refused_adopt += 1
    finally:
        w.hq.resume()
    return {
        "mode": w.hq.mode,
        "outage_s": round(time.monotonic() - started, 1),
        "writes_landed": writes,
        "handoffs_refused": refused_place + refused_adopt,
    }


def test_s7_hq_down_past_the_ttl_keeps_the_primary_writing_and_refuses_handoff(tmp_path):
    """dolt-server HQ: session TTL 2 s, failover_after 3 s, outage 5 s. Writes land throughout;
    every placement CAS fails; on recovery the director's observer rule decides nothing."""
    with cf.composed_world(
        tmp_path / "sql", [("a", BD_EMBEDDED), ("c", DOLT_CLI)], session_ttl_ms=2000
    ) as w:
        renewer = cf.Renewer(w.hq, "a", 0.3)
        try:
            director = cf.Director(w.hq, failover_after=3.0)
            assert not director.due()
            before = w.hq.placement()
            sql = _outage(w, "a", "c", 5.0)
            stale = w.hq.staleness("a")
            assert director.due() is False  # it observed none of the outage
            time.sleep(0.8)  # a renews on its cadence once HQ is back
            assert w.hq.staleness("a") < 2.0 and not director.due()
            assert w.hq.placement() == before
            sql["server_staleness_at_recovery_s"] = stale
        finally:
            renewer.stop()
        _clean(w, "s7-sql")
    with cf.composed_world(
        tmp_path / "git", [("a", BD_EMBEDDED), ("c", DOLT_CLI)], hq_mode="git"
    ) as w:
        before = w.hq.placement()
        git = _outage(w, "a", "c", 3.0)
        assert w.hq.placement() == before
        _clean(w, "s7-git")
    _evidence("s7", [sql, git])


# =============================================================================================
# Scenario 8 -- forward a claim; push a branch that merges through the primary
# =============================================================================================


def test_s8_forwarded_claims_have_one_winner_and_a_branch_merges_through_the_primary(tmp_path):
    rounds = int(os.environ.get("BH_JBB6R_CLAIM_ROUNDS", "5"))
    racers = int(os.environ.get("BH_JBB6R_CLAIM_RACERS", "4"))
    frames = [("q", BD_EMBEDDED), ("p", BD_SERVER), ("f", DOLT_CLI)]
    with cf.composed_world(tmp_path, frames) as w:
        p, q, f = w["p"], w["q"], w["f"]
        _take(w, "p")
        fwd = cf.Forwarder(f, p)
        latency: dict[str, list[float]] = {"forward_ack": [], "forward_on_main": []}
        latency |= {"primary_on_main": [], "branch_on_main": []}

        for i in range(3):  # forward: ack = committed on the primary; on_main = after its push
            t0 = time.monotonic()
            made = fwd.bd("create", "--title", f"s8-fwd-{i}", "-t", "task", "--json", actor="f")
            assert made.returncode == 0, made.stdout + made.stderr
            latency["forward_ack"].append(round(time.monotonic() - t0, 3))
            assert _push(w, "p") == "pushed"
            latency["forward_on_main"].append(round(time.monotonic() - t0, 3))
            t0 = time.monotonic()
            assert cf.write(p, f"s8-primary-{i}").ok
            assert _push(w, "p") == "pushed"
            latency["primary_on_main"].append(round(time.monotonic() - t0, 3))

        beads = [
            json.loads(fwd.bd("create", "--title", f"s8-race-{i}", "-t", "task", "--json").stdout)[
                "id"
            ]
            for i in range(rounds)
        ]
        exit0 = []
        for n, bead in enumerate(beads):
            codes = fwd.race(bead, [f"w{i}" for i in range(racers)], f.dir / f"gate-{n}")
            row = json.loads(fwd.bd("show", bead, "--json").stdout)[0]
            winners = [a for a, code in codes.items() if code == 0]
            exit0.append(len(winners))
            assert winners == [row["assignee"]] and cf.claim_won(row, winners[0]), (codes, row)
        assert exit0 == [1] * rounds
        assert _push(w, "p") == "pushed"

        # Branch path: q is a branch-role frame; non-allocating edits only (bh-sieai R2), on a
        # frame-private data ref so even its raw push cannot touch refs/dolt/data.
        wg.provision(q, role="branch")
        url = w.cluster.remote.url
        q.dolt("remote", "add", "--ref", "refs/dolt/frame/q", "branchout", url).check()
        # The trailing slash is load-bearing: the Dolt 2.3.5 sql-server reuses its open remote
        # for an identical URL and ignores the new remote's --ref, so fetching `frame-q` with
        # the same URL as origin silently re-reads refs/dolt/data (measured; see the doc).
        added = p.bd(
            "sql", f"CALL DOLT_REMOTE('add', '--ref', 'refs/dolt/frame/q', 'frame-q', '{url}/')"
        )
        added.check()
        for i in range(2):
            q.pull().check()
            t0 = time.monotonic()
            q.bd("label", "add", beads[0], f"branch-{i}").check()
            q.bd("comment", beads[0], f"s8-branch-comment-{i}").check()
            q.dolt("push", "--force", "branchout", "main").check()
            p.bd("dolt", "commit", "-m", "bh: commit working set before merge")
            merged = cf.merge_branch(p, "frame-q", "main", f"q/{i}")
            assert merged.ok, merged.output
            assert _push(w, "p") == "pushed"
            latency["branch_on_main"].append(round(time.monotonic() - t0, 3))
        view = w.cluster.remote.view(("labels",))
        labels = sorted({r["label"] for r in view["tables"]["labels"] or []})
        assert {"branch-0", "branch-1"} <= set(labels)
        wg.provision(q)  # back to an ordinary replica
        _evidence("s8", {"exit0_per_round": exit0, "latency": latency, "labels": labels})
        _clean(w, "s8")


# =============================================================================================
# Scenario 9 -- frame killed; session goes stale; failover; reclaim exactly once
# =============================================================================================


def test_s9_killed_primary_fails_over_and_its_claims_are_reclaimed_exactly_once(tmp_path):
    frames = [("q", BD_EMBEDDED), ("p", BD_SERVER), ("f", DOLT_CLI)]
    with cf.composed_world(tmp_path, frames, session_ttl_ms=2000) as w:
        p, q, f = w["p"], w["q"], w["f"]
        _take(w, "p")
        renewers = {n: cf.Renewer(w.hq, n, 0.3) for n in ("p", "q")}
        try:
            fwd = cf.Forwarder(f, p)
            claimed = []
            for i in range(3):
                bead = json.loads(
                    fwd.bd("create", "--title", f"s9-{i}", "-t", "task", "--json").stdout
                )["id"]
                assert fwd.bd("update", bead, "--claim", actor=f"w{i}").returncode == 0
                claimed.append(bead)
            assert _push(w, "p") == "pushed"
            director = cf.Director(w.hq, failover_after=3.0)
            time.sleep(1.0)
            assert not director.due()

            p.kill()
            renewers["p"].kill()
            killed = time.monotonic()
            while not director.due():
                assert time.monotonic() - killed < 30, "failover never became due"
                time.sleep(0.2)
            detected = time.monotonic() - killed
            assert detected >= 3.0
            placed = director.failover("q")
            assert w.hq.placement() == cf.Placement("q", placed.epoch)
            result = cf.adopt(w.cluster, q, placed.epoch, reclaim=True)
            w.moves.observe("failover-adopt", "q")
            assert result.landed and result.reclaimed == sorted(claimed), result
            remote = w.cluster.remote.view(("issues",))["tables"]["issues"]
            status = {r["id"]: r["status"] for r in remote}
            assert all(status[b] == "open" for b in claimed), status

            # A new claim granted by the new primary survives a re-run of the same adopt.
            q.bd("update", claimed[0], "--claim").check()
            assert _push(w, "q") == "pushed"
            again = cf.adopt(w.cluster, q, placed.epoch, reclaim=True)
            assert again.outcome == "landed" and again.reclaimed == []
            assert (
                "in_progress"
                == {
                    r["id"]: r["status"]
                    for r in w.cluster.remote.view(("issues",))["tables"]["issues"]
                }[claimed[0]]
            )
            reverts = cf._observer_docs(
                w.cluster,
                "SELECT to_id, COUNT(*) AS n FROM dolt_diff_issues "
                "WHERE from_status = 'in_progress' AND to_status = 'open' GROUP BY to_id",
            )[0]
            assert {r["to_id"]: int(r["n"]) for r in reverts} == {b: 1 for b in claimed}
            time.sleep(1.0)
            assert not director.due()  # q renews: no second failover

            # The dead primary comes back stale: a forwarder still pointed at it fails closed.
            p.restart()
            p.pull().check()
            late = fwd.bd("create", "--title", "s9-late", "-t", "task", actor="f")
            assert late.returncode != 0 and wg.GUARD_REFUSAL in late.stdout + late.stderr
            _evidence(
                "s9",
                {
                    "failover_after_s": 3.0,
                    "detected_after_kill_s": round(detected, 2),
                    "adopt_with_reclaim_s": result.seconds,
                    "reclaimed": len(result.reclaimed),
                },
            )
        finally:
            for renewer in renewers.values():
                renewer.stop()
        _clean(w, "s9")


# =============================================================================================
# Scenario 10 -- conformance probe slower than the session TTL
# =============================================================================================


def test_s10_slow_probe_keeps_the_session_fresh_and_only_expired_evidence_blocks(tmp_path):
    with cf.composed_world(
        tmp_path,
        [("a", BD_EMBEDDED), ("c", DOLT_CLI)],
        session_ttl_ms=3000,
        evidence_ttl_ms=1500,
    ) as w:
        renewer = cf.Renewer(w.hq, "a", 0.3)
        try:
            director = cf.Director(w.hq, failover_after=2.0)
            w.hq.publish_evidence("a")
            time.sleep(0.6)
            assert all(w.hq.eligibility("a").values())

            done = threading.Event()

            def probe():  # a measurement that takes twice the session TTL
                time.sleep(6.0)
                w.hq.publish_evidence("a")
                done.set()

            thread = threading.Thread(target=probe)
            thread.start()
            samples, writes = [], 0
            while not done.is_set():
                samples.append(w.hq.eligibility("a"))
                assert not director.due()
                if writes < 2:  # the write path does not consult evidence at all
                    assert cf.write(w["a"], f"s10-{writes}").ok
                    assert _push(w, "a") == "pushed"
                    writes += 1
                time.sleep(0.1)
            thread.join()
            assert all(s["session_fresh"] for s in samples)
            blocked = [frozenset(k for k, v in s.items() if not v) for s in samples]
            assert frozenset({"evidence_unexpired"}) in blocked
            assert all(b <= {"evidence_unexpired"} for b in blocked), blocked
            assert all(w.hq.eligibility("a").values())
            _evidence(
                "s10",
                {
                    "samples": len(samples),
                    "blocked_by_evidence_only": blocked.count(frozenset({"evidence_unexpired"})),
                    "writes_during_probe": writes,
                },
            )
        finally:
            renewer.stop()
        _clean(w, "s10")


# =============================================================================================
# The checker is not vacuous; fixed-seed randomized interleavings
# =============================================================================================


def test_invariant_checker_flags_a_stale_write_forced_past_the_fence(tmp_path):
    """Negative control: a stale Dolt CLI writer commits a refused pull with ``--force`` (raw
    Dolt, break-glass; bh-vje85 E11) and pushes. Every invariant that should see it does."""
    with cf.composed_world(tmp_path, [("a", BD_EMBEDDED), ("c", DOLT_CLI)]) as w:
        c = w["c"]
        _make_stale(w, "c", "a", "neg-stale")
        _refused(c.dolt("pull", "origin", "main"), *FK_REFUSALS)
        c.dolt("add", "-A").check()
        c.dolt("commit", "--force", "-m", "stale merge forced past the fence").check()
        c.push().check()
        w.moves.observe("forced", "c")
        result = cf.check_invariants(w)
        _evidence("negative-control", result["violations"])
        assert not result["ok"]
        for key in (
            "i1_one_writer_per_epoch",
            "i3_late_epoch_commit",
            "i4_retired_mark_in_merge",
            "audit",
        ):
            assert result["violations"].get(key), (key, result["violations"])


def test_fixed_seed_randomized_interleavings_hold_every_invariant(tmp_path):
    """A handful of fixed seeds of the soak's schedule driver (adopt / push / stale pull+push
    through bd / bd sync / auto-push / partition / kill / mid-push handoff / push race / divert /
    orphan merge / HQ outage), each followed by recovery and the full invariant check."""
    seeds = [int(s) for s in os.environ.get("BH_JBB6R_SEEDS", "").split(",") if s] or FIXED_SEEDS
    with cf.composed_world(tmp_path, FRAMES) as w:
        assert cf.write(w["a"], "warm-a").ok and _push(w, "a") == "pushed"
        _take(w, "b")
        assert cf.write(w["b"], "warm-b").ok and _push(w, "b") == "pushed"
        cp = cf.checkpoint_world(w)
        failures = []
        for seed in seeds:
            schedule = cf.run_schedule(w, seed)
            line = {
                "seed": seed,
                "s": schedule.seconds,
                "error": schedule.error,
                "events": [e["event"] for e in schedule.events],
            }
            if schedule.error or not (schedule.result or {}).get("ok"):
                failures.append(
                    {
                        **line,
                        "detail": schedule.events,
                        "violations": (schedule.result or {}).get("violations"),
                    }
                )
            _evidence("random:fixed-seed", line)
            cf.rewind_world(w, cp)
        assert not failures, json.dumps(failures, indent=1, default=str)
