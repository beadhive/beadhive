"""`ws plan` self-checks — the mount point plus the `file` compiler (spec → swarm).

Two layers:
  * skeleton smoke-tests — the module/Typer app import and `ws plan --help` mount;
  * `file` tests — a real git rig under $GIT_WORKSPACE (so the identity triplet resolves
    for real) with `bd` faked by patching `ws.plan.run` (the module's only subprocess seam,
    mirroring tests/test_work.py's FakeBd). `create --silent` returns a synthetic id so the
    handle→id mapping + `--deps` wiring are exercised end to end.
"""

from __future__ import annotations

import json
import os
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from ws import plan
from ws.cli import app
from ws.run import run as real_run

_runner = CliRunner()

_CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
_CP = namedtuple("CP", "returncode stdout stderr")

CONFIG_YAML = """\
providers: [github]
managed_repos:
  - {provider: github, org: myorg, repo: myrepo, prefix: mr, kind: personal}
"""


# ---- import / attribute -------------------------------------------------


def test_plan_app_exists():
    """plan.app is a Typer instance (not None, not a plain function)."""
    import typer

    assert isinstance(plan.app, typer.Typer)


def test_plan_bd_helpers_exist():
    """_bd and _bd_json are callable — later verbs depend on them."""
    assert callable(plan._bd)
    assert callable(plan._bd_json)


# ---- CLI mount ----------------------------------------------------------


def test_ws_plan_help():
    """`ws plan --help` exits 0 and mentions the planning-plane description."""
    result = _runner.invoke(app, ["plan", "--help"])
    assert result.exit_code == 0, result.output
    assert "plan" in result.output.lower()


def test_ws_help_lists_plan():
    """`ws --help` output lists `plan` in the Workspace panel."""
    result = _runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "plan" in result.output


def test_ws_plan_no_args_shows_help():
    """`ws plan` with no subcommand shows help (no_args_is_help=True).

    Typer's no_args_is_help exits 2 via CliRunner (same as --help on an empty
    sub-app); the important thing is that help text is rendered, not an error.
    """
    result = _runner.invoke(app, ["plan"])
    # no_args_is_help renders the help panel — check the text, not the exit code
    assert "plan" in result.output.lower()
    assert "Usage" in result.output


# ---- file: fixtures + fake bd -------------------------------------------


def _git(*args, cwd):
    return real_run(["git", *args], cwd=str(cwd), check=True, capture=True, env=_CLEAN_ENV)


class FakeBd:
    """Stand-in for `bd`: records calls, hands `create --silent` a synthetic id, and delegates
    non-`bd` invocations (e.g. the identity-triplet `git`) to the real runner."""

    def __init__(self):
        self.calls = []  # (actor, [args]) for every bd call
        self.created = []  # (new_id, [create-args]) in creation order
        self._n = 0

    def __call__(self, cmd, *, check=True, capture=False, env=None, cwd=None, text_input=None):
        if not cmd or cmd[0] != "bd":
            return real_run(
                cmd, check=check, capture=capture, env=env, cwd=cwd, text_input=text_input
            )
        args = cmd[1:]
        actor = None
        while args and args[0] in ("-C", "--actor"):
            if args[0] == "--actor":
                actor = args[1]
            args = args[2:]
        self.calls.append((actor, args))
        if args and args[0] == "create":
            self._n += 1
            new_id = f"mr-{self._n}"
            self.created.append((new_id, args[1:]))
            return _CP(0, new_id + "\n", "")
        return _CP(0, "", "")

    def did(self, *needles):
        """True iff some recorded call's args contain all needle tokens."""
        return any(all(n in args for n in needles) for _actor, args in self.calls)

    def create_args(self, *, title):
        """The create-arg list for the issue whose title positional matches `title`."""
        for _id, args in self.created:
            if args and args[0] == title:
                return args
        return None


@pytest.fixture
def rig(tmp_path, monkeypatch):
    ws_root = tmp_path / "ws"
    main = ws_root / "github" / "myorg" / "myrepo"
    main.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.email", "human@example.com", cwd=main)
    _git("config", "user.name", "human", cwd=main)
    # An initial commit so the `main` ref exists for the rig clone.
    _git("commit", "--allow-empty", "-m", "init", cwd=main)

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(CONFIG_YAML)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("WS_CONFIG", str(cfg_path))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.delenv("WS_CREW", raising=False)
    return SimpleNamespace(main=main, tmp=tmp_path)


