"""Guards the setup Guide's step files (bh-0olv9.4 / .5 / .8).

``tests/test_setup_guide_asset.py`` guards the ENVELOPE — ``GUIDE.md``, ``SKILL.md`` and wheel
packaging. This module guards the ``steps/`` and ``scripts/`` content that fills it, and it is a
separate file on purpose: the envelope's contracts are fixed by the ADR and rarely move, while
step content changes every time a route or a verb does.

Three classes of contract, none of which survives review-by-eye:

1. **Schema conformance.** A third-party harness parses each step's frontmatter. A typo there
   is not a rendering bug, it is an unwalkable step. The 0.1 ``step.schema.json`` is vendored
   next to the two the envelope already uses, for the same reason: deterministic and offline.
2. **Referential integrity.** ``requires:`` naming a step id that does not exist, or a
   ``script:`` path that is not on disk (or not executable), fails only at walk time — on a
   user's machine, mid-install.
3. **The decisions the beads fixed**, asserted exactly where they are cheap to reverse by
   accident: 010 asks nothing and mutates nothing; 030 forces and verifies the VERSION; 040
   reads by route; 065 skips cleanly off Claude; 080 is the terminal rung-1 exit; 090-092 hang
   off it rather than sitting in the linear walk, and 092 carries rung 4's gap note.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from ruamel.yaml import YAML

jsonschema = pytest.importorskip("jsonschema")

_REPO = Path(__file__).resolve().parents[1]
_GUIDE_DIR = _REPO / "src" / "beadhive" / "assets" / "guides" / "setup"
_STEPS_DIR = _GUIDE_DIR / "steps"
_SCRIPTS_DIR = _GUIDE_DIR / "scripts"
_SCHEMAS = Path(__file__).resolve().parent / "schemas" / "agentguides-0.1"

# `steps/NNN[a-z]?-<id>.md`, per the 0.1 step schema's own description of the layout.
_STEP_FILENAME = re.compile(r"^(\d{3})([a-z]?)-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")

# The install block ends at 040; configuration runs 050-080; everything from 090 is a rung
# transition and is entered only on request (bh-0olv9.8).
_RUNG_TRANSITION_FLOOR = 90


def _step_files() -> list[Path]:
    return sorted(p for p in _STEPS_DIR.glob("*.md"))


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name} must open with a YAML frontmatter fence"
    _, block, _ = text.split("---\n", 2)
    return YAML(typ="safe").load(block)


def _body(path: Path) -> str:
    return path.read_text(encoding="utf-8").split("---\n", 2)[2]


def _steps() -> dict[str, dict]:
    """step id → the `step:` block, for every authored step."""
    return {_frontmatter(p)["step"]["id"]: _frontmatter(p)["step"] for p in _step_files()}


def _by_number() -> dict[int, dict]:
    out: dict[int, dict] = {}
    for p in _step_files():
        m = _STEP_FILENAME.match(p.name)
        assert m, f"{p.name} does not match steps/NNN[a-z]?-<id>.md"
        out[int(m.group(1))] = _frontmatter(p)["step"]
    return out


def _step(number: int) -> dict:
    return _by_number()[number]


def _prompt(step: dict) -> str:
    """A step's action prompt with whitespace collapsed.

    Prompts are hand-wrapped block scalars, so an assertion on a phrase must not depend on
    where the author happened to break the line — otherwise re-wrapping a paragraph silently
    turns a contract test green or red for no semantic reason.
    """
    return " ".join(step["action"]["prompt"].split())


def _step_schema() -> dict:
    return json.loads((_SCHEMAS / "step.schema.json").read_text(encoding="utf-8"))


# --- 1. schema conformance ------------------------------------------------------------------


def test_vendored_step_schema_is_a_valid_draft_2020_12_schema() -> None:
    """Catches a truncated or hand-edited vendored copy before it silently passes everything."""
    jsonschema.Draft202012Validator.check_schema(_step_schema())


def test_there_are_steps_at_all() -> None:
    """A guard on the guard: every assertion below is vacuously true over an empty steps/."""
    assert _step_files(), "steps/ is empty — nothing here is actually being checked"


@pytest.mark.parametrize("path", _step_files(), ids=lambda p: p.name)
def test_every_step_validates_against_the_0_1_step_schema(path: Path) -> None:
    """ACCEPTANCE (.4/.5/.8): every step file validates against the step schema."""
    jsonschema.Draft202012Validator(_step_schema()).validate(_frontmatter(path))


@pytest.mark.parametrize("path", _step_files(), ids=lambda p: p.name)
def test_step_id_matches_its_filename_slug(path: Path) -> None:
    """`requires:` refers to ids while humans refer to filenames; drift makes both lie."""
    m = _STEP_FILENAME.match(path.name)
    assert m, f"{path.name} does not match steps/NNN[a-z]?-<id>.md"
    assert _frontmatter(path)["step"]["id"] == m.group(3)


# --- 2. referential integrity ----------------------------------------------------------------


def test_requires_only_names_steps_that_exist() -> None:
    """A dangling `requires:` fails at walk time, on a user's machine, mid-install."""
    steps = _steps()
    for step_id, step in steps.items():
        for dep in step.get("requires", []):
            assert dep in steps, f"step {step_id!r} requires unknown step {dep!r}"


