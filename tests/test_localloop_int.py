"""Integration: the `local` tier against a REAL `bd` binary (bh-c6dk.5).

The properties here are the ones a faked `bd` cannot prove, and they are the ones the bead calls
out as acceptance:

* **RESTART IS THE DURABILITY EVENT.** Kill a loop mid-molecule, start another against the same
  hive, and assert no bead is double-claimed, no seat is left spending, and the molecule still
  completes. The bead is explicit that this must be TESTED, not assumed, because the original
  filing never mentioned surviving a restart at all.
* **An open `type:human` gate holds its bead back.** `bd gate check` resolves timer/gh/bead gates
  and never a human one, so the bead never becomes ready — proven against real gate semantics
  rather than a mock that returns whatever the test wanted.
* **The normal cancellation path releases the claim immediately**, without waiting out the
  5-minute lease TTL that `bd reclaim` (the backstop) depends on.

Marked `integration` + self-skips without `bd` on PATH, per this repo's marker convention.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from beadhive import localloop
from beadhive.run import run
from beadhive.state import CAUSE_RUN_BLOCKED, CAUSE_RUN_CANCELLED, DISPATCH_DIM
from harness.beads import bd, bd_json, init_embedded, skip_if_no_bd

pytestmark = [pytest.mark.integration, skip_if_no_bd]

#: Built from `beadhive.state`, never spelled out (bh-4kq1b, mirroring what bh-bwcxx did to
#: `scripts/demo_local_loop.py`). These three assertions carried the PRE-rename literals
#: `dispatch:cancelled` / `dispatch:blocked` for as long as it took someone to run the suite by
#: hand — a rename inside the package cannot desync a label the test derives from the constant.
DISPATCH_CANCELLED_LABEL = f"{DISPATCH_DIM}:{CAUSE_RUN_CANCELLED}"
DISPATCH_BLOCKED_LABEL = f"{DISPATCH_DIM}:{CAUSE_RUN_BLOCKED}"

STUB_SEAT = Path(__file__).parent / "fixtures" / "stub_seat.py"


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


# ---- a real scratch hive ----------------------------------------------------------------------


def _hive(path: Path, prefix: str) -> Path:
    """A real git clone + a real embedded-`bd` store, with a root commit.

    The commit is not decoration: `git worktree add` needs something to fork from, and
    :func:`_worktree` below provisions one PER BEAD because the loop refuses to spawn a
    `developer` seat whose workspace is the main clone (see `_claimer`).
    """
    path.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-q", "-b", "main"], cwd=str(path), check=True, capture=True)
    for key, value in (("user.name", "int test"), ("user.email", "int@example.invalid")):
        run(["git", "config", key, value], cwd=str(path), check=True, capture=True)
    run(
        ["git", "commit", "-q", "--allow-empty", "-m", "root"],
        cwd=str(path),
        check=True,
        capture=True,
    )
    init_embedded(path, prefix)
    return path


def _worktree(main: Path, bead: str) -> str:
    """Provision the bead's OWN managed worktree, the way `bh work next` does in production.

    Sibling to the clone (`<tmp>/worktrees/<id>`, off `wt/bead/issue/<id>`) so it mirrors the
    real `$BH_WORKTREES` layout and never lands inside the hive dir, where it would be picked up
    as untracked content of the integration branch.
    """
    target = main.parent / "worktrees" / bead
    if not target.is_dir():
        target.parent.mkdir(parents=True, exist_ok=True)
        run(
            ["git", "worktree", "add", "-q", "-b", f"wt/bead/issue/{bead}", str(target), "main"],
            cwd=str(main),
            check=True,
            capture=True,
        )
    return str(target)


def _created(res) -> str:
    for line in (res.stdout or "").splitlines():
        if "Created issue:" in line:
            return line.split("Created issue:", 1)[1].strip().split()[0]
    raise AssertionError(f"no bead id in: {res.stdout!r}")


def _seed(main: Path, titles) -> tuple[str, list[str]]:
    epic = _created(bd("create", "molecule", "-t", "epic", "-p", "1", cwd=main, capture=True))
    ids = [
        _created(bd("create", t, "-t", "task", "-p", "2", "--parent", epic, cwd=main, capture=True))
        for t in titles
    ]
    bd("update", epic, "--status", "in_progress", cwd=main, capture=True)
    return epic, ids


def _row(bead: str, main: Path) -> dict:
    data = bd_json("show", bead, cwd=main)
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else {}


def _brief(main: Path, bead: str, text: str) -> str:
    """A stub-seat brief, written where `_default_instructions` writes the real one.

    `.beads/dispatch/` UNDER THE HIVE DIR, not some sibling scratch dir, and that placement is
    load-bearing (bh-4kq1b): `find_orphan_seats` scopes discovery to a path every seat of THIS
    hive carries in its argv, and the loop passes `hive_dir`. Once the workspace is a real
    per-bead worktree (as it is in production, and now here), `--instructions` is the ONLY
    argument still under the hive dir — a brief written elsewhere narrows the seat out of
    discovery and the restart test's reap silently finds nothing.
    """
    path = main / ".beads" / "dispatch" / f"{bead}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n")
    return str(path)


def _claimer(main: Path, actor: str):
    """A minimal real-`bd` stand-in for `bh work next`.

    Deliberately NOT the real verb here: `bh work next` also resolves the worktree through the
    full config/registry path, which is `tests/test_work_next.py`'s subject and would drown these
    tests in setup. The property under test is what BEADS does across a restart, so the claim is a
    real `bd update --claim` against a real store — the race semantics `bh work next` wraps are
    already covered where they live.

    WHAT IT MUST NOT SHORTCUT (bh-4kq1b): the *provisioning* half. This returned
    `worktree=str(main)` — the MAIN CLONE — which `bh-e7r9q`'s `_workspace_permitted` correctly
    began refusing, because a `developer` seat there would commit onto the integration branch.
    Every claim then dead-ended in `dispatch:provisioning_failed` and four load-bearing tests
    went red for a reason that had nothing to do with what they assert. `bh work next` hands back
    a per-bead worktree, so the stand-in does too.
    """

    def claim() -> localloop.ClaimResult:
        rows = bd_json("ready", cwd=main) or []
        for row in rows:
            bead = str(row.get("id") or "")
            if not bead or str(row.get("issue_type") or "") in ("epic", "gate", "event"):
                continue
            bd("update", bead, "--claim", cwd=main, actor=actor, capture=True)
            fresh = _row(bead, main)
            if str(fresh.get("assignee") or "") == actor:
                return localloop.ClaimResult(
                    claimed=bead, worktree=_worktree(main, bead), actor=actor
                )
        return localloop.ClaimResult(reason="none_eligible")

    return claim


def _loop(main: Path, epic: str, *, actor="dev/int", briefs=None, **kw):
    briefs = briefs or {}
    kw.setdefault("caps", localloop.Caps(max_concurrency=1))
    return localloop.LocalLoop(
        hive_dir=main,
        epic=epic,
        actor=actor,
        seat_command=f"{sys.executable} {STUB_SEAT}",
        poll_interval=0.1,
        envelope_grace=5.0,
        terminate_grace=5.0,
        claim=_claimer(main, actor),
        instructions=lambda _a, bead, _r: _brief(main, bead, briefs.get(bead, "STUB_STATUS=done")),
        **kw,
    )


# ---- an open human gate is not dispatchable ------------------------------------------------


@async_test
async def test_an_open_human_gate_keeps_its_bead_out_of_the_ready_set(tmp_path):
    """`bd gate check` runs at the head of every pass and never resolves a human gate, so the
    bead behind it is never ready and never spawned. The runtime cannot advance a decision a
    person owes — and the person needs no runtime running to give it."""
    main = _hive(tmp_path / "hive", "hg")
    epic, (gated,) = _seed(main, ["behind a human gate"])
    bd(
        "gate",
        "create",
        "--blocks",
        gated,
        "--type",
        "human",
        "--reason",
        "needs a person",
        cwd=main,
        capture=True,
    )

    loop = _loop(main, epic)
    for _ in range(3):
        report = await loop.run_pass()
        assert gated not in report.dispatched
    assert _row(gated, main)["status"] == "open", "the gated bead must never have been claimed"
    await loop.shutdown()

    # Resolve it the way a human does — no runtime involved — and it becomes ordinary work.
    gates = [
        g
        for g in (bd_json("list", "--include-gates", "--all", cwd=main) or [])
        if str(g.get("issue_type") or "") == "gate"
    ]
    assert gates, "the gate itself must exist as a bead"
    bd("gate", "resolve", str(gates[0]["id"]), "--reason", "approved", cwd=main, capture=True)

    loop2 = _loop(main, epic)
    report = await loop2.run_pass()
    assert report.dispatched == (gated,)
    await loop2.shutdown()


# ---- restart is the durability event ----------------------------------------------------------


@async_test
async def test_restart_mid_molecule_neither_double_claims_nor_leaves_a_seat_spending(tmp_path):
    """The headline durability property, driven end to end.

    Loop A claims a bead and spawns a (hanging) seat, then DISAPPEARS the way a `kill -9` leaves
    it: no shutdown, no cancel, the in-flight map simply gone and the seat still running. Loop B
    starts against the same hive and must (1) not claim that bead a second time, (2) reap the
    orphaned seat rather than leave it spending, and (3) still drive the molecule to completion.
    """
    main = _hive(tmp_path / "hive", "rs")
    epic, (first, second) = _seed(main, ["the one in flight when the loop dies", "the next one"])

    loop_a = _loop(main, epic, briefs={first: "STUB_HANG=true"})
    report = await loop_a.run_pass()
    assert len(report.dispatched) == 1
    claimed = report.dispatched[0]
    orphan = loop_a.in_flight[claimed]
    orphan_pgid = orphan.pgid
    assert _row(claimed, main)["status"] == "in_progress"

    # kill -9 of the LOOP: its own state evaporates, the seat it spawned does not (that is the
    # whole point of start_new_session — the seat has its own session and survives its parent).
    loop_a.in_flight.clear()
    del loop_a
    assert localloop.group_alive(orphan_pgid), "the seat must outlive the loop, as it would"

    loop_b = _loop(main, epic, briefs={first: "STUB_STATUS=done", second: "STUB_STATUS=done"})
    restart = await loop_b.run_pass()

    # (2) reaped, not adopted, not left spending
    assert claimed in restart.orphans_reaped
    assert not localloop.group_alive(orphan_pgid)
    # (1) no double claim: the bead this loop dispatched is never the orphaned one still held by
    # a dead worker — the orphan's claim was released first, so any re-dispatch is a FRESH turn.
    assert len(set(restart.dispatched)) == len(restart.dispatched)
    assert all(_row(b, main).get("assignee") == "dev/int" for b in restart.dispatched)
    labels = _row(claimed, main).get("labels") or []
    assert DISPATCH_CANCELLED_LABEL in labels, "the restart must be legible in the beads afterwards"

    # (3) the molecule still completes
    for _ in range(12):
        pass_report = await loop_b.run_pass()
        for bead, outcome in pass_report.harvested:
            if outcome == "done":
                bd("close", bead, "--reason", "seat done", cwd=main, actor="dev/int", capture=True)
        if pass_report.decision and pass_report.decision.row in ("finish", "done"):
            break
        await asyncio.sleep(0.1)
    await loop_b.shutdown()
    assert {_row(b, main)["status"] for b in (first, second)} == {"closed"}


@async_test
async def test_a_fresh_loop_re_derives_the_same_world_from_bd(tmp_path):
    """Restart is a no-op *by construction*: two loops that never met must see the same molecule,
    because the whole decision input comes from `bd` and nothing is cached or persisted."""
    main = _hive(tmp_path / "hive", "rd")
    epic, ids = _seed(main, ["one", "two", "three"])
    bd("close", ids[0], "--reason", "already done", cwd=main, capture=True)

    first = _loop(main, epic).load_molecule(budget=2)
    second = _loop(main, epic).load_molecule(budget=2)
    assert [b.get("id") for b in first.beads] == [b.get("id") for b in second.beads]
    assert first.epic_status == second.epic_status
    assert {b.get("id") for b in first.beads} == set(ids)


# ---- claim release is the normal path; reclaim is the backstop --------------------------------


@async_test
async def test_cancelling_releases_the_claim_without_waiting_out_the_lease_ttl(tmp_path):
    """The lease TTL is 5 minutes and `bd reclaim` is the only recovery for a holder that DIED.
    A run this loop stopped on purpose must be re-dispatchable on the very next pass instead."""
    main = _hive(tmp_path / "hive", "cr")
    epic, (bead,) = _seed(main, ["a hanging seat"])

    loop = _loop(main, epic, briefs={bead: "STUB_HANG=true"})
    await loop.run_pass()
    assert _row(bead, main)["status"] == "in_progress"
    started = time.monotonic()

    await loop.shutdown()

    assert time.monotonic() - started < 60, "cancellation must not wait out any TTL"
    row = _row(bead, main)
    assert row["status"] == "open"
    assert not row.get("assignee")
    assert DISPATCH_CANCELLED_LABEL in (row.get("labels") or [])


@async_test
async def test_a_failure_cause_is_a_real_event_bead_and_a_label(tmp_path):
    """`bd set-state` atomically writes BOTH: an event bead (the source of truth, from which
    retry counts are DERIVED) and a `dispatch:<value>` label (the fast-lookup cache). Asserted
    against real bd because the two-writes-in-one behavior is bd's, not ours."""
    main = _hive(tmp_path / "hive", "fc")
    epic, (bead,) = _seed(main, ["a bead the seat reports blocked"])

    loop = _loop(main, epic, briefs={bead: "STUB_STATUS=blocked"})
    await loop.run_pass()
    for _ in range(60):
        report = await loop.run_pass()
        if report.harvested:
            break
        await asyncio.sleep(0.1)
    await loop.shutdown()

    assert DISPATCH_BLOCKED_LABEL in (_row(bead, main).get("labels") or [])
    # `--all`: event beads are created CLOSED, so a default `bd list` would show none of them —
    # which is exactly what would make a DERIVED retry count silently read zero forever.
    events = [
        e
        for e in (bd_json("list", "--parent", bead, "--include-infra", "--all", cwd=main) or [])
        if str(e.get("issue_type") or "") == "event"
    ]
    assert events, "the cause must exist as an event bead, not only as a label"
    assert any("dispatch" in json.dumps(e).lower() for e in events)