@pytest.fixture
def fakebd(monkeypatch):
    fb = FakeBd()
    monkeypatch.setattr(plan, "run", fb)
    return fb


def _write_spec(rig) -> Path:
    """A small valid molecule: epic + root issue 'a' + dependent issue 'b' (deps: [a])."""
    spec = rig.tmp / "mol.yaml"
    spec.write_text(
        "epic:\n"
        "  title: Add widgets\n"
        "  description: why\n"
        "  design: arch\n"
        "issues:\n"
        "  - handle: a\n"
        "    title: scaffold\n"
        "    type: feature\n"
        "    priority: 1\n"
        "    acceptance: module exists\n"
        "    design: des-a\n"
        "    component: runtime\n"
        "    deps: []\n"
        "  - handle: b\n"
        "    title: wire it\n"
        "    type: task\n"
        "    acceptance: wired up\n"
        "    deps: [a]\n"
    )
    return spec


# ---- file: dry-run creates nothing --------------------------------------


def test_file_dry_run_creates_nothing(rig, fakebd):
    spec = _write_spec(rig)
    plan.file(spec=str(spec), dry_run=True, save="", rig="myrepo")
    assert fakebd.calls == []  # no bd subprocess at all → nothing mutated
    assert fakebd.created == []


def test_file_dry_run_save_writes_spec(rig, fakebd):
    spec = _write_spec(rig)
    out = rig.tmp / "audit" / "saved.yaml"
    plan.file(spec=str(spec), dry_run=True, save=str(out), rig="myrepo")
    assert out.exists()
    assert "Add widgets" in out.read_text()
    assert fakebd.calls == []  # --save on a dry-run still makes no bd calls


def test_file_invalid_spec_aborts(rig, fakebd):
    bad = rig.tmp / "bad.yaml"
    bad.write_text("epic: {title: E}\nissues:\n  - {handle: a, title: t}\n")  # missing acceptance
    with pytest.raises(typer.Exit):
        plan.file(spec=str(bad), dry_run=False, save="", rig="myrepo")
    assert fakebd.created == []  # validation fails before any create


# ---- file: real run wires epic + children + deps + labels + gate + state -


def test_file_creates_full_swarm(rig, fakebd):
    spec = _write_spec(rig)
    plan.file(spec=str(spec), dry_run=False, save="", rig="myrepo")

    # epic first, then issues in dependency order (a before b) → mr-1, mr-2, mr-3
    epic_args = fakebd.create_args(title="Add widgets")
    assert epic_args is not None and "--type=epic" in epic_args
    assert fakebd.created[0][0] == "mr-1"  # epic is the first create

    # children parented to the epic; identity triplet injected onto every issue
    triplet = "provider:github,org:myorg,repo:myrepo"
    a_args = fakebd.create_args(title="scaffold")
    assert "--parent" in a_args and a_args[a_args.index("--parent") + 1] == "mr-1"
    assert "--acceptance" in a_args  # accuracy field carried (--graph drops it; per-issue keeps)
    assert any(triplet in tok and "component:runtime" in tok for tok in a_args)

    # dependent issue b carries --deps pointing at a's real id (mr-2)
    b_args = fakebd.create_args(title="wire it")
    assert "--deps" in b_args and b_args[b_args.index("--deps") + 1] == "mr-2"

    # swarm built on the epic; kickoff gate blocks the ROOT (a=mr-2), not the dependent
    assert fakebd.did("swarm", "create", "mr-1")
    assert fakebd.did("gate", "create", "--blocks", "mr-2")
    assert not fakebd.did("gate", "create", "--blocks", "mr-3")
    assert fakebd.did("set-state", "mr-1", "kickoff=pending")


def test_file_carries_batch_membership_to_filed_beads(rig, fakebd):
    """A batch:<group> declared in the spec lands as a label on the filed bead."""
    spec = rig.tmp / "batch.yaml"
    spec.write_text(
        "epic:\n"
        "  title: Add widgets\n"
        "issues:\n"
        "  - handle: a\n"
        "    title: scaffold\n"
        "    acceptance: exists\n"
        "    component: runtime\n"
        "    batch: same-file\n"
        "    deps: []\n"
        "  - handle: b\n"
        "    title: extend\n"
        "    acceptance: works\n"
        "    component: runtime\n"
        "    batch: same-file\n"
        "    deps: [a]\n"
    )
    plan.file(spec=str(spec), dry_run=False, save="", rig="myrepo")
    a_args = fakebd.create_args(title="scaffold")
    assert any("batch:same-file" in tok for tok in a_args)
    b_args = fakebd.create_args(title="extend")
    assert any("batch:same-file" in tok for tok in b_args)


