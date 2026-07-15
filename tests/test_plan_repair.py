"""`ws plan repair` self-checks — idempotent backfill for a hand-assembled molecule.

Mirrors tests/test_plan.py's seam: a real git rig under $GIT_WORKSPACE (so the identity
triplet resolves for real) with `bd` faked by patching the `bd._run` seam. The fake here is
STATEFUL — swarm create / gate create / set-state / label add mutate the served data — so
"repair twice ⇒ second run is a clean no-op" exercises real convergence, not a canned answer.
"""

from __future__ import annotations

import json
import os
from collections import namedtuple
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from beadhive import bd as bd_mod
from beadhive import plan
from beadhive.cli import app
from beadhive.run import run as real_run

_runner = CliRunner()

_CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
_CP = namedtuple("CP", "returncode stdout stderr")

CONFIG_YAML = """\
providers: [github]
managed_repos:
  - {provider: github, org: myorg, repo: myrepo, prefix: mr, kind: personal}
dimensions:
  model: {values: [opus, sonnet, haiku]}
  harness: {values: [claude, codex]}
  component: {description: open dim}
"""

TRIPLET = ["provider:github", "org:myorg", "repo:myrepo"]


def _git(*args, cwd):
    return real_run(["git", *args], cwd=str(cwd), check=True, capture=True, env=_CLEAN_ENV)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    ws_root = tmp_path / "ws"
    main = ws_root / "github" / "myorg" / "myrepo"
    main.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.email", "human@example.com", cwd=main)
    _git("config", "user.name", "human", cwd=main)
    _git("commit", "--allow-empty", "-m", "init", cwd=main)

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(CONFIG_YAML)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("WS_CONFIG", str(cfg_path))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.delenv("WS_CREW", raising=False)
    return SimpleNamespace(main=main, tmp=tmp_path)


def _child(cid, *, deps=(), labels=(), acceptance="done means done", status="open", type_="task"):
    """A bd-list-shaped child dict: parent-child link to epic-1 + optional sibling blocks deps."""
    dependencies = [
        {"issue_id": cid, "depends_on_id": "epic-1", "type": "parent-child"},
        *({"issue_id": cid, "depends_on_id": d, "type": "blocks"} for d in deps),
    ]
    return {
        "id": cid,
        "title": f"issue {cid}",
        "issue_type": type_,
        "status": status,
        "labels": list(labels),
        "acceptance_criteria": acceptance,
        "dependencies": dependencies,
    }


class FakeBdRepair:
    """Stateful bd stand-in: serves the reads repair/verify make (show, list --parent,
    swarm list, gate list [--all], state) from mutable fields, and APPLIES the writes
    (swarm create, gate create, set-state, label add) to them."""

    def __init__(
        self,
        *,
        epic_type="epic",
        children=None,
        has_swarm=False,
        gates=None,
        kickoff="",
        exists=True,
    ):
        self.calls = []
        self.epic_type = epic_type
        self.children = children if children is not None else []
        self.has_swarm = has_swarm
        self.gates = list(gates or [])
        self.kickoff = kickoff
        self.exists = exists
        self._n = 0

    # -- helpers ---------------------------------------------------------
    def did(self, *needles):
        return any(all(n in args for n in needles) for args in self.calls)

    def mutations(self):
        """The mutating bd calls recorded (create/resolve/set-state/label writes)."""
        heads = {("swarm", "create"), ("gate", "create"), ("gate", "resolve")}
        out = []
        for args in self.calls:
            if tuple(args[:2]) in heads or args[:1] in (["set-state"], ["label"]):
                out.append(args)
        return out

    def _flag(self, args, flag):
        return args[args.index(flag) + 1] if flag in args else ""

    # -- the seam ---------------------------------------------------------
    def __call__(self, cmd, *, check=True, capture=False, env=None, cwd=None, text_input=None):
        if not cmd or cmd[0] != "bd":
            return real_run(
                cmd, check=check, capture=capture, env=env, cwd=cwd, text_input=text_input
            )
        args = list(cmd[1:])
        while args and args[0] in ("-C", "--actor"):
            args = args[2:]
        self.calls.append(list(args))
        args = [a for a in args if a != "--json"]

        if args[:1] == ["show"]:
            if not self.exists:
                return _CP(1, "", "not found")
            epic = {
                "id": "epic-1",
                "title": "Hand-built",
                "issue_type": self.epic_type,
                "description": "assembled by hand",
            }
            return _CP(0, json.dumps([epic]) + "\n", "")
        if args[:1] == ["list"] and "--parent" in args:
            return _CP(0, json.dumps(self.children) + "\n", "")
        if args[:2] == ["swarm", "list"]:
            swarms = [{"epic_id": "epic-1"}] if self.has_swarm else []
            return _CP(0, json.dumps({"schema_version": 1, "swarms": swarms}) + "\n", "")
        if args[:2] == ["swarm", "create"]:
            self.has_swarm = True
            return _CP(0, "", "")
        if args[:2] == ["gate", "list"]:
            gates = (
                self.gates
                if "--all" in args
                else [g for g in self.gates if g.get("status") == "open"]
            )
            return _CP(0, json.dumps(gates) + "\n", "")
        if args[:2] == ["gate", "create"]:
            self._n += 1
            root = self._flag(args, "--blocks")
            reason = self._flag(args, "--reason")
            self.gates.append(
                {
                    "id": f"g-{self._n}",
                    "status": "open",
                    "description": f"Ad-hoc gate blocking {root}\n\nReason: {reason}",
                }
            )
            return _CP(0, "", "")
        if args[:2] == ["gate", "resolve"]:
            for g in self.gates:
                if g["id"] == args[2]:
                    g["status"] = "closed"
            return _CP(0, "", "")
        if args[:1] == ["state"]:
            return _CP(0, self.kickoff + "\n", "")
        if args[:1] == ["set-state"]:
            self.kickoff = args[2].split("=", 1)[1]
            return _CP(0, "", "")
        if args[:2] == ["label", "add"]:
            cid, label = args[2], args[3]
            for child in self.children:
                if child["id"] == cid and label not in child["labels"]:
                    child["labels"].append(label)
            return _CP(0, "", "")
        return _CP(0, "", "")


