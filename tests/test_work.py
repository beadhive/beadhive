"""`ws work` self-checks — the WS-WORK-IMPL checklist.

Real git in tmp_path (worktrees, identity stamping, push) + a faked `bd`. The test seam:
work.py shells out to `bd` ONLY through `ws.work.run`, so we patch that one symbol to fake
Beads while every git/worktree op runs for real. Non-`bd` calls (the validation command in
`check`) delegate to the real runner.
"""

from __future__ import annotations

import json
import os
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from ws import config, registry, work, worktree
from ws.run import run as real_run

_CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
_CP = namedtuple("CP", "returncode stdout stderr")

CONFIG_YAML = """\
providers: [github]
work:
  validate_cmd: "true"
  review_gate: "human"
  identity: {mode: agent, name: "crew/default", email: "agents@test.dev"}
managed_repos:
  - {provider: github, org: myorg, repo: myrepo, prefix: mr, kind: personal}
"""


def _git(*args, cwd):
    return real_run(["git", *args], cwd=str(cwd), check=True, capture=True, env=_CLEAN_ENV)


def _cfg_get(wt, key):
    cmd = ["git", "config", "--get", key]
    r = real_run(cmd, cwd=str(wt), check=False, capture=True, env=_CLEAN_ENV)
    return (r.stdout or "").strip()


def _commit(wt, msg, fname="change.txt"):
    (Path(wt) / fname).write_text(msg)
    _git("add", "-A", cwd=wt)
    _git("commit", "-qm", msg, cwd=wt)


# ---- fake bd ---------------------------------------------------------------


class FakeBd:
    """Stand-in for the `bd` CLI. Records calls; mutates in-memory bead + state stores.
    Anything that isn't a `bd` invocation is delegated to the real subprocess runner."""

    def __init__(self):
        self.beads = {}  # id -> {"id","title","status","assignee","description",...}
        self.states = {}  # id -> {dimension: value}
        self.gates = []  # [{"id","status","description"}] — review gates blocking a bead
        self.calls = []  # (actor, [args]) for every bd call

    def seed(self, bead_id, **fields):
        self.beads[bead_id] = {"id": bead_id, "status": "open", "assignee": "", **fields}

    def __call__(self, cmd, *, check=True, capture=False, env=None, cwd=None, text_input=None):
        if not cmd or cmd[0] != "bd":
            return real_run(
                cmd, check=check, capture=capture, env=env, cwd=cwd, text_input=text_input
            )
        # strip leading global flags: -C <dir> and --actor <name> (any order)
        args = cmd[1:]
        actor = None
        while args and args[0] in ("-C", "--actor"):
            if args[0] == "--actor":
                actor = args[1]
            args = args[2:]
        self.calls.append((actor, args))
        return self._dispatch(actor, args)

    def _dispatch(self, actor, args):
        sub = args[0] if args else ""
        if sub == "show":
            data = self.beads.get(args[1])
            return _CP(0 if data else 1, json.dumps(data) if data else "", "")
        if sub == "state":
            return _CP(0, self.states.get(args[1], {}).get(args[2], ""), "")
        if sub == "assign":
            self.beads.setdefault(args[1], {"id": args[1], "status": "open"})["assignee"] = args[2]
            return _CP(0, "", "")
        if sub == "update":
            bead = self.beads.setdefault(args[1], {"id": args[1]})
            if "--claim" in args:
                bead.update(assignee=actor, status="in_progress")
            if "--status" in args:
                bead["status"] = args[args.index("--status") + 1]
            if "--assignee" in args:
                bead["assignee"] = args[args.index("--assignee") + 1]
            return _CP(0, "", "")
        if sub == "set-state":
            dim, _, val = args[2].partition("=")
            self.states.setdefault(args[1], {})[dim] = val
            return _CP(0, "", "")
        if sub == "close":
            bead = self.beads.setdefault(args[1], {"id": args[1]})
            bead["status"] = "closed"
            return _CP(0, "", "")
        if sub == "list":
            if "--parent" in args:
                parent = args[args.index("--parent") + 1]
                kids = [b for b in self.beads.values() if b.get("parent") == parent]
                return _CP(0, json.dumps(kids), "")
            return _CP(0, json.dumps(list(self.beads.values())), "")
        if sub == "gate":
            return self._gate(args[1:])
        if sub == "merge-slot":
            return _CP(0, "", "")  # acquire/release/check always succeed in the fake
        if sub == "comments":
            return _CP(0, "ok", "")
        return _CP(0, "", "")

    def _gate(self, args):
        op = args[0] if args else ""
        if op == "create":
            bead = args[args.index("--blocks") + 1] if "--blocks" in args else ""
            self.gates.append(
                {"id": f"g{len(self.gates)}", "status": "open", "description": f"blocks {bead}"}
            )
            return _CP(0, "", "")
        if op == "list":
            return _CP(0, json.dumps(self.gates), "")
        if op == "resolve":
            for g in self.gates:
                if g["id"] == args[1]:
                    g["status"] = "closed"
            return _CP(0, "", "")
        return _CP(0, "", "")

    def approve(self, bead):
        """Reviewer approves: resolve every open gate blocking `bead` (mirrors gate resolve)."""
        for g in self.gates:
            if g["status"] == "open" and bead in g["description"]:
                g["status"] = "closed"

    def did(self, *needles):
        """True iff some recorded call's args contain all needle tokens."""
        return any(all(n in args for n in needles) for _actor, args in self.calls)


