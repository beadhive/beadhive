"""Opt-in proof of the Herdr ``workspace.binding`` against a real, isolated Herdr server.

Skipped unless ``BH_TEST_HERDR_REAL_BINDING=1`` (and marked ``integration``, so the default
``check-native`` selection never collects it). It starts a private headless server under a
throwaway ``XDG_CONFIG_HOME`` — never the operator's session or config — proves bind-after-create
(E28) and bind-after-attach (E29), idempotent re-bind (E34), the ``worktree list`` round trip
(E35) and release-before-remove (E30), then stops the server and asserts nothing it started is
left running.

Run it with::

    BH_TEST_HERDR_REAL_BINDING=1 uv run pytest -m integration \
        tests/test_herdr_workspace_binding_real.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from beadhive.integrations.herdr.workspace_binding import HerdrWorkspaceBinding
from beadhive.modules.worktrees import (
    NativeGitWorktreeManager,
    WorktreeHandle,
    WorktreeLifecycleService,
    WorktreeSpec,
)

pytestmark = pytest.mark.integration


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _run_git(args, **_kwargs):
    return subprocess.run(args, check=False, capture_output=True, text=True)


def _session_processes(session: str) -> list[str]:
    found = subprocess.run(
        ["pgrep", "-af", f"herdr --session {session}"], capture_output=True, text=True
    )
    return [line for line in found.stdout.splitlines() if "pgrep" not in line]


@pytest.fixture
def herdr_server():
    if os.environ.get("BH_TEST_HERDR_REAL_BINDING") != "1":
        pytest.skip("set BH_TEST_HERDR_REAL_BINDING=1 to prove the binding against real Herdr")
    if shutil.which("herdr") is None:
        pytest.skip("herdr is not installed")
    # Herdr's socket lives under XDG_CONFIG_HOME; keep the path short (sun_path limit).
    root = Path(tempfile.mkdtemp(prefix="bhwb", dir="/tmp"))
    session = f"bhwb{os.getpid()}"
    env = {**os.environ, "XDG_CONFIG_HOME": str(root / "x")}
    try:
        server = subprocess.Popen(
            ["herdr", "--session", session, "server"],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise

    def herdr(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["herdr", "--session", session, *args],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    try:
        deadline = time.monotonic() + 20
        while herdr("status").returncode != 0:
            if time.monotonic() > deadline or server.poll() is not None:
                pytest.fail("isolated herdr server did not start")
            time.sleep(0.2)
        yield root, herdr, session
    finally:
        herdr("server", "stop")
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(server.pid, 9)
            server.wait(timeout=5)
        shutil.rmtree(root, ignore_errors=True)
        deadline = time.monotonic() + 10
        while _session_processes(session) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert _session_processes(session) == [], "the isolated herdr server left processes"


def test_real_herdr_binds_after_create_and_attach_then_releases_before_remove(herdr_server):
    root, herdr, _session = herdr_server
    main = root / "repo"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
        cwd=main,
    )
    _git("branch", "wt/bead/issue/a-2", cwd=main)

    binding = HerdrWorkspaceBinding(herdr, label="bh:proof/binding/real")
    service = WorktreeLifecycleService(
        manager=NativeGitWorktreeManager(_run_git), bindings=(binding,)
    )
    assert service.binding("herdr") is binding  # native binds nothing: the binding composes

    created = service.create(WorktreeSpec(main, "wt/bead/issue/a-1", root / "wts" / "a-1", "main"))
    attached = service.attach(WorktreeSpec(main, "wt/bead/issue/a-2", root / "wts" / "a-2"))

    # E28 / E29: bind after create and after attach, each to its own workspace.
    bound_created = service.bind(created)
    bound_attached = service.bind(attached)
    assert bound_created.gaps == bound_attached.gaps == ()
    created_id = bound_created.handle.bindings["herdr"]
    attached_id = bound_attached.handle.bindings["herdr"]
    assert created_id and attached_id and created_id != attached_id

    # E34: a repeated bind returns the same reference; E35: worktree list re-derives it.
    assert service.bind(bound_created.handle).handle.bindings["herdr"] == created_id
    assert binding.last_opened is not None and binding.last_opened.already_open
    assert binding.derive(created.path) == created_id
    assert binding.derive(attached.path) == attached_id

    # E30: release, then the native remove; the branch survives and Herdr keeps no residue.
    for handle in (bound_created.handle, bound_attached.handle):
        service.remove(handle, force=False)
        assert not handle.path.exists()
    remaining = {record.workspace_id for record in binding.workspaces()}
    assert created_id not in remaining and attached_id not in remaining
    assert binding.orphans() == ()
    branches = _git("branch", "--list", "wt/bead/issue/*", cwd=main).stdout
    assert "wt/bead/issue/a-1" in branches and "wt/bead/issue/a-2" in branches


def test_real_herdr_unavailable_at_bind_keeps_the_native_worktree(herdr_server):
    """E33 + E34 against the real server: bind fails while the server is stopped, the native
    worktree is untouched, and the same `open --path` re-binds it once Herdr is back."""
    root, herdr, session = herdr_server
    main = root / "repo"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
        cwd=main,
    )
    path = root / "wts" / "a-4"
    _git("worktree", "add", "-q", "-b", "wt/bead/issue/a-4", str(path), cwd=main)
    binding = HerdrWorkspaceBinding(herdr)
    service = WorktreeLifecycleService(
        manager=NativeGitWorktreeManager(_run_git), bindings=(binding,)
    )
    handle = WorktreeHandle(path.name, main, path, "wt/bead/issue/a-4")

    herdr("server", "stop")
    deadline = time.monotonic() + 15
    while herdr("status").returncode == 0 and time.monotonic() < deadline:
        time.sleep(0.2)
    down = service.bind(handle)

    assert [gap.code for gap in down.gaps] == ["server_not_running"]
    assert path.is_dir() and "a-4" in _git("worktree", "list", cwd=main).stdout

    restarted = subprocess.Popen(
        ["herdr", "--session", session, "server"],
        cwd=root,
        env={**os.environ, "XDG_CONFIG_HOME": str(root / "x")},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 20
        while herdr("status").returncode != 0:
            assert time.monotonic() < deadline, "restarted herdr server did not come up"
            time.sleep(0.2)
        first = service.bind(handle)
        again = service.bind(first.handle)
        assert first.gaps == again.gaps == ()
        assert first.handle.bindings == again.handle.bindings
        service.remove(first.handle, force=False)
    finally:
        herdr("server", "stop")
        try:
            restarted.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(restarted.pid, 9)
            restarted.wait(timeout=5)
