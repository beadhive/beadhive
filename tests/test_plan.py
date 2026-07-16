"""`ws plan` self-checks — the mount point plus the `file` compiler (spec → swarm).

Two layers:
  * skeleton smoke-tests — the module/Typer app import and `ws plan --help` mount;
  * `file` tests — a real git hive under $GIT_WORKSPACE (so the identity triplet resolves
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

from beadhive import bd as bd_mod
from beadhive import plan, state
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


# ---- import / attribute -------------------------------------------------


def test_plan_app_exists():
    """plan.app is a Typer instance (not None, not a plain function)."""
    import typer

    assert isinstance(plan.app, typer.Typer)


def test_plan_bd_helpers_exist():
    """The bd seam is hoisted to bd.py — plan carries no private _bd / _bd_json copy."""
    from beadhive import bd as bd_mod

    assert callable(bd_mod.run)
    assert callable(bd_mod.json)
    assert not hasattr(plan, "_bd")  # removed — use bd.run directly
    assert not hasattr(plan, "_bd_json")  # removed — use bd.json directly


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
def hive(tmp_path, monkeypatch):
    ws_root = tmp_path / "ws"
    main = ws_root / "github" / "myorg" / "myrepo"
    main.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.email", "human@example.com", cwd=main)
    _git("config", "user.name", "human", cwd=main)
    # An initial commit so the `main` ref exists for the hive clone.
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
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)
    # bd.json uses ws.bd.run — patch it so bd.json calls (show/list/swarm/gate reads)
    # go through the same fake instead of hitting the real bd binary.
    monkeypatch.setattr(bd_mod, "_run", fb)
    return fb


def _write_spec(hive) -> Path:
    """A small valid molecule: epic + root issue 'a' + dependent issue 'b' (deps: [a])."""
    spec = hive.tmp / "mol.yaml"
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


def test_file_dry_run_creates_nothing(hive, fakebd):
    spec = _write_spec(hive)
    plan.file(spec=str(spec), dry_run=True, save="", hive="myrepo")
    assert fakebd.calls == []  # no bd subprocess at all → nothing mutated
    assert fakebd.created == []


def test_file_dry_run_save_writes_spec(hive, fakebd):
    spec = _write_spec(hive)
    out = hive.tmp / "audit" / "saved.yaml"
    plan.file(spec=str(spec), dry_run=True, save=str(out), hive="myrepo")
    assert out.exists()
    assert "Add widgets" in out.read_text()
    assert fakebd.calls == []  # --save on a dry-run still makes no bd calls


def test_file_invalid_spec_aborts(hive, fakebd):
    bad = hive.tmp / "bad.yaml"
    bad.write_text("epic: {title: E}\nissues:\n  - {handle: a, title: t}\n")  # missing acceptance
    with pytest.raises(typer.Exit):
        plan.file(spec=str(bad), dry_run=False, save="", hive="myrepo")
    assert fakebd.created == []  # validation fails before any create


# ---- file: real run wires epic + children + deps + labels + gate + state -


def test_file_creates_full_swarm(hive, fakebd):
    spec = _write_spec(hive)
    plan.file(spec=str(spec), dry_run=False, save="", hive="myrepo")

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


def test_file_carries_batch_membership_to_filed_beads(hive, fakebd):
    """A batch:<group> declared in the spec lands as a label on the filed bead."""
    spec = hive.tmp / "batch.yaml"
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
    plan.file(spec=str(spec), dry_run=False, save="", hive="myrepo")
    a_args = fakebd.create_args(title="scaffold")
    assert any("batch:same-file" in tok for tok in a_args)
    b_args = fakebd.create_args(title="extend")
    assert any("batch:same-file" in tok for tok in b_args)


def test_file_save_writes_spec(hive, fakebd):
    spec = _write_spec(hive)
    out = hive.tmp / "saved.yaml"
    plan.file(spec=str(spec), dry_run=False, save=str(out), hive="myrepo")
    assert out.exists() and "Add widgets" in out.read_text()


# ---- adopt: file-time report↔epic linking + provenance survival ------------
#
# The planning-plane ADOPT path (bead). A frame carries `adopts` + native
# provenance on its epic; `ws plan file` births the epic (carrying provenance) and links each
# originating report as CHILD-OF the epic — report depends-on epic, so the epic OWNS/blocks the
# report and the report is NEVER a blocker of the epic (the crux invariant, unit-tested below).


class FakeBdAdopt(FakeBd):
    """Extends FakeBd with `bd import - --json` support (returns a created id like create), so the
    provenance-carrying epic can be born via import. Every other verb (create/dep/gate/…) flows
    through the base fake and is recorded for assertion."""

    def __init__(self):
        super().__init__()
        self.imported = []  # (new_id, stdin-json) for every import call

    def __call__(self, cmd, *, check=True, capture=False, env=None, cwd=None, text_input=None):
        if cmd and cmd[0] == "bd":
            args = list(cmd[1:])
            while args and args[0] in ("-C", "--actor"):
                args = args[2:]
            if args and args[0] == "import":
                self.calls.append((None, args))
                self._n += 1
                new_id = f"mr-{self._n}"
                self.imported.append((new_id, text_input))
                return _CP(0, json.dumps({"created": 1, "ids": [new_id]}) + "\n", "")
        return super().__call__(
            cmd, check=check, capture=capture, env=env, cwd=cwd, text_input=text_input
        )


def _dep_add_args(fb):
    """The positional args of the first `bd dep add …` call (order carries the edge DIRECTION:
    `dep add <dependent> <depended-on>`), or None."""
    for _actor, args in fb.calls:
        if args[:2] == ["dep", "add"]:
            return args
    return None


def _write_adopt_spec(hive, *, report="rep-1", source_system="github", external_ref="gh-9"):
    """A minimal ADOPTED frame fleshed with one issue: epic carries `adopts` + optional native
    provenance, plus one root work issue so the molecule is fileable."""
    lines = [
        "epic:",
        "  title: Adopt widget bug",
        "  description: from report",
        f"  adopts: [{report}]",
    ]
    if source_system:
        lines.append(f"  source_system: {source_system}")
    if external_ref:
        lines.append(f"  external_ref: {external_ref}")
    lines += [
        "issues:",
        "  - handle: a",
        "    title: fix it",
        "    acceptance: fixed",
        "    component: runtime",
        "    deps: []",
    ]
    spec = hive.tmp / "adopt.yaml"
    spec.write_text("\n".join(lines) + "\n")
    return spec


def test_file_adopted_epic_births_with_provenance(hive, monkeypatch):
    """Provenance survives onto the epic: with source_system set, the epic is BORN via `bd import`
    (the only way to set source_system) carrying both source_system and external_ref."""
    fb = FakeBdAdopt()
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)
    plan.file(
        spec=str(_write_adopt_spec(hive, report="rep-1")), dry_run=False, save="", hive="myrepo"
    )

    assert fb.imported, "provenance-carrying epic must be born via bd import"
    record = json.loads(fb.imported[0][1])
    assert record["issue_type"] == "epic"
    assert record["source_system"] == "github"
    assert record["external_ref"] == "gh-9"
    # identity triplet rode onto the imported epic as INDIVIDUAL labels (not one comma-joined blob)
    assert "provider:github" in record["labels"]


def test_file_adopted_report_is_child_of_epic_correct_direction(hive, monkeypatch):
    """THE CRUX: the report links as CHILD-OF the epic — `bd dep add <report> <epic> -t
    parent-child`, i.e. the REPORT depends-on the epic. The report must NEVER be wired as a
    blocker/dependency of the epic (that would wrongly gate the molecule on an open report)."""
    fb = FakeBdAdopt()
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)
    plan.file(
        spec=str(_write_adopt_spec(hive, report="rep-1")), dry_run=False, save="", hive="myrepo"
    )

    epic_id = "mr-1"  # the imported epic is the first created id
    # Correct direction (POSITION carries direction — `dep add <dependent> <depended-on>`):
    # report first, epic second. The report DEPENDS-ON the epic (child-of), so it can never gate it.
    assert _dep_add_args(fb) == ["dep", "add", "rep-1", epic_id, "-t", "parent-child"]
    # Every dep-add edge must keep the report on the DEPENDENT side and the epic on the depended-on
    # side — the epic is NEVER made to depend-on / be-blocked-by the report.
    dep_adds = [args for _actor, args in fb.calls if args[:2] == ["dep", "add"]]
    for args in dep_adds:
        assert not (args[2] == epic_id and epic_id != "rep-1"), (
            f"epic must not depend-on report: {args}"
        )
    # No blocking-edge form is used to wire the report to the epic (bd forbids epic↔task blocks).
    assert not any("--blocks" in args for _actor, args in fb.calls if args and args[0] == "dep")


def test_file_adopted_external_ref_only_uses_create_not_import(hive, monkeypatch):
    """A born-native report with only external_ref (no source_system) does NOT force an import: the
    epic is `bd create`-d carrying `--external-ref`, and the report still links child-of the epic.
    """
    fb = FakeBdAdopt()
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)
    spec = _write_adopt_spec(hive, report="rep-1", source_system="", external_ref="gh-9")
    plan.file(spec=str(spec), dry_run=False, save="", hive="myrepo")

    assert not fb.imported, "no source_system ⇒ no import birth needed"
    epic_args = fb.create_args(title="Adopt widget bug")
    assert "--external-ref" in epic_args
    assert epic_args[epic_args.index("--external-ref") + 1] == "gh-9"
    assert _dep_add_args(fb) == ["dep", "add", "rep-1", "mr-1", "-t", "parent-child"]


def test_file_non_adopted_molecule_makes_no_dep_or_import(hive, monkeypatch):
    """Regression guard: a plain (non-adopted) molecule takes NEITHER the import nor the dep-link
    path — the adopt wiring is inert unless the spec declares `adopts`."""
    fb = FakeBdAdopt()
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)
    plan.file(spec=str(_write_spec(hive)), dry_run=False, save="", hive="myrepo")
    assert not fb.imported
    assert _dep_add_args(fb) is None


# ---- adopt verb: seed a frame from a promoted report ------------------------


class FakeBdAdoptShow(FakeBd):
    """Serves `bd show <bead> --json` for the adopt verb from a canned bead map; everything else
    flows through the base fake."""

    def __init__(self, beads):
        super().__init__()
        self._beads = {b["id"]: b for b in beads}

    def __call__(self, cmd, *, check=True, capture=False, env=None, cwd=None, text_input=None):
        if cmd and cmd[0] == "bd":
            args = list(cmd[1:])
            while args and args[0] in ("-C", "--actor"):
                args = args[2:]
            if args and args[0] == "show":
                self.calls.append((None, args))
                bead = self._beads.get(args[1])
                return _CP(0, json.dumps([bead] if bead else []) + "\n", "")
        return super().__call__(
            cmd, check=check, capture=capture, env=env, cwd=cwd, text_input=text_input
        )


def test_adopt_seeds_frame_from_promoted_bead(hive, monkeypatch):
    """`ws plan adopt <promoted-bead>` writes a seed frame: origin id under `adopts`, report text
    seeding the epic, and native provenance carried through."""
    report = {
        "id": "rep-1",
        "title": "login broken",
        "description": "cannot log in",
        "labels": [state.INTAKE_PROMOTED, state.ORIGIN_GITHUB],
        "source_system": "github",
        "external_ref": "gh-9",
    }
    fb = FakeBdAdoptShow([report])
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)
    out = hive.tmp / "frame.yaml"
    result = _runner.invoke(app, ["plan", "adopt", "rep-1", "--out", str(out), "--hive", "myrepo"])
    assert result.exit_code == 0, result.output
    text = out.read_text()
    assert "rep-1" in text  # recorded under adopts
    assert "login broken" in text  # report text seeds the frame
    assert "source_system: github" in text
    assert "external_ref: gh-9" in text


def test_adopt_refuses_non_promoted_bead(hive, monkeypatch):
    """Only reports handed over by triage `promote` (intake:promoted) are adoptable — an untriaged
    bead is refused (exit non-zero) so adopt only consumes the promoted queue."""
    report = {"id": "rep-2", "title": "x", "labels": [state.INTAKE_UNTRIAGED]}
    fb = FakeBdAdoptShow([report])
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)
    result = _runner.invoke(app, ["plan", "adopt", "rep-2", "--hive", "myrepo"])
    assert result.exit_code != 0
    assert "not promoted" in result.output


# ---- check: standalone validation ------------------------------------------


def test_check_valid_spec_exits_zero(hive):
    """check exits 0 and prints '✓ valid' for a well-formed spec."""
    spec = _write_spec(hive)
    result = _runner.invoke(app, ["plan", "check", str(spec), "--hive", "myrepo"])
    assert result.exit_code == 0, result.output
    assert "✓ valid" in result.output


def test_check_invalid_spec_exits_nonzero_and_prints_problems(hive):
    """check exits non-zero and prints each validation problem for a bad spec."""
    bad = hive.tmp / "bad.yaml"
    bad.write_text(
        "epic:\n  title: E\nissues:\n  - handle: a\n    title: t\n"
    )  # missing acceptance
    result = _runner.invoke(app, ["plan", "check", str(bad), "--hive", "myrepo"])
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
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)
    # approve now gates on the convention validator; neutralize it here so these tests exercise
    # gate-resolution mechanics. The gate's own tests (test_approve_*_conventions) drive it.
    monkeypatch.setattr(plan, "verify_epic", lambda *a, **k: [])
    return fb


# ---- approve: success cases -------------------------------------------------


def test_approve_resolves_gate_and_sets_state(hive, fakebd_approve):
    """approve resolves the open kickoff gate and sets kickoff=approved on the epic."""
    plan.approve(epic="epic-1", hive="myrepo")

    # state was queried first
    assert fakebd_approve.did("state", "epic-1", "kickoff")
    # gate was resolved
    assert fakebd_approve.did("gate", "resolve", "gate-42")
    # kickoff=approved was set on the epic
    assert fakebd_approve.did("set-state", "epic-1", "kickoff=approved")


def test_approve_does_not_open_mol_branch(hive, fakebd_approve):
    """Plane separation: approve is pure planning — it must NOT create the container branch.
    The integration plane opens wt/bead/epic/<epic> on first start/assign (worktree.ensure,
    kind='epic')."""
    plan.approve(epic="epic-1", hive="myrepo")
    branches = _git("branch", "--list", "wt/bead/epic/epic-1", cwd=hive.main).stdout.strip()
    assert branches == "", "approve must not open the container branch"


def test_approve_resolves_multiple_gates(hive, monkeypatch):
    """When multiple kickoff gates exist (multi-root molecule), all open ones are resolved."""
    gates = [
        {"id": "gate-1", "status": "open", "description": "kickoff epic-x"},
        {"id": "gate-2", "status": "open", "description": "kickoff epic-x"},
        # closed gate for the same epic — must NOT be resolved
        {"id": "gate-3", "status": "closed", "description": "kickoff epic-x"},
    ]
    fb = FakeBdApprove(kickoff_state="pending", gates=gates)
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(plan, "verify_epic", lambda *a, **k: [])

    plan.approve(epic="epic-x", hive="myrepo")

    assert fb.did("gate", "resolve", "gate-1")
    assert fb.did("gate", "resolve", "gate-2")
    assert not fb.did("gate", "resolve", "gate-3")
    assert fb.did("set-state", "epic-x", "kickoff=approved")


def test_approve_skips_gates_for_other_epics(hive, monkeypatch):
    """Gates belonging to a different epic are not resolved."""
    gates = [
        {"id": "gate-mine", "status": "open", "description": "kickoff epic-target"},
        {"id": "gate-other", "status": "open", "description": "kickoff epic-other"},
    ]
    fb = FakeBdApprove(kickoff_state="pending", gates=gates)
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(plan, "verify_epic", lambda *a, **k: [])

    plan.approve(epic="epic-target", hive="myrepo")

    assert fb.did("gate", "resolve", "gate-mine")
    assert not fb.did("gate", "resolve", "gate-other")
    assert fb.did("set-state", "epic-target", "kickoff=approved")


# ---- approve: reconciling half-states (idempotent convergence) ---------------


def test_approve_noop_when_already_fully_approved(hive, monkeypatch, capsys):
    """kickoff=approved with no open gates is the reconciled fixpoint — approve is a clean
    no-op (exit 0, nothing resolved, nothing restamped)."""
    fb = FakeBdApprove(kickoff_state="approved", gates=[])
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(plan, "verify_epic", lambda *a, **k: [])

    plan.approve(epic="epic-1", hive="myrepo")

    assert "already approved" in capsys.readouterr().out
    assert not fb.did("gate", "resolve")
    assert not fb.did("set-state", "epic-1", "kickoff=approved")


def test_approve_converges_approved_state_with_open_gates(hive, monkeypatch):
    """The bh-3a8r incident shape: kickoff=approved was stamped but the root kickoff gates are
    still open. Re-running approve resolves the leftover gates (no raw gate ids, no
    BH_BD_PASS_ENABLED) and leaves the already-approved state alone."""
    gates = [{"id": "gate-7", "status": "open", "description": "kickoff epic-1"}]
    fb = FakeBdApprove(kickoff_state="approved", gates=gates)
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(plan, "verify_epic", lambda *a, **k: [])

    plan.approve(epic="epic-1", hive="myrepo")

    assert fb.did("gate", "resolve", "gate-7")
    assert not fb.did("set-state", "epic-1", "kickoff=approved")


def test_approve_converges_pending_state_with_no_open_gates(hive, monkeypatch):
    """The mirror half-state: gates were hand-resolved but kickoff still says pending.
    approve flips the state to approved (nothing left to resolve) instead of aborting."""
    gates = [{"id": "gate-99", "status": "closed", "description": "kickoff epic-2"}]
    fb = FakeBdApprove(kickoff_state="pending", gates=gates)
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(plan, "verify_epic", lambda *a, **k: [])

    plan.approve(epic="epic-2", hive="myrepo")

    assert not fb.did("gate", "resolve")
    assert fb.did("set-state", "epic-2", "kickoff=approved")


def test_approve_rerun_after_converge_is_noop(hive, monkeypatch, capsys):
    """approve twice in a row: the first converges, the second is a clean no-op — re-running
    is always safe."""
    gates = [{"id": "gate-7", "status": "open", "description": "kickoff epic-1"}]
    fb = FakeBdApprove(kickoff_state="pending", gates=gates)
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(plan, "verify_epic", lambda *a, **k: [])

    plan.approve(epic="epic-1", hive="myrepo")
    assert fb.did("gate", "resolve", "gate-7")

    # reflect the converged world, then re-run
    fb.kickoff_state = "approved"
    fb._gates = []
    fb.calls.clear()
    plan.approve(epic="epic-1", hive="myrepo")
    assert "already approved" in capsys.readouterr().out
    assert not fb.did("gate", "resolve")
    assert not fb.did("set-state")


def test_approve_refuses_when_kickoff_unset(hive, monkeypatch, capsys):
    """An epic whose kickoff was never stamped is an UNFILED shape, not a half-state — approve
    refuses through the convention gate (which points at `plan repair`, the verb that stamps
    kickoff=pending) instead of converging it silently."""
    fb = FakeBdApprove(kickoff_state="", gates=[])
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(
        plan, "verify_epic", lambda *a, **k: ["kickoff state unset on epic-3"]
    )

    with pytest.raises(typer.Exit):
        plan.approve(epic="epic-3", hive="myrepo")

    err = capsys.readouterr().err
    assert "plan repair" in err
    assert not fb.did("set-state", "epic-3", "kickoff=approved")


# ---- approve: convention gate -----------------------------------------------
#
# approve now refuses to finalize a MALFORMED molecule, surfacing plan.verify_epic's problem list.
# These reuse FakeBdVerify (defined with the verify tests below), which serves a well-formed
# molecule and lets each convention be flipped; late name binding makes the forward reference fine.


def test_approve_refuses_malformed_molecule_conventions(hive, monkeypatch, capsys):
    """A molecule with an open kickoff gate but a broken convention (no swarm) is NOT approved —
    approve prints the validator's specific problem and exits non-zero."""
    fb = FakeBdVerify(kickoff="pending", swarm_epics=())
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)
    with pytest.raises(typer.Exit):
        plan.approve(epic="epic-1", hive="myrepo")
    assert "no bd swarm" in capsys.readouterr().err
    assert not fb.did("set-state", "epic-1", "kickoff=approved")