# ---- fixtures --------------------------------------------------------------


@pytest.fixture
def rig(tmp_path, monkeypatch):
    ws_root = tmp_path / "ws"
    main = ws_root / "github" / "myorg" / "myrepo"
    main.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.email", "human@example.com", cwd=main)
    _git("config", "user.name", "human", cwd=main)
    (main / "README.md").write_text("# x\n")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "chore: init", cwd=main)

    remote = tmp_path / "remote.git"
    _git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    _git("remote", "add", "origin", str(remote), cwd=main)

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(CONFIG_YAML)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("WS_WORKTREES", str(tmp_path / "wts"))
    monkeypatch.setenv("WS_CONFIG", str(cfg_path))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.delenv("WS_CREW", raising=False)
    return SimpleNamespace(main=main, wts=tmp_path / "wts", remote=remote, cfg_path=cfg_path)


@pytest.fixture
def fakebd(monkeypatch):
    fb = FakeBd()
    monkeypatch.setattr(work, "run", fb)
    return fb


def _wt(rig, bead):
    return rig.wts / "github" / "myorg" / "myrepo" / bead


def _remote_has(rig, branch):
    cmd = ["git", "branch", "--list", branch]
    out = real_run(cmd, cwd=str(rig.remote), check=False, capture=True, env=_CLEAN_ENV).stdout
    return bool((out or "").strip())


# ---- the history guard (ponytail self-check) -------------------------------


def test_history_ok_rules():
    assert work._history_ok(2, ["feat: a", "fix(x): b"], 10)[0]
    assert not work._history_ok(0, [], 10)[0]  # nothing to submit
    assert not work._history_ok(11, ["feat: x"] * 11, 10)[0]  # too many commits
    assert not work._history_ok(1, ["wip junk"], 10)[0]  # non-conventional
    assert not work._history_ok(-1, [], 10)[0]  # base missing


# ---- claim -----------------------------------------------------------------


def test_claim_provisions_worktree_with_identity(rig, fakebd):
    fakebd.seed("mr-1", title="t")
    work.claim(bead="mr-1", as_="", rig="myrepo")
    wt = _wt(rig, "mr-1")
    assert wt.exists()
    assert _cfg_get(wt, "user.name") == "crew/default"
    assert _cfg_get(wt, "user.email") == "agents@test.dev"
    # agent identity with no key → signing pinned off (don't inherit the human's global key)
    assert _cfg_get(wt, "commit.gpgsign") == "false"
    assert fakebd.beads["mr-1"]["status"] == "in_progress"
    assert fakebd.did("update", "mr-1", "--claim")
    assert ("crew/default", ["update", "mr-1", "--claim"]) in fakebd.calls


def test_claim_as_flag_overrides_identity(rig, fakebd):
    fakebd.seed("mr-1", title="t")
    work.claim(bead="mr-1", as_="crew/alice", rig="myrepo")
    assert _cfg_get(_wt(rig, "mr-1"), "user.name") == "crew/alice"
    assert ("crew/alice", ["update", "mr-1", "--claim"]) in fakebd.calls


def test_claim_twice_reattaches(rig, fakebd):
    fakebd.seed("mr-1", title="t")
    work.claim(bead="mr-1", as_="", rig="myrepo")
    work.claim(bead="mr-1", as_="", rig="myrepo")  # no exception
    assert _wt(rig, "mr-1").exists()


