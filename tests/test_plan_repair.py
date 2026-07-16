"""`ws plan repair` self-checks — idempotent backfill for a hand-assembled molecule.

Mirrors tests/test_plan.py's seam: a real git hive under $GIT_WORKSPACE (so the identity
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
from harness.beads import skip_if_no_bd

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
def hive(tmp_path, monkeypatch):
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


def _repair(hive, monkeypatch, fake):
    _patch(monkeypatch, fake)
    return _runner.invoke(app, ["plan", "repair", "epic-1", "--hive", "myrepo"])


# ---- mount ---------------------------------------------------------------


def test_plan_repair_mounted():
    """`bh plan repair --help` exits 0 — the verb rides plan.app despite living in its own
    module (plan.py bottom mount)."""
    result = _runner.invoke(app, ["plan", "repair", "--help"])
    assert result.exit_code == 0, result.output
    assert "backfill" in result.output


# ---- backfill: each convention -------------------------------------------


def test_repair_backfills_swarm_gates_state_and_labels(hive, monkeypatch):
    """A fully hand-assembled epic (no swarm, no gates, unset kickoff, unlabeled children)
    is converged in one run: swarm created, each root gated via the shared contract,
    kickoff=pending set, identity triplet backfilled — and verify then passes (exit 0)."""
    fake = FakeBdRepair(children=[_child("epic-1.1"), _child("epic-1.2", deps=["epic-1.1"])])
    result = _repair(hive, monkeypatch, fake)

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


def test_repair_gate_rides_shared_contract(hive, monkeypatch):
    """The gate repair creates satisfies _check_kickoff_gates — proof file and repair share
    one authoritative description contract (no format drift possible)."""
    fake = FakeBdRepair(
        children=[_child("epic-1.1", labels=TRIPLET)], has_swarm=True, kickoff="pending"
    )
    result = _repair(hive, monkeypatch, fake)
    assert result.exit_code == 0, result.output
    assert plan._check_kickoff_gates("epic-1", [{"handle": "epic-1.1", "deps": []}], "unused") == []


def test_repair_is_idempotent_clean_noop(hive, monkeypatch):
    """Re-running repair on the converged molecule mutates nothing and reports the no-op."""
    fake = FakeBdRepair(children=[_child("epic-1.1"), _child("epic-1.2", deps=["epic-1.1"])])
    first = _repair(hive, monkeypatch, fake)
    assert first.exit_code == 0, first.output

    before = len(fake.mutations())
    second = _runner.invoke(app, ["plan", "repair", "epic-1", "--hive", "myrepo"])
    assert second.exit_code == 0, second.output
    assert len(fake.mutations()) == before, "second run must not mutate"
    assert "nothing to repair" in second.output


def test_repair_preserves_existing_state_and_swarm(hive, monkeypatch):
    """Already-present plumbing is left alone: pending/approved kickoff is not restamped and
    an existing swarm is not recreated (partial backfill only fills the gaps)."""
    fake = FakeBdRepair(
        children=[_child("epic-1.1", labels=TRIPLET)],
        has_swarm=True,
        kickoff="approved",
    )
    result = _repair(hive, monkeypatch, fake)
    assert result.exit_code == 0, result.output
    assert not fake.did("swarm", "create")
    assert not fake.did("set-state")
    assert fake.did("gate", "create", "--blocks", "epic-1.1")


def test_repair_missing_epic_aborts(hive, monkeypatch):
    """A nonexistent epic aborts with the retrieval error (exit 1)."""
    fake = FakeBdRepair(exists=False)
    result = _repair(hive, monkeypatch, fake)
    assert result.exit_code == 1
    assert "could not retrieve epic" in result.output


def test_repair_non_epic_refuses(hive, monkeypatch):
    """repair backfills molecule plumbing only — a task-typed bead is refused, not mutated."""
    fake = FakeBdRepair(epic_type="task", children=[_child("epic-1.1")])
    result = _repair(hive, monkeypatch, fake)
    assert result.exit_code == 1
    assert "not an epic" in result.output
    assert fake.mutations() == []


def test_repair_surfaces_unfixable_problems(hive, monkeypatch):
    """Problems repair cannot backfill (e.g. a child missing acceptance) still exit 1 with the
    verify problem list, after the fixable plumbing was applied."""
    fake = FakeBdRepair(children=[_child("epic-1.1", acceptance="")])
    result = _repair(hive, monkeypatch, fake)
    assert result.exit_code == 1
    assert fake.did("swarm", "create", "epic-1")  # fixable parts still applied
    assert "acceptance" in result.output
    assert "problem(s) remain" in result.output


# ---- bh-2qbe: the bh-er55 hand-assembly repro + edge cases -------------------
#
# Captured while hand-assembling bh-er55 (an epic built by `bd create --type=epic` +
# `bd dep add <child> <epic> -t parent-child` over pre-existing beads instead of
# `bh plan file`). Two things a naive repair would get wrong:
#   A) origin-report children (intake:untriaged / origin:*) are neither gated nor counted
#      as roots — repair must reuse the same is_origin_report exclusion verify uses;
#   B) identity-label backfill is one `bd label add` per missing field per child.

ORIGIN_LABELS = ["intake:untriaged", "origin:report"]


def _er55_children():
    """The bh-er55 shape, scaled down: 2 genuine roots (one with a partial identity triplet,
    one with none), a dependent chain member, and 2 origin-report children that were never
    triaged (childless — a naive 'every childless child is a root' would over-gate them)."""
    return [
        _child("mr-8", labels=["provider:github"]),
        _child("mr-12"),
        _child("mr-3", deps=["mr-8"], labels=TRIPLET),
        _child("mr-62ex", labels=ORIGIN_LABELS, acceptance=""),
        _child("mr-bwhq", labels=ORIGIN_LABELS, acceptance=""),
    ]


def test_repair_excludes_origin_report_children_from_roots(hive, monkeypatch):
    """Edge case A: origin-report children are NEITHER gated NOR counted as roots — repair
    reuses the same adopt.is_origin_report exclusion _epic_molecule/verify apply, and the
    molecule still verifies clean with them ungated (and without acceptance/triplet)."""
    fake = FakeBdRepair(children=_er55_children())
    result = _repair(hive, monkeypatch, fake)

    assert result.exit_code == 0, result.output
    assert fake.did("gate", "create", "--blocks", "mr-8")
    assert fake.did("gate", "create", "--blocks", "mr-12")
    for origin_child in ("mr-62ex", "mr-bwhq"):
        assert not fake.did("gate", "create", "--blocks", origin_child)
        assert not fake.did("label", "add", origin_child)
    # the dependent member is not a root either
    assert not fake.did("gate", "create", "--blocks", "mr-3")


def test_repair_backfills_only_missing_identity_fields_one_label_per_call(hive, monkeypatch):
    """Edge case B: backfill adds exactly the missing provider/org/repo fields, one label per
    `bd label add` call (bd rejects a multi-label list — it degrades to per-issue-id parsing
    errors), and leaves already-labeled children untouched."""
    fake = FakeBdRepair(children=_er55_children())
    result = _repair(hive, monkeypatch, fake)
    assert result.exit_code == 0, result.output

    label_adds = [args for args in fake.calls if args[:2] == ["label", "add"]]
    # every label-add is exactly ["label", "add", <child>, <one label>]
    assert all(len(args) == 4 for args in label_adds), label_adds
    by_child = {}
    for args in label_adds:
        by_child.setdefault(args[2], []).append(args[3])
    # mr-8 already carried provider: — only org/repo are added
    assert by_child["mr-8"] == ["org:myorg", "repo:myrepo"]
    # mr-12 carried nothing — full triplet
    assert by_child["mr-12"] == ["provider:github", "org:myorg", "repo:myrepo"]
    # mr-3 was fully labeled, origin reports are excluded
    assert set(by_child) == {"mr-8", "mr-12"}


def test_repair_then_approve_twice_each_converges(hive, monkeypatch):
    """The bh-3a8r regression sequence on the bh-er55 shape: `plan repair` twice, then
    `plan approve` twice — repair converges (second run a no-op), approve resolves both
    root gates and flips kickoff=approved (second run a clean no-op), and the molecule
    verifies clean throughout. No raw gate ids, no BH_BD_PASS_ENABLED."""
    fake = FakeBdRepair(children=_er55_children())
    _patch(monkeypatch, fake)

    first = _runner.invoke(app, ["plan", "repair", "epic-1", "--hive", "myrepo"])
    assert first.exit_code == 0, first.output
    second = _runner.invoke(app, ["plan", "repair", "epic-1", "--hive", "myrepo"])
    assert second.exit_code == 0, second.output
    assert "nothing to repair" in second.output

    approve1 = _runner.invoke(app, ["plan", "approve", "epic-1", "--hive", "myrepo"])
    assert approve1.exit_code == 0, approve1.output
    assert "2 gate(s) resolved" in approve1.output
    assert fake.kickoff == "approved"
    assert all(g["status"] == "closed" for g in fake.gates)

    approve2 = _runner.invoke(app, ["plan", "approve", "epic-1", "--hive", "myrepo"])
    assert approve2.exit_code == 0, approve2.output
    assert "already approved" in approve2.output

    verify = _runner.invoke(app, ["plan", "verify", "epic-1", "--hive", "myrepo"])
    assert verify.exit_code == 0, verify.output


# ---- integration: the full bh-vhdf/bh-er55 regression on real bd -------------


@pytest.mark.integration
@skip_if_no_bd
def test_repair_and_approve_converge_hand_assembled_epic_real_bd(world):
    """END-TO-END regression (real bd): hand-assemble an epic exactly like bh-er55 —
    `bd create --type=epic` + `bd dep add <child> <epic> -t parent-child` over pre-existing
    beads (no swarm, no gates, no kickoff state, no identity labels, one untriaged
    origin-report child) — then run `plan repair` twice and `plan approve` twice, and prove
    `bh work start` takes the dispatcher seat."""
    from harness.beads import bd as hbd
    from harness.hive import make_hive

    hive = make_hive(world)
    m = hive.main

    def _create(*args):
        res = hbd("create", *args, "--silent", cwd=m, capture=True)
        return (res.stdout or "").strip().splitlines()[-1].strip()

    epic = _create("cleanup epic", "--type=epic", "-d", "assembled by hand")
    root_a = _create("pre-existing root a", "--acceptance", "a done")
    root_b = _create("pre-existing root b", "--acceptance", "b done")
    dependent = _create("pre-existing dependent", "--acceptance", "c done")
    origin = _create("reported thing", "-l", "origin:report,intake:untriaged")

    for child in (root_a, root_b, dependent, origin):
        hbd("dep", "add", child, epic, "-t", "parent-child", cwd=m, capture=True)
    hbd("dep", "add", dependent, root_a, cwd=m, capture=True)  # dependent is not a root

    # the refusal trail on the malformed molecule: work start points at approve, and approve
    # refuses on the convention gate pointing at repair — the operator never gate-spelunks
    refused_start = _runner.invoke(
        app, ["work", "start", epic, "--as", "disp/tester", "--hive", "mr"]
    )
    assert refused_start.exit_code != 0
    assert "plan approve" in refused_start.output
    refused_approve = _runner.invoke(app, ["plan", "approve", epic, "--hive", "mr"])
    assert refused_approve.exit_code != 0
    assert "plan repair" in refused_approve.output

    # repair twice: converge, then clean no-op
    first = _runner.invoke(app, ["plan", "repair", epic, "--hive", "mr"])
    assert first.exit_code == 0, first.output
    assert f"created bd swarm for {epic}" in first.output
    assert f"created kickoff gate for root {root_a}" in first.output
    assert f"created kickoff gate for root {root_b}" in first.output
    assert origin not in first.output  # origin report: not gated, not labeled
    assert dependent not in [
        line.split()[-1] for line in first.output.splitlines() if "kickoff gate" in line
    ]
    second = _runner.invoke(app, ["plan", "repair", epic, "--hive", "mr"])
    assert second.exit_code == 0, second.output
    assert "nothing to repair" in second.output

    # approve twice: converge, then clean no-op — no raw gate ids anywhere
    approve1 = _runner.invoke(app, ["plan", "approve", epic, "--hive", "mr"])
    assert approve1.exit_code == 0, approve1.output
    assert "2 gate(s) resolved" in approve1.output
    approve2 = _runner.invoke(app, ["plan", "approve", epic, "--hive", "mr"])
    assert approve2.exit_code == 0, approve2.output
    assert "already approved" in approve2.output

    verify = _runner.invoke(app, ["plan", "verify", epic, "--hive", "mr"])
    assert verify.exit_code == 0, verify.output

    # the dispatcher seat now opens
    started = _runner.invoke(app, ["work", "start", epic, "--as", "disp/tester", "--hive", "mr"])
    assert started.exit_code == 0, started.output
    assert f"started {epic}" in started.output