def test_approve_passes_wellformed_molecule(hive, monkeypatch):
    """A well-formed molecule passes the gate and is approved (gate resolved, kickoff=approved)."""
    fb = FakeBdVerify(kickoff="pending")
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)
    plan.approve(epic="epic-1", hive="myrepo")
    assert fb.did("gate", "resolve", "g-0")
    assert fb.did("set-state", "epic-1", "kickoff=approved")


def test_approve_bhdebug_overrides_malformed_molecule(hive, monkeypatch, capsys):
    """BH_DEBUG downgrades the convention gate to a warning so a human can force approve through."""
    fb = FakeBdVerify(kickoff="pending", swarm_epics=())
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setenv("BH_DEBUG", "1")
    plan.approve(epic="epic-1", hive="myrepo")
    assert "BH_DEBUG override" in capsys.readouterr().err
    assert fb.did("set-state", "epic-1", "kickoff=approved")


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


def test_show_from_spec_renders_epic_issues_and_roots(hive):
    """ws plan show <spec> renders the epic title, issues in topo order, and root set."""
    spec = _write_spec(hive)
    result = _runner.invoke(app, ["plan", "show", str(spec), "--hive", "myrepo"])
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


def test_show_from_spec_shows_labels_and_deps(hive):
    """ws plan show shows dimension labels and dep handles for each issue."""
    spec = _write_spec(hive)
    result = _runner.invoke(app, ["plan", "show", str(spec), "--hive", "myrepo"])
    assert result.exit_code == 0, result.output
    # Issue 'a' carries component:runtime label from spec
    assert "component:runtime" in result.output
    # Issue 'b' has a dep listed
    assert "deps" in result.output


