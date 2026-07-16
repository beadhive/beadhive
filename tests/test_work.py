"""`ws work` self-checks — the WS-WORK-IMPL checklist.

Real git in tmp_path (worktrees, identity stamping, push) + a faked `bd`. The test seam:
work.py shells out to `bd` ONLY through `ws.work.run`, so we patch that one symbol to fake
Beads while every git/worktree op runs for real. Non-`bd` calls (the validation command in
`check`) delegate to the real runner.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import typer

from beadhive import bd as bd_mod
from beadhive import config, otel, plan, registry, work, worktree
from beadhive.run import run as real_run

_CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
_CP = namedtuple("CP", "returncode stdout stderr")

CONFIG_YAML = """\
providers: [github]
work:
  validate_cmd: "true"
  review_gate: "human"
  identity: {mode: agent, name: "dev/default", email: "agents@test.dev"}
managed_repos:
  - {provider: github, org: myorg, repo: myrepo, prefix: mr, kind: personal}
"""

CONFIG_YAML_WITH_UNION = """\
providers: [github]
work:
  validate_cmd: "true"
  review_gate: "human"
  identity: {mode: agent, name: "dev/default", email: "agents@test.dev"}
  conflict:
    union_globs: ["notes.txt"]
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
            rows = list(self.beads.values())
            if "--status" in args:
                want = set(args[args.index("--status") + 1].split(","))
                rows = [b for b in rows if b.get("status") in want]
            if "--label" in args:
                lbl = args[args.index("--label") + 1]
                rows = [b for b in rows if lbl in (b.get("labels") or [])]
            return _CP(0, json.dumps(rows), "")
        if sub == "label" and len(args) >= 4 and args[1] == "add":
            bead = self.beads.setdefault(args[2], {"id": args[2]})
            labels = list(bead.get("labels") or [])
            if args[3] not in labels:  # additive + idempotent, mirroring `bd label add`
                labels.append(args[3])
            bead["labels"] = labels
            return _CP(0, "", "")
        if sub == "label" and len(args) >= 4 and args[1] == "remove":
            bead = self.beads.setdefault(args[2], {"id": args[2]})
            bead["labels"] = [x for x in (bead.get("labels") or []) if x != args[3]]
            return _CP(0, "", "")
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
            reason = args[args.index("--reason") + 1] if "--reason" in args else ""
            gtype = args[args.index("--type") + 1] if "--type" in args else "human"
            # Mirror real `bd gate` shape: description carries the reason (so `_review_gate` can
            # tell a review gate from a kickoff one) and the gate records its await_type.
            self.gates.append(
                {
                    "id": f"g{len(self.gates)}",
                    "status": "open",
                    "description": f"blocks {bead}\n\nReason: {reason}",
                    "await_type": gtype,
                }
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
def hive(tmp_path, monkeypatch):
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
    # Isolate HOME: ws's git calls scrub GIT_CONFIG_GLOBAL and fall back to ~/.gitconfig, so an
    # empty HOME pins merge/rebase to default git behaviour (no developer rerere/diff overrides
    # leaking in) — the conflict-recovery tests must be deterministic on any machine.
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return SimpleNamespace(main=main, wts=tmp_path / "wts", remote=remote, cfg_path=cfg_path)


@pytest.fixture
def fakebd(monkeypatch):
    fb = FakeBd()
    monkeypatch.setattr(work, "run", fb)
    # bd.json uses ws.bd.run — patch it so bd.json calls (e.g. _show, _review_gate, _flow_events)
    # are intercepted by the same fake instead of hitting the real bd binary.
    monkeypatch.setattr(bd_mod, "_run", fb)
    # The dispatch convention gate (assign/claim/start) reuses plan.verify_epic; neutralize it here
    # so these tests exercise dispatch mechanics, not molecule conventions. The gate's own tests
    # (test_dispatch_convention_gate_*) drive verify_epic explicitly.
    monkeypatch.setattr(plan, "verify_epic", lambda *a, **k: [])
    return fb


def _wt(hive, bead):
    return hive.wts / "github" / "myorg" / "myrepo" / bead


def _remote_has(hive, branch):
    cmd = ["git", "branch", "--list", branch]
    out = real_run(cmd, cwd=str(hive.remote), check=False, capture=True, env=_CLEAN_ENV).stdout
    return bool((out or "").strip())


# ---- the history guard (ponytail self-check) -------------------------------


def test_history_ok_rules():
    assert work._history_ok(2, ["feat: a", "fix(x): b"], 10)[0]
    assert not work._history_ok(0, [], 10)[0]  # nothing to submit
    assert not work._history_ok(11, ["feat: x"] * 11, 10)[0]  # too many commits
    assert not work._history_ok(1, ["wip junk"], 10)[0]  # non-conventional
    assert not work._history_ok(-1, [], 10)[0]  # base missing


# ---- claim -----------------------------------------------------------------


def test_claim_provisions_worktree_with_identity(hive, fakebd):
    fakebd.seed("mr-1", title="t")
    work.claim(bead="mr-1", as_="", hive="myrepo")
    wt = _wt(hive, "mr-1")
    assert wt.exists()
    assert _cfg_get(wt, "user.name") == "dev/default"
    assert _cfg_get(wt, "user.email") == "agents@test.dev"
    # agent identity with no key → signing pinned off (don't inherit the human's global key)
    assert _cfg_get(wt, "commit.gpgsign") == "false"
    assert fakebd.beads["mr-1"]["status"] == "in_progress"
    assert fakebd.did("update", "mr-1", "--claim")
    assert ("dev/default", ["update", "mr-1", "--claim"]) in fakebd.calls


def _mol_listed(hive, epic):
    return _git("branch", "--list", f"wt/bead/epic/{epic}", cwd=hive.main).stdout.strip()


def test_claim_auto_opens_molecule_when_epic_kicked_off(hive, fakebd):
    """Kickoff relocated to the integration plane: claiming a child of a kickoff=approved epic
    lazily opens wt/bead/epic/<epic>, so the child worktree forks off the molecule (not main)."""
    fakebd.seed("mr-1.1", title="t")
    fakebd.states["mr-1"] = {"kickoff": "approved"}
    work.claim(bead="mr-1.1", as_="", hive="myrepo")
    assert _mol_listed(hive, "mr-1") != "", "claim should open the mr-1 container"


def test_assign_auto_opens_molecule_when_epic_kicked_off(hive, fakebd):
    """assign (orchestrator dispatch) also opens the container for a kicked-off epic's child."""
    fakebd.seed("mr-1.1", title="t")
    fakebd.states["mr-1"] = {"kickoff": "approved"}
    work.assign(bead="mr-1.1", to="dev/dev", as_="disp/lead", hive="myrepo")
    assert _mol_listed(hive, "mr-1") != "", "assign should open the mr-1 container"


def test_claim_no_molecule_when_epic_not_kicked_off(hive, fakebd):
    """Backward-compatible: a dotted bead whose epic was never kicked off opens no molecule branch
    — it targets main directly, exactly as before the kickoff relocation."""
    fakebd.seed("mr-2.1", title="t")  # no kickoff state on epic mr-2
    work.claim(bead="mr-2.1", as_="", hive="myrepo")
    assert _mol_listed(hive, "mr-2") == "", "no molecule branch without kickoff=approved"


# ---- container refresh ----------------------------------
#
# The container opens ONCE, on the first child's dispatch. When main advances mid-molecule,
# later children must not provision from the stale open-time base — claim/assign refresh the
# container from its integration base first (ff or merge), warning-but-provisioning on conflict.


def _kicked_off_pair(fakebd, epic="mr-1"):
    fakebd.seed(f"{epic}.1", title="t")
    fakebd.seed(f"{epic}.2", title="t")
    fakebd.states[epic] = {"kickoff": "approved"}


def test_claim_refreshes_stale_container_from_main(hive, fakebd):
    """Regression: fixes landing on main AFTER the container opened must be visible to the next
    provisioned child — the container fast-forwards to main and the child forks from it."""
    _kicked_off_pair(fakebd)
    work.claim(bead="mr-1.1", as_="", hive="myrepo")  # opens the container at main's current tip
    _commit(hive.main, "fix: landed on main mid-molecule", fname="mainfix.txt")

    work.claim(bead="mr-1.2", as_="", hive="myrepo")

    wt2 = _wt_of(hive, "mr-1.2")
    assert (wt2 / "mainfix.txt").exists(), "child must contain main's tip, not the stale base"
    main_tip = _git("rev-parse", "main", cwd=hive.main).stdout.strip()
    mol_tip = _git("rev-parse", "wt/bead/epic/mr-1", cwd=hive.main).stdout.strip()
    assert mol_tip == main_tip  # strictly-behind container fast-forwarded (no merge commit)


def test_claim_conflicting_container_refresh_warns_but_provisions(hive, fakebd, capsys):
    """A conflicting refresh NEVER blocks dispatch: loud warning, merge aborted (seat left
    clean), and the child still provisions from the stale base."""
    _kicked_off_pair(fakebd)
    work.claim(bead="mr-1.1", as_="", hive="myrepo")
    seat = _wt(hive, "mr-1")  # coordinator seat holds the container branch
    _commit(seat, "feat: container-side edit", fname="clash.txt")
    _commit(hive.main, "fix: main-side edit", fname="clash.txt")  # add/add conflict vs the seat
    capsys.readouterr()

    work.claim(bead="mr-1.2", as_="", hive="myrepo")  # must not raise

    err = capsys.readouterr().err
    assert "WARNING" in err and "behind" in err and "CONFLICTS" in err
    assert _wt_of(hive, "mr-1.2").exists()
    assert worktree.is_clean(seat), "conflicted refresh merge must be aborted"


def test_submit_tolerates_container_refresh_merge(hive, fakebd):
    """The refresh lands on the CONTAINER (merge commit and all) — submit's history guard judges
    base..child only, so a child provisioned after a merge-refresh still submits green."""
    _kicked_off_pair(fakebd)
    work.claim(bead="mr-1.1", as_="", hive="myrepo")
    seat = _wt(hive, "mr-1")
    _commit(seat, "wip container scratch", fname="mol.txt")  # container diverges → non-ff refresh
    _commit(hive.main, "fix: landed on main mid-molecule", fname="mainfix.txt")

    work.claim(bead="mr-1.2", as_="", hive="myrepo")  # refresh = a merge commit on the container
    wt2 = _wt_of(hive, "mr-1.2")
    assert (wt2 / "mainfix.txt").exists()
    _commit(wt2, "feat: the change")

    work.submit(bead="mr-1.2", hive="myrepo")  # rejects if the refresh polluted base..child

    assert fakebd.states["mr-1.2"]["review"] == "pending"


def test_claim_as_flag_overrides_identity(hive, fakebd):
    fakebd.seed("mr-1", title="t")
    work.claim(bead="mr-1", as_="dev/alice", hive="myrepo")
    assert _cfg_get(_wt(hive, "mr-1"), "user.name") == "dev/alice"
    assert ("dev/alice", ["update", "mr-1", "--claim"]) in fakebd.calls


def test_claim_twice_reattaches(hive, fakebd):
    fakebd.seed("mr-1", title="t")
    work.claim(bead="mr-1", as_="", hive="myrepo")
    work.claim(bead="mr-1", as_="", hive="myrepo")  # no exception
    assert _wt(hive, "mr-1").exists()


def test_claim_refuses_other_actor(hive, fakebd):
    fakebd.seed("mr-1", title="t", assignee="dev/bob")
    with pytest.raises(typer.Exit):
        work.claim(bead="mr-1", as_="dev/alice", hive="myrepo")
    assert not _wt(hive, "mr-1").exists()  # refused before provisioning


def test_claim_signing_config_when_key_set(hive, fakebd, monkeypatch):
    monkeypatch.setattr(
        config,
        "work_identity",
        lambda cfg, entry, actor="": {
            "mode": "agent",
            "name": "dev/signer",
            "email": "s@test.dev",
            "signing_key": "/keys/x.pub",
            "sign": True,
        },
    )
    fakebd.seed("mr-1", title="t")
    work.claim(bead="mr-1", as_="", hive="myrepo")
    wt = _wt(hive, "mr-1")
    assert _cfg_get(wt, "gpg.format") == "ssh"
    assert _cfg_get(wt, "commit.gpgsign") == "true"
    assert _cfg_get(wt, "user.signingkey") == "/keys/x.pub"


def test_claim_supervised_leaves_identity(hive, fakebd, monkeypatch):
    monkeypatch.setattr(
        config,
        "work_identity",
        lambda cfg, entry, actor="": {
            "mode": "supervised",
            "name": None,
            "email": None,
            "signing_key": None,
            "sign": False,
        },
    )
    fakebd.seed("mr-1", title="t")
    work.claim(bead="mr-1", as_="", hive="myrepo")
    # no stamp → worktree inherits the human's identity; we never enable per-worktree config
    assert _cfg_get(_wt(hive, "mr-1"), "user.name") == "human"
    assert _cfg_get(_wt(hive, "mr-1"), "extensions.worktreeConfig") == ""


def test_concurrent_claims_keep_separate_identities(hive, fakebd):
    """Two beads claimed as different actors must not clobber each other's git identity."""
    fakebd.seed("mr-8", title="a")
    fakebd.seed("mr-9", title="b")
    work.claim(bead="mr-8", as_="dev/alice", hive="myrepo")
    work.claim(bead="mr-9", as_="dev/bob", hive="myrepo")
    assert _cfg_get(_wt(hive, "mr-8"), "user.name") == "dev/alice"
    assert _cfg_get(_wt(hive, "mr-9"), "user.name") == "dev/bob"


# per-crew SSH signing: each crew authors + signs as its own ledger identity, distinct from
# the human and from sibling crews. The base agent identity supplies defaults; the crews map
# layers per-crew email + signing key over it (no real keys needed — assert the git config).
CREWS_CONFIG_YAML = """\
providers: [github]
work:
  validate_cmd: "true"
  review_gate: "human"
  identity:
    mode: agent
    name: "dev/default"
    email: "agents@test.dev"
    crews:
      dev/alice: {email: "alice@agents.dev", signing_key: "/keys/alice.pub", sign: true}
      dev/bob: {email: "bob@agents.dev", signing_key: "/keys/bob.pub", sign: true}
managed_repos:
  - {provider: github, org: myorg, repo: myrepo, prefix: mr, kind: personal}
"""


def test_claim_stamps_per_crew_signing_identity(hive, fakebd):
    hive.cfg_path.write_text(CREWS_CONFIG_YAML)
    fakebd.seed("mr-1", title="a")
    fakebd.seed("mr-2", title="b")
    work.claim(bead="mr-1", as_="dev/alice", hive="myrepo")
    work.claim(bead="mr-2", as_="dev/bob", hive="myrepo")
    a, b = _wt(hive, "mr-1"), _wt(hive, "mr-2")

    assert _cfg_get(a, "user.name") == "dev/alice"
    assert _cfg_get(a, "user.email") == "alice@agents.dev"
    assert _cfg_get(a, "user.signingkey") == "/keys/alice.pub"
    assert _cfg_get(a, "gpg.format") == "ssh"
    assert _cfg_get(a, "commit.gpgsign") == "true"

    assert _cfg_get(b, "user.name") == "dev/bob"
    assert _cfg_get(b, "user.email") == "bob@agents.dev"
    assert _cfg_get(b, "user.signingkey") == "/keys/bob.pub"

    # distinct from each other and from the human (human@example.com, no signing key)
    assert _cfg_get(a, "user.signingkey") != _cfg_get(b, "user.signingkey")
    assert _cfg_get(a, "user.email") != _cfg_get(b, "user.email")
    assert _cfg_get(a, "user.email") != "human@example.com"


# ---- cwd guard (A1: warn when agent edits from main clone, not worktree) ----
#
# Sub-agents share the session cwd.  Absolute paths under the hive root resolve to the main
# clone, not the worktree — so an agent that skips `cd <worktree>` silently edits the wrong
# tree.  `claim` (and `check`/`submit`) detect this and emit a prominent, copy-pasteable
# `cd` reminder so the misdirection is impossible to miss.


def test_claim_warns_when_cwd_is_main_clone(hive, fakebd, capsys, monkeypatch):
    """claim emits a WARNING with the exact cd path when cwd is the main clone."""
    fakebd.seed("mr-1", title="t")
    monkeypatch.chdir(hive.main)
    work.claim(bead="mr-1", as_="", hive="myrepo")
    err = capsys.readouterr().err
    wt = _wt(hive, "mr-1")
    assert "WARNING" in err
    assert str(wt) in err
    assert "cd" in err


def test_claim_no_warning_when_cwd_is_worktree(hive, fakebd, capsys, monkeypatch):
    """claim emits no WARNING when cwd is already the bead's worktree."""
    fakebd.seed("mr-1", title="t")
    # First claim provisions the worktree; re-claim from inside it to test the no-warning path.
    work.claim(bead="mr-1", as_="", hive="myrepo")
    wt = _wt(hive, "mr-1")
    monkeypatch.chdir(wt)
    capsys.readouterr()  # drain previous output
    work.claim(bead="mr-1", as_="", hive="myrepo")
    err = capsys.readouterr().err
    assert "WARNING" not in err


# ---- assign → claim handshake ----------------------------------------------


def test_assign_then_claim(hive, fakebd):
    fakebd.seed("mr-2", title="t")
    work.assign(bead="mr-2", to="dev/carol", as_="disp/lead", hive="myrepo")
    assert fakebd.beads["mr-2"]["status"] == "open"  # assignment is not the ack
    assert fakebd.beads["mr-2"]["assignee"] == "dev/carol"
    assert _cfg_get(_wt(hive, "mr-2"), "user.name") == "dev/carol"

    work.claim(bead="mr-2", as_="dev/carol", hive="myrepo")
    assert fakebd.beads["mr-2"]["status"] == "in_progress"  # claim is the ack


# ---- seat enforcement: epic->coordinator, issue->developer ------------------


def test_assign_epic_only_to_coordinator(hive, fakebd):
    """An epic (container) may only be assigned to a dispatcher (disp/<name>); a developer
    target is refused before any provisioning. A dispatcher target is accepted."""
    fakebd.seed("mr-epic", title="e", issue_type="epic")
    with pytest.raises(typer.Exit):
        work.assign(bead="mr-epic", to="dev/dev", as_="disp/lead", hive="myrepo")
    assert not _wt(hive, "mr-epic").exists()  # rejected before provisioning
    work.assign(bead="mr-epic", to="disp/lead", as_="disp/lead", hive="myrepo")
    assert fakebd.beads["mr-epic"]["assignee"] == "disp/lead"


def test_assign_issue_only_to_developer(hive, fakebd):
    """A non-epic (leaf) bead may only be assigned to a developer (dev/<name>), not a
    dispatcher."""
    fakebd.seed("mr-7", title="t")  # no issue_type -> leaf
    with pytest.raises(typer.Exit):
        work.assign(bead="mr-7", to="disp/lead", as_="disp/lead", hive="myrepo")
    assert not _wt(hive, "mr-7").exists()


# ---- assign orchestrator-only hard gate (bead .38) --------------------------


def test_assign_denied_from_developer_seat(hive, fakebd, capsys):
    """assign is orchestrator-only: a developer (dev/) acting seat is hard-denied before any
    bd write or worktree provisioning — a leaf worker cannot dispatch work."""
    fakebd.seed("mr-7", title="t")
    with pytest.raises(typer.Exit):
        work.assign(bead="mr-7", to="dev/carol", as_="dev/alice", hive="myrepo")
    assert "orchestrator-only" in capsys.readouterr().err
    assert not _wt(hive, "mr-7").exists()  # denied before provisioning
    assert not fakebd.did("assign", "mr-7", "dev/carol")


def test_assign_denied_from_reviewer_seat(hive, fakebd):
    """A reviewer (rev/) — a recognized non-orchestrator seat — is also denied from assigning."""
    fakebd.seed("mr-7", title="t")
    with pytest.raises(typer.Exit):
        work.assign(bead="mr-7", to="dev/carol", as_="rev/rob", hive="myrepo")
    assert not fakebd.did("assign", "mr-7", "dev/carol")


def test_assign_allowed_from_director_seat(hive, fakebd):
    """A director (dir/) is an orchestrator seat and may assign work."""
    fakebd.seed("mr-7", title="t")
    work.assign(bead="mr-7", to="dev/carol", as_="dir/dana", hive="myrepo")
    assert fakebd.beads["mr-7"]["assignee"] == "dev/carol"


def test_assign_allowed_from_legacy_coord_seat(hive, fakebd):
    """The legacy coord/ prefix still resolves to a dispatcher, so it may assign (back-compat)."""
    fakebd.seed("mr-7", title="t")
    work.assign(bead="mr-7", to="dev/carol", as_="coord/lead", hive="myrepo")
    assert fakebd.beads["mr-7"]["assignee"] == "dev/carol"


def test_assign_exempts_bare_human(hive, fakebd):
    """A bare human/supervised operator (no recognized seat prefix) is exempt — existing
    supervised flows are unaffected by the orchestrator gate."""
    fakebd.seed("mr-7", title="t")
    work.assign(bead="mr-7", to="dev/carol", as_="brian", hive="myrepo")
    assert fakebd.beads["mr-7"]["assignee"] == "dev/carol"


def test_claim_epic_only_by_coordinator(hive, fakebd):
    """Claiming an epic requires acting as a dispatcher; a developer identity is refused, a
    dispatcher identity is accepted."""
    fakebd.seed("mr-epic", title="e", issue_type="epic")
    with pytest.raises(typer.Exit):
        work.claim(bead="mr-epic", as_="dev/dev", hive="myrepo")
    assert not _wt(hive, "mr-epic").exists()
    work.claim(bead="mr-epic", as_="disp/lead", hive="myrepo")
    assert fakebd.beads["mr-epic"]["status"] == "in_progress"


def test_legacy_seat_prefixes_still_resolve():
    """Back-compat shim (bead .32): legacy coord//crew/ prefixes still resolve to the
    dispatcher/developer seats during the migration window (removed later per limn/kkke)."""
    assert work._seat_of("coord/lead") == "dispatcher"
    assert work._seat_of("crew/dev") == "developer"
    # canonical prefixes still win, and a bare human identity stays exempt ('').
    assert work._seat_of("disp/lead") == "dispatcher"
    assert work._seat_of("dev/dev") == "developer"
    assert work._seat_of("brian") == ""


def test_legacy_prefix_seat_enforcement_still_applies(hive, fakebd):
    """A legacy coord/ identity satisfies the epic (dispatcher) seat guard; a legacy crew/
    identity satisfies the leaf (developer) guard — so in-flight sessions keep working."""
    fakebd.seed("mr-epic", title="e", issue_type="epic")
    work.claim(bead="mr-epic", as_="coord/lead", hive="myrepo")  # coord/ -> dispatcher, epic ok
    assert fakebd.beads["mr-epic"]["status"] == "in_progress"
    fakebd.seed("mr-7", title="t")  # leaf
    work.claim(bead="mr-7", as_="crew/dev", hive="myrepo")  # crew/ -> developer, leaf ok
    assert fakebd.beads["mr-7"]["status"] == "in_progress"


def test_assign_emits_genai_dispatch_span(hive, fakebd, monkeypatch):
    """cit.5 (EXPERIMENTAL): the assign seam is the coordinator->developer dispatch — with otel on
    it emits a GenAI `invoke_agent` span carrying the brief as a droppable EVENT, not an attr."""
    fakebd.seed("mr-9", title="t", description="secret brief body — may contain PII")
    # Force otel on with a mocked, inspectable tracer/span (the SDK isn't installed in test env).
    span = MagicMock(name="span")
    cm = MagicMock(name="cm")
    cm.__enter__.return_value = span
    cm.__exit__.return_value = False
    tracer = MagicMock(name="tracer")
    tracer.start_as_current_span.return_value = cm
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(otel, "get_tracer", lambda *a, **k: tracer)
    monkeypatch.setenv("WS_GENAI_MODEL", "opus")

    work.assign(bead="mr-9", to="dev/carol", as_="disp/lead", hive="myrepo")

    # The dispatch span is the `invoke_agent {agent}`-named one (the verb-level work.assign span
    # is also opened by @trace_verb; pick the gen_ai one out of the calls).
    dispatch = [
        c
        for c in tracer.start_as_current_span.call_args_list
        if c.args and str(c.args[0]).startswith("invoke_agent")
    ]
    assert len(dispatch) == 1
    assert dispatch[0].args[0] == "invoke_agent dev/carol"
    attrs = dispatch[0].kwargs["attributes"]
    assert attrs["gen_ai.operation.name"] == "invoke_agent"
    assert attrs["gen_ai.request.model"] == "opus"
    assert attrs["gen_ai.agent.name"] == "dev/carol"
    assert attrs["bh.bead"] == "mr-9"
    # brief is an EVENT, never an attribute
    assert "secret brief body — may contain PII" not in attrs.values()
    span.add_event.assert_called_once()
    ev_name, ev_attrs = span.add_event.call_args.args
    assert ev_name == "gen_ai.user.message"
    assert ev_attrs["bh.genai.content_kind"] == "brief"
    assert ev_attrs["content"] == "secret brief body — may contain PII"


# ---- submit ----------------------------------------------------------------


def test_submit_rejects_noisy_history(hive, fakebd):
    fakebd.seed("mr-3", title="t")
    work.claim(bead="mr-3", as_="", hive="myrepo")
    _commit(_wt(hive, "mr-3"), "wip junk")  # non-conventional subject
    with pytest.raises(typer.Exit):
        work.submit(bead="mr-3", hive="myrepo")
    assert "review" not in fakebd.states.get("mr-3", {})  # no state change
    assert not fakebd.did("set-state", "mr-3")


def test_submit_clean_local_gate_no_push(hive, fakebd):
    fakebd.seed("mr-4", title="t")
    work.claim(bead="mr-4", as_="", hive="myrepo")
    _commit(_wt(hive, "mr-4"), "feat: the change")
    work.submit(bead="mr-4", hive="myrepo")
    assert fakebd.states["mr-4"]["review"] == "pending"
    assert fakebd.did("gate", "create", "--blocks", "mr-4")
    assert not _remote_has(hive, "wt/bead/issue/mr-4")  # local gate → no push


def test_submit_ghpr_gate_pushes(hive, fakebd, monkeypatch):
    monkeypatch.setattr(config, "review_gate", lambda cfg, entry: "gh:pr")
    fakebd.seed("mr-5", title="t")
    work.claim(bead="mr-5", as_="", hive="myrepo")
    _commit(_wt(hive, "mr-5"), "feat: x")
    work.submit(bead="mr-5", hive="myrepo")
    assert _remote_has(hive, "wt/bead/issue/mr-5")  # out-of-process gate → branch pushed
    assert fakebd.states["mr-5"]["review"] == "pending"


def test_submit_refuses_when_claim_abandoned(hive, fakebd):
    """A submit from an agent whose claim was released (assignee cleared) refuses — no review
    gate opened, no review:pending set — so `abandon` is authoritative against a still-running
    agent that can't be signalled to stop."""
    fakebd.seed("mr-80", title="t")
    work.claim(bead="mr-80", as_="", hive="myrepo")
    _commit(_wt(hive, "mr-80"), "feat: the change")
    fakebd.beads["mr-80"]["assignee"] = ""  # abandon released the claim mid-flight
    with pytest.raises(typer.Exit):
        work.submit(bead="mr-80", hive="myrepo")
    assert "review" not in fakebd.states.get("mr-80", {})
    assert not fakebd.did("gate", "create", "--blocks", "mr-80")
    assert not fakebd.did("set-state", "mr-80")


def test_submit_refuses_when_reassigned_to_other(hive, fakebd):
    """Submit refuses when the bead was reassigned to a different actor — the stale agent
    can't open a review gate on a bead it no longer holds."""
    fakebd.seed("mr-81", title="t")
    work.claim(bead="mr-81", as_="", hive="myrepo")
    _commit(_wt(hive, "mr-81"), "feat: x")
    fakebd.beads["mr-81"]["assignee"] = "dev/someone-else"
    with pytest.raises(typer.Exit):
        work.submit(bead="mr-81", hive="myrepo")
    assert not fakebd.did("gate", "create", "--blocks", "mr-81")
    assert "review" not in fakebd.states.get("mr-81", {})


# ---- approve (first-class review-gate resolve; replaces `ws bd gate resolve`) ----
#
# A reviewer/coordinator clears a submitted bead's HUMAN review gate through the ws convention
# layer — attributed to the actor, with the `ws bd` passthrough OFF (no WS_BD_PASS_ENABLED). The
# guard paths: refuse when there's no open review gate (or only a non-review/kickoff gate), and
# refuse an out-of-process (gh:*) gate that isn't a human's to approve.


def test_approve_resolves_review_gate_and_unblocks_merge(hive, fakebd):
    """claim → commit → submit opens a human review gate; `ws work approve` resolves it (no
    passthrough override), and the bead then merges — proving the gate really cleared."""
    fakebd.seed("mr-70", title="t")
    work.claim(bead="mr-70", as_="", hive="myrepo")
    _commit(_wt(hive, "mr-70"), "feat: the change")
    work.submit(bead="mr-70", hive="myrepo")
    assert any(g["status"] == "open" for g in fakebd.gates)  # gate is open pre-approve

    work.approve(bead="mr-70", as_="dev/reviewer", hive="myrepo")

    assert all(g["status"] == "closed" for g in fakebd.gates)  # review gate cleared
    # the resolve wrapped `bd gate resolve`, attributed to the approving actor
    assert any(
        actor == "dev/reviewer" and a[:2] == ["gate", "resolve"] for actor, a in fakebd.calls
    )
    # and the merger can now land it (gate no longer blocks)
    work.merge(bead="mr-70", hive="myrepo", rm=False, molecule=False)
    assert fakebd.beads["mr-70"]["status"] == "closed"


def test_approve_attributes_config_identity_when_no_as(hive, fakebd):
    """Actor precedence mirrors claim: with no `--as`, approve attributes the config identity."""
    fakebd.seed("mr-71", title="t")
    work.claim(bead="mr-71", as_="", hive="myrepo")
    _commit(_wt(hive, "mr-71"), "feat: x")
    work.submit(bead="mr-71", hive="myrepo")

    work.approve(bead="mr-71", as_="", hive="myrepo")

    assert any(actor == "dev/default" and a[:2] == ["gate", "resolve"] for actor, a in fakebd.calls)


def test_approve_refuses_when_no_review_gate(hive, fakebd):
    """Guard: a bead with no open review gate (never submitted) can't be approved — the verb
    refuses instead of resolving something that isn't there."""
    fakebd.seed("mr-72", title="t")
    work.claim(bead="mr-72", as_="", hive="myrepo")  # claimed but not submitted → no gate
    with pytest.raises(typer.Exit):
        work.approve(bead="mr-72", as_="dev/reviewer", hive="myrepo")


def test_approve_refuses_non_review_gate(hive, fakebd):
    """Guard: a non-review gate (e.g. a kickoff gate) is NOT clearable via approve — it only
    resolves the review gate, so a kickoff-only block is left standing."""
    fakebd.seed("mr-73", title="t")
    fakebd.gates.append(
        {
            "id": "k0",
            "status": "open",
            "description": "blocks mr-73\n\nReason: kickoff mr-73",
            "await_type": "human",
        }
    )
    with pytest.raises(typer.Exit):
        work.approve(bead="mr-73", as_="dev/reviewer", hive="myrepo")
    assert fakebd.gates[0]["status"] == "open"  # kickoff gate untouched


def test_approve_refuses_out_of_process_gate(hive, fakebd, monkeypatch):
    """Guard: a gh:* review gate resolves out-of-process (CI / PR merge), not by a human via
    approve — the verb refuses and leaves the gate open."""
    monkeypatch.setattr(config, "review_gate", lambda cfg, entry: "gh:pr")
    fakebd.seed("mr-74", title="t")
    work.claim(bead="mr-74", as_="", hive="myrepo")
    _commit(_wt(hive, "mr-74"), "feat: x")
    work.submit(bead="mr-74", hive="myrepo")
    with pytest.raises(typer.Exit):
        work.approve(bead="mr-74", as_="dev/reviewer", hive="myrepo")
    assert any(g["status"] == "open" for g in fakebd.gates)  # gh:pr gate left for CI/PR


# ---- reviewer cross-seat policy: advise (default) | hard (bead .39) ----------


def _submitted(hive, fakebd, bead, author):
    """Claim (as author) → commit → submit, leaving an open human review gate authored by
    `author`. Returns nothing; the bead's assignee is `author`."""
    fakebd.seed(bead, title="t")
    work.claim(bead=bead, as_=author, hive="myrepo")
    _commit(_wt(hive, bead), "feat: the change")
    work.submit(bead=bead, as_=author, hive="myrepo")


def test_approve_advises_on_self_review_by_default(hive, fakebd, capsys):
    """Default reviewer cross-seat policy is `advise`: approving your OWN bead warns but still
    clears the gate (advisory, not a blanket block)."""
    _submitted(hive, fakebd, "mr-90", author="dev/alice")
    work.approve(bead="mr-90", as_="dev/alice", hive="myrepo")  # same person approves
    assert "self-review" in capsys.readouterr().err  # advisory warning emitted
    assert all(g["status"] == "closed" for g in fakebd.gates)  # …but the gate still cleared


def test_approve_advises_cross_seat_same_person(hive, fakebd, capsys):
    """Self-review is judged by PERSON, not seat: dev/alice authoring and rev/alice approving is
    still a self-review (the same person in an author + reviewer hat) — warned under `advise`."""
    _submitted(hive, fakebd, "mr-91", author="dev/alice")
    work.approve(bead="mr-91", as_="rev/alice", hive="myrepo")
    assert "self-review" in capsys.readouterr().err
    assert all(g["status"] == "closed" for g in fakebd.gates)


def test_approve_blocks_self_review_when_hard(hive, fakebd, monkeypatch, capsys):
    """`hard` reviewer cross-seat policy BLOCKS a self-approval: the gate is left open and the
    author must get a different seat/person to approve."""
    monkeypatch.setattr(config, "dispatch_reviewer_cross_seat", lambda cfg, entry: "hard")
    _submitted(hive, fakebd, "mr-92", author="dev/alice")
    with pytest.raises(typer.Exit):
        work.approve(bead="mr-92", as_="rev/alice", hive="myrepo")
    assert "self-review blocked" in capsys.readouterr().err
    assert any(g["status"] == "open" for g in fakebd.gates)  # gate untouched


def test_approve_allows_different_person_under_hard(hive, fakebd, monkeypatch):
    """`hard` only blocks self-review: a genuinely different reviewer still clears the gate."""
    monkeypatch.setattr(config, "dispatch_reviewer_cross_seat", lambda cfg, entry: "hard")
    _submitted(hive, fakebd, "mr-93", author="dev/alice")
    work.approve(bead="mr-93", as_="rev/bob", hive="myrepo")  # different person
    assert all(g["status"] == "closed" for g in fakebd.gates)


# ---- warden + security:* gate parallel to review (Assurance, bead .33) -------


def test_warden_clears_security_gate_parallel_to_review(hive, fakebd):
    """A security:* gate blocks the merge IN PARALLEL with review. A non-warden approve clears the
    review gate but leaves the warden's security gate open (merge still blocked); a warden approve
    clears the security gate; only when BOTH clear does the merge land."""
    fakebd.seed("mr-80", title="t")
    work.claim(bead="mr-80", as_="", hive="myrepo")
    _commit(_wt(hive, "mr-80"), "feat: x")
    work.submit(bead="mr-80", hive="myrepo")  # opens the human review gate
    # the warden opens a security gate blocking the SAME bead (secret-scan / SBOM / policy)
    fakebd.gates.append(
        {
            "id": "sec0",
            "status": "open",
            "description": "blocks mr-80\n\nReason: security:secret-scan",
            "await_type": "human",
        }
    )

    # a non-warden clears the REVIEW gate — routed to review, NOT the warden's security gate
    work.approve(bead="mr-80", as_="dev/reviewer", hive="myrepo")
    sec = next(g for g in fakebd.gates if g["id"] == "sec0")
    assert sec["status"] == "open"  # security gate untouched by the non-warden
    with pytest.raises(typer.Exit):  # merge still blocked while the security gate is open
        work.merge(bead="mr-80", hive="myrepo", rm=False, molecule=False)

    # the warden clears the security gate via the same approve verb
    work.approve(bead="mr-80", as_="warden/sec", hive="myrepo")
    assert sec["status"] == "closed"
    work.merge(bead="mr-80", hive="myrepo", rm=False, molecule=False)  # both clear → lands
    assert fakebd.beads["mr-80"]["status"] == "closed"


def test_non_warden_cannot_resolve_security_gate(hive, fakebd, capsys):
    """Assurance RBAC: a non-warden targeting an open security gate is refused (guard.py enforces
    warden-only resolution) and the gate stays open."""
    fakebd.seed("mr-81", title="t")
    fakebd.gates.append(
        {
            "id": "sec1",
            "status": "open",
            "description": "blocks mr-81\n\nReason: security:sbom",
            "await_type": "human",
        }
    )
    with pytest.raises(typer.Exit):
        work.approve(bead="mr-81", as_="dev/dev", hive="myrepo")
    assert fakebd.gates[0]["status"] == "open"
    assert "warden" in capsys.readouterr().err


# ---- merge -----------------------------------------------------------------


def _take_to_approved(hive, fakebd, bead, msg="feat: the change"):
    """claim → commit → submit → reviewer approves; leaves the bead ready to merge."""
    work.claim(bead=bead, as_="", hive="myrepo")
    _commit(_wt(hive, bead), msg)
    work.submit(bead=bead, hive="myrepo")
    fakebd.approve(bead)


def test_merge_no_ff_lands_and_closes(hive, fakebd):
    fakebd.seed("mr-10", title="t")
    _take_to_approved(hive, fakebd, "mr-10")

    work.merge(bead="mr-10", hive="myrepo", rm=False, molecule=False)

    # a real merge commit landed on the integration branch (two parents, --no-ff)
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=hive.main).stdout.strip() == "main"
    assert (
        _git("log", "-1", "--format=%s", cwd=hive.main).stdout.strip() == "chore(merge): bead mr-10"
    )
    parents = _git("rev-list", "--parents", "-n", "1", "HEAD", cwd=hive.main).stdout.split()
    assert len(parents) == 3  # commit + two parents
    # merge commit carries the agent-mode merger identity, and the bead's change is integrated
    assert _git("log", "-1", "--format=%an", cwd=hive.main).stdout.strip() == "dev/default"
    assert (hive.main / "change.txt").exists()
    assert fakebd.beads["mr-10"]["status"] == "closed"
    assert fakebd.did("merge-slot", "acquire") and fakebd.did("merge-slot", "release")


