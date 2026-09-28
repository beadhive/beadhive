"""`bh work next` — the decision core (pure) and the pick → claim → re-verify loop (CLI).

The pure half needs no fixtures at all: `work_next.decide` takes plain `bd` JSON dicts and returns
a dataclass, so the 12-row priority table is tested AS a table. The CLI half fakes `bd` at the
`bd._run` seam (the single subprocess boundary every `bd` call in `work.py` goes through) but DOES
touch real git (bh-qczj.2): a won claim provisions a real worktree via `worktree.ensure`, so the
`nexthive` fixture is a real (committed) git repo, not a bare directory.

Since bh-l5sxi.2, this is the CLI-COMPATIBILITY route only — the explicitly-selected fallback
`beadhive.work_queue` picks for an unscoped undeclared actor, or for any claim (`--epic` scoped or
not) in a hive with no capable Beads service. The atomic `work.claim-next` route and the `--epic`
guarded-claim route (bh-7ip8t), with their real-contention proofs, live in `packages/beadhive-core`;
`tests/test_work_queue.py` covers the seam that chooses between them.
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
    events = [
        {**_event("review -> pending"), "id": "ev.1", "created_at": "2026-01-01T00:00:00Z"},
        {
            **_event("merge conflict onto main"),
            "id": "ev.2",
            "created_at": "2026-01-01T00:01:00Z",
        },
    ]
    assert work_next.attempt_count(events, "resume") == 0
    assert work_next.attempt_count(events, "merge") == 1


def test_attempt_count_resets_after_a_later_submit():
    """bh-7679k: a bead that failed once and then submitted must not carry that failure into its
    next dispatch cycle — the submit (`review -> pending`) event ends the sequence, so only events
    AFTER it count."""
    events = [
        {**_event("dispatched"), "id": "ev.1", "created_at": "2026-01-01T00:00:00Z"},
        {**_event("dispatched"), "id": "ev.2", "created_at": "2026-01-01T00:01:00Z"},
        {
            **_event("review -> pending"),
            "id": "ev.3",
            "created_at": "2026-01-01T00:02:00Z",
        },
    ]
    assert work_next.attempt_count(events, "dispatch") == 0
    events_with_a_later_failure = events + [
        {**_event("dispatched"), "id": "ev.4", "created_at": "2026-01-01T00:03:00Z"}
    ]
    assert work_next.attempt_count(events_with_a_later_failure, "dispatch") == 1


def test_attempt_count_normalizes_real_newest_first_bounce_history():
    """Real bd list order is newest-first. At bounce time the failure precedes the older pending
    row in the returned list; chronology normalization must retain that one post-submit retry."""
    events = [
        {
            "id": "bh-hnnlb.2",
            "issue_type": "event",
            "title": "State change: review → changes-requested",
            "description": "Changed review from pending to changes-requested",
            "created_at": "2026-09-13T01:35:47Z",
        },
        {
            "id": "bh-hnnlb.1",
            "issue_type": "event",
            "title": "State change: review → pending",
            "description": "Set review to pending\n\nReason: submitted b77b8569",
            "created_at": "2026-09-13T01:06:38Z",
        },
    ]

    assert work_next.attempt_count(events, "resume") == 1


def test_chronology_fallback_is_natural_id_order_not_input_order():
    rows = [{"id": "bh-x.10"}, {"id": "bh-x.2"}, {"id": "bh-x.1"}]
    expected = ["bh-x.1", "bh-x.2", "bh-x.10"]
    assert [row["id"] for row in work_next.chronological_rows(rows)] == expected
    assert [row["id"] for row in work_next.chronological_rows(reversed(rows))] == expected


@pytest.mark.parametrize(
    "event",
    [
        {
            "to_state": "review=pending",
            "title": "State change: review → changes-requested",
        },
        {
            "title": "State change: review → pending; review -> changes-requested",
        },
        {
            "title": "State change: review → pending",
            "description": "Changed review from pending to changes-requested",
        },
        {"title": "State change: review pending"},
    ],
)
def test_transition_destination_fails_closed_on_ambiguous_or_malformed_events(event):
    assert work_next.transition_destination(event, "review") == ""


def test_transition_destination_accepts_multiple_sources_only_when_they_agree():
    event = {
        "to_state": "review=pending",
        "title": "State change: review → pending",
        "description": "Changed review from changes-requested to pending",
    }
    assert work_next.transition_destination(event, "review") == "pending"


def test_loop_breaker_never_escalates_a_bead_whose_review_gate_is_open():
    """bh-7679k: submitted-and-awaiting-review is not stuck — the guard holds even if a stale/
    pre-submit event count would otherwise trip the loop-breaker for the naming decision."""
    beads = (_bead("b1", labels=["review:pending"]),)
    events = {"b1": [_event("dispatched"), _event("dispatched")]}
    decision = work_next.Decision("dispatch-up-to-budget", "dispatch", beads=("b1",))
    mol = _mol(beads=beads, events=events)
    assert work_next.loop_break(mol, decision) == decision


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
  beads: {route: "api+cli-fallback"}  # FakeBd-backed: opt into the bd route (bh-m36pc)
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

    def __init__(self, ready=(), stolen_by=None, children=None):
        self.beads = {b["id"]: dict(b) for b in ready}
        self.order = [b["id"] for b in ready]
        self.stolen_by = dict(stolen_by or {})  # bead id -> the actor that wins the race
        self.claims = []  # bead ids a claim was attempted on, in order
        self.ready_args: list[list[str]] = []  # every `bd ready` argv, so truncation is visible
        self.children = {k: list(v) for k, v in (children or {}).items()}  # epic id -> child ids
        self.list_args: list[list[str]] = []  # every `bd list` argv, so the scope read is visible

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
            self.ready_args.append(list(args))
            rows = [self.beads[i] for i in self.order if self.beads[i]["status"] == "open"]
            # bd's REAL default: a `ready` read with no `--limit` is CAPPED AT 100 rows, and a
            # truncated result is indistinguishable from a short one. Modelled here because an
            # unattended driver is exactly the caller that cannot notice (bh-fruer).
            if "--limit" not in args:
                rows = rows[:100]
            return _CP(0, json.dumps(rows), "")
        if sub == "list":
            # `bd list --parent <epic>` — ONE level, exactly as bd serves it. The molecule scope
            # (`--epic`) edge-filters this raw prefix result, so a fake that recursed would test
            # a membership rule the product does not have.
            self.list_args.append(list(args))
            parent = args[args.index("--parent") + 1] if "--parent" in args else ""
            kids = self.children.get(parent, [])
            rows = []
            for kid in kids:
                row = dict(self.beads.get(kid) or {"id": kid})
                row.setdefault("parent", parent)
                rows.append(row)
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


# ---- the concurrency contract now lives against the real runtime (bh-l5sxi.2) ----------------
#
# This section used to prove "two real drivers can never double-drive a bead" with `RacyBd`, an
# in-memory double built to mirror `bd update --claim`'s literal, documented non-CAS contract —
# exactly the kind of FakeBd readiness/race engine bh-l5sxi.2's acceptance retires: "no FakeBd
# readiness engine or less-atomic substitute is retained as evidence" of atomicity. `bh work next`
# now selects the atomic `work.claim-next` route whenever a declared actor and a capable Beads
# service are both available (`beadhive.work_queue`), and that route's exclusivity under real
# contention is proven against a real, disposable Beads 1.3.0 service — not a fake — by
# `packages/beadhive-core/tests/test_core_queue_real_service.py::
# test_claim_next_is_exclusive_under_real_contention`. `tests/test_work_queue.py` covers the
# composition seam itself: which route gets selected, and when.
#
# The CLI-compatibility pick/claim/re-verify loop this file's other tests exercise (`FakeBd`,
# `_try_claim`, `work_next.claim_won`) remains: it is still the explicitly-selected route for an
# unscoped undeclared actor, or — `--epic` scoped or not — a hive with no capable Beads service
# whose `work.beads.route` permits the `bd` route (these fixtures opt in). The `--epic` API route
# (bh-7ip8t) is covered in `tests/test_work_queue.py` and against a real service in
# `packages/beadhive-core/tests/test_core_epic_claim_real_service.py`.


# ---- the ready read must not be truncated (bh-fruer, P0) -----------------------------------


def test_next_reads_the_WHOLE_ready_set_not_bd_s_default_100(nexthive, monkeypatch, capsys):
    """`bd ready` caps at 100 rows. `bh work next` is the atomic claim verb an UNATTENDED driver
    polls, so a truncated queue means every bead past position 100 is never claimed — silently,
    since nothing distinguishes a truncated read from a short one. `bh work ready --json` at
    least signals it out of band (READY_TRUNCATED_EXIT); this verb has no such channel, so it
    must ask for everything."""
    beads = [_open(f"b{i:03d}") for i in range(150)]
    # The only claimable bead is past bd's default cap — unreachable without `--limit 0`.
    for bead in beads[:-1]:
        bead["assignee"] = "dev/someone-else"
        bead["status"] = "in_progress"
    fake = _fake_bd(monkeypatch, FakeBd(ready=beads))

    code, payload = _run_next(capsys)

    assert fake.ready_args and fake.ready_args[0][:3] == ["ready", "--limit", "0"]
    assert code == 0
    assert payload["bead"] == "b149", "the 150th ready bead must still be reachable"


# ---- molecule scope: `--epic` bounds the IDENTITY, not just the count (bh-sh6yt, P0) --------
#
# `bh work loop <epic>` is required to claim through this verb, and this verb had no way to say
# "only this molecule" — so a loop pointed at a two-bead epic spawned live seats for beads in
# other molecules, provisioned worktrees for them, and flipped them to in_progress. The epic
# bounded how MANY seats spawned and never bounded WHICH beads they took.


def _molecule_hive(monkeypatch, epic="e1", mine=("e1.1", "e1.2"), theirs=("other-1", "other-2")):
    """A hive whose ready set holds one molecule's beads AND unrelated ready work.

    `theirs` is listed FIRST so a verb with no scope reaches for it — the failing order is the
    default, not something the test has to contrive.
    """
    ready = [_open(b) for b in (*theirs, *mine)]
    return _fake_bd(monkeypatch, FakeBd(ready=ready, children={epic: list(mine)}))


def test_next_epic_scope_never_claims_a_bead_outside_the_molecule(nexthive, monkeypatch, capsys):
    """THE REGRESSION (bh-sh6yt). Unrelated ready work sits at the head of the queue; a scoped
    call must walk past all of it rather than claim the first thing it sees."""
    fake = _molecule_hive(monkeypatch)

    code, payload = _run_next(capsys, epic="e1")

    assert code == 0
    assert payload["bead"] == "e1.1"
    assert fake.claims == ["e1.1"], "no out-of-molecule bead may even be ATTEMPTED"
    assert payload["tried"] == ["e1.1"]
    assert fake.beads["other-1"]["status"] == "open", "an unrelated bead must be left alone"
    assert fake.beads["other-2"]["status"] == "open"


def test_next_epic_scope_excludes_a_detached_dotted_prefix_match(nexthive, monkeypatch, capsys):
    """bd's raw parent query also returns dotted-id matches without a parent edge; they are not
    molecule members and must never enter the scoped claim candidates."""
    attached = _open("e1.1", parent="e1")
    detached = _open("e1.2", parent="elsewhere")
    fake = _fake_bd(
        monkeypatch,
        FakeBd(ready=[detached, attached], children={"e1": ["e1.2", "e1.1"]}),
    )

    code, payload = _run_next(capsys, epic="e1")

    assert (code, payload["bead"]) == (0, "e1.1")
    assert fake.claims == ["e1.1"]
    assert fake.beads["e1.2"]["status"] == "open"


def test_next_epic_scope_admits_the_epic_itself(nexthive, monkeypatch, capsys):
    """The molecule is the epic PLUS its children — `start` / `finish` name the epic, so a scope
    that admitted only children would lock the loop out of its own container bead."""
    fake = _fake_bd(
        monkeypatch,
        FakeBd(ready=[_open("other-1"), _open("e1", issue_type="epic")], children={"e1": []}),
    )

    code, payload = _run_next(capsys, as_="disp/next", epic="e1")

    assert (code, payload["bead"]) == (0, "e1")
    assert fake.claims == ["e1"]


def test_next_epic_scope_declines_rather_than_reaching_outside_the_molecule(
    nexthive, monkeypatch, capsys
):
    """A molecule with nothing takeable is a DECLINE, not a licence to take someone else's work.
    This is the case the bug got wrong most expensively: it spent a model turn per stolen bead."""
    fake = _fake_bd(
        monkeypatch,
        FakeBd(ready=[_open("other-1"), _open("other-2")], children={"e1": ["e1.1"]}),
    )
    fake.beads["e1.1"] = {"id": "e1.1", "status": "closed", "assignee": "", "issue_type": "task"}

    code, payload = _run_next(capsys, epic="e1")

    assert code == work.NEXT_DECLINE_EXIT
    assert payload["status"] == "declined"
    assert fake.claims == [], "nothing outside the molecule may be claimed on a decline"


def test_next_epic_scope_only_removes_rows_bd_never_stops_being_the_blocking_authority(
    nexthive, monkeypatch, capsys
):
    """A molecule member `bd ready` did NOT return stays unclaimable. The filter is a subset of
    the ready set, never a second source of truth about what is ready."""
    fake = _fake_bd(
        monkeypatch,
        # e1.1 is a member but blocked (absent from `ready`); only e1.2 is actually takeable.
        FakeBd(ready=[_open("e1.2")], children={"e1": ["e1.1", "e1.2"]}),
    )

    code, payload = _run_next(capsys, epic="e1")

    assert (code, payload["bead"]) == (0, "e1.2")
    assert fake.claims == ["e1.2"]


def test_next_without_epic_is_unchanged_and_reads_no_membership(nexthive, monkeypatch, capsys):
    """The human verb must not acquire a molecule opinion. Unscoped, the candidate set is still
    the whole hive and the extra `bd list --parent` read is not even issued."""
    fake = _molecule_hive(monkeypatch)

    code, payload = _run_next(capsys)

    assert (code, payload["bead"]) == (0, "other-1"), "unscoped still takes the head of the queue"
    assert fake.list_args == [], "no membership read may happen when no scope was asked for"


def test_next_epic_scope_reads_membership_one_level_matching_the_loop_s_own_molecule(
    nexthive, monkeypatch, capsys
):
    """Membership is one edge-filtered parent read with the same options LocalLoop uses. If the
    two ever diverge the loop decides against one set and claims against another."""
    fake = _molecule_hive(monkeypatch)

    _run_next(capsys, epic="e1")

    assert fake.list_args == [
        ["list", "--parent", "e1", "--limit", "0", "--include-infra", "--all"]
    ], (
        "exactly ONE membership read: `_molecule_members` shells out to `bd`, so resolving it "
        "per candidate row would spawn a subprocess per ready bead"
    )
