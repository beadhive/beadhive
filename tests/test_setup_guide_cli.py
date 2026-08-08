"""`bh setup guide` (bh-0olv9.6) — export, handoff, and the CLI wizard fallback.

THREE THINGS THESE TESTS EXIST TO STOP.

**An export that produces an unrunnable tree.** The source tree and the wheel were both
asserted executable; the exported copy — the only one a user ever runs — was not, and shipped at
0644 for exactly that reason. The lesson generalizes past the mode: assert the ARTIFACT, not its
inputs.

**A silent clobber.** The export writes into a directory the user owns and is invited to edit
(the whole point of exporting is that they own the copy). Overwriting an edit without saying so
is the one behaviour that would make people stop re-running the verb, so "differs → left alone
→ named in the report" is asserted directly, as is `--force` being the only thing that changes
it.

**Wizard drift.** The wizard duplicates the Guide's control flow in Python, so every step added
to `steps/` is a step it can silently lack — the bead names this as the part that will rot. The
mitigation is that its step list is DERIVED from the step files, and the assertion that keeps
the mitigation honest is here: the walk covers every step file in the exported guide, and it
does so for step files this test invents, which no hardcoded list could satisfy.

The `steps/` dir is legitimately EMPTY today (bh-0olv9.4/.5/.8 are being written in parallel),
so the coverage test against the real bundle would pass vacuously. It is kept anyway — it is the
one that turns red if a future wizard grows a parallel list — and paired with a synthetic-guide
test that is non-vacuous now.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import setup_guide
from beadhive.cli import app

runner = CliRunner()


@pytest.fixture()
def ws_home(tmp_path, monkeypatch):
    """Redirect ~/.beadhive so the export never touches the operator's real home."""
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    return tmp_path


def _write_step(root, filename: str, step_id: str, title: str, extra: str = "") -> None:
    (root / "steps").mkdir(parents=True, exist_ok=True)
    (root / "steps" / filename).write_text(
        "---\nstep:\n"
        f"  id: {step_id}\n"
        f"  title: {title}\n"
        "  performer: agent\n"
        f"{extra}"
        "---\n\nbody text\n",
        encoding="utf-8",
    )


# ---- 1. export ---------------------------------------------------------------


def test_export_lands_the_bundled_guide_under_beadhive_home(ws_home):
    """ACCEPTANCE: `bh setup guide` exports to ~/.beadhive/guides/setup/."""
    result = runner.invoke(app, ["setup", "guide", "--handoff"])
    assert result.exit_code == 0
    root = setup_guide.export_root()
    assert root == ws_home / "guides" / "setup"
    assert (root / "GUIDE.md").is_file()
    assert (root / "SKILL.md").is_file()
    # Every shippable bundled file, not just the envelope — so the step/script beads inherit
    # the guarantee without touching this test.
    for src in setup_guide._bundled_files(setup_guide.bundled_root()):
        assert (root / src.relative_to(setup_guide.bundled_root())).is_file()
    # ...and the structure, including the dirs that are empty until bh-0olv9.4/.5/.8 land: the
    # handoff tells the reader to walk `steps/`, so it has to be there to walk.
    assert (root / "steps").is_dir()
    assert (root / "scripts").is_dir()


def test_every_executable_in_the_bundle_is_executable_once_exported(ws_home):
    """The exported copy is the ONLY one a user ever runs, so it is the one that has to be
    executable. `tests/test_setup_guide_steps.py` asserts this on the source tree and the
    packaging test asserts it on the wheel; both were green while every exported script sat at
    0644, because nothing looked at the artifact in between.

    Vacuous in this branch alone (`scripts/` ships empty until bh-0olv9.4/.5/.8 land) and kept
    for exactly that reason — it is the tripwire that fires the moment real scripts arrive.
    :func:`test_an_exported_script_actually_runs` is the non-vacuous half.
    """
    setup_guide.export()
    src_root, root = setup_guide.bundled_root(), setup_guide.export_root()
    for src in setup_guide._bundled_files(src_root):
        if os.access(src, os.X_OK):
            dst = root / src.relative_to(src_root)
            assert os.access(dst, os.X_OK), f"{dst} lost the execute bit the bundle ships"


def _synthetic_bundle(tmp_path, monkeypatch) -> Path:
    """A bundle with an executable script in it, standing in for `scripts/*.sh` until the
    step/script beads land."""
    bundle = tmp_path / "bundle"
    (bundle / "scripts").mkdir(parents=True)
    (bundle / "GUIDE.md").write_text("---\n---\n", encoding="utf-8")
    script = bundle / "scripts" / "check-thing.sh"
    script.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(setup_guide, "bundled_root", lambda: bundle)
    return bundle