def test_every_referenced_script_exists_and_is_executable() -> None:
    """`action`/`verify` script paths resolve relative to the Guide root, at walk time."""
    referenced: set[str] = set()
    for step in _steps().values():
        for block in (step.get("action"), step.get("verify")):
            if isinstance(block, dict) and block.get("type") == "script":
                referenced.add(block["script"])
    assert referenced, "no step declares a script — every check here is agent judgment"
    for rel in sorted(referenced):
        target = _GUIDE_DIR / rel
        assert target.is_file(), f"{rel} is referenced by a step but is not on disk"
        assert os.access(target, os.X_OK), f"{rel} is referenced but is not executable"


def test_every_shipped_script_is_referenced_by_a_step() -> None:
    """The other direction: an orphaned script is dead weight in the wheel."""
    referenced = {
        block["script"]
        for step in _steps().values()
        for block in (step.get("action"), step.get("verify"))
        if isinstance(block, dict) and block.get("type") == "script"
    }
    for script in sorted(_SCRIPTS_DIR.glob("*.sh")):
        rel = script.relative_to(_GUIDE_DIR).as_posix()
        assert rel in referenced, f"{rel} ships but no step references it"


def test_terminates_at_only_names_declared_end_states() -> None:
    """The walk DAG refuses to build otherwise, and the failure is a stack trace, not a hint."""
    guide = YAML(typ="safe").load((_GUIDE_DIR / "GUIDE.md").read_text().split("---\n", 2)[1])
    declared = {e["id"] for e in guide["guide"]["end_states"]}
    for step_id, step in _steps().items():
        terminus = step.get("terminates_at")
        if terminus is not None:
            assert terminus in declared, f"step {step_id!r} terminates at unknown {terminus!r}"


# --- 3. the install block: bh-0olv9.4 ---------------------------------------------------------


def test_010_preflight_is_read_only_and_asks_nothing() -> None:
    """ACCEPTANCE (.4): 010 performs no mutation, and `interactions: []`.

    Asking before you know the machine's state is how a guide becomes an interrogation — and
    how a user gets offered a route their hardware cannot run.
    """
    step = _step(10)
    assert step["effect"] == "read-only"
    assert step["interactions"] == []


def test_010_emits_machine_readable_state_for_later_steps() -> None:
    """ACCEPTANCE (.4): the probe's output is JSON, declared as such."""
    step = _step(10)
    assert step["verify"]["type"] == "script"
    assert step["verify"]["output_schema"] == "json"


def test_010_probe_does_not_shell_out_to_bh_setup_check() -> None:
    """`bh setup check` WRITES ~/.beadhive/setup-state.json — and `bh` may not exist yet.

    A probe that refreshes the cache it is reporting on has changed the thing it measured.
    """
    probe = (_SCRIPTS_DIR / "preflight.sh").read_text(encoding="utf-8")
    code = "\n".join(line for line in probe.splitlines() if not line.lstrip().startswith("#"))
    assert "setup check" not in code


