"""Guards on the `just local-install` router + its derived version pin (bh-q160.5).

`local-install` takes a checkout to a provisioned Linux host. Two of its promises cannot be
proved by reading the recipe on a laptop, and both have already been broken in the field:

- **steps 3-5 must not assume `bh` is on PATH.** `uv tool install` puts `bh` in
  `uv tool dir --bin` (~/.local/bin), which a fresh host does not have on PATH — measured under
  Nix on 2026-08-05. `export PATH=…` on an earlier recipe line cannot fix it, because just runs
  every line in its own shell. The recipe addresses `bh` absolutely; these tests hold that.
- **the version pin is derived, never typed.** A literal here would be a second place for the
  release version to be wrong, and "the tag names the PyPI release" would become discipline
  rather than construction.

The text contracts and the pin script need no toolchain and always run; the recipe tests skip
loudly without `just`. Everything that could touch a uv tool store is pointed at a tmp dir
first — this suite never installs into the machine running it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JUSTFILE = (ROOT / "justfile").read_text()
PIN_SCRIPT = ROOT / "scripts" / "release-pin.sh"

#: The `local-install` section: its banner through to the next one.
SECTION = JUSTFILE[
    JUSTFILE.index("# ---- local-install:") : JUSTFILE.index("# ---- container image")
]

#: The body of the private recipe that holds the ordered steps.
STEPS = SECTION[SECTION.index("_local-install:") + len("_local-install:") :]

#: Command lines in that body (the `@echo` labels are not commands).
COMMAND_LINES = [
    line.strip()
    for line in STEPS.splitlines()
    if line.startswith("    ") and not line.strip().startswith("@echo")
]


# ---- the PATH guarantee -----------------------------------------------------


def test_bh_is_addressed_absolutely_from_uvs_tool_bin_dir():
    """`_bh` must resolve through uv itself, not through the invoking shell's PATH."""
    assert '_bh := "$(uv tool dir --bin)/bh"' in SECTION


@pytest.mark.parametrize("verb", ["setup check", "harness auth --check"])
def test_steps_three_and_four_invoke_bh_through_that_path(verb):
    assert f'"{{{{ _bh }}}}" {verb}' in STEPS, (
        f"`bh {verb}` must be invoked as {{{{ _bh }}}}, not as a bare `bh` — a fresh host does "
        f"not have uv's tool bin dir on PATH."
    )


@pytest.mark.parametrize("verb", ["host provision --answers", "config init", "hq init"])
def test_step_five_invokes_bh_through_that_path_on_both_postures(verb):
    """STEP 5 MOVED, ITS GUARANTEE DID NOT (bh-vmdq.3). It resolves by posture now — `host` runs
    `host provision --answers`, `laptop` runs `config init && hq init` — so the command lives in
    the `_step5_cmd` variable rather than inline in the recipe body, and is built by just's
    string concatenation instead of `{{ }}` interpolation.

    The PATH guarantee is unchanged and still asserted: every verb on either posture is reached
    through `_bh`, never a bare `bh`. `test_no_step_invokes_a_bare_bh` below is the stronger
    check and covers both postures unchanged."""
    assert f"_bh + '\" {verb}" in SECTION, (
        f"`bh {verb}` must be reached through `_bh` on its posture branch — a fresh host does "
        f"not have uv's tool bin dir on PATH."
    )


def test_no_step_invokes_a_bare_bh():
    """The failure this guards is silent on a dev laptop and fatal on a fresh host."""
    bare = [line for line in COMMAND_LINES if re.search(r"(?<![/\w])bh\s", line.replace("_bh", ""))]
    assert bare == [], f"these lines rely on `bh` being on PATH: {bare}"


# ---- the derived pin --------------------------------------------------------


def test_the_pin_is_derived_not_typed():
    assert "$(scripts/release-pin.sh)" in SECTION


def test_no_version_literal_in_the_local_install_section():
    """A literal would be a second pin source, and the tag would name the release by discipline
    rather than by construction. Comments are exempt — they cite measured tool versions."""
    code = [line for line in SECTION.splitlines() if not line.lstrip().startswith("#")]
    literals = re.findall(r"\b\d+\.\d+\.\d+\b", "\n".join(code))
    assert literals == [], f"version literals must not appear here: {literals}"


def test_the_release_is_verified_before_anything_is_installed():
    """Skew must abort BEFORE `uv tool install`, not be discovered by it."""
    verify = STEPS.index("scripts/release-pin.sh --verify")
    install = STEPS.index("uv tool install")
    assert verify < install


# ---- ordering + plan mode ---------------------------------------------------


def test_the_five_steps_appear_in_the_order_the_bead_states():
    """Step 5's needle is its LABEL, not its command: the command resolves by posture and lives
    in `_step5_cmd` above the recipe (bh-vmdq.3). The ordering contract is unchanged — five
    steps, same sequence, on either posture."""
    offsets = [
        STEPS.index(needle)
        for needle in (
            "1. toolchain",
            "uv tool install",
            "setup check",
            "harness auth --check",
            "5. {{ _step5_label }}",
        )
    ]
    assert offsets == sorted(offsets)