# ---- review-label hygiene (bh-mgo3): clear stale review:pending on approve/merge + backfill ---


def test_approve_clears_stale_review_pending_label(hive, fakebd):
    """Resolving the review gate to approved strips the review:pending dimension label — else a
    'what's awaiting review' query keeps surfacing the bead after review is done (bh-mgo3)."""
    fakebd.seed("mr-95", title="t")
    work.claim(bead="mr-95", as_="", hive="myrepo")
    _commit(_wt(hive, "mr-95"), "feat: x")
    work.submit(bead="mr-95", hive="myrepo")
    fakebd.beads["mr-95"]["labels"] = ["review:pending"]  # the label bd materializes at submit

    work.approve(bead="mr-95", as_="dev/reviewer", hive="myrepo")

    assert fakebd.did("label", "remove", "review:pending")
    assert "review:pending" not in (fakebd.beads["mr-95"].get("labels") or [])


def test_merge_clears_stale_review_pending_label(hive, fakebd):
    """Merging (and closing) a bead strips its review:pending label so closed work no longer
    pollutes review queries (bh-mgo3)."""
    fakebd.seed("mr-96", title="t")
    _take_to_approved(hive, fakebd, "mr-96")
    fakebd.beads["mr-96"]["labels"] = ["review:pending"]

    work.merge(bead="mr-96", hive="myrepo", rm=False, molecule=False)

    assert fakebd.beads["mr-96"]["status"] == "closed"
    assert "review:pending" not in (fakebd.beads["mr-96"].get("labels") or [])