# ---- show: from epic (filed) ------------------------------------------------


def test_show_from_epic_renders_filed_molecule(hive, monkeypatch):
    """ws plan show <epic_id> renders the filed molecule from beads (round-trip view)."""
    fb = FakeBdShow("epic-1", "Add widgets", _FILED_CHILDREN)
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)

    result = _runner.invoke(app, ["plan", "show", "epic-1", "--hive", "myrepo"])
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


# ---- show: originating (adopted) reports ------------------------------------

_ORIGIN_CHILD = {
    "id": "rep-9",
    "title": "user report",
    "issue_type": "bug",
    "status": "open",
    "labels": ["intake:promoted", "origin:github"],
    "source_system": "github",
    "external_ref": "gh-9",
    "acceptance_criteria": "",  # a report has no acceptance — must NOT be treated as work
    "dependencies": [{"depends_on_id": "epic-1", "type": "parent-child"}],
}


def test_show_from_epic_displays_originating_reports(hive, monkeypatch):
    """Round-trip: `ws plan show <epic>` surfaces the adopted originating report(s) in their own
    section (with channel + provenance), while the work siblings still render normally."""
    fb = FakeBdShow("epic-1", "Add widgets", _FILED_CHILDREN + [_ORIGIN_CHILD])
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)
    result = _runner.invoke(app, ["plan", "show", "epic-1", "--hive", "myrepo"])
    assert result.exit_code == 0, result.output
    assert "originating reports" in result.output
    assert "rep-9" in result.output
    assert "gh-9" in result.output  # native provenance shown for traceability
    # the report is a source link, NOT a work sibling — the real work cards still render
    assert "scaffold" in result.output and "wire it" in result.output


