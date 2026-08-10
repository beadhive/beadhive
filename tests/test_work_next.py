"""`bh work next` — the decision core (pure) and the pick → claim → re-verify loop (CLI).

The pure half needs no fixtures at all: `work_next.decide` takes plain `bd` JSON dicts and returns
a dataclass, so the 12-row priority table is tested AS a table. The CLI half fakes `bd` at the
`bd._run` seam (the single subprocess boundary every `bd` call in `work.py` goes through) but DOES
touch real git (bh-qczj.2): a won claim provisions a real worktree via `worktree.ensure`, so the
`nexthive` fixture is a real (committed) git repo, not a bare directory.
"""

from __future__ import annotations

import json
import random
import subprocess
import threading
import time
from collections import namedtuple
from types import SimpleNamespace

import pytest
import typer

from beadhive import bd as bd_mod
from beadhive import config, guard, work, work_next

_CP = namedtuple("CP", "returncode stdout stderr")


# ---- the pure decision core -------------------------------------------------


def _bead(bead_id, status="open", labels=(), deps=(), issue_type="task"):
    return {
        "id": bead_id,
        "status": status,
        "labels": list(labels),
        "dependencies": list(deps),
        "issue_type": issue_type,
    }


def _mol(**kw):
    kw.setdefault("epic", "ep-1")
    return work_next.Molecule(**kw)


def test_rows_actions_and_reasons_are_the_documented_closed_sets():
    """The row/action/reason names are the contract a driver keys off — pinned verbatim so a
    rename is a deliberate, visible change rather than a silent one."""
    assert work_next.ROWS == (
        "done",
        "not_dispatchable",
        "halt-on-escalation",
        "start",
        "resume-changes-requested",
        "merge-exactly-one",
        "review",
        "finish",
        "wrap_up",
        "dispatch-up-to-budget",
        "wait",
        "deadlock-escalate",
    )
    assert len(work_next.ROWS) == 12
    assert set(work_next.REASONS) == {
        "not_dispatchable",
        "deadlock",
        "repeated_changes_requested",
        "repeated_merge_failure",
        "ambiguous_gate",
        "stuck",
    }


def test_decision_refuses_a_row_action_or_reason_outside_the_closed_set():
    with pytest.raises(ValueError):
        work_next.Decision("nope", "wait")
    with pytest.raises(ValueError):
        work_next.Decision("wait", "improvise")
    with pytest.raises(ValueError):
        work_next.Decision("wait", "escalate", reason="because")
    with pytest.raises(ValueError):
        work_next.Decision("deadlock-escalate", "escalate")  # escalate without a reason


def test_row_done_when_the_epic_is_closed():
    d = work_next.decide(_mol(epic_status="closed", beads=(_bead("b1"),)))
    assert (d.row, d.action) == ("done", "done")


def test_row_not_dispatchable_escalates_with_its_reason_code():
    d = work_next.decide(_mol(dispatchable=False, beads=(_bead("b1"),)))
    assert (d.row, d.action, d.reason) == ("not_dispatchable", "escalate", "not_dispatchable")


def test_row_halt_on_escalation_beats_every_dispatchable_row():
    d = work_next.decide(_mol(beads=(_bead("b1"),), escalations=("esc-1",)))
    assert (d.row, d.action, d.beads) == ("halt-on-escalation", "halt", ("esc-1",))


def test_row_start_when_the_epic_has_not_been_started():
    d = work_next.decide(_mol(epic_status="open", beads=(_bead("b1"),)))
    assert (d.row, d.action, d.beads) == ("start", "start", ("ep-1",))


def test_row_resume_changes_requested_beats_merge_review_and_dispatch():
    beads = (
        _bead("b1", labels=["review:changes-requested"]),
        _bead("b2", labels=["review:approved"]),
        _bead("b3", labels=["review:pending"]),
        _bead("b4"),
    )
    d = work_next.decide(_mol(beads=beads))
    assert (d.row, d.action, d.beads) == ("resume-changes-requested", "resume", ("b1",))


def test_row_merge_takes_exactly_one_bead_even_with_several_approved():
    beads = (
        _bead("b1", labels=["review:approved"]),
        _bead("b2", labels=["review:approved"]),
        _bead("b3", labels=["review:pending"]),
    )
    d = work_next.decide(_mol(beads=beads))
    assert (d.row, d.action, d.beads) == ("merge-exactly-one", "merge", ("b1",))


def test_row_review_when_only_a_submitted_bead_remains():
    d = work_next.decide(_mol(beads=(_bead("b1", labels=["review:pending"]),)))
    assert (d.row, d.action, d.beads) == ("review", "review", ("b1",))