def test_file_save_writes_spec(rig, fakebd):
    spec = _write_spec(rig)
    out = rig.tmp / "saved.yaml"
    plan.file(spec=str(spec), dry_run=False, save=str(out), rig="myrepo")
    assert out.exists() and "Add widgets" in out.read_text()


# ---- check: standalone validation ------------------------------------------


def test_check_valid_spec_exits_zero(rig):
    """check exits 0 and prints '✓ valid' for a well-formed spec."""
    spec = _write_spec(rig)
    result = _runner.invoke(app, ["plan", "check", str(spec), "--rig", "myrepo"])
    assert result.exit_code == 0, result.output
    assert "✓ valid" in result.output


def test_check_invalid_spec_exits_nonzero_and_prints_problems(rig):
    """check exits non-zero and prints each validation problem for a bad spec."""
    bad = rig.tmp / "bad.yaml"
    bad.write_text(
        "epic:\n"
        "  title: E\n"
        "issues:\n"
        "  - handle: a\n"
        "    title: t\n"
    )  # missing acceptance
    result = _runner.invoke(app, ["plan", "check", str(bad), "--rig", "myrepo"])
    assert result.exit_code != 0
    assert "acceptance" in result.output


# ---- approve: fixtures + fake bd ----------------------------------------


class FakeBdApprove(FakeBd):
    """Extends FakeBd for approve tests: configurable `bd state` + `bd gate list` responses."""

    def __init__(self, kickoff_state="pending", gates=None):
        super().__init__()
        self.kickoff_state = kickoff_state
        self._gates = gates or []

    def __call__(self, cmd, *, check=True, capture=False, env=None, cwd=None, text_input=None):
        if not cmd or cmd[0] != "bd":
            return real_run(
                cmd, check=check, capture=capture, env=env, cwd=cwd, text_input=text_input
            )
        args = list(cmd[1:])
        actor = None
        while args and args[0] in ("-C", "--actor"):
            if args[0] == "--actor":
                actor = args[1]
            args = args[2:]
        self.calls.append((actor, list(args)))

        if args and args[0] == "state":
            # bd state <epic> kickoff → return configured state
            return _CP(0, self.kickoff_state + "\n", "")
        if args and len(args) > 1 and args[0] == "gate" and args[1] == "list":
            # bd gate list --json → return configured gates as JSON
            return _CP(0, json.dumps(self._gates) + "\n", "")
        if args and args[0] == "create":
            self._n += 1
            new_id = f"mr-{self._n}"
            self.created.append((new_id, args[1:]))
            return _CP(0, new_id + "\n", "")
        return _CP(0, "", "")


@pytest.fixture
def fakebd_approve(monkeypatch):
    """Default: kickoff=pending + one open kickoff gate for epic-1."""
    gates = [{"id": "gate-42", "status": "open", "description": "kickoff epic-1"}]
    fb = FakeBdApprove(kickoff_state="pending", gates=gates)
    monkeypatch.setattr(plan, "run", fb)
    return fb


# ---- approve: success cases -------------------------------------------------


def test_approve_resolves_gate_and_sets_state(rig, fakebd_approve):
    """approve resolves the open kickoff gate and sets kickoff=approved on the epic."""
    plan.approve(epic="epic-1", rig="myrepo")

    # state was queried first
    assert fakebd_approve.did("state", "epic-1", "kickoff")
    # gate was resolved
    assert fakebd_approve.did("gate", "resolve", "gate-42")
    # kickoff=approved was set on the epic
    assert fakebd_approve.did("set-state", "epic-1", "kickoff=approved")


def test_approve_does_not_open_mol_branch(rig, fakebd_approve):
    """Plane separation: approve is pure planning — it must NOT create the mol/<epic> branch.
    The integration plane opens it on first start/assign (worktree.ensure_integration_branch)."""
    plan.approve(epic="epic-1", rig="myrepo")
    branches = _git("branch", "--list", "mol/epic-1", cwd=rig.main).stdout.strip()
    assert branches == "", "approve must not open the molecule branch"