def test_show_from_epic_omits_originating_section_when_not_adopted(hive, monkeypatch):
    """A non-adopted molecule shows no 'originating reports' section (the feature is inert)."""
    fb = FakeBdShow("epic-1", "Add widgets", _FILED_CHILDREN)
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)
    result = _runner.invoke(app, ["plan", "show", "epic-1", "--hive", "myrepo"])
    assert result.exit_code == 0, result.output
    assert "originating reports" not in result.output


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
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)
    return fb


# ---- status: list (no epic arg) -----------------------------------------------


def test_status_list_shows_each_epic_with_kickoff(hive, fakebd_status):
    """ws plan status (no arg) lists all swarms, each with its kickoff column."""
    result = _runner.invoke(app, ["plan", "status", "--hive", "myrepo"])
    assert result.exit_code == 0, result.output
    assert "epic-1" in result.output
    assert "epic-2" in result.output
    assert "Feature Alpha" in result.output
    assert "Feature Beta" in result.output
    assert "pending" in result.output
    assert "approved" in result.output


def test_status_list_shows_progress(hive, fakebd_status):
    """ws plan status (no arg) shows completed/total progress for each swarm."""
    result = _runner.invoke(app, ["plan", "status", "--hive", "myrepo"])
    assert result.exit_code == 0, result.output
    assert "2/5" in result.output