def test_row_finish_when_every_child_is_closed_but_the_epic_is_not():
    d = work_next.decide(_mol(beads=(_bead("b1", status="closed"),)))
    assert (d.row, d.action, d.beads) == ("finish", "finish", ("ep-1",))


def test_row_wrap_up_when_only_wrap_up_beads_are_left():
    beads = (_bead("b1", status="closed"), _bead("b2", labels=["wrap-up"]))
    d = work_next.decide(_mol(beads=beads))
    assert (d.row, d.action, d.beads) == ("wrap_up", "wrap_up", ("b2",))


def test_row_dispatch_fills_the_remaining_budget_in_ready_order():
    beads = (
        _bead("b1", status="in_progress"),
        _bead("b2"),
        _bead("b3"),
        _bead("b4"),
    )
    d = work_next.decide(_mol(beads=beads, budget=3))
    assert (d.row, d.action, d.beads) == ("dispatch-up-to-budget", "dispatch", ("b2", "b3"))


def test_row_dispatch_skips_a_bead_whose_in_molecule_dependency_is_open():
    beads = (_bead("b1"), _bead("b2", deps=["b1"]))
    d = work_next.decide(_mol(beads=beads, budget=5))
    assert d.beads == ("b1",)


def test_row_wait_when_the_budget_is_full():
    beads = (_bead("b1", status="in_progress"), _bead("b2"))
    d = work_next.decide(_mol(beads=beads, budget=1))
    assert (d.row, d.action) == ("wait", "wait")


def test_row_deadlock_escalates_when_nothing_is_ready_and_nothing_is_in_flight():
    beads = (_bead("b1", deps=["b2"]), _bead("b2", deps=["b1"]))
    d = work_next.decide(_mol(beads=beads))
    assert (d.row, d.action, d.reason) == ("deadlock-escalate", "escalate", "deadlock")


def test_infra_beads_are_never_dispatchable_work():
    """A gate is a blocker record and an event is the audit trail the loop-breaker COUNTS —
    dispatching either would be a category error, so they don't even count as open work."""
    beads = (_bead("g1", issue_type="gate"), _bead("e1", issue_type="event"))
    assert work_next.decide(_mol(beads=beads)).row == "done"


# ---- the loop-breaker (counts DERIVED from event beads, never stored) --------


def _event(text):
    return {"id": "ev", "issue_type": "event", "title": text}


def test_loop_breaker_escalates_on_the_nth_identical_action_with_a_mapped_reason():
    beads = (_bead("b1", labels=["review:changes-requested"]),)
    events = {"b1": [_event("review -> changes-requested"), _event("review -> changes-requested")]}
    d = work_next.decide(_mol(beads=beads, events=events))
    assert (d.row, d.action, d.reason, d.beads) == (
        "resume-changes-requested",
        "escalate",
        "repeated_changes_requested",
        ("b1",),
    )


def test_loop_breaker_stays_quiet_below_the_threshold():
    beads = (_bead("b1", labels=["review:changes-requested"]),)
    events = {"b1": [_event("review -> changes-requested")]}
    assert work_next.decide(_mol(beads=beads, events=events)).action == "resume"


def test_loop_breaker_threshold_is_configurable_per_molecule():
    beads = (_bead("b1", labels=["review:changes-requested"]),)
    events = {"b1": [_event("review -> changes-requested")]}
    d = work_next.decide(_mol(beads=beads, events=events, max_action_retries=1))
    assert d.action == "escalate"


def test_loop_breaker_maps_a_repeated_merge_to_repeated_merge_failure():
    beads = (_bead("b1", labels=["review:approved"]),)
    events = {"b1": [_event("merge conflict onto main"), _event("merge conflict onto main")]}
    assert work_next.decide(_mol(beads=beads, events=events)).reason == "repeated_merge_failure"


def test_loop_breaker_maps_a_repeated_review_to_ambiguous_gate():
    beads = (_bead("b1", labels=["review:pending"]),)
    events = {"b1": [_event("ambiguous gate"), _event("ambiguous gate")]}
    assert work_next.decide(_mol(beads=beads, events=events)).reason == "ambiguous_gate"


def test_loop_breaker_falls_back_to_stuck_for_an_action_with_no_signature():
    beads = (_bead("b1"),)
    events = {"b1": [_event("dispatched"), _event("dispatched")]}
    d = work_next.decide(_mol(beads=beads, events=events))
    assert (d.action, d.reason) == ("escalate", "stuck")