def test_approve_resolves_multiple_gates(rig, monkeypatch):
    """When multiple kickoff gates exist (multi-root molecule), all open ones are resolved."""
    gates = [
        {"id": "gate-1", "status": "open", "description": "kickoff epic-x"},
        {"id": "gate-2", "status": "open", "description": "kickoff epic-x"},
        # closed gate for the same epic — must NOT be resolved
        {"id": "gate-3", "status": "closed", "description": "kickoff epic-x"},
    ]
    fb = FakeBdApprove(kickoff_state="pending", gates=gates)
    monkeypatch.setattr(plan, "run", fb)

    plan.approve(epic="epic-x", rig="myrepo")

    assert fb.did("gate", "resolve", "gate-1")
    assert fb.did("gate", "resolve", "gate-2")
    assert not fb.did("gate", "resolve", "gate-3")
    assert fb.did("set-state", "epic-x", "kickoff=approved")


def test_approve_skips_gates_for_other_epics(rig, monkeypatch):
    """Gates belonging to a different epic are not resolved."""
    gates = [
        {"id": "gate-mine", "status": "open", "description": "kickoff epic-target"},
        {"id": "gate-other", "status": "open", "description": "kickoff epic-other"},
    ]
    fb = FakeBdApprove(kickoff_state="pending", gates=gates)
    monkeypatch.setattr(plan, "run", fb)

    plan.approve(epic="epic-target", rig="myrepo")

    assert fb.did("gate", "resolve", "gate-mine")
    assert not fb.did("gate", "resolve", "gate-other")
    assert fb.did("set-state", "epic-target", "kickoff=approved")


# ---- approve: refusal cases -------------------------------------------------


def test_approve_refuses_when_already_approved(rig, monkeypatch):
    """approve exits non-zero when kickoff=approved (not pending)."""
    fb = FakeBdApprove(kickoff_state="approved", gates=[])
    monkeypatch.setattr(plan, "run", fb)

    with pytest.raises(typer.Exit):
        plan.approve(epic="epic-1", rig="myrepo")

    # no gate resolve or state flip after the early guard
    assert not fb.did("gate", "resolve")
    assert not fb.did("set-state", "epic-1", "kickoff=approved")


def test_approve_refuses_when_kickoff_unset(rig, monkeypatch):
    """approve exits non-zero when kickoff state is unset (empty string)."""
    fb = FakeBdApprove(kickoff_state="", gates=[])
    monkeypatch.setattr(plan, "run", fb)

    with pytest.raises(typer.Exit):
        plan.approve(epic="epic-3", rig="myrepo")

    assert not fb.did("set-state", "epic-3", "kickoff=approved")


def test_approve_refuses_when_no_open_gates(rig, monkeypatch):
    """approve exits non-zero when kickoff=pending but no open kickoff gates exist."""
    # Only a closed gate — nothing open to resolve
    gates = [{"id": "gate-99", "status": "closed", "description": "kickoff epic-2"}]
    fb = FakeBdApprove(kickoff_state="pending", gates=gates)
    monkeypatch.setattr(plan, "run", fb)

    with pytest.raises(typer.Exit):
        plan.approve(epic="epic-2", rig="myrepo")

    assert not fb.did("gate", "resolve")
    assert not fb.did("set-state", "epic-2", "kickoff=approved")


# ---- show: FakeBd for bd show + bd list --parent ----------------------------


_FILED_CHILDREN = [
    {
        "id": "epic-1.1",
        "title": "scaffold",
        "issue_type": "feature",
        "status": "open",
        "labels": ["component:runtime", "model:sonnet"],
        "acceptance_criteria": "module exists",
        "dependencies": [
            {
                "issue_id": "epic-1.1",
                "depends_on_id": "epic-1",
                "type": "parent-child",
                "created_at": "",
                "created_by": "",
                "metadata": "{}",
            },
        ],
    },
    {
        "id": "epic-1.2",
        "title": "wire it",
        "issue_type": "task",
        "status": "open",
        "labels": ["model:opus"],
        "acceptance_criteria": "wired up",
        "dependencies": [
            {
                "issue_id": "epic-1.2",
                "depends_on_id": "epic-1",
                "type": "parent-child",
                "created_at": "",
                "created_by": "",
                "metadata": "{}",
            },
            {
                "issue_id": "epic-1.2",
                "depends_on_id": "epic-1.1",
                "type": "blocks",
                "created_at": "",
                "created_by": "",
                "metadata": "{}",
            },
        ],
    },
]