def test_backfill_strips_review_pending_from_closed_beads(hive, fakebd):
    """One-time backfill clears review:pending from every already-closed bead and leaves open
    in-review beads untouched, so a review:pending query returns only genuine work (bh-mgo3)."""
    fakebd.seed("old-1", title="a", status="closed", labels=["review:pending", "org:x"])
    fakebd.seed("old-2", title="b", status="closed", labels=["review:pending"])
    fakebd.seed("live-1", title="c", status="open", labels=["review:pending"])

    cleaned = work.backfill_stale_review_labels(hive.main)

    assert cleaned == 2
    assert fakebd.beads["old-1"]["labels"] == ["org:x"]  # non-review labels preserved
    assert "review:pending" not in (fakebd.beads["old-2"].get("labels") or [])
    assert fakebd.beads["live-1"]["labels"] == ["review:pending"]  # open bead untouched


def test_merge_otel_off_emits_no_span(hive, fakebd, monkeypatch):
    # Acceptance (otel off, the default): a real `ws work merge` lands exactly as before and
    # never builds a span — instrumentation is a zero-overhead no-op.
    monkeypatch.setattr(otel, "span", MagicMock(side_effect=AssertionError("no span when off")))
    fakebd.seed("mr-14", title="t")
    _take_to_approved(hive, fakebd, "mr-14")

    work.merge(bead="mr-14", hive="myrepo", rm=False, molecule=False)

    assert fakebd.beads["mr-14"]["status"] == "closed"  # unchanged behavior


def test_merge_otel_on_emits_subprocess_and_verb_spans_and_metrics(hive, fakebd, monkeypatch):
    # Acceptance (otel on, mocked provider): a real `ws work merge` produces the verb span, the
    # subprocess (git) span at the run() seam, the merge-duration metric, and the lifecycle counter.
    fakebd.seed("mr-15", title="t")
    _take_to_approved(hive, fakebd, "mr-15")  # taken to approved with otel still off

    tracer = MagicMock(name="tracer")
    meter = MagicMock(name="meter")
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(otel, "get_tracer", lambda *a, **k: tracer)
    monkeypatch.setattr(otel, "get_meter", lambda *a, **k: meter)
    otel._instruments.clear()

    work.merge(bead="mr-15", hive="myrepo", rm=False, molecule=False)

    span_names = [c.args[0] for c in tracer.start_as_current_span.call_args_list]
    assert "work.merge" in span_names  # the verb span
    assert any(n.startswith("git") for n in span_names)  # ≥1 subprocess span at the run() seam

    # merge.duration is one of several flow histograms the seam now emits — assert it's present
    # (it's no longer the LAST create_histogram call now that cycle/stage/slot ride here too).
    hist_names = {c.args[0] for c in meter.create_histogram.call_args_list}
    assert "bh.work.merge.duration" in hist_names
    assert meter.create_histogram.return_value.record.call_count >= 1
    adds = meter.create_counter.return_value.add.call_args_list
    # All counters share one mocked instrument (merge.outcome rides here too) — pick the bead
    # transitions by their key. The bead id is no longer a metric attr; it rides the span instead.
    transitions = [
        c.args[1]["bh.bead.transition"] for c in adds if "bh.bead.transition" in c.args[1]
    ]
    assert "merged" in transitions
    assert not any("bh.bead" in c.args[1] for c in adds)  # bead id never on a metric point

    otel._instruments.clear()  # don't leak mocked instruments into later tests


def test_merge_refuses_open_gate(hive, fakebd):
    fakebd.seed("mr-11", title="t")
    work.claim(bead="mr-11", as_="", hive="myrepo")
    _commit(_wt(hive, "mr-11"), "feat: x")
    work.submit(bead="mr-11", hive="myrepo")  # gate opened, NOT approved
    before = _git("rev-parse", "HEAD", cwd=hive.main).stdout.strip()
    with pytest.raises(typer.Exit):
        work.merge(bead="mr-11", hive="myrepo", rm=False, molecule=False)
    assert _git("rev-parse", "HEAD", cwd=hive.main).stdout.strip() == before  # main untouched
    assert fakebd.beads["mr-11"]["status"] != "closed"


def test_merge_refuses_changes_requested(hive, fakebd):
    fakebd.seed("mr-12", title="t")
    _take_to_approved(hive, fakebd, "mr-12")
    fakebd.states["mr-12"]["review"] = "changes-requested"  # bounced after approval
    with pytest.raises(typer.Exit):
        work.merge(bead="mr-12", hive="myrepo", rm=False, molecule=False)
    assert fakebd.beads["mr-12"]["status"] != "closed"


def test_merge_rm_removes_worktree(hive, fakebd):
    fakebd.seed("mr-13", title="t")
    _take_to_approved(hive, fakebd, "mr-13")
    assert _wt(hive, "mr-13").exists()
    work.merge(bead="mr-13", hive="myrepo", rm=True, molecule=False)
    assert not _wt(hive, "mr-13").exists()


# ---- rebase-then-retry conflict recovery -----------------------------------
#
# Two file-coupled-but-DAG-parallel beads. When the second's plain --no-ff merge conflicts, the
# merge verb attempts a bounded recovery: snapshot the bead branch, rebase it onto the newer base,
# and retry — landing the replayable case without hand serialization. A genuinely divergent edit
# stays a real conflict: it fails cleanly with the bead branch RESTORED, so work is never dropped.
#
# Note: modern git (ort) often auto-resolves the replayable "coupled" case at merge time, so the
# happy-path test asserts the acceptance-level property (both beads land, no manual step, nothing
# dropped) rather than which mechanism fired. The recovery orchestration itself (snapshot → rebase
# → restore) is exercised deterministically by the divergent-conflict test below.


def _set_line(wt, content, fname="shared.txt"):
    """Overwrite `fname` to `content` in the worktree and commit it (conventional subject)."""
    (Path(wt) / fname).write_text(content)
    _git("add", "-A", cwd=wt)
    _git("commit", "-qm", f"feat: set {content.strip()}", cwd=wt)


def _append(wt, line, fname="shared.txt"):
    """Append `line` to `fname` in the worktree and commit it (conventional subject)."""
    p = Path(wt) / fname
    p.write_text(p.read_text() + line)
    _git("add", "-A", cwd=wt)
    _git("commit", "-qm", f"feat: append {line.strip()}", cwd=wt)


def test_merge_lands_coupled_beads_without_manual_step(hive, fakebd):
    """Two coupled beads touch the same file: A adds a boilerplate line; B adds the same line
    (a patch git can replay-skip) plus its own unique line. Both land via the merger with no hand
    serialization — the second is recovered by rebase-retry when its plain merge conflicts — and
    no work is dropped: the final file carries A's line once and B's unique line, under a real
    --no-ff bubble. (HOME is isolated by the fixture so git config can't perturb the merge.)"""
    _commit(hive.main, "L0\n", fname="shared.txt")  # shared base so beads append (not add/add)
    fakebd.seed("mr-20", title="t")
    fakebd.seed("mr-21", title="t")
    # Claim BOTH before either merges, so they fork off the SAME base.
    work.claim(bead="mr-20", as_="", hive="myrepo")
    work.claim(bead="mr-21", as_="", hive="myrepo")
    _append(_wt(hive, "mr-20"), "shared\n")  # bead A adds the boilerplate line
    _append(_wt(hive, "mr-21"), "shared\n")  # bead B adds the SAME line (replay-skippable patch)…
    _append(_wt(hive, "mr-21"), "bonly\n")  # …plus its own unique change
    work.submit(bead="mr-20", hive="myrepo")
    fakebd.approve("mr-20")
    work.submit(bead="mr-21", hive="myrepo")
    fakebd.approve("mr-21")

    work.merge(bead="mr-20", hive="myrepo", rm=False, molecule=False)
    work.merge(bead="mr-21", hive="myrepo", rm=False, molecule=False)

    shared = (hive.main / "shared.txt").read_text()
    assert "bonly" in shared  # bead B's unique work landed
    assert shared.count("shared") == 1  # A's coupled line is present exactly once (no dup, no loss)
    # history preserved: the second bead landed as a real --no-ff merge bubble
    assert (
        _git("log", "-1", "--format=%s", cwd=hive.main).stdout.strip() == "chore(merge): bead mr-21"
    )
    parents = _git("rev-list", "--parents", "-n", "1", "HEAD", cwd=hive.main).stdout.split()
    assert len(parents) == 3  # merge commit + two parents
    assert fakebd.beads["mr-21"]["status"] == "closed"