def test_status_list_unset_kickoff_shows_dash(hive, monkeypatch):
    """ws plan status shows — for epics whose kickoff state is unset."""
    fb = FakeBdStatus(
        swarms_list=_SWARMS_LIST,
        kickoff_by_epic={"epic-1": "", "epic-2": ""},
    )
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)
    result = _runner.invoke(app, ["plan", "status", "--hive", "myrepo"])
    assert result.exit_code == 0, result.output
    assert "—" in result.output


# ---- status: with epic arg ----------------------------------------------------


def test_status_epic_shows_detail_and_kickoff(hive, fakebd_status):
    """ws plan status <epic> shows swarm detail and kickoff state."""
    result = _runner.invoke(app, ["plan", "status", "epic-1", "--hive", "myrepo"])
    assert result.exit_code == 0, result.output
    assert "epic-1" in result.output
    assert "Feature Alpha" in result.output
    assert "kickoff" in result.output
    assert "pending" in result.output


def test_status_epic_shows_active_ready_blocked(hive, fakebd_status):
    """ws plan status <epic> shows active, ready, and blocked issue groups."""
    result = _runner.invoke(app, ["plan", "status", "epic-1", "--hive", "myrepo"])
    assert result.exit_code == 0, result.output
    assert "active" in result.output
    assert "ready" in result.output
    assert "blocked" in result.output


