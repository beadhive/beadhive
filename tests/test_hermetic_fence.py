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
import socket
import subprocess
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


def test_the_land_gate_runs_the_integration_suite_through_the_fence():
    """The wiring, asserted against the justfile rather than trusted. bh-njdxk's whole lesson is
    that an isolation property nothing enforces decays without anyone noticing."""
    justfile = (REPO / "justfile").read_text()
    target = justfile.split("test-integration-land:", 1)
    assert len(target) == 2, "the `test-integration-land` recipe is gone — re-wire the fence"

    recipe = target[1].split("\n\n", 1)[0]
    assert "hermetic.sh" in recipe, (
        "`test-integration-land` no longer runs through scripts/hermetic.sh — the integration "
        "suite is unfenced again (bh-njdxk)"
    )


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

    incident = (
        "git config core.bare true; echo rc-config=$?; "
        "git remote set-url origin /tmp/evil; echo rc-remote=$?; "
        "echo tampered > .beads/metadata.json; echo rc-beads=$?; "
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
    assert "read-only file system" in combined.lower(), (
        f"the mutations failed for a reason OTHER than the fence — that is the linked-worktree "
        f"false comfort this test exists to rule out:\n{combined}"
    )
    assert "rc-checkout=0" in combined, (
        f"an ordinary write inside the checkout was refused; the fence is too tight for the "
        f"suite to run:\n{combined}"
    )

    assert (clone / ".git" / "config").read_bytes() == config_before, ".git/config was modified"
    assert git("config", "--get", "core.bare").stdout.strip() == "false"
    assert git("remote", "get-url", "origin").stdout.strip().endswith("beadhive/beadhive.git")
    assert (clone / ".beads" / "metadata.json").read_text() == "{}"


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is Linux-only")
def test_the_wrapper_cleans_up_its_scratch_tree(tmp_path):
    """`exec bwrap` replaced the shell, so the EXIT trap never fired and $SCRATCH — which is
    TMPDIR inside the fence, i.e. pytest's whole tree of dolt stores and hive clones — was left
    on the host: 60 directories and 2.2 GB, one per invocation, measured. That is bh-njdxk's
    factor 3 (leaked state accumulating across runs) re-created by the script claiming to remove
    it, so the cleanup is pinned rather than trusted."""
    scratch_root = Path(os.environ.get("TMPDIR", "/tmp"))
    before = set(scratch_root.glob("bh-hermetic-*"))

    for _ in range(3):
        subprocess.run([str(WRAPPER), "true"], capture_output=True, timeout=120)

    leaked = set(scratch_root.glob("bh-hermetic-*")) - before
    assert not leaked, f"the wrapper leaked scratch directories onto the host: {sorted(leaked)}"