def test_merge_real_conflict_fails_clean_and_restores_branch(hive, fakebd):
    """Two beads edit the SAME line divergently — a real conflict the rebase can't resolve. The
    recovery path runs (a `.premerge-*` snapshot is taken, the rebase is attempted and fails), then
    the merge fails non-zero with main untouched, the bead not closed, and the bead branch restored
    to its pre-rebase tip (work never dropped)."""
    _commit(hive.main, "base\n", fname="shared.txt")
    fakebd.seed("mr-30", title="t")
    fakebd.seed("mr-31", title="t")
    work.claim(bead="mr-30", as_="", hive="myrepo")
    work.claim(bead="mr-31", as_="", hive="myrepo")
    _set_line(_wt(hive, "mr-30"), "X\n")  # both rewrite the one line they share, divergently
    _set_line(_wt(hive, "mr-31"), "Y\n")
    work.submit(bead="mr-30", hive="myrepo")
    fakebd.approve("mr-30")
    work.submit(bead="mr-31", hive="myrepo")
    fakebd.approve("mr-31")

    work.merge(bead="mr-30", hive="myrepo", rm=False, molecule=False)  # clean → base has X

    main_tip = _git("rev-parse", "main", cwd=hive.main).stdout.strip()
    branch_tip = _git("rev-parse", "wt/bead/issue/mr-31", cwd=hive.main).stdout.strip()
    with pytest.raises(typer.Exit):
        work.merge(bead="mr-31", hive="myrepo", rm=False, molecule=False)

    assert _git("rev-parse", "main", cwd=hive.main).stdout.strip() == main_tip  # main untouched
    # the bead branch is restored to its exact pre-merge tip, still carrying its divergent change
    assert _git("rev-parse", "wt/bead/issue/mr-31", cwd=hive.main).stdout.strip() == branch_tip
    assert _git("show", "wt/bead/issue/mr-31:shared.txt", cwd=hive.main).stdout.strip() == "Y"
    # the recovery path was entered: a pre-merge snapshot of the bead branch exists
    branches = _git("branch", "--list", "wt/bead/issue/mr-31.premerge-*", cwd=hive.main).stdout
    assert "premerge" in branches
    assert fakebd.beads["mr-31"]["status"] != "closed"
    assert fakebd.did("merge-slot", "release")  # slot freed even on the failing path


# ---- commit-flow metrics at the merge seam (hqfy.2) ------------------------


def _otel_meter_on(monkeypatch):
    """Force otel on with a mocked tracer + meter (the SDK isn't installed in the test env)."""
    tracer = MagicMock(name="tracer")
    meter = MagicMock(name="meter")
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(otel, "get_tracer", lambda *a, **k: tracer)
    monkeypatch.setattr(otel, "get_meter", lambda *a, **k: meter)
    otel._instruments.clear()
    return meter


def _iso_ago(**kw):
    dt = datetime.datetime.now(datetime.UTC) - datetime.timedelta(**kw)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_merge_emits_slot_cycle_stage_outcome_metrics(hive, fakebd, monkeypatch):
    """The happy merge seam emits slot wait/hold, cycle_time(+active), the coding/review_wait/
    merge_latency stage breakdown and a merge.outcome counter — bounded attrs only, no bead id."""
    fakebd.seed("mr-40", title="t")
    _take_to_approved(hive, fakebd, "mr-40")
    # at-merge bd reads: created/started on the bead, a review→pending + changes-requested event,
    # and a resolved review gate (reason 'review <sha>').
    fakebd.beads["mr-40"].update(created_at=_iso_ago(hours=2), started_at=_iso_ago(hours=1))
    fakebd.beads["mr-40.e1"] = {
        "id": "mr-40.e1",
        "parent": "mr-40",
        "issue_type": "event",
        "title": "set-state review=pending",
        "created_at": _iso_ago(minutes=40),
    }
    fakebd.beads["mr-40.e2"] = {
        "id": "mr-40.e2",
        "parent": "mr-40",
        "issue_type": "event",
        "title": "review=changes-requested",
    }
    # the review gate submit opened + approve resolved carries the resolution timestamp
    fakebd.gates[0].update(status="closed", closed_at=_iso_ago(minutes=10))

    meter = _otel_meter_on(monkeypatch)
    work.merge(bead="mr-40", hive="myrepo", rm=False, molecule=False)

    hist_names = {c.args[0] for c in meter.create_histogram.call_args_list}
    assert {
        "bh.work.merge_slot.wait",
        "bh.work.merge_slot.hold",
        "bh.work.cycle_time",
        "bh.work.cycle_time.active",
        "bh.work.stage.coding",
        "bh.work.stage.review_wait",
        "bh.work.stage.merge_latency",
    } <= hist_names
    adds = meter.create_counter.return_value.add.call_args_list
    outcomes = [c.args[1] for c in adds if "bh.merge.how" in c.args[1]]
    assert len(outcomes) == 1
    assert outcomes[0]["bh.merge.kind"] == "bead" and outcomes[0]["bh.hive"] == "mr"
    assert outcomes[0]["bh.merge.how"] in ("clean", "rebased", "union")
    assert all("bh.bead" not in c.args[1] and "bh.epic" not in c.args[1] for c in adds)
    otel._instruments.clear()


def test_merge_bd_read_failure_does_not_block_merge(hive, fakebd, monkeypatch):
    """A bead with NO timestamps/events/gate (the at-merge reads come back empty) still merges and
    closes — the flow metrics are best-effort and never block the land."""
    fakebd.seed("mr-45", title="t")
    _take_to_approved(hive, fakebd, "mr-45")  # no created_at/started_at/events seeded
    _otel_meter_on(monkeypatch)
    work.merge(bead="mr-45", hive="myrepo", rm=False, molecule=False)
    assert fakebd.beads["mr-45"]["status"] == "closed"  # merge succeeded regardless
    otel._instruments.clear()


def test_merge_conflict_emits_conflict_outcome(hive, fakebd, monkeypatch):
    """A real conflict bumps the merge.outcome counter with how=conflict BEFORE the raise."""
    _commit(hive.main, "base\n", fname="shared.txt")
    fakebd.seed("mr-30", title="t")
    fakebd.seed("mr-31", title="t")
    work.claim(bead="mr-30", as_="", hive="myrepo")
    work.claim(bead="mr-31", as_="", hive="myrepo")
    _set_line(_wt(hive, "mr-30"), "X\n")
    _set_line(_wt(hive, "mr-31"), "Y\n")
    work.submit(bead="mr-30", hive="myrepo")
    fakebd.approve("mr-30")
    work.submit(bead="mr-31", hive="myrepo")
    fakebd.approve("mr-31")
    work.merge(bead="mr-30", hive="myrepo", rm=False, molecule=False)  # clean → base has X

    meter = _otel_meter_on(monkeypatch)
    with pytest.raises(typer.Exit):
        work.merge(bead="mr-31", hive="myrepo", rm=False, molecule=False)  # real conflict

    adds = meter.create_counter.return_value.add.call_args_list
    outcomes = [c.args[1] for c in adds if "bh.merge.how" in c.args[1]]
    assert any(o["bh.merge.how"] == "conflict" and o["bh.merge.kind"] == "bead" for o in outcomes)
    otel._instruments.clear()


def test_check_emits_validation_duration(hive, fakebd, monkeypatch):
    fakebd.seed("mr-60", title="t")
    work.claim(bead="mr-60", as_="", hive="myrepo")
    meter = _otel_meter_on(monkeypatch)
    work.check(bead="mr-60", hive="myrepo")
    records = meter.create_histogram.return_value.record.call_args_list
    vd = [c.args[1] for c in records if c.args[1].get("bh.work.phase") == "check"]
    assert vd and vd[0]["bh.validation.result"] == "pass" and vd[0]["bh.hive"] == "mr"
    assert "bh.bead" not in vd[0]
    hist_names = {c.args[0] for c in meter.create_histogram.call_args_list}
    assert "bh.work.validation.duration" in hist_names
    otel._instruments.clear()


def test_submit_emits_validation_duration(hive, fakebd, monkeypatch):
    fakebd.seed("mr-61", title="t")
    work.claim(bead="mr-61", as_="", hive="myrepo")
    _commit(_wt(hive, "mr-61"), "feat: x")
    meter = _otel_meter_on(monkeypatch)
    work.submit(bead="mr-61", hive="myrepo")
    records = meter.create_histogram.return_value.record.call_args_list
    vd = [c.args[1] for c in records if c.args[1].get("bh.work.phase") == "submit"]
    assert vd and vd[0]["bh.validation.result"] == "pass" and vd[0]["bh.hive"] == "mr"
    otel._instruments.clear()


# ---- molecule-aware base (two-level integration) ---------------------------
#
# A bead id `mr-1.1` has epic `mr-1`; when `wt/bead/epic/mr-1` exists in the main clone the
# molecule was kicked off, so the bead measures + merges against it (not `main`). A bead
# with no `.` (mr-10 above) has no molecule and still targets `main` — see the merge tests above.


def _mol_branch(hive, epic, extra_subject=""):
    """Create the container integration branch `wt/bead/epic/<epic>` off main. With
    commit ahead of main so the molecule diverges and the resolved base is observable."""
    _git("branch", f"wt/bead/epic/{epic}", "main", cwd=hive.main)
    if extra_subject:
        _git("checkout", "-q", f"wt/bead/epic/{epic}", cwd=hive.main)
        _commit(hive.main, extra_subject, fname="mol.txt")
        _git("checkout", "-q", "main", cwd=hive.main)


def _wt_of(hive, bead):
    """Worktree dir for a (possibly dotted) bead — the leaf is sanitized (mr-1.1 -> mr-1-1)."""
    return hive.wts / "github" / "myorg" / "myrepo" / registry.sanitize(bead)


def test_merge_lands_bead_into_molecule_not_main(hive, fakebd):
    """A bead in a kicked-off molecule merges into its container --no-ff; main stays untouched."""
    _mol_branch(hive, "mr-1")
    main_before = _git("rev-parse", "main", cwd=hive.main).stdout.strip()
    fakebd.seed("mr-1.1", title="t")
    work.claim(bead="mr-1.1", as_="", hive="myrepo")
    _commit(_wt_of(hive, "mr-1.1"), "feat: the change")
    work.submit(bead="mr-1.1", hive="myrepo")
    fakebd.approve("mr-1.1")

    work.merge(bead="mr-1.1", hive="myrepo", rm=False, molecule=False)

    # the bead landed on wt/bead/epic/mr-1, not main — the molecule assembles in isolation
    mol = "wt/bead/epic/mr-1"
    mol_tip_subject = _git("log", "-1", "--format=%s", mol, cwd=hive.main).stdout.strip()
    assert mol_tip_subject == "chore(merge): bead mr-1.1"
    parents = _git("rev-list", "--parents", "-n", "1", mol, cwd=hive.main).stdout.split()
    assert len(parents) == 3  # merge commit + two parents (--no-ff)
    assert _git("rev-parse", "main", cwd=hive.main).stdout.strip() == main_before  # main untouched
    assert fakebd.beads["mr-1.1"]["status"] == "closed"


def test_submit_measures_history_against_molecule(hive, fakebd):
    """submit's history guard is computed against the container `wt/bead/epic/<epic>`: a noisy
    molecule branch stays out of the bead's range, so submit passes. Measured against main the same
    range would drag in that non-conventional commit and be rejected — so a green submit proves
    the molecule-aware base."""
    _mol_branch(hive, "mr-1", extra_subject="wip molecule scratch")  # mol = main + a noisy commit
    fakebd.seed("mr-1.1", title="t")
    work.claim(bead="mr-1.1", as_="", hive="myrepo")
    wt = _wt_of(hive, "mr-1.1")
    # The bead forks off the molecule tip (start-point threading is a sibling bead's job; here we
    # only exercise which base work.py measures against).
    _git("reset", "--hard", "wt/bead/epic/mr-1", cwd=wt)
    _commit(wt, "feat: the change")

    work.submit(bead="mr-1.1", hive="myrepo")  # raises if measured against main (noisy range)

    assert fakebd.states["mr-1.1"]["review"] == "pending"


def test_show_measures_against_molecule(hive, fakebd, capsys):
    """show renders base..branch against the molecule tip, not main, when the container exists."""
    _mol_branch(hive, "mr-1", extra_subject="wip molecule scratch")
    fakebd.seed("mr-1.1", title="t")
    work.claim(bead="mr-1.1", as_="", hive="myrepo")
    wt = _wt_of(hive, "mr-1.1")
    _git("reset", "--hard", "wt/bead/epic/mr-1", cwd=wt)
    _commit(wt, "feat: the change")

    capsys.readouterr()  # drain claim/setup chatter so only show's JSON remains
    work.show(bead="mr-1.1", view=["log"], json_out=True, hive="myrepo")

    payload = json.loads(capsys.readouterr().out.strip())
    mol_tip = _git("rev-parse", "wt/bead/epic/mr-1", cwd=hive.main).stdout.strip()
    assert payload["base"] == mol_tip[:7]  # forked off the molecule, so base == mol tip


# ---- merge --molecule (the wrap-up / land verb) ----------------------------
#
# When the molecule is whole, `ws work merge <epic> --molecule` collapses the assembled
# `wt/bead/epic/<epic>` (holding the per-bead --no-ff merges) onto the hive integration branch
# as ONE --no-ff bubble, closes the epic, and deletes the branch — the two-level AGF integration
# shape.


def _land_two_bead_molecule(hive, fakebd, epic="mr-1"):
    """Build a complete molecule: kick off the container, then claim→commit→submit→approve→merge two
    child beads INTO it. Leaves the epic open with both children closed, ready to land."""
    _mol_branch(hive, epic)
    fakebd.seed(epic, title="epic")
    for bid in (f"{epic}.1", f"{epic}.2"):
        fakebd.seed(bid, title="t", parent=epic)
        work.claim(bead=bid, as_="", hive="myrepo")
        _commit(_wt_of(hive, bid), f"feat: {bid}")
        work.submit(bead=bid, hive="myrepo")
        fakebd.approve(bid)
        work.merge(bead=bid, hive="myrepo", rm=False, molecule=False)


def test_merge_molecule_lands_as_one_bubble(hive, fakebd):
    _land_two_bead_molecule(hive, fakebd, "mr-1")
    main_before = _git("rev-parse", "main", cwd=hive.main).stdout.strip()

    work.merge(bead="mr-1", hive="myrepo", molecule=True)

    # ONE --no-ff bubble on main: subject "chore(merge): molecule <epic>", merge + two parents
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=hive.main).stdout.strip() == "main"
    assert (
        _git("log", "-1", "--format=%s", cwd=hive.main).stdout.strip()
        == "chore(merge): molecule mr-1"
    )
    parents = _git("rev-list", "--parents", "-n", "1", "HEAD", cwd=hive.main).stdout.split()
    assert len(parents) == 3
    assert _git("rev-parse", "main", cwd=hive.main).stdout.strip() != main_before  # main advanced
    # the per-bead merges live INSIDE the bubble (reachable from main now)
    subjects = _git("log", "--format=%s", "main", cwd=hive.main).stdout.split("\n")
    assert "chore(merge): bead mr-1.1" in subjects and "chore(merge): bead mr-1.2" in subjects
    # epic closed (reason recorded), molecule branch deleted, slot released
    assert fakebd.beads["mr-1"]["status"] == "closed"
    assert fakebd.did("close", "mr-1", "--reason", "molecule landed")
    assert not worktree._branch_exists(hive.main, "wt/bead/epic/mr-1")
    assert fakebd.did("merge-slot", "acquire") and fakebd.did("merge-slot", "release")


# ---- start / finish: epic-only aliases (kickoff + land) ---------------------


def test_start_opens_molecule_and_claims_epic(hive, fakebd):
    """start <epic> --as disp/<id> opens the container AND provisions the coordinator seat
    worktree on wt/bead/epic/<epic> (integration-plane kickoff via ensure kind='epic'), then takes
    the epic seat (in_progress, assigned to the coordinator)."""
    fakebd.seed("mr-epic", title="e", issue_type="epic")
    fakebd.states["mr-epic"] = {"kickoff": "approved"}
    work.start(epic="mr-epic", as_="disp/lead", hive="myrepo")
    assert _mol_listed(hive, "mr-epic") != ""  # container branch opened
    # the coordinator seat worktree is provisioned on the container branch (not a Phase-A no-op)
    seat = worktree.locate(config.load(), "myrepo", "mr-epic", kind="epic")[2]
    assert seat.exists()
    assert (
        _git("rev-parse", "--abbrev-ref", "HEAD", cwd=seat).stdout.strip() == "wt/bead/epic/mr-epic"
    )
    assert fakebd.beads["mr-epic"]["status"] == "in_progress"
    assert fakebd.beads["mr-epic"]["assignee"] == "disp/lead"


