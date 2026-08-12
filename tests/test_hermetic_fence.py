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
def test_the_host_filesystem_outside_the_repo_is_read_only(tmp_path_factory):
    """The measured incident: the suite set `origin` to a pytest temp dir and flipped
    `core.bare = true` on the operator's LIVE clone, and only the pre-push hook stopped 65
    commits going to that temp dir. Under the fence a write outside the checkout under test
    fails at the kernel, so no test can reach another repo however it resolves its way there."""
    for target in (Path("/usr/lib/bh-fence-probe"), Path.home().parent / "bh-fence-probe"):
        with pytest.raises(OSError) as excinfo:
            target.write_text("this must never land on the host")
        assert excinfo.value.errno in (errno_ro := (30, 13, 2)), (
            f"writing {target} failed with an unexpected errno {excinfo.value.errno} "
            f"(expected read-only/permission/absent: {errno_ro})"
        )


@inside_fence
def test_the_operators_home_state_is_not_resolvable():
    """`bd` resolves configuration by walking UP from cwd and from BEADS_* env, which is why the
    same code passed outside GIT_WORKSPACE and failed inside a live hive. A tmpfs HOME takes
    ~/.beads and ~/.gitconfig out of that walk entirely — the mechanism, not a symptom.

    Asserts the MECHANISM rather than enumerating what is absent under HOME, and that distinction
    is not pedantry: ~/.beads, ~/.gitconfig and ~/.beadhive all get created inside the fence
    during an ordinary run (tests provision hives; binding a bh-managed checkout recreates its
    ancestor chain), so an absence check passes or fails on test ORDER while the fence is equally
    intact either way. A fresh tmpfs is both the stronger statement and an order-independent one:
    nothing under it came from the operator, and nothing written to it survives the run.
    """
    home = Path.home()
    mounts = Path("/proc/mounts").read_text().splitlines()
    home_mount = [m.split() for m in mounts if len(m.split()) > 2 and m.split()[1] == str(home)]

    assert home_mount, f"{home} is not its own mount — HOME is the host's, not a fenced tmpfs"
    assert home_mount[-1][2] == "tmpfs", f"{home} is a {home_mount[-1][2]}, expected tmpfs"


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
