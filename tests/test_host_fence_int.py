"""Integration: does the repo `prepush` installs the fence hook into actually perform the push?

`bh-areg.6`'s acceptance asks for the hook to "install and fire in the target mode, from
whichever repo actually performs the push" — a claim no unit test can settle, because the
answer is bd's to give. `bh-ukit.2` measured it by hand
(`docs/spikes/bh-ukit.2-fence-under-a-dolt-server.md`); this is that measurement pinned as a
test, so a future bd release that moves the transport cannot quietly disarm the fence again.

The instrument is a MARKER hook (writes a file, exits 0 — observes, never blocks) placed in
exactly the location `host_fence.transport_lookup` reports, rather than bh's own shim: what is
under test is the LOCATION, not the shim's own decision logic (that is `test_prepush.py`'s job).

Fully local, and deliberately so: `file://` bare repos as remotes, and an isolated
`BEADS_SHARED_SERVER_DIR` + non-default port, so the operator's real shared server is never
touched and gains no scratch database (`bh-lxpf`).

Marked `integration` (drives a real `bd init` + `bd dolt push`) + self-skips without `bd` on
PATH, per this repo's marker convention (`justfile`: `just test` excludes "integration").
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import uuid

import pytest

from beadhive import host_fence
from beadhive.run import run

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("bd") is None, reason="bd not installed"),
]

SCRATCH_SERVER_PORT = "3399"  # never bd's 3308 default: the operator's own server keeps that


def _git(args, cwd):
    return run(["git", *args], cwd=str(cwd), check=False, capture=True)


def _marker_hook(repo, marker):
    hooks = repo / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 0\n')
    hook.chmod(0o755)


def _hive_with_remote(tmp_path, name):
    remote = tmp_path / f"{name}.git"
    _git(["init", "--bare", "-q", str(remote)], tmp_path)
    hive = tmp_path / name
    hive.mkdir()
    _git(["init", "-q"], hive)
    _git(["config", "user.email", "t@example.invalid"], hive)
    _git(["config", "user.name", "t"], hive)
    _git(["remote", "add", "origin", str(remote)], hive)
    _git(["commit", "-q", "--allow-empty", "-m", "init"], hive)
    _git(["push", "-q", "origin", "HEAD:main"], hive)
    return hive


def _bd(args, cwd, env=None):
    return run(
        ["bd", *args], cwd=str(cwd), check=False, capture=True, timeout=180, env=env or os.environ
    )


@pytest.fixture
def scratch_server(tmp_path):
    """An isolated shared-server data dir, torn down after the test — a bd-spawned server left
    running would outlive the suite and hold the scratch tmp dir open."""
    root = tmp_path / "scratch-server"
    yield root
    pid_file = root / "dolt-server.pid"
    if pid_file.is_file():
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)


@pytest.mark.parametrize("shared_server", [False, True], ids=["embedded", "shared-server"])
def test_the_located_transport_repo_is_the_one_that_pushes(
    tmp_path, monkeypatch, scratch_server, shared_server
):
    """The property the fence depends on, in both modes: the repo `transport_lookup` points at
    is where bd's own `git push` originates — so a hook installed there sees the data push, and
    one installed anywhere else does not."""
    if shared_server:
        monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(scratch_server))
        monkeypatch.setenv("BEADS_DOLT_SERVER_PORT", SCRATCH_SERVER_PORT)
        monkeypatch.setenv("BEADS_DOLT_SHARED_SERVER", "1")

    hive = _hive_with_remote(tmp_path, "hive")
    prefix = f"fx{uuid.uuid4().hex[:6]}"
    init = ["init", "--prefix", prefix, "--non-interactive"]
    if shared_server:
        init.insert(1, "--shared-server")
    assert _bd(init, hive).returncode == 0
    assert _bd(["create", "--title", "one", "-t", "task", "-p", "2"], hive).returncode == 0
    # bd stages the transport repo lazily, on the FIRST push — before this there is nothing
    # to hook, which is `transport_lookup`'s NOT_FOUND state.
    assert host_fence.transport_lookup(hive).state == host_fence.NOT_FOUND
    assert _bd(["dolt", "push"], hive).returncode == 0

    lookup = host_fence.transport_lookup(hive)
    assert lookup.state == host_fence.FOUND, lookup.detail
    assert len(lookup.repos) == 1

    marker = tmp_path / "fired"
    _marker_hook(lookup.repos[0], marker)
    hive_marker = tmp_path / "fired-in-the-hive-checkout"
    hive_hooks = (hive / _git(["rev-parse", "--git-path", "hooks"], hive).stdout.strip()).resolve()
    hive_hooks.mkdir(parents=True, exist_ok=True)
    (hive_hooks / "pre-push").write_text(f'#!/bin/sh\ntouch "{hive_marker}"\nexit 0\n')
    (hive_hooks / "pre-push").chmod(0o755)

    assert _bd(["create", "--title", "two", "-t", "task", "-p", "2"], hive).returncode == 0
    assert _bd(["dolt", "push"], hive).returncode == 0

    assert marker.exists(), "the located transport repo did not perform the push"
    assert not hive_marker.exists(), "the hive checkout is NOT where a data push fires from"