def test_finish_lands_nested_epic_onto_workstream_then_workstream_onto_main(hive, fakebd):
    """Recursive land (xn3o.7): finish resolves its target one tier up via integration_base.
    finish <ws>.<epic> lands the child-epic container onto the WORKSTREAM container (not main);
    then finish <ws> lands the workstream container onto main. Top-level epics stay byte-identical
    (their integration_base is main); this proves the nested tier."""
    main_before = _git("rev-parse", "main", cwd=hive.main).stdout.strip()

    # workstream (epic-of-epics) → child epic → leaf issue; all epic-typed containers get a seat.
    fakebd.seed("mr-ws", title="workstream", issue_type="epic")
    fakebd.states["mr-ws"] = {"kickoff": "approved"}
    fakebd.seed("mr-ws.1", title="child epic", issue_type="epic", parent="mr-ws")
    fakebd.states["mr-ws.1"] = {"kickoff": "approved"}
    work.start(epic="mr-ws", as_="disp/ws", hive="myrepo")  # seat wt/bead/epic/mr-ws off main
    work.start(epic="mr-ws.1", as_="disp/e", hive="myrepo")  # seat forked off the workstream

    # the nested container forked off the workstream, not main
    entry = registry.resolve_hive(config.load(), "myrepo")
    assert worktree.integration_base(entry, "mr-ws.1", "main") == "wt/bead/epic/mr-ws"

    # land one leaf INTO the child-epic container
    fakebd.seed("mr-ws.1.1", title="t", parent="mr-ws.1")
    work.claim(bead="mr-ws.1.1", as_="", hive="myrepo")
    _commit(_wt_of(hive, "mr-ws.1.1"), "feat: mr-ws.1.1")
    work.submit(bead="mr-ws.1.1", hive="myrepo")
    fakebd.approve("mr-ws.1.1")
    work.merge(bead="mr-ws.1.1", hive="myrepo", rm=False, molecule=False)

    ws_before = _git("rev-parse", "wt/bead/epic/mr-ws", cwd=hive.main).stdout.strip()
    work.finish(epic="mr-ws.1", hive="myrepo")  # lands child epic onto the workstream container

    # the child-epic bubble landed on the WORKSTREAM container, and main is untouched
    ws_after = _git("rev-parse", "wt/bead/epic/mr-ws", cwd=hive.main).stdout.strip()
    assert ws_after != ws_before
    ws_tip = _git("log", "-1", "--format=%s", "wt/bead/epic/mr-ws", cwd=hive.main).stdout.strip()
    assert ws_tip == "chore(merge): molecule mr-ws.1"
    assert _git("rev-parse", "main", cwd=hive.main).stdout.strip() == main_before  # main untouched
    assert fakebd.beads["mr-ws.1"]["status"] == "closed"
    # child container torn down
    assert not worktree._branch_exists(hive.main, "wt/bead/epic/mr-ws.1")

    # now the workstream itself lands onto main (its integration_base is the dotless root → main)
    work.finish(epic="mr-ws", hive="myrepo")
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=hive.main).stdout.strip() == "main"
    assert (
        _git("log", "-1", "--format=%s", cwd=hive.main).stdout.strip()
        == "chore(merge): molecule mr-ws"
    )
    assert _git("rev-parse", "main", cwd=hive.main).stdout.strip() != main_before  # main advanced
    assert fakebd.beads["mr-ws"]["status"] == "closed"
    assert not worktree._branch_exists(hive.main, "wt/bead/epic/mr-ws")


def test_finish_tears_down_coordinator_seat(hive, fakebd):
    """finish tears the seat down after the land: the coordinator worktree is removed AND the
    container branch wt/bead/epic/<epic> is deleted (mirrors merge --rm)."""
    fakebd.seed("mr-1", title="epic", issue_type="epic")
    fakebd.states["mr-1"] = {"kickoff": "approved"}
    work.start(epic="mr-1", as_="disp/lead", hive="myrepo")
    seat = worktree.locate(config.load(), "myrepo", "mr-1", kind="epic")[2]
    assert seat.exists()  # provisioned by start

    # land one child INTO the container so the molecule is non-empty + complete
    fakebd.seed("mr-1.1", title="t", parent="mr-1")
    work.claim(bead="mr-1.1", as_="", hive="myrepo")
    _commit(_wt_of(hive, "mr-1.1"), "feat: mr-1.1")
    work.submit(bead="mr-1.1", hive="myrepo")
    fakebd.approve("mr-1.1")
    work.merge(bead="mr-1.1", hive="myrepo", rm=False, molecule=False)

    work.finish(epic="mr-1", hive="myrepo")

    assert not seat.exists()  # seat worktree torn down
    assert not worktree._branch_exists(hive.main, "wt/bead/epic/mr-1")  # container branch deleted
    assert fakebd.beads["mr-1"]["status"] == "closed"


def test_start_rejects_non_epic(hive, fakebd):
    """start refuses a leaf bead — that's `claim`'s job."""
    fakebd.seed("mr-5", title="t")
    with pytest.raises(typer.Exit):
        work.start(epic="mr-5", as_="disp/lead", hive="myrepo")


def test_start_requires_kickoff_approved(hive, fakebd):
    """start refuses an epic that planning hasn't approved (no molecule opened)."""
    fakebd.seed("mr-epic", title="e", issue_type="epic")  # no kickoff state
    with pytest.raises(typer.Exit):
        work.start(epic="mr-epic", as_="disp/lead", hive="myrepo")
    assert _mol_listed(hive, "mr-epic") == ""


def test_start_requires_coordinator_seat(hive, fakebd):
    """start refuses a developer identity — an epic is a dispatcher's seat (no molecule opened)."""
    fakebd.seed("mr-epic", title="e", issue_type="epic")
    fakebd.states["mr-epic"] = {"kickoff": "approved"}
    with pytest.raises(typer.Exit):
        work.start(epic="mr-epic", as_="dev/dev", hive="myrepo")
    assert _mol_listed(hive, "mr-epic") == ""


# ---- dispatch convention gate (assign / claim / start) ----------------------
#
# The coordinator guards refuse to route work off a MALFORMED molecule, surfacing plan.verify_epic's
# specific problem list. These tests drive verify_epic explicitly (the fakebd fixture otherwise
# neutralizes it) to prove the gate wiring on each dispatch verb, plus the BH_DEBUG override.


def _malformed(*problems):
    """A verify_epic stub returning a fixed problem list, ignoring its args."""
    return lambda *a, **k: list(problems)


def test_dispatch_gate_refuses_malformed_epic_on_claim(hive, fakebd, capsys, monkeypatch):
    """claiming a child of a malformed molecule refuses with the validator's problem list — the
    child is NOT claimed."""
    fakebd.seed("mr-1.1", title="t")  # leaf child of epic mr-1
    monkeypatch.setattr(
        plan,
        "verify_epic",
        _malformed("mr-1: no bd swarm", "mr-1.1: missing identity label 'org:'"),
    )
    with pytest.raises(typer.Exit):
        work.claim(bead="mr-1.1", as_="dev/dev", hive="myrepo")
    err = capsys.readouterr().err
    assert "no bd swarm" in err
    assert "missing identity label" in err
    assert not fakebd.did("update", "mr-1.1", "--claim")


def test_dispatch_gate_refuses_malformed_epic_on_assign(hive, fakebd, capsys, monkeypatch):
    """assign (orchestrator dispatch) refuses a child of a malformed molecule — no assignee set."""
    fakebd.seed("mr-1.1", title="t")
    monkeypatch.setattr(plan, "verify_epic", _malformed("mr-1: no bd swarm"))
    with pytest.raises(typer.Exit):
        work.assign(bead="mr-1.1", to="dev/dev", as_="disp/lead", hive="myrepo")
    assert "no bd swarm" in capsys.readouterr().err
    assert not fakebd.did("assign", "mr-1.1", "dev/dev")


def test_dispatch_gate_refuses_malformed_epic_on_start(hive, fakebd, capsys, monkeypatch):
    """start refuses a malformed epic — no molecule opened, epic not marked in_progress."""
    fakebd.seed("mr-epic", title="e", issue_type="epic")
    fakebd.states["mr-epic"] = {"kickoff": "approved"}
    monkeypatch.setattr(plan, "verify_epic", _malformed("mr-epic: no bd swarm"))
    with pytest.raises(typer.Exit):
        work.start(epic="mr-epic", as_="disp/lead", hive="myrepo")
    assert "no bd swarm" in capsys.readouterr().err
    assert _mol_listed(hive, "mr-epic") == ""
    assert fakebd.beads["mr-epic"]["status"] != "in_progress"


def test_dispatch_gate_bhdebug_overrides_on_start(hive, fakebd, capsys, monkeypatch):
    """BH_DEBUG downgrades the dispatch gate to a warning so a human can force a malformed epic
    through — start proceeds (molecule opened, epic claimed)."""
    fakebd.seed("mr-epic", title="e", issue_type="epic")
    fakebd.states["mr-epic"] = {"kickoff": "approved"}
    monkeypatch.setattr(plan, "verify_epic", _malformed("mr-epic: no bd swarm"))
    monkeypatch.setenv("BH_DEBUG", "1")
    work.start(epic="mr-epic", as_="disp/lead", hive="myrepo")
    assert _mol_listed(hive, "mr-epic") != ""
    assert fakebd.beads["mr-epic"]["status"] == "in_progress"
    assert "BH_DEBUG override" in capsys.readouterr().err


def test_dispatch_gate_passes_wellformed_and_resolves_parent(hive, fakebd, monkeypatch):
    """A well-formed molecule dispatches unchanged; the gate verifies the child's PARENT epic."""
    fakebd.seed("mr-1.1", title="t", parent="mr-1")
    seen = {}

    def _verify(epic_id, cfg, cwd):
        seen["epic"] = epic_id
        return []

    monkeypatch.setattr(plan, "verify_epic", _verify)
    work.claim(bead="mr-1.1", as_="dev/dev", hive="myrepo")
    assert seen["epic"] == "mr-1"  # resolved parent epic, not the child id
    assert fakebd.did("update", "mr-1.1", "--claim")


def test_finish_lands_molecule_like_merge_molecule(hive, fakebd):
    """finish <epic> is the epic-only alias of `merge --molecule`: lands the assembled molecule as
    one bubble and closes the epic."""
    _land_two_bead_molecule(hive, fakebd, "mr-1")
    fakebd.beads["mr-1"]["issue_type"] = "epic"  # finish guards issue_type == epic
    work.finish(epic="mr-1", hive="myrepo")
    assert (
        _git("log", "-1", "--format=%s", cwd=hive.main).stdout.strip()
        == "chore(merge): molecule mr-1"
    )
    assert fakebd.beads["mr-1"]["status"] == "closed"
    assert not worktree._branch_exists(hive.main, "wt/bead/epic/mr-1")


def test_finish_rejects_non_epic(hive, fakebd):
    """finish refuses a non-epic bead."""
    fakebd.seed("mr-5", title="t")
    with pytest.raises(typer.Exit):
        work.finish(epic="mr-5", hive="myrepo")


def test_validation_mode_gates_molecule_clean_checkouts(hive, fakebd, monkeypatch):
    """relaxed runs exactly the assembled-mol pre-land check (1 clean_checkout); loose skips even
    that (0); conservative adds the post-land re-test (2). Asserts mode gating at the molecule
    boundary without depending on validate outcome (config validate_cmd is `true`). Molecule lands
    in every mode."""
    seen = []
    real_cc = worktree.clean_checkout
    monkeypatch.setattr(
        worktree,
        "clean_checkout",
        lambda entry, branch, cmd: seen.append(branch) or real_cc(entry, branch, cmd),
    )

    for mode, expected in (("relaxed", 1), ("loose", 0), ("conservative", 2)):
        epic = f"mr-{mode}"
        _land_two_bead_molecule(hive, fakebd, epic)  # setup runs its own validations
        monkeypatch.setattr(config, "validation_mode", lambda cfg, entry, m=mode: m)
        seen.clear()  # count only the molecule-land boundary
        work.merge(bead=epic, hive="myrepo", molecule=True)
        assert len(seen) == expected, f"{mode}: {seen}"
        assert fakebd.beads[epic]["status"] == "closed"


def test_validation_mode_per_point_entrypoint(hive, fakebd, monkeypatch):
    """A per-point override at work.validate.<phase> wins over validate_cmd for that boundary."""
    hive.cfg_path.write_text(
        CONFIG_YAML.replace(
            'validate_cmd: "true"',
            'validate_cmd: "true"\n  validate: {molecule: "true # MOLECULE"}',
        )
    )
    seen = []
    real_cc = worktree.clean_checkout
    monkeypatch.setattr(
        worktree,
        "clean_checkout",
        lambda entry, branch, cmd: seen.append(cmd) or real_cc(entry, branch, cmd),
    )
    _land_two_bead_molecule(hive, fakebd, "mr-1")  # setup uses validate_cmd ("true")
    seen.clear()  # observe only the molecule-land boundary
    work.merge(bead="mr-1", hive="myrepo", molecule=True)

    # relaxed default → one molecule-phase validation, using the per-point command, not validate_cmd
    assert seen == ["true # MOLECULE"]


def test_merge_molecule_revalidates_and_rolls_back_when_main_went_stale_red(hive, fakebd):
    """main advances after the molecule was cut; the combined --no-ff tree is logically red. The
    pre-land mol validation passes, but the staleness-triggered POST-land validation (relaxed mode,
    a correctness backstop) catches it and rolls main back — lossless: mol branch preserved, epic
    still open."""
    # validate_cmd: green on the container (no marker), red once main advances into the tree
    hive.cfg_path.write_text(
        CONFIG_YAML.replace('validate_cmd: "true"', 'validate_cmd: "test ! -f main_advance.txt"')
    )
    _land_two_bead_molecule(hive, fakebd, "mr-1")

    # a concurrent commit lands directly on main AFTER the molecule forked → stale. (The bead
    # merges parked the clone on wt/bead/epic/mr-1, so check out main first or it poisons the mol.)
    _git("checkout", "-q", "main", cwd=hive.main)
    _commit(hive.main, "feat: concurrent", fname="main_advance.txt")
    advanced = _git("rev-parse", "main", cwd=hive.main).stdout.strip()

    with pytest.raises(typer.Exit):
        work.merge(bead="mr-1", hive="myrepo", molecule=True)

    # rolled back to the pre-land tip (the concurrent commit), NOT the merge bubble
    assert _git("rev-parse", "main", cwd=hive.main).stdout.strip() == advanced
    assert _git("log", "-1", "--format=%s", cwd=hive.main).stdout.strip() == "feat: concurrent"
    # lossless + not finalized: mol branch intact, epic still open, slot acquired+released
    assert worktree._branch_exists(hive.main, "wt/bead/epic/mr-1")
    assert fakebd.beads["mr-1"]["status"] != "closed"
    assert fakebd.did("merge-slot", "acquire") and fakebd.did("merge-slot", "release")


def test_merge_molecule_does_not_rewrite_shared_main_on_postland_red(hive, fakebd):
    """When the integration branch is shared (pushed → has an upstream), a post-land red must NOT
    rewrite it — the land was intentional; fix forward. The bubble stays on main, epic left open."""
    hive.cfg_path.write_text(
        CONFIG_YAML.replace('validate_cmd: "true"', 'validate_cmd: "test ! -f main_advance.txt"')
    )
    _land_two_bead_molecule(hive, fakebd, "mr-1")

    # main moves AND becomes shared (pushed → has an upstream). Check out main first: the bead
    # merges parked the clone on wt/bead/epic/mr-1.
    _git("checkout", "-q", "main", cwd=hive.main)
    _commit(hive.main, "feat: concurrent", fname="main_advance.txt")  # main moved → stale
    _git("push", "-u", "-q", "origin", "main", cwd=hive.main)  # now main has an upstream

    with pytest.raises(typer.Exit):
        work.merge(bead="mr-1", hive="myrepo", molecule=True)

    # NOT rewritten: the --no-ff bubble landed and stands on main (HEAD is the merge, not reset)
    assert (
        _git("log", "-1", "--format=%s", cwd=hive.main).stdout.strip()
        == "chore(merge): molecule mr-1"
    )
    # lossless + escalated, not finalized: epic still open, slot released
    assert fakebd.beads["mr-1"]["status"] != "closed"
    assert fakebd.did("merge-slot", "acquire") and fakebd.did("merge-slot", "release")