def test_claim_refuses_other_actor(rig, fakebd):
    fakebd.seed("mr-1", title="t", assignee="crew/bob")
    with pytest.raises(typer.Exit):
        work.claim(bead="mr-1", as_="crew/alice", rig="myrepo")
    assert not _wt(rig, "mr-1").exists()  # refused before provisioning


def test_claim_signing_config_when_key_set(rig, fakebd, monkeypatch):
    monkeypatch.setattr(
        config,
        "work_identity",
        lambda cfg, entry: {
            "mode": "agent",
            "name": "crew/signer",
            "email": "s@test.dev",
            "signing_key": "/keys/x.pub",
            "sign": True,
        },
    )
    fakebd.seed("mr-1", title="t")
    work.claim(bead="mr-1", as_="", rig="myrepo")
    wt = _wt(rig, "mr-1")
    assert _cfg_get(wt, "gpg.format") == "ssh"
    assert _cfg_get(wt, "commit.gpgsign") == "true"
    assert _cfg_get(wt, "user.signingkey") == "/keys/x.pub"


def test_claim_supervised_leaves_identity(rig, fakebd, monkeypatch):
    monkeypatch.setattr(
        config,
        "work_identity",
        lambda cfg, entry: {
            "mode": "supervised",
            "name": None,
            "email": None,
            "signing_key": None,
            "sign": False,
        },
    )
    fakebd.seed("mr-1", title="t")
    work.claim(bead="mr-1", as_="", rig="myrepo")
    # no stamp → worktree inherits the human's identity; we never enable per-worktree config
    assert _cfg_get(_wt(rig, "mr-1"), "user.name") == "human"
    assert _cfg_get(_wt(rig, "mr-1"), "extensions.worktreeConfig") == ""


def test_concurrent_claims_keep_separate_identities(rig, fakebd):
    """Two beads claimed as different actors must not clobber each other's git identity."""
    fakebd.seed("mr-8", title="a")
    fakebd.seed("mr-9", title="b")
    work.claim(bead="mr-8", as_="crew/alice", rig="myrepo")
    work.claim(bead="mr-9", as_="crew/bob", rig="myrepo")
    assert _cfg_get(_wt(rig, "mr-8"), "user.name") == "crew/alice"
    assert _cfg_get(_wt(rig, "mr-9"), "user.name") == "crew/bob"


# ---- assign → claim handshake ----------------------------------------------


def test_assign_then_claim(rig, fakebd):
    fakebd.seed("mr-2", title="t")
    work.assign(bead="mr-2", to="crew/carol", rig="myrepo")
    assert fakebd.beads["mr-2"]["status"] == "open"  # assignment is not the ack
    assert fakebd.beads["mr-2"]["assignee"] == "crew/carol"
    assert _cfg_get(_wt(rig, "mr-2"), "user.name") == "crew/carol"

    work.claim(bead="mr-2", as_="crew/carol", rig="myrepo")
    assert fakebd.beads["mr-2"]["status"] == "in_progress"  # claim is the ack


# ---- submit ----------------------------------------------------------------


def test_submit_rejects_noisy_history(rig, fakebd):
    fakebd.seed("mr-3", title="t")
    work.claim(bead="mr-3", as_="", rig="myrepo")
    _commit(_wt(rig, "mr-3"), "wip junk")  # non-conventional subject
    with pytest.raises(typer.Exit):
        work.submit(bead="mr-3", rig="myrepo")
    assert "review" not in fakebd.states.get("mr-3", {})  # no state change
    assert not fakebd.did("set-state", "mr-3")


def test_submit_clean_local_gate_no_push(rig, fakebd):
    fakebd.seed("mr-4", title="t")
    work.claim(bead="mr-4", as_="", rig="myrepo")
    _commit(_wt(rig, "mr-4"), "feat: the change")
    work.submit(bead="mr-4", rig="myrepo")
    assert fakebd.states["mr-4"]["review"] == "pending"
    assert fakebd.did("gate", "create", "--blocks", "mr-4")
    assert not _remote_has(rig, "wt/bead/mr-4")  # local gate → no push


def test_submit_ghpr_gate_pushes(rig, fakebd, monkeypatch):
    monkeypatch.setattr(config, "review_gate", lambda cfg, entry: "gh:pr")
    fakebd.seed("mr-5", title="t")
    work.claim(bead="mr-5", as_="", rig="myrepo")
    _commit(_wt(rig, "mr-5"), "feat: x")
    work.submit(bead="mr-5", rig="myrepo")
    assert _remote_has(rig, "wt/bead/mr-5")  # out-of-process gate → branch pushed
    assert fakebd.states["mr-5"]["review"] == "pending"


