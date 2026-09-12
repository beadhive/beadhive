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
import subprocess
from pathlib import Path

from beadhive import config, identity
from beadhive.validation_admission import slot_root
from harness.world import _is_git_repository_env

# Captured while pytest imports this module, before the autouse fixture replaces HOME for an
# individual test.  `Path.home()` inside a test is deliberately the isolated home.
_OPERATOR_HOME = Path.home()


def _is_sandboxed(path: Path, basetemp: Path) -> bool:
    """Whether `path` is under pytest's own tmp root — positive proof it came from a
    `tmp_path`/`tmp_path_factory` fixture rather than the operator's real home or workspace.
    Resolves both sides so a symlinked tmp root (macOS: /tmp -> /private/tmp) still matches.

    ASKS PYTEST WHERE ITS TMP ROOT IS, rather than inferring it from $HOME (bh-u5i2r). This
    used to assert "not under the real home", which silently assumed the tmp root never is.
    On a host with TMPDIR=~/tmp — a completely ordinary setup, and this factory host's — every
    correctly sandboxed path is under the real home, so both guards failed while the fixtures
    they guard were working perfectly. A hermeticity check that fires on a hermetic suite
    teaches people to distrust it.
    """
    resolved = Path(path).resolve()
    base = Path(basetemp).resolve()
    return resolved == base or base in resolved.parents


def test_git_workspace_is_sandboxed_away_from_the_real_workspace(tmp_path_factory):
    """The bh-myp0 hole. `identity.workspace_root()` reads $GIT_WORKSPACE and ignores config,
    so an unsandboxed value sends every fleet-scanning code path into the operator's own repos."""
    root = Path(identity.workspace_root())

    assert os.environ.get("GIT_WORKSPACE"), "$GIT_WORKSPACE must be set, not left to the default"
    assert _is_sandboxed(root, tmp_path_factory.getbasetemp()), (
        f"workspace_root() resolves outside pytest's tmp root: {root}"
    )


def test_bh_home_is_sandboxed_away_from_the_real_home(tmp_path_factory):
    """The sibling fixture, guarded the same way."""
    home = Path(config.home())

    assert os.environ.get("BH_HOME"), "$BH_HOME must be set"
    assert _is_sandboxed(home, tmp_path_factory.getbasetemp()), (
        f"config.home() resolves outside pytest's tmp root: {home}"
    )


def test_validation_host_is_sandboxed_away_from_the_real_host(tmp_path_factory):
    """Nested real-CLI tests own a fixture host, never the gate runner's admission semaphore."""
    root = slot_root()

    assert os.environ.get("BH_VALIDATION_SLOT_ROOT"), "$BH_VALIDATION_SLOT_ROOT must be set"
    assert _is_sandboxed(root, tmp_path_factory.getbasetemp()), (
        f"validation slot root resolves outside pytest's tmp root: {root}"
    )
    assert "BH_VALIDATION_SLOTS" not in os.environ, (
        "ambient host capacity must not override a test's fixture/configured capacity"
    )


def test_the_sandbox_check_still_rejects_the_real_home(tmp_path_factory):
    """The guard on the guard. `_is_sandboxed` is only worth asserting if it can say no — a
    containment check that accepts everything is the same nothing as the check it replaced
    accepting everything on a /tmp host. The operator's real home and workspace are the two
    paths these tests exist to catch, so name them."""
    base = tmp_path_factory.getbasetemp()

    assert not _is_sandboxed(_OPERATOR_HOME, base)
    assert not _is_sandboxed(_OPERATOR_HOME / "workspace", base)


def test_the_shared_server_target_is_sandboxed_away_from_the_real_one(tmp_path_factory):
    """The third sandbox fixture, guarded like its siblings (bh-u5i2r). `bd` resolves
    `BEADS_SHARED_SERVER_DIR`/`BEADS_DOLT_SERVER_PORT` from the ambient environment, defaulting
    to `~/.beads/shared-server` on the fixed port 3308 — the host's REAL fleet server, which
    v0.8.0 made the default and which is running on any machine where bh works. Unset, a test
    that runs a real `bd init --shared-server` lands scratch databases on it, or (when it is
    already up) dies on "port 3308 is busy" and reads as a broken machine. Nothing asserted
    this; `harness.world.World` scrubbed both vars for four months and nobody could see it."""
    server_dir = os.environ.get("BEADS_SHARED_SERVER_DIR")
    port = os.environ.get("BEADS_DOLT_SERVER_PORT")

    assert server_dir, "$BEADS_SHARED_SERVER_DIR must be set, not left to bd's ~/.beads default"
    assert _is_sandboxed(Path(server_dir), tmp_path_factory.getbasetemp()), (
        f"the shared-server dir resolves outside pytest's tmp root: {server_dir}"
    )
    assert port and port.isdigit(), "$BEADS_DOLT_SERVER_PORT must be set to an ephemeral port"
    assert int(port) != 3308, "3308 is bd's own shared-server default — never bind it under test"


def test_the_sandboxed_workspace_holds_no_repos():
    """A scan of the sandboxed root must find nothing — the property that makes fleet-walking
    code paths cheap AND deterministic. If this ever finds repos, some test is seeding the
    shared root instead of its own tmp dir, and runtime becomes order-dependent."""
    root = Path(identity.workspace_root())

    assert not list(root.glob("*/*/*/.git"))


def test_every_git_reported_repo_local_environment_variable_is_scrubbed():
    """Git owns this list; a new release adding a local redirect must fail the harness gate."""
    result = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"],
        check=True,
        capture_output=True,
        text=True,
    )
    local_vars = {line.strip() for line in result.stdout.splitlines() if line.strip()}

    assert local_vars, "installed Git returned no repository-local environment variables"
    assert not {key for key in local_vars if not _is_git_repository_env(key)}


def test_only_isolated_file_based_git_config_channels_are_preserved():
    """Keep fixture-owned config files; reject every ambient command/repository override."""
    assert not _is_git_repository_env("GIT_CONFIG_GLOBAL")
    assert not _is_git_repository_env("GIT_CONFIG_SYSTEM")
    for key in (
        "GIT_CONFIG",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
    ):
        assert _is_git_repository_env(key), key
