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
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import typer

from ws import config, otel, registry, work, worktree
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

CONFIG_YAML_WITH_UNION = """\
providers: [github]
work:
  validate_cmd: "true"
  review_gate: "human"
  identity: {mode: agent, name: "crew/default", email: "agents@test.dev"}
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


def _mol_listed(rig, epic):
    return _git("branch", "--list", f"mol/{epic}", cwd=rig.main).stdout.strip()


def test_claim_auto_opens_molecule_when_epic_kicked_off(rig, fakebd):
    """Kickoff relocated to the integration plane: claiming a child of a kickoff=approved epic
    lazily opens mol/<epic>, so the child worktree forks off the molecule (not main)."""
    fakebd.seed("mr-1.1", title="t")
    fakebd.states["mr-1"] = {"kickoff": "approved"}
    work.claim(bead="mr-1.1", as_="", rig="myrepo")
    assert _mol_listed(rig, "mr-1") != "", "claim should open mol/mr-1 for a kicked-off epic"


def test_assign_auto_opens_molecule_when_epic_kicked_off(rig, fakebd):
    """assign (orchestrator dispatch) also opens mol/<epic> for a kicked-off epic's child."""
    fakebd.seed("mr-1.1", title="t")
    fakebd.states["mr-1"] = {"kickoff": "approved"}
    work.assign(bead="mr-1.1", to="crew/dev", rig="myrepo")
    assert _mol_listed(rig, "mr-1") != "", "assign should open mol/mr-1 for a kicked-off epic"