# ---- verify: filed-molecule convention gate ---------------------------------
#
# A well-formed filed molecule: epic-1 (issue_type=epic) with two children carrying the
# identity triplet + valid closed-dim labels, a swarm over the epic, a kickoff gate blocking
# the root, and kickoff=approved. Each malformed variant flips exactly one of those.

_TRIPLET = ["provider:github", "org:myorg", "repo:myrepo"]


def _child(cid, title, *, labels, deps=(), acceptance="works", issue_type="feature", status="open"):
    """A filed-child dict shaped like `bd list --parent --all` output (sibling deps as 'blocks').
    `status="closed"` models a predecessor that has since merged out of the active molecule."""
    dependencies = [{"depends_on_id": "epic-1", "type": "parent-child"}]
    dependencies += [{"depends_on_id": d, "type": "blocks"} for d in deps]
    return {
        "id": cid,
        "title": title,
        "issue_type": issue_type,
        "status": status,
        "labels": list(labels),
        "acceptance_criteria": acceptance,
        "dependencies": dependencies,
    }


def _good_children():
    return [
        _child("epic-1.1", "scaffold", labels=_TRIPLET + ["model:sonnet"]),
        _child("epic-1.2", "wire it", labels=_TRIPLET + ["model:sonnet"], deps=["epic-1.1"]),
    ]