def test_an_exported_script_actually_runs(ws_home, tmp_path, monkeypatch):
    """REGRESSION (bounce on bh-0olv9.6): `shutil.copyfile` copies content only, so 0755 -> 0644
    and the exported script exits 126 — "Permission denied", which is not among the codes any
    step's handler contracts for (060 branches on 0/1/3), so the handler falls through and
    "fixes" a machine that was already fine.

    Executing it is the assertion, not just `os.access`: the step files invoke these directly.
    """
    _synthetic_bundle(tmp_path, monkeypatch)

    setup_guide.export()

    exported = setup_guide.export_root() / "scripts" / "check-thing.sh"
    assert os.access(exported, os.X_OK), "exported script is not executable"
    assert subprocess.run([str(exported)], check=False).returncode == 3, "126 means unrunnable"


def test_a_stale_non_executable_export_is_repaired(ws_home, tmp_path, monkeypatch):
    """Anyone who exported under the broken build has bytes that MATCH the bundle, so the file is
    `unchanged` forever and no upgrade would ever restore its execute bit. The mode is repaired
    on any destination, not only on the ones we rewrite."""
    _synthetic_bundle(tmp_path, monkeypatch)
    setup_guide.export()
    exported = setup_guide.export_root() / "scripts" / "check-thing.sh"
    exported.chmod(0o644)

    results = setup_guide.export()

    assert {r.status for r in results} == {setup_guide.UNCHANGED}, "no byte should have moved"
    assert os.access(exported, os.X_OK)


def test_dry_run_never_touches_the_mode_either(ws_home, tmp_path, monkeypatch):
    _synthetic_bundle(tmp_path, monkeypatch)
    setup_guide.export()
    exported = setup_guide.export_root() / "scripts" / "check-thing.sh"
    exported.chmod(0o644)

    setup_guide.export(dry_run=True)

    assert not os.access(exported, os.X_OK), "dry-run mutated the export"


def test_export_is_idempotent_and_reports_what_changed(ws_home):
    """ACCEPTANCE: re-running is idempotent and reports what changed."""
    first = setup_guide.export()
    assert first and {r.status for r in first} == {setup_guide.CREATED}
    second = setup_guide.export()
    assert {r.status for r in second} == {setup_guide.UNCHANGED}
    assert "unchanged" in setup_guide.summarize(second)


def test_a_local_edit_is_never_silently_overwritten(ws_home):
    """ACCEPTANCE: local edits are never silently overwritten — told, not surprised."""
    setup_guide.export()
    edited = setup_guide.export_root() / "GUIDE.md"
    edited.write_text("MY OWN NOTES\n", encoding="utf-8")

    result = runner.invoke(app, ["setup", "guide", "--handoff"])

    assert edited.read_text(encoding="utf-8") == "MY OWN NOTES\n", "bh clobbered a user edit"
    assert "NOT overwritten" in result.stdout
    assert "GUIDE.md" in result.stdout
    assert "--force" in result.stdout


def test_force_is_what_takes_the_bundled_copy(ws_home):
    setup_guide.export()
    edited = setup_guide.export_root() / "GUIDE.md"
    edited.write_text("MY OWN NOTES\n", encoding="utf-8")

    runner.invoke(app, ["setup", "guide", "--handoff", "--force"])

    assert edited.read_text(encoding="utf-8").startswith("---")


def test_dry_run_changes_nothing(ws_home):
    result = runner.invoke(app, ["setup", "guide", "--dry-run"])
    assert result.exit_code == 0
    assert "would export" in result.stdout
    assert not setup_guide.export_root().exists()


def test_a_file_the_bundle_does_not_own_is_left_alone_and_named(ws_home):
    """A user's own note beside the exported guide is not deleted by a re-export — and neither
    would a step removed upstream be."""
    setup_guide.export()
    stray = setup_guide.export_root() / "my-notes.md"
    stray.write_text("mine\n", encoding="utf-8")

    result = runner.invoke(app, ["setup", "guide", "--handoff"])

    assert stray.exists()
    assert "my-notes.md" in result.stdout


# ---- 2. handoff --------------------------------------------------------------


def test_handoff_names_the_exported_guide_by_path(ws_home):
    """ACCEPTANCE: prints the harness handoff naming the exported GUIDE.md."""
    result = runner.invoke(app, ["setup", "guide", "--handoff"])
    assert str(setup_guide.export_root() / "GUIDE.md") in result.stdout
    # All three readers, because bh cannot probe which one is reading (module docstring).
    assert "Guide-aware harness" in result.stdout
    assert "plain-Skill harness" in result.stdout
    assert "--wizard" in result.stdout


def test_non_interactive_never_prompts(ws_home):
    """A harness capturing this output must not be blocked on a confirm it cannot answer."""
    result = runner.invoke(app, ["setup", "guide"])
    assert result.exit_code == 0
    assert "Walk it here now" not in result.stdout


