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
    prompt = _step(20)["action"]["prompt"]
    assert "managed_route.supported" in prompt
    assert "blocked_reason" in prompt
    assert "verbatim" in prompt
    probe = (_SCRIPTS_DIR / "preflight.sh").read_text(encoding="utf-8")
    assert "Intel macOS" in probe, "the probe must carry the reason it is forcing the route"


def test_020_offers_the_nix_installer_and_does_not_run_it() -> None:
    """ACCEPTANCE (.4) + ADR Decision 3: offer the command, explain it, WAIT."""
    step = _step(20)
    prompt = step["action"]["prompt"]
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
    prompt = step["action"]["prompt"]
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