# ---- merge -----------------------------------------------------------------


def _take_to_approved(rig, fakebd, bead, msg="feat: the change"):
    """claim → commit → submit → reviewer approves; leaves the bead ready to merge."""
    work.claim(bead=bead, as_="", rig="myrepo")
    _commit(_wt(rig, bead), msg)
    work.submit(bead=bead, rig="myrepo")
    fakebd.approve(bead)


def test_merge_no_ff_lands_and_closes(rig, fakebd):
    fakebd.seed("mr-10", title="t")
    _take_to_approved(rig, fakebd, "mr-10")

    work.merge(bead="mr-10", rig="myrepo", rm=False, molecule=False)

    # a real merge commit landed on the integration branch (two parents, --no-ff)
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=rig.main).stdout.strip() == "main"
    assert _git("log", "-1", "--format=%s", cwd=rig.main).stdout.strip() == "merge mr-10"
    parents = _git("rev-list", "--parents", "-n", "1", "HEAD", cwd=rig.main).stdout.split()
    assert len(parents) == 3  # commit + two parents
    # merge commit carries the agent-mode merger identity, and the bead's change is integrated
    assert _git("log", "-1", "--format=%an", cwd=rig.main).stdout.strip() == "crew/default"
    assert (rig.main / "change.txt").exists()
    assert fakebd.beads["mr-10"]["status"] == "closed"
    assert fakebd.did("merge-slot", "acquire") and fakebd.did("merge-slot", "release")


def test_merge_refuses_open_gate(rig, fakebd):
    fakebd.seed("mr-11", title="t")
    work.claim(bead="mr-11", as_="", rig="myrepo")
    _commit(_wt(rig, "mr-11"), "feat: x")
    work.submit(bead="mr-11", rig="myrepo")  # gate opened, NOT approved
    before = _git("rev-parse", "HEAD", cwd=rig.main).stdout.strip()
    with pytest.raises(typer.Exit):
        work.merge(bead="mr-11", rig="myrepo", rm=False, molecule=False)
    assert _git("rev-parse", "HEAD", cwd=rig.main).stdout.strip() == before  # main untouched
    assert fakebd.beads["mr-11"]["status"] != "closed"


def test_merge_refuses_changes_requested(rig, fakebd):
    fakebd.seed("mr-12", title="t")
    _take_to_approved(rig, fakebd, "mr-12")
    fakebd.states["mr-12"]["review"] = "changes-requested"  # bounced after approval
    with pytest.raises(typer.Exit):
        work.merge(bead="mr-12", rig="myrepo", rm=False, molecule=False)
    assert fakebd.beads["mr-12"]["status"] != "closed"


def test_merge_rm_removes_worktree(rig, fakebd):
    fakebd.seed("mr-13", title="t")
    _take_to_approved(rig, fakebd, "mr-13")
    assert _wt(rig, "mr-13").exists()
    work.merge(bead="mr-13", rig="myrepo", rm=True, molecule=False)
    assert not _wt(rig, "mr-13").exists()


# ---- molecule-aware base (two-level integration) ---------------------------
#
# A bead id `mr-1.1` has epic `mr-1`; when `mol/mr-1` exists in the main clone the molecule was
# kicked off, so the bead's lifecycle measures and merges against `mol/mr-1` (not `main`). A bead
# with no `.` (mr-10 above) has no molecule and still targets `main` — see the merge tests above.


def _mol_branch(rig, epic, extra_subject=""):
    """Create the molecule integration branch `mol/<epic>` off main. With `extra_subject`, add one
    commit ahead of main so the molecule diverges and the resolved base is observable."""
    _git("branch", f"mol/{epic}", "main", cwd=rig.main)
    if extra_subject:
        _git("checkout", "-q", f"mol/{epic}", cwd=rig.main)
        _commit(rig.main, extra_subject, fname="mol.txt")
        _git("checkout", "-q", "main", cwd=rig.main)


def _wt_of(rig, bead):
    """Worktree dir for a (possibly dotted) bead — the leaf is sanitized (mr-1.1 -> mr-1-1)."""
    return rig.wts / "github" / "myorg" / "myrepo" / registry.sanitize(bead)