class FakeBdVerify(FakeBd):
    """Serves the read-only queries verify makes: `bd show <epic>`, `bd list --parent <epic>`,
    `bd swarm list`, `bd gate list`, and `bd state <epic> kickoff`. Every field is configurable
    so each test can flip exactly one convention."""

    def __init__(
        self,
        *,
        epic_type="epic",
        epic_id="epic-1",
        children=None,
        swarm_epics=("epic-1",),
        gate_descs=("Ad-hoc gate blocking epic-1.1\n\nReason: kickoff epic-1",),
        kickoff="approved",
    ):
        super().__init__()
        self._epic_type = epic_type
        self._epic_id = epic_id
        self._children = _good_children() if children is None else children
        self._swarm_epics = list(swarm_epics)
        self._gate_descs = list(gate_descs)
        self._kickoff = kickoff

    def __call__(self, cmd, *, check=True, capture=False, env=None, cwd=None, text_input=None):
        if not cmd or cmd[0] != "bd":
            return real_run(
                cmd, check=check, capture=capture, env=env, cwd=cwd, text_input=text_input
            )
        args = list(cmd[1:])
        while args and args[0] in ("-C", "--actor"):
            args = args[2:]
        self.calls.append((None, list(args)))

        if args and args[0] == "show":
            epic = {
                "id": self._epic_id,
                "title": "Add widgets",
                "issue_type": self._epic_type,
                "description": "why",
            }
            return _CP(0, json.dumps([epic]) + "\n", "")
        if args and args[0] == "list" and "--parent" in args:
            return _CP(0, json.dumps(self._children) + "\n", "")
        if len(args) >= 2 and args[0] == "swarm" and args[1] == "list":
            swarms = [{"epic_id": e} for e in self._swarm_epics]
            return _CP(0, json.dumps({"schema_version": 1, "swarms": swarms}) + "\n", "")
        if len(args) >= 2 and args[0] == "gate" and args[1] == "list":
            gates = [
                {"id": f"g-{i}", "status": "open", "description": d}
                for i, d in enumerate(self._gate_descs)
            ]
            return _CP(0, json.dumps(gates) + "\n", "")
        if args and args[0] == "state":
            return _CP(0, self._kickoff + "\n", "")
        return _CP(0, "", "")


def _verify(hive, monkeypatch, **kwargs):
    fb = FakeBdVerify(**kwargs)
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)
    return _runner.invoke(app, ["plan", "verify", "epic-1", "--hive", "myrepo"])


