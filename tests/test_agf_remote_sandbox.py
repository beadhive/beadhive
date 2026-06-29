"""AGF harness: the remote-sandbox modality — the executable spec for `ws work`'s
not-yet-built remote path. Exercises real BEADS-SYNC state transfer over a file:// dolt
remote, fresh-config isolation, and injected-key signing.
"""

from __future__ import annotations

import pytest

from harness import graph, history
from harness.beads import skip_if_no_bd, status
from harness.modalities import RemoteSandboxModality, run_flow
from harness.render import Timeline, diff_report
from harness.rig import make_rig
from harness.world import git

pytestmark = [pytest.mark.integration, skip_if_no_bd]


def test_remote_sandbox_state_transfer_and_signing(world):
    mod = RemoteSandboxModality(world)
    rig = make_rig(world, work=mod.work_block(), with_remotes=True)
    ids = graph.independent(rig, 2)

    order = run_flow(rig, ids, mod, label="remote-sandbox")
    assert set(order) == set(ids)

    # STATE TRANSFER: the sandbox's own embedded bd (bootstrapped from the file:// dolt remote)
    # saw the coordinator's pushed assignment — no shared in-process state.
    sandbox = mod.last_sandbox
    assert status(sandbox, order[-1]).get("assignee") == "crew/remote"

    # ISOLATION: the sandbox started from an empty global config — its only identity is the
    # injected agent one (no human leak).
    assert (
        git("-C", str(sandbox), "config", "user.email", check=False).stdout.strip()
        == "remote@fixture"
    )

    # the bead branch was pushed to the shared (bare) git remote
    remote_branches = set(history.remote_branches(rig.git_remote))
    for bead in ids:
        assert f"wt/bead/{bead}" in remote_branches

    # SIGNING + AUTHOR + structure: the integration history matches the fixture's expectation
    # (injected-key signatures, authored by the agent, one --no-ff merge per bead).
    expected = Timeline.from_expected("remote-sandbox", world, mod, order)
    actual = Timeline.from_actual("remote-sandbox", rig)
    assert diff_report(expected, actual), "history diverged — run `just render-int diff` to see"