def _patch(monkeypatch, fake):
    monkeypatch.setattr(bd_mod, "_run", fake)
    return fake


def _repair(rig, monkeypatch, fake):
    _patch(monkeypatch, fake)
    return _runner.invoke(app, ["plan", "repair", "epic-1", "--rig", "myrepo"])


# ---- mount ---------------------------------------------------------------


def test_plan_repair_mounted():
    """`bh plan repair --help` exits 0 — the verb rides plan.app despite living in its own
    module (plan.py bottom mount)."""
    result = _runner.invoke(app, ["plan", "repair", "--help"])
    assert result.exit_code == 0, result.output
    assert "backfill" in result.output


# ---- backfill: each convention -------------------------------------------


def test_repair_backfills_swarm_gates_state_and_labels(rig, monkeypatch):
    """A fully hand-assembled epic (no swarm, no gates, unset kickoff, unlabeled children)
    is converged in one run: swarm created, each root gated via the shared contract,
    kickoff=pending set, identity triplet backfilled — and verify then passes (exit 0)."""
    fake = FakeBdRepair(children=[_child("epic-1.1"), _child("epic-1.2", deps=["epic-1.1"])])
    result = _repair(rig, monkeypatch, fake)

    assert result.exit_code == 0, result.output
    assert fake.did("swarm", "create", "epic-1")
    # only the genuine root is gated; the description carries the kickoff marker
    assert fake.did("gate", "create", "--blocks", "epic-1.1", "--reason", "kickoff epic-1")
    assert not fake.did("gate", "create", "--blocks", "epic-1.2")
    assert fake.did("set-state", "epic-1", "kickoff=pending")
    for label in TRIPLET:
        assert fake.did("label", "add", "epic-1.1", label)
        assert fake.did("label", "add", "epic-1.2", label)
    assert "✓ repaired epic-1" in result.output


def test_repair_gate_rides_shared_contract(rig, monkeypatch):
    """The gate repair creates satisfies _check_kickoff_gates — proof file and repair share
    one authoritative description contract (no format drift possible)."""
    fake = FakeBdRepair(
        children=[_child("epic-1.1", labels=TRIPLET)], has_swarm=True, kickoff="pending"
    )
    result = _repair(rig, monkeypatch, fake)
    assert result.exit_code == 0, result.output
    assert plan._check_kickoff_gates("epic-1", [{"handle": "epic-1.1", "deps": []}], "unused") == []


def test_repair_is_idempotent_clean_noop(rig, monkeypatch):
    """Re-running repair on the converged molecule mutates nothing and reports the no-op."""
    fake = FakeBdRepair(children=[_child("epic-1.1"), _child("epic-1.2", deps=["epic-1.1"])])
    first = _repair(rig, monkeypatch, fake)
    assert first.exit_code == 0, first.output

    before = len(fake.mutations())
    second = _runner.invoke(app, ["plan", "repair", "epic-1", "--rig", "myrepo"])
    assert second.exit_code == 0, second.output
    assert len(fake.mutations()) == before, "second run must not mutate"
    assert "nothing to repair" in second.output


def test_repair_preserves_existing_state_and_swarm(rig, monkeypatch):
    """Already-present plumbing is left alone: pending/approved kickoff is not restamped and
    an existing swarm is not recreated (partial backfill only fills the gaps)."""
    fake = FakeBdRepair(
        children=[_child("epic-1.1", labels=TRIPLET)],
        has_swarm=True,
        kickoff="approved",
    )
    result = _repair(rig, monkeypatch, fake)
    assert result.exit_code == 0, result.output
    assert not fake.did("swarm", "create")
    assert not fake.did("set-state")
    assert fake.did("gate", "create", "--blocks", "epic-1.1")


def test_repair_missing_epic_aborts(rig, monkeypatch):
    """A nonexistent epic aborts with the retrieval error (exit 1)."""
    fake = FakeBdRepair(exists=False)
    result = _repair(rig, monkeypatch, fake)
    assert result.exit_code == 1
    assert "could not retrieve epic" in result.output


def test_repair_non_epic_refuses(rig, monkeypatch):
    """repair backfills molecule plumbing only — a task-typed bead is refused, not mutated."""
    fake = FakeBdRepair(epic_type="task", children=[_child("epic-1.1")])
    result = _repair(rig, monkeypatch, fake)
    assert result.exit_code == 1
    assert "not an epic" in result.output
    assert fake.mutations() == []


def test_repair_surfaces_unfixable_problems(rig, monkeypatch):
    """Problems repair cannot backfill (e.g. a child missing acceptance) still exit 1 with the
    verify problem list, after the fixable plumbing was applied."""
    fake = FakeBdRepair(children=[_child("epic-1.1", acceptance="")])
    result = _repair(rig, monkeypatch, fake)
    assert result.exit_code == 1
    assert fake.did("swarm", "create", "epic-1")  # fixable parts still applied
    assert "acceptance" in result.output
    assert "problem(s) remain" in result.output
