"""Integration: `coordination.py`'s wrappers against a REAL `bd` binary (bh-c6dk.3) — the
properties runtime tiers actually depend on, which a mocked `bd._run` cannot prove:

  - merge-slot acquire is genuinely EXCLUSIVE under concurrency: two simultaneous acquires
    against the same real store yield exactly one holder and one queued waiter — proven with
    real OS threads racing real `bd` subprocesses, not a canned return-code sequence.
  - `gate check` resolves timer/bead gates and LEAVES a `human` gate open, on a hive carrying
    all three at once (a mock could pass this by only ever asserting one gate's fate).
  - `gate resolve` on an already-closed gate is a no-op, not an error.
  - `merge-slot release` with the wrong holder is refused.
  - a STALE lease is reclaimed by `bd reclaim` and a LIVE (heartbeated) one is not — the
    headline durability property (5-minute lease TTL; `bd reclaim` is the only recovery when a
    holder dies). Waiting out a real 5-minute TTL is infeasible in a test, so this one drives a
    `--server`-mode store and backdates `lease_expires_at` directly via `bd sql` (only
    reachable in server mode — embedded mode refuses `bd sql`) rather than faking `bd`'s own
    judgment; `bd reclaim` still makes the real staleness call.

Marked `integration` (spins up real `bd`/Dolt) + self-skips without a `bd` binary on PATH, per
this repo's marker convention.
"""

from __future__ import annotations

import threading
import time

import pytest

from beadhive import coordination as coord
from beadhive.run import run
from harness.beads import bd, bd_json, create, init_embedded, skip_if_no_bd
from harness.world import reap_dolt_server

pytestmark = [pytest.mark.integration, skip_if_no_bd]

_TIMEOUT = 60


def _init(path, prefix):
    path.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-q", "-b", "main"], cwd=str(path), check=True, capture=True)
    init_embedded(path, prefix)


# ---- merge-slot: exclusivity under real concurrency ------------------------------------------


def test_merge_slot_acquire_is_exclusive_under_concurrency(tmp_path):
    _init(tmp_path, "cc")
    assert coord.merge_slot_create(tmp_path) is True

    outcomes: dict[str, coord.SlotAcquireResult] = {}

    def go(name):
        outcomes[name] = coord.merge_slot_acquire(tmp_path, name, wait=True)

    t1 = threading.Thread(target=go, args=("agentA",))
    t2 = threading.Thread(target=go, args=("agentB",))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    winners = [n for n, r in outcomes.items() if r.acquired]
    losers = [n for n, r in outcomes.items() if not r.acquired]
    assert len(winners) == 1, outcomes  # EXACTLY one holder, never zero, never both
    assert len(losers) == 1, outcomes
    loser = outcomes[losers[0]]
    assert loser.waiting is True  # the loser queued, it wasn't just refused
    assert loser.position == 1

    status = coord.merge_slot_check(tmp_path)
    assert status.held is True
    assert status.holder == winners[0]
    assert status.waiters == (losers[0],)  # EXACTLY one queued waiter, no duplicates/drops


def test_merge_slot_release_wrong_holder_fails_for_real(tmp_path):
    _init(tmp_path, "cc")
    coord.merge_slot_create(tmp_path)
    won = coord.merge_slot_acquire(tmp_path, "real-holder")
    assert won.acquired is True

    refused = coord.merge_slot_release(tmp_path, holder="impostor")
    assert refused.ok is False
    assert "real-holder" in refused.error  # names the ACTUAL holder, not a generic refusal

    # the slot is provably still held — the failed release changed nothing
    assert coord.merge_slot_check(tmp_path).holder == "real-holder"

    released = coord.merge_slot_release(tmp_path, holder="real-holder")
    assert released.ok is True
    assert coord.merge_slot_check(tmp_path).held is False


# ---- gate: check resolves timer/bead, leaves human open ---------------------------------------


def test_gate_check_resolves_timer_and_bead_gates_leaves_human_open(tmp_path):
    _init(tmp_path, "gg")
    human_target = create(tmp_path, "blocked by human gate")
    timer_target = create(tmp_path, "blocked by timer gate")
    bead_target = create(tmp_path, "blocked by bead gate")
    watched = create(tmp_path, "the watched bead")
    bd("close", watched, cwd=tmp_path, capture=True)  # so the bead gate's condition is already met

    human = coord.gate_create(tmp_path, blocks=human_target, gate_type="human", reason="review")
    timer = coord.gate_create(tmp_path, blocks=timer_target, gate_type="timer", timeout="1ns")
    beadg = coord.gate_create(tmp_path, blocks=bead_target, gate_type="bead", await_id=watched)
    assert human.ok and timer.ok and beadg.ok

    time.sleep(1)  # let the 1ns timer actually elapse in wall-clock terms

    result = coord.gate_check(tmp_path)
    assert result.ok is True
    assert result.resolved == 2  # timer + bead, NOT human

    def gate_status(gate_id):
        rows = bd_json("gate", "list", "--all", cwd=tmp_path) or []
        return next(r["status"] for r in rows if r["id"] == gate_id)

    assert gate_status(timer.gate_id) == "closed"
    assert gate_status(beadg.gate_id) == "closed"
    assert gate_status(human.gate_id) == "open"  # the property: human gates are left alone