def test_loop_breaker_escalates_only_the_looping_member_of_a_budgeted_dispatch():
    """The innocent beads must not be dropped — one stuck bead is not a stalled molecule."""
    beads = (_bead("b1"), _bead("b2"))
    events = {"b1": [_event("x"), _event("x")]}
    d = work_next.decide(_mol(beads=beads, events=events, budget=5))
    assert (d.action, d.beads) == ("escalate", ("b1",))


def test_attempt_count_ignores_events_that_are_not_the_actions_failure():
    events = [_event("review -> pending"), _event("merge conflict onto main")]
    assert work_next.attempt_count(events, "resume") == 0
    assert work_next.attempt_count(events, "merge") == 1


def test_nothing_in_the_core_persists_a_counter():
    """The execution-memory boundary, asserted rather than trusted: counts are DERIVED from event
    beads on every call, so the SAME molecule decided twice gives the same answer and no state
    accumulates anywhere (bh-c6dk's replan — a stored counter would be runtime state outside
    beads, which the epic's invariant forbids)."""
    mol = _mol(
        beads=(_bead("b1", labels=["review:changes-requested"]),),
        events={"b1": [_event("review -> changes-requested")]},
    )
    assert work_next.decide(mol) == work_next.decide(mol) == work_next.decide(mol)
    assert not [n for n in dir(work_next) if "counter" in n.lower()]


# ---- the pick / verify predicates -------------------------------------------


def test_eligible_preserves_bd_ready_order_and_filters_the_untakeable():
    rows = [
        _bead("b1", status="closed"),
        _bead("b2", status="in_progress"),
        _bead("g1", issue_type="gate"),
        {"id": "b3", "status": "open", "assignee": "dev/other"},
        {"id": "b4", "status": "open", "assignee": "dev/me"},
        _bead("b5"),
    ]
    assert work_next.eligible(rows, "dev/me") == ("b4", "b5")


def test_claim_won_requires_both_the_holder_and_the_transition():
    assert work_next.claim_won({"assignee": "dev/me", "status": "in_progress"}, "dev/me")
    assert not work_next.claim_won({"assignee": "dev/other", "status": "in_progress"}, "dev/me")
    assert not work_next.claim_won({"assignee": "dev/me", "status": "open"}, "dev/me")
    assert not work_next.claim_won(None, "dev/me")


def test_decline_codes_distinguish_empty_from_ineligible_from_lost():
    assert work_next.decline([], []) == work_next.DECLINE_EMPTY_QUEUE
    assert work_next.decline([_bead("b1")], []) == work_next.DECLINE_NONE_ELIGIBLE
    assert work_next.decline([_bead("b1")], ["b1"]) == work_next.DECLINE_ALL_LOST


# ---- the CLI loop: win / lose-then-win / empty queue -------------------------

CONFIG_YAML = """\
providers: [github]
work:
  validate_cmd: "true"
  review_gate: "human"
  identity: {mode: agent, name: "dev/next", email: "agents@test.dev"}
managed_repos:
  - {provider: github, org: myorg, repo: myrepo, prefix: mr, kind: personal}
"""


class FakeBd:
    """`bd` at the `bd._run` seam: an in-memory bead store serving `ready` / `show` / `update`.

    `update --claim` deliberately models bd's REAL behaviour — it is not a compare-and-swap, so a
    claim onto a bead someone else already holds still exits 0 while the store keeps the existing
    holder. That is the race `bh work next` exists to survive, and a fake that refused the second
    claim would test a `bd` that does not exist."""

    def __init__(self, ready=(), stolen_by=None):
        self.beads = {b["id"]: dict(b) for b in ready}
        self.order = [b["id"] for b in ready]
        self.stolen_by = dict(stolen_by or {})  # bead id -> the actor that wins the race
        self.claims = []  # bead ids a claim was attempted on, in order

    def __call__(self, cmd, **_kw):
        args = list(cmd[1:])
        actor = ""
        while args and args[0] in ("-C", "--actor"):
            if args[0] == "--actor":
                actor = args[1]
            args = args[2:]
        return self._dispatch(actor, [a for a in args if a != "--json"])

    def _dispatch(self, actor, args):
        sub = args[0] if args else ""
        if sub == "ready":
            rows = [self.beads[i] for i in self.order if self.beads[i]["status"] == "open"]
            return _CP(0, json.dumps(rows), "")
        if sub == "show":
            row = self.beads.get(args[1])
            return _CP(0 if row else 1, json.dumps(row) if row else "", "")
        if sub == "update" and "--claim" in args:
            bead = self.beads.setdefault(args[1], {"id": args[1]})
            self.claims.append(args[1])
            # The race: whoever `stolen_by` names got there first. bd still exits 0 (no CAS).
            winner = self.stolen_by.get(args[1], actor)
            bead.update(assignee=winner, status="in_progress")
            return _CP(0, "", "")
        if sub == "update" and "--status" in args:
            # `_release_claim`'s reopen/unassign write (bh-qczj.2): `update <id> --status open
            # --assignee ""`.
            bead = self.beads.setdefault(args[1], {"id": args[1]})
            if "--status" in args:
                bead["status"] = args[args.index("--status") + 1]
            if "--assignee" in args:
                bead["assignee"] = args[args.index("--assignee") + 1]
            return _CP(0, "", "")
        if sub == "set-state":
            return _CP(0, "", "")
        return _CP(0, "", "")


