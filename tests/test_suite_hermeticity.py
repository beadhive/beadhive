"""Guards on conftest's autouse isolation fixtures (bh-myp0).

Those fixtures are the reason a test that merely imports `beadhive.config` or drives the CLI
through `CliRunner` cannot read or mutate real state on the machine running the suite. Nothing
enforced that, so `$GIT_WORKSPACE` went unsandboxed for a long time without anyone noticing —
the tell was cost, not a failure: `metadata.refresh` recomputes "the full on-disk fleet" from
`identity.workspace_root()` WITHOUT consulting config, so one `bh doctor` invocation ran 84
`safety.scan`s over 125 unrelated repos and took 26s.

These tests exist so deleting a sandbox fixture fails loudly instead of quietly re-pointing the
suite at somebody's home directory.
"""

from __future__ import annotations

import os
from pathlib import Path

from beadhive import config, identity


def _is_temporary(path: Path) -> bool:
    """Whether `path` is under pytest's tmp root rather than a real home/workspace. Compares
    resolved paths so a symlinked /tmp (macOS: /tmp -> /private/tmp) still matches."""
    real_home = Path.home().resolve()
    resolved = Path(path).resolve()
    return resolved != real_home and real_home not in resolved.parents


def test_git_workspace_is_sandboxed_away_from_the_real_workspace():
    """The bh-myp0 hole. `identity.workspace_root()` reads $GIT_WORKSPACE and ignores config,
    so an unsandboxed value sends every fleet-scanning code path into the operator's own repos."""
    root = Path(identity.workspace_root())

    assert os.environ.get("GIT_WORKSPACE"), "$GIT_WORKSPACE must be set, not left to the default"
    assert _is_temporary(root), f"workspace_root() resolves under the real home: {root}"


def test_bh_home_is_sandboxed_away_from_the_real_home():
    """The sibling fixture, guarded the same way."""
    home = Path(config.home())

    assert os.environ.get("BH_HOME"), "$BH_HOME must be set"
    assert _is_temporary(home), f"config.home() resolves under the real home: {home}"


def test_the_sandboxed_workspace_holds_no_repos():
    """A scan of the sandboxed root must find nothing — the property that makes fleet-walking
    code paths cheap AND deterministic. If this ever finds repos, some test is seeding the
    shared root instead of its own tmp dir, and runtime becomes order-dependent."""
    root = Path(identity.workspace_root())

    assert not list(root.glob("*/*/*/.git"))
