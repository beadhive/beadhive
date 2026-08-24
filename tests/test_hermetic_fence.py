"""The bubblewrap fence around the suite (bh-pxoby, fixing bh-njdxk's first contributing factor).

Two halves, because they can fail independently:

* Tests that run ONLY INSIDE the fence (`BH_HERMETIC_FENCE=1`, set by `scripts/hermetic.sh`) and
  assert the boundary actually holds — the host is read-only, the operator's HOME state is gone,
  external network is unreachable. These skip outside the fence rather than pretending to pass,
  so a fenced run proves the fence and an unfenced run says so.
* Tests that run ANYWHERE and assert the fence stays WIRED — the wrapper exists, is executable,
  and the gate goes through it. A fence nothing invokes is the failure mode bh-njdxk already had:
  "it happened to work outside GIT_WORKSPACE" is not isolation.

What the fence does NOT claim: the checkout under test is writable, because the suite legitimately
writes there (.venv, .pytest_cache). The boundary is everything else.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WRAPPER = REPO / "scripts" / "hermetic.sh"

inside_fence = pytest.mark.skipif(
    os.environ.get("BH_HERMETIC_FENCE") != "1",
    reason="only meaningful inside the bwrap fence — run via scripts/hermetic.sh",
)


# ---- the boundary itself (fenced runs only) --------------------------------------------------


@inside_fence
def test_the_checkout_itself_is_still_writable():
    """The fence must not be so tight the suite cannot run: pytest writes .pytest_cache and uv
    writes .venv inside the checkout. Pinning this stops a future tightening from being 'fixed'
    by loosening .git back to read-write."""
    probe = REPO / ".bh-fence-writable-probe"
    try:
        probe.write_text("ordinary in-checkout write")
        assert probe.read_text() == "ordinary in-checkout write"
    finally:
        probe.unlink(missing_ok=True)


@inside_fence
def test_the_operators_home_state_is_not_resolvable():
    """`bd` resolves configuration by walking UP from cwd and from BEADS_* env, which is why the
    same code passed outside GIT_WORKSPACE and failed inside a live hive. A tmpfs HOME takes
    ~/.beads and ~/.gitconfig out of that walk entirely — the mechanism, not a symptom.

    Asserts on the DEVICE, which is what makes this load-bearing without being order-dependent.
    Two weaker versions were tried and both were wrong: "these paths are absent" fails on test
    ORDER (an ordinary run provisions hives under the tmpfs HOME), and "HOME is a tmpfs" passes
    even when the operator's real ~/.beads is bound straight back in — a review demonstrated
    exactly that. A bind from the host necessarily lands on a DIFFERENT device than the tmpfs, so
    "every path bd's walk consults is on HOME's own device" catches the re-bind and does not care
    what the run created."""
    home = Path.home()
    mounts = Path("/proc/mounts").read_text().splitlines()
    home_mount = [m.split() for m in mounts if len(m.split()) > 2 and m.split()[1] == str(home)]

    assert home_mount, f"{home} is not its own mount — HOME is the host's, not a fenced tmpfs"
    assert home_mount[-1][2] == "tmpfs", f"{home} is a {home_mount[-1][2]}, expected tmpfs"

    home_dev = home.stat().st_dev
    for name in (".beads", ".gitconfig", ".beadhive", ".config", ".local/share/beadhive"):
        candidate = home / name
        if candidate.exists():
            assert candidate.stat().st_dev == home_dev, (
                f"~/{name} is on device {candidate.stat().st_dev}, not HOME's tmpfs "
                f"({home_dev}) — the operator's real state is bound back into the fence and is "
                f"in bd's config-resolution walk again"
            )


@inside_fence
def test_external_network_is_unreachable_but_loopback_still_works():
    """Egress off by default; loopback UP, because the integration tests start REAL dolt
    sql-servers and talk to them over 127.0.0.1. A fence that broke those would just be turned
    off."""
    external = socket.socket()
    external.settimeout(5)
    with pytest.raises(OSError):
        external.connect(("1.1.1.1", 443))

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.socket()
    client.settimeout(5)
    client.connect(("127.0.0.1", server.getsockname()[1]))  # must not raise
    client.close()
    server.close()


@inside_fence
def test_the_checkout_is_still_a_usable_git_repository():
    """The fence must not break git in the checkout it fences (bh-gsg8x).

    In a LINKED WORKTREE — which is where every bead is developed — `.git` is a FILE holding a
    `gitdir:` pointer at the main clone, OUTSIDE $REPO. Binding that file bound the pointer and
    not its target, and the tmpfs $HOME hid the target, so inside the fence git answered
    `fatal: not a git repository: (null)`. One integration test failed there and nowhere else
    (test_migrated_furnished_hive_does_not_untrack_the_moved_aside_store) and was quarantined for
    a release as an unexplained fence incompatibility.

    The bind is READ-ONLY, so this is not a loosening:
    `test_the_fence_blocks_the_bh_njdxk_incident_in_a_live_clone` still proves the config of the
    clone under test cannot be rewritten."""
    res = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )

    assert res.returncode == 0, (
        f"git is broken inside the fence: {(res.stderr or '').strip()} — a linked worktree's "
        "gitdir has to be bound read-only too, see scripts/hermetic.sh"
    )
    assert Path((res.stdout or "").strip()).resolve() == REPO


# ---- the fence stays wired (every run) -------------------------------------------------------


def test_the_wrapper_exists_and_is_executable():
    assert WRAPPER.is_file(), f"{WRAPPER} is missing — the gate's fence has no implementation"
    assert os.access(WRAPPER, os.X_OK), f"{WRAPPER} is not executable"


def test_the_wrapper_degrades_loudly_rather_than_silently_when_disabled():
    """BH_HERMETIC=0 is a real escape hatch, but it must announce itself: a fence that is quietly
    absent is worse than no fence, because the gate still reports green."""
    result = subprocess.run(
        [str(WRAPPER), "true"],
        env={**os.environ, "BH_HERMETIC": "0"},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0
    assert "DISABLED" in result.stderr and "BH_HERMETIC=0" in result.stderr


def test_the_wrapper_passes_the_command_through_and_preserves_its_exit_code():
    """Whatever else it does, it must be transparent: the gate reads pytest's exit code through
    it, so a swallowed failure would make the gate green on a red suite."""
    ok = subprocess.run([str(WRAPPER), "true"], capture_output=True, timeout=120)
    bad = subprocess.run([str(WRAPPER), "false"], capture_output=True, timeout=120)

    assert ok.returncode == 0
    assert bad.returncode != 0


def _recipe(name: str) -> str:
    """The body of justfile recipe *name*, up to the next blank line."""
    justfile = (REPO / "justfile").read_text()
    target = justfile.split(f"\n{name}:", 1)
    assert len(target) == 2, f"the `{name}` recipe is gone — re-wire the fence"
    return target[1].split("\n\n", 1)[0]


@pytest.mark.parametrize(
    "recipe",
    ["test set=FAST", "test-integration-land", "demo-local-loop", "demo-live-ingress"],
)
def test_every_gate_phase_runs_through_the_fence(recipe):
    """EVERY phase, asserted against the justfile rather than trusted (bh-yndxi).

    This used to check `test-integration-land` alone, and that was the hole: `check-all` runs
    three test phases and exactly ONE went through the wrapper. The other two — 4,824 of 4,873
    tests — were unfenced, including bh-njdxk's own named culprit
    (tests/test_guard_primary.py:280), which carries no `integration` marker and so was never
    collected by the one fenced recipe. The fence was strong where applied and simply was not
    applied to the phase it was built for.

    Parametrized so a NEW phase added to `check-all` unfenced fails here by name rather than
    silently inheriting the old single-recipe assertion. bh-njdxk's whole lesson is that an
    isolation property nothing enforces decays without anyone noticing."""
    assert "hermetic.sh" in _recipe(recipe), (
        f"`{recipe}` no longer runs through scripts/hermetic.sh — that phase is unfenced again "
        f"(bh-njdxk, bh-yndxi)"
    )


def test_the_demo_is_back_on_the_check_all_line():
    """bh-ik08j dropped `demo-local-loop` from `check-all` because its `~/.beadhive` tripwire
    fired on ambient writes. Fencing the demo gives it a private tmpfs HOME, which removes the
    shared object instead of weakening the assertion — so the phase comes back.

    Pinned because `check-all` losing a phase is the exact shape bh-dfz2 and bh-4kq1b were filed
    about, and it happened again anyway."""
    line = next(
        ln for ln in (REPO / "justfile").read_text().splitlines() if ln.startswith("check-all:")
    )

    assert "demo-local-loop" in line, (
        "`demo-local-loop` is off the check-all line again — that is the ONLY end-to-end proof "
        "that a molecule reaches its terminal state, which is the product claim (bh-ik08j)"
    )
    assert "demo-live-ingress" in line, "the L1-L4 live-ingress proof is off check-all"


# ---- THE incident, reproduced end to end against a live-clone-shaped checkout -----------------


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is Linux-only")
def test_the_fence_blocks_the_bh_njdxk_incident_in_a_live_clone(tmp_path):
    """The single most important test here, and the one whose absence let a broken fence ship.

    bh-njdxk's damage was `git config core.bare true` + `git remote set-url origin <tmpdir>`
    against the clone the suite was RUNNING IN. At push time (scripts/main-push-gate.sh ->
    just check-all) that clone IS the checkout under test, so the fence has to hold when $REPO is
    a live clone — and the first version did not: it bound $REPO read-write and an adversarial
    review landed every mutation of the incident inside the fence.

    That escaped review because the author tested from a LINKED WORKTREE, where .git is a pointer
    file into a path the tmpfs hides, so git answers "fatal: not a git repository". That reads
    like a refusal and is not one — the protection was an accident of layout that evaporates in
    the main clone.

    So this test does not use this checkout at all. It builds a repo shaped like the operator's
    live clone (real origin, core.bare=false, a .beads store), runs the wrapper with THAT as the
    checkout under test, and asserts the mutations are refused with EROFS and the host bytes are
    unchanged. It runs unfenced, spawning its own fence, so it is exercised on every ordinary
    `just test` rather than only when someone remembers to run the suite fenced."""
    clone = tmp_path / "liveclone"
    (clone / "scripts").mkdir(parents=True)
    shutil.copy2(WRAPPER, clone / "scripts" / WRAPPER.name)
    (clone / ".beads").mkdir()
    (clone / ".beads" / "metadata.json").write_text("{}")
    (clone / "seed.txt").write_text("hi\n")

    def git(*argv, cwd=clone):
        return subprocess.run(["git", *argv], cwd=cwd, capture_output=True, text=True, timeout=60)

    git("init", "-q", ".")
    git("config", "user.email", "fence@test.invalid")
    git("config", "user.name", "fence")
    git("remote", "add", "origin", "git@github.com:beadhive/beadhive.git")
    git("add", "-A")
    git("commit", "-qm", "chore: seed")

    config_before = (clone / ".git" / "config").read_bytes()

    # Asserts the PROPERTY (all of .git is read-only), not just the two commands from the report.
    # A review mutated the wrapper to ro-bind only `.git/config`: both incident commands still
    # failed, so a command-shaped test passed — while `.git/hooks/pre-push` stayed plantable,
    # which is arbitrary code execution in the operator's clone at their next push and strictly
    # WORSE than the original incident. Hooks, HEAD and refs are probed for that reason.
    incident = (
        "git config core.bare true; echo rc-config=$?; "
        "git remote set-url origin /tmp/evil; echo rc-remote=$?; "
        "echo tampered > .beads/metadata.json; echo rc-beads=$?; "
        "mkdir -p .git/hooks; printf '#!/bin/sh\\nexit 1\\n' > .git/hooks/pre-push; "
        "echo rc-hook=$?; "
        "echo 'ref: refs/heads/evil' > .git/HEAD; echo rc-head=$?; "
        "echo deadbeef > .git/refs/heads/evil; echo rc-ref=$?; "
        "echo ok > in-checkout-write; echo rc-checkout=$?"
    )
    result = subprocess.run(
        [str(clone / "scripts" / WRAPPER.name), "bash", "-c", incident],
        capture_output=True,
        text=True,
        timeout=180,
    )

    combined = result.stdout + result.stderr
    assert "rc-config=0" not in combined, f"`git config core.bare true` SUCCEEDED:\n{combined}"
    assert "rc-remote=0" not in combined, f"`git remote set-url` SUCCEEDED:\n{combined}"
    assert "rc-beads=0" not in combined, f"the .beads store was writable:\n{combined}"
    assert "rc-hook=0" not in combined, (
        f"a pre-push HOOK could be planted in the operator's clone — arbitrary code at their "
        f"next push, worse than the incident this fence is for:\n{combined}"
    )
    assert "rc-head=0" not in combined, f".git/HEAD was rewritable:\n{combined}"
    assert "rc-ref=0" not in combined, f".git/refs was writable:\n{combined}"
    assert "read-only file system" in combined.lower(), (
        f"the mutations failed for a reason OTHER than the fence — that is the linked-worktree "
        f"false comfort this test exists to rule out:\n{combined}"
    )
    assert "rc-checkout=0" in combined, (
        f"an ordinary write inside the checkout was refused; the fence is too tight for the "
        f"suite to run:\n{combined}"
    )

    assert (clone / ".git" / "config").read_bytes() == config_before, ".git/config was modified"
    assert not (clone / ".git" / "hooks" / "pre-push").exists(), "a hook was planted on the host"
    assert (clone / ".git" / "HEAD").read_text().strip().endswith(("master", "main"))
    assert git("config", "--get", "core.bare").stdout.strip() == "false"
    assert git("remote", "get-url", "origin").stdout.strip().endswith("beadhive/beadhive.git")
    assert (clone / ".beads" / "metadata.json").read_text() == "{}"


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is Linux-only")
def test_the_wrapper_cleans_up_its_scratch_tree(tmp_path):
    """`exec bwrap` replaced the shell, so the EXIT trap never fired and $SCRATCH — which is
    TMPDIR inside the fence, i.e. pytest's whole tree of dolt stores and hive clones — was left
    on the host: 60 directories and 2.2 GB, one per invocation, measured. That is bh-njdxk's
    factor 3 (leaked state accumulating across runs) re-created by the script claiming to remove
    it, so the cleanup is pinned rather than trusted.

    Runs the probes under a PRIVATE TMPDIR. Globbing the shared one made this flaky under xdist
    (2 failures in ~11 runs at -n 4): a sibling worker's IN-FLIGHT fence is a `bh-hermetic-*`
    directory too, and it read as a leak. A flaky gate teaches `--no-verify`, which is bh-njdxk's
    own stated failure mode — so a test guarding that gate must not be the thing that fires
    spuriously."""
    private_tmp = tmp_path / "scratch-probe"
    private_tmp.mkdir()
    env = {**os.environ, "TMPDIR": str(private_tmp)}

    for argv in (["true"], ["false"], ["sh", "-c", "exit 42"]):
        subprocess.run([str(WRAPPER), *argv], capture_output=True, timeout=120, env=env)

    leaked = sorted(private_tmp.glob("bh-hermetic-*"))
    assert not leaked, f"the wrapper leaked scratch directories onto the host: {leaked}"


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is Linux-only")
def test_an_interrupted_run_never_exits_zero(tmp_path):
    """A fenced gate killed part-way must not report success. With only an EXIT trap, bash
    exiting on SIGINT took its status from the trap's last command and the wrapper exited 0 —
    SIGTERM and SIGHUP were already correct, which is what made it easy to miss."""
    private_tmp = tmp_path / "signal-probe"
    private_tmp.mkdir()
    proc = subprocess.Popen(
        [str(WRAPPER), "sleep", "30"],
        env={**os.environ, "TMPDIR": str(private_tmp)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    proc.send_signal(signal.SIGINT)
    rc = proc.wait(timeout=60)

    assert rc != 0, "an interrupted fenced run exited 0 — a gate can now go green on a SIGINT"
    assert sorted(private_tmp.glob("bh-hermetic-*")) == [], "the signal path skipped cleanup"


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is Linux-only")
def test_the_boundary_properties_are_checked_by_an_ordinary_unfenced_run(tmp_path):
    """The boundary half (@inside_fence) is executed by NO gate: this file carries no `integration`
    marker, so the only fenced invocation in the repo — `just test-integration-land`, which selects
    `-m integration` — never collects it, and `just check`'s unfenced run skips it. A review found
    that the HOME and network assertions are real, correct, and never run.

    So spawn the fence here and assert the two properties from inside it, the same way the incident
    test does. Now an ordinary `just test` catches a fence that stopped isolating HOME or stopped
    blocking egress, instead of only catching it when a human remembers to run the suite fenced.

    Asserts the ROOT MOUNT is `ro` too, which closes the one mutation the in-fence tests still
    missed: swapping `--ro-bind / /` for `--bind / /` left /home/linuxbrew (owned by this uid,
    outside $HOME, so not covered by the tmpfs) writable, and every existing assertion passed."""
    probe = (
        "import socket, sys\n"
        "from pathlib import Path\n"
        "home = Path.home()\n"
        "mounts = Path('/proc/mounts').read_text().splitlines()\n"
        "fields = [m.split() for m in mounts if len(m.split()) > 3]\n"
        "home_fs = [f[2] for f in fields if f[1] == str(home)]\n"
        "root_opts = [f[3] for f in fields if f[1] == '/']\n"
        "assert home_fs and home_fs[-1] == 'tmpfs', f'HOME is {home_fs}, not a tmpfs'\n"
        "assert root_opts and root_opts[-1].split(',')[0] == 'ro', f'/ is {root_opts}, not ro'\n"
        "s = socket.socket(); s.settimeout(5)\n"
        "try:\n"
        "    s.connect(('1.1.1.1', 443)); sys.exit('egress reachable inside the fence')\n"
        "except OSError:\n"
        "    pass\n"
        "print('BOUNDARY-OK')\n"
    )
    result = subprocess.run(
        [str(WRAPPER), "python3", "-c", probe],
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "TMPDIR": str(tmp_path)},
    )

    assert "BOUNDARY-OK" in result.stdout, (
        f"the fence's boundary properties do not hold:\n{result.stdout}\n{result.stderr}"
    )