class FakeBdShow(FakeBd):
    """Extends FakeBd for show tests: serves `bd show <epic>` + `bd list --parent <epic>`."""

    def __init__(self, epic_id: str, epic_title: str, children: list):
        super().__init__()
        self._epic_id = epic_id
        self._epic_title = epic_title
        self._children = children

    def __call__(self, cmd, *, check=True, capture=False, env=None, cwd=None, text_input=None):
        if not cmd or cmd[0] != "bd":
            return real_run(
                cmd, check=check, capture=capture, env=env, cwd=cwd, text_input=text_input
            )
        args = list(cmd[1:])
        actor = None
        while args and args[0] in ("-C", "--actor"):
            if args[0] == "--actor":
                actor = args[1]
            args = args[2:]
        self.calls.append((actor, list(args)))

        if args and args[0] == "show" and self._epic_id in args:
            epic = {
                "id": self._epic_id,
                "title": self._epic_title,
                "issue_type": "epic",
                "description": "",
            }
            return _CP(0, json.dumps([epic]) + "\n", "")
        if args and args[0] == "list" and "--parent" in args:
            return _CP(0, json.dumps(self._children) + "\n", "")
        return _CP(0, "", "")


# ---- show: from spec --------------------------------------------------------


def test_show_from_spec_renders_epic_issues_and_roots(rig):
    """ws plan show <spec> renders the epic title, issues in topo order, and root set."""
    spec = _write_spec(rig)
    result = _runner.invoke(app, ["plan", "show", str(spec), "--rig", "myrepo"])
    assert result.exit_code == 0, result.output
    # Header identifies source as spec
    assert "from spec" in result.output
    # Epic title present
    assert "Add widgets" in result.output
    # Both issues rendered
    assert "scaffold" in result.output
    assert "wire it" in result.output
    # Topo order: a (no deps) before b (depends on a)
    assert result.output.index("scaffold") < result.output.index("wire it")
    # Root set rendered
    assert "roots" in result.output


def test_show_from_spec_shows_labels_and_deps(rig):
    """ws plan show shows dimension labels and dep handles for each issue."""
    spec = _write_spec(rig)
    result = _runner.invoke(app, ["plan", "show", str(spec), "--rig", "myrepo"])
    assert result.exit_code == 0, result.output
    # Issue 'a' carries component:runtime label from spec
    assert "component:runtime" in result.output
    # Issue 'b' has a dep listed
    assert "deps" in result.output


# ---- show: from epic (filed) ------------------------------------------------


def test_show_from_epic_renders_filed_molecule(rig, monkeypatch):
    """ws plan show <epic_id> renders the filed molecule from beads (round-trip view)."""
    fb = FakeBdShow("epic-1", "Add widgets", _FILED_CHILDREN)
    monkeypatch.setattr(plan, "run", fb)

    result = _runner.invoke(app, ["plan", "show", "epic-1", "--rig", "myrepo"])
    assert result.exit_code == 0, result.output
    # Header identifies source as beads
    assert "from beads (filed)" in result.output
    # Epic title present
    assert "Add widgets" in result.output
    # Both issues rendered
    assert "scaffold" in result.output
    assert "wire it" in result.output
    # Topo order: epic-1.1 (root) before epic-1.2 (depends on epic-1.1)
    assert result.output.index("scaffold") < result.output.index("wire it")
    # Dimension labels visible (triplet labels suppressed)
    assert "model:sonnet" in result.output
    # wire it's dep on scaffold is shown
    assert "epic-1.1" in result.output
    # Root set rendered
    assert "roots" in result.output


# ---- status: FakeBd + fixtures -----------------------------------------------

_SWARMS_LIST = {
    "schema_version": 1,
    "swarms": [
        {
            "id": "sw-1",
            "epic_id": "epic-1",
            "epic_title": "Feature Alpha",
            "completed_issues": 2,
            "total_issues": 5,
            "progress_percent": 40,
            "status": "open",
        },
        {
            "id": "sw-2",
            "epic_id": "epic-2",
            "epic_title": "Feature Beta",
            "completed_issues": 0,
            "total_issues": 3,
            "progress_percent": 0,
            "status": "open",
        },
    ],
}

_SWARM_STATUS_EPIC1 = {
    "epic_id": "epic-1",
    "epic_title": "Feature Alpha",
    "total_issues": 5,
    "progress_percent": 40,
    "completed": [{"id": "epic-1.1"}, {"id": "epic-1.2"}],
    "active": [{"id": "epic-1.3"}],
    "ready": [{"id": "epic-1.4"}],
    "blocked": [{"id": "epic-1.5"}],
    "active_count": 1,
    "ready_count": 1,
    "blocked_count": 1,
}