def test_both_postures_keep_the_same_first_four_steps():
    """A FLAG, NOT A SECOND RECIPE. The whole argument for `posture=` over a parallel recipe is
    that steps 1-4 cannot drift apart — a laptop needs the same toolchain, the same bh and the
    same gates a fleet host does. Only step 5 is allowed to differ."""
    assert "_step5_label := if posture ==" in SECTION
    assert "_step5_cmd := if posture ==" in SECTION
    # the four shared steps are unconditional — no `posture` appears in their lines
    shared = [line for line in COMMAND_LINES if "_step5" not in line]
    assert [line for line in shared if "posture" in line] == []


def test_plan_mode_gates_every_executing_line():
    """`plan=1` mutates nothing only if every command carries the `{{ _do }}` prefix. The one
    exception is the release verification, which is a read and is deliberately run in plan mode
    too — so a preview catches a broken release before a run does."""
    ungated = [
        line
        for line in COMMAND_LINES
        if not line.startswith("@{{ _do }}") and "release-pin.sh --verify" not in line
    ]
    assert ungated == [], f"these run even under plan=1: {ungated}"


# ---- scripts/release-pin.sh, driven for real --------------------------------


def _fixture_checkout(tmp_path: Path, version: str, tag: str | None) -> Path:
    """A minimal checkout laid out the way the script expects: `<root>/scripts/release-pin.sh`."""
    (tmp_path / "scripts").mkdir()
    shutil.copy2(PIN_SCRIPT, tmp_path / "scripts" / "release-pin.sh")
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "beadhive"\nversion = "{version}"\nrequires-python = ">=3.11"\n'
    )

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    # Signing off, and an identity of its own: this repo's own config signs commits and forces
    # annotated tags, which a throwaway fixture can neither satisfy nor should inherit.
    for key, value in (
        ("user.name", "fixture"),
        ("user.email", "t@example.invalid"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
    ):
        git("config", key, value)
    git("add", "-A")
    git("commit", "-qm", "fixture")
    if tag:
        git("tag", tag)
    return tmp_path / "scripts" / "release-pin.sh"


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(script), *args], capture_output=True, text=True)


def test_an_untagged_checkout_pins_whatever_pyproject_says(tmp_path):
    """Every working tree between releases is untagged — that is not skew, there is simply no
    claim to contradict."""
    script = _fixture_checkout(tmp_path, "1.2.3", tag=None)
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1.2.3"


def test_a_matching_tag_verifies(tmp_path):
    script = _fixture_checkout(tmp_path, "1.2.3", tag="v1.2.3")
    result = _run(script, "--verify")
    assert result.returncode == 0, result.stderr
    assert "1.2.3" in result.stdout and "v1.2.3" in result.stdout


def test_a_tag_that_disagrees_with_pyproject_is_a_broken_release(tmp_path):
    """The acceptance criterion: this aborts, and prints no pin for a caller to splice."""
    script = _fixture_checkout(tmp_path, "1.2.3", tag="v9.9.9")
    for args in ((), ("--verify",)):
        result = _run(script, *args)
        assert result.returncode == 1, result.stdout
        assert result.stdout.strip() == ""
        assert "v9.9.9" in result.stderr and "1.2.3" in result.stderr


# ---- the recipe, driven for real --------------------------------------------

needs_just = pytest.mark.skipif(
    shutil.which("just") is None or shutil.which("uv") is None, reason="needs just + uv"
)


def _just(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run `just` against the real justfile with uv's tool store pointed at a tmp dir, so a
    mistake in this suite installs into the tmp dir rather than the machine."""
    env = {
        **os.environ,
        "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
    }
    return subprocess.run(["just", *args], cwd=ROOT, capture_output=True, text=True, env=env)


@needs_just
def test_plan_prints_all_five_steps_and_mutates_nothing(tmp_path):
    result = _just(tmp_path, "local-install", "plan=1")
    assert result.returncode == 0, result.stderr
    for step in ("1. toolchain", "2. uv tool install", "3. ", "4. ", "5. "):
        assert step in result.stdout
    assert not (tmp_path / "bin").exists(), "plan=1 installed something"


@needs_just
def test_plan_shows_bh_at_uvs_tool_bin_dir_not_a_bare_name(tmp_path):
    """The plan is the evidence for the PATH decision: it names the absolute path it will use."""
    result = _just(tmp_path, "local-install", "plan=1")
    assert f"{tmp_path / 'bin' / 'bh'} setup check" in result.stdout


@needs_just
def test_from_source_installs_the_checkout_and_the_default_installs_the_release(tmp_path):
    source = _just(tmp_path, "local-install", "plan=1", "from_source=1")
    default = _just(tmp_path, "local-install", "plan=1")
    assert "uv tool install .[otel]" in source.stdout
    assert re.search(r"uv tool install beadhive\[otel\]==\d", default.stdout), default.stdout


@needs_just
def test_docker_mode_is_refused_rather_than_silently_taking_the_native_path(tmp_path):
    result = _just(tmp_path, "local-install", "mode=docker", "plan=1")
    assert result.returncode != 0
    assert "bh-q160.7" in result.stderr


@needs_just
def test_an_unknown_setting_is_refused(tmp_path):
    """The forward to the private recipe keeps just's own typo guard — a misspelled setting
    must not be accepted and then ignored."""
    result = _just(tmp_path, "local-install", "answer=host.yaml", "plan=1")
    assert result.returncode != 0
    assert "answer" in result.stderr