def test_claim_no_molecule_when_epic_not_kicked_off(rig, fakebd):
    """Backward-compatible: a dotted bead whose epic was never kicked off opens no molecule branch
    — it targets main directly, exactly as before the kickoff relocation."""
    fakebd.seed("mr-2.1", title="t")  # no kickoff state on epic mr-2
    work.claim(bead="mr-2.1", as_="", rig="myrepo")
    assert _mol_listed(rig, "mr-2") == "", "no molecule branch without kickoff=approved"


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
        lambda cfg, entry, actor="": {
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
        lambda cfg, entry, actor="": {
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
    name: "crew/default"
    email: "agents@test.dev"
    crews:
      crew/alice: {email: "alice@agents.dev", signing_key: "/keys/alice.pub", sign: true}
      crew/bob: {email: "bob@agents.dev", signing_key: "/keys/bob.pub", sign: true}
managed_repos:
  - {provider: github, org: myorg, repo: myrepo, prefix: mr, kind: personal}
"""


def test_claim_stamps_per_crew_signing_identity(rig, fakebd):
    rig.cfg_path.write_text(CREWS_CONFIG_YAML)
    fakebd.seed("mr-1", title="a")
    fakebd.seed("mr-2", title="b")
    work.claim(bead="mr-1", as_="crew/alice", rig="myrepo")
    work.claim(bead="mr-2", as_="crew/bob", rig="myrepo")
    a, b = _wt(rig, "mr-1"), _wt(rig, "mr-2")

    assert _cfg_get(a, "user.name") == "crew/alice"
    assert _cfg_get(a, "user.email") == "alice@agents.dev"
    assert _cfg_get(a, "user.signingkey") == "/keys/alice.pub"
    assert _cfg_get(a, "gpg.format") == "ssh"
    assert _cfg_get(a, "commit.gpgsign") == "true"

    assert _cfg_get(b, "user.name") == "crew/bob"
    assert _cfg_get(b, "user.email") == "bob@agents.dev"
    assert _cfg_get(b, "user.signingkey") == "/keys/bob.pub"

    # distinct from each other and from the human (human@example.com, no signing key)
    assert _cfg_get(a, "user.signingkey") != _cfg_get(b, "user.signingkey")
    assert _cfg_get(a, "user.email") != _cfg_get(b, "user.email")
    assert _cfg_get(a, "user.email") != "human@example.com"


# ---- cwd guard (A1: warn when agent edits from main clone, not worktree) ----
#
# Sub-agents share the session cwd.  Absolute paths under the rig root resolve to the main
# clone, not the worktree — so an agent that skips `cd <worktree>` silently edits the wrong
# tree.  `claim` (and `check`/`submit`) detect this and emit a prominent, copy-pasteable
# `cd` reminder so the misdirection is impossible to miss.


def test_claim_warns_when_cwd_is_main_clone(rig, fakebd, capsys, monkeypatch):
    """claim emits a WARNING with the exact cd path when cwd is the main clone."""
    fakebd.seed("mr-1", title="t")
    monkeypatch.chdir(rig.main)
    work.claim(bead="mr-1", as_="", rig="myrepo")
    err = capsys.readouterr().err
    wt = _wt(rig, "mr-1")
    assert "WARNING" in err
    assert str(wt) in err
    assert 'cd' in err


def test_claim_no_warning_when_cwd_is_worktree(rig, fakebd, capsys, monkeypatch):
    """claim emits no WARNING when cwd is already the bead's worktree."""
    fakebd.seed("mr-1", title="t")
    # First claim provisions the worktree; re-claim from inside it to test the no-warning path.
    work.claim(bead="mr-1", as_="", rig="myrepo")
    wt = _wt(rig, "mr-1")
    monkeypatch.chdir(wt)
    capsys.readouterr()  # drain previous output
    work.claim(bead="mr-1", as_="", rig="myrepo")
    err = capsys.readouterr().err
    assert "WARNING" not in err


# ---- assign → claim handshake ----------------------------------------------


def test_assign_then_claim(rig, fakebd):
    fakebd.seed("mr-2", title="t")
    work.assign(bead="mr-2", to="crew/carol", rig="myrepo")
    assert fakebd.beads["mr-2"]["status"] == "open"  # assignment is not the ack
    assert fakebd.beads["mr-2"]["assignee"] == "crew/carol"
    assert _cfg_get(_wt(rig, "mr-2"), "user.name") == "crew/carol"

    work.claim(bead="mr-2", as_="crew/carol", rig="myrepo")
    assert fakebd.beads["mr-2"]["status"] == "in_progress"  # claim is the ack


# ---- seat enforcement: epic->coordinator, issue->developer ------------------


def test_assign_epic_only_to_coordinator(rig, fakebd):
    """An epic (container) may only be assigned to a coordinator (coord/<name>); a developer
    target is refused before any provisioning. A coordinator target is accepted."""
    fakebd.seed("mr-epic", title="e", issue_type="epic")
    with pytest.raises(typer.Exit):
        work.assign(bead="mr-epic", to="crew/dev", rig="myrepo")
    assert not _wt(rig, "mr-epic").exists()  # rejected before provisioning
    work.assign(bead="mr-epic", to="coord/lead", rig="myrepo")
    assert fakebd.beads["mr-epic"]["assignee"] == "coord/lead"


def test_assign_issue_only_to_developer(rig, fakebd):
    """A non-epic (leaf) bead may only be assigned to a developer (crew/<name>), not a
    coordinator."""
    fakebd.seed("mr-7", title="t")  # no issue_type -> leaf
    with pytest.raises(typer.Exit):
        work.assign(bead="mr-7", to="coord/lead", rig="myrepo")
    assert not _wt(rig, "mr-7").exists()


def test_claim_epic_only_by_coordinator(rig, fakebd):
    """Claiming an epic requires acting as a coordinator; a developer identity is refused, a
    coordinator identity is accepted."""
    fakebd.seed("mr-epic", title="e", issue_type="epic")
    with pytest.raises(typer.Exit):
        work.claim(bead="mr-epic", as_="crew/dev", rig="myrepo")
    assert not _wt(rig, "mr-epic").exists()
    work.claim(bead="mr-epic", as_="coord/lead", rig="myrepo")
    assert fakebd.beads["mr-epic"]["status"] == "in_progress"


def test_assign_emits_genai_dispatch_span(rig, fakebd, monkeypatch):
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

    work.assign(bead="mr-9", to="crew/carol", rig="myrepo")

    # The dispatch span is the `invoke_agent {agent}`-named one (the verb-level work.assign span
    # is also opened by @trace_verb; pick the gen_ai one out of the calls).
    dispatch = [
        c for c in tracer.start_as_current_span.call_args_list
        if c.args and str(c.args[0]).startswith("invoke_agent")
    ]
    assert len(dispatch) == 1
    assert dispatch[0].args[0] == "invoke_agent crew/carol"
    attrs = dispatch[0].kwargs["attributes"]
    assert attrs["gen_ai.operation.name"] == "invoke_agent"
    assert attrs["gen_ai.request.model"] == "opus"
    assert attrs["gen_ai.agent.name"] == "crew/carol"
    assert attrs["ws.bead"] == "mr-9"
    # brief is an EVENT, never an attribute
    assert "secret brief body — may contain PII" not in attrs.values()
    span.add_event.assert_called_once()
    ev_name, ev_attrs = span.add_event.call_args.args
    assert ev_name == "gen_ai.user.message"
    assert ev_attrs["ws.genai.content_kind"] == "brief"
    assert ev_attrs["content"] == "secret brief body — may contain PII"


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


def test_merge_otel_off_emits_no_span(rig, fakebd, monkeypatch):
    # Acceptance (otel off, the default): a real `ws work merge` lands exactly as before and
    # never builds a span — instrumentation is a zero-overhead no-op.
    monkeypatch.setattr(otel, "span", MagicMock(side_effect=AssertionError("no span when off")))
    fakebd.seed("mr-14", title="t")
    _take_to_approved(rig, fakebd, "mr-14")

    work.merge(bead="mr-14", rig="myrepo", rm=False, molecule=False)

    assert fakebd.beads["mr-14"]["status"] == "closed"  # unchanged behavior


def test_merge_otel_on_emits_subprocess_and_verb_spans_and_metrics(rig, fakebd, monkeypatch):
    # Acceptance (otel on, mocked provider): a real `ws work merge` produces the verb span, the
    # subprocess (git) span at the run() seam, the merge-duration metric, and the lifecycle counter.
    fakebd.seed("mr-15", title="t")
    _take_to_approved(rig, fakebd, "mr-15")  # taken to approved with otel still off

    tracer = MagicMock(name="tracer")
    meter = MagicMock(name="meter")
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(otel, "get_tracer", lambda *a, **k: tracer)
    monkeypatch.setattr(otel, "get_meter", lambda *a, **k: meter)
    otel._instruments.clear()

    work.merge(bead="mr-15", rig="myrepo", rm=False, molecule=False)

    span_names = [c.args[0] for c in tracer.start_as_current_span.call_args_list]
    assert "work.merge" in span_names  # the verb span
    assert any(n.startswith("git") for n in span_names)  # ≥1 subprocess span at the run() seam

    # merge.duration is one of several flow histograms the seam now emits — assert it's present
    # (it's no longer the LAST create_histogram call now that cycle/stage/slot ride here too).
    hist_names = {c.args[0] for c in meter.create_histogram.call_args_list}
    assert "ws.work.merge.duration" in hist_names
    assert meter.create_histogram.return_value.record.call_count >= 1
    adds = meter.create_counter.return_value.add.call_args_list
    # All counters share one mocked instrument (merge.outcome rides here too) — pick the bead
    # transitions by their key. The bead id is no longer a metric attr; it rides the span instead.
    transitions = [
        c.args[1]["ws.bead.transition"] for c in adds if "ws.bead.transition" in c.args[1]
    ]
    assert "merged" in transitions
    assert not any("ws.bead" in c.args[1] for c in adds)  # bead id never on a metric point

    otel._instruments.clear()  # don't leak mocked instruments into later tests


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


def test_merge_lands_coupled_beads_without_manual_step(rig, fakebd):
    """Two coupled beads touch the same file: A adds a boilerplate line; B adds the same line
    (a patch git can replay-skip) plus its own unique line. Both land via the merger with no hand
    serialization — the second is recovered by rebase-retry when its plain merge conflicts — and
    no work is dropped: the final file carries A's line once and B's unique line, under a real
    --no-ff bubble. (HOME is isolated by the fixture so git config can't perturb the merge.)"""
    _commit(rig.main, "L0\n", fname="shared.txt")  # shared base so beads append (not add/add)
    fakebd.seed("mr-20", title="t")
    fakebd.seed("mr-21", title="t")
    # Claim BOTH before either merges, so they fork off the SAME base.
    work.claim(bead="mr-20", as_="", rig="myrepo")
    work.claim(bead="mr-21", as_="", rig="myrepo")
    _append(_wt(rig, "mr-20"), "shared\n")  # bead A adds the boilerplate line
    _append(_wt(rig, "mr-21"), "shared\n")  # bead B adds the SAME line (replay-skippable patch)…
    _append(_wt(rig, "mr-21"), "bonly\n")  # …plus its own unique change
    work.submit(bead="mr-20", rig="myrepo")
    fakebd.approve("mr-20")
    work.submit(bead="mr-21", rig="myrepo")
    fakebd.approve("mr-21")

    work.merge(bead="mr-20", rig="myrepo", rm=False, molecule=False)
    work.merge(bead="mr-21", rig="myrepo", rm=False, molecule=False)

    shared = (rig.main / "shared.txt").read_text()
    assert "bonly" in shared  # bead B's unique work landed
    assert shared.count("shared") == 1  # A's coupled line is present exactly once (no dup, no loss)
    # history preserved: the second bead landed as a real --no-ff merge bubble
    assert _git("log", "-1", "--format=%s", cwd=rig.main).stdout.strip() == "merge mr-21"
    parents = _git("rev-list", "--parents", "-n", "1", "HEAD", cwd=rig.main).stdout.split()
    assert len(parents) == 3  # merge commit + two parents
    assert fakebd.beads["mr-21"]["status"] == "closed"


def test_merge_real_conflict_fails_clean_and_restores_branch(rig, fakebd):
    """Two beads edit the SAME line divergently — a real conflict the rebase can't resolve. The
    recovery path runs (a `.premerge-*` snapshot is taken, the rebase is attempted and fails), then
    the merge fails non-zero with main untouched, the bead not closed, and the bead branch restored
    to its pre-rebase tip (work never dropped)."""
    _commit(rig.main, "base\n", fname="shared.txt")
    fakebd.seed("mr-30", title="t")
    fakebd.seed("mr-31", title="t")
    work.claim(bead="mr-30", as_="", rig="myrepo")
    work.claim(bead="mr-31", as_="", rig="myrepo")
    _set_line(_wt(rig, "mr-30"), "X\n")  # both rewrite the one line they share, divergently
    _set_line(_wt(rig, "mr-31"), "Y\n")
    work.submit(bead="mr-30", rig="myrepo")
    fakebd.approve("mr-30")
    work.submit(bead="mr-31", rig="myrepo")
    fakebd.approve("mr-31")

    work.merge(bead="mr-30", rig="myrepo", rm=False, molecule=False)  # clean → base has X

    main_tip = _git("rev-parse", "main", cwd=rig.main).stdout.strip()
    branch_tip = _git("rev-parse", "wt/bead/mr-31", cwd=rig.main).stdout.strip()
    with pytest.raises(typer.Exit):
        work.merge(bead="mr-31", rig="myrepo", rm=False, molecule=False)

    assert _git("rev-parse", "main", cwd=rig.main).stdout.strip() == main_tip  # main untouched
    # the bead branch is restored to its exact pre-merge tip, still carrying its divergent change
    assert _git("rev-parse", "wt/bead/mr-31", cwd=rig.main).stdout.strip() == branch_tip
    assert _git("show", "wt/bead/mr-31:shared.txt", cwd=rig.main).stdout.strip() == "Y"
    # the recovery path was entered: a pre-merge snapshot of the bead branch exists
    branches = _git("branch", "--list", "wt/bead/mr-31.premerge-*", cwd=rig.main).stdout
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


def test_merge_emits_slot_cycle_stage_outcome_metrics(rig, fakebd, monkeypatch):
    """The happy merge seam emits slot wait/hold, cycle_time(+active), the coding/review_wait/
    merge_latency stage breakdown and a merge.outcome counter — bounded attrs only, no bead id."""
    fakebd.seed("mr-40", title="t")
    _take_to_approved(rig, fakebd, "mr-40")
    # at-merge bd reads: created/started on the bead, a review→pending + changes-requested event,
    # and a resolved review gate (reason 'review <sha>').
    fakebd.beads["mr-40"].update(created_at=_iso_ago(hours=2), started_at=_iso_ago(hours=1))
    fakebd.beads["mr-40.e1"] = {
        "id": "mr-40.e1", "parent": "mr-40", "issue_type": "event",
        "title": "set-state review=pending", "created_at": _iso_ago(minutes=40),
    }
    fakebd.beads["mr-40.e2"] = {
        "id": "mr-40.e2", "parent": "mr-40", "issue_type": "event",
        "title": "review=changes-requested",
    }
    fakebd.gates.append({
        "id": "rg", "status": "closed",
        "description": "blocking mr-40\n\nReason: review abc", "closed_at": _iso_ago(minutes=10),
    })

    meter = _otel_meter_on(monkeypatch)
    work.merge(bead="mr-40", rig="myrepo", rm=False, molecule=False)

    hist_names = {c.args[0] for c in meter.create_histogram.call_args_list}
    assert {
        "ws.work.merge_slot.wait", "ws.work.merge_slot.hold",
        "ws.work.cycle_time", "ws.work.cycle_time.active",
        "ws.work.stage.coding", "ws.work.stage.review_wait", "ws.work.stage.merge_latency",
    } <= hist_names
    adds = meter.create_counter.return_value.add.call_args_list
    outcomes = [c.args[1] for c in adds if "ws.merge.how" in c.args[1]]
    assert len(outcomes) == 1
    assert outcomes[0]["ws.merge.kind"] == "bead" and outcomes[0]["ws.rig"] == "mr"
    assert outcomes[0]["ws.merge.how"] in ("clean", "rebased", "union")
    assert all("ws.bead" not in c.args[1] and "ws.epic" not in c.args[1] for c in adds)
    otel._instruments.clear()


def test_merge_bd_read_failure_does_not_block_merge(rig, fakebd, monkeypatch):
    """A bead with NO timestamps/events/gate (the at-merge reads come back empty) still merges and
    closes — the flow metrics are best-effort and never block the land."""
    fakebd.seed("mr-45", title="t")
    _take_to_approved(rig, fakebd, "mr-45")  # no created_at/started_at/events seeded
    _otel_meter_on(monkeypatch)
    work.merge(bead="mr-45", rig="myrepo", rm=False, molecule=False)
    assert fakebd.beads["mr-45"]["status"] == "closed"  # merge succeeded regardless
    otel._instruments.clear()


def test_merge_conflict_emits_conflict_outcome(rig, fakebd, monkeypatch):
    """A real conflict bumps the merge.outcome counter with how=conflict BEFORE the raise."""
    _commit(rig.main, "base\n", fname="shared.txt")
    fakebd.seed("mr-30", title="t")
    fakebd.seed("mr-31", title="t")
    work.claim(bead="mr-30", as_="", rig="myrepo")
    work.claim(bead="mr-31", as_="", rig="myrepo")
    _set_line(_wt(rig, "mr-30"), "X\n")
    _set_line(_wt(rig, "mr-31"), "Y\n")
    work.submit(bead="mr-30", rig="myrepo")
    fakebd.approve("mr-30")
    work.submit(bead="mr-31", rig="myrepo")
    fakebd.approve("mr-31")
    work.merge(bead="mr-30", rig="myrepo", rm=False, molecule=False)  # clean → base has X

    meter = _otel_meter_on(monkeypatch)
    with pytest.raises(typer.Exit):
        work.merge(bead="mr-31", rig="myrepo", rm=False, molecule=False)  # real conflict

    adds = meter.create_counter.return_value.add.call_args_list
    outcomes = [c.args[1] for c in adds if "ws.merge.how" in c.args[1]]
    assert any(o["ws.merge.how"] == "conflict" and o["ws.merge.kind"] == "bead" for o in outcomes)
    otel._instruments.clear()


def test_check_emits_validation_duration(rig, fakebd, monkeypatch):
    fakebd.seed("mr-60", title="t")
    work.claim(bead="mr-60", as_="", rig="myrepo")
    meter = _otel_meter_on(monkeypatch)
    work.check(bead="mr-60", rig="myrepo")
    records = meter.create_histogram.return_value.record.call_args_list
    vd = [c.args[1] for c in records if c.args[1].get("ws.work.phase") == "check"]
    assert vd and vd[0]["ws.validation.result"] == "pass" and vd[0]["ws.rig"] == "mr"
    assert "ws.bead" not in vd[0]
    hist_names = {c.args[0] for c in meter.create_histogram.call_args_list}
    assert "ws.work.validation.duration" in hist_names
    otel._instruments.clear()


def test_submit_emits_validation_duration(rig, fakebd, monkeypatch):
    fakebd.seed("mr-61", title="t")
    work.claim(bead="mr-61", as_="", rig="myrepo")
    _commit(_wt(rig, "mr-61"), "feat: x")
    meter = _otel_meter_on(monkeypatch)
    work.submit(bead="mr-61", rig="myrepo")
    records = meter.create_histogram.return_value.record.call_args_list
    vd = [c.args[1] for c in records if c.args[1].get("ws.work.phase") == "submit"]
    assert vd and vd[0]["ws.validation.result"] == "pass" and vd[0]["ws.rig"] == "mr"
    otel._instruments.clear()


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


# ---- start / finish: epic-only aliases (kickoff + land) ---------------------


def test_start_opens_molecule_and_claims_epic(rig, fakebd):
    """start <epic> --as coord/<id> opens mol/<epic> (integration-plane kickoff) and takes the
    epic seat (in_progress, assigned to the coordinator)."""
    fakebd.seed("mr-epic", title="e", issue_type="epic")
    fakebd.states["mr-epic"] = {"kickoff": "approved"}
    work.start(epic="mr-epic", as_="coord/lead", rig="myrepo")
    assert _mol_listed(rig, "mr-epic") != ""
    assert fakebd.beads["mr-epic"]["status"] == "in_progress"
    assert fakebd.beads["mr-epic"]["assignee"] == "coord/lead"


def test_start_rejects_non_epic(rig, fakebd):
    """start refuses a leaf bead — that's `claim`'s job."""
    fakebd.seed("mr-5", title="t")
    with pytest.raises(typer.Exit):
        work.start(epic="mr-5", as_="coord/lead", rig="myrepo")


def test_start_requires_kickoff_approved(rig, fakebd):
    """start refuses an epic that planning hasn't approved (no molecule opened)."""
    fakebd.seed("mr-epic", title="e", issue_type="epic")  # no kickoff state
    with pytest.raises(typer.Exit):
        work.start(epic="mr-epic", as_="coord/lead", rig="myrepo")
    assert _mol_listed(rig, "mr-epic") == ""


def test_start_requires_coordinator_seat(rig, fakebd):
    """start refuses a developer identity — an epic is a coordinator's seat (no molecule opened)."""
    fakebd.seed("mr-epic", title="e", issue_type="epic")
    fakebd.states["mr-epic"] = {"kickoff": "approved"}
    with pytest.raises(typer.Exit):
        work.start(epic="mr-epic", as_="crew/dev", rig="myrepo")
    assert _mol_listed(rig, "mr-epic") == ""


def test_finish_lands_molecule_like_merge_molecule(rig, fakebd):
    """finish <epic> is the epic-only alias of `merge --molecule`: lands the assembled molecule as
    one bubble and closes the epic."""
    _land_two_bead_molecule(rig, fakebd, "mr-1")
    fakebd.beads["mr-1"]["issue_type"] = "epic"  # finish guards issue_type == epic
    work.finish(epic="mr-1", rig="myrepo")
    assert _git("log", "-1", "--format=%s", cwd=rig.main).stdout.strip() == "merge molecule mr-1"
    assert fakebd.beads["mr-1"]["status"] == "closed"
    assert not worktree._branch_exists(rig.main, "mol/mr-1")


def test_finish_rejects_non_epic(rig, fakebd):
    """finish refuses a non-epic bead."""
    fakebd.seed("mr-5", title="t")
    with pytest.raises(typer.Exit):
        work.finish(epic="mr-5", rig="myrepo")


def test_validation_mode_gates_molecule_clean_checkouts(rig, fakebd, monkeypatch):
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
        _land_two_bead_molecule(rig, fakebd, epic)  # setup runs its own validations
        monkeypatch.setattr(config, "validation_mode", lambda cfg, entry, m=mode: m)
        seen.clear()  # count only the molecule-land boundary
        work.merge(bead=epic, rig="myrepo", molecule=True)
        assert len(seen) == expected, f"{mode}: {seen}"
        assert fakebd.beads[epic]["status"] == "closed"


def test_validation_mode_per_point_entrypoint(rig, fakebd, monkeypatch):
    """A per-point override at work.validate.<phase> wins over validate_cmd for that boundary."""
    rig.cfg_path.write_text(
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
    _land_two_bead_molecule(rig, fakebd, "mr-1")  # setup uses validate_cmd ("true")
    seen.clear()  # observe only the molecule-land boundary
    work.merge(bead="mr-1", rig="myrepo", molecule=True)

    # relaxed default → one molecule-phase validation, using the per-point command, not validate_cmd
    assert seen == ["true # MOLECULE"]


def test_merge_molecule_revalidates_and_rolls_back_when_main_went_stale_red(rig, fakebd):
    """main advances after the molecule was cut; the combined --no-ff tree is logically red. The
    pre-land mol validation passes, but the staleness-triggered POST-land validation (relaxed mode,
    a correctness backstop) catches it and rolls main back — lossless: mol branch preserved, epic
    still open."""
    # validate_cmd: green on mol/<epic> (no marker), red once main's advance commit is in the tree
    rig.cfg_path.write_text(
        CONFIG_YAML.replace(
            'validate_cmd: "true"', 'validate_cmd: "test ! -f main_advance.txt"'
        )
    )
    _land_two_bead_molecule(rig, fakebd, "mr-1")

    # a concurrent commit lands directly on main AFTER the molecule forked → stale. (The bead
    # merges parked the clone on mol/mr-1, so check out main first or the commit poisons the mol.)
    _git("checkout", "-q", "main", cwd=rig.main)
    _commit(rig.main, "feat: concurrent", fname="main_advance.txt")
    advanced = _git("rev-parse", "main", cwd=rig.main).stdout.strip()

    with pytest.raises(typer.Exit):
        work.merge(bead="mr-1", rig="myrepo", molecule=True)

    # rolled back to the pre-land tip (the concurrent commit), NOT the merge bubble
    assert _git("rev-parse", "main", cwd=rig.main).stdout.strip() == advanced
    assert _git("log", "-1", "--format=%s", cwd=rig.main).stdout.strip() == "feat: concurrent"
    # lossless + not finalized: mol branch intact, epic still open, slot acquired+released
    assert worktree._branch_exists(rig.main, "mol/mr-1")
    assert fakebd.beads["mr-1"]["status"] != "closed"
    assert fakebd.did("merge-slot", "acquire") and fakebd.did("merge-slot", "release")


def test_merge_molecule_does_not_rewrite_shared_main_on_postland_red(rig, fakebd):
    """When the integration branch is shared (pushed → has an upstream), a post-land red must NOT
    rewrite it — the land was intentional; fix forward. The bubble stays on main, epic left open."""
    rig.cfg_path.write_text(
        CONFIG_YAML.replace('validate_cmd: "true"', 'validate_cmd: "test ! -f main_advance.txt"')
    )
    _land_two_bead_molecule(rig, fakebd, "mr-1")

    # main moves AND becomes shared (pushed → has an upstream). Check out main first: the bead
    # merges parked the clone on mol/mr-1.
    _git("checkout", "-q", "main", cwd=rig.main)
    _commit(rig.main, "feat: concurrent", fname="main_advance.txt")  # main moved → stale
    _git("push", "-u", "-q", "origin", "main", cwd=rig.main)  # now main has an upstream

    with pytest.raises(typer.Exit):
        work.merge(bead="mr-1", rig="myrepo", molecule=True)

    # NOT rewritten: the --no-ff bubble landed and stands on main (HEAD is the merge, not reset)
    assert _git("log", "-1", "--format=%s", cwd=rig.main).stdout.strip() == "merge molecule mr-1"
    # lossless + escalated, not finalized: epic still open, slot released
    assert fakebd.beads["mr-1"]["status"] != "closed"
    assert fakebd.did("merge-slot", "acquire") and fakebd.did("merge-slot", "release")


def test_merge_bead_conservative_rolls_back_and_bounces_on_combined_red(rig, fakebd):
    """conservative: a bead green at submit but red in COMBINATION on the mol tip is rolled back to
    the pre-merge sha and bounced to changes-requested — never closed, never left broken."""
    # submit stays green (validate_cmd "true"); only the merge-phase re-test goes red once the
    # second bead's file is on the tip — isolating the break to the combined integration tip.
    rig.cfg_path.write_text(
        CONFIG_YAML.replace(
            'validate_cmd: "true"',
            'validate_cmd: "true"\n  validation: conservative'
            '\n  validate: {merge: "test ! -f mr-1.2.txt"}',
        )
    )
    _mol_branch(rig, "mr-1")
    fakebd.seed("mr-1", title="epic")
    # first bead merges clean (its file alone keeps validate green)
    fakebd.seed("mr-1.1", title="t", parent="mr-1")
    work.claim(bead="mr-1.1", as_="", rig="myrepo")
    _commit(_wt_of(rig, "mr-1.1"), "feat: one", fname="mr-1.1.txt")
    work.submit(bead="mr-1.1", rig="myrepo")
    fakebd.approve("mr-1.1")
    work.merge(bead="mr-1.1", rig="myrepo", rm=False, molecule=False)
    assert fakebd.beads["mr-1.1"]["status"] == "closed"

    mol_before = _git("rev-parse", "mol/mr-1", cwd=rig.main).stdout.strip()

    # second bead is individually fine but turns the mol tip red (mr-1.2.txt now present)
    fakebd.seed("mr-1.2", title="t", parent="mr-1")
    work.claim(bead="mr-1.2", as_="", rig="myrepo")
    _commit(_wt_of(rig, "mr-1.2"), "feat: two", fname="mr-1.2.txt")
    work.submit(bead="mr-1.2", rig="myrepo")
    fakebd.approve("mr-1.2")

    with pytest.raises(typer.Exit):
        work.merge(bead="mr-1.2", rig="myrepo", rm=False, molecule=False)

    # mol tip rolled back to before the bad merge; bead bounced, not closed; slot released
    assert _git("rev-parse", "mol/mr-1", cwd=rig.main).stdout.strip() == mol_before
    assert fakebd.beads["mr-1.2"]["status"] != "closed"
    assert fakebd.states.get("mr-1.2", {}).get("review") == "changes-requested"
    assert fakebd.did("merge-slot", "release")


def test_merge_target_aware_command_main_vs_mol(rig, fakebd, monkeypatch):
    """The per-bead merge re-test resolves `merge-main` for an ad-hoc bead → main, and the plain
    `merge` for a molecule member → mol/<epic>."""
    rig.cfg_path.write_text(
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
    _take_to_approved(rig, fakebd, "mr-10")
    seen.clear()
    work.merge(bead="mr-10", rig="myrepo", rm=False, molecule=False)
    assert "true # MAIN" in seen and "true # MOL" not in seen

    # molecule member → base is mol/<epic> → plain merge
    _mol_branch(rig, "mr-2")
    fakebd.seed("mr-2", title="epic")
    fakebd.seed("mr-2.1", title="t", parent="mr-2")
    work.claim(bead="mr-2.1", as_="", rig="myrepo")
    _commit(_wt_of(rig, "mr-2.1"), "feat: a", fname="a.txt")
    work.submit(bead="mr-2.1", rig="myrepo")
    fakebd.approve("mr-2.1")
    seen.clear()
    work.merge(bead="mr-2.1", rig="myrepo", rm=False, molecule=False)
    assert "true # MOL" in seen and "true # MAIN" not in seen


def test_merge_adhoc_main_gate_fires_in_relaxed_and_rolls_back(rig, fakebd):
    """relaxed: an ad-hoc bead → main always gets the on_main re-validation; on red an unpushed main
    is rolled back to its pre-merge sha and the bead is bounced (no conservative mode needed)."""
    rig.cfg_path.write_text(
        CONFIG_YAML.replace(
            'validate_cmd: "true"',
            'validate_cmd: "true"\n  validate: {merge-main: "test ! -f mr-9.txt"}',
        )
    )
    main_before = _git("rev-parse", "main", cwd=rig.main).stdout.strip()
    fakebd.seed("mr-9", title="t")
    work.claim(bead="mr-9", as_="", rig="myrepo")
    _commit(_wt(rig, "mr-9"), "feat: nine", fname="mr-9.txt")  # submit green; merge-main red
    work.submit(bead="mr-9", rig="myrepo")
    fakebd.approve("mr-9")

    with pytest.raises(typer.Exit):
        work.merge(bead="mr-9", rig="myrepo", rm=False, molecule=False)

    # unpushed main rolled back to pre-merge; bead bounced, not closed; slot released
    assert _git("rev-parse", "main", cwd=rig.main).stdout.strip() == main_before
    assert fakebd.beads["mr-9"]["status"] != "closed"
    assert fakebd.states.get("mr-9", {}).get("review") == "changes-requested"
    assert fakebd.did("merge-slot", "release")


def test_merge_adhoc_main_gate_escalates_red_kept_on_pushed_main(rig, fakebd):
    """relaxed: an ad-hoc bead → a SHARED (pushed) main that goes red is NOT rewritten — the merge
    bubble stands, escalated for fix-forward; the bead is still bounced."""
    rig.cfg_path.write_text(
        CONFIG_YAML.replace(
            'validate_cmd: "true"',
            'validate_cmd: "true"\n  validate: {merge-main: "test ! -f mr-6.txt"}',
        )
    )
    _git("push", "-u", "-q", "origin", "main", cwd=rig.main)  # main is now shared (has an upstream)
    fakebd.seed("mr-6", title="t")
    work.claim(bead="mr-6", as_="", rig="myrepo")
    _commit(_wt(rig, "mr-6"), "feat: six", fname="mr-6.txt")  # submit green; merge-main red on main
    work.submit(bead="mr-6", rig="myrepo")
    fakebd.approve("mr-6")

    with pytest.raises(typer.Exit):
        work.merge(bead="mr-6", rig="myrepo", rm=False, molecule=False)

    # pushed main NOT rewritten — the bubble stands; bead bounced, not closed
    assert _git("log", "-1", "--format=%s", cwd=rig.main).stdout.strip() == "merge mr-6"
    assert fakebd.beads["mr-6"]["status"] != "closed"
    assert fakebd.states.get("mr-6", {}).get("review") == "changes-requested"


def test_merge_adhoc_main_gate_skipped_under_loose(rig, fakebd, monkeypatch):
    """loose trusts submits and skips main-gate checks — an ad-hoc bead → main does NO post-merge
    re-validation (consistent with loose skipping the molecule pre-land gate)."""
    rig.cfg_path.write_text(
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
    _take_to_approved(rig, fakebd, "mr-8")
    seen.clear()  # ignore submit's clean_checkout
    work.merge(bead="mr-8", rig="myrepo", rm=False, molecule=False)
    assert seen == []  # loose: no post-merge re-validation, even for an ad-hoc → main land
    assert fakebd.beads["mr-8"]["status"] == "closed"


def test_merge_mol_member_relaxed_runs_no_post_merge_validation(rig, fakebd, monkeypatch):
    """No regression: in relaxed, a bead merging into its mol/<epic> gets NO post-merge re-test
    (on_main is false for a mol target); the mol→main land is its backstop."""
    seen = []
    real_cc = worktree.clean_checkout
    monkeypatch.setattr(
        worktree,
        "clean_checkout",
        lambda entry, branch, cmd: seen.append(branch) or real_cc(entry, branch, cmd),
    )
    _mol_branch(rig, "mr-7")
    fakebd.seed("mr-7", title="epic")
    fakebd.seed("mr-7.1", title="t", parent="mr-7")
    work.claim(bead="mr-7.1", as_="", rig="myrepo")
    _commit(_wt_of(rig, "mr-7.1"), "feat: x", fname="x.txt")
    work.submit(bead="mr-7.1", rig="myrepo")
    fakebd.approve("mr-7.1")
    seen.clear()  # ignore submit's clean_checkout
    work.merge(bead="mr-7.1", rig="myrepo", rm=False, molecule=False)
    assert seen == []  # a bead → mol/<epic> in relaxed does no post-merge re-validation
    assert fakebd.beads["mr-7.1"]["status"] == "closed"


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


# ---- lifecycle transitions (assigned / claimed / abandoned) -----------------
#
# Complete the ws.work.bead.transitions counter: assign/claim/abandon were the holes (merged /
# molecule_landed / review_pending already fired). With otel on (mocked meter) each verb bumps the
# counter with its transition value; off, the verbs run unchanged and create no instrument.


def test_assign_claim_abandon_emit_lifecycle_transitions(rig, fakebd, monkeypatch):
    meter = MagicMock(name="meter")
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(otel, "get_tracer", lambda *a, **k: MagicMock())
    monkeypatch.setattr(otel, "get_meter", lambda *a, **k: meter)
    otel._instruments.clear()

    fakebd.seed("mr-20", title="t")
    work.assign(bead="mr-20", to="crew/carol", rig="myrepo")
    work.claim(bead="mr-20", as_="crew/carol", rig="myrepo")
    work.abandon(bead="mr-20", rig="myrepo", rm=False)

    # All counters share one mocked instrument, so filter the bead transitions out of the
    # interleaved worktree-event adds by their transition key (the bead id is no longer a metric
    # attr — it rides the verb span via set_bead).
    adds = meter.create_counter.return_value.add.call_args_list
    transitions = [
        c.args[1]["ws.bead.transition"] for c in adds if "ws.bead.transition" in c.args[1]
    ]
    assert transitions == ["assigned", "claimed", "abandoned"]
    assert not any("ws.bead" in c.args[1] for c in adds)  # bead id never on a metric point
    otel._instruments.clear()  # don't leak mocked instruments into later tests


def test_lifecycle_transitions_are_noop_when_otel_off(rig, fakebd):
    # Default/off path: the verbs run unchanged and cache no instrument (zero-cost no-op).
    otel._instruments.clear()
    fakebd.seed("mr-21", title="t")
    work.assign(bead="mr-21", to="crew/carol", rig="myrepo")
    work.claim(bead="mr-21", as_="crew/carol", rig="myrepo")
    work.abandon(bead="mr-21", rig="myrepo", rm=False)
    assert fakebd.beads["mr-21"]["status"] == "open"  # abandon reopened it — behavior intact
    assert otel._instruments == {}  # nothing cached on the off-path


# ---- worktree lifecycle events (ws.worktree.events) -------------------------
#
# create (worktree.add → _do_add chokepoint) / remove / prune each emit a ws.worktree.events
# counter tagged op + outcome + ws.rig/ws.worktree; off, they emit nothing. The ephemeral verify-
# clean-checkout worktrees (not a seat) are excluded.


def test_worktree_create_remove_prune_emit_events_when_on(rig, fakebd, monkeypatch):
    events = []
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(
        otel,
        "record_worktree_event",
        lambda op, outcome="ok", attrs=None: events.append((op, attrs)),
    )

    worktree.add(rig="myrepo", bead="wt-1")
    worktree.remove("myrepo", "wt-1", force=True)
    worktree.add(rig="myrepo", bead="wt-2")
    worktree.prune(rig="myrepo")

    assert [op for op, _ in events] == ["create", "remove", "create", "prune"]
    assert all(a.get("ws.rig") == "mr" for _, a in events)  # rig tagged on every event
    assert events[0][1]["ws.worktree"] == "wt-1"  # create tags the leaf
    assert events[1][1]["ws.worktree"] == "wt-1"  # remove tags the leaf
    assert events[3][1]["ws.worktree"] == "wt-2"  # prune tags the leaf


def test_worktree_events_are_noop_when_otel_off(rig, fakebd, monkeypatch):
    monkeypatch.setattr(
        otel, "record_worktree_event", MagicMock(side_effect=AssertionError("no event when off"))
    )
    # Off by default: the create/remove/prune seams must never reach the emitter.
    worktree.add(rig="myrepo", bead="wt-3")
    worktree.remove("myrepo", "wt-3", force=True)
    worktree.prune(rig="myrepo")  # reached here → off-path emitted nothing


def test_record_wt_event_excludes_verify_leaf(monkeypatch):
    monkeypatch.setattr(otel, "_initialized", True)
    calls = []
    monkeypatch.setattr(otel, "record_worktree_event", lambda *a, **k: calls.append((a, k)))
    worktree._record_wt_event("prune", rig="mr", leaf="verify-ag-1")
    assert calls == []  # ephemeral verify- clean-checkout worktree is not a seat → no event
    worktree._record_wt_event("prune", rig="mr", leaf="ag-1")
    assert len(calls) == 1  # a real seat emits


def test_record_wt_event_never_raises_on_emitter_failure(monkeypatch):
    # Best-effort: a telemetry failure must never propagate out and block the worktree op.
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(
        otel, "record_worktree_event", MagicMock(side_effect=RuntimeError("exporter down"))
    )
    worktree._record_wt_event("create", rig="mr", leaf="ag-1")  # must not raise


# ---- worktree op duration + real error outcomes (hqfy.3) -------------------


def test_worktree_create_remove_prune_emit_op_duration_when_on(rig, fakebd, monkeypatch):
    """create/remove/prune each emit ws.worktree.op.duration tagged op + outcome=ok + ws.rig."""
    durations = []
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(
        otel, "record_worktree_op_duration", lambda seconds, attrs=None: durations.append(attrs)
    )

    worktree.add(rig="myrepo", bead="wt-1")
    worktree.remove("myrepo", "wt-1", force=True)
    worktree.add(rig="myrepo", bead="wt-2")
    worktree.prune(rig="myrepo")

    assert [a["ws.worktree.op"] for a in durations] == ["create", "remove", "create", "prune"]
    assert all(a["ws.worktree.outcome"] == "ok" for a in durations)
    assert all(a.get("ws.rig") == "mr" for a in durations)
    assert durations[0]["ws.worktree"] == "wt-1"  # leaf tagged like the events counter


def test_worktree_op_duration_noop_when_off(rig, fakebd, monkeypatch):
    monkeypatch.setattr(
        otel,
        "record_worktree_op_duration",
        MagicMock(side_effect=AssertionError("no duration when off")),
    )
    worktree.add(rig="myrepo", bead="wt-3")  # off by default → the seam never reaches the emitter
    worktree.remove("myrepo", "wt-3", force=True)
    worktree.prune(rig="myrepo")


def test_worktree_create_failure_records_error_then_reraises(rig, fakebd, monkeypatch):
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
        worktree.add(rig="myrepo", bead="wt-err")

    assert events == [("create", "error", {"ws.rig": "mr", "ws.worktree": "wt-err"})]
    assert durations == [
        {"ws.worktree.op": "create", "ws.worktree.outcome": "error",
         "ws.rig": "mr", "ws.worktree": "wt-err"}
    ]


def test_record_wt_op_duration_excludes_verify_leaf(monkeypatch):
    monkeypatch.setattr(otel, "_initialized", True)
    calls = []
    monkeypatch.setattr(otel, "record_worktree_op_duration", lambda *a, **k: calls.append((a, k)))
    worktree._record_wt_op_duration("create", 0.1, rig="mr", leaf="verify-ag-1")
    assert calls == []  # ephemeral verify- clean-checkout worktree is not a seat → no duration
    worktree._record_wt_op_duration("create", 0.1, rig="mr", leaf="ag-1")
    assert len(calls) == 1


def test_record_wt_op_duration_never_raises_on_emitter_failure(monkeypatch):
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(
        otel,
        "record_worktree_op_duration",
        MagicMock(side_effect=RuntimeError("exporter down")),
    )
    worktree._record_wt_op_duration("create", 0.1, rig="mr", leaf="ag-1")  # must not raise


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


# ---- union conflict resolution tier ----------------------------------------
#
# Two beads each write different content to the same whitelisted file from an empty base.
# The second bead's plain merge AND its rebase-retry both conflict — the union tier then
# resolves it by keeping both sides, and the success message surfaces how="union".
# Without union_globs configured, the same real-conflict scenario fails cleanly (unchanged
# behavior exercised by the existing divergent-conflict test above).


def test_merge_via_union_tier_when_configured(rig, fakebd, capsys):
    """With union_globs matching the conflicted file, the second bead lands via the union tier:
    both beads' content is present, the bead is closed, and the success message mentions union."""
    rig.cfg_path.write_text(CONFIG_YAML_WITH_UNION)
    # seed an empty notes.txt on the integration branch so both beads start from the same base
    (rig.main / "notes.txt").write_text("")
    _git("add", "-A", cwd=rig.main)
    _git("commit", "-qm", "chore: add notes.txt", cwd=rig.main)
    fakebd.seed("mr-40", title="t")
    fakebd.seed("mr-41", title="t")
    work.claim(bead="mr-40", as_="", rig="myrepo")
    work.claim(bead="mr-41", as_="", rig="myrepo")
    # each bead writes a different line to notes.txt from an empty base → add-add conflict
    _set_line(_wt(rig, "mr-40"), "noteA\n", fname="notes.txt")
    _set_line(_wt(rig, "mr-41"), "noteB\n", fname="notes.txt")
    work.submit(bead="mr-40", rig="myrepo")
    fakebd.approve("mr-40")
    work.submit(bead="mr-41", rig="myrepo")
    fakebd.approve("mr-41")

    work.merge(bead="mr-40", rig="myrepo", rm=False, molecule=False)
    capsys.readouterr()  # drain mr-40 output
    work.merge(bead="mr-41", rig="myrepo", rm=False, molecule=False)

    out = capsys.readouterr().out
    content = (rig.main / "notes.txt").read_text()
    assert "noteA" in content  # first bead's content preserved
    assert "noteB" in content  # second bead's content landed via union
    assert "union" in out      # success message reflects how="union"
    assert fakebd.beads["mr-41"]["status"] == "closed"
    assert fakebd.did("merge-slot", "release")


def test_merge_no_union_note_when_clean(rig, fakebd, capsys):
    """Without union_globs configured, a clean merge emits no union note in the output."""
    fakebd.seed("mr-42", title="t")
    _take_to_approved(rig, fakebd, "mr-42")
    capsys.readouterr()
    work.merge(bead="mr-42", rig="myrepo", rm=False, molecule=False)
    out = capsys.readouterr().out
    assert "union" not in out
    assert "merged mr-42" in out


# ---- work groups (batch mechanics) -----------------------------------------
#
# A batch = several beads sharing a `batch:<group>` label, handled by ONE agent in ONE shared
# `wt/batch/<group>` worktree, validated + merged ONCE as a single --no-ff bubble (per-bead
# commits preserved inside). `--group <ids>` reads the members' existing labels (8v8.1 data
# model) to resolve the group name. Single-bead behaviour (everything above) stays the default.


def _batch_wt(rig, group):
    """The shared batch worktree dir for a group (leaf is the sanitized group name)."""
    return rig.wts / "github" / "myorg" / "myrepo" / registry.sanitize(group)


def test_claim_group_provisions_one_shared_worktree_and_claims_all(rig, fakebd):
    """Group claim provisions the single wt/batch/<group> worktree (one identity), claims every
    member, and creates NO per-bead worktrees — one agent owns the whole batch."""
    fakebd.seed("mr-1.1", title="a", labels=["batch:samefile"])
    fakebd.seed("mr-1.2", title="b", labels=["batch:samefile"])

    work.claim(bead="", as_="crew/group", group="mr-1.1,mr-1.2", rig="myrepo")

    wt = _batch_wt(rig, "samefile")
    assert wt.exists()
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt).stdout.strip() == "wt/batch/samefile"
    assert _cfg_get(wt, "user.name") == "crew/group"  # one shared identity for the group
    # every member claimed by the one actor → in_progress
    assert fakebd.beads["mr-1.1"]["status"] == "in_progress"
    assert fakebd.beads["mr-1.2"]["status"] == "in_progress"
    assert ("crew/group", ["update", "mr-1.1", "--claim"]) in fakebd.calls
    assert ("crew/group", ["update", "mr-1.2", "--claim"]) in fakebd.calls
    # opt-in: NO per-bead worktrees were created (the whole point of batching)
    assert not _wt_of(rig, "mr-1.1").exists()
    assert not _wt_of(rig, "mr-1.2").exists()


def test_claim_group_refuses_member_without_batch_label(rig, fakebd):
    """A member lacking a batch:<group> label isn't a runnable unit — refuse before provisioning."""
    fakebd.seed("mr-1.1", title="a", labels=["batch:samefile"])
    fakebd.seed("mr-1.2", title="b")  # no batch label
    with pytest.raises(typer.Exit):
        work.claim(bead="", as_="", group="mr-1.1,mr-1.2", rig="myrepo")
    assert not _batch_wt(rig, "samefile").exists()  # refused before any worktree
    assert not fakebd.did("update", "mr-1.1", "--claim")  # no member claimed


def test_claim_group_refuses_mixed_groups(rig, fakebd):
    """Members spanning two batch groups can't share one worktree — refuse."""
    fakebd.seed("mr-1.1", title="a", labels=["batch:alpha"])
    fakebd.seed("mr-1.2", title="b", labels=["batch:beta"])
    with pytest.raises(typer.Exit):
        work.claim(bead="", as_="", group="mr-1.1,mr-1.2", rig="myrepo")


def test_claim_refuses_bead_and_group_together(rig, fakebd):
    fakebd.seed("mr-1.1", title="a", labels=["batch:samefile"])
    with pytest.raises(typer.Exit):
        work.claim(bead="mr-1.1", as_="", group="mr-1.1", rig="myrepo")


def _claim_and_commit_batch(rig, fakebd, group="samefile", epic="mr-1"):
    """Kick off mol/<epic>, claim a two-member batch, and lay down one conventional commit per
    bead in the shared batch worktree. Returns the batch worktree path."""
    _mol_branch(rig, epic)
    fakebd.seed(f"{epic}.1", title="a", parent=epic, labels=[f"batch:{group}"])
    fakebd.seed(f"{epic}.2", title="b", parent=epic, labels=[f"batch:{group}"])
    work.claim(bead="", as_="", group=f"{epic}.1,{epic}.2", rig="myrepo")
    wt = _batch_wt(rig, group)
    _commit(wt, f"feat: {epic}.1 work", fname="a.txt")
    _commit(wt, f"feat: {epic}.2 work", fname="b.txt")
    return wt


def test_merge_group_lands_one_bubble_with_per_bead_commits_and_closes_all(rig, fakebd):
    """merge --group validates once, lands ONE --no-ff bubble into the molecule (per-bead commits
    preserved inside → bisectable), closes every member, and leaves the integration branch alone."""
    _claim_and_commit_batch(rig, fakebd)
    main_before = _git("rev-parse", "main", cwd=rig.main).stdout.strip()

    work.merge(bead="", group="mr-1.1,mr-1.2", rig="myrepo")

    # ONE --no-ff bubble on the molecule branch, subject "merge batch <group>"
    assert _git("log", "-1", "--format=%s", "mol/mr-1", cwd=rig.main).stdout.strip() == (
        "merge batch samefile"
    )
    parents = _git("rev-list", "--parents", "-n", "1", "mol/mr-1", cwd=rig.main).stdout.split()
    assert len(parents) == 3  # merge commit + two parents
    # per-bead commits live INSIDE the one bubble (lossless / bisectable)
    subjects = _git("log", "--format=%s", "mol/mr-1", cwd=rig.main).stdout.split("\n")
    assert "feat: mr-1.1 work" in subjects and "feat: mr-1.2 work" in subjects
    # both members' changes landed
    assert _git("cat-file", "-e", "mol/mr-1:a.txt", cwd=rig.main).returncode == 0
    assert _git("cat-file", "-e", "mol/mr-1:b.txt", cwd=rig.main).returncode == 0
    # every member closed (with the batch reason), integration branch untouched, slot released
    assert fakebd.beads["mr-1.1"]["status"] == "closed"
    assert fakebd.beads["mr-1.2"]["status"] == "closed"
    assert fakebd.did("close", "mr-1.1", "--reason", "merged in batch samefile")
    assert fakebd.did("close", "mr-1.2", "--reason", "merged in batch samefile")
    assert _git("rev-parse", "main", cwd=rig.main).stdout.strip() == main_before
    assert fakebd.did("merge-slot", "acquire") and fakebd.did("merge-slot", "release")


def test_merge_group_relaxed_budget_admits_cohesive_batch(rig, fakebd, monkeypatch):
    """The history budget for a batch is per-bead-commits × members, not the flat single-bead cap:
    with max_commits pinned to 1, a 2-commit batch (which the flat cap would reject) still lands."""
    # the flat single-bead cap (1) rejects the same 2-commit history the relaxed cap (1×2) admits
    assert not work._history_ok(2, ["feat: one", "feat: two"], 1)[0]
    assert work._history_ok(2, ["feat: one", "feat: two"], 2)[0]

    monkeypatch.setattr(config, "max_commits", lambda cfg, entry: 1)
    _claim_and_commit_batch(rig, fakebd)  # two per-bead commits on the batch branch

    work.merge(bead="", group="mr-1.1,mr-1.2", rig="myrepo")  # raises if the cap weren't relaxed

    assert fakebd.beads["mr-1.1"]["status"] == "closed"
    assert fakebd.beads["mr-1.2"]["status"] == "closed"


def test_merge_group_refuses_open_gate_and_drops_nothing(rig, fakebd):
    """If any member's review gate is still open the batch isn't approved — refuse, leaving the
    molecule untouched and no member closed."""
    _claim_and_commit_batch(rig, fakebd)
    fakebd.gates.append({"id": "g0", "status": "open", "description": "blocks mr-1.2"})
    before = _git("rev-parse", "mol/mr-1", cwd=rig.main).stdout.strip()

    with pytest.raises(typer.Exit):
        work.merge(bead="", group="mr-1.1,mr-1.2", rig="myrepo")

    assert _git("rev-parse", "mol/mr-1", cwd=rig.main).stdout.strip() == before
    assert fakebd.beads["mr-1.1"]["status"] != "closed"
    assert fakebd.beads["mr-1.2"]["status"] != "closed"


def test_merge_group_rm_removes_shared_worktree(rig, fakebd):
    _claim_and_commit_batch(rig, fakebd)
    assert _batch_wt(rig, "samefile").exists()
    work.merge(bead="", group="mr-1.1,mr-1.2", rig="myrepo", rm=True)
    assert not _batch_wt(rig, "samefile").exists()


# ---- review (merger/reviewer walkthrough packet) ---------------------------


def test_review_molecule_aggregates_intent_and_change(rig, fakebd, capsys):
    """Molecule review: epic brief + every child's acceptance + the mol/<epic> change vs main."""
    _land_two_bead_molecule(rig, fakebd, "mr-1")
    fakebd.beads["mr-1"]["title"] = "the epic"
    fakebd.beads["mr-1.1"]["acceptance_criteria"] = "accept one"
    fakebd.beads["mr-1.2"]["acceptance_criteria"] = "accept two"

    work.review(bead="mr-1", run_validate=False, demo=False, view=["stat"], rig="myrepo")
    out = capsys.readouterr().out

    assert "# mr-1  the epic" in out
    assert "## Molecule children (2)" in out
    assert "accept one" in out and "accept two" in out
    assert "## Change (mol/mr-1 vs main)" in out
    assert "change.txt" in out  # the child merges show up in the stat view


def test_review_bead_mode_shows_brief_and_change(rig, fakebd, capsys):
    """A bead with no mol/<id> branch reviews wt/bead/<id> against the integration base."""
    fakebd.seed("mr-5", title="lone bead", description="do the thing")
    work.claim(bead="mr-5", as_="", rig="myrepo")
    _commit(_wt_of(rig, "mr-5"), "feat: mr-5 work")

    work.review(bead="mr-5", run_validate=False, demo=False, view=["log"], rig="myrepo")
    out = capsys.readouterr().out

    assert "# mr-5  lone bead" in out
    assert "do the thing" in out
    assert "feat: mr-5 work" in out


def test_review_run_reports_validate_exit(rig, fakebd, capsys):
    fakebd.seed("mr-5", title="t")
    work.claim(bead="mr-5", as_="", rig="myrepo")
    _commit(_wt_of(rig, "mr-5"), "feat: mr-5")

    work.review(bead="mr-5", run_validate=True, demo=False, view=["log"], rig="myrepo")
    out = capsys.readouterr().out
    assert "## Validation (true)" in out  # CONFIG_YAML validate_cmd
    assert "validate exit 0" in out


def test_review_demo_none_then_runs_when_configured(rig, fakebd, capsys):
    fakebd.seed("mr-5", title="t")
    work.claim(bead="mr-5", as_="", rig="myrepo")
    _commit(_wt_of(rig, "mr-5"), "feat: mr-5")

    # CONFIG_YAML has no demo_cmd → review --demo says so
    work.review(bead="mr-5", run_validate=False, demo=True, view=["log"], rig="myrepo")
    assert "no demo_cmd configured" in capsys.readouterr().out

    # configure demo_cmd → review --demo runs it from a clean checkout
    rig.cfg_path.write_text(
        CONFIG_YAML.replace('validate_cmd: "true"', 'validate_cmd: "true"\n  demo_cmd: "true"')
    )
    work.review(bead="mr-5", run_validate=False, demo=True, view=["log"], rig="myrepo")
    out = capsys.readouterr().out
    assert "## Demo (true)" in out and "demo exit 0" in out