def test_020_presents_the_fork_by_consequence_not_mechanism() -> None:
    """ACCEPTANCE (.4): the route choice is presented by what it buys and costs.

    A user asked to choose between `nix profile install` and `uv tool install` is being asked
    to have an opinion about package managers. What they can actually judge is the consequence:
    whether the four tools bh drives come with it.
    """
    choice = next(i for i in _step(20)["interactions"] if i["kind"] == "choice")
    assert len(choice["choices"]) == 2
    joined = " ".join(choice["choices"])
    for tool in ("bd", "dolt", "gh", "git-workspace"):
        assert tool in joined, f"the fork's consequence is stated in terms of {tool}"
    # Mechanism must not be the headline: neither install command appears in the choice text.
    assert "nix profile install" not in joined
    assert "uv tool install" not in joined


def test_020_forces_pypi_on_intel_macos_with_the_reason_shown() -> None:
    """ACCEPTANCE (.4): forced, and the reason read out — a silent force reads as a bug."""
    prompt = _prompt(_step(20))
    assert "managed_route.supported" in prompt
    assert "blocked_reason" in prompt
    assert "verbatim" in prompt
    probe = (_SCRIPTS_DIR / "preflight.sh").read_text(encoding="utf-8")
    assert "Intel macOS" in probe, "the probe must carry the reason it is forcing the route"


def test_020_offers_the_nix_installer_and_does_not_run_it() -> None:
    """ACCEPTANCE (.4) + ADR Decision 3: offer the command, explain it, WAIT."""
    step = _step(20)
    prompt = _prompt(step)
    assert "install.determinate.systems/nix" in prompt
    assert "Do not run it." in prompt
    assert step["effect"] == "none", "020 decides; it must not install anything"
    # The wait is a real interaction, and declining is allowed (`required: false`).
    handoff = next(i for i in step["interactions"] if i["id"] == "nix-install-handoff")
    assert handoff["required"] is False


def test_030_uses_force_and_verifies_the_version_string() -> None:
    """ACCEPTANCE (.4): --force, and a verify that compares versions rather than exit codes.

    INSTALL.md:120-126: on macOS with 0.7.1 installed, an unforced `uv tool install` printed
    "Installed 2 executables", exited 0, and `bh --version` still said 0.7.1.
    """
    step = _step(30)
    prompt = _prompt(step)
    assert "uv tool install --force" in prompt
    assert "pipx install --force" in prompt
    assert "pip install --upgrade" in prompt
    assert step["verify"]["type"] == "script"
    assert step["verify"]["script"] == "scripts/verify-bh-version.sh"


def test_030_verify_script_never_reads_an_installer_exit_code() -> None:
    """The measured failure exits 0. Comparing versions is the only signal left."""
    script = (_SCRIPTS_DIR / "verify-bh-version.sh").read_text(encoding="utf-8")
    assert "bh --version" in script
    # It must corroborate against what the package manager claims to have installed.
    for probe in ("uv tool list", "pipx list --short", "pip show beadhive", "brew list"):
        assert probe in script


def test_040_on_failure_differs_by_route_with_the_reason_in_the_step() -> None:
    """ACCEPTANCE (.4): abort on managed, carry on for PyPI, each reason stated.

    All four tools IS the managed route's contract; on PyPI a missing tool is the expected
    state and `bh setup check` is advice, not a gate.
    """
    clauses = _step(40)["on_failure"]
    assert isinstance(clauses, list) and len(clauses) == 2
    managed = next(c for c in clauses if "MANAGED" in c["reason"])
    pypi = next(c for c in clauses if "PYPI" in c["reason"])
    assert managed["strategy"] == "abort"
    # `continue` is not one of the 0.1 strategies (retry/recover/abort/ask); the PyPI clause
    # spells the intent out in its reason instead of pretending the schema has it.
    assert pypi["strategy"] == "ask"
    assert "continue" in pypi["reason"]


def test_040_is_a_declared_successful_exit_for_a_user_who_stops_after_installing() -> None:
    """`installed-unwired` (0.5) is a finish, not an abandonment — so the Guide never has an
    incentive to push a user past "not now"."""
    assert _step(40)["terminates_at"] == "installed-unwired"


# --- 4. the configuration block: bh-0olv9.5 ---------------------------------------------------