def test_verify_wellformed_molecule_exits_zero(hive, monkeypatch):
    """A well-formed filed molecule prints an OK line and exits 0."""
    result = _verify(hive, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "✓ verified" in result.output


def test_verify_ignores_adopted_origin_report_child(hive, monkeypatch):
    """An adopted origin report is a child of the epic but NOT molecule work: it carries no
    acceptance and no identity triplet, yet verify must PASS — the report is held out of the
    work-sibling set, so it never triggers a spurious 'missing acceptance / label' problem."""
    children = _good_children() + [
        {
            "id": "rep-9",
            "title": "user report",
            "issue_type": "bug",
            "status": "open",
            "labels": ["intake:promoted", "origin:github"],  # no triplet, no closed dims
            "acceptance_criteria": "",  # no acceptance
            "dependencies": [{"depends_on_id": "epic-1", "type": "parent-child"}],
        }
    ]
    result = _verify(hive, monkeypatch, children=children)
    assert result.exit_code == 0, result.output
    assert "rep-9" not in result.output  # the report is never flagged as a work sibling


def test_verify_is_read_only(hive, monkeypatch):
    """verify makes no mutating bd calls (create/update/set-state/gate resolve/swarm create)."""
    fb = FakeBdVerify()
    monkeypatch.setattr(bd_mod, "_run", fb)
    monkeypatch.setattr(bd_mod, "_run", fb)
    _runner.invoke(app, ["plan", "verify", "epic-1", "--hive", "myrepo"])
    mutating = {"create", "update", "set-state", "delete", "close", "resolve"}
    for _actor, args in fb.calls:
        assert not (set(args) & mutating), f"verify must not mutate — saw {args}"


def test_verify_missing_swarm_exits_nonzero(hive, monkeypatch):
    """No swarm over the epic → a specific 'no bd swarm' problem, non-zero exit."""
    result = _verify(hive, monkeypatch, swarm_epics=())
    assert result.exit_code != 0
    assert "no bd swarm" in result.output


def test_verify_missing_kickoff_gate_exits_nonzero(hive, monkeypatch):
    """No kickoff gate blocking the root → a specific 'no kickoff gate' problem, non-zero exit."""
    result = _verify(hive, monkeypatch, gate_descs=())
    assert result.exit_code != 0
    assert "no kickoff gate" in result.output
    assert "epic-1.1" in result.output  # names the specific root


def test_verify_unset_kickoff_state_exits_nonzero(hive, monkeypatch):
    """kickoff state unset → a specific 'kickoff state unset' problem, non-zero exit."""
    result = _verify(hive, monkeypatch, kickoff="")
    assert result.exit_code != 0
    assert "kickoff state unset" in result.output


def test_verify_missing_identity_labels_exits_nonzero(hive, monkeypatch):
    """A child missing the identity triplet → a specific 'missing identity label' problem."""
    children = [
        _child("epic-1.1", "scaffold", labels=["model:sonnet"]),  # no provider/org/repo
    ]
    result = _verify(hive, monkeypatch, children=children)
    assert result.exit_code != 0
    assert "missing identity label" in result.output
    assert "epic-1.1" in result.output


def test_verify_bad_closed_dimension_label_exits_nonzero(hive, monkeypatch):
    """A child with a closed-dim label outside the allowed set → a specific problem."""
    children = [
        _child("epic-1.1", "scaffold", labels=_TRIPLET + ["model:gpt4"]),  # gpt4 ∉ closed set
    ]
    result = _verify(hive, monkeypatch, children=children)
    assert result.exit_code != 0
    assert "not in closed set" in result.output
    assert "epic-1.1" in result.output


def test_verify_non_epic_bead_exits_nonzero(hive, monkeypatch):
    """The verified bead not being an epic → a specific 'not an epic' problem."""
    result = _verify(hive, monkeypatch, epic_type="feature")
    assert result.exit_code != 0
    assert "not an epic" in result.output


def test_verify_structural_problem_from_validate_spec(hive, monkeypatch):
    """molecule.validate_spec still runs: a child missing acceptance surfaces its problem."""
    children = [
        _child("epic-1.1", "scaffold", labels=_TRIPLET, acceptance=""),  # missing acceptance
    ]
    result = _verify(hive, monkeypatch, children=children)
    assert result.exit_code != 0
    assert "acceptance" in result.output


def test_verify_lists_each_problem_for_fully_malformed(hive, monkeypatch):
    """A hand-created molecule that flouts several conventions lists EACH problem at once."""
    children = [_child("epic-1.1", "scaffold", labels=["model:gpt4"])]  # no triplet + bad model
    result = _verify(
        hive,
        monkeypatch,
        epic_type="task",
        children=children,
        swarm_epics=(),
        gate_descs=(),
        kickoff="",
    )
    assert result.exit_code != 0
    out = result.output
    assert "not an epic" in out
    assert "no bd swarm" in out
    assert "no kickoff gate" in out
    assert "kickoff state unset" in out
    assert "missing identity label" in out
    assert "not in closed set" in out


# ---- verify: merged/closed predecessor (satisfied root) — the mid-molecule false-positive -----
#
# Once a predecessor bead merges (closes) it drops out of the default child list; its `blocks`
# edge to the successor vanishes and the successor is promoted to a root that verify then wrongly
# demands a kickoff gate for — forcing a BH_DEBUG=1 override each dispatch. verify must treat a
# root whose blocking siblings are ALL closed/merged as a satisfied root: no gate demanded, while
# still catching a genuinely ungated (never-gated) root.


def test_verify_merged_predecessor_root_needs_no_kickoff_gate(hive, monkeypatch):
    """A successor left as the only live bead after its predecessor merged is a SATISFIED root —
    verify passes with NO kickoff gate and NO BH_DEBUG override (the regression scenario)."""
    children = [
        # epic-1.1 has merged out of the molecule (closed); epic-1.2 blocked on it is now the
        # only live issue — a satisfied root, not a fresh entry point.
        _child("epic-1.1", "scaffold", labels=_TRIPLET + ["model:sonnet"], status="closed"),
        _child("epic-1.2", "wire it", labels=_TRIPLET + ["model:sonnet"], deps=["epic-1.1"]),
    ]
    # gate_descs=() ⇒ prove NO kickoff gate is demanded for the satisfied root.
    result = _verify(hive, monkeypatch, children=children, gate_descs=())
    assert result.exit_code == 0, result.output
    assert "✓ verified" in result.output
    assert "no kickoff gate" not in result.output


def test_verify_still_catches_genuinely_ungated_root_alongside_satisfied_one(hive, monkeypatch):
    """The satisfied-root allowance must NOT mask a genuinely ungated root: a fresh root with no
    predecessor and no kickoff gate still fails, naming that specific root."""
    children = [
        _child("epic-1.1", "scaffold", labels=_TRIPLET + ["model:sonnet"], status="closed"),
        _child("epic-1.2", "wire it", labels=_TRIPLET + ["model:sonnet"], deps=["epic-1.1"]),
        # epic-1.3 is a genuine, never-gated root (no predecessor at all).
        _child("epic-1.3", "new work", labels=_TRIPLET + ["model:sonnet"]),
    ]
    result = _verify(hive, monkeypatch, children=children, gate_descs=())
    assert result.exit_code != 0
    assert "no kickoff gate" in result.output
    assert "epic-1.3" in result.output  # names the genuinely ungated root
    assert "epic-1.2" not in result.output  # the satisfied root is not flagged
