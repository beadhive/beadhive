"""Guards the bundled setup Guide asset (bh-0olv9.3).

Two contracts, both of which fail silently in the wild if they are only asserted by eye:

1. **Schema conformance.** ``GUIDE.md`` and ``SKILL.md`` are agentguides.io 0.1 artifacts
   (docs/design/setup-guide-adr.md, Decision 4). A third-party harness parses their
   frontmatter, so a typo there is not a rendering bug — it is an unloadable Guide. The 0.1
   schemas are vendored under ``tests/schemas/agentguides-0.1/`` rather than fetched, so the
   gate is deterministic and runs offline; refresh them from
   ``https://agentguides.io/schemas/0.1/<name>.schema.json`` when the family version moves.

2. **Packaging.** The Guide ships through the EXISTING ``src/beadhive/assets/`` mechanism with
   nothing added to the build (ADR Decision 1). That claim is only worth anything if it is
   checked against a built artifact — an asset that does not ship is invisible until a user
   installs. So the packaging test builds a wheel and UNPACKS it; it deliberately does not
   read ``pyproject.toml``, which is the assertion that was already believed to be true.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from ruamel.yaml import YAML

jsonschema = pytest.importorskip("jsonschema")

_REPO = Path(__file__).resolve().parents[1]
_GUIDE_DIR = _REPO / "src" / "beadhive" / "assets" / "guides" / "setup"
_SCHEMAS = Path(__file__).resolve().parent / "schemas" / "agentguides-0.1"

# Where the Guide lands inside the wheel: `packages = ["src/beadhive"]` strips the `src/`.
_WHEEL_PREFIX = "beadhive/assets/guides/setup"


def _frontmatter(path: Path) -> dict:
    """Parse the YAML frontmatter block of a Markdown file."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name} must open with a YAML frontmatter fence"
    _, block, _ = text.split("---\n", 2)
    return YAML(typ="safe").load(block)


def _schema(name: str) -> dict:
    return json.loads((_SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


# --- 1. the artifact exists in the shape the step beads will land into ----------------------


def test_guide_directory_has_its_envelope_and_both_subdirs() -> None:
    """ACCEPTANCE: GUIDE.md, SKILL.md, steps/ and scripts/ all exist."""
    assert (_GUIDE_DIR / "GUIDE.md").is_file()
    assert (_GUIDE_DIR / "SKILL.md").is_file()
    assert (_GUIDE_DIR / "steps").is_dir()
    assert (_GUIDE_DIR / "scripts").is_dir()


# --- 2. schema conformance -----------------------------------------------------------------


@pytest.mark.parametrize("name", ["guide", "skill-guide-extension"])
def test_vendored_schema_is_a_valid_draft_2020_12_schema(name: str) -> None:
    """Catches a truncated or hand-edited vendored copy before it silently passes everything."""
    jsonschema.Draft202012Validator.check_schema(_schema(name))


def test_guide_frontmatter_validates_against_the_0_1_guide_schema() -> None:
    """ACCEPTANCE: GUIDE.md validates against the agentguides.io 0.1 guide schema."""
    fm = _frontmatter(_GUIDE_DIR / "GUIDE.md")
    jsonschema.Draft202012Validator(_schema("guide")).validate(fm)


def test_skill_frontmatter_validates_against_the_guide_extension_schema() -> None:
    """ACCEPTANCE: SKILL.md validates against the skill-guide-extension schema."""
    fm = _frontmatter(_GUIDE_DIR / "SKILL.md")
    jsonschema.Draft202012Validator(_schema("skill-guide-extension")).validate(fm)
    # The two halves of Decision 4's degradation contract: a Guide-aware harness needs the
    # entry pointer, a plain-Skill harness needs to be told what to do without it.
    assert fm["metadata"]["type"] == "guide"
    assert fm["metadata"]["guide"]["entry"] == "GUIDE.md"
    assert "runbook" in (_GUIDE_DIR / "SKILL.md").read_text(encoding="utf-8")


# --- 3. the decisions the ADR fixed, asserted where they are cheap to reverse ----------------


def test_three_scored_end_states_including_one_that_succeeds_short_of_rung_1() -> None:
    """ACCEPTANCE: three scored end states, one of which does NOT reach rung 1.

    A user who installs `bh` and stops has not failed, and the Guide must be able to say so —
    otherwise it has an incentive to push past a "not now".
    """
    end_states = _frontmatter(_GUIDE_DIR / "GUIDE.md")["guide"]["end_states"]
    assert len(end_states) == 3
    by_id = {e["id"]: e for e in end_states}
    assert by_id["rung-1-reached"]["score"] == 1.0
    short_of_rung_1 = by_id["installed-unwired"]
    assert 0.0 < short_of_rung_1["score"] < 1.0
    assert by_id["aborted-clean"]["score"] < short_of_rung_1["score"]
    # Ordering is preference in this schema family; the goal state must lead.
    assert end_states[0]["id"] == "rung-1-reached"


def test_rollback_is_none_because_every_step_is_rerunnable() -> None:
    """Differs deliberately from the infra exemplar's `best-effort` (ADR, "differs")."""
    assert _frontmatter(_GUIDE_DIR / "GUIDE.md")["guide"]["rollback_strategy"] == "none"


def test_every_prerequisite_names_who_performs_it() -> None:
    """`performer` is what keeps Decision 3 honest: the nix prerequisite is a HUMAN's."""
    prereqs = _frontmatter(_GUIDE_DIR / "GUIDE.md")["guide"]["prerequisites"]
    assert prereqs, "a Guide that installs software has entry conditions"
    assert all(p.get("performer") in {"agent", "human"} for p in prereqs)
    assert any(p["performer"] == "human" for p in prereqs)


def test_guide_links_the_decision_record() -> None:
    """ACCEPTANCE: the ADR from bh-0olv9.1 is linked from GUIDE.md."""
    assert (_REPO / "docs" / "design" / "setup-guide-adr.md").is_file()
    assert "docs/design/setup-guide-adr.md" in (_GUIDE_DIR / "GUIDE.md").read_text(encoding="utf-8")


# --- 4. packaging: the point of the bead ----------------------------------------------------


@pytest.mark.skipif(shutil.which("uv") is None, reason="wheel build needs the `uv` binary")
def test_built_wheel_actually_contains_every_guide_file(tmp_path: Path) -> None:
    """ACCEPTANCE: `uv build` produces a wheel CONTAINING the guide files.

    Verified by unpacking the wheel, not by inspecting pyproject — the whole failure mode this
    bead exists to prevent is a declaration that looks right and an artifact that ships without
    the asset. Asserting over the whole tree (not just GUIDE.md) means the step and script
    beads inherit the guarantee without touching this test.
    """
    out = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out)],
        cwd=_REPO,
        check=True,
        capture_output=True,
    )
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"

    with zipfile.ZipFile(wheels[0]) as zf:
        shipped = set(zf.namelist())

    expected = {
        f"{_WHEEL_PREFIX}/{p.relative_to(_GUIDE_DIR).as_posix()}"
        for p in _GUIDE_DIR.rglob("*")
        if p.is_file() and p.name != ".gitkeep"
    }
    assert expected, "no guide files on disk to check — the source tree is empty"
    missing = sorted(expected - shipped)
    assert not missing, f"guide files absent from the built wheel: {missing}"
