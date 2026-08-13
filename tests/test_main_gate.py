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


def _run_gate(
    ref_lines: str, stub_dir: Path, *, gate_exit: int = 0, env: dict | None = None
) -> subprocess.CompletedProcess:
    """Run the gate script with a `just` that only echoes, so the assertion is about WHETHER
    the full suite would run — not about running it (that is 11 minutes). `gate_exit` makes
    that stub fail, which is how the red-gate path is exercised without a red suite."""
    stub = stub_dir / "just"
    stub.write_text(f'#!/bin/sh\necho JUST-RAN "$@"\nexit {gate_exit}\n')
    stub.chmod(0o755)
    return subprocess.run(
        [str(GATE)],
        input=ref_lines,
        text=True,
        capture_output=True,
        env={"PATH": f"{stub_dir}:/usr/bin:/bin", **(env or {})},
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


# ---- a green gate is not a landed push (bh-53o8f) -------------------------------------------
#
# git opens its connection to the remote BEFORE this hook runs (the hook needs the remote's ref
# list on stdin), so the socket is idle for the whole ~390s gate and GitHub closes it. git then
# finishes a fully GREEN gate and writes to a dead socket: exit 141, "failed to push some refs",
# remote unchanged. Measured three times pushing 0.11.2 — and one of those runs was REPORTED AS
# SUCCESSFUL on the strength of the green output, because the SIGPIPE arrives after the last
# thing anyone reads.

MAIN_PUSH = f"refs/heads/main abc refs/heads/main {ZERO}\n"
KEEPALIVE = {"GIT_SSH_COMMAND": "ssh -o ServerAliveInterval=30"}


def test_a_green_gate_says_the_push_has_not_happened_yet(tmp_path):
    """The last line before git writes to a possibly-dead socket must not be six minutes of
    green. An operator who reads only the tail of this output has to come away knowing the
    push is still ahead of them."""
    res = _run_gate(MAIN_PUSH, tmp_path, env=KEEPALIVE)
    assert res.returncode == 0
    assert "THE PUSH HAS NOT HAPPENED YET" in res.stderr
    assert "git ls-remote" in res.stderr


def test_a_green_gate_names_a_later_transport_failure_as_the_transport(tmp_path):
    """bh-53o8f AC3: a push that fails AFTER a green gate must be distinguishable from a failed
    suite. The hook cannot see git's later SIGPIPE, so it pre-empts the misreading: it says what
    exit 141 will mean if it appears next."""
    res = _run_gate(MAIN_PUSH, tmp_path, env=KEEPALIVE)
    assert "141" in res.stderr and "SIGPIPE" in res.stderr
    assert "THAT IS THE TRANSPORT, NOT THE CODE" in res.stderr
    assert "--no-verify" in res.stderr  # …named as the thing NOT to reach for


def test_a_red_gate_is_not_dressed_up_as_a_transport_problem(tmp_path):
    """The other half: a failing suite must still fail, loudly and as a SUITE failure, with the
    hook's exit code preserved. The green banner must not print."""
    res = _run_gate(MAIN_PUSH, tmp_path, gate_exit=3, env=KEEPALIVE)
    assert res.returncode == 3
    assert "gate FAILED" in res.stderr
    assert "THE PUSH HAS NOT HAPPENED YET" not in res.stderr


def test_a_push_with_no_keepalive_is_warned_before_it_spends_six_minutes(tmp_path):
    res = _run_gate(MAIN_PUSH, tmp_path)
    assert "no SSH keepalive" in res.stderr
    assert "just push" in res.stderr


def test_a_push_that_already_carries_a_keepalive_is_not_nagged(tmp_path):
    """An operator who did the right thing must not be told to do it — a warning that fires
    unconditionally is a warning that gets tuned out."""
    res = _run_gate(MAIN_PUSH, tmp_path, env=KEEPALIVE)
    assert "no SSH keepalive" not in res.stderr


# ---- …and the wrapper that makes the mitigation permanent -----------------------------------

PUSH = ROOT / "scripts" / "push-main.sh"


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(path), "config", k, v], check=True)


def test_the_push_wrapper_supplies_the_keepalive_and_verifies_the_remote(tmp_path):
    """End to end against a local bare remote: the wrapper pushes, then confirms the sha the
    REMOTE actually holds. `origin/main` is a local tracking ref a failed push never updates —
    reading it is how run 1 was reported as a success while the remote sat unchanged."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    work = tmp_path / "work"
    _init_repo(work)
    (work / "f.txt").write_text("hi\n")
    subprocess.run(["git", "-C", str(work), "add", "f.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-qm", "chore: seed", "--no-verify"], check=True
    )
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(remote)], check=True)

    res = subprocess.run(
        [str(PUSH), "origin", "main"], cwd=work, capture_output=True, text=True, check=False
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "ServerAliveInterval=30" in res.stderr
    head = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "refs/heads/main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    on_remote = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "refs/heads/main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == on_remote
    assert head[:12] in res.stderr and "verified with ls-remote" in res.stderr


def test_the_push_wrapper_fails_loudly_when_the_ref_does_not_move(tmp_path):
    """A rejected push must read as "THE PUSH DID NOT LAND", with the remote's actual sha —
    never as the bare 'failed to push some refs' that named neither cause nor consequence."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    seed = tmp_path / "seed"
    _init_repo(seed)
    (seed / "a.txt").write_text("a\n")
    subprocess.run(["git", "-C", str(seed), "add", "a.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "commit", "-qm", "chore: theirs", "--no-verify"], check=True
    )
    subprocess.run(["git", "-C", str(seed), "push", "-q", str(remote), "main"], check=True)

    # A divergent local history — the push is rejected as non-fast-forward.
    work = tmp_path / "work"
    _init_repo(work)
    (work / "b.txt").write_text("b\n")
    subprocess.run(["git", "-C", str(work), "add", "b.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-qm", "chore: mine", "--no-verify"], check=True
    )
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(remote)], check=True)

    res = subprocess.run(
        [str(PUSH), "origin", "main"], cwd=work, capture_output=True, text=True, check=False
    )
    assert res.returncode != 0
    assert "THE PUSH DID NOT LAND" in res.stderr


def test_the_exit_code_through_a_pipe_trap_is_written_down_where_a_pusher_will_hit_it():
    """bh-53o8f AC4. `git push | tail` returns tail's status, so the failure is invisible to any
    wrapper that pipes — the mistake that produced two false 'it pushed' reports in one evening.
    It has to be recorded next to the mitigation, not only in a bead."""
    for path in (PUSH, ROOT / "CONTRIBUTING.md"):
        text = path.read_text()
        assert "git push | tail" in text, path
        assert "pipefail" in text or "PIPESTATUS" in text, path


def test_the_keepalive_is_explained_where_someone_would_delete_it():
    """AC2: 'a future reader must not remove a keepalive as unexplained cruft.'"""
    text = PUSH.read_text()
    assert "ServerAliveInterval" in text
    assert "DO NOT REMOVE IT AS UNEXPLAINED CRUFT" in text
    assert "bh-53o8f" in text