def test_wizard_and_handoff_together_are_refused(ws_home):
    result = runner.invoke(app, ["setup", "guide", "--wizard", "--handoff"])
    assert result.exit_code == 2


def test_setup_help_documents_guide_alongside_the_other_three(ws_home):
    """ACCEPTANCE: `bh setup --help` documents it alongside check/show/toolchain."""
    out = runner.invoke(app, ["setup", "--help"]).stdout
    for verb in ("check", "show", "toolchain", "guide"):
        assert verb in out


# ---- 3. step discovery is DERIVED -------------------------------------------


def test_discovery_reads_whatever_ships_including_names_nobody_wrote_down(ws_home, tmp_path):
    """The anti-drift property, proven against step files this test invents: no list in the
    module could know these names, so a walk that covers them is derived by construction."""
    root = tmp_path / "guide"
    _write_step(root, "010-alpha.md", "alpha", "Alpha step")
    _write_step(root, "090-omega.md", "omega", "Omega step")
    _write_step(root, "050-middle.md", "middle", "Middle step")

    steps = setup_guide.discover_steps(root)

    assert [s.id for s in steps] == ["alpha", "middle", "omega"], "numbering is the ordering"
    assert [s.title for s in steps] == ["Alpha step", "Middle step", "Omega step"]


def test_an_unparseable_step_is_still_walked(ws_home, tmp_path):
    """Dropping a malformed step silently is the exact failure the derivation prevents — so it
    degrades to a filename-derived title instead of disappearing."""
    root = tmp_path / "guide"
    (root / "steps").mkdir(parents=True)
    (root / "steps" / "020-broken.md").write_text("---\nstep: [not, a, mapping\n", encoding="utf-8")

    steps = setup_guide.discover_steps(root)

    assert len(steps) == 1
    assert steps[0].id == "020-broken"
    assert steps[0].title == "broken"


def test_no_steps_dir_is_not_a_crash(ws_home, tmp_path):
    """`steps/` is legitimately empty while bh-0olv9.4/.5/.8 are in flight."""
    assert setup_guide.discover_steps(tmp_path / "nowhere") == []


# ---- 4. the wizard covers every step ----------------------------------------


def _record(lines):
    return lambda text: lines.append(text)


def test_wizard_covers_every_step_in_the_exported_guide(ws_home):
    """ACCEPTANCE: a test asserts the wizard covers every step present in the exported guide.

    Vacuous while `steps/` is empty and deliberately kept: it is what turns red if the wizard
    ever grows a step list of its own beside the files.
    """
    setup_guide.export()
    root = setup_guide.export_root()
    on_disk = sorted(p.name for p in (root / "steps").glob("*.md"))
    walked = [s.file.name for s in setup_guide.discover_steps(root)]
    assert walked == on_disk


def test_the_walk_visits_every_discovered_step(tmp_path):
    """The non-vacuous half: drive the walk over a synthetic guide and assert every step's title
    was shown. Covers the ordering too — a wizard that walks 3 of 3 in the wrong order is still
    wrong."""
    root = tmp_path / "guide"
    _write_step(root, "010-first.md", "first", "First step")
    _write_step(root, "020-second.md", "second", "Second step")
    _write_step(root, "030-third.md", "third", "Third step")
    seen: list[str] = []

    rc = setup_guide.wizard(root, ask=lambda prompt, default: "y", echo=_record(seen))

    assert rc == 0
    shown = "\n".join(seen)
    for index, title in enumerate(["First step", "Second step", "Third step"], start=1):
        assert f"[{index}/3] {title}" in shown


def test_the_walk_offers_a_step_command_and_can_be_quit(tmp_path):
    """A `command` step is OFFERED, never run unasked — the Guide's own tenet, in the fallback."""
    root = tmp_path / "guide"
    marker = tmp_path / "ran"
    _write_step(
        root,
        "010-run.md",
        "run",
        "Run something",
        extra=f"  action:\n    type: command\n    command: touch {marker}\n",
    )
    seen: list[str] = []

    rc = setup_guide.wizard(root, ask=lambda prompt, default: "q", echo=_record(seen))

    assert rc == 1
    assert not marker.exists(), "declining must not run the command"
    assert f"$ touch {marker}" in "\n".join(seen)

    rc = setup_guide.wizard(root, ask=lambda prompt, default: "y", echo=_record(seen))
    assert rc == 0
    assert marker.exists()


def test_the_walk_reports_an_empty_guide_rather_than_crashing(ws_home, tmp_path):
    seen: list[str] = []
    rc = setup_guide.wizard(tmp_path / "empty", ask=lambda p, d: "y", echo=_record(seen))
    assert rc == 0
    assert "No steps found" in "\n".join(seen)
