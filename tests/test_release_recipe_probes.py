"""`just attest` / `just release-preview` capability probes (bh-k5te9), driven for real.

The gap these close was MEASURED, not imagined: 2026-08-16, one commit after bh-0jndj merged, an
operator ran `just release-preview` with an installed `bh` one commit behind and got typer's raw
`No such command 'preview'` — with nothing in it saying the answer was `just install`. The two
older recipes in the same file (`_await-bump-gate`, `_refuse-if-bump-pending`, covered by
tests/test_push_bump_gate.py) already degrade helpfully; these two did not.

THE ONE DIFFERENCE THIS FILE EXISTS TO PIN. Those two are gates on an ordinary push, so they warn
and FAIL OPEN. These two are commands an operator invoked deliberately and whose entire output is
the point, so they warn and FAIL — `attest` exiting 0 having attested nothing is worse than a
clean failure, because the next command is then slow for an unexplained reason.

Same method as tests/test_push_bump_gate.py: stub `bh` via `BH_EXEC` (the escape hatch the
recipes themselves document) and run real `just`. No hive, no network.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JUSTFILE = ROOT / "justfile"

needs_just = pytest.mark.skipif(shutil.which("just") is None, reason="needs just")


def _stub_bh(
    tmp_path: Path, *, has_if_needed: bool = True, has_preview: bool = True, has_next: bool = True
) -> Path:
    """A `bh` stand-in answering the two probes and echoing whatever verb call it is handed.

    A missing verb prints NOTHING and exits non-zero, exactly as typer does — which is what the
    `release preview --help` probe reads as absence."""
    script = tmp_path / "bh"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "release" ] && [ "$2" = "attest" ] && [ "$3" = "--help" ]; then\n'
        f'  echo "--background --gate{" --if-needed" if has_if_needed else ""}"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "release" ] && [ "$2" = "preview" ] && [ "$3" = "--help" ]; then\n'
        + ("" if has_preview else "  echo \"No such command 'preview'.\" >&2\n  exit 2\n")
        + f'  echo "--tag --remote --gate{" --next" if has_next else ""}"\n'
        "  exit 0\n"
        "fi\n"
        'echo "RAN: $*"\n'
    )
    script.chmod(0o755)
    return script


def _run(bh: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["just", "-f", str(JUSTFILE), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env={**os.environ, "BH_EXEC": str(bh)},
        check=False,
    )


@needs_just
def test_attest_on_an_old_bh_fails_and_says_just_install(tmp_path):
    """The whole point of criterion 2: a missing verb must not let `just attest` exit 0 having
    attested nothing."""
    res = _run(_stub_bh(tmp_path, has_if_needed=False), "attest")

    assert res.returncode != 0, res.stdout
    assert "NOTHING WAS ATTESTED" in res.stderr
    assert "just install" in res.stderr
    assert "BH_EXEC" in res.stderr
    assert "0.11.5" in res.stderr  # which bh is old enough to be the cause


@needs_just
def test_attest_on_a_current_bh_runs_the_verb_unchanged(tmp_path):
    res = _run(_stub_bh(tmp_path), "attest")

    assert res.returncode == 0, res.stderr
    assert "RAN: release attest --if-needed --gate just check-all" in res.stdout


@needs_just
def test_release_preview_on_an_old_bh_fails_and_says_just_install(tmp_path):
    """The exact failure the operator hit, now answerable from the message alone."""
    res = _run(_stub_bh(tmp_path, has_preview=False), "release-preview")

    assert res.returncode != 0, res.stdout
    assert "no `release preview`" in res.stderr
    assert "NOTHING WAS CHECKED" in res.stderr
    assert "just install" in res.stderr
    assert "BH_EXEC" in res.stderr
    assert "0.11.5" in res.stderr


@needs_just
def test_release_preview_probes_the_flag_it_was_asked_for_too(tmp_path):
    """One level down and one bead younger: a `bh` that HAS `preview` but not `--next` answers
    `No such option: --next`, which is the same cryptic failure. So the probe reads what THIS
    invocation needs — and a bare `release-preview` against that same bh must still work."""
    bh = _stub_bh(tmp_path, has_next=False)

    res = _run(bh, "release-preview", "--next")
    assert res.returncode != 0, res.stdout
    assert "no `release preview --next`" in res.stderr
    assert "just install" in res.stderr

    assert _run(bh, "release-preview").returncode == 0  # the flag is not needed, so not required


@needs_just
def test_release_preview_passes_flags_through(tmp_path):
    """`--next` has to reach the verb, or the superset half of the recipe is unreachable."""
    res = _run(_stub_bh(tmp_path), "release-preview", "--next")

    assert res.returncode == 0, res.stderr
    assert "RAN: release preview --gate just check-all --next" in res.stdout


@needs_just
def test_both_preview_summaries_state_the_superset_relationship():
    """Criterion 10. `release-preview --next` runs what `bump-preview` runs, so an operator must
    be able to see from `just --list` alone that one covers the other — that property is real but
    invisible unless it is written down where they choose between them."""
    listing = subprocess.run(
        ["just", "-f", str(JUSTFILE), "--list"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    summaries = {
        line.split()[0]: line
        for line in listing.splitlines()
        if line.startswith(("    bump", "    release"))
    }

    assert "release-preview" in summaries["bump-preview"]
    assert "bump-preview" in summaries["release-preview"]


def test_the_next_version_lookup_has_exactly_one_implementation():
    """Criterion 9, guarded where it can actually drift: `bump-preview` and `bh release preview
    --next` must both go through scripts/next-version.sh. A second `cz bump --dry-run` spelled in
    either caller is how two answers to one question appear."""
    script = ROOT / "scripts" / "next-version.sh"
    assert script.exists() and os.access(script, os.X_OK)

    justfile = JUSTFILE.read_text()
    body = justfile.split("\nbump-preview:\n", 1)[1].split("\n\n", 1)[0]
    assert "next-version.sh" in body
    assert "cz bump" not in body  # asked through the shared script, never re-spelled here

    release_py = (ROOT / "src" / "beadhive" / "release.py").read_text()
    assert "next-version.sh" in release_py
    assert "cz bump" not in release_py.replace("`cz bump`", "")  # prose may name it; code may not