_CONFIGURE_BLOCK = (50, 60, 65, 70, 80)


@pytest.mark.parametrize("number", _CONFIGURE_BLOCK)
def test_configure_steps_probe_before_acting(number: int) -> None:
    """ACCEPTANCE (.5): every step probes first and reports already-done as SUCCESS.

    `INSTALL.md`'s `configure[]` block runs `bh config init`, `bh mcp install` and the plugin
    install BEFORE this Guide is ever invoked. Already-done is therefore the state most users
    arrive in, and a step that errors on it fails for the majority of them.
    """
    step = _by_number()[number]
    prompt = _prompt(step)
    assert "PROBE" in prompt or "ALREADY SATISFIED" in prompt or "SKIP CONDITION" in prompt, (
        f"step {number} does not probe before acting"
    )


@pytest.mark.parametrize("number", (50, 60, 65, 70))
def test_configure_steps_treat_already_done_as_satisfied(number: int) -> None:
    """The words matter: a silent skip leaves the user unsure anything happened."""
    assert "ALREADY SATISFIED" in _prompt(_by_number()[number])


def test_060_probes_claude_mcp_list_before_invoking_bh_mcp_install() -> None:
    """ACCEPTANCE (.5): the probe comes first, and it is the same script as the verify."""
    step = _step(60)
    prompt = _prompt(step)
    assert prompt.index("scripts/check-mcp-wired.sh") < prompt.index("bh mcp install")
    assert step["verify"]["script"] == "scripts/check-mcp-wired.sh"
    assert "claude mcp list" in (_SCRIPTS_DIR / "check-mcp-wired.sh").read_text(encoding="utf-8")


def test_060_skips_cleanly_when_the_harness_is_not_claude() -> None:
    """A third exit code, because "no claude here" is neither wired nor unwired.

    Folding it into 0 would be a false green; folding it into 1 would send a non-Claude user to
    install something that does not apply to them.
    """
    script = (_SCRIPTS_DIR / "check-mcp-wired.sh").read_text(encoding="utf-8")
    assert "NOT-APPLICABLE" in script
    assert "exit 3" in script
    clauses = _step(60)["on_failure"]
    skip = next(c for c in clauses if "NOT APPLICABLE" in c["reason"])
    assert "skip and continue" in skip["reason"]


def test_065_plugin_is_optional_approval_gated_and_skips_with_a_pointer() -> None:
    """ACCEPTANCE (.5): optional, gated, and clean off Claude Code.

    The plugin is Claude-Code-specific; OpenCode is supported through `bh hive onboard
    --opencode` instead. A guide that hard-requires the plugin silently excludes every other
    harness.
    """
    step = _step(65)
    gate = next(i for i in step["interactions"] if i["id"] == "want-plugin")
    assert gate["kind"] == "confirm"
    assert gate["required"] is False, "an optional step's gate must accept 'no'"
    prompt = _prompt(step)
    assert "--opencode" in prompt, "skipping must point at the other harness's path"
    assert "Do NOT install Claude Code" in prompt
    # It hangs off a step that legitimately skips, so it must accept a skipped predecessor.
    assert step["accepts_skipped"] is True


def test_065_never_blocks_a_step_that_follows_it() -> None:
    """Optional means declining it cannot strand the run.

    070 DOES depend on 065 — that edge is what keeps the walk linear, and without it 065 is a
    leaf whose implicit terminus is the first end state, so a run that stopped at the optional
    plugin would be scored as having reached rung 1 with no HQ and no hive. The edge is made
    safe by `accepts_skipped`, which every dependent must therefore carry.
    """
    dependents = [
        (step_id, step)
        for step_id, step in _steps().items()
        if "plugin" in step.get("requires", [])
    ]
    assert dependents, "070 follows 065; losing that edge makes 065 a spurious rung-1 finish"
    for step_id, step in dependents:
        assert step.get("accepts_skipped") is True, (
            f"{step_id} requires the optional plugin but does not accept it skipped"
        )


