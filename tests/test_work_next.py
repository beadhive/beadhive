"""`bh work next` — the decision core (pure) and the pick → claim → re-verify loop (CLI).

The pure half needs no fixtures at all: `work_next.decide` takes plain `bd` JSON dicts and returns
a dataclass, so the 12-row priority table is tested AS a table. The CLI half fakes `bd` at the
`bd._run` seam (the single subprocess boundary every `bd` call in `work.py` goes through) and
never touches git — this slice provisions no worktree.
"""

from __future__ import annotations

import json
import subprocess
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
        return _CP(0, "", "")


@pytest.fixture
def nexthive(tmp_path, monkeypatch):
    """A hive `bh work next` can resolve, with `bd` faked and the host-lease/state seams stubbed.
    No git: this slice provisions no worktree, so there is nothing for git to do."""
    ws_root = tmp_path / "ws"
    main = ws_root / "github" / "myorg" / "myrepo"
    main.mkdir(parents=True)
    # A real (empty) git repo: `registry.entry_for_dir` resolves the hive via
    # `git rev-parse --show-toplevel`, so a bare directory is a hive nowhere.
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(CONFIG_YAML)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("WS_CONFIG", str(cfg_path))
    monkeypatch.setenv("WS_WORKTREES", str(tmp_path / "wts"))
    monkeypatch.setattr(guard, "guard_primary", lambda *a, **k: None)
    monkeypatch.setattr(work, "_pull_state", lambda *a, **k: None)
    return SimpleNamespace(main=main, cfg_path=cfg_path)


def _fake_bd(monkeypatch, fake):
    monkeypatch.setattr(bd_mod, "_run", fake)
    return fake


def _open(bead_id, **kw):
    return {"id": bead_id, "status": "open", "assignee": "", "issue_type": "task", **kw}


def _run_next(capsys, **kw):
    """Invoke the verb as a plain function; returns (exit code, parsed json payload)."""
    code = 0
    try:
        work.next_(as_="dev/next", hive="mr", as_json=True, **kw)
    except typer.Exit as exc:
        code = exc.exit_code
    return code, json.loads(capsys.readouterr().out)


def test_next_claims_the_first_eligible_bead(nexthive, monkeypatch, capsys):
    fake = _fake_bd(monkeypatch, FakeBd(ready=[_open("b1"), _open("b2")]))
    code, payload = _run_next(capsys)
    assert code == 0
    assert (payload["status"], payload["bead"], payload["actor"]) == ("claimed", "b1", "dev/next")
    assert payload["seat"] == "developer"
    assert payload["worktree"] is None  # bh-qczj.2 owns provisioning; this slice has none
    assert fake.claims == ["b1"]


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


def test_next_declines_with_a_distinct_exit_code_on_an_empty_queue(nexthive, monkeypatch, capsys):
    _fake_bd(monkeypatch, FakeBd(ready=[]))
    code, payload = _run_next(capsys)
    assert code == work.NEXT_DECLINE_EXIT == 3
    assert (payload["status"], payload["reason"], payload["bead"]) == (
        "declined",
        "empty_queue",
        "",
    )


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