@pytest.fixture
def nexthive(tmp_path, monkeypatch):
    """A hive `bh work next` can resolve, with `bd` faked and the host-lease/state seams stubbed.

    A real, committed git repo (bh-qczj.2): a won claim now provisions a real worktree via
    `worktree.ensure`, which forks a new branch off the integration base — that needs an actual
    commit to fork from, not just an initialized repo."""
    ws_root = tmp_path / "ws"
    main = ws_root / "github" / "myorg" / "myrepo"
    main.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    subprocess.run(
        ["git", "-C", str(main), "config", "user.email", "human@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(main), "config", "user.name", "human"], check=True)
    (main / "README.md").write_text("# x\n")
    subprocess.run(["git", "-C", str(main), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(main), "commit", "-qm", "chore: init"], check=True)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(CONFIG_YAML)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("WS_CONFIG", str(cfg_path))
    monkeypatch.setenv("WS_WORKTREES", str(tmp_path / "wts"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(guard, "guard_primary", lambda *a, **k: None)
    monkeypatch.setattr(work, "_pull_state", lambda *a, **k: None)
    return SimpleNamespace(main=main, wts=tmp_path / "wts", cfg_path=cfg_path)


def _fake_bd(monkeypatch, fake):
    monkeypatch.setattr(bd_mod, "_run", fake)
    return fake


def _open(bead_id, **kw):
    return {"id": bead_id, "status": "open", "assignee": "", "issue_type": "task", **kw}


def _run_next(capsys, as_="dev/next", **kw):
    """Invoke the verb as a plain function; returns (exit code, parsed json payload)."""
    code = 0
    try:
        work.next_(as_=as_, hive="mr", as_json=True, **kw)
    except typer.Exit as exc:
        code = exc.exit_code
    return code, json.loads(capsys.readouterr().out)


def test_next_claims_the_first_eligible_bead(nexthive, monkeypatch, capsys):
    fake = _fake_bd(monkeypatch, FakeBd(ready=[_open("b1"), _open("b2")]))
    code, payload = _run_next(capsys)
    assert code == 0
    assert (payload["status"], payload["bead"], payload["actor"]) == ("claimed", "b1", "dev/next")
    assert payload["seat"] == "developer"
    assert fake.claims == ["b1"]
    # bh-qczj.2: a won claim provisions/attaches its worktree via `worktree.ensure` — the same op
    # `claim`/`assign`/`start` already use — and reports it + the stamped identity back.
    wt = nexthive.wts / "github" / "myorg" / "myrepo" / "b1"
    assert payload["worktree"] == str(wt)
    assert wt.is_dir()
    assert (
        subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "wt/bead/issue/b1"
    )
    assert payload["identity"]["name"] == "dev/next"
    assert payload["identity"]["email"] == "agents@test.dev"
    assert payload["identity"]["mode"] == "agent"


def test_next_retries_the_following_candidate_when_it_loses_the_race(nexthive, monkeypatch, capsys):
    """The whole point: `bd update --claim` exits 0 for the LOSER too, so the re-read is what
    catches the steal — and a lost race moves on rather than failing the call."""
    fake = _fake_bd(
        monkeypatch,
        FakeBd(ready=[_open("b1"), _open("b2")], stolen_by={"b1": "dev/other"}),
    )
    code, payload = _run_next(capsys)
    assert code == 0
    assert (payload["status"], payload["bead"]) == ("claimed", "b2")
    assert fake.claims == ["b1", "b2"], "it must have tried the lost bead before moving on"
    assert payload["tried"] == ["b1", "b2"]
    # only the WINNING candidate (b2) gets a worktree — b1 was never ours to provision
    wt2 = nexthive.wts / "github" / "myorg" / "myrepo" / "b2"
    assert payload["worktree"] == str(wt2)
    assert wt2.is_dir()
    assert not (nexthive.wts / "github" / "myorg" / "myrepo" / "b1").exists()


def test_next_provisioning_failure_releases_the_claim(nexthive, monkeypatch, capsys):
    """A provisioning failure must never leave a bead claimed with no worktree behind it: the
    claim is released (reopened, unassigned) and the failure surfaces rather than a clean
    `status: claimed` envelope."""
    fake = _fake_bd(monkeypatch, FakeBd(ready=[_open("b1")]))
    monkeypatch.setattr(
        work.worktree, "ensure", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with pytest.raises(RuntimeError, match="boom"):
        work.next_(as_="dev/next", hive="mr", as_json=True)
    capsys.readouterr()
    # released: reopened and unassigned, not left claimed with no worktree
    row = fake.beads["b1"]
    assert row["status"] == "open"
    assert row["assignee"] == ""


def test_next_declines_with_a_distinct_exit_code_on_an_empty_queue(nexthive, monkeypatch, capsys):
    _fake_bd(monkeypatch, FakeBd(ready=[]))
    code, payload = _run_next(capsys)
    assert code == work.NEXT_DECLINE_EXIT == 3
    assert (payload["status"], payload["reason"], payload["bead"]) == (
        "declined",
        "empty_queue",
        "",
    )
    assert payload["worktree"] is None
    assert payload["identity"] is None


def test_next_declines_all_lost_when_every_candidate_was_stolen(nexthive, monkeypatch, capsys):
    _fake_bd(
        monkeypatch,
        FakeBd(
            ready=[_open("b1"), _open("b2")],
            stolen_by={"b1": "dev/other", "b2": "dev/other"},
        ),
    )
    code, payload = _run_next(capsys)
    assert code == work.NEXT_DECLINE_EXIT
    assert (payload["status"], payload["reason"], payload["tried"]) == (
        "declined",
        "all_lost",
        ["b1", "b2"],
    )


def test_next_declines_none_eligible_when_the_queue_is_all_someone_elses(
    nexthive, monkeypatch, capsys
):
    fake = _fake_bd(monkeypatch, FakeBd(ready=[_open("b1", assignee="dev/other")]))
    code, payload = _run_next(capsys)
    assert code == work.NEXT_DECLINE_EXIT
    assert payload["reason"] == "none_eligible"
    assert fake.claims == [], "a bead held by another actor must never be claimed at"


def test_next_envelope_is_versioned_and_named(nexthive, monkeypatch, capsys):
    _fake_bd(monkeypatch, FakeBd(ready=[_open("b1")]))
    _code, payload = _run_next(capsys)
    assert payload["schema_version"] == work.NEXT_SCHEMA
    assert payload["command"] == "work next"


# ---- seat-typing: resolution when the caller declares none, refusal when it mismatches -------


def test_next_resolves_a_bare_actor_to_developer_for_a_leaf(nexthive, monkeypatch, capsys):
    """The caller declared no seat (no disp/dev/ prefix) — the server resolves it, per AGF's
    recursive dispatch rule, rather than leaving it untyped."""
    fake = _fake_bd(monkeypatch, FakeBd(ready=[_open("b1")]))
    code, payload = _run_next(capsys, as_="scheduler")
    assert code == 0
    assert (payload["status"], payload["actor"], payload["seat"]) == (
        "claimed",
        "dev/scheduler",
        "developer",
    )
    assert fake.claims == ["b1"]


def test_next_resolves_a_bare_actor_to_dispatcher_for_an_epic(nexthive, monkeypatch, capsys):
    fake = _fake_bd(monkeypatch, FakeBd(ready=[_open("e1", issue_type="epic")]))
    code, payload = _run_next(capsys, as_="scheduler")
    assert code == 0
    assert (payload["status"], payload["actor"], payload["seat"]) == (
        "claimed",
        "disp/scheduler",
        "dispatcher",
    )
    assert fake.claims == ["e1"]


def test_next_refuses_a_developer_seat_against_an_epic_only_queue(nexthive, monkeypatch, capsys):
    """A declared seat is VALIDATED, not trusted: a dev/ actor may not be handed an epic, and with
    nothing else ready this is a REFUSAL — distinct from a decline (`empty_queue`/`none_eligible`/
    `all_lost` all mean "nothing right now"; this means "you asked for something you can never
    have")."""
    fake = _fake_bd(monkeypatch, FakeBd(ready=[_open("e1", issue_type="epic")]))
    code, payload = _run_next(capsys, as_="dev/scheduler")
    assert code == work.NEXT_REFUSE_EXIT == 4
    assert (payload["status"], payload["reason"], payload["bead"]) == (
        "refused",
        "seat_mismatch",
        "",
    )
    assert payload["refused"] == ["e1"]
    assert payload["tried"] == []
    assert fake.claims == [], "a seat-mismatched candidate must never be claimed at"


def test_next_refuses_a_dispatcher_seat_against_a_leaf_only_queue(nexthive, monkeypatch, capsys):
    fake = _fake_bd(monkeypatch, FakeBd(ready=[_open("b1")]))
    code, payload = _run_next(capsys, as_="disp/scheduler")
    assert code == work.NEXT_REFUSE_EXIT
    assert (payload["status"], payload["reason"], payload["refused"]) == (
        "refused",
        "seat_mismatch",
        ["b1"],
    )
    assert fake.claims == []


def test_next_claims_the_matching_candidate_and_still_records_a_mismatched_one_as_refused(
    nexthive, monkeypatch, capsys
):
    """A mismatched candidate elsewhere in the ready queue does not block a legitimately-typed
    claim — it is simply not this seat's work — but it is still surfaced for visibility."""
    fake = _fake_bd(monkeypatch, FakeBd(ready=[_open("e1", issue_type="epic"), _open("b1")]))
    code, payload = _run_next(capsys, as_="dev/scheduler")
    assert code == 0
    assert (payload["status"], payload["bead"]) == ("claimed", "b1")
    assert payload["refused"] == ["e1"]
    assert fake.claims == ["b1"], "the mismatched epic must never be claimed at"


def test_next_declared_seat_matching_the_candidate_is_used_unchanged(nexthive, monkeypatch, capsys):
    fake = _fake_bd(monkeypatch, FakeBd(ready=[_open("e1", issue_type="epic")]))
    code, payload = _run_next(capsys, as_="disp/scheduler")
    assert code == 0
    assert (payload["status"], payload["actor"], payload["seat"]) == (
        "claimed",
        "disp/scheduler",
        "dispatcher",
    )
    assert fake.claims == ["e1"]


# ---- the config knob ---------------------------------------------------------


def test_max_action_retries_default_override_and_clamp():
    assert config.dispatch_max_action_retries({}, None) == work_next.DEFAULT_MAX_ACTION_RETRIES
    glob = {"work": {"dispatch": {"max_action_retries": 4}}}
    assert config.dispatch_max_action_retries(glob, {}) == 4
    # A threshold of 0 would escalate before anything had been tried.
    assert (
        config.dispatch_max_action_retries({"work": {"dispatch": {"max_action_retries": 0}}}, {})
        == 1
    )


# ---- the concurrency contract: two REAL drivers can never double-drive a bead ----------------
#
# The verb's whole reason to exist. `bd update --claim` is NOT a hard compare-and-swap (see the
# block comment above `next_`): two callers racing the SAME bead can both get exit 0, and
# whichever write physically lands last is who the store ends up naming as holder. The caller
# only learns the truth by re-reading (`work_next.claim_won`), never from the exit code. These
# tests race real OS threads (not a scripted `stolen_by` sequence like the tests above) against a
# `bd` double that mirrors that literal contract, and prove the guarantee two ways: the loop
# actually survives the race (this section), and the re-verify step is WHY (the mutation test at
# the bottom — remove it and the SAME race produces two winners).
#
# `_provision_claim` (real git worktree creation) is stubbed in every test below: it is already
# covered single-threaded in `test_next_claims_the_first_eligible_bead`, and letting two threads
# run real concurrent `git worktree add` against the same clone would test git's own locking, not
# the claim protocol these tests exist to prove.
#
# `config.load` is serialized (`_serialize_config_load`) for the same reason: its module-level
# `ruamel.yaml.YAML()` parser is not thread-safe, and two threads racing it hit ruamel's OWN
# threading bug, not anything about `bh work next`. A real unattended driver never hits this at
# all — each invocation is its OWN process with its own interpreter — so serializing it here is
# purely a test-harness accommodation for running two "drivers" as threads in one process, not a
# weakening of the property under test.

_config_load_lock = threading.Lock()


def _serialize_config_load(monkeypatch):
    orig_load = config.load

    def locked_load(*a, **kw):
        with _config_load_lock:
            return orig_load(*a, **kw)

    monkeypatch.setattr(config, "load", locked_load)


class RacyBd:
    """A `bd` double that mirrors `update --claim`'s real, documented contract literally — not a
    compare-and-swap: every concurrent caller against the same bead gets exit 0, and whichever
    write physically commits LAST is who the store ends up naming as holder.

    `contest={"b1": 2}` tells the fake exactly how many racers to expect on bead `b1`: a
    `threading.Barrier` of that size holds every one of them at TWO checkpoints — once before any
    of them writes, once after all of them have — so the write genuinely overlaps across real OS
    threads (one caller can never finish, and report success, before the other has even reached
    the store) while leaving WHICH actor's write physically lands last to real thread scheduling,
    never to a scripted answer. A bead absent from `contest` gets no forced rendezvous — plain
    check-then-write, for candidates nobody else is racing.
    """

    def __init__(self, ready, contest=None):
        self.beads = {b["id"]: dict(b) for b in ready}
        self.order = [b["id"] for b in ready]
        self.lock = threading.Lock()
        self.claims: list[str] = []
        self._barriers = {bead: threading.Barrier(n) for bead, n in (contest or {}).items()}

    def __call__(self, cmd, **_kw):
        args = list(cmd[1:])
        actor = ""
        while args and args[0] in ("-C", "--actor"):
            if args[0] == "--actor":
                actor = args[1]
            args = args[2:]
        return self._dispatch(actor, [a for a in args if a != "--json"])

    def _dispatch(self, actor, args):
        sub = args[0] if args else ""
        if sub == "ready":
            with self.lock:
                rows = [self.beads[i] for i in self.order if self.beads[i]["status"] == "open"]
            return _CP(0, json.dumps(rows), "")
        if sub == "show":
            with self.lock:
                row = self.beads.get(args[1])
                row = dict(row) if row else None
            return _CP(0 if row else 1, json.dumps(row) if row else "", "")
        if sub == "update" and "--claim" in args:
            bead = args[1]
            with self.lock:
                self.claims.append(bead)
                # bd's real precondition check (verified empirically against the actual binary:
                # a claim on an ALREADY in_progress bead is refused outright, every time — this is
                # what makes an ordinary, non-overlapping second claim fail cleanly rather than
                # silently double-report). Only a request that still sees `open` proceeds to race.
                still_open = self.beads.get(bead, {}).get("status", "open") == "open"
            if not still_open:
                return _CP(1, "", f"issue already claimed: {bead}")
            barrier = self._barriers.get(bead)
            if barrier is not None:
                barrier.wait(timeout=10)  # every racer arrives before ANY of them writes
            time.sleep(random.uniform(0, 0.005))  # widen the window; real scheduling decides
            with self.lock:
                row = self.beads.setdefault(bead, {"id": bead})
                row["assignee"] = actor  # unconditional overwrite — no compare, exactly as bd
                row["status"] = "in_progress"
            if barrier is not None:
                barrier.wait(timeout=10)  # every racer's write is committed before ANY returns
            return _CP(0, "", "")
        if sub == "update" and "--status" in args:
            bead = self.beads.setdefault(args[1], {"id": args[1]})
            if "--status" in args:
                bead["status"] = args[args.index("--status") + 1]
            if "--assignee" in args:
                bead["assignee"] = args[args.index("--assignee") + 1]
            return _CP(0, "", "")
        if sub == "set-state":
            return _CP(0, "", "")
        return _CP(0, "", "")


def _capture(bucket, lock, payload):
    with lock:
        bucket.append(payload)


def _stub_provision(monkeypatch):
    """Stand in for real worktree creation (see the section docstring above)."""
    monkeypatch.setattr(
        work, "_provision_claim", lambda cfg, hive, main, bead, actor: (f"/fake/{bead}", {})
    )


def test_next_race_two_real_drivers_one_bead_exactly_one_wins(nexthive, monkeypatch):
    """THE central guarantee, proven under genuine concurrency: two real OS threads calling
    `bh work next` against the SAME single ready bead. Exactly one ends up holding it; the other
    declines cleanly — never crashes, and never ends up believing it holds a bead it does not."""
    fake = RacyBd(ready=[_open("b1")], contest={"b1": 2})
    monkeypatch.setattr(bd_mod, "_run", fake)
    _stub_provision(monkeypatch)
    _serialize_config_load(monkeypatch)
    captured: list[dict] = []
    cap_lock = threading.Lock()
    monkeypatch.setattr(work.jsonout, "emit", lambda p: _capture(captured, cap_lock, p))

    exits: dict[str, int] = {}
    exits_lock = threading.Lock()
    start = threading.Barrier(2)

    def go(name):
        start.wait(timeout=10)
        code = 0
        try:
            work.next_(as_=name, hive="mr", as_json=True)
        except typer.Exit as exc:
            code = exc.exit_code
        with exits_lock:
            exits[name] = code

    threads = [threading.Thread(target=go, args=(n,)) for n in ("dev/a", "dev/b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    claimed = [e for e in captured if e["status"] == "claimed"]
    declined = [e for e in captured if e["status"] == "declined"]
    assert len(claimed) == 1, captured  # never zero, never both
    assert len(declined) == 1, captured
    assert claimed[0]["bead"] == "b1"
    assert declined[0]["reason"] == "all_lost"  # the loser tried it, then lost the race
    winner_actor = claimed[0]["actor"]
    loser_actor = "dev/b" if winner_actor == "dev/a" else "dev/a"
    assert exits[winner_actor] == 0
    assert exits[loser_actor] == work.NEXT_DECLINE_EXIT
    # the store's own final state agrees with who the loop believes won — no split brain
    assert fake.beads["b1"]["assignee"] == winner_actor


def test_next_race_two_drivers_against_a_queue_no_double_claim_no_drop(nexthive, monkeypatch):
    """A queue of several ready beads, two drivers looping `bh work next` until each declines on
    an empty queue. Losing the first (contested) bead's race must not drop that driver out of the
    run — it advances to the next candidate — and across the whole run every bead is claimed
    exactly once: none twice, none silently skipped."""
    ids = [f"b{i}" for i in range(1, 6)]
    fake = RacyBd(ready=[_open(i) for i in ids], contest={ids[0]: 2})
    monkeypatch.setattr(bd_mod, "_run", fake)
    _stub_provision(monkeypatch)
    _serialize_config_load(monkeypatch)
    captured: list[dict] = []
    cap_lock = threading.Lock()
    monkeypatch.setattr(work.jsonout, "emit", lambda p: _capture(captured, cap_lock, p))
    start = threading.Barrier(2)

    def driver(name):
        start.wait(timeout=10)
        while True:
            try:
                work.next_(as_=name, hive="mr", as_json=True)
            except typer.Exit as exc:
                if exc.exit_code == work.NEXT_DECLINE_EXIT:
                    return
                raise  # any other exit (refusal, hard error) is a real failure, not "keep going"

    threads = [threading.Thread(target=driver, args=(n,)) for n in ("dev/a", "dev/b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    claims = [e for e in captured if e["status"] == "claimed"]
    claimed_ids = [e["bead"] for e in claims]
    assert sorted(claimed_ids) == ids, claims  # every bead claimed — none silently skipped
    assert len(claimed_ids) == len(set(claimed_ids)), claims  # never claimed twice
    first = next(e for e in claims if e["bead"] == ids[0])
    assert first["actor"] in ("dev/a", "dev/b")  # the contested one still went to exactly one


def test_claim_won_reverify_is_load_bearing_without_it_both_drivers_win(nexthive, monkeypatch):
    """Mutation test: bypass the re-verify half of pick-claim-verify — `work_next.claim_won`
    forced to trust the exit code alone, the same mistake a driver would make by not calling it —
    and re-run the IDENTICAL one-bead race from the test above. Both drivers now believe they
    hold `b1`: exactly the double-claim `claim_won`'s re-read exists to prevent, and exactly
    what does NOT happen with the guard intact (see the "exactly one wins" test above)."""
    fake = RacyBd(ready=[_open("b1")], contest={"b1": 2})
    monkeypatch.setattr(bd_mod, "_run", fake)
    _stub_provision(monkeypatch)
    _serialize_config_load(monkeypatch)
    monkeypatch.setattr(work_next, "claim_won", lambda *_a, **_k: True)  # THE MUTATION
    captured: list[dict] = []
    cap_lock = threading.Lock()
    monkeypatch.setattr(work.jsonout, "emit", lambda p: _capture(captured, cap_lock, p))
    start = threading.Barrier(2)

    def go(name):
        start.wait(timeout=10)
        try:
            work.next_(as_=name, hive="mr", as_json=True)
        except typer.Exit:
            pass  # only the claimed/declined envelope matters here, not the exit code

    threads = [threading.Thread(target=go, args=(n,)) for n in ("dev/a", "dev/b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    claimed = [e for e in captured if e["status"] == "claimed"]
    assert len(claimed) == 2, claimed  # WITH the re-verify bypassed, both believe they won
    assert {e["bead"] for e in claimed} == {"b1"}
    assert {e["actor"] for e in claimed} == {"dev/a", "dev/b"}