def test_merge_bead_conservative_rolls_back_and_bounces_on_combined_red(hive, fakebd):
    """conservative: a bead green at submit but red in COMBINATION on the mol tip is rolled back to
    the pre-merge sha and bounced to changes-requested — never closed, never left broken."""
    # submit stays green (validate_cmd "true"); only the merge-phase re-test goes red once the
    # second bead's file is on the tip — isolating the break to the combined integration tip.
    hive.cfg_path.write_text(
        CONFIG_YAML.replace(
            'validate_cmd: "true"',
            'validate_cmd: "true"\n  validation: conservative'
            '\n  validate: {merge: "test ! -f mr-1.2.txt"}',
        )
    )
    _mol_branch(hive, "mr-1")
    fakebd.seed("mr-1", title="epic")
    # first bead merges clean (its file alone keeps validate green)
    fakebd.seed("mr-1.1", title="t", parent="mr-1")
    work.claim(bead="mr-1.1", as_="", hive="myrepo")
    _commit(_wt_of(hive, "mr-1.1"), "feat: one", fname="mr-1.1.txt")
    work.submit(bead="mr-1.1", hive="myrepo")
    fakebd.approve("mr-1.1")
    work.merge(bead="mr-1.1", hive="myrepo", rm=False, molecule=False)
    assert fakebd.beads["mr-1.1"]["status"] == "closed"

    mol_before = _git("rev-parse", "wt/bead/epic/mr-1", cwd=hive.main).stdout.strip()

    # second bead is individually fine but turns the mol tip red (mr-1.2.txt now present)
    fakebd.seed("mr-1.2", title="t", parent="mr-1")
    work.claim(bead="mr-1.2", as_="", hive="myrepo")
    _commit(_wt_of(hive, "mr-1.2"), "feat: two", fname="mr-1.2.txt")
    work.submit(bead="mr-1.2", hive="myrepo")
    fakebd.approve("mr-1.2")

    with pytest.raises(typer.Exit):
        work.merge(bead="mr-1.2", hive="myrepo", rm=False, molecule=False)

    # mol tip rolled back to before the bad merge; bead bounced, not closed; slot released
    assert _git("rev-parse", "wt/bead/epic/mr-1", cwd=hive.main).stdout.strip() == mol_before
    assert fakebd.beads["mr-1.2"]["status"] != "closed"
    assert fakebd.states.get("mr-1.2", {}).get("review") == "changes-requested"
    assert fakebd.did("merge-slot", "release")


def test_merge_target_aware_command_main_vs_mol(hive, fakebd, monkeypatch):
    """The per-bead merge re-test resolves `merge-main` for an ad-hoc bead → main, and the plain
    `merge` for a molecule member → wt/bead/epic/<epic>."""
    hive.cfg_path.write_text(
        CONFIG_YAML.replace(
            'validate_cmd: "true"',
            'validate_cmd: "true"\n  validation: conservative'
            '\n  validate: {merge: "true # MOL", merge-main: "true # MAIN"}',
        )
    )
    seen = []
    real_cc = worktree.clean_checkout
    monkeypatch.setattr(
        worktree,
        "clean_checkout",
        lambda entry, branch, cmd: seen.append(cmd) or real_cc(entry, branch, cmd),
    )

    # ad-hoc bead (no '.') → base is main → merge-main
    fakebd.seed("mr-10", title="t")
    _take_to_approved(hive, fakebd, "mr-10")
    seen.clear()
    work.merge(bead="mr-10", hive="myrepo", rm=False, molecule=False)
    assert "true # MAIN" in seen and "true # MOL" not in seen

    # molecule member → base is wt/bead/epic/<epic> → plain merge
    _mol_branch(hive, "mr-2")
    fakebd.seed("mr-2", title="epic")
    fakebd.seed("mr-2.1", title="t", parent="mr-2")
    work.claim(bead="mr-2.1", as_="", hive="myrepo")
    _commit(_wt_of(hive, "mr-2.1"), "feat: a", fname="a.txt")
    work.submit(bead="mr-2.1", hive="myrepo")
    fakebd.approve("mr-2.1")
    seen.clear()
    work.merge(bead="mr-2.1", hive="myrepo", rm=False, molecule=False)
    assert "true # MOL" in seen and "true # MAIN" not in seen


def test_merge_adhoc_main_gate_fires_in_relaxed_and_rolls_back(hive, fakebd):
    """relaxed: an ad-hoc bead → main always gets the on_main re-validation; on red an unpushed main
    is rolled back to its pre-merge sha and the bead is bounced (no conservative mode needed)."""
    hive.cfg_path.write_text(
        CONFIG_YAML.replace(
            'validate_cmd: "true"',
            'validate_cmd: "true"\n  validate: {merge-main: "test ! -f mr-9.txt"}',
        )
    )
    main_before = _git("rev-parse", "main", cwd=hive.main).stdout.strip()
    fakebd.seed("mr-9", title="t")
    work.claim(bead="mr-9", as_="", hive="myrepo")
    _commit(_wt(hive, "mr-9"), "feat: nine", fname="mr-9.txt")  # submit green; merge-main red
    work.submit(bead="mr-9", hive="myrepo")
    fakebd.approve("mr-9")

    with pytest.raises(typer.Exit):
        work.merge(bead="mr-9", hive="myrepo", rm=False, molecule=False)

    # unpushed main rolled back to pre-merge; bead bounced, not closed; slot released
    assert _git("rev-parse", "main", cwd=hive.main).stdout.strip() == main_before
    assert fakebd.beads["mr-9"]["status"] != "closed"
    assert fakebd.states.get("mr-9", {}).get("review") == "changes-requested"
    assert fakebd.did("merge-slot", "release")


def test_merge_adhoc_main_gate_escalates_red_kept_on_pushed_main(hive, fakebd):
    """relaxed: an ad-hoc bead → a SHARED (pushed) main that goes red is NOT rewritten — the merge
    bubble stands, escalated for fix-forward; the bead is still bounced."""
    hive.cfg_path.write_text(
        CONFIG_YAML.replace(
            'validate_cmd: "true"',
            'validate_cmd: "true"\n  validate: {merge-main: "test ! -f mr-6.txt"}',
        )
    )
    _git(
        "push", "-u", "-q", "origin", "main", cwd=hive.main
    )  # main is now shared (has an upstream)
    fakebd.seed("mr-6", title="t")
    work.claim(bead="mr-6", as_="", hive="myrepo")
    _commit(
        _wt(hive, "mr-6"), "feat: six", fname="mr-6.txt"
    )  # submit green; merge-main red on main
    work.submit(bead="mr-6", hive="myrepo")
    fakebd.approve("mr-6")

    with pytest.raises(typer.Exit):
        work.merge(bead="mr-6", hive="myrepo", rm=False, molecule=False)

    # pushed main NOT rewritten — the bubble stands; bead bounced, not closed
    assert (
        _git("log", "-1", "--format=%s", cwd=hive.main).stdout.strip() == "chore(merge): bead mr-6"
    )
    assert fakebd.beads["mr-6"]["status"] != "closed"
    assert fakebd.states.get("mr-6", {}).get("review") == "changes-requested"


def test_merge_adhoc_main_gate_skipped_under_loose(hive, fakebd, monkeypatch):
    """loose trusts submits and skips main-gate checks — an ad-hoc bead → main does NO post-merge
    re-validation (consistent with loose skipping the molecule pre-land gate)."""
    hive.cfg_path.write_text(
        CONFIG_YAML.replace('validate_cmd: "true"', 'validate_cmd: "true"\n  validation: loose')
    )
    seen = []
    real_cc = worktree.clean_checkout
    monkeypatch.setattr(
        worktree,
        "clean_checkout",
        lambda entry, branch, cmd: seen.append(branch) or real_cc(entry, branch, cmd),
    )
    fakebd.seed("mr-8", title="t")
    _take_to_approved(hive, fakebd, "mr-8")
    seen.clear()  # ignore submit's clean_checkout
    work.merge(bead="mr-8", hive="myrepo", rm=False, molecule=False)
    assert seen == []  # loose: no post-merge re-validation, even for an ad-hoc → main land
    assert fakebd.beads["mr-8"]["status"] == "closed"


def test_merge_mol_member_relaxed_runs_no_post_merge_validation(hive, fakebd, monkeypatch):
    """No regression: in relaxed, a bead merging into its container gets NO post-merge re-test
    (on_main is false for a mol target); the mol→main land is its backstop."""
    seen = []
    real_cc = worktree.clean_checkout
    monkeypatch.setattr(
        worktree,
        "clean_checkout",
        lambda entry, branch, cmd: seen.append(branch) or real_cc(entry, branch, cmd),
    )
    _mol_branch(hive, "mr-7")
    fakebd.seed("mr-7", title="epic")
    fakebd.seed("mr-7.1", title="t", parent="mr-7")
    work.claim(bead="mr-7.1", as_="", hive="myrepo")
    _commit(_wt_of(hive, "mr-7.1"), "feat: x", fname="x.txt")
    work.submit(bead="mr-7.1", hive="myrepo")
    fakebd.approve("mr-7.1")
    seen.clear()  # ignore submit's clean_checkout
    work.merge(bead="mr-7.1", hive="myrepo", rm=False, molecule=False)
    assert seen == []  # a bead → wt/bead/epic/<epic> in relaxed does no post-merge re-validation
    assert fakebd.beads["mr-7.1"]["status"] == "closed"


def test_merge_molecule_refuses_open_child(hive, fakebd):
    """An incomplete molecule (a child still open) is refused before any merge — never drops work:
    main untouched, epic still open, molecule branch intact, no slot acquired."""
    _mol_branch(hive, "mr-1")
    fakebd.seed("mr-1", title="epic")
    fakebd.seed("mr-1.1", title="t", parent="mr-1", status="closed")
    fakebd.seed("mr-1.2", title="t", parent="mr-1")  # still open
    main_before = _git("rev-parse", "main", cwd=hive.main).stdout.strip()

    with pytest.raises(typer.Exit):
        work.merge(bead="mr-1", hive="myrepo", molecule=True)

    assert _git("rev-parse", "main", cwd=hive.main).stdout.strip() == main_before
    assert fakebd.beads["mr-1"]["status"] != "closed"
    assert worktree._branch_exists(hive.main, "wt/bead/epic/mr-1")
    assert not fakebd.did("merge-slot", "acquire")


def test_merge_molecule_lands_and_auto_closes_adopted_origin_report(hive, fakebd):
    """Regression: an adopted origin report linked child-of the epic is
    PROVENANCE, not molecule work. It must NOT gate the land while still open, and it auto-closes
    WITH the molecule — so a report->promote->adopt->file->finish loop can actually land."""
    _land_two_bead_molecule(hive, fakebd, "mr-1")
    # An open origin report hangs off the epic as an adopted-provenance child (still open on land).
    fakebd.seed("mr-1.rpt", title="origin report", parent="mr-1", labels=["intake:promoted"])
    assert fakebd.beads["mr-1.rpt"]["status"] == "open"

    work.merge(bead="mr-1", hive="myrepo", molecule=True)  # must NOT be refused by the open report

    # the molecule landed and the epic closed despite the still-open report at check time
    assert (
        _git("log", "-1", "--format=%s", cwd=hive.main).stdout.strip()
        == "chore(merge): molecule mr-1"
    )
    assert fakebd.beads["mr-1"]["status"] == "closed"
    # the origin report auto-closed on land (rides the epic to completion — the jf5k/jey0 intent)
    assert fakebd.beads["mr-1.rpt"]["status"] == "closed"
    assert fakebd.did("close", "mr-1.rpt", "--reason", "adopted epic mr-1 landed")


# ---- resume ----------------------------------------------------------------


def test_resume_reprovisions_after_worktree_removed(hive, fakebd):
    fakebd.seed("mr-6", title="t")
    work.claim(bead="mr-6", as_="", hive="myrepo")
    _commit(_wt(hive, "mr-6"), "feat: x")
    work.submit(bead="mr-6", hive="myrepo")
    # review came back rejected; the worktree directory was reclaimed
    fakebd.states["mr-6"]["review"] = "changes-requested"
    _git("worktree", "remove", "--force", str(_wt(hive, "mr-6")), cwd=hive.main)
    assert not _wt(hive, "mr-6").exists()

    work.resume(bead="mr-6", as_="", hive="myrepo")
    wt = _wt(hive, "mr-6")
    assert wt.exists()
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt).stdout.strip() == "wt/bead/issue/mr-6"


def test_resume_refuses_wrong_state(hive, fakebd):
    fakebd.seed("mr-6", title="t")
    work.claim(bead="mr-6", as_="", hive="myrepo")
    with pytest.raises(typer.Exit):  # not changes-requested
        work.resume(bead="mr-6", as_="", hive="myrepo")


# ---- abandon ---------------------------------------------------------------


def test_abandon_rm_removes_worktree(hive, fakebd):
    fakebd.seed("mr-7", title="t")
    work.claim(bead="mr-7", as_="", hive="myrepo")
    assert _wt(hive, "mr-7").exists()
    work.abandon(bead="mr-7", hive="myrepo", rm=True)
    assert not _wt(hive, "mr-7").exists()
    assert fakebd.states["mr-7"]["review"] == "abandoned"
    assert fakebd.beads["mr-7"]["assignee"] == ""


# ---- lifecycle transitions (assigned / claimed / abandoned) -----------------
#
# Complete the ws.work.bead.transitions counter: assign/claim/abandon were the holes (merged /
# molecule_landed / review_pending already fired). With otel on (mocked meter) each verb bumps the
# counter with its transition value; off, the verbs run unchanged and create no instrument.


def test_assign_claim_abandon_emit_lifecycle_transitions(hive, fakebd, monkeypatch):
    meter = MagicMock(name="meter")
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(otel, "get_tracer", lambda *a, **k: MagicMock())
    monkeypatch.setattr(otel, "get_meter", lambda *a, **k: meter)
    otel._instruments.clear()

    fakebd.seed("mr-20", title="t")
    work.assign(bead="mr-20", to="dev/carol", as_="disp/lead", hive="myrepo")
    work.claim(bead="mr-20", as_="dev/carol", hive="myrepo")
    work.abandon(bead="mr-20", hive="myrepo", rm=False)

    # All counters share one mocked instrument, so filter the bead transitions out of the
    # interleaved worktree-event adds by their transition key (the bead id is no longer a metric
    # attr — it rides the verb span via set_bead).
    adds = meter.create_counter.return_value.add.call_args_list
    transitions = [
        c.args[1]["bh.bead.transition"] for c in adds if "bh.bead.transition" in c.args[1]
    ]
    assert transitions == ["assigned", "claimed", "abandoned"]
    assert not any("bh.bead" in c.args[1] for c in adds)  # bead id never on a metric point
    otel._instruments.clear()  # don't leak mocked instruments into later tests


def test_lifecycle_transitions_are_noop_when_otel_off(hive, fakebd):
    # Default/off path: the verbs run unchanged and cache no instrument (zero-cost no-op).
    otel._instruments.clear()
    fakebd.seed("mr-21", title="t")
    work.assign(bead="mr-21", to="dev/carol", as_="disp/lead", hive="myrepo")
    work.claim(bead="mr-21", as_="dev/carol", hive="myrepo")
    work.abandon(bead="mr-21", hive="myrepo", rm=False)
    assert fakebd.beads["mr-21"]["status"] == "open"  # abandon reopened it — behavior intact
    assert otel._instruments == {}  # nothing cached on the off-path


# ---- worktree lifecycle events (ws.worktree.events) -------------------------
#
# create (worktree.add → _do_add chokepoint) / remove / prune each emit a ws.worktree.events
# counter tagged op + outcome + bh.hive/bh.worktree; off, they emit nothing. The ephemeral verify-
# clean-checkout worktrees (not a seat) are excluded.


def test_worktree_create_remove_prune_emit_events_when_on(hive, fakebd, monkeypatch):
    events = []
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(
        otel,
        "record_worktree_event",
        lambda op, outcome="ok", attrs=None: events.append((op, attrs)),
    )

    worktree.add(hive="myrepo", bead="wt-1")
    worktree.remove("myrepo", "wt-1", force=True)
    worktree.add(hive="myrepo", bead="wt-2")
    # Seed wt-2 as a closed bead so the classifier marks it SAFE (closed + merged at
    # main's tip + clean working tree) and prune removes it with a telemetry event.
    fakebd.seed("wt-2", status="closed")
    worktree.prune(hive="myrepo")

    assert [op for op, _ in events] == ["create", "remove", "create", "prune"]
    assert all(a.get("bh.hive") == "mr" for _, a in events)  # hive tagged on every event
    assert events[0][1]["bh.worktree"] == "wt-1"  # create tags the leaf
    assert events[1][1]["bh.worktree"] == "wt-1"  # remove tags the leaf
    assert events[3][1]["bh.worktree"] == "wt-2"  # prune tags the leaf


def test_worktree_events_are_noop_when_otel_off(hive, fakebd, monkeypatch):
    monkeypatch.setattr(
        otel, "record_worktree_event", MagicMock(side_effect=AssertionError("no event when off"))
    )
    # Off by default: the create/remove/prune seams must never reach the emitter.
    worktree.add(hive="myrepo", bead="wt-3")
    worktree.remove("myrepo", "wt-3", force=True)
    worktree.prune(hive="myrepo")  # reached here → off-path emitted nothing


def test_record_wt_event_excludes_verify_leaf(monkeypatch):
    monkeypatch.setattr(otel, "_initialized", True)
    calls = []
    monkeypatch.setattr(otel, "record_worktree_event", lambda *a, **k: calls.append((a, k)))
    worktree._record_wt_event("prune", hive="mr", leaf="verify-ag-1")
    assert calls == []  # ephemeral verify- clean-checkout worktree is not a seat → no event
    worktree._record_wt_event("prune", hive="mr", leaf="ag-1")
    assert len(calls) == 1  # a real seat emits