def test_merge_lands_bead_into_molecule_not_main(rig, fakebd):
    """A bead in a kicked-off molecule merges into mol/<epic> --no-ff; main stays untouched."""
    _mol_branch(rig, "mr-1")
    main_before = _git("rev-parse", "main", cwd=rig.main).stdout.strip()
    fakebd.seed("mr-1.1", title="t")
    work.claim(bead="mr-1.1", as_="", rig="myrepo")
    _commit(_wt_of(rig, "mr-1.1"), "feat: the change")
    work.submit(bead="mr-1.1", rig="myrepo")
    fakebd.approve("mr-1.1")

    work.merge(bead="mr-1.1", rig="myrepo", rm=False, molecule=False)

    # the bead landed on mol/mr-1, not main — the molecule assembles in isolation
    mol_tip_subject = _git("log", "-1", "--format=%s", "mol/mr-1", cwd=rig.main).stdout.strip()
    assert mol_tip_subject == "merge mr-1.1"
    parents = _git("rev-list", "--parents", "-n", "1", "mol/mr-1", cwd=rig.main).stdout.split()
    assert len(parents) == 3  # merge commit + two parents (--no-ff)
    assert _git("rev-parse", "main", cwd=rig.main).stdout.strip() == main_before  # main untouched
    assert fakebd.beads["mr-1.1"]["status"] == "closed"


def test_submit_measures_history_against_molecule(rig, fakebd):
    """submit's history guard is computed against mol/<epic>: a noisy commit living only on the
    molecule branch stays out of the bead's range, so submit passes. Measured against main the same
    range would drag in that non-conventional commit and be rejected — so a green submit proves
    the molecule-aware base."""
    _mol_branch(rig, "mr-1", extra_subject="wip molecule scratch")  # mol = main + a noisy commit
    fakebd.seed("mr-1.1", title="t")
    work.claim(bead="mr-1.1", as_="", rig="myrepo")
    wt = _wt_of(rig, "mr-1.1")
    # The bead forks off the molecule tip (start-point threading is a sibling bead's job; here we
    # only exercise which base work.py measures against).
    _git("reset", "--hard", "mol/mr-1", cwd=wt)
    _commit(wt, "feat: the change")

    work.submit(bead="mr-1.1", rig="myrepo")  # raises if measured against main (noisy range)

    assert fakebd.states["mr-1.1"]["review"] == "pending"


def test_show_measures_against_molecule(rig, fakebd, capsys):
    """show renders base..branch against the molecule tip, not main, when mol/<epic> exists."""
    _mol_branch(rig, "mr-1", extra_subject="wip molecule scratch")
    fakebd.seed("mr-1.1", title="t")
    work.claim(bead="mr-1.1", as_="", rig="myrepo")
    wt = _wt_of(rig, "mr-1.1")
    _git("reset", "--hard", "mol/mr-1", cwd=wt)
    _commit(wt, "feat: the change")

    capsys.readouterr()  # drain claim/setup chatter so only show's JSON remains
    work.show(bead="mr-1.1", view=["log"], json_out=True, rig="myrepo")

    payload = json.loads(capsys.readouterr().out.strip())
    mol_tip = _git("rev-parse", "mol/mr-1", cwd=rig.main).stdout.strip()
    assert payload["base"] == mol_tip[:7]  # forked off the molecule, so base == mol tip


# ---- merge --molecule (the wrap-up / land verb) ----------------------------
#
# When the molecule is whole, `ws work merge <epic> --molecule` collapses the assembled
# `mol/<epic>` (which holds the per-bead --no-ff merges) onto the rig integration branch as ONE
# --no-ff bubble, closes the epic, and deletes the branch — the two-level AGF integration shape.


def _land_two_bead_molecule(rig, fakebd, epic="mr-1"):
    """Build a complete molecule: kick off mol/<epic>, then claim→commit→submit→approve→merge two
    child beads INTO mol/<epic>. Leaves the epic open with both children closed, ready to land."""
    _mol_branch(rig, epic)
    fakebd.seed(epic, title="epic")
    for bid in (f"{epic}.1", f"{epic}.2"):
        fakebd.seed(bid, title="t", parent=epic)
        work.claim(bead=bid, as_="", rig="myrepo")
        _commit(_wt_of(rig, bid), f"feat: {bid}")
        work.submit(bead=bid, rig="myrepo")
        fakebd.approve(bid)
        work.merge(bead=bid, rig="myrepo", rm=False, molecule=False)