def test_065_presents_both_plugin_commands_as_one_decision() -> None:
    """`marketplace add` then `install`; neither is useful alone."""
    prompt = _prompt(_step(65))
    assert "claude plugin marketplace add beadhive/claude-plugin" in prompt
    assert "claude plugin install bh@beadhive" in prompt
    assert prompt.index("marketplace add") < prompt.index("plugin install bh@beadhive")


def test_070_states_rung_1_cost_inline_in_the_justfile_framing() -> None:
    """ACCEPTANCE (.5): the cost note, verbatim, in the step — not in a footnote.

    The justfile's `local-install` `_step5_note` already says it exactly right. A user who does
    not know rung 1 is deliberately remote-less reads a local-only HQ as a broken install.
    """
    step = _step(70)
    note_fragments = (
        "HQ is LOCAL with no remote — the posture, not an omission",
        "no backup, and no second machine until you wire one",
    )
    prompt = _prompt(step)
    for fragment in note_fragments:
        assert fragment in prompt, f"the cost note must be IN the step: missing {fragment!r}"
    ack = next(i for i in step["interactions"] if i["id"] == "acknowledge-local-only")
    assert ack["when"] == "after"


def test_070_does_not_wire_a_remote() -> None:
    """Rung 2 is step 090 and is opt-in. `--create` here would take it by accident."""
    prompt = _prompt(_step(70))
    assert "--create" in prompt and "do not offer `--create`" in prompt


def test_080_onboards_exactly_one_hive_and_ends_on_bh_work_ready() -> None:
    """ACCEPTANCE (.5): one hive, and the loop demonstrated answering.

    `bh hive onboard` requires {PROVIDER/ORG/REPO} — there is no bare form.
    """
    prompt = _prompt(_step(80))
    assert "ONE hive, not all of them." in prompt
    assert "PROVIDER/ORG/REPO" in prompt
    assert "bh work ready" in prompt
    # An empty ready list on a fresh hive is the expected pass, and must be said so.
    assert "EMPTY ready list is a PASS" in prompt


def test_080_is_the_terminal_rung_1_exit() -> None:
    """ACCEPTANCE (.5/.8): the run is FINISHED here; 090+ are entered only on request."""
    assert _step(80)["terminates_at"] == "rung-1-reached"
    assert "you now have a running factory on rung 1" in _prompt(_step(80))


# --- 5. the rung transitions: bh-0olv9.8 ------------------------------------------------------

_RUNG_STEPS = (90, 91, 92)


@pytest.mark.parametrize("number", _RUNG_STEPS)
def test_rung_transitions_are_reachable_only_after_the_terminal_rung_1_exit(number: int) -> None:
    """ACCEPTANCE (.8): the rung-1 end state is TERMINAL without 090-092.

    Modelled as a branch hanging off the finish, not as steps 090+ in a linear walk: each one
    depends (transitively) on `first-hive`, which itself declares `terminates_at:
    rung-1-reached`. A user who wanted rung 1 and nothing else stops there having missed
    nothing.
    """
    steps = _steps()
    step = _by_number()[number]

    seen: set[str] = set()
    frontier = list(step.get("requires", []))
    while frontier:
        dep = frontier.pop()
        if dep in seen:
            continue
        seen.add(dep)
        frontier.extend(steps[dep].get("requires", []))
    assert "first-hive" in seen, "a rung transition must hang off the rung-1 finish"
    assert steps["first-hive"]["terminates_at"] == "rung-1-reached"


@pytest.mark.parametrize("number", _RUNG_STEPS)
def test_rung_transitions_are_opt_in_and_declinable(number: int) -> None:
    """ACCEPTANCE (.8): entered ONLY on explicit request. `required: false` is the "no"."""
    step = _by_number()[number]
    gate = next(i for i in step["interactions"] if i["id"].startswith("want-rung"))
    assert gate["when"] == "before"
    assert gate["kind"] == "confirm"
    assert gate["required"] is False
    assert "OPT-IN ONLY" in _prompt(step)


@pytest.mark.parametrize("number", _RUNG_STEPS)
def test_rung_transitions_score_no_higher_than_rung_1(number: int) -> None:
    """Climbing is optional, so declining must not score lower — there is no rung-2 end state.

    The Guide's goal_state IS rung 1; a transition is beyond the goal, not a better finish.
    """
    assert _by_number()[number]["terminates_at"] == "rung-1-reached"