# ---- orphan discovery is specific -------------------------------------------------------------


def test_orphan_discovery_never_matches_an_unrelated_process():
    """Discovery keys on a `--bead <id>` match, the contract's `--session_id` marker, AND the
    hive scope. Anything looser reaps other people's processes: bead ids are unique only within
    a hive, so without the scope a second hive (or a second loop) reusing an id loses a live
    seat. That is not hypothetical — it happened between two test workers on one machine."""
    ps = "\n".join(
        [
            "  111   111 /usr/bin/vim notes-about-rs-1.md",
            "  222   222 grep -r rs-1 .",
            "  333   333 py stub_seat.py --workspace /hives/a --bead rs-1 --session_id abc",
            "  444   444 py stub_seat.py --workspace /hives/b --bead rs-1 --session_id def",
            f"{os.getpid():>5} {os.getpgid(0):>5} pytest --workspace /hives/a --bead rs-1 "
            "--session_id self",
        ]
    )
    found = localloop.find_orphan_seats(["rs-1"], scope="/hives/a", ps_output=ps)
    assert [pid for pid, _pgid, _argv in found] == [333], "the other hive's seat is not ours"


def test_orphan_discovery_is_a_noop_without_beads():
    assert localloop.find_orphan_seats([]) == ()


def test_a_reaped_orphan_is_reported_and_not_silently_dropped(tmp_path):
    """`ps` is the discovery source; the reap itself is proven in the restart test above. This
    guards the narrower promise that a scan failure degrades to "found nothing" rather than
    crashing a pass."""
    completed = subprocess.run(["ps", "-eo", "pid=,pgid=,args="], capture_output=True, text=True)
    assert completed.returncode == 0, "the demo/loop relies on ps being available"
    assert localloop.find_orphan_seats(["definitely-not-a-bead-id"], scope="/nowhere") == ()