def test_record_wt_event_never_raises_on_emitter_failure(monkeypatch):
    # Best-effort: a telemetry failure must never propagate out and block the worktree op.
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(
        otel, "record_worktree_event", MagicMock(side_effect=RuntimeError("exporter down"))
    )
    worktree._record_wt_event("create", hive="mr", leaf="ag-1")  # must not raise


# ---- worktree op duration + real error outcomes (hqfy.3) -------------------


def test_worktree_create_remove_prune_emit_op_duration_when_on(hive, fakebd, monkeypatch):
    """create/remove/prune each emit ws.worktree.op.duration tagged op + outcome=ok + bh.hive."""
    durations = []
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(
        otel, "record_worktree_op_duration", lambda seconds, attrs=None: durations.append(attrs)
    )

    worktree.add(hive="myrepo", bead="wt-1")
    worktree.remove("myrepo", "wt-1", force=True)
    worktree.add(hive="myrepo", bead="wt-2")
    # Seed wt-2 as closed so it classifies SAFE and prune emits a duration record.
    fakebd.seed("wt-2", status="closed")
    worktree.prune(hive="myrepo")

    assert [a["bh.worktree.op"] for a in durations] == ["create", "remove", "create", "prune"]
    assert all(a["bh.worktree.outcome"] == "ok" for a in durations)
    assert all(a.get("bh.hive") == "mr" for a in durations)
    assert durations[0]["bh.worktree"] == "wt-1"  # leaf tagged like the events counter


def test_worktree_op_duration_noop_when_off(hive, fakebd, monkeypatch):
    monkeypatch.setattr(
        otel,
        "record_worktree_op_duration",
        MagicMock(side_effect=AssertionError("no duration when off")),
    )
    worktree.add(hive="myrepo", bead="wt-3")  # off by default → the seam never reaches the emitter
    worktree.remove("myrepo", "wt-3", force=True)
    worktree.prune(hive="myrepo")


def test_worktree_create_failure_records_error_then_reraises(hive, fakebd, monkeypatch):
    """The always-ok gap closed: a failing `git worktree add` records the events counter AND the
    op.duration histogram with outcome=error BEFORE re-raising (previously it emitted nothing)."""
    events, durations = [], []
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(
        otel,
        "record_worktree_event",
        lambda op, outcome="ok", attrs=None: events.append((op, outcome, attrs)),
    )
    monkeypatch.setattr(
        otel, "record_worktree_op_duration", lambda seconds, attrs=None: durations.append(attrs)
    )
    real = worktree._run_git

    def fake(args, **kw):
        if "worktree" in args and "add" in args:
            return _CP(1, "", "boom")  # force the create subprocess to fail
        return real(args, **kw)

    monkeypatch.setattr(worktree, "_run_git", fake)

    with pytest.raises(typer.Exit):
        worktree.add(hive="myrepo", bead="wt-err")

    assert events == [("create", "error", {"bh.hive": "mr", "bh.worktree": "wt-err"})]
    assert durations == [
        {
            "bh.worktree.op": "create",
            "bh.worktree.outcome": "error",
            "bh.hive": "mr",
            "bh.worktree": "wt-err",
        }
    ]


def test_record_wt_op_duration_excludes_verify_leaf(monkeypatch):
    monkeypatch.setattr(otel, "_initialized", True)
    calls = []
    monkeypatch.setattr(otel, "record_worktree_op_duration", lambda *a, **k: calls.append((a, k)))
    worktree._record_wt_op_duration("create", 0.1, hive="mr", leaf="verify-ag-1")
    assert calls == []  # ephemeral verify- clean-checkout worktree is not a seat → no duration
    worktree._record_wt_op_duration("create", 0.1, hive="mr", leaf="ag-1")
    assert len(calls) == 1


def test_record_wt_op_duration_never_raises_on_emitter_failure(monkeypatch):
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(
        otel,
        "record_worktree_op_duration",
        MagicMock(side_effect=RuntimeError("exporter down")),
    )
    worktree._record_wt_op_duration("create", 0.1, hive="mr", leaf="ag-1")  # must not raise


# ---- worktree path/rm --bead (Fix 2) ---------------------------------------


def test_worktree_path_and_rm_accept_bead(hive, fakebd, capsys):
    from beadhive import cli

    fakebd.seed("mr-1", title="t")
    work.claim(bead="mr-1", as_="", hive="myrepo")
    wt = _wt(hive, "mr-1")

    cli.wt_path(ref="", bead="mr-1", hive="myrepo")  # resolve by --bead
    assert str(wt) in capsys.readouterr().out

    with pytest.raises(typer.Exit):  # neither ref nor --bead
        cli.wt_path(ref="", bead="", hive="myrepo")

    cli.wt_rm(ref="", bead="mr-1", hive="myrepo", force=True)  # remove by --bead
    assert not wt.exists()


# ---- union conflict resolution tier ----------------------------------------
#
# Two beads each write different content to the same whitelisted file from an empty base.
# The second bead's plain merge AND its rebase-retry both conflict — the union tier then
# resolves it by keeping both sides, and the success message surfaces how="union".
# Without union_globs configured, the same real-conflict scenario fails cleanly (unchanged
# behavior exercised by the existing divergent-conflict test above).


def test_merge_via_union_tier_when_configured(hive, fakebd, capsys):
    """With union_globs matching the conflicted file, the second bead lands via the union tier:
    both beads' content is present, the bead is closed, and the success message mentions union."""
    hive.cfg_path.write_text(CONFIG_YAML_WITH_UNION)
    # seed an empty notes.txt on the integration branch so both beads start from the same base
    (hive.main / "notes.txt").write_text("")
    _git("add", "-A", cwd=hive.main)
    _git("commit", "-qm", "chore: add notes.txt", cwd=hive.main)
    fakebd.seed("mr-40", title="t")
    fakebd.seed("mr-41", title="t")
    work.claim(bead="mr-40", as_="", hive="myrepo")
    work.claim(bead="mr-41", as_="", hive="myrepo")
    # each bead writes a different line to notes.txt from an empty base → add-add conflict
    _set_line(_wt(hive, "mr-40"), "noteA\n", fname="notes.txt")
    _set_line(_wt(hive, "mr-41"), "noteB\n", fname="notes.txt")
    work.submit(bead="mr-40", hive="myrepo")
    fakebd.approve("mr-40")
    work.submit(bead="mr-41", hive="myrepo")
    fakebd.approve("mr-41")

    work.merge(bead="mr-40", hive="myrepo", rm=False, molecule=False)
    capsys.readouterr()  # drain mr-40 output
    work.merge(bead="mr-41", hive="myrepo", rm=False, molecule=False)

    out = capsys.readouterr().out
    content = (hive.main / "notes.txt").read_text()
    assert "noteA" in content  # first bead's content preserved
    assert "noteB" in content  # second bead's content landed via union
    assert "union" in out  # success message reflects how="union"
    assert fakebd.beads["mr-41"]["status"] == "closed"
    assert fakebd.did("merge-slot", "release")


def test_merge_no_union_note_when_clean(hive, fakebd, capsys):
    """Without union_globs configured, a clean merge emits no union note in the output."""
    fakebd.seed("mr-42", title="t")
    _take_to_approved(hive, fakebd, "mr-42")
    capsys.readouterr()
    work.merge(bead="mr-42", hive="myrepo", rm=False, molecule=False)
    out = capsys.readouterr().out
    assert "union" not in out
    assert "merged mr-42" in out


# ---- work groups (batch mechanics) -----------------------------------------
#
# A batch = several beads sharing a `batch:<group>` label, handled by ONE agent in ONE shared
# `wt/batch/<group>` worktree, validated + merged ONCE as a single --no-ff bubble (per-bead
# commits preserved inside). `--group <ids>` reads the members' existing labels (8v8.1 data
# model) to resolve the group name. Single-bead behaviour (everything above) stays the default.


def _batch_wt(hive, group):
    """The shared batch worktree dir for a group. The leaf carries a `batch-` prefix so the batch
    worktree never collides with a bead worktree sharing the group name — notably the epic seat
    wt/bead/epic/<epic> in collapsed mode."""
    return hive.wts / "github" / "myorg" / "myrepo" / ("batch-" + registry.sanitize(group))


def test_claim_group_provisions_one_shared_worktree_and_claims_all(hive, fakebd):
    """Group claim provisions the single wt/batch/<group> worktree (one identity), claims every
    member, and creates NO per-bead worktrees — one agent owns the whole batch."""
    fakebd.seed("mr-1.1", title="a", labels=["batch:samefile"])
    fakebd.seed("mr-1.2", title="b", labels=["batch:samefile"])

    work.claim(bead="", as_="dev/group", group="mr-1.1,mr-1.2", hive="myrepo")

    wt = _batch_wt(hive, "samefile")
    assert wt.exists()
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt).stdout.strip() == "wt/batch/samefile"
    assert _cfg_get(wt, "user.name") == "dev/group"  # one shared identity for the group
    # every member claimed by the one actor → in_progress
    assert fakebd.beads["mr-1.1"]["status"] == "in_progress"
    assert fakebd.beads["mr-1.2"]["status"] == "in_progress"
    assert ("dev/group", ["update", "mr-1.1", "--claim"]) in fakebd.calls
    assert ("dev/group", ["update", "mr-1.2", "--claim"]) in fakebd.calls
    # opt-in: NO per-bead worktrees were created (the whole point of batching)
    assert not _wt_of(hive, "mr-1.1").exists()
    assert not _wt_of(hive, "mr-1.2").exists()


def test_claim_group_refuses_member_without_batch_label(hive, fakebd):
    """A member lacking a batch:<group> label isn't a runnable unit — refuse before provisioning."""
    fakebd.seed("mr-1.1", title="a", labels=["batch:samefile"])
    fakebd.seed("mr-1.2", title="b")  # no batch label
    with pytest.raises(typer.Exit):
        work.claim(bead="", as_="", group="mr-1.1,mr-1.2", hive="myrepo")
    assert not _batch_wt(hive, "samefile").exists()  # refused before any worktree
    assert not fakebd.did("update", "mr-1.1", "--claim")  # no member claimed


def test_claim_group_refuses_mixed_groups(hive, fakebd):
    """Members spanning two batch groups can't share one worktree — refuse."""
    fakebd.seed("mr-1.1", title="a", labels=["batch:alpha"])
    fakebd.seed("mr-1.2", title="b", labels=["batch:beta"])
    with pytest.raises(typer.Exit):
        work.claim(bead="", as_="", group="mr-1.1,mr-1.2", hive="myrepo")


def test_claim_refuses_bead_and_group_together(hive, fakebd):
    fakebd.seed("mr-1.1", title="a", labels=["batch:samefile"])
    with pytest.raises(typer.Exit):
        work.claim(bead="mr-1.1", as_="", group="mr-1.1", hive="myrepo")


def test_claim_collapse_synthesizes_batch_label_on_unbatched_children(hive, fakebd):
    """Collapsed claim of an epic whose ready children carry NO planner-authored batch: label:
    the pre-step stamps a synthetic batch:<epic> on each, making resolve_group's precondition
    true, so claim_group then succeeds — one shared wt/batch/<epic> worktree, all claimed."""
    fakebd.seed("mr-1.1", title="a", parent="mr-1")  # no batch label (un-batched by the planner)
    fakebd.seed("mr-1.2", title="b", parent="mr-1")

    work.claim(bead="", as_="dev/group", collapse="mr-1", hive="myrepo")

    # synthetic label stamped on every ready child, additively (nothing removed)
    assert "batch:mr-1" in fakebd.beads["mr-1.1"]["labels"]
    assert "batch:mr-1" in fakebd.beads["mr-1.2"]["labels"]
    assert fakebd.did("label", "add", "mr-1.1", "batch:mr-1")
    # claim_group succeeded afterward: one shared worktree, every member claimed by the one actor
    wt = _batch_wt(hive, "mr-1")
    assert wt.exists()
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt).stdout.strip() == "wt/batch/mr-1"
    assert fakebd.beads["mr-1.1"]["status"] == "in_progress"
    assert fakebd.beads["mr-1.2"]["status"] == "in_progress"
    assert not _wt_of(hive, "mr-1.1").exists()  # collapsed: no per-bead worktrees


def test_claim_collapse_lands_commits_on_batch_worktree_not_coordinator_seat(hive, fakebd):
    """Regression: with the coordinator SEAT worktree already provisioned on
    wt/bead/epic/<epic>, a collapsed claim must give the group its OWN wt/batch/<epic> worktree in
    a DISTINCT dir — not silently reuse the seat dir (they share the bare-<epic> leaf). A commit in
    the batch worktree must land on wt/batch/<epic>, leaving the seat branch untouched."""
    fakebd.seed("mr-1", title="e", issue_type="epic")
    fakebd.states["mr-1"] = {"kickoff": "approved"}
    fakebd.seed("mr-1.1", title="a", parent="mr-1")  # un-batched ready children
    fakebd.seed("mr-1.2", title="b", parent="mr-1")

    # coordinator takes the epic seat FIRST — its worktree occupies leaf `mr-1`
    work.start(epic="mr-1", as_="disp/lead", hive="myrepo")
    seat = worktree.locate(config.load(), "myrepo", "mr-1", kind="epic")[2]
    assert seat.exists()
    seat_tip_before = _git("rev-parse", "wt/bead/epic/mr-1", cwd=hive.main).stdout.strip()

    # collapsed claim from the same context: must NOT reuse the seat dir/branch
    work.claim(bead="", as_="dev/group", collapse="mr-1", hive="myrepo")

    batch_wt = _batch_wt(hive, "mr-1")
    assert batch_wt.exists()
    assert batch_wt.resolve() != seat.resolve()  # a distinct directory, not the seat
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=batch_wt).stdout.strip() == "wt/batch/mr-1"
    # the seat is untouched: still on its own branch, still its own dir
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=seat).stdout.strip() == "wt/bead/epic/mr-1"

    # a commit in the batch worktree lands on wt/batch/mr-1 — NOT the coordinator seat branch
    _commit(batch_wt, "feat: mr-1.1 work", fname="a.txt")
    assert (
        _git("log", "-1", "--format=%s", cwd=batch_wt).stdout.strip() == "feat: mr-1.1 work"
    )  # commit visible on the batch branch
    assert (
        _git("rev-parse", "wt/bead/epic/mr-1", cwd=hive.main).stdout.strip() == seat_tip_before
    )  # seat/container branch never advanced


def test_claim_group_refuses_when_worktree_is_wrong_branch(hive, fakebd, monkeypatch):
    """Defense-in-depth: if provisioning ever resolves the batch worktree onto a
    dir checked out on a different branch, claim_group hard-fails (non-zero) rather than stamping +
    claiming into the wrong tree — a collapsed seat can never silently commit on a wrong branch."""
    fakebd.seed("mr-1.1", title="a", labels=["batch:samefile"])
    fakebd.seed("mr-1.2", title="b", labels=["batch:samefile"])

    real_ensure = worktree.ensure

    def _wrong_branch_ensure(cfg, hive, **kw):
        entry, target, _branch = real_ensure(cfg, hive, **kw)
        return entry, target, "wt/bead/epic/mr-1"  # pretend it resolved onto the seat branch

    monkeypatch.setattr(worktree, "ensure", _wrong_branch_ensure)
    with pytest.raises(typer.Exit):
        work.claim(bead="", as_="dev/group", group="mr-1.1,mr-1.2", hive="myrepo")
    assert not fakebd.did("update", "mr-1.1", "--claim")  # refused before any member claimed


def test_merge_group_empty_batch_gives_actionable_wrong_branch_hint(hive, fakebd, capsys):
    """merge --group on a batch branch with no delta must distinguish 'work landed on the wrong
    branch' from a genuinely empty group, pointing at the recovery path."""
    _mol_branch(hive, "mr-1")
    fakebd.seed("mr-1.1", title="a", parent="mr-1", labels=["batch:samefile"])
    fakebd.seed("mr-1.2", title="b", parent="mr-1", labels=["batch:samefile"])
    work.claim(bead="", as_="", group="mr-1.1,mr-1.2", hive="myrepo")  # claimed; nothing committed

    with pytest.raises(typer.Exit):
        work.merge(bead="", group="mr-1.1,mr-1.2", hive="myrepo")

    err = capsys.readouterr().err
    assert "no commits" in err and "wrong branch" in err  # actionable, not the generic submit msg
    assert fakebd.beads["mr-1.1"]["status"] != "closed"  # nothing landed / closed


