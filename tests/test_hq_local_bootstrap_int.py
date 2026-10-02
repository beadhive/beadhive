"""Actual console HQ init and provision clone step with Git-ref Dolt transport."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from beadhive import config, hq
from harness.beads import skip_if_no_bd
from harness.world import free_port, git, reap_dolt_server


@pytest.mark.parametrize(
    "remote",
    ["ext::sh -c id", "https://example.invalid/hq", "file://remote/hq", "../hq", "owner/repo;id"],
)
def test_remote_rejects_uncontrolled_transport(remote):
    with pytest.raises(ValueError):
        hq._remote_urls(remote)


def test_local_urls_preserve_forge_and_git_ref_dolt(tmp_path):
    remote = tmp_path / "hq.git"
    assert hq._remote_urls(str(remote)) == (remote.as_uri(), "git+" + remote.as_uri())
    assert hq._remote_urls(remote.as_uri()) == (remote.as_uri(), "git+" + remote.as_uri())
    assert hq._remote_urls("owner/repo") == (
        "git@github.com:owner/repo.git",
        "git+ssh://git@github.com/owner/repo.git",
    )


@pytest.mark.integration
@pytest.mark.dolt_server
@skip_if_no_bd
def test_actual_init_and_fresh_provision_clone_with_dolt(world):
    remote = world.remotes / "hq.git"
    git("init", "--bare", "-q", "-b", "main", str(remote), cwd=world.remotes)
    world.gitconfig.write_text(
        world.gitconfig.read_text()
        + "\n[user]\n\tname = Local HQ Operator\n\temail = hq@example.invalid\n"
    )
    assert config.set_value("hq.remote", remote.as_uri())["ok"]
    source = world.home / "hq"
    console = Path(sys.executable).parent / "bh"
    environment = dict(os.environ, BH_HQ=str(source), BH_SKIP_SETUP_CHECK="1")
    initialized = subprocess.run(
        [str(console), "hq", "init", "--auto"],
        env=environment,
        cwd=world.tmp,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    assert git("rev-parse", "refs/dolt/data", cwd=remote)
    assert git("rev-parse", "main", cwd=remote)
    fresh_home = world.tmp / "fresh-frame"
    fresh_home.mkdir()
    fresh_config = fresh_home / "config.yaml"
    fresh_config.write_text(
        "schema_version: 1\nhq:\n  mode: git\n  remote: " + remote.as_uri() + "\n"
    )
    fresh = fresh_home / "hq"
    fresh_server = world.tmp / "fresh-shared-server"
    environment.update(
        BH_HOME=str(fresh_home),
        BH_CONFIG=str(fresh_config),
        BH_HQ=str(fresh),
        BEADS_SHARED_SERVER_DIR=str(fresh_server),
        BEADS_DOLT_SERVER_PORT=str(free_port()),
    )
    # Execute the actual provision clone step with real loaders/engine/identity/registry.
    # The complete machine setup pipeline is exercised by the separate NixOS Frame fixture.
    try:
        cloned = subprocess.run(
            [str(console), "hq", "clone", "--auto"],
            env=environment,
            cwd=world.tmp,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert cloned.returncode == 0, cloned.stdout + cloned.stderr
        assert (fresh / ".beads/metadata.json").is_file()
        verified = subprocess.run(
            ["bd", "-C", str(fresh), "status", "--json"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert verified.returncode == 0, verified.stdout + verified.stderr
        assert '"summary"' in verified.stdout
    finally:
        reap_dolt_server(fresh_server)