class FakeBdStatus(FakeBd):
    """Extends FakeBd for status tests: serves swarm list, swarm status, and state queries."""

    def __init__(self, swarms_list=None, swarm_status_by_epic=None, kickoff_by_epic=None):
        super().__init__()
        self._swarms_list = swarms_list or {"schema_version": 1, "swarms": []}
        self._swarm_status_by_epic = swarm_status_by_epic or {}
        self._kickoff_by_epic = kickoff_by_epic or {}

    def __call__(self, cmd, *, check=True, capture=False, env=None, cwd=None, text_input=None):
        if not cmd or cmd[0] != "bd":
            return real_run(
                cmd, check=check, capture=capture, env=env, cwd=cwd, text_input=text_input
            )
        args = list(cmd[1:])
        actor = None
        while args and args[0] in ("-C", "--actor"):
            if args[0] == "--actor":
                actor = args[1]
            args = args[2:]
        self.calls.append((actor, list(args)))

        # bd swarm list [--json]
        if len(args) >= 2 and args[0] == "swarm" and args[1] == "list":
            return _CP(0, json.dumps(self._swarms_list) + "\n", "")
        # bd swarm status <epic> [--json]
        if len(args) >= 3 and args[0] == "swarm" and args[1] == "status":
            epic_id = args[2]
            data = self._swarm_status_by_epic.get(epic_id, {})
            return _CP(0, json.dumps(data) + "\n", "")
        # bd state <epic> kickoff  (plain text, no --json flag)
        if len(args) >= 2 and args[0] == "state":
            epic_id = args[1]
            kickoff = self._kickoff_by_epic.get(epic_id, "")
            return _CP(0, kickoff + "\n", "")
        return _CP(0, "", "")


@pytest.fixture
def fakebd_status(monkeypatch):
    """Two swarms: epic-1 (kickoff=pending) and epic-2 (kickoff=approved)."""
    fb = FakeBdStatus(
        swarms_list=_SWARMS_LIST,
        swarm_status_by_epic={"epic-1": _SWARM_STATUS_EPIC1},
        kickoff_by_epic={"epic-1": "pending", "epic-2": "approved"},
    )
    monkeypatch.setattr(plan, "run", fb)
    return fb


# ---- status: list (no epic arg) -----------------------------------------------


def test_status_list_shows_each_epic_with_kickoff(rig, fakebd_status):
    """ws plan status (no arg) lists all swarms, each with its kickoff column."""
    result = _runner.invoke(app, ["plan", "status", "--rig", "myrepo"])
    assert result.exit_code == 0, result.output
    assert "epic-1" in result.output
    assert "epic-2" in result.output
    assert "Feature Alpha" in result.output
    assert "Feature Beta" in result.output
    assert "pending" in result.output
    assert "approved" in result.output


def test_status_list_shows_progress(rig, fakebd_status):
    """ws plan status (no arg) shows completed/total progress for each swarm."""
    result = _runner.invoke(app, ["plan", "status", "--rig", "myrepo"])
    assert result.exit_code == 0, result.output
    assert "2/5" in result.output


def test_status_list_unset_kickoff_shows_dash(rig, monkeypatch):
    """ws plan status shows — for epics whose kickoff state is unset."""
    fb = FakeBdStatus(
        swarms_list=_SWARMS_LIST,
        kickoff_by_epic={"epic-1": "", "epic-2": ""},
    )
    monkeypatch.setattr(plan, "run", fb)
    result = _runner.invoke(app, ["plan", "status", "--rig", "myrepo"])
    assert result.exit_code == 0, result.output
    assert "—" in result.output


# ---- status: with epic arg ----------------------------------------------------


def test_status_epic_shows_detail_and_kickoff(rig, fakebd_status):
    """ws plan status <epic> shows swarm detail and kickoff state."""
    result = _runner.invoke(app, ["plan", "status", "epic-1", "--rig", "myrepo"])
    assert result.exit_code == 0, result.output
    assert "epic-1" in result.output
    assert "Feature Alpha" in result.output
    assert "kickoff" in result.output
    assert "pending" in result.output


def test_status_epic_shows_active_ready_blocked(rig, fakebd_status):
    """ws plan status <epic> shows active, ready, and blocked issue groups."""
    result = _runner.invoke(app, ["plan", "status", "epic-1", "--rig", "myrepo"])
    assert result.exit_code == 0, result.output
    assert "active" in result.output
    assert "ready" in result.output
    assert "blocked" in result.output