def test_claim_collapse_preserves_existing_planner_batch_label(hive, fakebd):
    """The stamping is read-only w.r.t. existing planner labels: a child the planner already
    batched keeps its own label and is not re-stamped with batch:<epic>."""
    fakebd.seed("mr-1.1", title="a", parent="mr-1", labels=["batch:planner"])
    fakebd.seed("mr-1.2", title="b", parent="mr-1", labels=["batch:planner"])

    work.claim(bead="", as_="dev/group", collapse="mr-1", hive="myrepo")

    assert fakebd.beads["mr-1.1"]["labels"] == ["batch:planner"]  # untouched
    assert not fakebd.did("label", "add", "mr-1.1", "batch:mr-1")
    assert _batch_wt(hive, "planner").exists()  # claimed under the planner's group


def _claim_and_commit_batch(hive, fakebd, group="samefile", epic="mr-1"):
    """Kick off the container, claim a two-member batch, and lay down one conventional commit per
    bead in the shared batch worktree. Returns the batch worktree path."""
    _mol_branch(hive, epic)
    fakebd.seed(f"{epic}.1", title="a", parent=epic, labels=[f"batch:{group}"])
    fakebd.seed(f"{epic}.2", title="b", parent=epic, labels=[f"batch:{group}"])
    work.claim(bead="", as_="", group=f"{epic}.1,{epic}.2", hive="myrepo")
    wt = _batch_wt(hive, group)
    _commit(wt, f"feat: {epic}.1 work", fname="a.txt")
    _commit(wt, f"feat: {epic}.2 work", fname="b.txt")
    return wt


def test_merge_group_lands_one_bubble_with_per_bead_commits_and_closes_all(hive, fakebd):
    """merge --group validates once, lands ONE --no-ff bubble into the molecule (per-bead commits
    preserved inside → bisectable), closes every member, and leaves the integration branch alone."""
    _claim_and_commit_batch(hive, fakebd)
    main_before = _git("rev-parse", "main", cwd=hive.main).stdout.strip()

    work.merge(bead="", group="mr-1.1,mr-1.2", hive="myrepo")

    # ONE --no-ff bubble on the molecule branch, subject "chore(merge): batch <group>"
    assert _git("log", "-1", "--format=%s", "wt/bead/epic/mr-1", cwd=hive.main).stdout.strip() == (
        "chore(merge): batch samefile"
    )
    parents = _git(
        "rev-list", "--parents", "-n", "1", "wt/bead/epic/mr-1", cwd=hive.main
    ).stdout.split()
    assert len(parents) == 3  # merge commit + two parents
    # per-bead commits live INSIDE the one bubble (lossless / bisectable)
    subjects = _git("log", "--format=%s", "wt/bead/epic/mr-1", cwd=hive.main).stdout.split("\n")
    assert "feat: mr-1.1 work" in subjects and "feat: mr-1.2 work" in subjects
    # both members' changes landed
    assert _git("cat-file", "-e", "wt/bead/epic/mr-1:a.txt", cwd=hive.main).returncode == 0
    assert _git("cat-file", "-e", "wt/bead/epic/mr-1:b.txt", cwd=hive.main).returncode == 0
    # every member closed (with the batch reason), integration branch untouched, slot released
    assert fakebd.beads["mr-1.1"]["status"] == "closed"
    assert fakebd.beads["mr-1.2"]["status"] == "closed"
    assert fakebd.did("close", "mr-1.1", "--reason", "merged in batch samefile")
    assert fakebd.did("close", "mr-1.2", "--reason", "merged in batch samefile")
    assert _git("rev-parse", "main", cwd=hive.main).stdout.strip() == main_before
    assert fakebd.did("merge-slot", "acquire") and fakebd.did("merge-slot", "release")


def test_merge_group_relaxed_budget_admits_cohesive_batch(hive, fakebd, monkeypatch):
    """The history budget for a batch is per-bead-commits × members, not the flat single-bead cap:
    with max_commits pinned to 1, a 2-commit batch (which the flat cap would reject) still lands."""
    # the flat single-bead cap (1) rejects the same 2-commit history the relaxed cap (1×2) admits
    assert not work._history_ok(2, ["feat: one", "feat: two"], 1)[0]
    assert work._history_ok(2, ["feat: one", "feat: two"], 2)[0]

    monkeypatch.setattr(config, "max_commits", lambda cfg, entry: 1)
    _claim_and_commit_batch(hive, fakebd)  # two per-bead commits on the batch branch

    work.merge(bead="", group="mr-1.1,mr-1.2", hive="myrepo")  # raises if the cap weren't relaxed

    assert fakebd.beads["mr-1.1"]["status"] == "closed"
    assert fakebd.beads["mr-1.2"]["status"] == "closed"


def test_merge_group_refuses_open_gate_and_drops_nothing(hive, fakebd):
    """If any member's review gate is still open the batch isn't approved — refuse, leaving the
    molecule untouched and no member closed."""
    _claim_and_commit_batch(hive, fakebd)
    fakebd.gates.append({"id": "g0", "status": "open", "description": "blocks mr-1.2"})
    before = _git("rev-parse", "wt/bead/epic/mr-1", cwd=hive.main).stdout.strip()

    with pytest.raises(typer.Exit):
        work.merge(bead="", group="mr-1.1,mr-1.2", hive="myrepo")

    assert _git("rev-parse", "wt/bead/epic/mr-1", cwd=hive.main).stdout.strip() == before
    assert fakebd.beads["mr-1.1"]["status"] != "closed"
    assert fakebd.beads["mr-1.2"]["status"] != "closed"


def test_merge_group_rm_removes_shared_worktree(hive, fakebd):
    _claim_and_commit_batch(hive, fakebd)
    assert _batch_wt(hive, "samefile").exists()
    work.merge(bead="", group="mr-1.1,mr-1.2", hive="myrepo", rm=True)
    assert not _batch_wt(hive, "samefile").exists()


# ---- review (merger/reviewer walkthrough packet) ---------------------------


def test_review_molecule_aggregates_intent_and_change(hive, fakebd, capsys):
    """Molecule review: epic brief + every child's acceptance + the container change vs main."""
    _land_two_bead_molecule(hive, fakebd, "mr-1")
    fakebd.beads["mr-1"]["title"] = "the epic"
    fakebd.beads["mr-1.1"]["acceptance_criteria"] = "accept one"
    fakebd.beads["mr-1.2"]["acceptance_criteria"] = "accept two"

    work.review(bead="mr-1", run_validate=False, demo=False, view=["stat"], hive="myrepo")
    out = capsys.readouterr().out

    assert "# mr-1  the epic" in out
    assert "## Molecule children (2)" in out
    assert "accept one" in out and "accept two" in out
    assert "## Change (wt/bead/epic/mr-1 vs main)" in out
    assert "change.txt" in out  # the child merges show up in the stat view


def test_review_bead_mode_shows_brief_and_change(hive, fakebd, capsys):
    """A bead with no wt/bead/epic/<id> branch reviews wt/bead/<id> against the integration base."""
    fakebd.seed("mr-5", title="lone bead", description="do the thing")
    work.claim(bead="mr-5", as_="", hive="myrepo")
    _commit(_wt_of(hive, "mr-5"), "feat: mr-5 work")

    work.review(bead="mr-5", run_validate=False, demo=False, view=["log"], hive="myrepo")
    out = capsys.readouterr().out

    assert "# mr-5  lone bead" in out
    assert "do the thing" in out
    assert "feat: mr-5 work" in out


def test_review_run_reports_validate_exit(hive, fakebd, capsys):
    fakebd.seed("mr-5", title="t")
    work.claim(bead="mr-5", as_="", hive="myrepo")
    _commit(_wt_of(hive, "mr-5"), "feat: mr-5")

    work.review(bead="mr-5", run_validate=True, demo=False, view=["log"], hive="myrepo")
    out = capsys.readouterr().out
    assert "## Validation (true)" in out  # CONFIG_YAML validate_cmd
    assert "validate exit 0" in out


def test_review_demo_none_then_runs_when_configured(hive, fakebd, capsys):
    fakebd.seed("mr-5", title="t")
    work.claim(bead="mr-5", as_="", hive="myrepo")
    _commit(_wt_of(hive, "mr-5"), "feat: mr-5")

    # CONFIG_YAML has no demo_cmd → review --demo says so
    work.review(bead="mr-5", run_validate=False, demo=True, view=["log"], hive="myrepo")
    assert "no demo_cmd configured" in capsys.readouterr().out

    # configure demo_cmd → review --demo runs it from a clean checkout
    hive.cfg_path.write_text(
        CONFIG_YAML.replace('validate_cmd: "true"', 'validate_cmd: "true"\n  demo_cmd: "true"')
    )
    work.review(bead="mr-5", run_validate=False, demo=True, view=["log"], hive="myrepo")
    out = capsys.readouterr().out
    assert "## Demo (true)" in out and "demo exit 0" in out


# ---- ws work schedule: work.dispatch.mode wiring (fanout | collapsed | auto) ----------------


def _dispatch_cfg(mode, *, auto_budget=None):
    """CONFIG_YAML with a `work.dispatch` block (mode + optional auto_budget)."""
    lines = ["  dispatch:", f"    mode: {mode}"]
    if auto_budget is not None:
        lines.append(f"    auto_budget: {auto_budget}")
    block = "\n".join(lines)
    return CONFIG_YAML.replace('review_gate: "human"', f'review_gate: "human"\n{block}')


def _seed_child(fakebd, bead_id, *, labels=None):
    fakebd.seed(bead_id, title=bead_id, parent="mr-epic", labels=list(labels or []))


def test_schedule_fanout_mode_is_the_default_and_fans_out(hive, fakebd, capsys):
    # No dispatch block → mode fanout: independent beads stay singletons, no groups.
    _seed_child(fakebd, "mr-1")
    _seed_child(fakebd, "mr-2")
    work.schedule(epic="mr-epic", hive="myrepo", as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["groups"] == []
    assert sorted(payload["singletons"]) == ["mr-1", "mr-2"]


def test_schedule_skips_batch_whose_group_branch_already_merged(hive, fakebd, capsys):
    # bh-bfoy: a stale batch: label whose wt/batch/<group> branch already merged must NOT be
    # resurrected as a batch — schedule leaves its members as ordinary singletons.
    _seed_child(fakebd, "mr-1", labels=["batch:dead"])
    _seed_child(fakebd, "mr-2", labels=["batch:dead"])
    _git("branch", "wt/batch/dead", "main", cwd=hive.main)  # branch is an ancestor of main → merged

    work.schedule(epic="mr-epic", hive="myrepo", as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["groups"] == []
    assert sorted(payload["singletons"]) == ["mr-1", "mr-2"]


def test_schedule_honors_batch_when_group_branch_not_merged(hive, fakebd, capsys):
    # Contrast: a live batch (branch exists but ahead of main, not merged) is still grouped —
    # the guard only skips genuinely-merged group branches.
    _seed_child(fakebd, "mr-1", labels=["batch:live"])
    _seed_child(fakebd, "mr-2", labels=["batch:live"])
    _git("branch", "wt/batch/live", "main", cwd=hive.main)
    _git("checkout", "-q", "wt/batch/live", cwd=hive.main)
    (hive.main / "batchwork.txt").write_text("x")
    _git("add", "-A", cwd=hive.main)
    _git("commit", "-qm", "feat: batch work", cwd=hive.main)  # ahead of main → not merged
    _git("checkout", "-q", "main", cwd=hive.main)

    work.schedule(epic="mr-epic", hive="myrepo", as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert len(payload["groups"]) == 1
    assert payload["groups"][0]["kind"] == "planner"
    assert sorted(payload["groups"][0]["ids"]) == ["mr-1", "mr-2"]


def test_schedule_collapsed_mode_forces_one_group_with_max_model_tier(hive, fakebd, capsys):
    # mode=collapsed collapses beads that would otherwise fan out into ONE collapsed group,
    # and the group reports the hardest member's tier (opus > sonnet).
    hive.cfg_path.write_text(_dispatch_cfg("collapsed"))
    _seed_child(fakebd, "mr-1", labels=["model:sonnet"])
    _seed_child(fakebd, "mr-2", labels=["model:opus"])
    work.schedule(epic="mr-epic", hive="myrepo", as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["groups"]) == 1
    g = payload["groups"][0]
    assert g["kind"] == "collapsed"
    assert sorted(g["ids"]) == ["mr-1", "mr-2"]
    assert g["model"] == "opus"
    assert payload["singletons"] == []


def test_schedule_auto_mode_collapses_small_epic_under_budget(hive, fakebd, capsys):
    # mode=auto with a cheap epic (xs+s = 3 ≤ budget) → collapse into one group.
    hive.cfg_path.write_text(_dispatch_cfg("auto", auto_budget=8))
    _seed_child(fakebd, "mr-1", labels=["size:xs"])
    _seed_child(fakebd, "mr-2", labels=["size:s"])
    work.schedule(epic="mr-epic", hive="myrepo", as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["groups"]) == 1
    assert payload["groups"][0]["kind"] == "collapsed"
    assert payload["singletons"] == []


def test_schedule_auto_mode_fans_out_when_over_budget(hive, fakebd, capsys):
    # mode=auto with cost over budget (l+xl = 9 > 8) → falls back to fanout (singletons).
    hive.cfg_path.write_text(_dispatch_cfg("auto", auto_budget=8))
    _seed_child(fakebd, "mr-1", labels=["size:l"])
    _seed_child(fakebd, "mr-2", labels=["size:xl"])
    work.schedule(epic="mr-epic", hive="myrepo", as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["groups"] == []
    assert sorted(payload["singletons"]) == ["mr-1", "mr-2"]


def test_schedule_dispatches_child_epic_to_a_nested_coordinator(hive, fakebd, capsys):
    # Dispatch-by-type (xn3o.8): a child EPIC is surfaced as a nested-coordinator dispatch, never a
    # developer singleton/group; a sibling leaf issue still fans out. max_depth (default 2 ≥ 1) →
    # the child epic runs as a nested-coordinator Task.
    fakebd.seed(
        "mr-ws.1", title="child epic", parent="mr-epic", issue_type="epic", labels=["model:opus"]
    )
    _seed_child(fakebd, "mr-2", labels=["model:sonnet"])
    work.schedule(epic="mr-epic", hive="myrepo", as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["coordinators"] == [
        {"id": "mr-ws.1", "dispatch": "nested-coordinator Task", "model": "opus"}
    ]
    assert payload["singletons"] == ["mr-2"]  # leaf still fans out
    assert all("mr-ws.1" not in g["ids"] for g in payload["groups"])
    assert payload["max_depth"] == 2


# --- bh-fr0a: merge-bubble subjects must be Conventional-Commits-compliant (no version bump) ---

# The exact commitizen `cz check` pattern this hive enforces via the tracked commit-msg hook
# (.githooks/commit-msg → cz check). A merge subject that fails this regex makes every merge
# verb (merge / merge --group / finish) abort on a hook-enforcing hive — the bh-fr0a bug.
_CZ_CONVENTIONAL = re.compile(
    r"(?s)(build|bump|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)"
    r"(\(\S+\))?!?: ([^\n\r]+)((\n\n.*)|(\s*))?$"
)


# The four merge-bubble subjects bh generates, mirroring each construction site's f-string:
#   work.py            → "chore(merge): molecule <epic>"  (finish / merge --molecule)
#   work.py            → "chore(merge): bead <bead>"      (merge <bead>)
#   work_group.py      → "chore(merge): batch <group>"    (merge --group)
#   worktree_merge.py  → "chore(merge): <branch>"         (fallback message)
_MERGE_SUBJECTS = [
    "chore(merge): molecule mr-1",
    "chore(merge): bead mr-1.2",
    "chore(merge): batch samefile",
    "chore(merge): wt/bead/issue/mr-3",
]


@pytest.mark.parametrize("subject", _MERGE_SUBJECTS)
def test_merge_subjects_are_conventional(subject):
    # Every generated merge-bubble subject satisfies commitizen's conventional pattern, so
    # `cz check` in the commit-msg hook accepts it and the merge lands.
    assert _CZ_CONVENTIONAL.match(subject), f"non-conventional merge subject: {subject!r}"


@pytest.mark.parametrize("subject", _MERGE_SUBJECTS)
def test_merge_subjects_never_trigger_a_version_bump(subject):
    # A merge bubble must never contribute a release bump — the per-bead commits inside carry
    # that. So the type is never feat/fix and there is no `!` breaking-change marker.
    type_token = subject.split("(", 1)[0].split(":", 1)[0]
    assert type_token not in ("feat", "fix"), f"bump-triggering type in merge subject: {subject!r}"
    assert "!:" not in subject, f"breaking-change marker in merge subject: {subject!r}"
    assert not subject.startswith(("feat", "fix")), f"bump-triggering subject: {subject!r}"
