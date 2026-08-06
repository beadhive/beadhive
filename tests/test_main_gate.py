"""The main-merge gate (bh-dfz2) — the wiring that makes `just check-all` a gate rather than a
recipe someone must remember to type.

Three properties, each of which failed silently before this existed and would fail silently
again if the wiring were dropped:

* `check-all` refuses to run without `bd` — every integration test self-skips without it, so a
  `bd`-less host ran ZERO integration tests and reported GREEN;
* lefthook's `pre-push` carries the gate job, with stdin — the ref list is the only reliable
  signal for "is this the push that updates main";
* the gate fires on the ref actually being pushed, not on the checked-out branch — lefthook's
  own `only: {ref: main}` gets that wrong in the dangerous direction (`git push origin
  HEAD:main` from a side branch would skip it entirely).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "main-push-gate.sh"
ZERO = "0" * 40


def _recipe_deps(name: str) -> str:
    """The dependency list on `just` recipe `name` (the text between ':' and end of line)."""
    m = re.search(rf"^{re.escape(name)}:(.*)$", (ROOT / "justfile").read_text(), re.M)
    assert m, f"no `{name}` recipe in the justfile"
    return m.group(1)


def _pre_push_jobs() -> list[dict]:
    return YAML(typ="safe").load((ROOT / "lefthook.yml").read_text())["pre-push"]["jobs"]


def _run_gate(ref_lines: str, stub_dir: Path) -> subprocess.CompletedProcess:
    """Run the gate script with a `just` that only echoes, so the assertion is about WHETHER
    the full suite would run — not about running it (that is 11 minutes)."""
    stub = stub_dir / "just"
    stub.write_text('#!/bin/sh\necho JUST-RAN "$@"\n')
    stub.chmod(0o755)
    return subprocess.run(
        [str(GATE)],
        input=ref_lines,
        text=True,
        capture_output=True,
        env={"PATH": f"{stub_dir}:/usr/bin:/bin"},
        check=False,
    )


def test_check_all_cannot_report_green_without_bd():
    assert "require-bd" in _recipe_deps("check-all")


def test_the_fast_check_is_not_burdened_with_the_bd_requirement():
    """`check` excludes integration by construction, so the guard would only cost contributors
    without `bd` a gate they can legitimately run."""
    assert "require-bd" not in _recipe_deps("check")


def test_lefthook_pre_push_runs_the_main_gate_and_feeds_it_the_ref_list():
    job = next((j for j in _pre_push_jobs() if j.get("name") == "main-gate"), None)
    assert job, "lefthook pre-push lost the main-gate job — nothing gates main any more"
    assert job["run"] == "scripts/main-push-gate.sh"
    assert job["use_stdin"] is True, "without stdin the gate cannot see which ref is pushed"


def test_the_fence_still_gets_the_ref_list_too():
    """Both pre-push jobs read stdin; lefthook replays it to each. A regression here disarms
    the multi-host fence, which fails OPEN — silently."""
    fence = next((j for j in _pre_push_jobs() if j.get("name") == "bh-fence"), None)
    assert fence and fence["use_stdin"] is True


def test_a_main_push_runs_the_full_gate(tmp_path):
    res = _run_gate(f"refs/heads/main abc refs/heads/main {ZERO}\n", tmp_path)
    assert "JUST-RAN check-all" in res.stdout


def test_a_push_of_head_to_main_from_a_side_branch_still_runs_the_full_gate(tmp_path):
    """The case lefthook's `only: {ref: main}` misses — the gate must key off the REMOTE ref."""
    res = _run_gate("HEAD abc refs/heads/main def\n", tmp_path)
    assert "JUST-RAN check-all" in res.stdout


def test_a_bead_branch_push_does_not_run_the_full_gate(tmp_path):
    res = _run_gate("refs/heads/wt/bead/issue/x abc refs/heads/wt/bead/issue/x def\n", tmp_path)
    assert res.returncode == 0
    assert "JUST-RAN" not in res.stdout


def test_deleting_the_branch_does_not_run_the_full_gate(tmp_path):
    """`git push origin :main` pushes no tree to test."""
    res = _run_gate(f"(delete) {ZERO} refs/heads/main abc\n", tmp_path)
    assert res.returncode == 0
    assert "JUST-RAN" not in res.stdout