def test_gate_resolve_already_resolved_is_a_noop_not_an_error(tmp_path):
    _init(tmp_path, "gg")
    target = create(tmp_path, "gated bead")
    g = coord.gate_create(tmp_path, blocks=target, gate_type="human", reason="first reason")
    assert g.ok

    first = coord.gate_resolve(tmp_path, g.gate_id, reason="first reason")
    assert first.ok is True

    def _gate_row():
        rows = bd_json("gate", "list", "--all", cwd=tmp_path) or []
        return next(r for r in rows if r["id"] == g.gate_id)

    before = _gate_row()

    second = coord.gate_resolve(tmp_path, g.gate_id, reason="a completely different reason")
    assert second.ok is True  # failure mode: NOT an error to resolve twice

    after = _gate_row()
    assert after["close_reason"] == before["close_reason"]  # the redundant call changed nothing


# ---- reclaim: stale reverted, live (heartbeated) untouched -------------------------------------


def _init_server(path, prefix):
    path.mkdir(parents=True, exist_ok=True)
    run(
        ["bd", "init", "--server", "--prefix", prefix, "--non-interactive"],
        cwd=str(path),
        check=True,
        capture=True,
        timeout=_TIMEOUT,
    )


@pytest.fixture
def server_store(tmp_path):
    """An owned-mode (`bd init --server`) store whose sql-server is reaped by a FINALIZER.

    Was a `finally:` running `bd … dolt stop` with `check=False` (bh-5mc8g). Two problems, both
    already established by bh-cbou: `bd dolt stop` resolves the server through
    `.beads/metadata.json`'s `dolt_mode` and refuses outright in some states — and with
    `check=False` that refusal was swallowed, leaving a real sql-server running against a tmp dir
    pytest then deleted (observed orphaned, reparented to PID 1, from this exact test). A
    finalizer also runs when the test fails partway, which a `finally` inside the body only does
    if the body was reached at all.

    Kills by the pidfile bd writes at `<store>/.beads/dolt-server.pid` (measured against a real
    bd for owned mode — the same file name shared mode puts under `BEADS_SHARED_SERVER_DIR`), so
    it can never name anything but this test's own server."""
    yield tmp_path
    reap_dolt_server(tmp_path / ".beads")


def _backdate_lease(path, bead_id, sql_timestamp):
    res = run(
        [
            "bd",
            "-C",
            str(path),
            "sql",
            f"UPDATE leases SET lease_expires_at = '{sql_timestamp}' WHERE issue_id = '{bead_id}'",
        ],
        check=True,
        capture=True,
        timeout=_TIMEOUT,
    )
    return res


def test_reclaim_reverts_stale_lease_and_leaves_a_heartbeated_one_untouched(server_store):
    tmp_path = server_store
    _init_server(tmp_path, "ls")
    stale = create(tmp_path, "dead worker's bead")
    live = create(tmp_path, "live worker's bead")
    bd("update", stale, "--claim", cwd=tmp_path, capture=True)
    bd("update", live, "--claim", cwd=tmp_path, capture=True)

    # simulate the dead holder: its lease is 5 minutes old and was never renewed. Backdate
    # directly in the `leases` table (only reachable via `bd sql`, only available in
    # server mode) rather than waiting out a real TTL — `bd reclaim` still makes the call.
    _backdate_lease(tmp_path, stale, "2000-01-01 00:00:00")
    # the live holder heartbeats right before reclaim runs, exactly like a real worker would
    hb = coord.heartbeat(tmp_path, live)
    assert hb.ok is True

    got = coord.reclaim(tmp_path, older_than="0s")

    assert got.ok is True
    assert got.reclaimed_ids == (stale,)  # EXACTLY the stale one
    assert live not in got.reclaimed_ids  # the heartbeated one is untouched

    assert bd_json("show", stale, cwd=tmp_path)[0]["status"] == "open"
    assert bd_json("show", stale, cwd=tmp_path)[0].get("assignee") in (None, "")
    live_row = bd_json("show", live, cwd=tmp_path)[0]
    assert live_row["status"] == "in_progress"
    assert live_row.get("assignee")