def test_merge_molecule_lands_as_one_bubble(rig, fakebd):
    _land_two_bead_molecule(rig, fakebd, "mr-1")
    main_before = _git("rev-parse", "main", cwd=rig.main).stdout.strip()

    work.merge(bead="mr-1", rig="myrepo", molecule=True)

    # ONE --no-ff bubble on main: subject "merge molecule <epic>", merge commit + two parents
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=rig.main).stdout.strip() == "main"
    assert _git("log", "-1", "--format=%s", cwd=rig.main).stdout.strip() == "merge molecule mr-1"
    parents = _git("rev-list", "--parents", "-n", "1", "HEAD", cwd=rig.main).stdout.split()
    assert len(parents) == 3
    assert _git("rev-parse", "main", cwd=rig.main).stdout.strip() != main_before  # main advanced
    # the per-bead merges live INSIDE the bubble (reachable from main now)
    subjects = _git("log", "--format=%s", "main", cwd=rig.main).stdout.split("\n")
    assert "merge mr-1.1" in subjects and "merge mr-1.2" in subjects
    # epic closed (reason recorded), molecule branch deleted, slot released
    assert fakebd.beads["mr-1"]["status"] == "closed"
    assert fakebd.did("close", "mr-1", "--reason", "molecule landed")
    assert not worktree._branch_exists(rig.main, "mol/mr-1")
    assert fakebd.did("merge-slot", "acquire") and fakebd.did("merge-slot", "release")


def test_merge_molecule_refuses_open_child(rig, fakebd):
    """An incomplete molecule (a child still open) is refused before any merge — never drops work:
    main untouched, epic still open, molecule branch intact, no slot acquired."""
    _mol_branch(rig, "mr-1")
    fakebd.seed("mr-1", title="epic")
    fakebd.seed("mr-1.1", title="t", parent="mr-1", status="closed")
    fakebd.seed("mr-1.2", title="t", parent="mr-1")  # still open
    main_before = _git("rev-parse", "main", cwd=rig.main).stdout.strip()

    with pytest.raises(typer.Exit):
        work.merge(bead="mr-1", rig="myrepo", molecule=True)

    assert _git("rev-parse", "main", cwd=rig.main).stdout.strip() == main_before
    assert fakebd.beads["mr-1"]["status"] != "closed"
    assert worktree._branch_exists(rig.main, "mol/mr-1")
    assert not fakebd.did("merge-slot", "acquire")


# ---- resume ----------------------------------------------------------------


def test_resume_reprovisions_after_worktree_removed(rig, fakebd):
    fakebd.seed("mr-6", title="t")
    work.claim(bead="mr-6", as_="", rig="myrepo")
    _commit(_wt(rig, "mr-6"), "feat: x")
    work.submit(bead="mr-6", rig="myrepo")
    # review came back rejected; the worktree directory was reclaimed
    fakebd.states["mr-6"]["review"] = "changes-requested"
    _git("worktree", "remove", "--force", str(_wt(rig, "mr-6")), cwd=rig.main)
    assert not _wt(rig, "mr-6").exists()

    work.resume(bead="mr-6", as_="", rig="myrepo")
    wt = _wt(rig, "mr-6")
    assert wt.exists()
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt).stdout.strip() == "wt/bead/mr-6"


def test_resume_refuses_wrong_state(rig, fakebd):
    fakebd.seed("mr-6", title="t")
    work.claim(bead="mr-6", as_="", rig="myrepo")
    with pytest.raises(typer.Exit):  # not changes-requested
        work.resume(bead="mr-6", as_="", rig="myrepo")


# ---- abandon ---------------------------------------------------------------


def test_abandon_rm_removes_worktree(rig, fakebd):
    fakebd.seed("mr-7", title="t")
    work.claim(bead="mr-7", as_="", rig="myrepo")
    assert _wt(rig, "mr-7").exists()
    work.abandon(bead="mr-7", rig="myrepo", rm=True)
    assert not _wt(rig, "mr-7").exists()
    assert fakebd.states["mr-7"]["review"] == "abandoned"
    assert fakebd.beads["mr-7"]["assignee"] == ""


# ---- worktree path/rm --bead (Fix 2) ---------------------------------------


def test_worktree_path_and_rm_accept_bead(rig, fakebd, capsys):
    from ws import cli

    fakebd.seed("mr-1", title="t")
    work.claim(bead="mr-1", as_="", rig="myrepo")
    wt = _wt(rig, "mr-1")

    cli.wt_path(ref="", bead="mr-1", rig="myrepo")  # resolve by --bead
    assert str(wt) in capsys.readouterr().out

    with pytest.raises(typer.Exit):  # neither ref nor --bead
        cli.wt_path(ref="", bead="", rig="myrepo")

    cli.wt_rm(ref="", bead="mr-1", rig="myrepo", force=True)  # remove by --bead
    assert not wt.exists()
