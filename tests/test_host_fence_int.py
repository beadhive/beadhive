"""Integration: establish bd's real transport and exercise the viable epoch-fence boundary.

Current bd shells out to real Git from exactly the repo ``transport_lookup`` locates, but
forces ``core.hooksPath=/dev/null``. A marker hook plus Git trace2 prove both facts in embedded
and shared-server modes. The same test then proves the managed CAS preflight rejects a stale
host before data, and its raw-bd adversarial control proves why doctor calls that bypass
unenforceable. This is mechanism evidence, not an argv-shaped mock.

Fully local, and deliberately so: `file://` bare repos as remotes, and an isolated
`BEADS_SHARED_SERVER_DIR` + non-default port, so the operator's real shared server is never
touched and gains no scratch database (`bh-lxpf`).

Marked `integration` (drives a real `bd init` + `bd dolt push`) + self-skips without `bd` on
PATH, per this repo's marker convention (`justfile`: `just test` excludes "integration").
"""

from __future__ import annotations

import json
import os
import shutil
import uuid

import pytest

from beadhive import engine, gitref, guard, host_fence
from beadhive.run import run
from harness.world import free_port, reap_dolt_server

pytestmark = [
    pytest.mark.integration,
    # `dolt_server`: the `shared-server` parametrization stands up a REAL server, so it holds one
    # of the run-wide slots `conftest._bound_concurrent_dolt_servers` hands out (bh-wa3ch).
    # File-level, so the `embedded` cases pay only the marker lookup.
    pytest.mark.dolt_server,
    pytest.mark.skipif(shutil.which("bd") is None, reason="bd not installed"),
]


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


def _remote_data(hive):
    out = _git(["ls-remote", "origin", host_fence.DATA_REF], hive).stdout.strip()
    return out.split()[0] if out else ""


class _LiveLease:
    epoch = 1

    @staticmethod
    def held_by(host_id):
        return host_id == "host-a"


def _trace_transport_push(trace_path, transport):
    events = [json.loads(line) for line in trace_path.read_text().splitlines() if line.strip()]
    pushes = [
        event
        for event in events
        if event.get("event") == "start"
        and len(event.get("argv", [])) > 1
        and event["argv"][1] == "push"
        and any(str(arg).endswith(":" + host_fence.DATA_REF) for arg in event["argv"])
    ]
    assert pushes, "bd did not expose the refs/dolt/data transport in Git trace2"
    push = pushes[-1]
    assert os.path.basename(push["argv"][0]) == "git"
    params = {
        event.get("param"): event.get("value")
        for event in events
        if event.get("event") == "def_param" and event.get("sid") == push["sid"]
    }
    assert os.path.realpath(params["GIT_DIR"]) == os.path.realpath(transport)
    assert "core.hooksPath=/dev/null" in params["GIT_CONFIG_PARAMETERS"]


@pytest.fixture
def scratch_server(tmp_path):
    """An isolated shared-server data dir, torn down after the test — a bd-spawned server left
    running would outlive the suite and hold the scratch tmp dir open."""
    root = tmp_path / "scratch-server"
    yield root
    # This fixture's pidfile reap was the pattern that WORKED while two sibling fixtures leaked
    # a server per run; it now lives in `harness.world` so there is one implementation to keep
    # right (bh-cbou). Same behaviour, plus a SIGKILL after the grace period.
    reap_dolt_server(root)


@pytest.mark.parametrize("shared_server", [False, True], ids=["embedded", "shared-server"])
def test_the_located_transport_repo_is_the_one_that_pushes(
    tmp_path, monkeypatch, scratch_server, shared_server
):
    """Pin the shipped bd mechanism, then prove managed and raw adversarial outcomes."""
    if shared_server:
        # An EPHEMERAL port, never a literal (this was 3399, and a stray dolt server another
        # session left on 3399 made this case fail permanently on that machine — the failure
        # says nothing about the fence). Also keeps it off bd's 3308 default, so the operator's
        # own fleet server is never the one under test.
        monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(scratch_server))
        monkeypatch.setenv("BEADS_DOLT_SERVER_PORT", str(free_port()))
        monkeypatch.setenv("BEADS_DOLT_SHARED_SERVER", "1")

    # The shared server inherits its environment only when it starts, so tracing must be
    # armed before `bd init` rather than immediately before the measured second push.
    trace = tmp_path / "git-trace.json"
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    monkeypatch.setenv("GIT_TRACE2_ENV_VARS", "GIT_DIR,GIT_CONFIG_PARAMETERS")
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

    # Real Git did push from this exact repo, but bd disabled every hook for the call. Both
    # marker absences are important: neither location is an enforcement point.
    _trace_transport_push(trace, lookup.repos[0])
    assert not marker.exists(), "bd unexpectedly stopped suppressing transport Git hooks"
    assert not hive_marker.exists(), "the hive checkout is NOT where a data push fires from"

    # Install the fence and exercise the actual supported boundary around a real bd push.
    monkeypatch.setattr(guard, "primary_state", lambda **_kw: (prefix, "host-a", _LiveLease()))
    initial = host_fence.install_fence(
        "origin",
        host_fence.EpochFence(epoch=1, host_id="host-a"),
        expected=gitref.ABSENT,
        cwd=hive,
    )
    assert _bd(["create", "--title", "three", "-t", "task", "-p", "2"], hive).returncode == 0
    managed = engine.BdEngine().push_state(hive, message="managed epoch-fenced push")
    assert managed.returncode == 0, managed.stderr
    reserved, fence = host_fence.read_fence("origin", cwd=hive)
    assert reserved != initial
    assert fence == host_fence.EpochFence(epoch=1, host_id="host-a", seq=1)
    assert not marker.exists(), "managed safety must not rely on bd's suppressed hook"

    # Another host takes over. The stale managed path loses before bd and leaves data alone.
    host_fence.install_fence(
        "origin",
        host_fence.EpochFence(epoch=2, host_id="host-b"),
        expected=reserved,
        cwd=hive,
    )
    assert _bd(["create", "--title", "four", "-t", "task", "-p", "2"], hive).returncode == 0
    before = _remote_data(hive)
    refused = engine.BdEngine().push_state(hive, message="stale managed push")
    assert refused.returncode != 0
    assert "refused before data transfer" in refused.stderr
    assert _remote_data(hive) == before

    # Adversarial control: OS-level raw bd is not interceptable, ignores refs/bh/epoch, and
    # publishes the same stale state. Doctor must therefore expose this exact posture.
    raw = _bd(["dolt", "push"], hive)
    assert raw.returncode == 0, raw.stderr
    assert _remote_data(hive) != before