def test_090_verifies_both_the_git_and_the_dolt_half() -> None:
    """ACCEPTANCE (.8): both halves, via `bh hq status`.

    A green git half over an unpushed Dolt half is the failure that looks like success: the
    fleet config published, the beads did not, and a cloning host gets a factory with no work.
    """
    step = _step(90)
    assert step["verify"]["script"] == "scripts/check-hq-remote.sh"
    script = (_SCRIPTS_DIR / "check-hq-remote.sh").read_text(encoding="utf-8")
    assert "bh hq status" in script
    # Both lines are extracted independently, not folded into one summary marker.
    assert "git_line=" in script and "dolt_line=" in script
    prompt = _prompt(step)
    assert "bh hq push" in prompt
    assert "git:" in prompt and "dolt:" in prompt


def test_091_refuses_when_nix_is_absent_instead_of_installing_it() -> None:
    """ACCEPTANCE (.8) + ADR Decision 3: explain and stop; never take root for the user."""
    step = _step(91)
    prompt = _prompt(step)
    assert "Do not attempt to install nix" in prompt
    assert "do not install it under sudo on the user's behalf" in prompt
    refusal = next(c for c in step["on_failure"] if "NIX ABSENT" in c["reason"])
    assert refusal["strategy"] == "abort"
    # Aborting an ORTHOGONAL rung must not read as aborting the run.
    assert "Abort THIS STEP, not the run" in refusal["reason"]
    assert "orthogonal" in prompt.lower()


def test_092_checks_the_rung_2_prerequisite_before_the_role_choice() -> None:
    """ACCEPTANCE (.8): prerequisite FIRST — a worker joins by cloning HQ.

    docs/ONBOARDING.md's second-machine section exists to prevent exactly the alternative:
    meeting the requirement as a provisioning failure halfway through the new machine.
    """
    step = _step(92)
    prompt = _prompt(step)
    assert prompt.index("check-hq-remote.sh") < prompt.index("executor")
    assert "BEFORE THE ROLE CHOICE" in prompt
    # The role choice is deliberately an `after` interaction: nothing is asked until the
    # prerequisite has been proven by the action's first move.
    role = next(i for i in step["interactions"] if i["id"] == "host-role")
    assert role["when"] == "after"
    assert len(role["choices"]) == 3
    blocked = next(c for c in step["on_failure"] if "RUNG 2 NOT REACHED" in c["reason"])
    assert blocked["strategy"] == "abort"


def test_092_emits_the_command_rather_than_running_it() -> None:
    """ACCEPTANCE (.8): rung 4 runs on a machine this Guide is not on."""
    step = _step(92)
    assert step["effect"] == "none", "a pointer step must not claim to change anything"
    assert step["action"]["type"] == "prompt"
    prompt = _prompt(step)
    assert "bh host provision --role executor" in prompt
    assert "EMIT the command to run THERE" in prompt


def test_092_carries_rung_4s_three_line_gap_note() -> None:
    """ACCEPTANCE (.8): the three beads, so advisory-only enforcement is not discovered by
    hitting it."""
    prompt = _prompt(_step(92))
    for bead in ("bh-ban1j", "bh-tx2hp", "bh-i7ws9"):
        assert bead in prompt, f"rung 4's gap note must name {bead}"


def test_no_second_rung_vocabulary_is_minted() -> None:
    """ACCEPTANCE (.8): rung names and ordering match docs/ADOPTION.md.

    Only four rungs exist, numbered 1-4, and the steps name them by number. This catches the
    cheap failure — inventing a fifth rung, or renaming one — not a paraphrase.
    """
    text = "\n".join(p.read_text(encoding="utf-8") for p in _step_files())
    stray = {m for m in re.findall(r"\brung[- ]?(\d+)", text, flags=re.IGNORECASE)}
    assert stray <= {"1", "2", "3", "4"}, f"steps name rungs outside ADOPTION.md's four: {stray}"
    # The step ids carry the numbering, and each names the rung it is.
    assert _step(90)["id"] == "rung2-hq-remote"
    assert _step(91)["id"] == "rung3-toolchain"
    assert _step(92)["id"] == "rung4-second-host"
